# M7 Server 产品候选生成：结构记录与重构契约

## 1. 目的与边界

本文记录 Server Mode 的产品主生成链。它不是产品抓取器，也不是产品选择命令，而是位于
“正式产品目录”和“人工确认”之间的可审阅候选层。后续可以替换 Provider、队列调度器、
Task JSONB 兼容模型或前端组件，但必须保留本文的数据准源、授权、提交和失败语义。

产品链固定拆成三个彼此独立的能力：

| 能力 | 接口 | 最小权限 | 写入结果 | 不得发生 |
|---|---|---|---|---|
| 产品重新发现 | `POST/GET .../product-rediscovery` | `knowledge.edit` | 官网内容进入不可变 Knowledge Inbox | 不修改 Task、不得自动发布或选择产品 |
| 产品候选生成 | `POST .../products`、`GET .../products/jobs/{job_id}` | `article.edit` | PostgreSQL Job；成功后只写 `product_candidate_ids` | 不修改 `task.products`、Workflow Status 或下游产物 |
| 人工产品提交 | `PUT .../products` | `article.edit` | 从当前正式证据投影 `task.products` 并使下游失效 | 不接受客户端产品事实、URL、图片地址或对象身份 |

候选生成不能调用 Local `perform_auto_products()`。Local Mode 的 SQLite Queue、Crawler、
Tavily、文件 Artifact 和原有产品流程继续保持独立；Server Mode 不回退到这些组件。

## 2. HTTP 契约与接口作用

### 2.1 创建候选 Job

```text
POST /api/projects/{project_id}/tasks/{task_id}/products
body: { revision }
```

接口只接受当前 Task Revision。候选数量固定为 1–3，客户端不能提交 Prompt、模板、模型、
候选产品、产品事实、URL、对象 Key、Actor、Role 或 Provider 参数。成功返回安全 Job 摘要；
私有请求和固定证据身份只保存在服务器 Queue 内部。

### 2.2 查询候选 Job

```text
GET /api/projects/{project_id}/tasks/{task_id}/products/jobs/{job_id}
```

查询要求 `project.view`，并同时按 Organization、Project、Task、Operation 与 Job ID 限定。
公开 DTO 只返回稳定 ID、状态、Revision、Attempt、时间戳、取消标记和安全错误状态，不返回
Prompt 正文/Hash、产品 URL、Snapshot/Object 身份、私有 Request、Requester 或 Provider
原始错误。候选结果通过更新后的 Task 读取，不复制进公开 Job Error/Result。

### 2.3 人工确认

```text
PUT /api/projects/{project_id}/tasks/{task_id}/products
body: { revision, product_ids[1..3] }
```

同一路径按 Method 区分“生成候选”和“提交选择”。PUT 保持已有正式命令语义：重新读取每个
Product 的 Confirmed + Published Current `primary_detail` Evidence，从
`selection_projection v1` 投影完整 Task Product Snapshot，执行 Task Revision CAS、
`invalidate_downstream("products")` 与脱敏 Audit。PUT 不信任候选生成时的旧事实，也不要求
选择必须来自最近一次候选；候选只是建议，正式目录和人工判断才是选择准源。

## 3. 候选上下文和固定身份

候选上下文只能来自同一 Project 的正式产品证据，需同时满足：

1. `knowledge_products.status = confirmed`；
2. 存在角色为 `primary_detail` 的产品来源证据；
3. Evidence 所属 Source 为 `published`；
4. Evidence Snapshot 精确等于 Source 的 `current_snapshot_id`；
5. Evidence 包含有效 `selection_projection.schema_version = 1`；
6. 产品、Source、Snapshot 与 Evidence 全部属于当前 Organization/Project。

Inbox、Pending、Rejected、旧 Snapshot、跨项目产品、没有 Primary Detail 的产品以及可变
`knowledge_products.metadata` 均不得进入 Provider Context。图片 URL、对象 URI/Key、原始
网页地址和附件正文也不进入候选 Prompt。

Enqueue 必须固定以下不可漂移身份，并保存到私有 Job Request：

- Organization、Project、Task 与请求者身份；
- 当前 Task Revision；
- checked-in 产品候选模板 Hash；
- Server Product Provider 的模型身份；
- 每个允许产品的 Product ID、Source ID、Current Snapshot ID、Primary Detail Evidence
  身份和 `selection_projection v1` 内容摘要。

Handler 在 Provider 前重新加载并逐项匹配这些身份。Task Revision、模板、模型、产品状态、
Source 发布状态、Current Snapshot 指针或 Evidence Projection 任一漂移，Job 都进入统一安全
Conflict/Failed 结果，不以“最新值”继续执行。

## 4. Provider 契约

`backend/prompts/products.txt` 是 checked-in System Template；
`backend/services/server_product_generation.py` 提供 Server-only Provider、Enqueue、Handler、
Registry 与停止语义。Provider 只接收有界 Task 主题/已确认标题/关键词和上述安全产品投影，
只允许返回严格 JSON：

```json
{"product_ids": ["product-id-1", "product-id-2"]}
```

输出必须满足：

- 仅包含 1–3 个非空且互不重复的 Product ID；
- 每个 ID 都在本 Job 固定的允许集合内；
- 顺序即候选优先级，服务端保留该顺序；
- 不接受名称、理由、事实、URL、图片、Markdown、额外字段或自由文本；
- 不足、超量、重复、未知 ID、非法 JSON、空响应或 Provider 异常全部 fail closed；
- 不生成 mock，不回退 Local Customer Context、SQLite、Crawler 或本地 Artifact。

Provider 异常只映射为固定安全错误。原始错误、请求、响应、Prompt、证据正文和 Secret 不进入
HTTP、Job 公共 DTO、Audit 或普通日志。

## 5. 数据流和事务边界

```text
Browser
  -> POST .../products {revision}
  -> project.view + article.edit 预检
  -> PostgreSQL Enqueue 事务
       锁定 Actor/Organization/Project/可撤权 Membership
       锁定 Task Revision 与允许的正式产品证据
       固定 Template/Model/Product Evidence Bindings
       创建 Batch + products Job(requested_by_user_id)
       写安全 Enqueue Audit
  -> Project Registry / AuthorizedPostgresJobQueue
       Claim 前只读最小元数据并复核 article.edit
  -> Reauthorizing Handler
       Handler 前再次复核 article.edit
       重载并匹配 Task/Template/Model/Product Evidence Bindings
       调用 Provider，严格解析 1–3 个允许 Product ID
  -> PostgreSQL Commit 事务
       再次锁定可撤权授权与 Task Revision
       只替换 product_candidate_ids
       Task Revision CAS + article.products.generated Audit
       Job Terminal + background_job.terminal Audit 原子提交
  -> GET Task 展示可审阅候选
  -> 人工 PUT .../products 提交最终选择
```

候选提交成功只允许改变：

- `product_candidate_ids`；
- Task Revision 与更新时间；
- 与本次候选生成对应的安全 Audit/Job 终态。

以下字段必须保持原值：

- `task.products`；
- Workflow Status；
- 已确认标题、大纲、文章版本、Review、图片、DOCX、TDK 和 Delivery 引用；
- Published Knowledge 与产品目录。

当上游已确认标题发生变化、执行完全重写或人工 PUT 提交正式产品时，应清除旧
`product_candidate_ids`，防止旧建议跨语义边界继续显示。这个清理属于对应的 Task 命令，
而不是候选 Job 对下游执行失效。

## 6. 授权、取消和有界停机

- Enqueue 在业务事务中锁定 `article.edit` 的全部可撤权事实，Audit 失败不得留下 Job；
- Claim 前只读取最小 Queue 元数据完成第一次 Worker 授权，未授权时不得暴露私有 Request；
- Handler/Provider 前再次授权；提交前与 Task CAS 一起重新锁定授权事实；
- 取消在 Provider 前和提交前均有协作检查点；用户取消不得再写候选；
- 服务停止时 Registry 先停止新 Claim，再有界等待在途 Job；协作退出且没有用户取消请求的
  Job 释放为 `queued`，不得伪装成 `cancelled`；
- 有界等待后仍有在途 Job 时，Lifespan 必须报告未排空并阻止提前释放数据库 Engine；
- Job 终态与 `background_job.terminal` Audit 同事务，Audit 失败时终态回滚并等待重试。

`products` 进入 Project Job Control 后，列表、取消与重试沿用服务器保存命令的重放语义。
Retry 只允许空 Body，不能替换 Revision、Requester、Operation、Template、Model 或 Evidence
Bindings；Worker 重试时仍执行完整的两阶段授权和漂移检查。

## 7. 失败不变量

以下任一失败都不得修改 `task.products`、Workflow Status 或下游产物，也不得留下部分
`product_candidate_ids`：

- Viewer、跨 Project 请求、撤权或 Disabled User；
- Task Revision/CAS 冲突；
- 模板、模型或任一产品证据绑定漂移；
- 产品不再 Confirmed、Source 不再 Published、Snapshot 不再 Current；
- Provider 未配置、超时、异常、非法输出或返回非允许 ID；
- 取消、受控停机、Job Lease 丢失；
- Task、Job Terminal 或 Audit 提交故障。

候选生成失败时，旧候选可以按现有 Task 状态继续显示；它们只是建议，不构成正式选择。
人工 PUT 在提交时始终重新验证当前正式证据，因此旧候选不能绕过 Published Current 门禁。

## 8. 模块职责与未来重构接缝

| 模块/接缝 | 当前职责 | 可替换实现 | 必须保留的不变量 |
|---|---|---|---|
| `server_project_http.py` | 严格 POST/GET DTO、Scope 与权限预检 | 拆分 Product Router | Method/Path 白名单、Body 拒绝额外字段、安全 DTO |
| `server_product_generation.py` | 上下文固定、Provider、Enqueue、Handler、Registry、Stop | 拆为 Application Service + Dispatcher + Provider Adapter | 证据绑定、两阶段授权、只写候选、CAS/Audit、脱敏 |
| `products.txt` | checked-in 候选选择协议 | 版本化 Template Registry | 精确 Hash、仅输出 1–3 个允许 ID |
| Product Context Reader | SQL 内 Project Scope，读取正式产品投影 | 独立 Read Model/Repository | Confirmed + Published Current + Primary Detail + Projection v1 |
| `authorized_job_queue.py` | Claim 前最小授权 | 共享消息总线 Adapter | 未授权前不解封私有 Request |
| `server_job_control.py` | `products` 列表/取消/重试 | 通用 Control Plane | Server 保存命令重放、操作权限、脱敏公共 DTO |
| `server_task_commands.py` | 候选 CAS 提交和安全 Audit | 强类型 Task Aggregate | 只改候选；最终选择仍走独立 PUT |
| Server Article Workbench | 发起/轮询、展示建议、人工勾选 | 独立 Product Recommendation Panel | 建议不自动提交；Dirty/Conflict 不静默覆盖 |

前端推荐标记只能来自当前 Task 的 `product_candidate_ids` 与当前安全 Catalog 卡片的交集。
点击候选只更新浏览器中的未保存选择；仍须用户显式保存，后端 PUT 才会写正式产品。
Task 刷新时若服务器产品基线变化而本地选择为 Dirty，界面必须显示 Conflict，不得静默覆盖。
动态 Project/Task 切换必须重置候选轮询、产品选择草稿和 Rediscovery Category URL 草稿。

## 9. Capability 边界

`products` 完成上述接口、Worker、Job Control 和验证后，可在人工 Route/Operation Inventory
中标为 Server Ready；这只证明一个 Operation 的链路完整。它不能自动把
`project_routes_scoped`、`postgres_task_single_write`、`postgres_job_single_write` 或
`worker_reauthorizes` 改为 true，也不改变另外两项 Capability。M7 在全部 Route、Operation、
冻结窗口、真实排空、恢复、IdP 与 ObjectStore 证据完成前仍为 `no-go`。
