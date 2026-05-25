"""通过飞书群自定义机器人 Webhook 发送一张「报告出炉」的消息卡片。

用法：
  python scripts/send_feishu.py --path reports/weekly/2026-05-22.html --title "5.18-5.22 生活报告"
  python scripts/send_feishu.py --url https://xxx.github.io/repo/reports/...   # 直接给完整链接

链接拼接：若只给 --path，会用环境变量 GITHUB_REPO（username/repo）拼出 GitHub Pages 地址：
  https://<username>.github.io/<repo>/<path>
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import os
import sys
import time

import requests

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

TIMEOUT = 15


def pages_url_from_path(path: str) -> str:
    repo = os.environ.get("GITHUB_REPO", "").strip()
    if not repo:
        raise SystemExit("缺少 GITHUB_REPO（username/repo），无法拼出 Pages 链接")
    # 兼容传成完整 git 地址的情况
    repo = repo.replace("https://github.com/", "").replace(".git", "").strip("/")
    owner, name = repo.split("/", 1)
    return f"https://{owner}.github.io/{name}/{path.lstrip('/')}"


def _sign(secret: str, timestamp: str) -> str:
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), b"", hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def build_card(title: str, url: str) -> dict:
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "indigo",
            "title": {"tag": "plain_text", "content": "📖 生活报告出炉啦"},
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md",
                                    "content": f"**{title}**\n微信读书风格 · 每人一屏，左右滑动查看 ✨"}},
            {"tag": "hr"},
            {"tag": "action", "actions": [{
                "tag": "button",
                "text": {"tag": "plain_text", "content": "🔗 立即查看报告"},
                "url": url,
                "type": "primary",
            }]},
            {"tag": "note", "elements": [
                {"tag": "plain_text", "content": "由飞书数据 + Claude 自动生成"}]},
        ],
    }


def send(title: str, url: str) -> None:
    webhook = os.environ.get("FEISHU_WEBHOOK_URL", "").strip()
    if not webhook:
        raise SystemExit("缺少 FEISHU_WEBHOOK_URL")
    payload = {"msg_type": "interactive", "card": build_card(title, url)}

    secret = os.environ.get("FEISHU_WEBHOOK_SECRET", "").strip()
    if secret:
        ts = str(int(time.time()))
        payload["timestamp"] = ts
        payload["sign"] = _sign(secret, ts)

    resp = requests.post(webhook, json=payload, timeout=TIMEOUT)
    data = resp.json()
    if data.get("code") not in (0, None) or data.get("StatusCode") not in (0, None):
        raise SystemExit(f"飞书发送失败：{data}")
    print(f"已发送到飞书群：{url}", file=sys.stderr)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="飞书群机器人发送报告链接卡片")
    p.add_argument("--title", default="最新生活报告")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--url", help="完整报告链接")
    g.add_argument("--path", help="报告相对路径，配合 GITHUB_REPO 拼链接")
    args = p.parse_args(argv)

    url = args.url or pages_url_from_path(args.path)
    send(args.title, url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
