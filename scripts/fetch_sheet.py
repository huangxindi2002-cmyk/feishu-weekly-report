"""从飞书知识库（Wiki）中的电子表格读取生活流水账，输出结构化数据。

链路：
  1. app_id/secret  -> tenant_access_token
  2. 数据源清单（sheets.json，见 sheet_store.py）里的每个表格：
     - wiki node token -> 内嵌 spreadsheet token（obj_token）
     - 独立电子表格 -> token 本身即 spreadsheet token
  3. spreadsheet token -> 工作表(Tab)列表
  4. 读取某个 Tab 的 A/B/D/F/H 列 -> 结构化 entries
  5. 跨所有表格聚合（按数据源添加顺序、再按 Tab 起始日期）

只读 A(日期) / B(时段) / D(黄欣迪) / F(刘嘉晨) / H(王江楠)，图片列 C/E/G 跳过。
A 列是合并单元格（一天占多行），API 只在左上角返回值，其余为空，这里做向下填充。

可单独当 CLI 用：
  python scripts/fetch_sheet.py --list                 # 列出所有 Tab
  python scripts/fetch_sheet.py --week 5.18-5.22        # 打印某周 JSON
  python scripts/fetch_sheet.py --month 2026-05         # 打印某月合并 JSON
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from urllib.parse import quote, urlparse

import requests

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # python-dotenv 可选
    pass

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
import sheet_store  # noqa: E402  数据源清单（sheets.json）


# ---- 配置 ----------------------------------------------------------------

BASE_URL = os.environ.get("FEISHU_BASE_URL", "https://open.feishu.cn").rstrip("/")

# 人名 -> 文字记录所在列（0 基列号）。图片列在文字列左边一列，跳过。
PERSON_COLUMNS = {
    "黄欣迪": 3,  # D 列
    "刘嘉晨": 5,  # F 列
    "王江楠": 7,  # H 列
}
DATE_COL = 0  # A 列
TIME_COL = 1  # B 列
DATA_START_ROW = 2  # 第 1 行表头、第 2 行人名，第 3 行起是数据（0 基为 index 2）

TIMEOUT = 30


class FeishuError(RuntimeError):
    pass


def _wiki_token() -> str:
    """从 FEISHU_WIKI_TOKEN 或 FEISHU_WIKI_URL 解析出 wiki node token。"""
    token = os.environ.get("FEISHU_WIKI_TOKEN", "").strip()
    if token:
        return token
    url = os.environ.get("FEISHU_WIKI_URL", "").strip()
    if not url:
        raise FeishuError("需要设置 FEISHU_WIKI_TOKEN 或 FEISHU_WIKI_URL")
    # .../wiki/<token>  ，去掉可能的查询参数
    path = urlparse(url).path
    m = re.search(r"/wiki/([A-Za-z0-9]+)", path)
    if not m:
        raise FeishuError(f"无法从链接解析 wiki token: {url}")
    return m.group(1)


# ---- 鉴权 ----------------------------------------------------------------

def get_tenant_token() -> str:
    app_id = os.environ.get("FEISHU_APP_ID", "").strip()
    app_secret = os.environ.get("FEISHU_APP_SECRET", "").strip()
    if not app_id or not app_secret:
        raise FeishuError("缺少 FEISHU_APP_ID / FEISHU_APP_SECRET")
    resp = requests.post(
        f"{BASE_URL}/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=TIMEOUT,
    )
    data = resp.json()
    if data.get("code") != 0:
        raise FeishuError(f"获取 tenant_access_token 失败: {data}")
    return data["tenant_access_token"]


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}


# ---- Wiki -> Spreadsheet token ------------------------------------------

def get_spreadsheet_token(token: str, wiki_token: str | None = None) -> str:
    """把 wiki node token 换成内嵌电子表格的真实 token（obj_token）。"""
    wiki_token = wiki_token or _wiki_token()
    resp = requests.get(
        f"{BASE_URL}/open-apis/wiki/v2/spaces/get_node",
        headers=_headers(token),
        params={"token": wiki_token, "obj_type": "wiki"},
        timeout=TIMEOUT,
    )
    data = resp.json()
    if data.get("code") != 0:
        raise FeishuError(f"获取 wiki 节点失败: {data}")
    node = data["data"]["node"]
    if node.get("obj_type") != "sheet":
        raise FeishuError(
            f"该 wiki 节点不是电子表格，obj_type={node.get('obj_type')}（标题：{node.get('title')}）"
        )
    return node["obj_token"]


# ---- 工作表列表 & 读值 ----------------------------------------------------

def list_sheets(token: str, spreadsheet_token: str) -> list[dict]:
    """返回 [{'sheet_id':..., 'title':...}, ...]。"""
    resp = requests.get(
        f"{BASE_URL}/open-apis/sheets/v3/spreadsheets/{spreadsheet_token}/sheets/query",
        headers=_headers(token),
        timeout=TIMEOUT,
    )
    data = resp.json()
    if data.get("code") != 0:
        raise FeishuError(f"获取工作表列表失败: {data}")
    out = []
    for s in data["data"]["sheets"]:
        out.append({"sheet_id": s["sheet_id"], "title": s.get("title", "")})
    return out


def read_sheet_values(token: str, spreadsheet_token: str, sheet_id: str,
                      cell_range: str = "A1:H600") -> list[list]:
    """读取单个 Tab 的二维数组（已按列对齐，缺失为 None）。"""
    range_str = f"{sheet_id}!{cell_range}"
    url = (
        f"{BASE_URL}/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}"
        f"/values/{quote(range_str, safe='')}"
    )
    resp = requests.get(
        url,
        headers=_headers(token),
        params={"valueRenderOption": "ToString", "dateTimeRenderOption": "FormattedString"},
        timeout=TIMEOUT,
    )
    data = resp.json()
    if data.get("code") != 0:
        raise FeishuError(f"读取 Tab {sheet_id} 失败: {data}")
    return data["data"]["valueRange"].get("values", []) or []


# ---- 解析 ----------------------------------------------------------------

def _cell(row: list, idx: int) -> str:
    """安全取单元格并规整为字符串。"""
    if idx >= len(row):
        return ""
    v = row[idx]
    if v is None:
        return ""
    # ToString 模式一般是字符串；偶发富文本会是 list/dict
    if isinstance(v, (list, dict)):
        return _flatten_rich(v)
    return str(v).strip()


def _flatten_rich(v) -> str:
    if isinstance(v, list):
        return "".join(_flatten_rich(x) for x in v).strip()
    if isinstance(v, dict):
        return str(v.get("text", "")).strip()
    return str(v).strip()


def parse_week(rows: list[list], week_title: str) -> dict:
    """把某个 Tab 的二维数组解析成 {'week':..., 'entries':[...]}。"""
    entries = []
    last_date = ""
    for row in rows[DATA_START_ROW:]:
        date = _cell(row, DATE_COL)
        if date:
            last_date = date  # 合并单元格向下填充
        else:
            date = last_date
        time = _cell(row, TIME_COL)
        people = {name: _cell(row, col) for name, col in PERSON_COLUMNS.items()}
        # 整行无时段且三人皆空 -> 跳过
        if not time and not any(people.values()):
            continue
        entry = {"date": date, "time": time}
        entry.update(people)
        entries.append(entry)
    return {"week": week_title, "entries": entries}


# ---- 多数据源：把清单里每个表格解析成 spreadsheet token --------------------

def spreadsheet_token_for(api_token: str, source: dict) -> str:
    """根据数据源类型拿到真正的 spreadsheet token。

    - kind == "sheet"：独立电子表格，链接里的 token 就是 spreadsheet token。
    - kind == "wiki" ：知识库节点，需再换成内嵌表格的 obj_token。
    """
    if source.get("kind") == "sheet":
        return source["id"]
    return get_spreadsheet_token(api_token, source["id"])


def iter_sources(api_token: str) -> list[tuple[dict, str]]:
    """返回 [(source, spreadsheet_token), ...]，跳过解析失败的源（打印告警）。"""
    sources = sheet_store.load_sources()
    if not sources:
        raise FeishuError("没有任何飞书表格数据源：请在 sheets.json 添加，或设置 FEISHU_WIKI_URL")
    out = []
    for src in sources:
        try:
            ss = spreadsheet_token_for(api_token, src)
        except FeishuError as e:
            label = src.get("label") or src.get("id")
            print(f"[告警] 数据源「{label}」解析失败，已跳过：{e}", file=sys.stderr)
            continue
        out.append((src, ss))
    if not out:
        raise FeishuError("所有数据源都解析失败")
    return out


def _src_label(src: dict) -> str:
    return src.get("label") or src.get("id") or "?"


# ---- 高层封装 ------------------------------------------------------------

def _find_sheet(sheets: list[dict], week_title: str) -> dict | None:
    norm = week_title.strip()
    for s in sheets:
        if s["title"].strip() == norm:
            return s
    # 宽松匹配：去掉空格/全角
    cleaned = norm.replace(" ", "")
    for s in sheets:
        if s["title"].strip().replace(" ", "") == cleaned:
            return s
    return None


def fetch_week(week_title: str) -> dict:
    """在所有数据源里查找该周 Tab（取第一个命中的）。"""
    token = get_tenant_token()
    avail_all = []
    for src, ss in iter_sources(token):
        sheets = list_sheets(token, ss)
        avail_all += [s["title"] for s in sheets]
        sheet = _find_sheet(sheets, week_title)
        if sheet:
            rows = read_sheet_values(token, ss, sheet["sheet_id"])
            return parse_week(rows, sheet["title"])
    raise FeishuError(f"未找到 Tab「{week_title}」。现有 Tab：{', '.join(avail_all)}")


def _tab_month(title: str) -> int | None:
    """从 Tab 名（如 '5.18-5.22'）解析出所属月份。"""
    m = re.match(r"\s*(\d{1,2})\s*[.．]", title)
    return int(m.group(1)) if m else None


def _tab_start_key(title: str) -> tuple[int, int]:
    """用于排序：(月, 起始日)。"""
    m = re.match(r"\s*(\d{1,2})\s*[.．]\s*(\d{1,2})", title)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    return (0, 0)


def fetch_all() -> dict:
    """跨所有数据源读取全部周 Tab，按「数据源添加顺序 -> Tab 起始日期」合并。

    同名 Tab（如两个表里都有 5.18-5.22）只取第一次出现的，避免重复。
    """
    token = get_tenant_token()
    all_entries, weeks = [], []
    seen_titles = set()
    for src, ss in iter_sources(token):
        sheets = list_sheets(token, ss)
        ordered = sorted(sheets, key=lambda s: _tab_start_key(s["title"]))
        for s in ordered:
            if _tab_start_key(s["title"]) == (0, 0):
                continue  # 跳过非「周」命名的 Tab
            title = s["title"].strip()
            if title in seen_titles:
                continue
            rows = read_sheet_values(token, ss, s["sheet_id"])
            wk = parse_week(rows, title)
            if not wk["entries"]:
                continue
            seen_titles.add(title)
            weeks.append(title)
            for e in wk["entries"]:
                e = dict(e)
                e["_week"] = title
                all_entries.append(e)
    if not all_entries:
        raise FeishuError("没有读到任何记录")
    range_label = f"{weeks[0]} ~ {weeks[-1]}" if weeks else "全部"
    return {"label": "全部记录", "range": range_label, "weeks": weeks, "entries": all_entries}


def fetch_month(year: int, month: int) -> dict:
    """跨所有数据源合并某月所有 Tab（同名 Tab 去重）。"""
    token = get_tenant_token()
    all_entries, weeks = [], []
    seen_titles = set()
    avail_all = []
    for src, ss in iter_sources(token):
        sheets = list_sheets(token, ss)
        avail_all += [s["title"] for s in sheets]
        matched = [s for s in sheets if _tab_month(s["title"]) == month]
        matched.sort(key=lambda s: _tab_start_key(s["title"]))
        for s in matched:
            title = s["title"].strip()
            if title in seen_titles:
                continue
            rows = read_sheet_values(token, ss, s["sheet_id"])
            wk = parse_week(rows, title)
            seen_titles.add(title)
            weeks.append(title)
            for e in wk["entries"]:
                e = dict(e)
                e["_week"] = title
                all_entries.append(e)
    if not weeks:
        raise FeishuError(f"{year}-{month:02d} 没有匹配的 Tab。现有 Tab：{', '.join(avail_all)}")
    return {"month": f"{year}-{month:02d}", "weeks": weeks, "entries": all_entries}


# ---- CLI -----------------------------------------------------------------

def _main(argv=None) -> int:
    p = argparse.ArgumentParser(description="从飞书知识库电子表格读取数据")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true", help="列出所有 Tab")
    g.add_argument("--week", help="某周 Tab 名，如 5.18-5.22")
    g.add_argument("--month", help="某月 YYYY-MM，如 2026-05")
    g.add_argument("--all", action="store_true", help="合并所有周 Tab")
    args = p.parse_args(argv)

    try:
        if args.list:
            token = get_tenant_token()
            for src, ss in iter_sources(token):
                print(f"# 数据源：{_src_label(src)}  ({src.get('kind')}:{src['id']})")
                for s in list_sheets(token, ss):
                    print(f"{s['title']}\t{s['sheet_id']}")
        elif args.all:
            print(json.dumps(fetch_all(), ensure_ascii=False, indent=2))
        elif args.week:
            print(json.dumps(fetch_week(args.week), ensure_ascii=False, indent=2))
        else:
            year, month = args.month.split("-")
            data = fetch_month(int(year), int(month))
            print(json.dumps(data, ensure_ascii=False, indent=2))
    except FeishuError as e:
        print(f"[错误] {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
