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

## 4. Server 工作台数据流

首次加载并行读取：

1. `GET /api/projects/{project}/tasks/{task_id}`：Task 正文和当前 Revision；
2. `GET /api/projects`：当前 Actor 的 Effective Role，仅用于按钮提示；
3. `GET /api/knowledge/{project}`：confirmed 产品目录。该读失败不阻止查看 Task，但产品
   选择区为空并解释需要先完成正式产品确认。

后端仍是权限准源。前端按钮隐藏或禁用不能替代以下事实：

- Project Scope 由 Cookie Actor 和路径共同解析；
- 写操作在事务内重新锁定可撤权权限；
- Task 更新使用 Revision CAS；
- Worker 在 Claim 前和 Handler 前再次授权。

## 5. 主链接口映射

| UI 阶段 | 接口 | 客户端允许提交的内容 |
|---|---|---|
| 标题候选 | `POST .../titles` + `GET .../titles/jobs/{job}` | Revision |
| 完全重写 | `POST .../rewrite-from-scratch` | Revision + 显式 UI 风险确认 |
| 选择标题 | `PUT .../selected-title` | Revision、Candidate Index |
| 选择产品 | `PUT .../products` | Revision、1–3 个 confirmed Product ID |
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

Hero 图同样只通过 Project 私有 `asset_id` 指定。浏览器不提交 Bucket、Object Key、本地路径、
图片 URL、产品描述或产品图片事实。以后增加图片选择器时，它只能把现有 Asset ID 可视化，
不能改变该命令契约。

产品重新发现和 Task 产品选择也保持两段式：

1. Rediscovery 只允许当前 Project 官方域名的 Category URL 和有界数量，Worker 把证据写入
   Inbox；
2. 人工审核、发布并确认产品后，Task 选择区才按 Product ID 投影正式事实；
3. Rediscovery Job 成功不修改 Task Revision、当前产品或文章，不把抓取结果直接当正式事实。

## 9. Project-scoped Job Control

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

## 10. 当前明确未接入的控制

本切片是现有 Task 的主链操作面，不是完整 Local UI 等价迁移。以下后端能力仍需专用面板：

- Product Rediscovery 的 Inbox 结果审阅；
- Hero/产品 Asset 的可视化选择器；
- Server Task 导入/创建。

Product Rediscovery 的创建与 Job 状态已接入；“结果审阅”仍依赖正式 Knowledge 页面，
尚未在文章工作台复制一套 Inbox 审阅器。

这些入口不能通过把 Local `ArticleWorkbench` 的 Handler 改个 URL 来补齐；每个面板都必须
只提交 Server 契约允许的字段，并保留 Revision、权限和私有对象边界。

## 11. 重构检查清单

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
