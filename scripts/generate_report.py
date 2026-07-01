"""调用 AI API 把一周/一月的生活流水账生成微信读书风格的 HTML 报告。

设计要点：
  - 默认使用 DeepSeek OpenAI 兼容接口（deepseek-v4-pro + JSON 输出）。
  - 仍保留 Anthropic 旧接口作为可选回退。
  - 每人产出一份结构化 JSON（关键词/金句/行为分类统计/叙事），再用固定模板渲染成一屏。

用法：
  python scripts/generate_report.py --type weekly  --target 5.18-5.22
  python scripts/generate_report.py --type monthly --target 2026-05
  python scripts/generate_report.py --type weekly                 # 自动选最近一周
  python scripts/generate_report.py --type weekly --data-file wk.json   # 用本地数据，不连飞书
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
from pathlib import Path
from typing import List

from pydantic import BaseModel, Field

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

import anthropic

# 允许作为脚本直接运行时 import 同目录的 fetch_sheet
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_sheet  # noqa: E402

ANTHROPIC_MODEL = "claude-opus-4-7"
DEEPSEEK_MODEL = "deepseek-v4-pro"
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR = ROOT / "reports"
PEOPLE = list(fetch_sheet.PERSON_COLUMNS.keys())  # 黄欣迪 / 刘嘉晨 / 王江楠

# 每人配色（背景渐变 + 强调色），渲染时按顺序取用
THEMES = [
    {"from": "#1a1033", "to": "#3a1c5c", "accent": "#ff8fb1", "soft": "#ffd6e5"},
    {"from": "#06141f", "to": "#0f3a4d", "accent": "#5fd0c5", "soft": "#bff0ea"},
    {"from": "#1f1206", "to": "#4a2c0f", "accent": "#ffb454", "soft": "#ffe2b3"},
    {"from": "#10131f", "to": "#2a2f4f", "accent": "#8fa6ff", "soft": "#cfd8ff"},
]


# ---- 结构化输出的数据模型 -------------------------------------------------

class Highlight(BaseModel):
    quote: str = Field(description="直接引用原文里的金句/名场面")
    note: str = Field(description="一句带调侃又温暖的点评")


class ActivityStat(BaseModel):
    category: str = Field(description="行为分类：吃饭/工作/社交/娱乐/休息/其他 之一")
    count: int = Field(description="本期归入该类的记录条数")
    examples: List[str] = Field(description="0-3 个该类的代表性原文片段")


class PersonReport(BaseModel):
    name: str
    chapter_title: str = Field(description="给 ta 这一期起的个性化章节标题，6-14 字")
    tagline: str = Field(description="一句话副标题/年度关键词式的概括")
    keywords: List[str] = Field(description="6-10 个关键词，用于关键词云")
    most_active_time: str = Field(description="最常出现的时段，并用一句话解读")
    activity_stats: List[ActivityStat] = Field(description="按行为分类的统计，按 count 从多到少")
    highlights: List[Highlight] = Field(description="3-5 个高亮时刻")
    narrative: List[str] = Field(description="2-4 段叙事，有洞察、有温度、带点调侃")
    closing: str = Field(description="一句温暖的收尾寄语")


SENTENCE_ENDINGS = "。！？!?…」』】）》）”\"'"


STYLE_GUIDE = """你是一个擅长写「有温度的个人生活记录总结」的助手。
用户会给你某个人在一段时间内的生活流水账（按日期+时段记录的零碎文字），
请为指定的某个人生成一份有趣、有洞察、带点调侃又温暖的总结，
风格参考「微信读书年度报告」：用数据说话、提炼关键词、高亮金句、做行为分类。

写作要求：
1. 语气：像一个懂 ta 的好朋友，温暖、俏皮、偶尔吐槽，但不刻薄、不油腻。第二人称「你」来写。
2. 关键词（keywords）：从流水账里提炼 6-10 个真正高频或有代表性的词/短语，可用于词云。
3. 金句（highlights）：必须 **直接引用原文**，挑最好笑/最戳/最典型的，note 给一句点评。
4. 行为分类（activity_stats）：把记录归入「吃饭/工作/社交/娱乐/休息/其他」，统计条数，给代表性原文片段；按条数从多到少排序；没有的分类不要硬凑。
5. 最常时段（most_active_time）：看哪个时段记录最多，并解读 ta 的作息/状态。
6. 叙事（narrative）：2-4 段，串起这一期的故事线与小情绪，可以引用与同伴的互动。
   明细里带「💬 评论互动」的行，是其他人在这条记录下的真实评论对话（已标明每句谁说的），
   是绝佳的「名场面」和叙事素材，可适当引用，体现三人之间的互动与关系。
7. 收尾（closing）：一句温暖的寄语。
8. 只基于提供的真实记录，不要编造没发生的事；记录稀少时就如实写「这周话不多」之类，依然写得可爱。
9. 全部使用简体中文。"""


# ---- 调模型 --------------------------------------------------------------

def _format_entries(data: dict) -> str:
    """把结构化数据拍平成给模型看的纯文本（稳定、可缓存）。"""
    lines = []
    period = data.get("week") or data.get("month") or "本期"
    lines.append(f"【时间范围】{period}")
    if data.get("weeks"):
        lines.append(f"【包含周】{', '.join(data['weeks'])}")
    lines.append(f"【记录人】{', '.join(PEOPLE)}")
    lines.append("【流水账明细】（格式：日期 | 时段 | 姓名：内容）")
    for e in data["entries"]:
        prefix = e.get("_week", "")
        day = e.get("date", "")
        time = e.get("time", "")
        head = f"{prefix} {day} {time}".strip()
        for name in PEOPLE:
            text = (e.get(name) or "").strip()
            if text:
                lines.append(f"{head} | {name}：{text}")
                # 紧跟着输出朋友们在这条记录下的评论互动
                for cm in e.get("comments", []):
                    if cm.get("on") != name:
                        continue
                    conv = " / ".join(f"{r['author']}：{r['text']}" for r in cm["conversation"])
                    lines.append(f"{head} |   💬 评论互动：{conv}")
    return "\n".join(lines)


def _model_schema() -> dict:
    if hasattr(PersonReport, "model_json_schema"):
        return PersonReport.model_json_schema()
    return PersonReport.schema()


def _validate_person_report_obj(obj: dict) -> PersonReport:
    if hasattr(PersonReport, "model_validate"):
        return PersonReport.model_validate(obj)
    return PersonReport.parse_obj(obj)


def _loads_model_json(content: str) -> dict:
    text = (content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"模型没有返回合法 JSON：{e}") from e


def generate_person_report_anthropic(client: anthropic.Anthropic, name: str,
                                     data_text: str, period_label: str) -> PersonReport:
    """为单个人生成结构化报告。system 段（风格指南+全量数据）可被缓存复用。"""
    system = [
        {"type": "text", "text": STYLE_GUIDE},
        {
            "type": "text",
            "text": f"以下是「{period_label}」全员的生活流水账，供你理解上下文：\n\n{data_text}",
            # 稳定前缀打缓存点：三个人的调用共享，命中后按 ~0.1x 计费
            "cache_control": {"type": "ephemeral"},
        },
    ]
    resp = client.messages.parse(
        model=os.environ.get("ANTHROPIC_MODEL", ANTHROPIC_MODEL),
        # adaptive thinking 与 JSON 输出共享 max_tokens 预算。effort=high（默认）在
        # 记录多的周会吃掉两三万 token 的「思考」，若上限太低，结尾字段(narrative/closing)
        # 会被截断——messages.parse 仍会受限解码出一个「合法但残缺」的对象照样渲染。
        # 调到 64000 给思考+输出都留足空间；这是单人报告，实际输出很短，预算只是天花板。
        max_tokens=64000,
        # max_tokens 调高后 SDK 默认会因「非流式可能超 10 分钟」直接报错；
        # 显式传 timeout 即跳过该估算。单人报告实际输出很短，15 分钟绰绰有余。
        timeout=900,
        thinking={"type": "adaptive"},  # 4.7 默认 effort=high
        system=system,
        messages=[{
            "role": "user",
            "content": (
                f"请只为「{name}」生成这一期的生活报告。"
                f"聚焦 ta 的记录，可适当引用与其他人的互动；"
                f"name 字段填「{name}」。"
            ),
        }],
        output_format=PersonReport,
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError(f"模型拒绝为 {name} 生成内容：{resp.stop_details}")
    # 截断防护：thinking 吃光预算时输出会在尾部字段退化成 test/x/, 之类的残缺值，
    # parse 仍会“凑”出一个对象。只有正常收尾(end_turn)才接受，否则中止而非渲染垃圾。
    if resp.stop_reason != "end_turn":
        raise RuntimeError(
            f"为 {name} 生成的内容未正常结束（stop_reason={resp.stop_reason}），"
            f"疑似被截断，已中止以免渲染残缺内容；可调大 max_tokens 后重试。"
        )
    # 内容级截断防护：思考吃光预算时，messages.parse 会受限解码出「合法但残缺」的
    # 对象（stop_reason 仍可能是 end_turn），表现为尾部字段空/半句话。stop_reason
    # 检查挡不住这种，必须实打实校验内容，残缺就中止而非把半截报告发出去。
    report = resp.parsed_output
    missing = []
    if not (report.closing or "").strip():
        missing.append("closing")
    if not [p for p in report.narrative if (p or "").strip()]:
        missing.append("narrative")
    if not [h for h in report.highlights if (h.quote or "").strip()]:
        missing.append("highlights")
    if missing:
        raise RuntimeError(
            f"为 {name} 生成的内容字段残缺（{', '.join(missing)} 为空），"
            f"疑似思考吃光 token 预算导致截断，已中止以免渲染残缺内容；"
            f"可调大 max_tokens 或降低 effort 后重试。"
        )
    _validate_report_content(name, report)
    u = resp.usage
    print(
        f"  [{name}] tokens in={u.input_tokens} "
        f"cache_write={getattr(u, 'cache_creation_input_tokens', 0)} "
        f"cache_read={getattr(u, 'cache_read_input_tokens', 0)} "
        f"out={u.output_tokens}",
        file=sys.stderr,
    )
    return report


def generate_person_report_deepseek(client, name: str,
                                    data_text: str, period_label: str) -> PersonReport:
    schema = json.dumps(_model_schema(), ensure_ascii=False)
    thinking = os.environ.get("DEEPSEEK_THINKING", "disabled").strip()
    extra_body = {"thinking": {"type": thinking}}
    if thinking == "enabled":
        extra_body["reasoning_effort"] = os.environ.get("DEEPSEEK_REASONING_EFFORT", "high")
    messages = [
        {
            "role": "system",
            "content": (
                f"{STYLE_GUIDE}\n\n"
                f"以下是「{period_label}」全员的生活流水账，供你理解上下文：\n\n{data_text}"
            ),
        },
        {
            "role": "user",
            "content": (
                f"请只为「{name}」生成这一期的生活报告。"
                f"聚焦 ta 的记录，可适当引用与其他人的互动；name 字段填「{name}」。\n\n"
                "只输出一个 JSON object，不要输出 Markdown，不要解释。"
                "JSON 必须符合这个 schema：\n"
                f"{schema}"
            ),
        },
    ]
    resp = client.chat.completions.create(
        model=os.environ.get("DEEPSEEK_MODEL", DEEPSEEK_MODEL),
        messages=messages,
        response_format={"type": "json_object"},
        max_tokens=int(os.environ.get("DEEPSEEK_MAX_TOKENS", "16000")),
        extra_body=extra_body,
        timeout=900,
    )
    choice = resp.choices[0]
    if choice.finish_reason not in {None, "stop"}:
        raise RuntimeError(
            f"为 {name} 生成的内容未正常结束（finish_reason={choice.finish_reason}），"
            f"疑似被截断，已中止以免渲染残缺内容。"
        )
    report = _validate_person_report_obj(_loads_model_json(choice.message.content))
    _validate_report_content(name, report)
    usage = getattr(resp, "usage", None)
    if usage:
        print(
            f"  [{name}] deepseek tokens in={getattr(usage, 'prompt_tokens', 0)} "
            f"out={getattr(usage, 'completion_tokens', 0)}",
            file=sys.stderr,
        )
    return report


def choose_ai_provider() -> str:
    provider = os.environ.get("AI_PROVIDER", "").strip().lower()
    if provider:
        return provider
    if os.environ.get("DEEPSEEK_API_KEY", "").strip():
        return "deepseek"
    return "anthropic"


def make_ai_client(provider: str):
    if provider == "deepseek":
        from openai import OpenAI

        api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("缺少 DEEPSEEK_API_KEY")
        return OpenAI(
            api_key=api_key,
            base_url=os.environ.get("DEEPSEEK_BASE_URL", DEEPSEEK_BASE_URL).strip(),
        )
    if provider == "anthropic":
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("缺少 ANTHROPIC_API_KEY")
        return anthropic.Anthropic(api_key=api_key)
    raise RuntimeError(f"不支持的 AI_PROVIDER：{provider}")


def _looks_truncated_sentence(text: str) -> bool:
    s = (text or "").strip()
    if not s:
        return True
    if s[-1] in SENTENCE_ENDINGS:
        return False
    # 这些字段应该是完整句子/段落；若以普通文字结尾但没有句末标点，
    # 在实践里通常意味着模型在尾部被截断了。
    return bool(re.search(r"[\u4e00-\u9fffA-Za-z0-9]$", s))


def _validate_report_content(name: str, report: PersonReport) -> None:
    issues = []
    narrative_parts = [p.strip() for p in report.narrative if (p or "").strip()]
    if len(narrative_parts) < 2:
        issues.append("narrative 段落少于 2 段")
    for idx, part in enumerate(narrative_parts, start=1):
        if _looks_truncated_sentence(part):
            issues.append(f"narrative 第 {idx} 段疑似半句截断")
    if _looks_truncated_sentence(report.closing):
        issues.append("closing 疑似半句截断")
    for idx, h in enumerate(report.highlights, start=1):
        if _looks_truncated_sentence(h.note):
            issues.append(f"highlight {idx} note 疑似半句截断")
    if issues:
        raise RuntimeError(
            f"为 {name} 生成的内容疑似被截断（{'；'.join(issues)}），"
            f"已中止以免渲染半截报告；请重试生成。"
        )


# ---- HTML 渲染 -----------------------------------------------------------

def _esc(s: str) -> str:
    return html.escape(s or "")


def _render_person_section(idx: int, r: PersonReport) -> str:
    theme = THEMES[idx % len(THEMES)]
    kw_chips = "".join(
        f'<span class="kw" style="--d:{i * 0.05:.2f}s">{_esc(k)}</span>'
        for i, k in enumerate(r.keywords)
    )
    max_count = max((s.count for s in r.activity_stats), default=1) or 1
    stat_rows = "".join(
        f'''<div class="stat">
              <div class="stat-top"><span>{_esc(s.category)}</span><b>{s.count}</b></div>
              <div class="bar"><i style="width:{(s.count / max_count) * 100:.0f}%"></i></div>
              <div class="ex">{_esc("、".join(s.examples[:3]))}</div>
            </div>'''
        for s in r.activity_stats
    )
    highlights = "".join(
        f'''<blockquote class="hl">
              <p class="q">“{_esc(h.quote)}”</p>
              <p class="n">{_esc(h.note)}</p>
            </blockquote>'''
        for h in r.highlights
    )
    narrative = "".join(f"<p>{_esc(par)}</p>" for par in r.narrative)
    return f'''
    <section class="screen" style="--from:{theme['from']};--to:{theme['to']};--accent:{theme['accent']};--soft:{theme['soft']}">
      <div class="inner">
        <p class="who">{_esc(r.name)} · 第 {idx + 1} 章</p>
        <h2 class="title">{_esc(r.chapter_title)}</h2>
        <p class="tagline">{_esc(r.tagline)}</p>

        <div class="kw-cloud">{kw_chips}</div>

        <div class="time-card">🕘 最常出没：<b>{_esc(r.most_active_time)}</b></div>

        <h3>这一期都在忙啥</h3>
        <div class="stats">{stat_rows}</div>

        <h3>名场面</h3>
        <div class="highlights">{highlights}</div>

        <h3>写给你的话</h3>
        <div class="narrative">{narrative}</div>

        <p class="closing">{_esc(r.closing)}</p>
      </div>
    </section>'''


def render_html(period_label: str, subtitle: str, reports: List[PersonReport]) -> str:
    cover_kw = "".join(
        f'<span>{_esc(k)}</span>'
        for r in reports for k in r.keywords[:3]
    )
    sections = "".join(_render_person_section(i, r) for i, r in enumerate(reports))
    dots = "".join(f'<button data-i="{i}"></button>' for i in range(len(reports) + 1))
    generated = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{_esc(period_label)} 生活报告</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  html, body {{ height:100%; }}
  body {{
    font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
    color:#fff; background:#0b0b12; overflow:hidden;
  }}
  #deck {{ height:100vh; overflow-y:scroll; scroll-snap-type:y mandatory; scroll-behavior:smooth; }}
  #deck::-webkit-scrollbar {{ display:none; }}
  .screen {{
    min-height:100vh; scroll-snap-align:start;
    display:flex; align-items:center; justify-content:center;
    padding:8vh 24px; background:linear-gradient(160deg,var(--from),var(--to));
  }}
  .inner {{ width:100%; max-width:680px; }}
  /* 封面 */
  .cover {{ --from:#0b0b12; --to:#241638; --accent:#ff8fb1; text-align:center; }}
  .cover .big {{ font-size:clamp(40px,11vw,84px); font-weight:800; line-height:1.05; letter-spacing:2px; }}
  .cover .sub {{ margin-top:18px; font-size:18px; opacity:.8; }}
  .cover .cloud {{ margin:36px auto 0; display:flex; flex-wrap:wrap; gap:10px; justify-content:center; max-width:480px; }}
  .cover .cloud span {{ font-size:14px; padding:6px 12px; border:1px solid rgba(255,255,255,.25); border-radius:999px; opacity:.85; }}
  .cover .hint {{ margin-top:48px; font-size:13px; opacity:.55; animation:bob 1.6s infinite; }}
  @keyframes bob {{ 0%,100%{{transform:translateY(0)}} 50%{{transform:translateY(8px)}} }}
  /* 人物页 */
  .who {{ font-size:14px; letter-spacing:3px; color:var(--soft); opacity:.85; }}
  .title {{ font-size:clamp(30px,7vw,52px); font-weight:800; margin:6px 0 10px; }}
  .tagline {{ font-size:17px; color:var(--soft); opacity:.9; margin-bottom:24px; }}
  h3 {{ font-size:15px; letter-spacing:2px; margin:30px 0 12px; color:var(--accent); }}
  .kw-cloud {{ display:flex; flex-wrap:wrap; gap:9px; }}
  .kw {{ font-size:14px; padding:6px 13px; border-radius:999px;
         background:rgba(255,255,255,.08); border:1px solid rgba(255,255,255,.15);
         animation:pop .5s var(--d) both; }}
  @keyframes pop {{ from{{opacity:0; transform:scale(.8)}} to{{opacity:1; transform:scale(1)}} }}
  .time-card {{ margin-top:22px; padding:14px 16px; border-radius:14px;
                background:rgba(255,255,255,.07); font-size:15px; }}
  .time-card b {{ color:var(--accent); }}
  .stats {{ display:flex; flex-direction:column; gap:14px; }}
  .stat-top {{ display:flex; justify-content:space-between; font-size:15px; }}
  .stat-top b {{ color:var(--accent); }}
  .bar {{ height:8px; border-radius:999px; background:rgba(255,255,255,.12); margin:6px 0 4px; overflow:hidden; }}
  .bar i {{ display:block; height:100%; background:var(--accent); border-radius:999px; }}
  .ex {{ font-size:12px; opacity:.6; }}
  .highlights {{ display:flex; flex-direction:column; gap:14px; }}
  .hl {{ padding:16px 18px; border-left:3px solid var(--accent);
         background:rgba(255,255,255,.06); border-radius:0 12px 12px 0; }}
  .hl .q {{ font-size:17px; line-height:1.5; }}
  .hl .n {{ font-size:13px; opacity:.7; margin-top:8px; }}
  .narrative p {{ font-size:15px; line-height:1.85; opacity:.92; margin-bottom:14px; }}
  .closing {{ margin-top:30px; font-size:18px; font-style:italic; color:var(--soft); }}
  /* 进度点 */
  #dots {{ position:fixed; right:14px; top:50%; transform:translateY(-50%);
           display:flex; flex-direction:column; gap:10px; z-index:10; }}
  #dots button {{ width:8px; height:8px; border:0; border-radius:50%;
                  background:rgba(255,255,255,.3); cursor:pointer; padding:0; }}
  #dots button.on {{ background:#fff; transform:scale(1.4); }}
</style>
</head>
<body>
<div id="deck">
  <section class="screen cover">
    <div class="inner">
      <div class="big">{_esc(period_label)}<br>生活报告</div>
      <div class="sub">{_esc(subtitle)}</div>
      <div class="cloud">{cover_kw}</div>
      <div class="hint">↓ 滑动 / 方向键查看每个人</div>
    </div>
  </section>
  {sections}
</div>
<div id="dots">{dots}</div>
<script>
  const deck = document.getElementById('deck');
  const screens = [...document.querySelectorAll('.screen')];
  const dots = [...document.querySelectorAll('#dots button')];
  const io = new IntersectionObserver((es) => {{
    es.forEach(e => {{ if (e.isIntersecting) {{
      const i = screens.indexOf(e.target);
      dots.forEach((d,j)=>d.classList.toggle('on', j===i));
    }} }});
  }}, {{threshold:0.6}});
  screens.forEach(s => io.observe(s));
  dots.forEach(d => d.onclick = () => screens[+d.dataset.i].scrollIntoView());
  let i = 0;
  addEventListener('keydown', e => {{
    if (e.key==='ArrowDown'||e.key==='ArrowRight') i=Math.min(i+1,screens.length-1);
    else if (e.key==='ArrowUp'||e.key==='ArrowLeft') i=Math.max(i-1,0);
    else return;
    screens[i].scrollIntoView();
  }});
</script>
<!-- generated {generated} -->
</body>
</html>'''


# ---- manifest（供 index.html 自动列出）-----------------------------------

def update_manifest(entry: dict) -> None:
    path = REPORTS_DIR / "manifest.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = {"reports": []}
    reports = [r for r in data.get("reports", []) if r.get("path") != entry["path"]]
    reports.append(entry)
    reports.sort(key=lambda r: (r.get("date", ""), r.get("path", "")), reverse=True)
    data["reports"] = reports
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ---- 文件名/日期推导 ------------------------------------------------------

def week_to_date(week_title: str, year: int) -> str:
    """'5.18-5.22' + year -> '2026-05-18'，解析失败回退今天。"""
    m = re.match(r"\s*(\d{1,2})\s*[.．]\s*(\d{1,2})", week_title)
    if m:
        return f"{year}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return dt.date.today().isoformat()


def auto_pick_week() -> str:
    """自动选「最近且不晚于今天」的周 Tab，没有则取最新一周。

    跨所有数据源（sheets.json 里的全部表格）扫描，这样新建的月度表里的最新周也能被选到。
    """
    token = fetch_sheet.get_tenant_token()
    today = dt.date.today()
    candidates = []
    seen = set()
    for _src, ss in fetch_sheet.iter_sources(token):
        for s in fetch_sheet.list_sheets(token, ss):
            title = s["title"].strip()
            if title in seen:
                continue
            key = fetch_sheet._tab_start_key(title)
            if key == (0, 0):
                continue
            try:
                d = dt.date(today.year, key[0], key[1])
            except ValueError:
                continue
            seen.add(title)
            candidates.append((d, title))
    if not candidates:
        raise fetch_sheet.FeishuError("没有可识别日期的周 Tab")
    past = [c for c in candidates if c[0] <= today]
    chosen = max(past or candidates, key=lambda c: c[0])
    return chosen[1]


# ---- 主流程 --------------------------------------------------------------

def build(report_type: str, target: str | None, year: int,
          data_file: str | None) -> dict:
    # 1) 取数据
    if data_file:
        data = json.loads(Path(data_file).read_text(encoding="utf-8"))
    elif report_type == "weekly":
        target = target or auto_pick_week()
        data = fetch_sheet.fetch_week(target)
    else:  # monthly
        target = target or dt.date.today().strftime("%Y-%m")
        y, m = target.split("-")
        data = fetch_sheet.fetch_month(int(y), int(m))

    period_label = data.get("week") or data.get("month") or target or "本期"
    n_entries = len(data.get("entries", []))
    if n_entries == 0:
        raise RuntimeError(f"「{period_label}」没有读到任何记录，已中止生成。")

    # 1.5) 给记录挂上飞书评论互动（增强项，失败不阻断报告生成）
    if not data_file:
        try:
            import fetch_comments
            token = fetch_sheet.get_tenant_token()
            n_cm = fetch_comments.attach_comments(token, data)
            print(f"已挂载 {n_cm} 条评论互动到记录", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            print(f"[告警] 评论挂载失败，跳过：{e}", file=sys.stderr)

    # 2) 调模型。默认优先 DeepSeek；也可显式 AI_PROVIDER=anthropic 回退旧接口。
    provider = choose_ai_provider()
    client = make_ai_client(provider)
    data_text = _format_entries(data)
    print(
        f"为「{period_label}」生成报告（{n_entries} 条记录，{len(PEOPLE)} 人，provider={provider}）…",
        file=sys.stderr,
    )
    reports: List[PersonReport] = []
    for name in PEOPLE:
        if provider == "deepseek":
            reports.append(generate_person_report_deepseek(client, name, data_text, period_label))
        else:
            reports.append(generate_person_report_anthropic(client, name, data_text, period_label))

    # 3) 渲染 & 落盘
    if report_type == "weekly":
        date_str = week_to_date(period_label, year)
        out_rel = f"reports/weekly/{date_str}.html"
        subtitle = f"{period_label} · 共 {n_entries} 条记录"
        manifest_date = date_str
    else:
        date_str = period_label  # YYYY-MM
        out_rel = f"reports/monthly/{date_str}.html"
        subtitle = f"{period_label} · {len(data.get('weeks', []))} 周 · 共 {n_entries} 条记录"
        manifest_date = f"{date_str}-01"

    out_path = ROOT / out_rel
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_html(period_label, subtitle, reports), encoding="utf-8")
    print(f"已写入 {out_path}", file=sys.stderr)

    # 4) 更新导航 manifest
    update_manifest({
        "type": report_type,
        "title": f"{period_label} 生活报告",
        "period": period_label,
        "path": out_rel,
        "date": manifest_date,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
    })
    return {"path": out_rel, "title": f"{period_label} 生活报告", "period": period_label}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="用 AI 模型生成生活周报/月报 HTML")
    p.add_argument("--type", choices=["weekly", "monthly"], default="weekly")
    p.add_argument("--target", help="周 Tab 名(5.18-5.22) 或 月份(2026-05)；留空自动")
    p.add_argument("--year", type=int, default=dt.date.today().year, help="周报文件名用的年份")
    p.add_argument("--data-file", help="改用本地 JSON（fetch_sheet 的输出），跳过飞书")
    args = p.parse_args(argv)

    try:
        result = build(args.type, args.target, args.year, args.data_file)
    except Exception as e:  # noqa: BLE001
        print(f"[错误] {e}", file=sys.stderr)
        cause = e.__cause__ or e.__context__
        if cause is not None:
            print(f"[底层原因] {type(cause).__name__}: {cause}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1

    # 给后续步骤（飞书通知）用：写到 .last_report 和 GITHUB_OUTPUT
    (ROOT / ".last_report").write_text(result["path"], encoding="utf-8")
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as f:
            f.write(f"report_path={result['path']}\n")
            f.write(f"report_title={result['title']}\n")
    print(result["path"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
