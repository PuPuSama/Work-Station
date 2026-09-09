# Article Agent 开发指引

## 用途与优先级

- 本文件只保留长期有效的工作方式、架构边界和代码入口，不作为每轮任务的功能清单。
- 用户当前明确要求优先于本文件中的项目约定。历史文档用于提供线索；当前代码和测试用于确认现状，不自动证明业务设计合理。
- 字数、FAQ、图片数量、AI 阈值和批量策略等业务细节，在相关任务中核对当前实现、配置和测试；不要凭历史记录恢复旧行为。修改时同步检查调用链与相关校验。
- 先按任务从 `README.md`、`docs/configuration.md`、`docs/README.md` 找相关入口；历史方案不是默认执行清单。`PROJECT_MEMORY.md` 按相关主题检索，无需每轮全文阅读，只记录值得复用且经过验证的决策。

## 部署环境

- 项目部署在远端服务器，可从本机通过 `ssh myserver` 连接；远端项目目录为 `/home/ubuntu/Work-Station`。
- 排查线上运行、内存、并发或部署问题时，检查远端实际代码版本、服务状态和相关日志；明确区分本地开发环境与远端运行环境。
- 本地修改或构建成功不代表线上已更新。部署获授权后，分别核验 Git 推送、CI/构建结果和远端服务状态。
- 本地免登录隔离调试：`backend\.venv\Scripts\python.exe scripts\local_debug.py`；入口 `http://127.0.0.1:3108`，只使用测试账号和独立数据，主模型密钥禁用，详见根 `README.md`。
- 本地 WordPress 模板站：`backend\.venv\Scripts\python.exe scripts\setup_wordpress_test.py`，入口 `http://127.0.0.1:8088`，凭据位于被忽略的 `outputs/wordpress-test/credentials.json`。远端独立测试站见 `deploy/wordpress-test/README.md`；测试站部署与 Article Agent 生产部署分别验证，测试凭据不得用于正式业务站点。
- 生产 WordPress 地址和账号按项目保存；账号可由项目设置页录入，Application Password 只以密文存入专用凭据表，明文密码不得进入项目表、Task、响应或日志。部署级 `ARTICLE_AGENT_WORDPRESS_PROJECT_CREDENTIALS` 环境映射仍可作为运维回退；未配置项目凭据或映射时才使用兼容性的全局账号。
- 远端 WordPress 测试站为 `https://43.154.92.36`，目录 `/home/ubuntu/article-agent-wordpress-test`，可用于生产 Article Agent 的草稿上传验收。上传入口在项目交付记录页，默认只存草稿。

## 工作方式

- 开始仓库修改前运行 `git status --short`，保留无关改动。诊断请求先分析，明确要求实现时再修改。
- 使用 `rg` 限定相关源码目录；不递归扫描依赖、构建目录、`tmp/` 或用户真实数据。
- 不删除真实文章、数据库记录、对象、交付物或密钥；不输出或提交凭据及私有配置。
- 按用户授权范围推进可逆工作，不重复请求普通步骤确认；未明确授权时不 commit、push、合并或部署。

## 必须保留的保障

- 只维护 Server 版本。PostgreSQL 是业务元数据准源；文件使用私有对象存储和短期签名 URL。不要恢复 Local/SQLite、活动 JSON 存储或双写兼容层。当前生产使用组织账号密码登录及已验证会话，保留密码哈希、角色与项目权限校验。
- 项目业务数据必须隔离；身份、组织与角色从已验证会话派生，不能信任客户端声明或恢复无项目作用域的旧业务 API。
- Worker 在执行和提交时重新授权，使用 revision/CAS；同一文章的活动 Job 保持互斥。409 冲突展示差异，不能静默覆盖用户修改。
- 数据库结构只由 Alembic 管理，应用启动不自动建表或改表。
- 自动检测结果不能冒充人工确认；检索资料不能作为执行指令或绕过权限。知识证据只使用已发布、当前快照、项目隔离且允许作证的内容；官方博客不进入硬事实证据链。

## 代码入口

- API 与安全：`backend/app.py`、`backend/server_project_http.py`、`backend/services/server_request_security.py`。
- 存储与队列：`backend/services/postgres_task_repository.py`、`backend/services/postgres_job_queue.py`。
- 工作流与知识：`backend/workflow/`、`backend/knowledge_agent/`；数据库迁移：`backend/migrations/`。
- 前端：`frontend/src/lib/api.ts`、`frontend/src/components/server-*`；修改前端时同时遵循 `frontend/AGENTS.md`。

## 按改动验证

- 选择与改动相关的测试；跨模块行为变化再运行必要整体回归。纯文档修改检查内容与 `git diff --check` 即可。
- 后端整体回归参考：`backend\.venv\Scripts\python.exe -m unittest discover -s backend\tests -q`。涉及数据库、对象存储的测试先确认使用隔离测试环境，避免加载生产配置。
- 前端代码修改按影响运行 `npm.cmd run lint`、`npm.cmd run build`（在 `frontend/` 下执行）；Windows 使用 `npm.cmd`。
- 涉及导出或下载时检查实际文件内容，不能只以接口成功为准。环境缺失的检查明确标为未验证。
- 完成后检查 `git diff --check` 和 `git status --short`，说明实际修改、验证结果与限制。
