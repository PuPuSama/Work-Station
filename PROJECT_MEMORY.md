# Article Agent 项目记忆

> 供 Claude、Codex 以及人工开发者共享。本文档记录已验证的项目决策、导航信息和协作规约；不保存密钥、真实数据、日志内容或未经验证的猜测。
>
> 最后整理：2026-08-28。当前状态类信息可能过期，使用前先运行 git status --short 并核对代码与测试。

## 1. 项目身份与硬边界

- 仓库：D:\Project\article\article-agent-formal。
- 只维护 Server 版本。不要恢复 Local/SQLite、密码登录、旧无项目作用域 API、自动双写或 Local fallback。
- PostgreSQL 是 Task、Prompt、Job、Audit 和知识元数据的准源；文件、图片、截图和交付包使用私有对象存储及短期签名 URL。
- 业务路由必须带 Project scope，并从已验证会话派生 Organization、User 与角色。
- Worker 在执行和提交时都要重新授权，并用 revision/CAS 防止覆盖新版本。
- 数据库结构只由 Alembic 管理；应用启动不建表、不改表。
- 不删除真实文章、图片、导出物、数据库、对象存储内容或密钥文件。

根目录 AGENTS.md 是更高优先级的项目规则；下级目录中的 AGENTS.md 还会补充局部规则。本文档与它们冲突时，以 AGENTS.md、当前代码、测试和数据库实际状态为准。

## 2. 主要代码导航

| 领域 | 入口或准源 |
| --- | --- |
| FastAPI | backend/app.py |
| 项目级文章 API | backend/server_project_http.py |
| 请求安全与路由边界 | backend/services/server_request_security.py |
| PostgreSQL Task Repository | backend/services/postgres_task_repository.py |
| PostgreSQL Job Queue | backend/services/postgres_job_queue.py |
| 工作流状态机 | backend/workflow/state_machine.py |
| 知识库 | backend/knowledge_agent/ |
| Alembic | backend/migrations/ |
| 前端 API | frontend/src/lib/api.ts |
| Server 文章工作台 | frontend/src/components/server-article-workbench.tsx |
| 项目、批次、知识、设置 UI | frontend/src/components/server-* |

## 3. 配置与环境变量决策

### 3.1 文件职责

- config.yaml：所有运行形态共用的非敏感配置基线。
- config.ci.yaml：CI 差异 overlay，通过 extends: config.yaml 继承基线。
- config.docker.yaml：Docker 差异 overlay，通过 extends: config.yaml 继承基线。
- packaging/portable-config.yaml：Windows 便携包的独立配置，不与 Server YAML 混用。
- .env.example：环境变量模板；根目录 .env 是推荐的唯一项目环境文件。
- backend/.env：旧布局兼容回退，不作为新配置入口。

### 3.2 加载入口与优先级

- backend/config.py::initialize_environment() 是统一 dotenv 加载入口。
- 应用在 FastAPI lifespan 启动时加载；独立 CLI 在自己的 main() 中加载；业务模块不得各自调用 dotenv。
- 未指定 ARTICLE_AGENT_ENV_FILE 时：进程环境 > 根目录 .env > backend/.env 中根文件缺少的键。
- 若进程环境设置 ARTICLE_AGENT_ENV_FILE，只加载指定文件，不再读取两份默认文件。
- ARTICLE_AGENT_CONFIG 的相对路径按应用根目录解析；未指定时使用根目录 config.yaml。
- ARTICLE_ENV_FILE 是 Docker Compose/部署脚本选择宿主机 env_file 的变量；它与应用直接读取文件的 ARTICLE_AGENT_ENV_FILE 用途不同，不要混用。
- 不把 API key、数据库密码、OIDC secret 或对象存储 secret 写入 YAML、源码、测试夹具、日志或提交。

### 3.3 各运行形态

- Dockerfile 同时复制 config.yaml 和 config.docker.yaml；Compose 显式选择 /app/config.docker.yaml。
- CI 显式选择 config.ci.yaml。
- Windows 便携包启动脚本显式选择包内 config.yaml，包内只保留一份选定的 .env：根目录 .env 优先，只有不存在时才回退 backend/.env。

## 2026-08-28：项目任务并发

- 背景：各 Server 项目操作 runner 原先把并发硬编码为 1，批量执行相同动作时同一项目内只能串行处理。
- 决策：`config.yaml` 的 `server_jobs.project_concurrency` 作为基线，`ARTICLE_AGENT_PROJECT_JOB_CONCURRENCY` 可覆盖，默认值为 3，允许范围为 1–32；所有授权项目操作 runner 统一使用该值。
- 安全边界：同一篇文章仍不能同时拥有两个活动 Job；并发上限只减少不必要的串行等待，不取消 PostgreSQL 队列、重授权和 revision/CAS 保护。
- 影响：标题、产品、大纲、正文/重写、降 AI、SEO 复检、链接恢复、产品再发现和知识研究均接入统一并发配置；Workflow Assistant 自身的 `WORKFLOW_ASSISTANT_MAX_CONCURRENCY` 仍独立控制计划步骤并发。

## 2026-08-28：基础设施命名

- 决策：Docker Compose 项目、应用容器和共享网络统一使用 `work-station`；业务产品名 Article Agent、环境变量前缀、数据库名和 PostgreSQL/MinIO 数据卷名保持不变。
- 原因：只改部署命名即可统一本地与服务器运行环境，同时避免新建空数据卷或影响已有数据连接。

## 4. Claude/Codex 并行开发规约

1. 开始任务先运行 git status --short，再查看目标文件的 git diff；把已有修改视为用户资产。
2. 优先使用独立 worktree + 独立分支。若必须共用一个工作树，不要让两个 Agent 同时写同一文件；先在协作消息中明确负责的路径。
3. 只修改任务需要的路径，使用路径范围明确的 git add；不要使用 git reset --hard、git checkout -- 或清理命令覆盖他人改动。
4. 发现目标文件在另一 Agent 的未提交 diff 中时，先停在该文件并协调，不要用“格式化”或整文件重写制造冲突。
5. 提交前分别检查：修改文件、敏感内容、测试结果和 git diff --check。不要把 .env、outputs/、tmp/、真实数据或构建产物带入提交。
6. 普通开发任务默认不 commit、push、合并或部署；这些是独立的用户授权动作。部署若获授权，必须分别核验 Git push、CI/Actions 和生产服务状态。
7. 任务交接至少说明：修改了哪些路径、保留了哪些既有改动、运行了哪些验证、是否存在环境阻塞。

## 5. 标准验证

在仓库根目录执行：

    backend\.venv\Scripts\python.exe -m unittest discover -s backend\tests -q

    cd frontend
    npm.cmd run lint
    npm.cmd run build

    cd ..
    docker compose config --quiet
    git diff --check
    git status --short

配置整理的专门测试：

    backend\.venv\Scripts\python.exe -m unittest discover -s backend\tests -p test_config_loading.py -q

测试输出可能包含预期的 provider/队列模拟告警；以最终退出码和 unittest 汇总为准。不要为了验证而回显环境变量值。

## 2026-08-28：环境配置整理

- 背景：根目录和 backend 下存在重复 dotenv，加上 CI/Docker YAML 重复，容易出现加载顺序和 profile 漂移。
- 决策：由 `backend/config.py::initialize_environment()` 统一加载；`config.yaml` 做基线，CI/Docker 做 overlay；根目录 `.env` 为推荐入口，`backend/.env` 仅兼容回退。
- 影响：应用 lifespan、独立 CLI、Docker Compose 和 Windows 便携打包均遵循同一套规则；业务模块不再自行加载 dotenv。
- 验证：配置专项测试、后端全量测试、前端 lint/build、Docker Compose 配置校验和 `git diff --check` 均通过。

## 2026-08-28：文章产品确认直达大纲

- 背景：文章主流程在确认产品后需要直接进入大纲，不应把 Workflow Assistant 的计划确认作为中间步骤。
- 决策：产品选择保存成功后跳转文章工作台的 `?step=outline`；已有已保存产品时提供“进入大纲”入口。计划能力暂不删除，保留后台执行和审计能力，并在等待确认状态提供“取消计划”。
- 影响：主要涉及 `frontend/src/components/server-article-product-selection.tsx` 和 `frontend/src/components/workflow-assistant-workspace.tsx`；没有绕过产品保存接口，也没有改变计划后端状态机。
- 验证：`npm.cmd run lint` 通过（仅保留既有未使用变量 warning）；`npm.cmd run build` 通过；`git diff --check` 通过。

## 2026-08-28：本地 Docker 测试入口

- 决策：本地测试统一使用 `docker-compose.yml` 加 `docker-compose.local.yml`，前端只从 `http://127.0.0.1:3000` 访问；不要同时启动仓库内的 standalone Uvicorn/Next 进程。
- 本地 override：容器后端通过 `host.docker.internal` 访问现有宿主机映射的 PostgreSQL `55433` 和 Object Storage `59000`；生产部署只使用基础 Compose 文件，不使用该 override。
- OIDC：本地前端 origin 为 `http://127.0.0.1:3000`，Docker 后端回调为 `http://127.0.0.1:8000/api/auth/oidc/callback`；登录后仍回到 3000。该回调必须在身份提供方的允许列表中登记。
- 验证：local Compose 配置校验、容器 build/healthcheck、前端根页面、`/api/health` 代理和 OIDC start 的实际 `redirect_uri` 均通过。

## 2026-08-28：文章工作台操作区收敛

- 决策：文章工作台移除“官网产品重新发现”和“完全重写”两张 setup 卡片；“完全重写”入口移动到标题/状态栏右侧，与提醒和刷新操作并列。
- 安全边界：重写仍使用原有权限判断、确认勾选、`rewrite-from-scratch` 接口和 Revision/CAS；点击顶部入口只打开确认对话框，不直接执行。
- 影响：产品重新发现能力及服务端接口不删除，仅从文章工作台页面隐藏；重写成功后仍回到 setup 步骤。
- 验证：前端 lint/build 和 `git diff --check` 通过；本地 Docker 重建后再做页面冒烟测试。

## 2026-08-28：正文 Prompt 预览前置条件

- 背景：正文 Prompt 必须注入已确认标题和大纲；大纲为空时服务端会按契约返回 409，而不是生成不完整预览。
- 决策：写作要求面板在标题或大纲未就绪时禁用“预览正文 Prompt”，并显示先生成并确认大纲的提示；保留服务端校验，避免把预期状态当作异常重试。
- 影响：只调整 `frontend/src/components/server-writing-requirements-panel.tsx` 的预览按钮和提示，不改变 Prompt 模板、文章生成接口或服务端状态机。
- 验证：前端 lint/build 通过；本地 Docker 重建后通过健康检查，需在具体文章任务页做交互冒烟测试。

## 2026-08-28：官网服务入口与产品页分类边界

- 背景：官方站点的根级 `/oem/`、`/odm/` 和 `/live/` 页面是 OEM/ODM 服务或公司/直播入口，旧的通用 B2B 产品兜底会因路径、标题、图片和正文足够丰富而误判为产品详情。
- 决策：只对页面自身末段精确命中这些入口 slug 的页面增加非产品边界，并让页面自身语义在通用 B2B 兜底前生效；不把 `oem`、`live` 等词加入全站关键词黑名单。若同名 slug 位于标准 `/product(s)/.../` 或产品分类路径下，仍保留产品识别。
- 影响：`backend/knowledge_agent/wordpress.py`、`backend/services/product_crawler.py` 和 `backend/services/server_product_rediscovery.py` 同步边界；解析器版本升为 `official-web-page/4`，旧快照需要重新分类，既有数据库产品记录不自动删除或修改。
- 验证：WordPress 分类 34 项、产品爬虫 27 项、产品再发现 13 项和后端全量 1066 项测试通过；实站复核中首页为 knowledge_page、`/oem/` 与 `/live/` 为 knowledge_page、`/products/` 为 product_category、`/cuticle-revitalizer/` 为 product_detail；本地 backend Docker 重建并健康，`/api/health` 返回 200。

## 6. 当前交接快照

- 最近一次已核对的回退基线是 main 上的 50370da；使用前仍需以 git rev-parse HEAD 和远端状态重新确认。
- 当前工作树可能同时包含环境配置整理的未提交修改，以及用户已有的未跟踪 outputs/、tmp/、测试文件和 workflow assistant 文件；除非用户明确要求，不要删除、重置或纳入这些内容。
- 环境配置整理的目标是先让加载规则和 profile 结构稳定，再由用户决定是否提交或部署；不要把本地验证结果当作生产已部署证明。

## 2026-08-28：文章工作助手跳过检查与批量交付

- 决策：用户明确写“跳过/不用复检”时，服务端解析器移除 SEO `review` 步骤；否定表达不触发跳过。
- 决策：若初检 AI 率对应当前初始正文且严格低于 `ai_pass_threshold`（默认 30），Workflow Assistant 自动复用初始正文，跳过降 AI 与第二次检测；ZeroGPT 结果仍必须来自人工操作/确认边界。
- 决策：同一项目计划中多个 `package_delivery` 步骤全部成功后，工作助手显示“一键下载”并生成按文章分目录的聚合 ZIP；跨项目计划暂按项目分别下载，未完成或资产哈希变化时拒绝聚合。
- 影响：涉及 `backend/services/server_delivery_package.py`、`backend/workflow_assistant/http.py`、`frontend/src/components/workflow-assistant-workspace.tsx` 和 `frontend/src/types.ts`；聚合包仍走私有对象存储、`article.deliver` 重授权和短期签名 URL。
- 验证：后端全量 1068 项测试通过（跳过 301 项）；前端 `npm.cmd run lint` 无错误、`npm.cmd run build` 通过；本地 backend 容器健康。前端镜像重建因 Docker Hub 拉取 `node:22-alpine` EOF 未完成，已用本机验证过的 standalone 产物更新现有前端容器供本地测试，镜像本身尚未固化。

## 2026-08-31：工作流助手文章链路完整性

- 背景：自然语言规划依赖模型自行补齐文章步骤，曾接受从标题直接进入大纲且缺少产品选择、TDK 的残缺交付计划。
- 决策：规划器按每篇文章和当前 Task 状态校验必需前置步骤及顺序；新文章固定经过标题、产品生成、产品确认、大纲、研究和正文，普通正文必须复检，只有用户明确要求时才跳过；打包交付继续要求降 AI、恢复链接、图片、正文 Word、TDK 和交付包。缺失或乱序会要求模型重规划，连续不合格则拒绝计划。
- 影响：`backend/workflow_assistant/planner.py` 负责完整性校验和重规划，`backend/workflow_assistant/context.py` 向规划器提供任务级候选标题、候选产品和已确认产品计数；不改变人工 ZeroGPT 边界。
- 验证：工作流助手 231 项测试通过（跳过 35 项），后端全量 1071 项测试通过（跳过 301 项），前端 lint/build 与 `git diff --check` 通过。

## 2026-08-31：工作流助手对话区高度

- 背景：消息滚动区只有弹性高度，父容器没有确定高度，历史消息增加时会持续撑高整个页面。
- 决策：对话消息区使用 18rem–32rem 的响应式固定高度，历史消息改为内部滚动；切换会话、发送或收到新消息时自动滚到最新消息。
- 影响：只修改 `frontend/src/components/workflow-assistant-workspace.tsx` 的消息区布局和滚动定位，不改变会话保留时间、接口或消息数据。
- 验证：前端 `npm.cmd run lint` 和 `npm.cmd run build` 通过，`git diff --check` 通过。

## 7. 记忆更新格式

只有稳定且经过验证的决策才追加到本文档。临时调试过程放在任务消息或专门的 runbook，不要持续膨胀本文件。建议使用以下格式：

    ## YYYY-MM-DD：简短主题

    - 背景：为什么改变。
    - 决策：最终采用什么约定。
    - 影响：哪些入口、文件或验证受到影响。
    - 验证：运行了什么，结果是什么。
