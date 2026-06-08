# feishu-life-report

从飞书知识库的电子表格读取生活流水账 → 用 Claude API 生成「微信读书年度报告」风格的周报/月报 → 部署到 GitHub Pages → 飞书群机器人发链接。

```
feishu-life-report/
├── scripts/
│   ├── fetch_sheet.py       # 飞书 wiki/sheet→token + 读数据 + 跨多表聚合
│   ├── sheet_store.py       # 数据源清单(sheets.json)的存取 + 链接解析
│   ├── manage_sheets.py     # 本地网页：粘贴链接增/删/验证飞书表格
│   ├── generate_report.py   # 调 Claude API 生成结构化内容并渲染 HTML
│   └── send_feishu.py       # 群机器人 Webhook 发消息卡片
├── sheets.json              # 所有飞书表格数据源清单（被聚合读取，需入库）
├── reports/
│   ├── weekly/YYYY-MM-DD.html
│   ├── monthly/YYYY-MM.html
│   └── manifest.json        # 报告索引，index.html 据此自动列出
├── index.html               # 导航首页（读 manifest.json）
├── .github/workflows/weekly_report.yml
├── requirements.txt
└── .env.example
```

## 一、准备

### 1. 飞书自建应用
开放平台 https://open.feishu.cn 创建「企业自建应用」，拿到 **App ID / App Secret**，并：
- 开通权限：`wiki:wiki:readonly`（读知识库节点）、`sheets:spreadsheet:readonly`（读电子表格）。
- 把应用加为知识库的协作者（否则读不到内嵌表格）。

### 2. 群自定义机器人
目标飞书群 → 设置 → 群机器人 → 添加「自定义机器人」，拿到 **Webhook URL**。
若开启「签名校验」，把密钥填到 `FEISHU_WEBHOOK_SECRET`。

### 3. 配置环境变量
```bash
cp .env.example .env   # 然后填入各项值
```

### 4. 多个飞书表格（每月新建表也能用）

早期所有数据在一个知识库表里（多个周 Tab）。现在每月不定期新建独立飞书表格时，
不用改任何环境变量——所有表格统一登记在 **`sheets.json`** 里，报告脚本会**聚合读取全部表格**。

最省心的方式是开本地网页管理：

```bash
python scripts/manage_sheets.py        # 打开 http://127.0.0.1:8765，自动唤起浏览器
```

- 粘贴新表格链接（支持 `/wiki/…` 知识库节点 和 `/sheets/…` 独立电子表格）→「添加」。
- 列表里可「验证」（连飞书确认链接可用、显示 Tab 数）、可「删除」。
- 首次打开会把旧的 `FEISHU_WIKI_URL` 自动收进 `sheets.json`，所以历史数据不丢。
- 改完记得 `git add sheets.json && git commit && git push`，CI 才会用上新表。

> 同名 Tab（多个表里都有 `5.18-5.22`）聚合时只取第一次出现的，自动去重。
> 不开网页也行：直接编辑 `sheets.json`，或 `FEISHU_WIKI_URL` 单表模式仍向后兼容。

## 二、本地测试（第 7 步）

```bash
# 0) 装依赖（建议虚拟环境）
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 1) 验证飞书连通 + 列出所有 Tab（确认 wiki→sheet 链路通）
python scripts/fetch_sheet.py --list

# 2) 只读数据、不调用 Claude（先确认解析对不对）
python scripts/fetch_sheet.py --week 5.18-5.22 > week.json
cat week.json

# 3) 用上一步的本地 JSON 生成 HTML（省钱，不重复连飞书）
python scripts/generate_report.py --type weekly --data-file week.json
open reports/weekly/*.html          # macOS 直接打开看效果

# 4) 一步到位：连飞书 + 调 Claude 生成周报（自动选最近一周也可不带 --target）
python scripts/generate_report.py --type weekly --target 5.18-5.22

# 5) 生成月报（合并当月所有 Tab）
python scripts/generate_report.py --type monthly --target 2026-05

# 6) 本地预览导航页（manifest 用相对路径，需起静态服务器）
python -m http.server 8000   # 浏览器打开 http://localhost:8000/

# 7) 手动发一条飞书测试卡片
python scripts/send_feishu.py --url "https://example.com" --title "测试卡片"
```

## 三、部署到 GitHub Pages + 定时

1. 推到 GitHub 仓库（即 `GITHUB_REPO`，如 `username/feishu-weekly-report`）。
2. 仓库 **Settings → Pages**：Source 选 `Deploy from a branch`，分支选 `main`，目录 `/ (root)`。
   发布地址：`https://<username>.github.io/<repo>/`
3. 仓库 **Settings → Secrets and variables → Actions**：
   - **Secrets**：`FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`ANTHROPIC_API_KEY`、`FEISHU_WEBHOOK_URL`、（可选）`FEISHU_WEBHOOK_SECRET`
   - **Variables**：`FEISHU_WIKI_URL`（知识库链接）、（可选）`FEISHU_BASE_URL`
4. 触发方式：
   - 自动：每周五 18:00（北京）出周报；每月 1 号 09:00（北京）出**上月**月报。
   - 手动：Actions → 选 workflow → Run workflow，可填 `report_type` 和 `target`。
   - 跑完会自动 commit 报告 + 更新 `index.html` 列表 + 飞书群发链接。

> 部署细节、Secrets/Variables 清单、排错速查见 [DEPLOY.md](./DEPLOY.md)。

## 四、Sheet 结构约定

每个 Tab 是一周，命名如 `5.18-5.22`。列固定：

| 列 | A | B | C | D | E | F | G | H |
|---|---|---|---|---|---|---|---|---|
| 含义 | 日期 | 时段 | 图片(跳过) | 黄欣迪 | 图片(跳过) | 刘嘉晨 | 图片(跳过) | 王江楠 |

第 1 行表头、第 2 行人名，第 3 行起为数据。A 列是合并单元格（一天多行），脚本会向下填充。
只读 **A/B/D/F/H**。改人名或列位置：编辑 `scripts/fetch_sheet.py` 里的 `PERSON_COLUMNS`。

## 五、关于 Claude 调用与成本

- 模型 `claude-opus-4-7`，adaptive thinking，结构化输出（Pydantic 校验）。
- **Prompt caching**：「风格指南 + 本期全部流水账」放在 system 段并打了缓存点，
  同一期三个人的三次调用共享这段前缀——第 1 次写缓存，后两次命中缓存（约 0.1× 输入成本）。
  运行时 stderr 会打印每人的 `cache_read` token 数，可据此确认缓存生效。
