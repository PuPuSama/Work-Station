# M7 Server 路由与 Worker 迁移矩阵

> 目的：记录 Local SQLite/File 工作流向 Server PostgreSQL/ObjectStore 迁移时的结构边界。
> 本文是重构导航，不是授权准源；运行时仍以
> `server_request_security.py`、Project-scoped Router 和事务服务为准。

## 1. 状态定义

| 状态 | 含义 |
|---|---|
| Server Ready | 路径显式包含 Project 或 Organization，使用服务器准源并有请求级授权 |
| Server Narrow | 只开放已验证的窄操作；同组其他旧路径继续 503 |
| Local Only | 仍读取 SQLite、本地文件或 Local 全局单例；Server Mode 必须 503 |
| External Gate | 代码能力存在，但完成验收需要生产 IdP、对象存储或运维环境 |

任何路由从 Local Only 变为 Server Ready 前，至少同时满足：

1. URL 含可信 Scope，或 Scope 完全来自已验签 Actor；
2. 读路径在 SQL 内过滤 Organization/Project，不先读全量再过滤；
3. 写路径在事务内重新锁定可撤权权限事实；
4. Task 使用 Revision CAS，Job 使用可信 Requester 和 Project Queue；
5. 私有请求、正文、Token、Subject、对象 URI 和底层错误不进入公开响应/Audit；
6. 本地与服务器组件树显式分流，不在 Server Mode 回退 SQLite/File；
7. 有跨项目、跨组织、撤权竞态、Audit 回滚和路由精确白名单测试。

## 2. 已迁移控制面

| 能力 | Server 路径 | 准源 | 权限/身份 | 状态 |
|---|---|---|---|---|
| OIDC 登录 | `/api/auth/oidc/*` | IdP + External Identity + Session Version | 已验证 Issuer/Subject | Server Ready |
| Workspace Invitation | `/api/organizations/{org}/invitations`、`/accept-invite` | PostgreSQL + OIDC State | Active Org Admin / Verified Identity | Server Ready |
| Organization User/Session | `/api/organizations/{org}/users/*` | PostgreSQL | Active Org Admin | Server Ready |
| Team/TeamMembership | `/api/organizations/{org}/teams/*` | PostgreSQL | Active Org Admin | Server Ready |
| External Identity 管理 | `/api/organizations/{org}/external-identities/*` | PostgreSQL | Active Org Admin | Server Ready |
| Project Directory/Membership | `/api/projects`、`/api/projects/{project}/members/*` | PostgreSQL | Project RBAC | Server Ready |
| Knowledge API | `/api/knowledge/projects/{project}/*` | PostgreSQL/pgvector/ObjectStore | Knowledge 权限矩阵 | Server Narrow |
| Project Task 读取 | `/api/projects/{project}/tasks/*` | PostgreSQL JSONB | `project.view` | Server Ready |
| Project Job Control | `/api/projects/{project}/batches*`、`/jobs*` | PostgreSQL Queue | `project.view` / Operation Worker 权限 | Server Narrow |
| Project Prompt Library | `/api/projects/{project}/prompt-snapshots*`、`/prompt-defaults/*` | PostgreSQL Immutable Snapshot | `project.view` / `article.edit` | Server Ready |

## 3. Task 写操作矩阵

| 业务操作 | Project-scoped Server 路径 | 权限 | 存储边界 | 状态 |
|---|---|---|---|---|
| 完全重写 | `POST .../rewrite-from-scratch` | `article.edit` | PostgreSQL CAS + Audit | Server Ready |
| 选择当前标题候选 | `PUT .../selected-title` | `article.edit` | Server-owned Candidate + CAS + Audit | Server Ready |
| 保存/确认已审阅大纲 | `PUT .../outline` | `article.edit` | PostgreSQL Version + CAS + Audit | Server Ready |
| 恢复历史大纲为草稿 | `POST .../outline/restore-version` | `article.edit` | Server-owned Version + CAS + Audit | Server Ready |
| 选择已确认产品 | `PUT .../products` | `article.edit` | Published Evidence 投影 + CAS + Audit | Server Ready |
| 产品重新发现 | `POST .../product-rediscovery` | `knowledge.edit` | PostgreSQL Job + S3 Inbox Evidence | Server Ready |
| 替换指定章节 | `PUT .../article/sections` | `article.edit` | Heading Scope + Version + CAS + Audit | Server Ready |
| 准备文章图片 | `POST .../prepare-images` | `article.edit` | 私有 Asset + 内存 WebP + CAS + Audit | Server Ready |
| 初稿 AI 截图 | `POST .../checks/initial-ai/screenshot` | `article.review` | 私有 Initial PNG Asset + CAS + Audit | Server Ready |
| 初稿 AI 确认 | `PUT .../checks/initial-ai` | `article.review` | Initial Article Hash 绑定 + CAS + Audit | Server Ready |
| 保存人工审阅 Humanized Article | `PUT .../humanized-article` | `article.edit` | 结构/事实门禁 + Version + CAS + Audit | Server Ready |
| 最终 AI 截图 | `POST .../checks/final-ai/screenshot` | `article.review` | 私有 PNG Asset + CAS + Audit | Server Ready |
| 最终 AI 确认 | `PUT .../checks/final-ai` | `article.review` | Article Hash 绑定 + CAS + Audit | Server Ready |
| 恢复首稿链接 | `POST .../restore-links`、`GET .../restore-links/jobs/{job_id}` | `article.edit` | PostgreSQL Job + Template/Article Hash + 确定性校验 + CAS/Audit | Server Ready |
| 保存 SEO Review 设置 | `PUT .../seo-review-settings` | `article.edit` | Project Review Prompt 解析 + Keyword 门禁 + CAS/Audit | Server Ready |
| 生成 SEO Review Run | `POST .../seo-reviews`、`GET .../seo-reviews/jobs/{job_id}` | `article.review` | PostgreSQL Job + Prompt/Template/Article/Published Chunk 身份 + CAS/Audit | Server Ready |
| 裁决/预览/应用/完成 SEO Review | `PUT .../seo-reviews/{review}/changes/{change}`、`POST .../{preview|apply|complete}` | `article.review`；Apply 为 `article.edit` | 精确 ID + Open/Source Hash + Preview Hash + CAS/Audit | Server Ready |
| 导出文章 DOCX | `POST .../export-docx` | `article.deliver` | 私有 DOCX Asset + CAS + Audit | Server Ready |
| 生成 TDK DOCX | `POST .../generate-tdk` | `article.deliver` | 私有 TDK Asset + CAS + Audit | Server Ready |
| 打包交付 ZIP | `POST .../package-delivery` | `article.deliver` | 私有 ZIP Asset + CAS + Audit | Server Ready |

旧 `/api/tasks/{task_id}/*` 路径即使存在同名操作也仍是 Local Only；Server 版本不能通过
重用旧 Handler 而省略 Project Scope、PostgreSQL Repository 或对象身份复核。

## 4. 仍为 Local Only 的路由组

| 路由组 | 当前依赖 | Server 目标 | 迁移前必须补齐 |
|---|---|---|---|
| `/api/config`、`/api/settings/llm` | Local Config/.env | Server 管理配置 | Secret Manager、组织/环境权限、公开字段分型 |
| `/api/dashboard`、`/api/sync-tasks`、`/api/init-week` | JSON/Excel/本地目录 | SQL Project Dashboard/Import | Project Scope、幂等导入、来源摘要与 Audit |
| `/api/topic-files/upload` | 本地上传路径 | 私有 Topic Asset | ObjectStore、内容哈希、Project 权限 |
| `/api/projects/{customer}/brand|context|domain` | Local TaskStore/Project 文件 | Project Metadata Service | PostgreSQL Schema、CAS/Audit、官网域名安全门 |
| Project Prompt Library | Project-scoped PostgreSQL HTTP、显式当前 Snapshot 导入和 Outline/Article/SEO Review 消费已完成；旧 Local HTTP 仍属 SQLite | Project Prompt Snapshot | 旧路由继续关闭；后续消费者必须固定精确 Version |
| Product 主生成链 | Local TaskStore + LLM | Project Task Command/Job | 候选与提交分离、Published Context、Provider 错误脱敏、CAS/Audit |
| Humanize Job | Local TaskStore + LLM | Project Job | 仍需正式 Server Prompt Snapshot/Provider；SEO Review 全闭环已迁移 |
| 本地图片上传/预览 | 本地文件路径 | 私有 Asset | 类型/像素/哈希门禁、短期下载 |
| `/api/batches*`、`/api/batch-jobs*` | SQLite Queue | 不迁移该无 Project 兼容路径 | 继续 503；调用方改用 Project-scoped Control |

## 5. PostgreSQL Job Operation 现状

| Operation | Enqueue | Worker | Claim 前授权 | Handler 前授权 | 控制面 |
|---|---|---|---|---|---|
| `product_rediscovery` | Project-scoped、与 Task Revision/Audit 同事务 | Project Registry | `knowledge.edit` | `knowledge.edit` | Project-scoped Batch/Job 列表、取消、重试已完成 |
| `titles` | Project-scoped、固定 Task Revision/Template Hash/Published Chunk ID | Project Registry，只写候选 | `article.edit` | `article.edit` | Project-scoped 状态与 Batch/Job 控制已完成 |
| `outline` | Project-scoped、固定 Task Revision/Prompt Version/Published Chunk ID | Project Registry，只写 Review Draft | `article.edit` | `article.edit` | Project-scoped 状态与 Batch/Job 控制已完成 |
| `article` | Project-scoped、固定 Task Revision/Prompt Version/目标字数/Published Chunk ID | Project Registry，只写 Raw/Initial Draft | `article.edit` | `article.edit` | Project-scoped 状态与 Batch/Job 控制已完成 |
| `restore_links` | Project-scoped、固定 Task Revision/Template Hash/Initial 与 Humanized Hash/链接数 | Project Registry；模型只产候选，确定性校验后写 Linked Version | `article.edit` | `article.edit` | Project-scoped 状态与 Batch/Job 控制已完成 |
| `seo_review` | Project-scoped、固定 Task Revision/Initial Article Hash/Prompt Version/System Template Hash/Published Chunk ID | 共享 Lifecycle + Operation-specific Enqueue/Handler；只追加 Open Review Run | `article.review` | `article.review` | Project-scoped 状态与 Batch/Job 控制已完成 |
| `products` | Local SQLite | Local Runner | 无 Server Actor | 无 Server Actor | Local Only |
| `rewrite_article` | Local SQLite | Local Runner | 无 Server Actor | 无 Server Actor | Local Only |
| `humanize` | Local SQLite | Local Runner | 无 Server Actor | 无 Server Actor | Local Only |
| `knowledge_research` | Local Research Queue | Local Runner | 无完整 Server 链 | 无完整 Server 链 | Local Only |

不能因为 `PostgresJobQueue` 支持任意 Operation 字符串，就把某个 Operation 标成已迁移。
每个 Operation 必须有可信 Requester、两阶段权限映射、Server-only Handler、私有存储
边界和取消/重试测试。

标题“生成”与标题“选择”是两个边界：`POST .../titles` 只接受 Revision，服务端固定
checked-in Template Hash 与当前 Published Chunk ID，Provider 返回不足/重复/超长候选
时失败，不调用本地 `collect_customer_context()` 或 `mock_titles()`。成功只写候选并
清空旧选择和下游。`selected-title` 命令只允许客户端提交当前 Revision 和候选索引，
服务端从 PostgreSQL Task 的当前 `title_candidates` 取值；它不接受调用方替换标题正文，
候选生成与人工选择保持两个独立 CAS/Audit。

大纲“生成”与大纲“保存/确认”继续分离：`POST .../outline` 只接受当前 Revision，
入队时固定 PostgreSQL Prompt ID + Version 与当前 Published Chunk ID；Worker 执行前
再次授权并复核 Chunk 仍属于同 Project 的当前 Published Snapshot。生成结果只追加
`outline_draft` Version，不替换确认大纲、不使下游失效；Provider 未配置、返回空结果、
Prompt/Chunk 漂移、撤权或 Task CAS 冲突均 fail closed，不生成 mock。
`PUT .../outline`
只保存编辑者已审阅的 Markdown；草稿追加 `outline_draft` Version 但保留当前确认大纲
和下游产物，确认则追加 `outline` Version 并使正文、图片、检查与交付产物失效。
`POST .../outline/restore-version` 只接受 Version Index，从当前 Task 的服务器版本历史
恢复 `outline/outline_draft` 为新草稿；它不接受客户端历史正文，也不恢复 Article Version。

正文初稿 `POST .../article` 只接受当前 Revision；服务端固定 Article Prompt Version、
目标字数和当前 Published Chunk ID。Worker 重新验证 Task 仍允许生成、Prompt/Chunk
仍有效后才调用 Provider；Provider 错误、空输出、缺少过渡段、H2/H3 或最终 FAQ 契约
都使 Job 失败，不补 mock、不写本地 Artifact。成功追加 Raw/Initial Version 并进入
`draft_ready`，但不会自动确认 AI 检查、Humanize、链接、图片或交付。

人工 `PUT .../humanized-article` 与自动 `humanize` Job 是两个边界：前者只接受 Revision
和有界 Markdown，服务端复用结构、数字事实、FAQ、表格、列表与必须短语校验，成功追加
`external_manual` Version 并进入 `humanized_ready`；它不读取本地 Prompt。自动 Job
仍依赖 `humanize_prompt_path` 外部文件，在正式 Prompt Snapshot 或其他服务器准源接线前
保持 Local Only。

`restore_links` 已从该 Local 组合中拆出。Server Enqueue 只接受 Revision，并固定
checked-in Template Hash、Initial/Humanized Article Hash、来源链接数和 Final Check
绑定。Provider 只产生候选；提交前必须由确定性校验证明链接/URL 多重集合与首稿完全
一致、非链接可见正文与 Humanized Article 一致。模板或正文漂移、撤权、Provider
失败、CAS/Audit 失败均不写 Task，也不向公开 Job/Audit 暴露正文、Hash 或 URL。

`seo_review` 生成也已从 Local 组合中拆出，但人工修改/应用仍未迁移。Server Enqueue
只接受 Revision，固定 Initial Article、精确 Project Review Prompt Version、
checked-in System Template Hash 与当前 Published Chunk ID。Provider 只能读取注入的
Published Context，不能调用本地 Customer Context 或生成 mock；成功只追加 Open
Review Run，不修改文章或 Workflow Status。Change/Preview/Apply/Complete 已拆成
Project-scoped 人工命令：Change/Preview/Complete 要求 `article.review`，Apply 额外要求
`article.edit`；路径固定 Review/Change ID，Body 不能覆盖身份。Apply 必须重新构建完整
Preview 并匹配 SHA-256，Change/Apply/Complete 使用 Revision CAS 与安全 Audit。

## 6. 已完成闭环：Project Job Control

已为迁移完成的
`product_rediscovery/titles/outline/article/restore_links/seo_review` 增加：

```text
GET  /api/projects/{project}/batches
GET  /api/projects/{project}/batches/{batch_id}
POST /api/projects/{project}/batches/{batch_id}/cancel
POST /api/projects/{project}/jobs/{job_id}/cancel
POST /api/projects/{project}/jobs/{job_id}/retry
```

目标公共读模型不得返回 `request`、`requested_by_user_id`、Category URL、原始错误或对象
URI。读取要求 `project.view`；Cancel/Retry 要求 Operation 对应的 Worker 权限，并在同一
事务锁定权限事实、Job/Batch 状态和 Audit。Retry 不接受客户端替换 Request、Requester、
Operation、Task 或 Source Revision；它重放服务器已保存的同一可信命令，并由 Worker
再次执行两阶段授权。

旧 `/api/batches*` 在控制面完成后仍保持 503，调用方必须使用 Project-scoped 路径，
不能建立无 Project 的兼容别名。下一项 Operation 只有在可信 Enqueue、两阶段权限、
Server-only Handler、私有存储和停机测试全部完成后，才能加入显式 Operation 集合。

## 7. 重构检查点

1. Route Gate 是否仍为精确 Method + Segment 白名单？
2. 新 Router 是否只消费 Project-scoped Service，不直接访问 Local `store()`/`batch_queue()`？
3. 公共 Job DTO 是否与 Queue 内部 Dict 分离？
4. Cancel/Retry 是否在事务内重新读取权限，而非只依赖 Router 的先前判断？
5. Retry 是否拒绝客户端修改私有 Request 或 Source Revision？
6. 未迁移 Operation 是否仍不可读、不可取消、不可重试、不可被 Server Runner Claim？
7. 取消终态与 `background_job.terminal`、操作者命令 Audit 是否保持一致且可回滚？
8. 跨 Organization/Project ID 是否只在当前 Scope 查询，不扫描后再过滤？
9. SQLite 冻结窗口证据是否仍与目标 Organization/Project/摘要绑定？
10. Server 单写切换后，Local Mode 是否仍可独立使用 SQLite，且两种模式不会双写？
11. 标题选择是否仍只读取当前 PostgreSQL Task 的候选并拒绝客户端标题正文？
12. `titles` Job 是否固定系统模板 Hash 与当前 Published Chunk ID，并在 Provider
    输出不完整时失败而不补 mock？
13. 大纲草稿是否仍保留当前确认大纲和下游产物，而确认大纲才执行下游失效？
14. `outline` Job 是否只固定 PostgreSQL Prompt Version 与当前 Published Chunk ID，
    且只写可审阅草稿、不自动确认或生成 mock？
15. 大纲恢复是否只按当前 Task 的 Version Index 读取 `outline/outline_draft`，并拒绝
    Article Version 与客户端历史正文？
16. `article` Job 是否固定 Prompt Version、目标字数和 Published Chunk ID，只写
    Raw/Initial Draft，并在结构错误时失败而不补 mock 或触发下游阶段？
17. Prompt Default 是否绑定精确不可变 Version，而不是只指向会漂移的可变正文？
18. Server Outline/Article Worker 接线后，旧 Local Prompt API 是否仍保持关闭，且 Worker
    只读取 PostgreSQL 不可变 Prompt Version、不混用 SQLite Prompt？
19. SQLite Prompt 是否只通过显式一次性导入进入指定 Project，且非空差异目标不会被
    覆盖、旧库未保存的历史 Version 不会被伪造？
20. `seo_review` Job 是否只接受 Revision，并固定 Initial Article、Review Prompt
    Version、System Template Hash 与 Published Current Chunk ID？
21. Server Review Provider 是否只读取注入 Context、不触碰本地 Customer 文件、不补
    mock，且只追加 Open Review Run？
22. Review 生成失败、身份漂移、撤权或 Audit/CAS 回滚时，是否保持文章、Workflow
    Status、Revision 和已有 Review Run 不变？
23. `ServerProjectJobRegistry` 后续被旧 Operation 采用时，是否仍把业务 Enqueue、
    权限、私有 Request 与 Handler 留在 Operation-specific 层？
24. Review Change/Preview/Apply/Complete 是否仍使用精确路径身份、Open/Source Article
    Hash 门禁，并拒绝 Body 注入 Review/Change ID？
25. Apply 是否仍要求 `article.edit` 和精确 Preview Hash，而 Complete 只允许没有
    Accepted Change 的 Open Run？
