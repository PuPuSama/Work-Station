# M7 Server Article Console：结构与接口痕迹

## 1. 目的

本文记录 Server 文章目录与单篇工作台的组件边界、API 作用和重构不变量。它是后期
重构导航，不是后端授权准源，也不表示 M7 已达到生产上线条件。

本切片解决的具体问题是：

- `/projects/{project}/articles` 和详情页此前总是挂载 Local SQLite 组件；
- Server Project Shell 只开放 Delivery，已完成的 Project-scoped Task 命令没有操作面；
- 如果直接复用 Local Workbench，会把 `/api/tasks*`、`/api/dashboard`、Local Prompt
  和本地文件路径重新带入 Server Mode。

## 2. 组件拓扑

```text
articles/page.tsx
  -> ProjectArticleDirectory
       -> GET /api/auth/status
       -> local  -> ProjectArticleList
       -> server -> ServerProjectArticleList
                    -> ServerTaskIntakePanel
                         -> POST /api/projects/{project}/tasks
                         -> POST /api/projects/{project}/task-imports

articles/[taskId]/page.tsx
  -> ProjectArticleWorkspace
       -> GET /api/auth/status
       -> local  -> ArticleWorkbench
       -> server -> ServerArticleWorkbench

ProjectShell
  -> server: Article + Batch/Job Control + Delivery + authorized Settings
  -> local:  existing Article + Knowledge flag + Batch + Delivery + Settings

batches/page.tsx
  -> ProjectBatchDirectory
       -> local  -> ProjectBatchCenter
       -> server -> ServerProjectBatchCenter

batches/[batchId]/page.tsx
  -> ProjectBatchWorkspace
       -> local  -> ProjectBatchDetail
       -> server -> ServerProjectBatchDetail
```

`ProjectArticleDirectory` 和 `ProjectArticleWorkspace` 是唯一模式分流点。Server 组件不接收
Local Store、Dashboard 或 Config 作为 Props，也不在 Server 请求失败后回退 Local。
批次页面使用同样的 `ProjectBatchDirectory/Workspace` 分流原则。

## 3. 主要代码职责

| 文件 | 作用 | 重构时不得丢失的边界 |
|---|---|---|
| `project-article-directory.tsx` | 列表页 Local/Server 组件树分流 | Auth 失败不挂载任一数据组件 |
| `project-article-workspace.tsx` | 详情页 Local/Server 组件树分流 | Server Task 不得进入 `ArticleWorkbench` |
| `server-project-article-list.tsx` | 读取 Project-scoped Task、搜索和状态定位 | 只调用 `/api/projects/{project}/tasks` |
| `server-task-intake-panel.tsx` | 单条创建与 Tab 分隔行导入 | 只提交话题行和幂等键；不提交 Task ID/序号/客户/状态/Revision，不读取 Local XLSX |
| `server-article-workbench.tsx` | 编排已迁移的人工命令与异步 Job | 每次写入提交当前 Revision；Job 只按公开 ID 轮询 |
| `server-seo-review-panel.tsx` | Review 设置、Run 选择、逐条裁决、预览与完成 | Apply 必须回传当前精确 Preview Hash；风险与 Pending 均显式确认 |
| `server-outline-history.tsx` | 展示 Task 内 Outline Version 并恢复草稿 | 只提交服务端 `version_index`，不回传历史正文 |
| `server-section-rewrite-panel.tsx` | 从 Initial Article 提取标题路径并提交局部替换 | 只提交 `heading_path` 与 `replacement_body`，后端仍是解析和校验准源 |
| `server-product-rediscovery-panel.tsx` | 按官网分类页启动产品重新发现 | 只提交 Revision、官方 Category URL 与 1–50 上限；结果只入 Inbox |
| `server-task-reset-panel.tsx` | 显式确认完全重写 | 只提交 Revision；不在浏览器删除历史对象或审计 |
| `project-batch-directory.tsx` / `project-batch-workspace.tsx` | Batch 页面 Local/Server 组件树分流 | Auth 失败不猜测准源，Server 失败不回退 Local |
| `server-project-batch-center.tsx` / `server-project-batch-detail.tsx` | Project-scoped Batch/Job 列表、取消和重试 | 公共 DTO 不读取 Request、Requester、URL、Prompt 或原始错误 |
| `server-project-job-center.tsx` | 全局 Server Job 抽屉 | 只展示已迁移 Operation；Cancel/Retry 使用空 Body |
| `project-shell.tsx` | Server 导航开放 Article/Batch/Delivery | 导航是可用性提示，不是授权准源 |
| `server-project-selector.tsx` | SQL Project Directory 的默认入口 | 只跳转当前返回的 `project_id` |
| `project-settings-entry.tsx` | Local/Server 项目设置组件树分流 | Auth 状态失败不挂载旧 Local Settings |
| `server-project-settings.tsx` | 共享项目身份资料表单 | 只提交 Revision、显示名和官方域名；冲突显式重载，不承载业务事实或 Prompt |
| `server-project-members.tsx` | 显式成员权限管理 | 与 Metadata 同页但使用独立 Project-scoped API 和事务 |

## 4. Server 工作台数据流

首次加载并行读取：

1. `GET /api/projects/{project}/tasks/{task_id}`：Task 正文和当前 Revision；
2. `GET /api/projects`：当前 Actor 的 Effective Role，仅用于按钮提示；
3. `GET /api/projects/{project}/catalog`：只读 confirmed Product 摘要与当前 Published
   Snapshot 图片摘要，不返回 Canonical/Source URL、对象 URI/Key、哈希或 Metadata；
4. `GET /api/projects/{project}/assets/{asset_id}/download`：对 Catalog 中的图片逐个
   重新授权并返回短时预览 URL；URL 只留在组件内存，不写回 Task 或 Catalog DTO。

后端仍是权限准源。前端按钮隐藏或禁用不能替代以下事实：

- Project Scope 由 Cookie Actor 和路径共同解析；
- 写操作在事务内重新锁定可撤权权限；
- Task 更新使用 Revision CAS；
- Worker 在 Claim 前和 Handler 前再次授权。

项目设置使用独立数据流，不复用 Task 写入：

```text
GET /api/projects/{project}/metadata
  -> project.view
  -> { project_id, customer_name, official_domain, revision }
PUT /api/projects/{project}/metadata
  -> project.members.manage
  -> transaction re-lock access facts + active project
  -> Revision CAS + redacted append-only Audit
  -> affects future Task Intake; never rewrites existing Tasks
```

Metadata 不接收自由 Context。权威客户/产品事实进入 Published Knowledge，写作规则进入
不可变 Prompt Snapshot；这条分界避免一个方便的设置表单重新成为不可审计的事实仓库。

## 5. 主链接口映射

| UI 阶段 | 接口 | 客户端允许提交的内容 |
|---|---|---|
| 单条创建 Task | `POST /api/projects/{project}/tasks` | Intake ID、Topic、可选竞对关键词/HTTP(S) URL |
| 批量导入 Task | `POST /api/projects/{project}/task-imports` | Intake ID、来源标签、1–200 条规范化话题行 |
| 标题候选 | `POST .../titles` + `GET .../titles/jobs/{job}` | Revision |
| 完全重写 | `POST .../rewrite-from-scratch` | Revision + 显式 UI 风险确认 |
| 选择标题 | `PUT .../selected-title` | Revision、Candidate Index |
| 选择产品 | `PUT .../products` | Revision、1–3 个 confirmed Product ID |
| 产品/图片目录 | `GET .../catalog` | Product/Image Limit；响应为无 URL 的最小摘要 |
| 图片短时预览 | `GET .../assets/{asset_id}/download` | Asset ID、短期有效秒数 |
| 产品重新发现 | `POST .../product-rediscovery` + Job GET | Revision、官方 Category URL、Max Products |
| 大纲生成 | `POST .../outline` + Job GET | Revision |
| 大纲保存/确认 | `PUT .../outline` | Revision、Markdown、Confirmed |
| 大纲版本恢复 | `POST .../outline/restore-version` | Revision、服务器 Version Index |
| 初稿生成 | `POST .../article` + Job GET | Revision |
| 章节重写 | `PUT .../article/sections` | Revision、Heading Path、Replacement Body |
| 初检截图/确认 | `POST .../checks/initial-ai/screenshot`、`PUT .../checks/initial-ai` | PNG、Revision、分数/报告 |
| 自动人化 | `POST .../humanize` + Job GET | Revision |
| 人工人化稿 | `PUT .../humanized-article` | Revision、有界 Markdown |
| 终检截图/确认 | `POST .../checks/final-ai/screenshot`、`PUT .../checks/final-ai` | PNG、Revision、分数/报告 |
| 链接恢复 | `POST .../restore-links` + Job GET | Revision |
| SEO Review 设置/生成 | `PUT .../seo-review-settings`、`POST .../seo-reviews` + Job GET | Keyword、Project Default 选择、Revision |
| SEO Change 裁决 | `PUT .../seo-reviews/{review}/changes/{change}` | Revision、Decision、Reviewed Text、Risk Confirmation |
| SEO Preview/Apply | `POST .../seo-reviews/{review}/preview|apply` | Revision；Apply 另带精确 Preview Hash 与 Pending Confirmation |
| SEO Complete | `POST .../seo-reviews/{review}/complete` | Revision、Pending Confirmation；不得存在 Accepted Change |
| 图片准备 | `POST .../prepare-images` | Revision、Hero Asset ID、Product ID 到 H2 的锚点 |
| Word/TDK/ZIP | `POST .../export-docx`、`generate-tdk`、`package-delivery` | Revision |
| 产物下载 | Task-scoped `.../download` | 无对象路径；响应为短期 URL |
| Batch 列表/详情 | `GET /api/projects/{project}/batches*` | Limit、稳定 Cursor；无私有 Job Request |
| Cancel/Retry | `POST /api/projects/{project}/batches|jobs/{id}/*` | 空 Body；服务端重放可信请求 |

异步 Job 的浏览器等待不是 Worker 生命周期。页面只轮询公开状态
`queued/retry_wait/running/succeeded/failed/cancelled/conflict`；等待超时只提示刷新，不发送
取消命令。响应不读取 Request、Requester、Prompt、Chunk、正文、URL 或原始错误。

## 6. SEO Review 状态机

```text
Open Review Run
  -> Change: pending / accepted / rejected
       -> accepted + protected-fact risk 必须 confirm_risks
       -> 每次保存推进 Task Revision，并使客户端旧 Preview 失效
  -> Preview: 由服务端按当前 Open Run 重新构建完整候选正文
       -> 返回 article_hash，不改变 Task
  -> Apply: 重新构建并匹配同一个 article_hash
       -> 要求 article.edit，追加 Initial Version，使下游失效
  -> Complete: 仅在没有 accepted Change 时完成
       -> 不改变正文；存在 pending 时必须 confirm_pending
```

前端可以展示 Score、Dimension、Change 和候选正文，但不能把 Preview 当成可持久化草稿。
任何 Change 保存、Task Revision 变化或源文章变化都会使旧 Preview Hash 失效。Reviewer
可以裁决、预览和不改正文完成；只有 Editor/Lead/Admin 可以 Apply。

## 7. 大纲版本与章节重写

大纲历史和正文局部编辑都使用“客户端选择身份、服务端读取内容”的窄命令：

- 大纲版本列表来自当前 Task 的 `article_versions`；UI 保留原数组索引，因为 Router
  接受的是该服务端版本索引。恢复只生成新的 `outline_draft/restored` 版本，不自动确认；
- 章节选择器从当前 Initial Article 提取 H2-H6 路径，仅作为操作辅助。服务端会重新
  解析 Markdown、忽略 fenced code block 内伪标题，并拒绝不存在或歧义路径；
- 章节提交不含目标标题、不含全文、不含字符偏移。`replacement_body` 不能引入同级或
  更高级标题；成功时服务端原子保存 Before/After Version 并使人化、链接、图片和交付
  下游失效。

前端 Markdown 提取器不是准源。以后替换为 AST 编辑器时，`heading_path` 窄契约和服务端
二次解析仍必须保留。

## 8. 产品与图片

产品选择和产品图片是两个独立身份层：

```text
Knowledge Product (confirmed)
  -> Task 只选择 product_id
  -> Server 投影 Published Current Snapshot 的事实与 selected_asset_id
  -> 图片准备时浏览器只提交 product_id -> heading
  -> Server 重新读取 Task Product 和私有 Asset
  -> 校验 Organization/Project、字节数、SHA-256
  -> 内存生成内容寻址 WebP
  -> Task ArticleImage 只保存 Asset 身份与正文锚点
```

Hero 图同样只通过 Project 私有 `asset_id` 指定。`PostgresServerProjectCatalog` 只列出
当前 Published Snapshot 中的图片，并把同一资产的多条 Snapshot Evidence 收敛成一个
`asset_id/content_type/byte_size/dimensions/label/evidence_kind` 摘要。它不返回 Bucket、
Object Key、Artifact URI、Hash、Source URL、Canonical URL 或 Metadata。

`ServerHeroAssetPicker` 再按 Asset ID 调用授权下载路由取得 5 分钟短时预览；预览 URL
只保存在组件内存，刷新即重新签名。选择结果仍只是 Asset ID，因此以后替换成缩略图服务、
CDN 或虚拟列表时，不需要改变 `prepare-images` 命令契约。产品图不允许在该面板另选：
它固定来自 Task 已确认产品的 `selected_asset_id`，避免浏览器把任意图片冒充产品证据。

产品重新发现和 Task 产品选择也保持两段式：

1. Rediscovery 只允许当前 Project 官方域名的 Category URL 和有界数量，Worker 把证据写入
   Inbox；
2. 人工审核、发布并确认产品后，Task 选择区才按 Product ID 投影正式事实；
3. Rediscovery Job 成功不修改 Task Revision、当前产品或文章，不把抓取结果直接当正式事实。

## 9. Server Knowledge Inbox

`/projects/{project}/knowledge` 与 Article/Batch 一样先读取 Auth Status，再显式分流组件树：

```text
Local
  -> ProjectKnowledgeLibrary
  -> ProjectResearchWorkspace
  -> ProjectEvidenceWorkbench

Server
  -> ServerKnowledgeWorkspace
       -> 来源 Inbox
          -> ServerKnowledgeInbox
          -> GET /api/knowledge/{project}
          -> ServerPrivateDocumentUpload
               -> POST .../sources/upload
          -> PUT .../sources/{source}/review
          -> POST .../sources/{source}/publish
          -> POST .../products/{product}/confirm
       -> 资料研究
          -> ServerResearchWorkspace
          -> POST /api/knowledge/{project}/tasks/{task}/retrieval-plan
          -> POST .../research-runs
          -> POST .../research-runs/{thread}/resume
          -> GET/SSE .../research-runs/{thread}/*
          -> GET .../evidence-packs/{pack}
```

Server 分支只挂已完成 PostgreSQL Project Scope 和 Knowledge 权限映射的入口。私有
Upload 已改为 Project-scoped ObjectStore Prepare + PostgreSQL/Audit 原子提交；
Research Plan/Start/Resume 已改为确认 Task、不可变 Plan、私有 PostgreSQL Job、
PostgreSQL Checkpoint 和 S3 候选入库，因此可渲染。通用 Plan POST、WordPress Sync 和
Raw Artifact 仍不渲染。不能因为同组窄路径已迁移就整体放开 503 路径。

Upload 也不是单事务伪装：先在初次授权后解析并写内容寻址对象，再在 PostgreSQL 事务内
重新锁定可撤权事实、Active Project 和 Source，把 Source/Snapshot/Chunk/Asset Link 与
脱敏 Audit 一次提交。Phase 2 失败只可能留下延迟对账的对象 orphan，不留下可查询的
半成品。完整结构和重构接缝见
`docs/architecture/m7-server-private-document-ingestion.md`。

来源审阅和发布是两个显式步骤：审阅保存 Source Kind、Trust Tier、Decision 与有界
Reason；只有 `inbox` 且包含 Chunk 的来源显示发布按钮。发布调用 Embedding Provider，
失败时 UI 保留当前来源状态并重新读取服务器事实，不伪造已发布结果。产品确认只改变
Catalog 身份；文章选择器仍以 Published Current Evidence 为二次门禁。

三个 Server 写命令不以 Router 的先验授权作为最终结论。后端统一进入
`PostgresServerKnowledgeCommands`：

```text
Review
  -> 事务内锁定可撤权 Project 事实 (knowledge.edit)
  -> 锁 Knowledge Source
  -> 更新分类/Review Metadata
  -> append knowledge.source.reviewed
  -> 同一事务提交

Publish
  -> 初次 knowledge.publish 拒绝无权 Provider 消耗
  -> prepare: 分批 Embedding + 保存 Candidate Vector
  -> 最终事务重新锁定 knowledge.publish
  -> 复核 approve Review + Source/Snapshot/Model
  -> 切换 Current Snapshot + append knowledge.source.published
  -> 同一事务提交

Confirm
  -> 事务内锁定 knowledge.publish
  -> 复核 Primary Detail Evidence + 更新 Product
  -> append knowledge.product.confirmed
  -> 同一事务提交
```

Embedding 是外部调用，不能伪装成 PostgreSQL 原子事务的一部分。它只准备尚未服务的
向量；最终激活与 Audit 才原子提交。Provider、撤权或 Audit 在最终提交前失败时，新向量
可以留作同一快照的幂等重试，但 `current_snapshot_id` 保持旧值。未显式指定 Snapshot
时，最终事务还会复核 Candidate 仍是 Latest Snapshot，避免并发入库后激活旧版本。
重复发布同一当前快照和重复确认已确认产品不追加第二条 Audit。Review Audit 只含
Decision、Source Kind 和 Trust Tier；Publish 只含不可变 Snapshot ID、Chunk Count
与 Embedding Model；Reason、正文、URL、原始 Content Hash、Artifact URI 和 Secret
均不进入 Audit。

Research 浏览器只提交 Candidate ID；URL 由当前 Run 的 Gap Attempt 在服务端解析并仅
进入私有 Job。Start/Resume 的 Run/Event/Batch/Job 与脱敏 Audit 同事务，Worker 在
Claim、Handler、逐候选抓取和最终 Publish 分层复核权限。Research Job 可在公共队列中
读取，但通用 Cancel/Retry 前后端都关闭；继续执行只能创建新的领域 Resume Job。完整
结构和重构接缝见 `docs/architecture/m7-server-knowledge-research.md`。

前端 Effective Role 只控制提示：Reviewer/Viewer 为只读，Editor/Lead/Admin 显示命令；
Router 仍分别要求 `project.view`、`knowledge.edit` 和 `knowledge.publish`。Server 页面
不提供 Raw Evidence 链接，也不复用 Local 文件持久化或 Local 研究组件。

## 10. Project-scoped Job Control

Server Header、批次列表和详情共用同一公共 DTO：

```text
ServerBatchPage
  -> ServerBatchSummary
       -> ServerJobSummary
            job_id / batch_id / task_id / operation / status
            revision / attempts / timestamps / booleans
            no request / requester / prompt / chunk / URL / raw error
```

列表使用稳定 `after_batch_id` Cursor；轮询只在存在 Active Job 时加速，使用串行
`setTimeout` 避免请求重叠。Cancel/Retry Body 始终为空，前端 Role 只决定按钮提示，后端
仍在事务内锁定可撤权事实并按 Operation 检查 `knowledge.edit/article.edit/article.review`。
Retry 重放服务器私有请求，浏览器不能修改 Source Revision、Task、Requester 或参数。

## 11. Server Task Intake

Task Intake 把“输入来源”和“正式 Task 身份”分开：

```text
ServerTaskIntakePanel
  -> 规范化单条或 1–200 条 Tab 分隔行
  -> POST Project-scoped Intake API
  -> 路由要求 article.edit
  -> PostgresServerTaskIntakeService
       -> 事务内重锁可撤权 Project 权限
       -> 以 Organization + Project + Intake ID 取得事务级幂等锁
       -> 同一 Intake ID + 同一规范化摘要：返回原 Task，不追加 Audit
       -> 同一 Intake ID + 不同摘要：409
       -> 锁 task_store_state，服务端顺序分配 topic_index
       -> 服务端生成 Task ID、Project customer/brand 和 server source identity
       -> 原子写 article_tasks + task_intakes + append-only Audit
```

`task_intakes` 是不保存原始正文的幂等凭据，只含 Intake Kind、来源标签、SHA-256 摘要、
Task ID 列表、数量和 Actor。Audit 只含 Kind、数量及首尾序号，不含 Topic、竞对关键词、
URL、来源摘要或文件内容。Audit 失败时 Task 和 Intake Receipt 一起回滚。

单条创建和行导入都不接受客户端 `task_id/topic_index/customer/brand/status/revision`。
新 Task 的 `week_folder=server`、`task_dir=""`，后续产物继续只走 PostgreSQL 和私有
ObjectStore。前端在一次失败重试期间保留 Intake ID；任意输入变化会生成新身份。以后把
Textarea 替换为 CSV/XLSX Parser 时，应只替换“文件到规范化行”的前置解析层，不改变
Project API、Receipt、服务端身份分配或事务审计边界。

## 12. 仍未等价迁移的 Local 控制

Task 单条创建与规范化行导入已经接入，但以下 Local 能力没有伪装成 Server 能力：

- `/api/topic-files/upload` 仍写本地 Topic Library；Server 若需要原始 XLSX 留档，必须
  先设计私有 Topic Asset、内容哈希和 Parser Version，不能把本地路径带进 Intake；
- `/api/dashboard`、`/api/sync-tasks`、`/api/init-week` 仍是 Local 兼容路径；Server
  目录直接读取 Project-scoped PostgreSQL Task；
- Project Brand/Context 元数据编辑仍缺独立 PostgreSQL Metadata Service。

Product Rediscovery 的创建与 Job 状态已接入；结果由独立 Server Knowledge Inbox
审阅，不在文章工作台复制来源、发布或产品确认状态机。

## 13. 重构检查清单

1. Auth Status 失败时是否仍不会猜测 Local/Server？
2. Server 列表和详情是否仍只使用显式 Project 路径？
3. Server 页面是否仍不会请求 `/api/tasks*`、`/api/dashboard` 或 `/api/config`？
4. Role 是否仍只影响界面提示，后端是否仍逐请求授权？
5. 每个 Task 写操作是否仍提交最新 Revision？
6. 截图上传使 Revision 增加后，确认命令是否先重新读取 Task？
7. Job 超时是否仍不等于取消？
8. 产品选择是否仍只提交 confirmed Product ID？
9. 图片准备是否仍不接受产品事实、Bucket、Key、URL 或本地路径？
10. 下载是否仍先取得 Task-scoped 短期 URL？
11. Local 页面、API 和导航是否仍可独立工作？
12. 新增命令面板时是否同步更新路由迁移矩阵与本文件？
13. SEO Apply 是否仍只能使用当前精确 Preview Hash，且 Pending/Risk 确认不会被默认勾选？
14. Outline 恢复是否仍只提交服务器数组索引而不回传历史正文？
15. Section Rewrite 是否仍只提交 Heading Path/Replacement Body，并由服务端重新解析全文？
16. Rediscovery 是否仍只产生 Inbox Evidence，不自动替换 Task Product？
17. 完全重写是否仍要求显式风险确认，且只提交 Revision？
18. Server Batch/Job UI 是否仍使用 Project 路径、公共 DTO 与空 Cancel/Retry Body？
19. Catalog 是否仍只列出当前 Published Snapshot，且不返回对象位置、哈希或来源 URL？
20. 短时图片 URL 是否仍只存在于组件内存，产品图是否仍不能由 Hero 选择器覆盖？
21. Knowledge 页面是否仍按 Auth Status 分流，Server Upload/Research 是否只走
    Project-scoped ObjectStore/PostgreSQL，且不挂载 Local Research、WordPress Sync 或
    Raw Artifact？
22. 来源 Review/Publish 是否仍为两个动作，产品 Confirm 是否仍不能绕过文章选择时的
    Published Current Evidence 门禁？
23. Task Intake 是否仍不接受客户端 Task ID、序号、客户、状态、Revision 或本地路径？
24. 同一 Intake ID 的同内容重试是否仍只产生一批 Task 和一条 Audit，不同内容是否 409？
25. Task、Intake Receipt 与安全 Audit 是否仍为同一事务，Audit 是否仍不含 Topic/URL/
    Source Digest？
26. 私有文档上传是否在对象写前和数据库事务内两次重验 `knowledge.edit`，并把
    Source/Snapshot/Chunk/Asset Link/Audit 原子提交？
27. 去重 Asset 是否使用 Repository 实际返回的 Asset ID，Phase 2 失败是否只留下受
    延迟对账保护的对象 orphan？
28. Research 页面是否仍只提交 Plan ID、Request ID 与 Candidate ID，不提交 Organization、
    Requester、URL、对象位置或私有 Job Request？
29. Research Job 是否仍链接回 Knowledge Research，并同时在 UI 与后端禁止通用
    Cancel/Retry？
