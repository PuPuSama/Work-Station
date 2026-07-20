# Article Agent 项目记忆

本文件是本仓库的 Codex 项目地图。进入 `D:\article\article-agent` 后先读本文件，不要为了了解项目而递归扫描整个 `D:\article`。

## 快速定位

- 项目根目录：`D:\article\article-agent`
- 前端：Next.js，目录 `frontend/`
- 后端：FastAPI，入口 `backend/app.py`
- 工作流状态机：`backend/workflow/state_machine.py`
- 任务存储：`backend/services/task_repository.py`，运行数据为 `data/tasks.sqlite3`
- 后台任务：`backend/services/job_queue.py`，运行数据为 `data/job_queue.sqlite3`
- LLM 调用：`backend/services/llm.py`
- 正文与大纲提示词：`backend/prompts/article.txt`、`backend/prompts/outline.txt`
- 产品抓取：`backend/services/product_crawler.py`、`product_asset_pipeline.py`、`product_assets.py`
- 图片准备：`backend/services/article_images.py`
- Word 导出：`backend/services/docx_export.py`
- 交付打包：`backend/services/delivery_package.py`
- 前端 API 封装：`frontend/src/lib/api.ts`
- 前端类型：`frontend/src/types.ts`
- 主文章工作台：`frontend/src/components/article-workbench.tsx`
- 重构规划与验收：`docs/frontend-workflow-restructure-plan.md`

## 不要扫描的目录

除非任务明确涉及运行数据或打包产物，否则不要递归读取：

- `data/`：真实任务数据库、日志和运行状态。
- `dist/`：便携版成品。
- `packaging/build/`：PyInstaller 生成物，文件很多且不属于源码。
- `frontend/node_modules/`、`frontend/.next/`、`backend/.venv/`。
- `D:\article\<客户网站>\topic_NNN`：真实文章资产，只在用户指定客户和 topic 时读取。

查找源码优先使用 `rg` 或 `rg --files`，并限定到 `backend/`、`frontend/src/`、`docs/` 或用户指定路径。

## 启动与健康检查

```powershell
cd D:\article\article-agent
.\start.ps1
```

- 前端：`http://127.0.0.1:3000`
- 后端：`http://127.0.0.1:8000`
- 后端健康检查：`GET /api/health`
- 日志：`data/backend.log`、`data/backend.err.log`、`data/frontend.log`、`data/frontend.err.log`

若端口被占用，先查清 3000/8000 的监听进程，不要直接结束未知进程。Codex 工具启动的后台进程可能随工具会话退出；用户自己的 PowerShell 运行 `start.ps1` 最稳定。

## 页面结构

- `/`：项目列表。
- `/projects/[customer]/articles`：文章任务列表。
- `/projects/[customer]/articles/[taskId]?step=...`：单篇文章工作台。
- `/projects/[customer]/batches`：批量生成中心。
- `/projects/[customer]/batches/[batchId]`：批次详情和逐篇快速链接。
- `/projects/[customer]/deliveries`：交付记录。
- `/projects/[customer]/settings`：项目设置。

单篇工作台按五个业务阶段组织，内部步骤为：`titles`、`products`、`requirements`、`outline`、`article`、`review`、`media`、`files`。每个步骤已有独立组件 `frontend/src/components/article-*-step.tsx`，新增功能应放入对应步骤，不要继续把大块 JSX 堆回 `article-workbench.tsx`。

## 核心业务约束

- ZeroGPT 始终是人工环节，不自动操作 ZeroGPT 网站；支持粘贴检测结果和截图。
- 英文正文目标为 1000–1200 词，不做机械截断或自动压缩。
- 每个非 FAQ 的 H2 至少有两个 H3。
- FAQ 必须是正文最后一个 H2，固定三组问答，问题格式为 `**Q: ...**`。
- 一篇文章最多三张不同图片，包括首图。
- 首图位于第一个 H2 前；若 H1 和首个 H2 之间没有过渡段，需要补过渡段。
- 正文图片放在匹配内容的完整段落末尾，图片后写 `img.<实际文件名>.webp`。
- 官网品牌名是项目元数据；正文引用官网时，把首页链接附在准确品牌名上。
- 产品链接和 Markdown 表格必须正确导出为 Word 超链接和 Word 表格；标题中的超链接保持蓝色。
- Word 全文 Times New Roman，标题黑色；超链接蓝色。
- 交付根目录包含正文 Word、`D.docx`、所有最终图片和最后一次 AI 检测截图，不包含 `delivery_manifest.json`，图片不放子文件夹。
- `D.docx`：T 与正文 H1 完全一致；D 最多 150 个字符；K 固定六个逗号分隔关键词。
- 产品自动发现只接受官网域名，详情页和图片资产必须有证据对应；一篇最多选择三张不重复图片。
- 标题、产品、大纲和正文的长操作走 SQLite 后台队列；同一文章不能同时运行两个任务。
- 保存使用 revision 冲突保护。遇到 409 时显示本地/服务器逐行对比，不能静默覆盖草稿。
- “完全重写”和“仅重写正文”均已支持；修改上游内容时要明确失效哪些下游结果。

## 项目数据与配置

- 话题库：`D:\article\话题库`
- 知识库：`D:\article\Knowledge\<customer>`
- 文章目录：`D:\article\<customer>\topic_NNN`
- 项目不再按周创建重复任务；同步话题库会复用长期任务和完成状态。
- 首次从旧 `tasks.json` 迁移后，活动存储为 SQLite；旧 JSON 仅作备份。
- `.env` 是本机真实密钥来源，严禁输出或提交密钥。
- 实际 LLM 配置先检查 `.env` 和 `backend/services/llm.py`，不要只根据 `config.yaml` 猜测。
- 当前模型调用使用 OpenAI Responses API 流式响应；真实 base URL 可能是兼容网关。

## 验证命令

后端全量测试：

```powershell
cd D:\article\article-agent
backend\.venv\Scripts\python.exe -m unittest discover -s backend\tests -q
```

前端：

```powershell
cd D:\article\article-agent\frontend
npm.cmd run lint
npm.cmd run build
```

提交前再运行：

```powershell
cd D:\article\article-agent
git diff --check
git status --short
```

Windows 下使用 `npm.cmd`，避免 PowerShell 执行策略拦截 `npm.ps1`。

## 修改原则

- 开始前先运行 `git status --short`，保留用户和其他任务的未提交改动。
- 只读取本次任务相关文件；本文件已经提供结构，不要重复全仓库摸底。
- 诊断请求只分析，不擅自修复；实现请求才修改代码。
- 不自动删除任务目录、真实文章、图片、数据库或便携版成品。
- 不把 `data/`、`dist/`、`packaging/build/` 加入 Git。
- 修改提示词时同时检查系统硬约束、用户补充提示词和 `Chatgpt写作流程.docx` 的一致性。
- 涉及 Word、图片或交付时，除了单元测试，还应检查真实输出文件或页面行为。

## 当前基线

截至 2026-07-17：前端工作流重构、SQLite 单任务存储、持久化后台队列、版本冲突对比、三槽位图片管理、批次详情页和便携版打包流程已经落地。最近一次完整验收为后端 225 项测试、前端 ESLint、Next.js 生产构建和真实浏览器页面回归全部通过。后续以当前工作树和最新测试结果为准，不要仅凭这段基线判断完成状态。
