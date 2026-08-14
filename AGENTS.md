# Article Agent 项目地图

本仓库位于 `D:\Project\article\article-agent-formal`，只维护 Server 版本。不要恢复 Local/SQLite 分支、密码登录、旧无项目作用域 API 或双写兼容层。

## 快速定位

- FastAPI 入口：`backend/app.py`
- 项目级文章 API：`backend/server_project_http.py`
- 请求安全与路由边界：`backend/services/server_request_security.py`
- PostgreSQL Task Repository：`backend/services/postgres_task_repository.py`
- PostgreSQL Job Queue：`backend/services/postgres_job_queue.py`
- 工作流状态机：`backend/workflow/state_machine.py`
- 知识库：`backend/knowledge_agent/`
- Alembic：`backend/migrations/`
- 前端 API：`frontend/src/lib/api.ts`
- Server 文章工作台：`frontend/src/components/server-article-workbench.tsx`
- Server 项目、批次、知识与设置组件：`frontend/src/components/server-*`

## 不要扫描或修改的内容

除非任务明确要求，否则不要递归读取或清理：

- `frontend/node_modules/`、`frontend/.next/`、`backend/.venv/`
- `dist/`、`packaging/build/`、`tmp/`
- 用户真实文章、图片、导出文件、数据库和对象存储内容

源码检索优先使用 `rg`，并限定到相关目录。

## Server 硬边界

- Task、Prompt、Job、Audit 与知识元数据以 PostgreSQL 为准。
- 文件、截图、图片和交付包使用私有对象存储及短期签名 URL。
- 所有业务路由必须带 Project scope，并从已验证会话派生 Organization、User 与角色。
- Worker 必须在执行和提交时重新授权，并使用 revision/CAS 防止覆盖新版本。
- 数据库结构只由 Alembic 迁移；应用启动不得建表或改表。
- 不新增 SQLite、JSON 活动存储、本地任务目录准源、自动双写或 Local fallback。
- 不恢复 `/api/tasks*`、`/api/batches*`、密码登录或 Local Dashboard/Config API。
- 官方博客仅可作为正文引用资料，不能进入 Evidence Pack、Hard Fact 或证据引用链。
- Agent 只检索已发布、当前快照、项目隔离且允许作证的内容。

## 核心业务约束

- ZeroGPT 始终人工操作。
- 英文正文目标 1000–1200 词，不机械截断。
- 非 FAQ 的 H2 至少包含两个 H3；FAQ 是最后一个 H2，固定三组 `**Q: ...**` 问答。
- 每篇最多三张不同图片，包括首图。
- 官网品牌链接、产品链接、Markdown 表格和图片位置必须正确导出到 Word。
- `D.docx` 的 T 与 H1 一致，D 最多 150 字符，K 固定六个逗号分隔关键词。
- 产品发现只接受官网域名；产品页和图片资产必须有证据对应。
- 标题、产品、大纲和正文长操作进入 PostgreSQL Queue；同一文章不能并发两个活动任务。
- 409 revision 冲突必须展示差异，不能静默覆盖。

## 修改原则

- 开始前运行 `git status --short`，保留无关的用户改动。
- 诊断请求只分析；实现请求才改代码。
- 不删除真实数据、对象、产物或密钥文件。
- 配置和密钥不得输出或提交。
- 修改提示词时同时检查系统硬约束、项目提示词和相关测试。
- 涉及产物时，除单元测试外还要验证真实下载/导出边界。

## 验证

```powershell
backend\.venv\Scripts\python.exe -m unittest discover -s backend\tests -q

cd frontend
npm.cmd run lint
npm.cmd run build

cd ..
git diff --check
git status --short
```

Windows 下使用 `npm.cmd`。以当前工作树和最新测试结果为准。
