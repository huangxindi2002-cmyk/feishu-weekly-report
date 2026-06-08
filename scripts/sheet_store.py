"""飞书表格「数据源清单」的存取与链接解析。

背景：以前只有一个知识库表格（FEISHU_WIKI_URL，一个表里多个周 Tab）。
现在每月会不定期新建独立的飞书表格，于是把「数据源」抽成一份可增删的清单
`sheets.json`，由本地网页（manage_sheets.py）维护，fetch_sheet.py 聚合读取。

清单条目结构：
  {
    "id":   "<token>",           # 飞书链接里的 token，作为去重主键
    "kind": "wiki" | "sheet",    # wiki 节点（需再换 obj_token）/ 独立电子表格
    "url":  "<原始链接>",
    "label":"<备注名，可空>",
    "added_at": "ISO 时间",
    "tabs": 8                     # 上次验证到的 Tab 数（可选）
  }

向后兼容：若 sheets.json 不存在，则从环境变量 FEISHU_WIKI_URL / FEISHU_WIKI_TOKEN
合成一个 wiki 数据源，旧的 .env / CI 配置无需改动即可继续工作。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
SHEETS_FILE = ROOT / "sheets.json"


class SheetLinkError(ValueError):
    pass


def parse_feishu_url(url: str) -> tuple[str, str]:
    """从飞书链接解析出 (kind, token)。

    支持：
      - 知识库节点  https://xxx.feishu.cn/wiki/<token>           -> ("wiki", token)
      - 独立电子表格 https://xxx.feishu.cn/sheets/<token>[?...]   -> ("sheet", token)
    也接受直接传入裸 token（默认按 wiki 处理）。
    """
    url = (url or "").strip()
    if not url:
        raise SheetLinkError("链接为空")
    path = urlparse(url).path if "/" in url else url
    m = re.search(r"/wiki/([A-Za-z0-9]+)", path)
    if m:
        return ("wiki", m.group(1))
    m = re.search(r"/sheets?/([A-Za-z0-9]+)", path)
    if m:
        return ("sheet", m.group(1))
    # 没有路径分隔符时，当作裸 token（兼容只填 token 的旧习惯）
    if re.fullmatch(r"[A-Za-z0-9]+", url):
        return ("wiki", url)
    raise SheetLinkError(f"无法识别的飞书链接（需含 /wiki/ 或 /sheets/）：{url}")


def _env_source() -> dict | None:
    """从环境变量合成的默认 wiki 数据源（向后兼容）。"""
    url = os.environ.get("FEISHU_WIKI_URL", "").strip()
    token = os.environ.get("FEISHU_WIKI_TOKEN", "").strip()
    try:
        if url:
            kind, tok = parse_feishu_url(url)
        elif token:
            kind, tok = "wiki", token
        else:
            return None
    except SheetLinkError:
        return None
    return {
        "id": tok,
        "kind": kind,
        "url": url or tok,
        "label": "历史（环境变量）",
        "added_at": "",
        "tabs": None,
    }


def load_sources() -> list[dict]:
    """读取数据源清单。文件不存在时回退到环境变量里的单个表格。"""
    if SHEETS_FILE.exists():
        try:
            data = json.loads(SHEETS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
        sheets = data.get("sheets") or []
        if sheets:
            return sheets
    env = _env_source()
    return [env] if env else []


def save_sources(sheets: list[dict]) -> None:
    SHEETS_FILE.write_text(
        json.dumps({"sheets": sheets}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def ensure_file_seeded() -> list[dict]:
    """若清单文件不存在，用环境变量里的表格初始化它，并返回当前清单。

    供网页首次打开时调用，把旧的 .env 配置「固化」进 sheets.json，便于后续增删。
    """
    if not SHEETS_FILE.exists():
        save_sources(load_sources())
    return load_sources()


def add_source(url: str, label: str = "") -> dict:
    """新增一个数据源（按 token 去重）。返回新增/已存在的条目。"""
    kind, token = parse_feishu_url(url)
    sheets = load_sources()
    for s in sheets:
        if s["id"] == token:
            # 已存在：更新链接/备注即可
            s["url"] = url.strip() or s.get("url", "")
            if label.strip():
                s["label"] = label.strip()
            save_sources(sheets)
            return s
    entry = {
        "id": token,
        "kind": kind,
        "url": url.strip(),
        "label": label.strip(),
        "added_at": dt.datetime.now().isoformat(timespec="seconds"),
        "tabs": None,
    }
    sheets.append(entry)
    save_sources(sheets)
    return entry


def remove_source(token: str) -> bool:
    sheets = load_sources()
    kept = [s for s in sheets if s["id"] != token]
    if len(kept) == len(sheets):
        return False
    save_sources(kept)
    return True


def update_source(token: str, **fields) -> dict | None:
    sheets = load_sources()
    for s in sheets:
        if s["id"] == token:
            s.update(fields)
            save_sources(sheets)
            return s
    return None
