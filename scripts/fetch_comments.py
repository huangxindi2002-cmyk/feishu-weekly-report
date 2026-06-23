"""探针脚本：验证「获取文档评论」权限是否生效，并打印评论原始结构。

跑法：
  python scripts/fetch_comments.py            # 取第一个数据源表格的评论
  python scripts/fetch_comments.py --raw      # 额外打印一条评论的完整 JSON

注意：只读，不改任何现有逻辑。确认能拿到评论后，再把解析逻辑接进 fetch_sheet.py。
"""
from __future__ import annotations

import argparse
import json
import re
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
import fetch_sheet as fs  # 复用鉴权、token 解析

import requests


# open_id -> 人名（飞书个人/外部身份在通讯录拿不到真名，手动映射；已与用户确认）
AUTHOR_NAMES = {
    "ou_91f8e77b801395ea69d5188b56d42f76": "黄欣迪",
    "ou_6a8063a44d7764ca02a09810bba42162": "刘嘉晨",
    "ou_fb28b52ba54e20577ddeaedcc3c4e75a": "王江楠",
}


def author_name(open_id: str) -> str:
    return AUTHOR_NAMES.get(open_id, open_id or "?")


# 评论 quote 里的单元格列字母 -> 被评论记录的主人。
# 文字列 D/F/H 是三人的记录，左边相邻的图片列 C/E/G 归同一个人。
COL_PERSON = {
    "C": "黄欣迪", "D": "黄欣迪",
    "E": "刘嘉晨", "F": "刘嘉晨",
    "G": "王江楠", "H": "王江楠",
}

_QUOTE_RE = re.compile(r"^([A-Za-z]+)(\d+)\s*(.*)$", re.S)


def _norm(s: str) -> str:
    return (s or "").replace("\n", "").replace(" ", "").strip()


def _get_comments_one(token: str, file_token: str, file_type: str, is_whole: bool) -> list[dict]:
    """按 is_whole（全文/局部）拉一类评论，自动翻页。"""
    items: list[dict] = []
    page_token = ""
    while True:
        params = {"file_type": file_type, "page_size": 100, "is_whole": str(is_whole).lower()}
        if page_token:
            params["page_token"] = page_token
        resp = requests.get(
            f"{fs.BASE_URL}/open-apis/drive/v1/files/{file_token}/comments",
            headers=fs._headers(token),
            params=params,
            timeout=fs.TIMEOUT,
        )
        data = resp.json()
        if data.get("code") != 0:
            raise fs.FeishuError(f"获取评论失败（code={data.get('code')}）：{data}")
        d = data.get("data", {})
        items += d.get("items", []) or []
        if not d.get("has_more"):
            break
        page_token = d.get("page_token", "")
        if not page_token:
            break
    return items


def get_comments(token: str, file_token: str, file_type: str = "sheet") -> list[dict]:
    """拉取一份云文档的全部评论：全文评论 + 局部(单元格)评论合并去重。"""
    seen, merged = set(), []
    for is_whole in (True, False):
        for c in _get_comments_one(token, file_token, file_type, is_whole):
            cid = c.get("comment_id") or id(c)
            if cid in seen:
                continue
            seen.add(cid)
            merged.append(c)
    return merged


def _reply_text(reply: dict) -> str:
    parts = [
        el.get("text_run", {}).get("text", "")
        for el in reply.get("content", {}).get("elements", [])
        if el.get("type") == "text_run"
    ]
    return "".join(parts).strip()


def _flatten_content(comment: dict) -> str:
    """把一条评论里所有 reply 拼成「作者：内容」串，标清每句谁说的。"""
    out = []
    for reply in comment.get("reply_list", {}).get("replies", []):
        text = _reply_text(reply)
        if text:
            out.append(f"{author_name(reply.get('user_id',''))}：{text}")
    return " / ".join(out)


def _conversation(comment: dict) -> list[dict]:
    """把一条评论的来回回复拆成 [{author, text}, ...]，标清每句谁说的。"""
    out = []
    for reply in comment.get("reply_list", {}).get("replies", []):
        text = _reply_text(reply)
        if text:
            out.append({"author": author_name(reply.get("user_id", "")), "text": text})
    return out


def attach_comments(token: str, data: dict, sources=None) -> int:
    """把评论按「列字母→人名 + quote 文本」匹配，挂到 data['entries'] 上。

    每个命中的 entry 会多一个 'comments' 字段：
        [{'on': 人名, 'conversation': [{'author':.., 'text':..}, ...]}, ...]
    返回成功挂载的评论条数。匹配不到的评论（图片评论、quote 截断、跨周）忽略。
    """
    entries = data.get("entries", [])
    if not entries:
        return 0
    sources = sources if sources is not None else fs.iter_sources(token)
    matched = 0
    for _src, ss in sources:
        for c in get_comments(token, ss, "sheet"):
            m = _QUOTE_RE.match(c.get("quote", ""))
            if not m:
                continue
            person = COL_PERSON.get(m.group(1).upper())
            txt = _norm(m.group(3))
            if not person or not txt or txt in ("[图片]", "[Image]"):
                continue
            key = txt[:12]
            cand = [e for e in entries if key in _norm(e.get(person, ""))]
            if not cand:
                continue
            conv = _conversation(c)
            if not conv:
                continue
            # 多个候选（同一人内容雷同）取第一条；少见，可接受
            cand[0].setdefault("comments", []).append({"on": person, "conversation": conv})
            matched += 1
    return matched


def _main(argv=None) -> int:
    p = argparse.ArgumentParser(description="验证飞书文档评论读取权限")
    p.add_argument("--raw", action="store_true", help="额外打印第一条评论的完整 JSON")
    args = p.parse_args(argv)

    try:
        token = fs.get_tenant_token()
        sources = fs.iter_sources(token)
        for src, ss in sources:
            label = fs._src_label(src)
            print(f"\n# 数据源：{label}  (spreadsheet_token={ss})")
            comments = get_comments(token, ss, "sheet")
            print(f"  共拿到 {len(comments)} 条评论")
            for c in comments[:10]:
                quote = c.get("quote", "")
                text = _flatten_content(c)
                solved = "✅已解决" if c.get("is_solved") else ""
                print(f"  - quote=「{quote}」 {text} {solved}")
            if args.raw and comments:
                print("\n  === 第一条评论完整结构 ===")
                print(json.dumps(comments[0], ensure_ascii=False, indent=2))
    except fs.FeishuError as e:
        print(f"[错误] {e}", file=sys.stderr)
        print("\n如果是权限错(code=99991672 等)：请确认「获取文档评论」权限已勾选，"
              "且已在『版本管理与发布』创建版本并发布/通过审批。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
