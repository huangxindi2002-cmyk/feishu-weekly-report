# 部署与运维手册

本文件沉淀本项目的部署步骤、定时策略、以及搭建过程中踩过的坑与解法。
日常使用看 [README.md](./README.md);这里偏运维与排错。

## 整体链路

```
飞书知识库(Wiki) ──get_node──▶ 内嵌电子表格 token ──读 A/B/D/F/H──▶ 结构化流水账
        │
        ▼
   Claude API (claude-opus-4-7, 结构化输出 + prompt caching)  每人一份 JSON
        │
        ▼
   渲染微信读书风 HTML ──▶ commit 到仓库 ──▶ GitHub Pages 部署
        │
        ▼
   飞书群机器人 Webhook 发链接卡片
```

## 一、前置配置（一次性）

### 1. 飞书自建应用
- 开放平台创建企业自建应用,拿到 **App ID / App Secret**。
- **权限管理**里开通(缺一不可):
  - `wiki:wiki:readonly`（或 `wiki:node:read`）— 读知识库节点
  - `sheets:spreadsheet:readonly` — 读电子表格内容
- **版本管理与发布 → 创建版本 → 申请发布**:权限改完必须发版才生效(只保存无效)。
- 打开目标知识库 → 成员设置 → 把该应用**加为可阅读成员**(否则即使有权限也读不到这个空间)。

### 2. 飞书群自定义机器人
- 群设置 → 群机器人 → 添加「自定义机器人」,拿到 **Webhook URL**。
- 安全设置二选一,对应填到下面的变量:
  - 「自定义关键词」→ `FEISHU_WEBHOOK_KEYWORD`（消息会自动带上它）
  - 「签名校验」→ `FEISHU_WEBHOOK_SECRET`

### 3. GitHub 仓库
- **Settings → Pages**:Source = `Deploy from a branch`,分支 `main`,目录 `/(root)`。
  发布地址 `https://<owner>.github.io/<repo>/`。
- **Settings → Actions → General → Workflow permissions**:确保是
  **Read and write permissions**(否则 Actions 自动 push 会 403)。
- **Settings → Secrets and variables → Actions**:

  | 类型 | 名称 | 说明 |
  |---|---|---|
  | Secret | `FEISHU_APP_ID` | 飞书应用 ID |
  | Secret | `FEISHU_APP_SECRET` | 飞书应用密钥 |
  | Secret | `ANTHROPIC_API_KEY` | Claude API Key（粘贴时勿带换行）|
  | Secret | `FEISHU_WEBHOOK_URL` | 群机器人 Webhook |
  | Secret | `FEISHU_WEBHOOK_SECRET` | 仅「签名校验」模式需要 |
  | Variable | `FEISHU_WIKI_URL` | 知识库链接 |
  | Variable | `FEISHU_WEBHOOK_KEYWORD` | 仅「关键词」模式需要 |
  | Variable | `FEISHU_BASE_URL` | 可选,默认 `https://open.feishu.cn` |

## 二、触发方式

| 方式 | 时机 | 行为 |
|---|---|---|
| 定时 · 周报 | 每周五 18:00（北京）`cron: 0 10 * * 5` | 自动选最近一周 |
| 定时 · 月报 | 每月 1 号 09:00（北京）`cron: 0 1 1 * *` | 自动出**上一个月**合集 |
| 手动 | Actions → Run workflow | 选 `weekly`/`monthly`,`target` 可指定周/月,留空则自动 |

> 月报定时取「上月」靠 workflow 里的 `date -u -d 'last month'`,所以 1 号当天出的是上个月。
> 定时任务在仓库 **60 天无提交**后会被 GitHub 自动停用,需手动重新启用。

## 三、本地手动运行

```bash
source .venv/bin/activate
python scripts/fetch_sheet.py --list                       # 看所有 Tab
python scripts/fetch_sheet.py --week 5.18-5.22 > week.json  # 只读数据
python scripts/generate_report.py --type weekly --data-file week.json   # 本地数据生成(省钱)
python scripts/generate_report.py --type monthly --target 2026-05       # 连飞书出月报
python scripts/send_feishu.py --path reports/weekly/2026-05-18.html --title "..."
```

## 四、排错速查

| 现象 | 原因 | 解法 |
|---|---|---|
| `99991672 Access denied ... wiki:...` | 应用没开 wiki 权限或没发版 | 开 `wiki:wiki:readonly` + 创建版本发布 |
| 读 Tab 报权限/找不到空间 | 应用未加入该知识库 | 把应用加为知识库可阅读成员 |
| `19024 Key Words Not Found` | 机器人开了「自定义关键词」,消息没带 | 配 `FEISHU_WEBHOOK_KEYWORD`=你的关键词 |
| 飞书发送报签名错误 | 开了「签名校验」没配密钥 | 配 `FEISHU_WEBHOOK_SECRET` |
| `[错误] Connection error.` | runner 到 api.anthropic.com 偶发抖动 | 重跑 workflow;SDK 已自动重试 2 次,频繁可调高 `max_retries` |
| `没有读到任何记录,已中止` | 目标周/月还没数据(如本周刚开始) | 指定一个有数据的 `target` |
| 生成步骤里看不到真因 | 已加底层异常打印 | 看「生成报告」日志里的 `[底层原因] ...` |
| Actions push 403 | Workflow 权限为只读 | 改成 Read and write permissions |

## 五、改配置

- 改人名 / 列位置:`scripts/fetch_sheet.py` 顶部 `PERSON_COLUMNS`。
- 改报告语气 / 关键词 / 行为分类口径:`scripts/generate_report.py` 的 `STYLE_GUIDE`。
- 改配色:`generate_report.py` 的 `THEMES`。
- 改定时:`.github/workflows/weekly_report.yml` 的两个 `cron`。
