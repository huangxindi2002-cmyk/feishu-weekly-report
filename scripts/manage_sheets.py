"""本地网页：管理飞书表格数据源（sheets.json）。

每月新建了飞书表格后，跑这个脚本会打开一个本地网页，粘贴链接点「添加」即可；
列表里可删除、可「验证」（连飞书读一下，确认链接能用、显示 Tab 数）。
保存的内容写进 sheets.json，被 fetch_sheet.py / 各报告脚本聚合读取。

用法：
  python scripts/manage_sheets.py            # 打开 http://127.0.0.1:8765 并自动唤起浏览器
  python scripts/manage_sheets.py --port 9000 --no-open

提交到 GitHub 后，CI 也会读同一份 sheets.json（记得 git add sheets.json）。
"""
from __future__ import annotations

import argparse
import html
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sheet_store  # noqa: E402
import fetch_sheet  # noqa: E402


def _esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>飞书表格管理</title>
<style>
  * {{ box-sizing:border-box; }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
         background:#0f1117; color:#e8eaf0; margin:0; padding:40px 16px; }}
  .wrap {{ max-width:680px; margin:0 auto; }}
  h1 {{ font-size:22px; margin:0 0 6px; }}
  .sub {{ color:#8b90a0; font-size:13px; margin-bottom:26px; }}
  .card {{ background:#171a23; border:1px solid #232735; border-radius:14px; padding:20px; margin-bottom:18px; }}
  form.add {{ display:flex; flex-direction:column; gap:10px; }}
  input[type=text] {{ width:100%; padding:11px 13px; border-radius:9px; border:1px solid #2c3142;
                      background:#0f1117; color:#e8eaf0; font-size:14px; }}
  input::placeholder {{ color:#5c627a; }}
  .row2 {{ display:flex; gap:10px; }}
  .row2 input {{ flex:1; }}
  button {{ border:0; border-radius:9px; padding:11px 18px; font-size:14px; font-weight:600;
            cursor:pointer; }}
  .primary {{ background:#5b8cff; color:#fff; }}
  .primary:hover {{ background:#7aa0ff; }}
  .ghost {{ background:#232735; color:#cfd4e4; padding:7px 13px; font-weight:500; }}
  .ghost:hover {{ background:#2c3142; }}
  .danger {{ background:transparent; color:#ff7a8a; padding:7px 11px; font-weight:500; }}
  .danger:hover {{ color:#ff5168; }}
  table {{ width:100%; border-collapse:collapse; }}
  td {{ padding:13px 6px; border-top:1px solid #232735; vertical-align:top; }}
  tr:first-child td {{ border-top:0; }}
  .label {{ font-weight:600; font-size:15px; }}
  .meta {{ color:#8b90a0; font-size:12px; margin-top:3px; word-break:break-all; }}
  .pill {{ display:inline-block; font-size:11px; padding:2px 8px; border-radius:999px;
           background:#232735; color:#9aa0b4; margin-left:6px; }}
  .tabs {{ color:#5fd0c5; font-size:12px; }}
  .acts {{ white-space:nowrap; text-align:right; }}
  .flash {{ padding:11px 14px; border-radius:9px; font-size:13px; margin-bottom:18px; }}
  .flash.ok {{ background:#10331f; color:#7ee0a2; border:1px solid #1c5c34; }}
  .flash.err {{ background:#3a1620; color:#ff9aa8; border:1px solid #5c1c2c; }}
  .empty {{ color:#8b90a0; font-size:14px; text-align:center; padding:22px 0; }}
  code {{ background:#0f1117; padding:1px 5px; border-radius:5px; color:#9ab8ff; }}
</style></head>
<body><div class="wrap">
  <h1>飞书表格管理</h1>
  <div class="sub">每月新建表格后，把链接粘进来「添加」。报告脚本会聚合读取下方所有表格。</div>
  {flash}
  <div class="card">
    <form class="add" method="post" action="/add">
      <input type="text" name="url" placeholder="粘贴飞书表格链接（/wiki/… 或 /sheets/…）" required autofocus>
      <div class="row2">
        <input type="text" name="label" placeholder="备注名（可选，如 2026-06）">
        <button class="primary" type="submit">添加</button>
      </div>
    </form>
  </div>
  <div class="card">
    {rows}
  </div>
  <div class="sub">数据源保存在 <code>sheets.json</code>。改完别忘了 <code>git add sheets.json &amp;&amp; git commit</code>，CI 才能用上。</div>
</div></body></html>"""


def _render_rows(sources: list[dict]) -> str:
    if not sources:
        return '<div class="empty">还没有任何表格。粘贴上面的链接添加第一个吧。</div>'
    trs = []
    for s in sources:
        kind = "知识库" if s.get("kind") == "wiki" else "电子表格"
        tabs = s.get("tabs")
        tabs_html = f'<span class="tabs">✓ {tabs} 个 Tab</span>' if tabs else '<span class="meta">未验证</span>'
        label = s.get("label") or s.get("id")
        added = s.get("added_at") or ""
        trs.append(f"""
        <tr>
          <td>
            <div class="label">{_esc(label)}<span class="pill">{kind}</span></div>
            <div class="meta">{_esc(s.get('url') or s['id'])}</div>
            <div class="meta">{tabs_html}{('　添加于 ' + _esc(added)) if added else ''}</div>
          </td>
          <td class="acts">
            <form method="post" action="/verify" style="display:inline">
              <input type="hidden" name="id" value="{_esc(s['id'])}">
              <button class="ghost" type="submit">验证</button>
            </form>
            <form method="post" action="/delete" style="display:inline"
                  onsubmit="return confirm('删除这个表格数据源？')">
              <input type="hidden" name="id" value="{_esc(s['id'])}">
              <button class="danger" type="submit">删除</button>
            </form>
          </td>
        </tr>""")
    return f"<table>{''.join(trs)}</table>"


def render_page(flash_kind: str = "", flash_msg: str = "") -> str:
    sources = sheet_store.ensure_file_seeded()
    flash = ""
    if flash_msg:
        flash = f'<div class="flash {flash_kind}">{_esc(flash_msg)}</div>'
    return PAGE.format(flash=flash, rows=_render_rows(sources))


def _verify(token_id: str) -> tuple[str, str]:
    """连飞书验证某个数据源，更新 Tab 数，返回 (kind, msg)。"""
    src = next((s for s in sheet_store.load_sources() if s["id"] == token_id), None)
    if not src:
        return ("err", "找不到该数据源")
    try:
        api = fetch_sheet.get_tenant_token()
        ss = fetch_sheet.spreadsheet_token_for(api, src)
        sheets = fetch_sheet.list_sheets(api, ss)
    except Exception as e:  # noqa: BLE001
        return ("err", f"验证失败：{e}")
    sheet_store.update_source(token_id, tabs=len(sheets))
    titles = "、".join(s["title"] for s in sheets[:8])
    more = " …" if len(sheets) > 8 else ""
    return ("ok", f"「{src.get('label') or token_id}」连通正常，共 {len(sheets)} 个 Tab：{titles}{more}")


class Handler(BaseHTTPRequestHandler):
    def _send(self, body: str, code: int = 200):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, kind: str, msg: str):
        # 用查询参数把一次性提示带回首页（PRG 模式，避免刷新重复提交）
        from urllib.parse import quote
        self.send_response(303)
        self.send_header("Location", f"/?k={kind}&m={quote(msg)}")
        self.end_headers()

    def _form(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        return {k: v[0] for k, v in parse_qs(raw).items()}

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path not in ("/", "/index.html"):
            self._send("<h1>404</h1>", 404)
            return
        q = parse_qs(parsed.query)
        self._send(render_page(q.get("k", [""])[0], q.get("m", [""])[0]))

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        form = self._form()
        try:
            if path == "/add":
                entry = sheet_store.add_source(form.get("url", ""), form.get("label", ""))
                self._redirect("ok", f"已添加：{entry.get('label') or entry['id']}")
            elif path == "/delete":
                ok = sheet_store.remove_source(form.get("id", ""))
                self._redirect("ok" if ok else "err", "已删除" if ok else "未找到该数据源")
            elif path == "/verify":
                kind, msg = _verify(form.get("id", ""))
                self._redirect(kind, msg)
            else:
                self._send("<h1>404</h1>", 404)
        except sheet_store.SheetLinkError as e:
            self._redirect("err", str(e))
        except Exception as e:  # noqa: BLE001
            self._redirect("err", f"出错了：{e}")

    def log_message(self, *args):  # 静音默认访问日志
        pass


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="本地网页：管理飞书表格数据源")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = p.parse_args(argv)

    sheet_store.ensure_file_seeded()
    url = f"http://{args.host}:{args.port}/"
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"飞书表格管理已启动：{url}（Ctrl+C 退出）")
    if not args.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
