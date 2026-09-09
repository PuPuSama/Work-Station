# 成品文章自动上传 WordPress：实施方案

初稿日期：2026-09-07；2026-09-09 基于最新 main 整理上线变更。

用户已确认：自动上传为草稿，检查后在 WordPress 发布。已实现单篇成品上传和项目级连接设置；生产验收使用服务器上的独立 WordPress 测试站，记录见 `deploy/wordpress-test/README.md`。下文用户流程包含后续设计，未实现的排队、分阶段进度和批量上传不能当成当前能力；当前上传是同步请求，保存上传状态和媒体检查点，失败后可重试。

## 1. 目标与首版范围

在项目设置中保存每个项目自己的 WordPress 站点地址和账号，单篇成品点击“上传 WordPress 草稿”，自动传入标题、净化后的正文、已准备图片及 TDK 元数据，返回可继续编辑的草稿入口。Application Password 由服务端加密保存；同一部署可以按项目使用不同凭据，地址为空时回退到部署级默认地址。保留 Word/TDK/ZIP 交付能力；批量计划接入仍是下一阶段。

首版一个项目绑定一个站点，只处理标准文章 post；不自动公开发布、不自动覆盖 WordPress 已被人工修改的内容、不重写正文。SEO 插件适配在确认站点插件后单独验收，不能把普通摘要冒充 meta description。

## 2. 当前依据与可复用入口

| 现有能力 | 入口及实施含义 |
| --- | --- |
| WordPress/官网内容读取 | `backend/knowledge_agent/wordpress.py`、`web_ingestion.py`；属于采集能力，不能当成已有发布客户端 |
| 项目授权、Job 排队及执行重授权 | `backend/server_project_http.py`、`backend/services/authorized_job_queue.py`、`postgres_job_queue.py`；扩展明确的上传 operation，复用文章活动任务互斥 |
| 成品正文、准备好的图片及 Word 导出 | `backend/services/server_docx_export.py`；复用成品版本选择与图片哈希校验，直接读取结构化内容，不反解析 Word |
| TDK | `backend/services/server_tdk_export.py`；当前生成要求已有 Word 产物，并保存 `task.tdk`。上传不应隐式重新调用模型或强制生成 Word |
| 私有交付物 | `backend/services/server_delivery_package.py`；对象下载后由后端传给 WordPress，不把短期签名 URL 留在公开正文 |
| 操作界面 | `frontend/src/components/server-article-workbench.tsx`、相关批量卡片、`frontend/src/lib/api.ts` |
| 生产部署 | `.github/workflows/deploy-production.yml`：main 的 push 会触发部署；PR 执行测试构建，部署 job 限制为 main push |

线上版本在本次对话前一轮经 `ssh myserver` 核对为 `29de408`。实施和发布时需要重新确认，不能把该版本号长期当作当前线上状态。

## 3. 用户流程

1. 项目设置增加“WordPress 站点地址”、账号和“测试连接”。地址按项目保存，测试连接可以使用尚未保存的输入值；点击保存后才成为该项目的上传地址。用户名只回显配置状态，Application Password 不回显；项目级凭据可由界面加密保存，也可由项目 ID 解析服务端的环境变量引用。
2. 文章交付区域显示上传准备情况：当前正文版本、图片是否准备好、站点是否可用、哪些 SEO 字段尚未支持。缺少关键条件时给出处理入口。
3. 用户点击一次“上传草稿”；界面显示真实阶段：排队、上传图片、写入草稿、核对结果。离开或刷新后仍可查看状态。
4. 完成后显示“WordPress 草稿已创建”、上传时间、源版本和“打开编辑页面”。草稿访问可能仍需要登录 WordPress；这与 Article Agent 本地免登录是两回事。
5. 批量计划显式选择上传动作后，逐篇复用相同任务处理；卡片区分成功、失败、结果待确认和源文已更新，只重试需要处理的文章。
6. “AI 率过高”等已有质量提示继续展示，上传成功不能变成“质量已人工确认”。首版只写草稿。

## 4. 内容及字段映射

| Article Agent 成品 | WordPress |
| --- | --- |
| 当前成品标题 | `title`；正文避免再插入重复 H1 |
| 成品 Markdown/结构化正文 | 转换并净化为 HTML，保留 H2/H3、列表、表格、链接、加粗及 FAQ；不承诺任意主题下像素一致 |
| 图片及插入位置 | 后端上传到 `/wp/v2/media`，将正文图片引用替换为返回的持久 URL；保留 alt 文本和尺寸 |
| 首图 | `featured_media`；正文是否同时保留遵循原插图布局 |
| 分类、标签 | 从目标站点读取并使用其 ID；首版选择已有项，不悄悄创建分类 |
| 文章标识 | `slug`；处理站点自动调整后的实际 slug |
| TDK | 标题和 SEO 标题分别建模；D、K 对接已确认 SEO 插件的可写字段。六个关键词不自动等同六个标签 |

成品选择必须绑定实际正文、图片与元数据版本，不能只检查“曾经导出过 Word”。生成、润色、图片或 TDK 有更新时显示过期状态。若上传时缺少 SEO 元数据，可明确保存不含 SEO 的草稿；不暗中追加模型调用。

使用 WordPress Application Password，通过 HTTPS 调用 REST API；不存普通管理员登录密码。该凭据继承所属用户权限，应创建专用账号并验证编辑文章、上传媒体的能力。没有公开发布需求，不要求提供管理员账号。[官方认证文档](https://developer.wordpress.org/rest-api/using-the-rest-api/authentication/)

WordPress 文章接口支持草稿、标题、正文、分类、标签及特色图；媒体接口负责文件上传和替代文本。[文章接口](https://developer.wordpress.org/rest-api/reference/posts/) · [媒体接口](https://developer.wordpress.org/rest-api/reference/media/)

## 5. 后端设计与失败恢复

当前已实现项目作用域的上传 API：`POST /api/projects/{project}/tasks/{task_id}/wordpress-upload`，沿用当前 actor/project 授权和 Task revision/CAS。项目资料中的 `wordpress_url` 由 Alembic 迁移持久化；`POST /api/projects/{project}/wordpress/test-connection` 可测试已保存地址或本次输入地址，不会写入密码。上传状态同时记录规范化的目标站点，切换项目地址后不会把旧站点草稿当成新站点结果。项目设置页提供 `GET/PUT /api/projects/{project}/wordpress/credentials`，账号按项目保存；Application Password 只在保存时提交，服务端使用 `ARTICLE_AGENT_WORDPRESS_CREDENTIALS_KEY` 加密后写入 `wordpress_project_credentials`，读取接口只返回是否已配置、用户名和 Revision，不返回密文或明文。若项目尚未保存凭据，服务端仍可按项目级环境映射或兼容性的全局环境变量解析；本地调试启动器只从被 Git 忽略的测试凭据文件注入测试环境。

### 项目级 WordPress 凭据

不同项目可以在项目设置页分别保存 WordPress 账号，也可以继续使用部署级环境映射。界面保存的 Application Password 使用服务器外部密钥加密，明文不进入项目表、Task、API 响应或日志；更换密码时输入新值，用户名不变时留空则保留已保存密码，修改用户名必须同时提交新密码。`article.edit` 才能读取账号状态或保存账号，读取结果不会包含密码；上传和连接测试仍需 `article.deliver`。服务器必须设置稳定的 `ARTICLE_AGENT_WORDPRESS_CREDENTIALS_KEY`（至少 32 个字符），轮换密钥前需完成数据迁移，否则旧密文无法解密。

若采用运维托管而不在界面保存账号，可在服务端设置一份非敏感的映射和每个项目自己的秘密环境变量。映射键必须是 Article Agent 的项目 ID，值只包含环境变量名称，不包含密码：

```json
{
  "site-a.example.com": {
    "username_env": "WP_SITE_A_USERNAME",
    "app_password_env": "WP_SITE_A_APP_PASSWORD"
  },
  "site-b.example.com": {
    "username_env": "WP_SITE_B_USERNAME",
    "app_password_env": "WP_SITE_B_APP_PASSWORD"
  }
}
```

将 JSON 放入 `ARTICLE_AGENT_WORDPRESS_PROJECT_CREDENTIALS`，并在部署秘密存储中分别提供上述四个环境变量。映射一旦启用，未列出的项目会拒绝上传，不会回退到其他项目或全局账号；未启用映射时仍兼容原有的 `ARTICLE_AGENT_WORDPRESS_USERNAME` 与 `ARTICLE_AGENT_WORDPRESS_APP_PASSWORD`。明文密码不进入项目表、Task、响应或日志；界面保存的密文只进入专用凭据表。

首版上传状态放在 Server Task JSONB 的 `wordpress_upload` 中，使用现有审计写入和 revision/CAS；项目凭据密文和 Revision 由 Alembic 管理的独立表保存。环境映射仍只保存环境变量引用，不把外部写入结果只放在进程内存或正文 JSON 中：

- 连接：organization/project、规范站点 URL、用户名、凭据引用或密文及密钥版本、连接 revision、能力检查结果。密钥位于部署环境的秘密存储，不与密文一起存数据库；轮换后可重新测试。
- 上传：upload_id、actor、连接 revision、文章/正文/图片/TDK 的版本与哈希、Job ID、状态、WordPress post ID、远端修改时间与内容哈希、脱敏错误。
- 媒体映射：连接身份、资产 ID/哈希、WordPress media ID/URL、状态。更换站点或连接身份不能沿用旧映射。

队列领取任务后重新授权、核对源版本并保存上传快照。每次外部副作用前检查取消和权限；完成后的记录提交仍有 CAS。WordPress HTTP 调用不占用长数据库事务，逐张图片上传、设置体积及响应大小上限、短时持有字节缓冲。

同一项目、文章、目标连接、源内容和上传模式建立唯一键，重复点击返回已有 Job。完成结果持久化，普通重试复用已确认的 media ID/post ID。

**不能声称本地唯一键可保证 WordPress 端恰好一次写入。** 核心 REST 创建接口未提供可依赖的通用幂等键。POST 已发送但超时、或收到结果前进程退出时，标记“结果待确认”，禁止自动再次 POST。通过已知 ID 读回；ID 未知时提供目标站点核对和明确关联操作。仅凭同名标题或 slug 不自动认定属于本任务。需要完全自动消除此窗口时，再考虑 WordPress 端小插件提供唯一上传标识和原子幂等创建；不纳入首版默认依赖。

远端编辑冲突也不能仅靠“读 modified 后写入”声称原子 CAS。首版已存在草稿默认不自动覆盖：源文改变后提示用户处理；不自动修改已发布文章。WordPress 端可靠的原子条件更新属于后续插件能力。

失败处理：

- 401/403：提示凭据或权限问题，停止自动重试。
- 429/网络失败：GET 按 Retry-After/有上限退避；POST 只有能确认未写入时才重试，否则进入待确认。
- 图片部分成功：保留映射，只补缺失图片。失败不自动删除已经上传的媒体或草稿。
- 本地 CAS 失败但远端已创建：保留外部回执，显示源版本已变化，不能丢失 post ID 或将远端结果当成不存在。
- 批量限速与现有并发控制共同生效；同站点首版限制并行上传任务数，测试后决定数值，不因上传加入而提高写作并发上限。

站点 URL 必须检查协议、DNS/IP、重定向和响应大小，阻止凭据随跨站重定向泄露及访问服务器内部地址。项目地址保存和测试连接共用同一校验：生产要求 HTTPS 并拒绝私网目标；本地调试只允许 loopback HTTP。项目地址为空时才使用服务端默认环境变量地址。

## 6. 本地免登录调试与 WordPress 模板站（本轮实施）

使用 `scripts/local_debug.py` 启动独立 PostgreSQL、MinIO，以及真实后端和 Next 前端。只创建测试组织、测试账号和测试项目；不复制现有数据库。

- 前端：`http://127.0.0.1:3108`；后端：`127.0.0.1:8108`。
- PostgreSQL：`127.0.0.1:55438/article_agent_debug`；MinIO：`127.0.0.1:59018`，独立 bucket 和 volumes。
- `scripts/local_debug_server.py` 是独立开发入口，生产 Dockerfile 不复制 scripts，生产仍用 `app:app`。
- 开发入口拒绝非预定数据库、对象存储和配置；只接受 loopback 请求及允许的 Host/Origin。使用独立测试 Cookie，不替换其他本地服务的会话。
- 第一次查询认证状态时自动签发固定测试账号会话；后续请求继续经过真实数据库会话校验和项目权限检查。非法/被撤销会话不能自动补签伪装有效。
- Alembic 和现有 checkpoint setup 由启动器显式执行；应用启动不增设 DDL。
- 不继承应用密钥，不读取仓库 `.env`；默认禁用模型密钥。免登录完成不等于真实模型写作/WordPress 上传已验证。
- WordPress 模板站由 `docker-compose.wordpress-test.yml` 启动，使用独立 MariaDB、WordPress 6.8、模板主题和 mu-plugin；站点地址为 `http://127.0.0.1:8088`。
- 执行 `backend\\.venv\\Scripts\\python.exe scripts\\setup_wordpress_test.py` 完成初始化。后台网页登录使用 `article_agent_publisher` 和脚本中的本地测试密码；REST 上传使用被忽略的 `outputs/wordpress-test/credentials.json` 中的 Application Password，两者不是同一个密码。重复执行会同步测试账号密码并复用已保存的 Application Password。
- 模板站仅用于本地验证，明确设置为 WordPress `local` 环境以支持本地 HTTP Application Password；生产连接仍强制 HTTPS。
- 启动 `local_debug.py` 时若测试凭据存在，会在当前进程环境注入 WordPress 连接；后端不把密码写入 Task、日志或响应。

在仓库根目录启动：

```powershell
backend\.venv\Scripts\python.exe scripts\local_debug.py
```

首次需要 Docker、Python 虚拟环境和前端依赖。终端保持运行，Ctrl+C 停止开发前后端；测试数据库和对象存储数据保留。可用 `docker compose -f docker-compose.debug.yml stop` 停止专属依赖，不使用 `down -v` 清空数据。

## 7. 我与 Luna 的任务分工

2026-09-09 补充：已搭建独立远端测试站 `https://43.154.92.36`，安装与验收范围见 [远端测试站说明](../deploy/wordpress-test/README.md)。原本仅在单篇工作台“图片与交付”中的上传入口，现已增加到项目“交付记录”页；上传仍为草稿。站点已部署，Article Agent 功能分支尚未合并或部署。

| 顺序 | 负责人 | 独立交付与边界 |
| --- | --- | --- |
| 本轮 A | Luna，实现后由我审查 | 独立本地认证入口、配置拒绝和会话专项测试；不改生产认证模块 |
| 本轮 B | 我 | 独立 Compose、启动器、种子数据、真实浏览器验证、本方案及 Git 分支 |
| WP-1 | 我 | 数据模型、连接密钥保存、权限与 SSRF 边界、队列契约、重复写入及冲突策略 |
| WP-2 | Luna | 按固定契约实现 Markdown→HTML 纯函数及夹具测试；覆盖 H1、表格、链接、图片映射、净化，不接凭据或发网络请求 |
| WP-3 | Luna | 按已确定 API 实现连接设置和上传状态 UI；只改指定组件，不自行修改接口或权限 |
| WP-4 | 我 | WordPress 客户端、媒体上传、草稿落库、恢复逻辑、单篇真实集成（已完成首版） |
| WP-5 | Luna | 按故障矩阵补 HTTP 模拟和 UI 回归用例；不以 mock 代替真实 WordPress 验收 |
| WP-6 | 我 | 批量计划接入、SEO 插件适配取舍、整体审查、真实测试站点及发布前验证 |

先确定契约，再让 Luna 同时处理互不重叠的文件。每项交付包含修改范围、测试命令、已知限制。我审查合并，避免多个 Agent 同时修改认证、队列或同一前端组件。以上 WP 任务是后续实施安排，不代表已派发或完成。

## 8. 验收与实施顺序

1. 本地基础：无需 OIDC 打开项目；刷新可用；匿名业务请求仍被拒绝；非法 Cookie、跨组织访问、非本地请求被拒绝；生产入口不支持免登录。
2. 单篇草稿：独立本地 WordPress + 独立数据库，用脱敏成品实际上传；读回 `status=draft`、来源哈希、任务 ID 和模板页面。含媒体的完整上传夹具和浏览器编辑页检查仍需补齐。
3. 恢复：重复点击、图片中断、写入超时、worker 重启、源版本变化、远端人工编辑、权限撤销；核对不会盲目重复创建或覆盖。
4. 批量：3 篇足够，覆盖成功、权限/连接失败、AI 高分警示；刷新后能定位失败文章，成功篇不再上传。
5. SEO：确认具体插件及版本后验证读写；不支持时明确显示未同步，而非成功。
6. 工程：专项测试后必要整体回归、前端 lint/build、diff 检查、迁移升级检查。测试环境不可用就明确未通过，不使用生产文章代替样本。

建议先完成单篇草稿再接批量，不一次性加入公开发布、排期、多站点、双向同步和自定义文章类型。

## 9. Git 操作说明

已经执行 `git switch -c codex/wordpress-upload`，相当于“从当前代码新开一条开发线并切过去”。旧式写法是 `git checkout -b ...`，不需要重复执行。

未提交文件会随当前工作目录保留，新分支不等于隔离了数据库，也不等于已保存提交。已有 AGENTS/项目记忆文档改动保留，用户已有 `docs/cliproxyapi-deployment-and-usage.md` 不纳入本功能提交。

后续流程：在功能分支开发 → 本地测试和审查 → 用户授权后按文件范围 commit → push 功能分支并创建 PR → CI 测试 → 用户授权合并 main → 自动部署 → 用 `ssh myserver` 核对 `/home/ubuntu/Work-Station` 版本与服务。

注意：当前工作流不对任意功能分支 push 自动运行质量 job，PR 到 main 才运行；main push 会部署。不要为了“只测试一下构建”直接推 main。

## 10. 后续接入前需要的信息

- 目标是自托管 WordPress 还是 WordPress.com、站点地址及可用测试站点。
- 使用的 SEO 插件、编辑器/主题，以及默认分类需求。
- 专用应用密码在功能完成后通过受保护配置入口录入，不写在方案、Git 或聊天中。

这些信息不阻塞本地免登录开发，也不要求现在提供生产凭据。

## 11. 本轮实际验证结果

- 新分支已创建并切换，未 commit、push、合并或部署。
- 独立 PostgreSQL 和 MinIO 启动成功，Alembic 升级及现有 checkpoint setup 成功；重启保留测试项目。
- 调试入口、原 Server session、请求安全及 PostgreSQL session 相关测试共 29 项通过。
- 通过真实 Next 代理进行 11 项 HTTP 检查：自动登录、项目可见、会话复用、匿名拒绝、非法/空 Cookie、外部 Origin、跨组织隔离、撤销旧会话、撤销身份不补签及测试状态恢复。
- 额外验证退出登录只删除调试 Cookie，随后可重新自动进入；生产会话 Cookie 不受退出操作影响。
- 应用内浏览器实际显示 Local Debug Project 和项目操作入口，没有要求 OIDC 登录；服务重启后再次打开仍可访问。
- WordPress 模板站已通过 `scripts/setup_wordpress_test.py` 初始化；独立 MariaDB、WordPress、模板主题和 mu-plugin 正常运行。
- 通过本地 Article Agent API 创建脱敏 Task、写入测试正文后，真实调用 `wordpress-upload` 创建 WordPress 草稿；REST 回读确认 `status=draft`、标题、HTML、来源哈希和任务 ID，重复上传返回 `already_exists`。
- 项目设置页已实际保存 `wordpress_url`，并保存项目级账号后通过“测试连接”连接本地模板站；刷新后只回显用户名和“已配置”，不回显 Application Password。上传服务读取该项目地址和凭据，空值才回退部署级默认地址。WordPress 客户端、凭据加密和映射专项 13 项、请求安全专项（含凭据路由）通过；后端完整回归 1147 项通过（307 skipped）；Alembic 已在隔离 PostgreSQL 升级到 `20260907_0037`；前端 `npm.cmd run lint` 和 `npm.cmd run build` 通过；`git diff --check` 通过。
- 已验证无媒体正文的真实草稿链路；媒体上传使用已准备 WebP 的完整端到端样本、浏览器编辑页/预览、批量计划接入、生产凭据配置和 SEO 插件字段仍未验证。
