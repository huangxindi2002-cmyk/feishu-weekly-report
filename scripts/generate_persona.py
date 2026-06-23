"""一次性任务：读取「全部历史记录」，为每个人生成一份「性格 & 生活方式画像」。

和周报/月报不同——这里看的是跨越数月的全部流水账，提炼的是长期的性格底色、
作息与生活方式、口头禅、人物关系，做成主流 App「年度报告」式、轻松诙谐的人格画像。

设计要点：
  - 模型 claude-opus-4-7，adaptive thinking，结构化输出（messages.parse + Pydantic）。
  - Prompt caching：把「风格指南 + 全部历史数据」放进 system 并打 cache_control，
    三个人的生成调用共享这一段长前缀，第 1 次写缓存、后 2 次命中读缓存（约 0.1x 成本）。
  - 每人产出一份结构化 JSON（人格原型/光谱维度/生活方式统计/口头禅/名场面/关系网/深度剖析），
    再用滑动卡片模板渲染成一个自包含的 HTML 文件。

用法：
  python scripts/generate_persona.py                       # 连飞书读全部 + 生成
  python scripts/generate_persona.py --data-file all.json  # 用本地 JSON，不连飞书（省钱调样式）
  python scripts/generate_persona.py --out ~/Desktop/性格画像报告.html
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import os
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_sheet  # noqa: E402

MODEL = "claude-opus-4-7"
PEOPLE = list(fetch_sheet.PERSON_COLUMNS.keys())  # 黄欣迪 / 刘嘉晨 / 王江楠
DEFAULT_OUT = Path.home() / "Desktop" / "性格画像报告.html"

# 每人配色（背景渐变 + 强调色），渲染时按顺序取用
THEMES = [
    {"from": "#1a1033", "to": "#3a1c5c", "accent": "#ff8fb1", "soft": "#ffd6e5"},
    {"from": "#06141f", "to": "#0f3a4d", "accent": "#5fd0c5", "soft": "#bff0ea"},
    {"from": "#1f1206", "to": "#4a2c0f", "accent": "#ffb454", "soft": "#ffe2b3"},
    {"from": "#10131f", "to": "#2a2f4f", "accent": "#8fa6ff", "soft": "#cfd8ff"},
]


# ---- 结构化输出的数据模型 -------------------------------------------------

class Dimension(BaseModel):
    name: str = Field(description="维度名，2-4 字，如「作息」「社交」「情绪」「行动力」「干饭欲」")
    left_label: str = Field(description="光谱左端标签，如「早睡党」「独行侠」「i 人」")
    right_label: str = Field(description="光谱右端标签，如「熬夜怪」「社牛」「e 人」")
    value: int = Field(description="0-100，ta 在这条光谱上的位置，越大越偏右端")
    blurb: str = Field(description="一句俏皮的解读，给出为什么打这个分（可引用记录细节）")


class LifestyleStat(BaseModel):
    category: str = Field(description="生活方式分类：吃饭/工作/社交/娱乐/休息/运动/其他 之一")
    count: int = Field(description="全部记录里归入该类的条数")
    examples: List[str] = Field(description="0-3 个该类代表性原文片段")


class Catchphrase(BaseModel):
    phrase: str = Field(description="高频口头禅 / 标志性表达，尽量贴近原文")
    note: str = Field(description="一句带调侃的点评")


class SignatureMoment(BaseModel):
    quote: str = Field(description="直接引用原文里最好笑/最戳/最能代表 ta 的名场面")
    note: str = Field(description="一句带调侃又温暖的点评")


class Relationship(BaseModel):
    who: str = Field(description="另外两人之一的姓名")
    label: str = Field(description="给这段关系起的趣味标签，如「饭搭子」「损友」「互相监督」")
    note: str = Field(description="一句话点评 ta 俩的相处模式，可引用互动记录")


class PersonaReport(BaseModel):
    name: str
    archetype: str = Field(description="人格原型标题，6-14 字，如「深夜放毒型干饭选手」")
    tagline: str = Field(description="一句话副标题/人格关键词式概括")
    mbti_tag: str = Field(description="一个趣味人格标签，可玩 MBTI 梗，如「ENFP·人间充电宝」")
    keywords: List[str] = Field(description="8-12 个真正高频或代表性的词/短语，用于词云")
    dimensions: List[Dimension] = Field(description="4-6 条人格光谱维度")
    lifestyle_stats: List[LifestyleStat] = Field(description="按生活方式分类的统计，按 count 从多到少，没有的不硬凑")
    catchphrases: List[Catchphrase] = Field(description="2-4 个口头禅/标志性表达")
    signature_moments: List[SignatureMoment] = Field(description="3-5 个名场面")
    relationships: List[Relationship] = Field(description="与另外两人的关系点评（数据少的人可只写有互动的）")
    analysis: List[str] = Field(description="2-4 段深度性格剖析，有洞察、有温度、带点调侃")
    closing: str = Field(description="一句温暖的收尾寄语")


STYLE_GUIDE = """你是一个擅长写「有温度的人格画像」的助手。
用户会给你某个人横跨数月的全部生活流水账（按日期+时段记录的零碎文字），
请基于这份长期记录，为指定的某个人生成一份「性格 & 生活方式画像」，
风格参考主流 App 的「年度报告」：用数据说话、提炼关键词、画人格光谱、做行为分类，整体轻松诙谐。

写作要求：
1. 语气：像一个非常懂 ta 的好朋友，温暖、俏皮、偶尔吐槽，但不刻薄、不油腻。第二人称「你」来写。
2. 这是长期画像，不是某一周总结：要看跨周的稳定规律（作息、习惯、性格底色、情绪走向、和谁互动多），
   而不是流水复述某天发生了什么。
3. 人格原型/标签（archetype / mbti_tag）：根据真实记录提炼，要让 ta 看了会心一笑、觉得「确实是我」。
4. 关键词（keywords）：从全部记录里提炼 8-12 个真正高频或有代表性的词/短语，可用于词云。
5. 人格光谱（dimensions）：选 4-6 个最能刻画 ta 的维度，每条给一对对立标签和 0-100 的位置，
   blurb 用记录细节解释为什么这么打分；维度要因人而异，别千篇一律。
6. 生活方式（lifestyle_stats）：把记录归入「吃饭/工作/社交/娱乐/休息/运动/其他」，统计条数、给代表片段，
   按条数从多到少；没有的分类不要硬凑。
7. 口头禅（catchphrases）：找 ta 反复出现的表达/句式/语气词，尽量贴近原文。
8. 名场面（signature_moments）：必须**直接引用原文**，挑最好笑/最戳/最能代表 ta 的。
9. 关系网（relationships）：分析 ta 和另外两人的互动与相处模式；如果某人记录极少、几乎没互动，可省略。
10. 深度剖析（analysis）：2-4 段，串起 ta 的性格底色、生活节奏与小情绪，可引用与同伴的互动。
11. 收尾（closing）：一句温暖的寄语。
12. 只基于提供的真实记录，绝不编造没发生的事。某人记录很少（比如只有寥寥几条）时，
    就如实、可爱地写「你是个话不多的神秘人」之类，光谱和统计也按现有的少量信息谨慎给出，不要硬编。
13. 全部使用简体中文。"""


# ---- 调 Claude -----------------------------------------------------------

def _format_entries(data: dict) -> str:
    """把全部记录拍平成给模型看的纯文本（稳定、可缓存）。"""
    lines = []
    rng = data.get("range") or "全部"
    lines.append(f"【时间范围】{rng}")
    if data.get("weeks"):
        lines.append(f"【包含周】{', '.join(data['weeks'])}")
    lines.append(f"【记录人】{', '.join(PEOPLE)}")
    lines.append("【流水账明细】（格式：周 日期 时段 | 姓名：内容）")
    for e in data["entries"]:
        head = f"{e.get('_week', '')} {e.get('date', '')} {e.get('time', '')}".strip()
        for name in PEOPLE:
            text = (e.get(name) or "").strip()
            if text:
                lines.append(f"{head} | {name}：{text}")
    return "\n".join(lines)


def generate_person_persona(client: anthropic.Anthropic, name: str,
                            data_text: str, range_label: str) -> PersonaReport:
    """为单个人生成结构化人格画像。system 段（风格指南+全量数据）可被缓存复用。"""
    system = [
        {"type": "text", "text": STYLE_GUIDE},
        {
            "type": "text",
            "text": f"以下是「{range_label}」全员的全部生活流水账，供你理解上下文：\n\n{data_text}",
            "cache_control": {"type": "ephemeral"},
        },
    ]
    resp = client.messages.parse(
        model=MODEL,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        system=system,
        messages=[{
            "role": "user",
            "content": (
                f"请只为「{name}」生成长期的「性格 & 生活方式画像」。"
                f"聚焦 ta 跨越整段时间的稳定规律与性格底色，可适当引用与其他人的互动；"
                f"name 字段填「{name}」。"
            ),
        }],
        output_format=PersonaReport,
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError(f"模型拒绝为 {name} 生成内容：{resp.stop_details}")
    u = resp.usage
    print(
        f"  [{name}] tokens in={u.input_tokens} "
        f"cache_write={getattr(u, 'cache_creation_input_tokens', 0)} "
        f"cache_read={getattr(u, 'cache_read_input_tokens', 0)} "
        f"out={u.output_tokens}",
        file=sys.stderr,
    )
    return resp.parsed_output


# ---- HTML 渲染 -----------------------------------------------------------

def _esc(s: str) -> str:
    return html.escape(s or "")


def _render_person_section(idx: int, r: PersonaReport) -> str:
    theme = THEMES[idx % len(THEMES)]
    kw_chips = "".join(
        f'<span class="kw" style="--d:{i * 0.04:.2f}s">{_esc(k)}</span>'
        for i, k in enumerate(r.keywords)
    )
    dim_rows = "".join(
        f'''<div class="dim">
              <div class="dim-name">{_esc(d.name)}</div>
              <div class="dim-bar">
                <span class="l">{_esc(d.left_label)}</span>
                <div class="track"><i style="left:{max(0, min(100, d.value))}%"></i></div>
                <span class="r">{_esc(d.right_label)}</span>
              </div>
              <div class="dim-blurb">{_esc(d.blurb)}</div>
            </div>'''
        for d in r.dimensions
    )
    max_count = max((s.count for s in r.lifestyle_stats), default=1) or 1
    stat_rows = "".join(
        f'''<div class="stat">
              <div class="stat-top"><span>{_esc(s.category)}</span><b>{s.count}</b></div>
              <div class="bar"><i style="width:{(s.count / max_count) * 100:.0f}%"></i></div>
              <div class="ex">{_esc("、".join(s.examples[:3]))}</div>
            </div>'''
        for s in r.lifestyle_stats
    )
    phrase_chips = "".join(
        f'''<div class="phrase">
              <p class="p">「{_esc(c.phrase)}」</p>
              <p class="pn">{_esc(c.note)}</p>
            </div>'''
        for c in r.catchphrases
    )
    moments = "".join(
        f'''<blockquote class="hl">
              <p class="q">“{_esc(m.quote)}”</p>
              <p class="n">{_esc(m.note)}</p>
            </blockquote>'''
        for m in r.signature_moments
    )
    rel_cards = "".join(
        f'''<div class="rel">
              <div class="rel-top"><span class="rel-who">{_esc(rel.who)}</span>
                <span class="rel-tag">{_esc(rel.label)}</span></div>
              <p class="rel-note">{_esc(rel.note)}</p>
            </div>'''
        for rel in r.relationships
    )
    analysis = "".join(f"<p>{_esc(par)}</p>" for par in r.analysis)
    rel_block = f'''<h3>关系网</h3><div class="rels">{rel_cards}</div>''' if r.relationships else ""
    phrase_block = f'''<h3>口头禅</h3><div class="phrases">{phrase_chips}</div>''' if r.catchphrases else ""
    return f'''
    <section class="screen" style="--from:{theme['from']};--to:{theme['to']};--accent:{theme['accent']};--soft:{theme['soft']}">
      <div class="inner">
        <p class="who">{_esc(r.name)} · 人格画像</p>
        <h2 class="title">{_esc(r.archetype)}</h2>
        <p class="tagline">{_esc(r.tagline)}</p>
        <div class="mbti">{_esc(r.mbti_tag)}</div>

        <div class="kw-cloud">{kw_chips}</div>

        <h3>人格光谱</h3>
        <div class="dims">{dim_rows}</div>

        <h3>生活方式构成</h3>
        <div class="stats">{stat_rows}</div>

        {phrase_block}

        <h3>名场面</h3>
        <div class="highlights">{moments}</div>

        {rel_block}

        <h3>深度剖析</h3>
        <div class="narrative">{analysis}</div>

        <p class="closing">{_esc(r.closing)}</p>
      </div>
    </section>'''


def render_html(range_label: str, subtitle: str, reports: List[PersonaReport]) -> str:
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
<title>性格 & 生活方式画像</title>
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
  .title {{ font-size:clamp(28px,7vw,50px); font-weight:800; margin:6px 0 10px; }}
  .tagline {{ font-size:17px; color:var(--soft); opacity:.9; margin-bottom:14px; }}
  .mbti {{ display:inline-block; font-size:14px; padding:7px 15px; border-radius:999px;
           background:var(--accent); color:#1a1020; font-weight:700; }}
  h3 {{ font-size:15px; letter-spacing:2px; margin:32px 0 14px; color:var(--accent); }}
  .kw-cloud {{ display:flex; flex-wrap:wrap; gap:9px; margin-top:24px; }}
  .kw {{ font-size:14px; padding:6px 13px; border-radius:999px;
         background:rgba(255,255,255,.08); border:1px solid rgba(255,255,255,.15);
         animation:pop .5s var(--d) both; }}
  @keyframes pop {{ from{{opacity:0; transform:scale(.8)}} to{{opacity:1; transform:scale(1)}} }}
  /* 人格光谱 */
  .dims {{ display:flex; flex-direction:column; gap:20px; }}
  .dim-name {{ font-size:14px; color:var(--soft); margin-bottom:8px; letter-spacing:1px; }}
  .dim-bar {{ display:flex; align-items:center; gap:10px; font-size:12px; opacity:.85; }}
  .dim-bar .l, .dim-bar .r {{ flex:0 0 auto; white-space:nowrap; }}
  .track {{ position:relative; flex:1; height:6px; border-radius:999px; background:rgba(255,255,255,.14); }}
  .track i {{ position:absolute; top:50%; width:18px; height:18px; border-radius:50%;
              background:var(--accent); transform:translate(-50%,-50%);
              box-shadow:0 0 0 4px rgba(255,255,255,.12); }}
  .dim-blurb {{ font-size:12.5px; opacity:.7; margin-top:7px; line-height:1.6; }}
  /* 生活方式统计 */
  .stats {{ display:flex; flex-direction:column; gap:14px; }}
  .stat-top {{ display:flex; justify-content:space-between; font-size:15px; }}
  .stat-top b {{ color:var(--accent); }}
  .bar {{ height:8px; border-radius:999px; background:rgba(255,255,255,.12); margin:6px 0 4px; overflow:hidden; }}
  .bar i {{ display:block; height:100%; background:var(--accent); border-radius:999px; }}
  .ex {{ font-size:12px; opacity:.6; }}
  /* 口头禅 */
  .phrases {{ display:flex; flex-direction:column; gap:12px; }}
  .phrase {{ padding:13px 16px; border-radius:12px; background:rgba(255,255,255,.06); }}
  .phrase .p {{ font-size:16px; font-weight:600; color:var(--soft); }}
  .phrase .pn {{ font-size:12.5px; opacity:.7; margin-top:5px; }}
  /* 名场面 */
  .highlights {{ display:flex; flex-direction:column; gap:14px; }}
  .hl {{ padding:16px 18px; border-left:3px solid var(--accent);
         background:rgba(255,255,255,.06); border-radius:0 12px 12px 0; }}
  .hl .q {{ font-size:17px; line-height:1.5; }}
  .hl .n {{ font-size:13px; opacity:.7; margin-top:8px; }}
  /* 关系网 */
  .rels {{ display:flex; flex-direction:column; gap:12px; }}
  .rel {{ padding:14px 16px; border-radius:12px; background:rgba(255,255,255,.06); }}
  .rel-top {{ display:flex; align-items:center; gap:10px; margin-bottom:6px; }}
  .rel-who {{ font-size:16px; font-weight:700; }}
  .rel-tag {{ font-size:12px; padding:3px 10px; border-radius:999px;
              background:var(--accent); color:#1a1020; font-weight:700; }}
  .rel-note {{ font-size:13.5px; opacity:.82; line-height:1.6; }}
  /* 叙事 */
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
      <div class="big">性格 &amp;<br>生活方式画像</div>
      <div class="sub">{_esc(subtitle)}</div>
      <div class="cloud">{cover_kw}</div>
      <div class="hint">↓ 滑动 / 方向键看每个人</div>
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


# ---- 主流程 --------------------------------------------------------------

def build(data_file: str | None, out_path: Path) -> Path:
    import json
    if data_file:
        data = json.loads(Path(data_file).read_text(encoding="utf-8"))
    else:
        data = fetch_sheet.fetch_all()

    range_label = data.get("range") or "全部记录"
    n_entries = len(data.get("entries", []))
    n_weeks = len(data.get("weeks", []))
    if n_entries == 0:
        raise RuntimeError("没有读到任何记录，已中止生成。")

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("缺少 ANTHROPIC_API_KEY")
    client = anthropic.Anthropic(api_key=api_key)
    data_text = _format_entries(data)
    print(f"读取全部记录：{range_label}（{n_weeks} 周 / {n_entries} 条），开始生成 {len(PEOPLE)} 人画像…",
          file=sys.stderr)

    reports: List[PersonaReport] = []
    for name in PEOPLE:
        reports.append(generate_person_persona(client, name, data_text, range_label))

    subtitle = f"{range_label} · {n_weeks} 周 · {n_entries} 条记录"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_html(range_label, subtitle, reports), encoding="utf-8")
    print(f"已写入 {out_path}", file=sys.stderr)
    return out_path


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="读取全部历史记录，生成每人的性格&生活方式画像 HTML")
    p.add_argument("--data-file", help="改用本地 JSON（fetch_sheet --all 的输出），跳过飞书")
    p.add_argument("--out", default=str(DEFAULT_OUT), help=f"输出 HTML 路径，默认 {DEFAULT_OUT}")
    args = p.parse_args(argv)

    out_path = Path(args.out).expanduser()
    try:
        result = build(args.data_file, out_path)
    except Exception as e:  # noqa: BLE001
        print(f"[错误] {e}", file=sys.stderr)
        cause = e.__cause__ or e.__context__
        if cause is not None:
            print(f"[底层原因] {type(cause).__name__}: {cause}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
