# Knowledge Agent M7：服务器切换、备份恢复与回滚 Runbook

## 1. 目的和硬门禁

本文是 M7 正式服务器切换的操作准源。它不表示当前系统已经可以上线。
任何一项未满足都停止切换：

1. `CURRENT_SERVER_CUTOVER_CAPABILITIES` 没有缺项；
2. 正式身份来源已完成 Subject -> Workspace User 映射；
3. 所有项目级 HTTP 路由、Retriever、Worker 和对象下载均重新授权；
4. Task/Job 已完成双读比对并准备 PostgreSQL 单写；
5. PostgreSQL 与对象存储的备份恢复演练已有本次发布对应的日期、操作者和证据；
6. 对象 Bucket 私有、启用服务端加密，并已确定版本、生命周期和异地备份策略；
7. 回滚负责人、维护窗口、RPO 和 RTO 已明确。

当前代码会让 `server_cutover` 检查失败。这是有意的 fail-closed 状态，不能通过
修改前端或传入请求字段绕过。

## 2. Preflight 接口和安全输出

以下是 Windows 本地候选验收命令，位置为正式 worktree 的 `backend`。生产发布时必须在
冻结的候选环境执行同一 Python 模块与参数；解释器路径和 Evidence 只读挂载由部署包确定，
不得因为生产环境使用容器而跳过该门禁。

```powershell
$env:ARTICLE_AGENT_CONFIG = `
  'D:\Project\article\article-agent-formal\config.ci.yaml'
$env:ARTICLE_AGENT_RECOVERY_EVIDENCE_PUBLIC_KEY = `
  '<由受控 Secret 注入的 Base64 32-byte Ed25519 raw public key>'
.\.venv\Scripts\python.exe -m knowledge_agent.m7_deployment_preflight `
  --recovery-evidence 'D:\controlled-evidence\recovery-envelope-v1.json' `
  --release-commit '<完整 40-hex 候选 Commit>'
```

不接受任何人工布尔恢复声明。Evidence 必须是严格
`article-agent.recovery-evidence.v1` Envelope：由独立 verifier 使用 Ed25519 签名，绑定上述
Commit、代码预期 Alembic Head、演练时间、独立 Operator/Reviewer、数据库恢复 Manifest/
固定检查结果、四类对象 Hash 抽样以及 RPO/RTO。唯一信任根是独立公钥环境变量；公钥缺失、
签名/指纹不符、Commit/Head 漂移、过期或任一恢复不变量失败都必须 fail closed。
完整字段契约见 `docs/architecture/m7-deployment-capability-evidence.md`。

当前仓库只消费和验证 Envelope，不提供 Capture/签发工具，也没有执行真实恢复演练。
Envelope、Dump、Inventory、抽样明细、检查输出和 Reviewer 记录必须保存在外部受控不可变
证据系统；仓库、聊天、普通日志和公开 Preflight 只能保存非敏感 Artifact ID/Digest 与
固定状态。

退出码：

- `0`：全部门禁通过；
- `2`：至少一项不通过；
- 其他非零值：命令环境或程序异常，同样停止发布。

输出只允许包含 `ready`、Check ID、布尔状态和固定说明。不得输出数据库 URL、
Session Secret、Embedding Key、对象存储 Key、供应商响应正文或客户内容。

## 3. PostgreSQL 备份与恢复演练

以下命令在持有备份权限的服务器终端执行。连接凭据通过受控 Secret 注入，
不要写进命令历史、仓库或报告。

### 3.1 备份

1. 记录发布候选 Commit、Alembic Head、UTC 时间和数据库实例标识。
2. 在一致性快照窗口暂停写入口和 Worker Claim。
3. 使用与 PostgreSQL 17 兼容的 `pg_dump`：

```powershell
pg_dump --format=custom --no-owner --no-acl `
  --file=article-agent-before-m7.dump `
  $env:ARTICLE_AGENT_BACKUP_DATABASE_URL
```

4. 对 Dump 文件计算 SHA-256，保存到受控发布证据，不在聊天或普通日志中上传数据库内容。
5. 恢复写入口前，记录对象存储 Inventory/版本水位，形成同一恢复点的证据对。

### 3.2 恢复验证

必须恢复到新的隔离数据库，禁止覆盖开发库或生产库：

```powershell
createdb article_agent_restore_drill
pg_restore --clean --if-exists --no-owner --no-acl `
  --dbname=$env:ARTICLE_AGENT_RESTORE_DATABASE_URL `
  article-agent-before-m7.dump
```

恢复后至少验证：

- `alembic_version = 20260806_0019`；
- `vector` 扩展存在；
- Organization、Project Ownership、Membership、Audit、Knowledge、
  External Identity、Task、Batch、Job 表均可读取；
- `workspace_users.session_version` 非空且大于 0；
- 复合租户外键仍存在；
- Audit Event 更新和删除仍被 Trigger 拒绝；
- `knowledge_sources.current_snapshot_id` 与 `pending_snapshot_id` 都由精确
  Project/Source/Snapshot 复合外键约束，且两个非空指针不能相同；
- `source_snapshot_review_receipts` 的复合主键、Project-scoped Receipt 唯一约束、枚举/
  Reviewer/Reason CHECK 和 Latest B-tree 索引存在；
- Snapshot Review Receipt 更新和删除被 `trg_snapshot_review_receipts_append_only` 拒绝；
- 旧 Published Source 的 Legacy Approval 只绑定其 Current Snapshot；无 Current 的多
  Snapshot Source 没有被迁移脚本猜测批准；
- Task/Job 迁移工具的数量、状态分布和内容 SHA-256 摘要一致；
- 抽样知识资产 URI 能在恢复用对象存储中读取，下载字节 SHA-256 与数据库一致。

演练完成后删除隔离恢复环境时，先再次确认目标实例标识；不要对未确认路径或共享实例
执行清理命令。

## 4. 对象存储备份与恢复

正式 Bucket 必须：

- 阻止公共访问，不使用长期公共 URL；
- 默认服务端加密，生产不使用 `ARTICLE_AGENT_OBJECT_STORE_SSE=none`；
- 启用供应商支持的对象版本或不可变备份；
- 设置异地复制/备份与生命周期，生命周期不得早于数据库证据保留期；
- 保存 Bucket Policy、加密 Key 策略和 Inventory 配置的版本化副本。

产品图片对象使用：

```text
organizations/{organization_id}/projects/{project_id}/blobs/{prefix}/{sha256}
```

恢复演练不能只验证“对象数量”。从 PostgreSQL 抽取一组
`knowledge_assets.artifact_uri + content_hash`，在隔离 Bucket 恢复并重新计算
SHA-256。至少覆盖产品主图、Gallery 图、私有文档和标准化产物。

因为 S3 与 PostgreSQL 没有跨系统事务，备份窗口必须记录数据库快照时间和对象
Inventory/版本水位。恢复时先保持应用离线，完成 URI 存在性和哈希抽样，再开放读流量。

### 4.1 Orphan 对账与延迟清理

Orphan 清理不是应用启动步骤，也不在上传失败请求里执行。它只允许 Active Org Admin
使用真实 Organization/User/Project 身份显式运行。命令输出只有项目和数量，不输出
Bucket、Key、URI、客户正文或供应商错误。

在 Windows 运维终端中先通过安全环境注入数据库和
`ARTICLE_AGENT_OBJECT_STORE_*` 配置，再运行“不删除对象”的观察；该命令会更新
`object_orphan_observations`，但不会删除 Asset 行或物理对象：

```powershell
Set-Location D:\Project\article\article-agent-formal\backend
.\.venv\Scripts\python.exe -m knowledge_agent.m7_object_orphans observe `
  --organization-id '<organization-id>' `
  --user-id '<active-org-admin-user-id>' `
  --project-id '<project-id>'
```

默认保留期为 7 天，代码拒绝短于 24 小时。必须至少在两个独立时间点观察，且期间对象
Fingerprint 不变；恢复引用或 Fingerprint 变化都会重新开始窗口。清理前先保存本次
Inventory 数量、操作者、UTC 时间和发布 Commit，人工确认 Snapshot URI、
Snapshot Asset 与 Task `*_asset_id` 三类引用都在当前 Schema 中。

跨过保留期后才运行显式清理，并重复输入完全相同的 Project ID：

```powershell
.\.venv\Scripts\python.exe -m knowledge_agent.m7_object_orphans cleanup `
  --organization-id '<organization-id>' `
  --user-id '<active-org-admin-user-id>' `
  --project-id '<project-id>' `
  --confirm-project-id '<project-id>'
```

退出码 `0` 表示本次 Provider Delete 全部成功；`2` 表示至少一个已退休引用的物理对象
删除失败。失败对象不会立即重试，而会作为 Unregistered Orphan 重新经历完整观察窗口。
不得用供应商控制台批量删除前缀，也不得通过降低保留期绕过对账。

## 5. Secret 与加密 Key 轮换

### Actor Session Secret

当前 Codec 只接受一个签名 Secret。轮换会让旧 Session 全部失效：

1. 公告重新登录窗口；
2. 注入新的 `ARTICLE_AGENT_SERVER_SESSION_SECRET`；
3. 滚动重启全部实例；
4. 确认旧 Token 被拒绝、新 Token 可解析；
5. 从 Secret Manager 撤销旧版本。

在实现双 Key 验签前，不做无感轮换承诺。

### Actor Session Version 与全会话撤销

`20260731_0013` 增加 `workspace_users.session_version`，新 Cookie 格式 v2 固定携带签发
版本。每次 Server 请求先验签，再读取 Active Organization/User 与当前版本；不匹配统一
返回 401，不进入 Project 权限查询。

首次发布必须按以下顺序：

1. 在流量关闭时先执行 `alembic upgrade head`；
2. 停止并排空全部旧应用实例；
3. 一次性启动新版本并确认数据库版本校验可用；
4. 再开放流量，并公告所有旧 v1 Cookie 需要重新登录。

旧实例不认识 v2 Cookie，新实例也有意拒绝 v1 Cookie；不得在新旧实例同时接收用户流量时
开始签发 v2。全会话撤销命令为
`POST /api/organizations/{organization_id}/users/{user_id}/sessions/revoke`，Body 必须是
空 JSON 对象。命令不接受版本、角色或目标 Organization 字段，成功响应也不返回内部版本。
Organization Admin Console 已接入这条 Organization-scoped 命令，但生产 IdP 的登录
关联、邀请邮件投递与供应商侧会话仍是独立边界，因此不能描述成完整身份生命周期能力。

撤销动作必须由同 Organization 的 Active Org Admin 发起，并验证版本递增与
`workspace_user.sessions.revoked` Audit 同事务。跨组织目标、非 Admin、目标不存在或
Audit 故障都必须失败；Audit 失败后旧版本必须仍有效，错误输出不得包含数据库或供应商
正文。

### Workspace Invitation 与 OIDC State

`20260731_0014` 增加只保存 Token SHA-256 的 Workspace Invitation。应用签发原 Token
后不能再次读取，生产邮件或其他受信投递系统必须自行安全传递；不得把 Token 放在查询
参数、服务日志或 IdP Redirect URI 中。推荐链接使用 `/accept-invite#token=...`，前端会
先清除 Fragment 再准备登录。

本版本同时把 OIDC State HMAC 域升级为 v2，并可选绑定 Invitation Token Hash。发布时
正在进行的旧 OIDC 登录会失败并要求重试，这是有意的 fail-closed 行为。Callback 成功
或失败都必须清除 State 与 Invitation HttpOnly Cookie；邀请过期、撤销或已兑换后不得
恢复。验收至少覆盖：Cookie 中途替换在 Token Endpoint 前失败、同 Token 重放失败，以及
Audit 故障同时回滚 External Identity 与 Invitation Accepted。

### Project Prompt Snapshot

`20260731_0015` 把 Server Prompt 拆成 `project_prompt_heads`、
`project_prompt_versions` 和 `project_prompt_defaults`。发布前必须验证：

- Version 表的 UPDATE/DELETE 被 Append-only Trigger 拒绝；
- Head 的 Current Version 与 Default 的 Prompt ID + Version 都由复合 FK 固定在同一
  Organization/Project；
- 创建 V2 后，已绑定 V1 的 Project Default 仍解析 V1；只有显式切换才采用 V2；
- Viewer 可解析已授权项目 Prompt，但创建/更新/归档/默认切换要求 `article.edit`；
- 写操作在事务内重新锁定权限，Audit 故障必须同时回滚 Head/Version/Default；
- Audit 不含 Prompt 名称、正文或 Hash；
- `20260731_0016` 同步扩展三张表的 Kind CHECK 以支持 `humanize`；若数据库仍有
  Humanize 历史，降级会在事务内拒绝收窄 CHECK，不允许删除不可变 Version 规避门禁；
- Project-scoped Prompt HTTP 目录/创建/更新/Active/Default 必须通过精确路由白名单；
  Viewer 仅可读，Editor 才可写，Local Mode 返回 404；
- 旧 SQLite Prompt 只能在冻结旧写入口后，由操作员显式调用
  `migrate_project_prompts()`，同时指定 Source Customer、目标 Project 和已认证
  Editor；应用启动不得自动导入或双写；
- 先运行 `dry_run=True` 记录 Prompt/Active/Default 数量和内容摘要，再执行正式导入；
  导入后以相同输入重跑必须得到 `already_matched=True`；
- 目标已有任意不同 Prompt/Default 时必须停止并调查，不允许清空、覆盖或合并猜测；
  旧 SQLite 只保存当前版本，因此导入保留当前 Version 号但不补造更早正文；
- Outline/Article/Humanize/SEO Review Worker 已接线后，旧 Local Prompt API 仍继续
  fail closed；Worker 只能读取入队时固定的 PostgreSQL Prompt Version。Humanize
  Prompt 必须恰好包含一个 `{{ARTICLE}}`，且必须配置显式 Project Default。

Prompt HTTP 冒烟：

```text
GET  /api/projects/{project}/prompt-snapshots
POST /api/projects/{project}/prompt-snapshots
PUT  /api/projects/{project}/prompt-snapshots/{prompt_id}
PUT  /api/projects/{project}/prompt-snapshots/{prompt_id}/active
PUT  /api/projects/{project}/prompt-defaults/{outline|article|review|humanize}
```

### Server Task 写作要求与 Effective Prompt Preview

候选发布必须对同一 Project/Task 验证：

- Editor 使用 `PUT /api/projects/{project}/tasks/{task}/writing-settings` 提交 Revision 与
  完整十字段；Viewer 返回 403，未知/缺失字段返回 422；
- 保存事务持有稳定 Project Prompt advisory lock，当前无 Default 行时的并发插入/切换也
  不能穿过验证窗口；Task CAS 与 `article.writing_settings.updated` Audit 原子提交；
- Audit 仅含字段变更布尔值、Use/Include 状态和 Prompt Source/Version，不含备注、自定义/
  Effective Prompt、Prompt ID/Hash、URL、Chunk、Provider/数据库错误或 Secret；
- Viewer 可使用 `POST .../writing-settings/preview` 预览未保存十字段；响应含安全 Prompt
  Identity、完整 Effective Prompt、Context 数量、目标词数和固定 Warning，Header 必须为
  `Cache-Control: no-store`；Preview 不写 Task/Job/Audit，也不调用模型；
- 旧 `/api/tasks/{task}/writing-settings` 与 `/api/tasks/{task}/prompt-preview` 在 Server 返回
  503；两条新 Project 路径在 Local 返回 404；
- 前端 Dirty、Prompt 不可用或 Revision Conflict 时必须阻止 Outline/Article 生成；409
  保留草稿并要求手动重载，保存不得覆盖正文/大纲/Review 等其他未保存草稿或跳转步骤；
- 动态切换 Project/Task 后，旧 Directory/Task/Save/Preview 响应不得写入新 Scope。

Prompt 数据切换顺序：

1. 停止目标 Customer 的旧 Prompt 写入口，记录 SQLite 文件只读备份；
2. 用 `ProjectPromptRepository` 打开明确的旧数据路径，构造目标 Project 的 Server
   Actor，再调用 `migrate_project_prompts(..., dry_run=True)`；
3. 人工复核报告中的 Organization、Project、三类数量和 `content_digest`，报告或日志
   不得输出 Prompt 名称、正文或 API Key；
4. 以完全相同的 Source/Target 调用 `dry_run=False`，保存 `imported=True` 的报告；
5. 再次执行并确认 `already_matched=True`，然后通过 Project-scoped GET 解析 Default；
6. 若出现 `ProjectPromptMigrationConflict`，保持两边原状并调查 Scope 或冻结窗口，
   不得用删除目标数据作为自动恢复动作。

请求不得携带 Organization、Actor、Role、Status、Content Hash 或服务端 Version 字段；
追加版本和 Active 切换都必须提交 `expected_version`，旧值返回 409。归档必须清除
指向该 Prompt 的 Default，但不得删除历史 Version。

### ProjectMembership 授权与撤销

成员管理命令为：

```text
GET    /api/projects/{project}/members?limit=50&after_user_id=...
GET    /api/projects/{project}/members/candidates?limit=50&after_user_id=...
PUT    /api/projects/{project}/members/{user_id}
DELETE /api/projects/{project}/members/{user_id}
```

GET 只返回显式成员的 `user_id/display_name/status/role`，不会混入继承访问，也不会
返回全组织候选用户；结果按 `user_id` 排序，`limit` 只允许 1–100。PUT Body 只能是
`{"role":"editor"}`、`{"role":"reviewer"}` 或
`{"role":"viewer"}`；DELETE 不接收由客户端提供的 Organization、Actor、Role 或
Permission。冒烟至少验证：

- 无 Cookie 返回 401，Editor/Reviewer/Viewer 返回统一 403；
- 同 Organization 的 Org Admin，以及该 Project Owning Team 的 Team Lead 可以授权和
  撤销；
- Roster 只出现本 Project 的显式成员，分页无重复/遗漏，Disabled User 的已有成员行仍
  可见以便清理，另一 Project/Organization 的成员不出现；
- Candidate 页只出现 Active 同组织普通成员；已有显式成员、Org Admin、Active Owning
  Team Lead、Disabled User 和另一 Organization 用户均不出现，授权成功后目标立即移出
  候选；Team 归档后原 Lead 不再有继承访问并重新成为候选；
- 另一 Organization 的 Project 返回 403；PUT 指向另一 Organization 或 Disabled User
  时返回不泄露细节的 404；
- `org_admin`、`team_lead`、未知 Role 和额外 Organization 字段返回 422；
- PUT 成功产生 `project.membership.granted`，实际删除成功产生
  `project.membership.revoked`；重复 DELETE 返回 `revoked=false` 且不伪造 Audit；
- 人工阻断 Audit Writer 时返回固定 503，Membership 保持原值，响应和日志不含底层异常；
- 在读写事务尚未结束时并发撤销 Actor 的 Org Admin、Team Lead 或显式
  ProjectMembership 事实会等待该事务，证明没有 check-then-revoke/read 窗口。

Project Settings 已完成共享 Metadata 与显式项目成员管理，Organization Admin Console 已完成
Workspace User、Team/TeamMembership、全会话撤销、邀请与外部身份关联；邀请邮件投递和
生产 IdP 管理仍未实现，不能把当前页面描述为完整身份平台。

前端冒烟从 `/projects/{project}/settings` 开始，至少验证：

- Local Mode 仍显示原品牌/上下文/域名设置；Server Mode 不请求旧 Task/Settings API；
- `org_admin/team_lead` 在侧栏看到“项目设置”，Editor/Reviewer/Viewer 不显示；直接
  深链接仍由后端返回 403，不能把导航隐藏当作授权；
- Metadata GET 返回不可变 Project ID、显示名、域名和 Revision；PUT 只接受 Revision、
  `customer_name`、`official_domain`，额外 Project ID/Context/Prompt 字段返回 422；
- 同值重试不增加 Revision 或 Audit；旧 Revision 返回 409；Audit 故障返回脱敏 503
  并回滚 Metadata；Editor 和跨项目 Actor 均不得更新；
- 官网域名只接受规范化 Hostname，不接受 Scheme、Path、Credential 或 Port；Metadata
  Audit 不含显示名、域名、URL 或 Secret；
- 更新后新 Task Intake 捕获新身份，已有 Task 不被回写；事实进入 Published Knowledge，
  写作规则进入 Prompt Snapshot；
- Roster/Candidate 首屏并行加载，更多按钮按游标追加且无重复；375px 下卡片纵向排列；
- Disabled 成员不能保存角色但可以撤销；添加、改角色成功后两份列表一起刷新；
- 保存与撤销期间禁用并发按钮并显示 Loading；失败在对应 Card 就近显示且可刷新恢复；
- 撤销对话框可用键盘操作、有明确目标与取消按钮；关键按钮/选择器触控高度至少 44px。

### OIDC Provider 与 Client Secret

以下配置必须一起注入，缺一项即关闭 Server 登录：

```text
ARTICLE_AGENT_OIDC_ISSUER
ARTICLE_AGENT_OIDC_CLIENT_ID
ARTICLE_AGENT_OIDC_CLIENT_SECRET
ARTICLE_AGENT_OIDC_REDIRECT_URI
ARTICLE_AGENT_OIDC_POST_LOGIN_PATH
```

Provider 注册的 Redirect URI 必须与配置逐字一致，并使用生产 HTTPS 域名。前端与
`/api/auth/*` 必须经同一站点反向代理提供，`POST_LOGIN_PATH` 只能是 `/` 开头的站内
路径。不要把 Client Secret、Authorization Code、ID Token、State Cookie 或签名下载
URL 写入普通日志和发布证据。

Client Secret 轮换：

1. 在 Provider 创建新 Secret，保留旧 Secret；
2. 用新 Secret 在隔离流量执行 Start -> Callback -> Actor Session 冒烟；
3. 运行 Deployment Preflight，确认 `oidc_config` 与 `identity_provider` 通过；
4. 滚动更新所有实例并观察登录错误率；
5. 撤销旧 Secret，再次执行登录和 Preflight。

Provider 的 RS256 Signing Key 由 JWKS 管理；应用遇到未知 `kid` 会刷新一次缓存。轮换
演练必须验证新 Key 可登录、旧已签发 Actor Session 按本地 Session 策略继续或到期，
不能把关闭签名校验当作应急回滚。

### S3 Access Key

1. 创建权限相同的新 Key，不立即删除旧 Key；
2. 用新 Key 执行 `HeadBucket`、测试前缀 Put/Get/Delete；
3. 更新 Secret Manager 并滚动重启；
4. 检查错误率和对象访问审计；
5. 撤销旧 Key，再运行一次只读 Preflight。

KMS Key 轮换遵循供应商策略；先验证旧对象仍可解密。不得把删除旧 Key 当作普通
应用回滚动作。

### Knowledge Research Runtime

Server Research 必须同时具备以下条件；任一缺失都不得把工作区描述成可执行：

```text
features.knowledge_agent_enabled=true
ARTICLE_AGENT_DATABASE_URL
EMBEDDING_BASE_URL
EMBEDDING_API_KEY
EMBEDDING_MODEL
EMBEDDING_DIMENSIONS=1536
TAVILY_API_KEY
ARTICLE_AGENT_OBJECT_STORE_BUCKET
ARTICLE_AGENT_OBJECT_STORE_REGION
ARTICLE_AGENT_OBJECT_STORE_ACCESS_KEY
ARTICLE_AGENT_OBJECT_STORE_SECRET_KEY
```

如使用自建 S3，还需按供应商配置 Endpoint、Path Style 和 SSE/KMS。密钥只由 Secret
Manager 注入，不写入 Config、Audit、Job 公共 DTO 或验证记录。

LangGraph Checkpointer Schema 是显式维护步骤，应用启动不会运行 `setup()`：

```powershell
# Server maintenance shell；先由安全环境注入数据库和 Embedding 配置
cd backend
.\.venv\Scripts\python.exe -m knowledge_agent.checkpoint_setup
```

重复执行必须安全。Schema 未准备时 Research Job 应失败并输出脱敏终态，不能临时回退
内存 Checkpointer 或 SQLite Queue。

### Snapshot Review Receipt Cutover

`20260806_0019` 会新增 Current/Pending 双指针约束、不可变 Receipt 表并执行保守 Backfill。
正式升级前必须暂停 Server Knowledge Review/Publish 写入口和相关 Worker Claim，避免旧进程
在 Backfill 后继续只写 `knowledge_sources.metadata.review`。

升级后、开放流量前验证：

- 已 Published Source 保持原 `current_snapshot_id`，`pending_snapshot_id` 不被臆造；
- 每个 Legacy Published Receipt 只批准该 Source 的精确 Current Snapshot；
- 无 Current 且恰好一个 Snapshot 的 Source 被设置为 Pending；只有合法旧 Review 才有
  对应 Receipt；
- 无 Current 且多个 Snapshot 的 Source 不产生推断 Receipt，旧 Inbox Approval 投影成为
  `needs_review`；
- Receipt UPDATE/DELETE Trigger 生效；
- Server Source-only Review/Publish 路由 fail closed，精确 Snapshot 路由可用；
- Server Library 的 Source-level `raw_evidence_url` 仍为 `null`；后续 Evidence Preview
  切片只通过 Current/Pending 精确 Snapshot 路径访问对象，不能按 Latest 猜测。

一旦新 Receipt 或 Pending 产生业务数据，不得把数据库降级到 0018，也不得重新开放旧
Source-scoped Server Writer。应用回滚时应关闭 Server Knowledge 写流量，保留 0019 Schema
与数据，直到 Receipt-aware 版本恢复。

## 6. 发布和回滚顺序

发布顺序：

1. 备份与恢复演练；由独立 Reviewer 复核外部不可变证据包并签发绑定候选 Commit 的
   Recovery Evidence Envelope；
2. 关闭外部流量，暂停 Server Knowledge Review/Publish 和 Worker Claim；
3. `alembic upgrade head`，确认 Head 为 `20260806_0019` 并完成上述 Backfill 验证；
4. 停止 SQLite 写入口和旧 Worker，完成其余一次性迁移；
5. 对每个项目运行 `m7_cutover_report` 并保存 matched JSON；
6. 只读 Preflight；
7. API/Worker 部署但保持流量关闭；
8. 身份、项目 Scope、对象下载、Retriever、Task/Job、写作要求/Prompt Preview 与 Snapshot
   Review/Publish 冒烟；
9. 小流量开放；
10. 观察期结束后才关闭旧服务器写路径。

OIDC 身份冒烟必须从登录页触发，并至少验证：

- `/api/auth/status` 在四项配置完整时返回 `mode=server` 与
  `login_available=true`，但不返回 Organization/User/Role；
- Start 重定向包含 Code Flow、`openid`、PKCE S256、State 和 Nonce；
- Callback 验证精确 Issuer、单一 Client Audience、RS256 Signature、exp/iat、
  Nonce 和本地 External Identity Mapping；
- 错误 Audience/Issuer/Nonce、过期 Token、HS256、未知 Subject、篡改/过期 State、
  重放 Code/State 和外部 Redirect 均 fail closed；
- 在 Provider 页面取消/拒绝授权时，Callback 返回统一错误、删除 State Cookie，且不
  回显 `error_description`；
- Provider Signing Key 轮换后未知 `kid` 触发一次 JWKS 刷新并成功验证；
- 登录成功后的 Cookie 只包含本地 Organization/User Actor，不包含外部 Role/Group；
- 本地模式仍显示密码入口；Server Mode 状态请求失败时前端不降级显示密码表单；
- Preflight 的 `oidc_config` 与实时 `identity_provider` 探测通过，公开输出不含 Client
  Secret、Token、Provider 正文或 URL。

Product/Image Catalog 冒烟必须先通过
`GET /api/projects/{project}/catalog`。确认 Viewer 只能看到当前 Project 中由当前
Published Snapshot 支撑的 confirmed Product 与图片摘要；Inbox/Rejected/旧 Snapshot、
跨 Project 和无 Snapshot Evidence 的 Asset 均不得出现。响应字段不得包含 Canonical/
Source URL、Artifact URI、Bucket、Object Key、Hash 或 Metadata。

Server Knowledge Inbox 冒烟必须通过 `/projects/{project}/knowledge`：

- Auth Status 为 Local 时仍挂原 Knowledge/Research/Evidence 组件；Server 时挂
  `ServerKnowledgeWorkspace`，由“来源 Inbox”和“资料研究”两个 Tab 组成；
- Viewer/Reviewer 只能读取；Editor 可保存 Source Kind、Trust Tier、Decision 和
  Reason，发布与产品确认仍由后端要求 `knowledge.publish`；
- Editor 上传一份不含真实客户正文的 DOCX/PDF/XLSX 测试资料，确认只进入 Inbox，
  `created=true`、`current_snapshot_id` 仍为空且 `pending_snapshot_id` 指向新 Snapshot；
  相同文件重试必须返回
  `created=false`，Source/Snapshot/Audit 数量不增加；
- 已 Published Source 抓取新内容时必须创建唯一 Pending Snapshot，原 Current、Published
  状态、Retriever 与 Catalog 不变；已有另一 Pending 时第二份不同新字节返回 409；
- 预置同 Hash 的 `file://`、错误 Bucket、错误 Organization/Project S3 Asset 后上传
  必须返回 409，Snapshot Link 不得复用 Server 无法授权读取的历史位置；
- Viewer 上传必须在任何 ObjectStore Put 前返回 403；在第一个对象写入后撤销 Editor
  Membership，最终数据库不得出现 Source/Snapshot/Chunk/Link 或成功 Audit；
- 注入 Audit 故障时 Source/Snapshot/Chunk/Asset Link 必须整笔回滚；已写对象作为
  orphan 候选由延迟对账处理，不在请求失败路径立即删除；
- Upload Audit 只含 Snapshot ID、Parser 和 Chunk/Asset 数量；公开响应与 Audit 不得
  出现文件名、正文、Hash、Bucket、Object Key、Artifact URI 或 Provider Secret；
- Review 与 Publish 是两个精确 Snapshot 请求：
  `PUT .../snapshots/{snapshot}/review` 与 `POST .../snapshots/{snapshot}/publish`；Server
  Source-only 路由必须返回 Conflict；Embedding 失败时未发布 Source 不得显示为 Published，
  已有旧 Published Snapshot 时继续服务；
- 同 Receipt ID、同业务 Payload 的 Review 重试返回同一 Version 且不重复 Audit；同 ID
  不同 Payload 返回 Conflict；新 ID 追加 Version；Receipt UPDATE/DELETE 被 Trigger 拒绝；
- 在 Review/Publish/Confirm 的 Router 授权后撤销 Actor Membership，命令事务必须再次
  返回 403；Publish 在 Embedding 期间撤权时可以保留 Candidate Vector，但不得切换
  Current Snapshot 或追加成功 Audit；
- 在 Embedding 期间追加新 Review、替换 Pending 或切换 Current 时，Activate 必须因 Receipt
  Version/Pending/Expected Current 漂移返回 409，不得把落后的 Candidate 激活为 Current；
- 注入 Audit Writer 故障后，Review 分类、Current Snapshot 激活和 Product Confirm
  必须分别回滚；公开 503 不得包含底层错误、Reason、Canonical URL 或对象信息；
- 重试已经激活的同一 Snapshot 或已经确认的 Product 时保持成功读模型，但不得重复追加
  `knowledge.snapshot.published` / `knowledge.product.confirmed`；
- 检查三类 Audit：Review 只含 Source ID、Decision、Source Kind、Trust Tier、Review
  Version 和 Receipt ID；Publish 只含不可变 Snapshot ID、Receipt Version/ID、Chunk Count、
  Embedding Model，Confirm 只含确认状态；正文、Reason、URL、
  原始 Content Hash、Artifact URI 和 Secret 均不得出现；
- Server DOM 与网络只允许已迁移的 Project-scoped Upload、Task Plan 和 Research
  Run/Resume；不得出现通用 Plan POST、WordPress Sync、Raw Artifact 或 Local
  `/api/config` 请求；
- Server Inbox 可以展示 Current/Pending ID、Pending 计数和 Receipt 投影；Source-level
  `raw_evidence_url` 必须为 `null`，Evidence 操作必须携带卡片对应的精确 Snapshot ID；
- Viewer 可以读取 Current/Pending Evidence Manifest 和有界 Normalized Text Preview，
  但不能 Review/Publish；撤权后 Preview/Download 必须拒绝；
- Raw Download 只能返回 60 秒 Attachment 签名，公共响应和日志不得包含 Bucket、Object
  Key、Artifact URI、Hash、Provider Body 或 Secret；
- Historical/Rejected Snapshot、错误 Source/Project、错误 Bucket/Prefix 和旧 Server
  `.../raw` 路径必须 fail closed；HTML/SVG 也不得以内联 Content-Type 执行；
- Product Confirm 不代表文章可选择；必须再验证 Project Catalog 只投影当前 Published
  Snapshot Evidence。

Server Knowledge Research 冒烟还必须覆盖：

- 从当前 Project 的已确认 PostgreSQL Task 创建 Plan；未确认大纲、旧 Revision、跨
  Project Task 和通用 `POST .../retrieval-plans` 均 fail closed；
- Start 只提交 Plan ID、查询预算和 Request ID；创建的 Job 使用真实 Task ID，同一
  Request ID 重试返回同一 Job，Run/Event/Batch/Job/Audit 任一失败时整笔回滚；
- Viewer/Reviewer 不能 Start/Resume；撤销 Publisher 后，Claim、Handler 和下一个候选
  抓取检查点必须拒绝，不能发布来源；
- Run 列表、详情、SSE、Gap Attempt、Job 和 Audit 不得公开私有 Request、Requester、
  Query、发现 URL、对象位置、Provider 原文或 Secret；
- 候选审批只提交 Candidate ID；伪造、过期或其他 Attempt 的 ID 返回冲突。Resume 创建
  新 Job，零选择有明确语义，不调用通用 Retry；
- SSE 中断后页面使用有界轮询恢复；Thread 深链刷新后仍能读取同一 Run，不回退 Local；
- 高置信度获批官网页只写当前 Organization/Project 的 S3 前缀并走审阅/发布命令；
  低置信度页面停在 Inbox，Embedding/Publish 失败时旧 Current Snapshot 继续服务；
- Research Job 在 Batch/Header 可读并链接回 Knowledge Research，但通用 Cancel/Retry
  前后端都返回不可用；Graph 终态失败不得被基础设施自动重放；
- Evidence Pack 只返回安全摘要和引用，不提供 Raw Artifact。

对象下载冒烟必须通过
`GET /api/projects/{project}/assets/{asset_id}/download`，验证 URL 过期时间不超过
3600 秒；同时使用另一 Project 的 Actor 和一条错误 Organization Key 前缀的测试资产，
分别确认 403 与 404。不要把签名 URL 写入长期发布证据或普通日志。

文章图片准备冒烟必须通过
`POST /api/projects/{project}/tasks/{task_id}/prepare-images`。至少验证：

- Viewer 返回 403；Editor 的请求只含当前 Revision、一个项目内 Hero Asset ID，以及
  可选的 `Product ID -> Heading` 人工锚点，不接受客户端产品图片 ID；
- 产品图只读取 Task 当前 `Product.selected_asset_id`；源对象读取前重新要求
  `article.edit`，且 Bucket、Organization/Project Key、字节数和 SHA-256 任一不一致
  都 fail closed；
- 源图只在内存中验证和派生；EXIF 方向、动画首帧、像素上限、确定性 WebP、内容哈希和
  视觉近重复检查通过，含 Hero 最多三张；
- 自动锚点无法解析时，响应返回当前文章的非 FAQ H2/H3 候选，并确认没有派生对象写入；
  人工锚点只能引用当前 Task 已选择的 Product ID；
- 成功后的 Task 只含源/派生 Asset ID、派生哈希、尺寸、Marker 和锚点诊断，
  `source_path/prepared_path` 为空，且不含对象 URI 或签名 URL；
- 旧 Revision 在读取对象前返回 409；并发 CAS 若发生在对象写入后，内容寻址的未引用
  对象进入延迟 orphan 对账，不在失败请求中立即删除；
- 派生 Asset 仍只能通过授权下载路由获取；图片准备本身不等于完整交付链路已切换。

文章 DOCX 冒烟必须通过
`POST /api/projects/{project}/tasks/{task_id}/export-docx`，随后使用
`GET /api/projects/{project}/tasks/{task_id}/docx/download`。至少验证：

- Viewer 返回 403；Editor 具备 `article.deliver` 时请求只含当前 Revision，不接受
  文件字节、图片 Asset ID、对象 URI 或输出路径；
- 导出只读取 Task 的 `prepared_asset_id`，对象读取前再次授权，并复核 Key Scope、
  Byte Size、SHA-256、`image/webp` 类型、数据库/Task/实际尺寸和像素上限；
- 现有 Word 排版逻辑接收内存 WebP 并返回 DOCX 字节；整个过程不创建 Task 目录、
  临时图片、Markdown 审计文件或本地 DOCX；
- 成功 Task 只保存 `docx_asset_id/docx_content_hash/docx_filename`，
  `docx_path` 为空；旧 Revision 在对象读取前返回 409；
- 通用 `GET .../assets/{asset_id}/download` 对 `article_docx` 返回 404，专用下载路由
  重新要求 `article.deliver` 并签发不超过一小时的 URL；
- 并发 CAS 后的未引用 DOCX 进入内容寻址 orphan 对账，不在失败请求中立即删除；
- Server Article Console 已接通现有 Task 的标题到交付主链，Delivery ZIP 与下载也已接线；
  “内容准备”包含十字段写作要求、显式保存和折叠 Effective Prompt Preview；
  SEO Change 裁决/Preview/Apply/Complete、历史大纲恢复和章节重写已有专用 Server
  面板；Product Rediscovery 创建/状态、Rewrite From Scratch、Project Batch/Job
  列表/详情/全局抽屉、Rediscovery Inbox 审阅、可视化 Hero Asset Picker，以及
  Project-scoped Task 单条创建/规范化行导入也已迁移。原始 XLSX Topic Asset 上传、
  生产 IdP/ObjectStore 与恢复演练仍未完成，发布证据不得写成“生产已上线”。

Server Task Intake 冒烟必须覆盖：

- Viewer/Reviewer 的 `POST /api/projects/{project}/tasks` 与 `/task-imports` 返回 403；
  Editor/Lead/Admin 才能创建，后端在事务内再次锁定权限；
- 单条 Body 只含 Intake ID、Topic 和可选竞对字段；批量 Body 只含 Intake ID、来源
  标签与 1–200 条规范化行。提交 `task_id/topic_index/customer/status/revision/task_dir`
  必须返回 422；
- 同一 Intake ID 与相同规范化输入重试返回相同 Task 且 `created=false`；同一 ID
  改变输入返回 409。并发相同请求只能留下一个 Receipt、一批 Task 和一条 Audit；
- 新 Task 的 `week_folder=server`、`task_dir=""`，Customer/Brand 来自当前 Active
  Project；跨 Project 路径不得创建或读取 Task；
- `article_tasks`、`task_intakes` 与 Audit 同事务。注入 Audit 故障应返回脱敏 503 且
  三者都不增加；Audit Details 不得包含 Topic、关键词、URL 或 Source Digest；
- Server 页面只解析 Tab 分隔行，不调用 `/api/topic-files/upload`、`/api/sync-tasks`
  或本地 Topic Library。原始 XLSX 留档仍是未迁移能力。

TDK DOCX 冒烟必须通过
`POST /api/projects/{project}/tasks/{task_id}/generate-tdk`，随后使用
`GET /api/projects/{project}/tasks/{task_id}/tdk/download`。至少验证：

- Viewer 返回 403；具备 `article.deliver` 的 Editor 请求只含当前 Revision；
- Task 必须已有 Server `docx_asset_id`，不能以本地 `docx_path` 冒充已导出文章；
- TDK Title 精确绑定当前文章 H1，Description 不超过 150 字符，Keyword 恰好 6 个、
  非空、唯一且不含逗号或换行；
- LLM/网关异常返回统一 503，响应和日志证据不包含 Provider 正文、Token、Key 或请求
  文章；校验失败返回 422 且不写对象；
- `D.docx` 在内存生成，Task 只保存 `tdk`、`tdk_asset_id`、`tdk_content_hash` 和
  `tdk_filename`，`tdk_path` 为空；
- 通用 Asset 下载对 `tdk_docx` 返回 404，文章 DOCX 专用下载也不能签发 TDK；只有
  Task-scoped TDK 下载重新授权后签发不超过一小时的 URL；
- 旧 Revision 在 LLM/对象访问前返回 409；并发 CAS 后的未引用 TDK DOCX 进入内容
  寻址 orphan 对账，不在失败请求中立即删除。

初稿 AI-rate Review 冒烟必须依次使用：

- `POST /api/projects/{project}/tasks/{task_id}/checks/initial-ai/screenshot?revision=...`；
- `PUT /api/projects/{project}/tasks/{task_id}/checks/initial-ai`；
- `GET /api/projects/{project}/tasks/{task_id}/checks/initial-ai/screenshot/download`。

并验证：

- 三条接口都要求 `article.review`，Viewer/跨 Project/撤权请求 fail closed；
- 初检截图在内存规范化为无元数据 PNG，以独立
  `initial_ai_rate_screenshot` 私有类型保存；通用下载和终检专用下载都不能签发；
- confirmed=true 前必须已有初检截图，确认绑定当前 Initial Article Hash，只推进到
  `initial_ai_checked`，低分也不会自动跳过 Humanize 或伪造终检；
- 上传和确认分别产生 `article.initial_ai_screenshot.uploaded` 与
  `article.initial_ai_check.updated`，Task CAS/Audit 同事务，Audit 不含 Report 或 Score 值；
- Local Mode 三条 Project 路由均为 404，Task 不保存本地截图路径。

最终 AI-rate Review 冒烟必须依次使用：

- `POST /api/projects/{project}/tasks/{task_id}/checks/final-ai/screenshot?revision=...`；
- `PUT /api/projects/{project}/tasks/{task_id}/checks/final-ai`；
- `GET /api/projects/{project}/tasks/{task_id}/checks/final-ai/screenshot/download`。

至少验证：

- Viewer 返回 403；Reviewer 可执行 Review，但不能调用 DOCX/TDK 的
  `article.deliver` 路由；
- 截图请求只含当前 Revision 和 multipart File，不接受 Asset ID、对象 URI 或本地路径；
- 读取文件前先检查 Revision 和 `ACTION_CONFIRM_FINAL_AI`；旧 Revision 不读取/上传对象；
- 输入执行 25 MB、4000 万像素、实际图片解码和 EXIF 方向门禁，输出为无元数据 PNG；
- AICheck 只保存 `screenshot_asset_id`、哈希、文件名和尺寸，`screenshot_path` 为空；
- confirmed=true 时没有 Screenshot Asset 必须返回 409；成功确认把 score/report 绑定
  当前 Humanized Article 哈希并推进 `final_ai_checked`；
- 通用 Asset 下载对 `final_ai_rate_screenshot` 返回 404；专用下载同时复核 Task 与
  Asset 的 ID、哈希、尺寸和类型，再按 `article.review` 签发短期 URL；
- Audit 只含截图尺寸、confirmed 和是否记录 score，不含 score 值、Report、图片字节、
  文章正文、对象 URI 或签名 URL；
- 并发 CAS 后的未引用 PNG 进入延迟 orphan 对账，不在失败请求中直接删除。

Delivery ZIP 冒烟必须通过
`POST /api/projects/{project}/tasks/{task_id}/package-delivery`，随后使用
`GET /api/projects/{project}/tasks/{task_id}/delivery-package/download`。至少验证：

- Viewer 和 Reviewer 返回 403；具备 `article.deliver` 的 Editor 请求只含当前
  Revision，不接受文件字节、Asset ID、对象 URI、输出路径或文件名；
- 终审必须已确认且其 `article_hash` 精确匹配当前 Humanized Article；不匹配时不读取
  或上传归档；
- 文章 DOCX、TDK DOCX、1–3 张 Prepared WebP 和 Review PNG 均由 Task 身份选取，
  对象读取前再次授权并复核 Key Scope、大小、SHA-256、访问类型和必要尺寸；
- ZIP 纯内存生成，不创建 Task 目录或临时文件；条目扁平且固定包含文章 DOCX、
  `D.docx`、WebP 和 `final-ai-rate.png`，同输入字节生成同一哈希；
- 成功 Task 只保存 `delivery_package_asset_id/delivery_package_content_hash/
  delivery_package_filename`，`delivery_package_path` 为空；
- 通用 Asset 下载对 `delivery_zip` 返回 404；专用下载再次要求 `article.deliver`，
  复核 Task Asset ID/Hash/类型后签发不超过一小时的 URL；
- Audit 只含文件数和图片数，不含文件名、正文、对象 URI、签名 URL 或归档字节；
- 旧 Revision 在对象读取前返回 409；并发 CAS 后的未引用 ZIP 进入延迟 orphan 对账，
  不在失败请求中直接删除。

Server Delivery Console 冒烟必须从 `/` 开始，至少验证：

- `/api/auth/status` 返回 Server Mode 后不再发起 Local Dashboard、Config、SQLite Task、
  Batch Job 或上传请求；
- 首页只渲染 `/api/projects` 返回的 SQL-scoped 项目，Card 使用 `project_id` 进入
  `/projects/{project_id}/deliveries`，不以显示客户名推导 Scope；
- 项目侧栏只显示 Delivery，且不挂载 Local Job Center、Article、Batch 或 Settings；
- Delivery 列表只请求 `/api/projects/{project_id}/tasks`，Server 产物以 Asset ID 判定，
  页面不显示 Path、Bucket、Object Key 或永久 URI；
- Viewer 不显示 Review/Delivery 动作；Reviewer 只可查看终审截图；
  `org_admin/team_lead/editor` 才显示 Word、TDK、打包和 ZIP 下载；
- 打包按钮发送当前 Task Revision，等待期间禁用同 Task 动作并显示 Loading；失败信息
  由 `role=alert` 宣告且可刷新重试，成功后重新读取 Task；
- 下载按钮先调用 Task-scoped 专用接口取得短期 URL，再导航到签名对象；前端不拼接
  S3 地址，也不使用通用 Asset 下载绕过访问分类；
- 375px 宽度下过滤器和表格可操作，表格允许水平滚动，无内容被固定侧栏覆盖；键盘
  Focus Ring、按钮 Disabled 状态、明暗主题语义 Token 保持可辨识；
- Local 模式仍挂载原 ProjectSelector、完整导航与 Path 下载，不因 Server UI 改动而
  改写现有行为。

当前已迁移 Server Task 写操作的事务内 Audit 冒烟必须同时验证：

- “完全重写、保存写作要求、生成标题候选、选择标题候选、保存/确认大纲、恢复历史大纲草稿、生成正文初稿、上传初检截图、确认初检、保存人工 Humanized Article、自动生成 Humanized Article、恢复链接、保存 SEO Review 设置、生成 SEO Review Run、裁决 Review Change、应用 Review、完成 Review、确认产品、替换章节、准备图片、导出 DOCX、生成 TDK、上传最终截图、
  确认最终检查、打包交付 ZIP”分别产生
  `article.task.rewritten`、`article.writing_settings.updated`、
  `article.titles.generated`、`article.title.selected`、
  `article.outline.updated`、`article.outline_version.restored`、
  `article.draft.generated`、
  `article.initial_ai_screenshot.uploaded`、`article.initial_ai_check.updated`、
  `article.humanized.updated`、
  `article.humanized.generated`、
  `article.links.restored`、
  `article.seo_review_settings.updated`、
  `article.seo_review.generated`、
  `article.seo_review.change.updated`、`article.seo_review.applied`、
  `article.seo_review.completed`、
  `article.products.confirmed`、
  `article.section.replaced`、`article.images.prepared`、
  `article.docx.exported`、`article.tdk.generated`、
  `article.final_ai_screenshot.uploaded`、`article.final_ai_check.updated`、
  `article.delivery.packaged`；
- Action 到 `article.edit/article.review/article.deliver` 的映射由服务端常量决定，
  请求不能提交或覆盖 Permission；
- Event 的 Organization/Project/Actor/Task 与请求 Scope 一致，Details 只含
  from/to Revision、Status 和安全计数/Heading 深度，不含文章正文、Replacement Body、
  URL、对象 URI、签名 URL、Token 或 Secret；
- 标题选择只提交 Candidate Index，不提交标题正文；服务端必须从当前 PostgreSQL Task
  的 `title_candidates` 取值，越界、空候选、旧 Revision 和跨项目请求均不写 Task/Audit；
- 标题生成只提交当前 Revision；入队时固定 checked-in `titles` Template Hash、Task
  Revision 与当前 Published Chunk ID，公开 Job 不返回 Template/Chunk 身份或请求正文。
  Worker 执行前重新核对模板、Task Revision 和 Published Chunk Scope，只读取当前项目
  发布态知识，不读取本地 Customer Context；Provider 返回不足、重复、空白或超长候选时
  Job 失败且不补 mock。成功只写 `title_candidates`、清空旧选择和下游，并以
  `article.titles.generated` 与 Task CAS 同事务提交，Audit 只含候选数和 Context Chunk 数；
- 大纲保存只提交有界 Markdown 与 Confirmed 标志；草稿保留当前确认大纲和下游，确认
  才使下游失效；Audit 只记录 Confirmed 与字符数，不记录 Markdown；
- 大纲恢复只提交 Version Index，只允许当前 Task 的 Outline Version 恢复成草稿；
  Article Version、越界索引和客户端历史正文均拒绝，Audit 不记录版本正文；
- 大纲生成只提交当前 Revision；入队时由服务端固定 Prompt ID + Version 与 Published
  Chunk ID，公开 Job 不返回这些私有输入。Worker 执行前复核 Prompt/Chunk Scope，
  成功只写 `outline_draft` 和 `generated` Version，不替换确认大纲、不使下游失效；
- 正文初稿生成只提交当前 Revision；入队时由服务端固定 Article Prompt ID + Version、
  目标字数和 Published Chunk ID，公开 Job 不返回这些私有输入。Worker 执行前复核
  Task/Prompt/Chunk Scope，只接受包含 H1/过渡段、H2/H3 和最终 FAQ 的完整 Markdown；
  失败不补 mock、不读写本地 Artifact。成功追加 Raw/Initial Version、进入
  `draft_ready` 并清空旧下游；Audit 只记录字数、Prompt 身份和 Chunk 数，不含正文；
- 人工 Humanized Article 保存只提交 Revision 与有界 Markdown；服务端必须拒绝标题
  层级、数字事实、FAQ、表格、列表或必须短语漂移，成功追加 `external_manual` Version、
  进入 `humanized_ready` 并清空终检之后的旧产物。Audit 只记录字数，不含正文；
- 自动 Humanize 只提交 Revision；Enqueue 必须解析显式 Project `humanize` Default，
  固定 Prompt ID/Version/Content Hash 与 Source Article Hash，公开 Job 不返回这些身份。
  Worker 两阶段要求 `article.edit`，加载精确不可变 Prompt 后才调用 Provider；Provider
  与提交变换分别执行结构/事实门禁。成功追加 `initial/rehumanized` 来源 Version，
  进入 `humanized_ready` 并清空下游；Server 不读取本地 Prompt 文件、SQLite Prompt、
  System 回退或 Published Context；
- Link Restore 只提交 Revision；Enqueue 固定 checked-in Template Hash、Initial/
  Humanized Article Hash、来源链接数和 Final Check 绑定，公开 Job 不返回这些身份。
  Worker 两阶段重授权并在 Provider 前复核固定身份；模型只返回候选，确定性校验必须
  证明 Markdown Link/URL 多重集合与 Initial Article 完全相同，非链接可见正文与
  Humanized Article 相同。成功只追加 `linked` Version、进入 `links_verified` 并
  记录来源/恢复链接数；模板或正文漂移、撤权、非法 URL、正文变化、Audit/CAS 失败
  不得留下 Task 部分写入；
- SEO Review Settings 只提交 Revision、关键词和 Prompt Selection；服务端必须解析
  当前 Project 的 `review` Prompt，不接受 Prompt 正文/Version/Provider。关键词规范化、
  去重并限制数量/长度；Audit 只记录长尾关键词数量和 Prompt Source/Version，不记录
  关键词或 Prompt 正文。该命令不得创建 Review Run；
- SEO Review 生成只提交 Revision；Enqueue 固定 Initial Article Hash、精确 Review
  Prompt Version、checked-in System Template Hash 与当前 Published Chunk ID，公开
  Job 不返回这些身份。Worker 两阶段要求 `article.review`，Provider 只读取注入的
  Published Context，不读取本地 Customer Context、不补 mock。成功只追加 Open Review
  Run，不修改文章或 Workflow Status；Audit 只记录安全计数、Prompt 身份与
  Publish-ready 布尔值，不记录文章、Report、Change、Prompt 或 Knowledge 正文。
  Change/Preview/Apply/Complete 必须走下述独立人工命令；
- Review Change/Preview/Complete 要求 `article.review`，Apply 要求 `article.edit`。
  Review/Change ID 只来自路径；Body 不能提交 Prompt、正文全文或服务端身份。Preview
  只读，Apply 必须携带当前 Revision 与 Preview Hash，服务端重新构建和验证全文后才
  追加 Initial Version；Change/Apply/Complete 的 CAS 与 Audit 同事务；
- 人工注入 Audit Writer 失败时 Task Revision、正文和派生引用全部保持原值；旧 Revision
  或事务内撤权也不产生 Audit；
- Audit Event 更新/删除仍被 Trigger 拒绝；图片/文章 DOCX/TDK DOCX/Review PNG/
  Delivery ZIP
  已先写对象而 Task/Audit 后失败时，只记录内容寻址 orphan 进入延迟对账，不直接删除。

产品重新发现冒烟必须通过
`POST /api/projects/{project}/tasks/{task_id}/product-rediscovery`，随后只使用响应中的
Job ID 调用
`GET /api/projects/{project}/tasks/{task_id}/product-rediscovery/jobs/{job_id}`。
至少验证：

- Viewer 返回 403；具备 `knowledge.edit` 的 Editor 才能入队；
- 请求只包含当前 Task Revision、官网内 Category URL 和 1–50 的抓取上限；
- Job 行保存可信 `requested_by_user_id`，公开响应不包含 Request、Requester、原始错误
  或对象 URI；
- 撤销 Requester 后，尚未执行的 Job 进入通用 conflict，Worker 不读取/执行私有请求；
- 抓取只使用 Active Project 的 `official_domain` 和 Organization/Project 绑定的私有
  S3 前缀，不创建本地 JSON、SQLite 或 Artifact 目录；
- 成功、失败或取消都不修改 Task Revision 和当前产品；新证据留在 Inbox，审核发布后
  才能由产品替换接口选择；
- 对象存储配置缺失时新 Job 返回 503，但已有 Job 的状态仍可读取；
- 重启恢复只处理 Active `product_rediscovery` Job，不得把旧无 Requester 历史重新执行。

Outline 生成冒烟必须通过
`POST /api/projects/{project}/tasks/{task_id}/outline`，随后只使用响应 Job ID 调用
`GET /api/projects/{project}/tasks/{task_id}/outline/jobs/{job_id}`。至少验证：

- Body 只允许当前 Revision；Prompt ID/正文、Chunk ID、Actor、Role 或 Provider 字段
  均返回 422；
- Viewer 与跨 Project 请求返回 403，可信 Requester 在 Claim 前和 Handler 前都要求
  `article.edit`；
- Project Default 在入队后切换到新 Version，已排队 Job 仍使用原不可变 Version；
- Knowledge Context 只来自同 Project 的当前 Published Snapshot；任一固定 Chunk 被
  取消发布或切换 Snapshot 后，Job 在调用 Provider 前进入 Conflict；
- Provider 未配置、空输出或异常只返回脱敏失败，不得回退 mock、本地 Customer
  Context、SQLite Prompt 或本地 Artifact；
- 成功后 Task Revision 加一，只更新 Draft、`last_outline_prompt_snapshot` 和
  `generated` Version；人工 PUT Confirmed 前正式 Outline 与下游保持原值；
- Task CAS、撤权或 Audit 失败不留下 Draft/Version/Prompt Snapshot 部分写入，日志和
  Audit 不含 Prompt 正文、Knowledge 正文或 Provider 原始响应。

Humanize 生成冒烟必须通过
`POST /api/projects/{project}/tasks/{task_id}/humanize`，随后只使用响应 Job ID 调用
`GET /api/projects/{project}/tasks/{task_id}/humanize/jobs/{job_id}`。至少验证：

- Body 只允许当前 Revision；Prompt ID/正文/Hash、Article Hash、Actor、Role、Provider
  或模型参数均返回 422；
- Viewer 与跨 Project 请求返回 403，可信 Requester 在 Claim 前和 Handler 前都要求
  `article.edit`；
- Project 必须有 Active `humanize` Default Prompt，且内容恰好包含一个
  `{{ARTICLE}}`；无 Default 时 409 且不创建 Job，Provider 未配置时 503；
- 入队固定精确 Prompt ID/Version/Content Hash、Task Revision 和 Source Article Hash；
  Default 切换、Prompt 不可用或 Article/Revision 漂移必须在 Provider 前停止；
- Server Provider 不读取 `humanize_prompt_path`、SQLite Prompt、System 回退或
  Published Context；异常、空输出或非法 Markdown 只返回脱敏失败；
- Provider 返回后，提交变换必须再次独立验证标题层级、数字事实、FAQ、表格、列表和
  必须短语；成功只追加 Humanized Version、进入 `humanized_ready` 并清空下游；
- 执行前撤权、Task CAS 或 Audit 故障不留下部分 Humanized Version；公开 Job/Audit
  不含 Article、Prompt、Hash、Requester 或原始错误；
- 已处于 `humanized_ready/final_ai_checked` 的 Task 重新 Humanize 时只读取身份完整的
  当前 Humanized Article，并在成功后使旧终检、Link/Image/Delivery 失效。

SEO Review 生成冒烟必须通过
`POST /api/projects/{project}/tasks/{task_id}/seo-reviews`，随后只使用响应 Job ID 调用
`GET /api/projects/{project}/tasks/{task_id}/seo-reviews/jobs/{job_id}`。至少验证：

- Body 只允许当前 Revision；Prompt/Chunk/Article Hash、Actor、Role、Provider 或模型
  参数均返回 422；
- Viewer 与跨 Project 请求返回 403，可信 Requester 在 Claim 前和 Handler 前都要求
  `article.review`；
- 入队固定 Task 已选择的精确 Project Review Prompt Version、System Template Hash、
  Initial Article Hash 与当前 Published Chunk ID；任一身份漂移必须在 Provider 或提交
  前停止；
- Context 只包含同 Project 的 Published Current Snapshot；Inbox、旧 Snapshot 和跨
  Project Chunk 不得传给 Provider，Server Provider 不读取本地 Customer Context；
- Provider 未配置、异常、空输出或非法 JSON 只返回脱敏失败，不回退 mock，也不把文章、
  Prompt、Knowledge、Report 或 Proposed Change 写入 Job/Audit；
- 成功只追加一个 Open Review Run 并使 Revision 加一；文章、Workflow Status 和已有
  Review Run 保持原语义，不自动 Change/Preview/Apply/Complete；
- 执行前撤权、Task CAS 或 Audit 故障不留下部分 Review Run；同一 Job ID 不得重复追加。

Project-scoped Job Control 冒烟必须另外验证：

- `GET /api/projects/{project}/batches` 与 Batch Detail 只返回
  `product_rediscovery/titles/outline/article/humanize/restore_links/seo_review/knowledge_research`，并且响应不存在 Request、Requester、URL、Prompt/
  Chunk 身份、原始 Error、Worker Lease、对象 URI 或签名 URL；
- Viewer 可读但 Cancel/Retry 返回 403；控制权限按 Operation 分别映射
  `knowledge.edit/article.edit/article.review`，且撤权后即使 Cookie 与页面仍有效也必须失败；
- Batch/Job ID 从另一 Project 或 Organization 带入当前路径时返回 404，不能读取后
  再由应用层过滤；
- `POST .../cancel` 使用空 Body；Queued/Retry-wait 直接成为 Cancelled，Running 只设置
  Cancel Requested，终态与命令 Audit 都不包含私有输入；
- `POST .../retry` 只允许 Failed/Cancelled/Conflict，空 Body 重放服务端保存的同一
  Request 与 Source Revision；任何客户端覆盖字段返回 422；
- `knowledge_research` 是显式例外：列表可见，但通用 Cancel/Retry 返回 409；继续执行
  必须通过 Research Resume 创建新的私有 Job；
- 人工注入 Audit 故障时状态变化完整回滚；旧 `/api/batches*`、无 Project Job Detail
  和未迁移 Operation 继续由精确白名单拒绝。

当前 Runner 只在整次官网同步前后检查停止/取消，产品明细循环中没有逐项检查点。
`stop()` 的运维语义是：

- 先停止新 Claim，再在有界时间内等待已领取工作；
- Handler 在协作检查点因服务停机退出时，Job 释放为 `queued`，不得写成用户
  `cancelled`；
- 有界等待后 `remaining_jobs > 0` 表示未排空，Lifespan 必须失败，且不得提前释放
  PostgreSQL Engine；
- 终态 Job 与 `background_job.terminal` Audit 必须同事务；Audit 失败时终态回滚，
  Claim 释放后等待恢复重试；
- Audit 只允许 Operation、Status、Attempt 和 Revision 等稳定字段，不得记录 Request、
  Category URL、对象 URI、原始异常或 Provider 响应。

因此发布窗口仍应允许长抓取自然结束，并把 `drained=true` 作为本 Operation 的停机证据。
非协作 Handler 的超时是明确的 no-go，不是强制中断成功。这一条 Operation 的接线仍不能
写成整体 Worker Cutover 完成；通用 Batch/Runner 和正式环境停机演练需单独验收。

产品替换冒烟必须通过
`PUT /api/projects/{project}/tasks/{task_id}/products`，请求只包含当前 Task Revision
和 1–3 个 Product ID。至少验证：

- 候选产品已经重新抓取并审核，当前 Primary Detail Evidence 含
  `selection_projection.schema_version=1`；旧 Evidence 不允许回退读取可变目录 Metadata；
- Viewer 返回 403，Editor 可提交；
- 未确认产品、另一 Project 产品、没有 Published Current Snapshot 主详情证据的产品
  返回同类不可选择错误；
- 成功响应只保存正式目录事实和 `selected_asset_id`，不含对象 URI、源站图片 URL 或
  本地路径；
- 模拟未发布刷新修改 `knowledge_products.metadata` 后，Task 仍只得到已发布快照的
  Evidence Projection；
- 重复提交旧 Revision 返回 409；
- 图片展示继续单独调用授权下载路由，不能把签名 URL 回写 Task。

章节替换冒烟必须通过
`PUT /api/projects/{project}/tasks/{task_id}/article/sections`。至少验证：

- Viewer 返回 403，Editor 只在 `ACTION_UPDATE_ARTICLE` 允许时提交；
- 不存在或重复的 Heading Path、同级/更高级 Heading 注入返回 422；
- fenced code block 中的 `##` 不被识别为章节边界；
- 成功后只有目标 Section Body 变化，相邻章节和目标 Heading 保持不变；
- `article_versions` 原子追加 `before_section_rewrite` 与 `section_rewrite`，下游人化、
  链接、图片和导出状态失效；
- 重复提交旧 Revision 返回 409，且不多追加版本；
- 本接口不调用 LLM；对话式生成只能提供候选 Body，不能直接写完整 Article。

回滚原则：

- 代码回滚优先，数据库迁移只在确认新表没有新业务数据时降级；
- 已写入 PostgreSQL 的服务器 Task/Job 不回灌 SQLite；
- 已写入的内容寻址对象保留，依靠引用对账后延迟清理；
- 身份或 Scope 异常立即关闭服务器入口，不退化为默认 Actor/默认 Project；
- 数据损坏才使用已验证的数据库和对象恢复点，不能用未经演练的备份覆盖生产。

## 7. 发布证据模板

每次候选发布保留：

```text
commit:
operator:
reviewer:
utc_started:
utc_completed:
database_backup_sha256:
database_restore_target:
database_restore_checks:
object_inventory_or_version_watermark:
object_restore_sample_count:
object_restore_hash_matches:
recovery_evidence_artifact:
recovery_evidence_bundle_sha256:
recovery_evidence_signing_key_id:
capability_manifest_artifact:
route_inventory_digest:
operation_inventory_digest:
cutover_report_artifacts:
cutover_reports_all_projects_matched:
worker_drain_report_artifact:
worker_drained:
idp_conformance_smoke:
object_store_policy_evidence:
products_smoke:
preflight_report_artifact:
writing_settings_smoke:
backend_regression:
frontend_build:
rpo:
rto:
rollback_owner:
decision: go | no-go
```

模板中的空值代表门禁未完成，不能写“默认通过”。
模板只记录受控系统中的 Artifact ID、Digest、数量和判断，不复制 Evidence Envelope、数据库
内容、对象 Key/URI、OIDC Token、Bucket Policy 正文、签名、公钥或 Secret。当前尚未进行
真实恢复演练，不能预填 `go`、`worker_drained=true` 或
`cutover_reports_all_projects_matched=true`。
