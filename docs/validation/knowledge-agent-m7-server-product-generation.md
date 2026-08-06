# Knowledge Agent M7：Server 产品候选生成验证记录

## 1. 当前结论

本文是产品候选生成切片的验证计划与证据位置，不是通过声明。

截至本文创建时：

- 架构契约和验证矩阵已记录；
- 本切片的整体后端回归、前端 Lint/Build 和真实 PostgreSQL/pgvector 集成验证尚未运行；
- `product_generation_smoke` 与 `product_selection_smoke` 尚无受控环境 Artifact；
- 所有未填 Evidence 位均视为未完成，M7 发布结论保持 `no-go`。

后续只在整个切片代码、前端和文档收口后运行一次整体回归；若某个命令因环境问题未执行，
只补跑该未执行部分，不重复已经通过的整套测试。

## 2. 验证准源

| 证据层 | 权威位置 | 证明范围 |
|---|---|---|
| 架构契约 | `docs/architecture/m7-server-product-generation.md` | 三条产品边界、接口、数据流、失败不变量、重构接缝 |
| Route/Operation Inventory | `docs/architecture/m7-server-route-migration-matrix.md` | `products` 路由、权限、存储准源和 Job Control 状态 |
| 后端自动测试 | `backend/tests/` 中产品生成、Task Command、Job Control、Route Security 测试 | DTO、Scope、事务、授权、CAS、Audit、取消/重试和安全输出 |
| 前端静态验证 | `frontend` ESLint 与 Next.js Production Build | 类型、路由调用、组件边界和构建完整性 |
| 受控环境冒烟 | 发布系统保存的 Artifact ID/Digest | PostgreSQL Worker、Provider、权限撤销、停机排空和人工选择真实闭环 |
| 发布门禁 | Preflight Report + 签名 Recovery Evidence | 整体发布条件；单个产品 Operation 不能代替其他门禁 |

## 3. 后端自动验证计划

### 3.1 HTTP 与 DTO

- `POST /api/projects/{project}/tasks/{task}/products` 只接受 `{revision}`；额外 Prompt、模型、
  Product ID、URL、Actor、Role、对象身份或 Provider 参数返回 422；
- `GET .../products/jobs/{job_id}` 只读取相同 Organization/Project/Task 的 `products` Job；
- Viewer 和跨项目 POST 返回 403；Job 查询不泄露其他项目对象；
- 公开 Job DTO 不包含私有 Request、Requester、Prompt/Template Hash、Model、Evidence
  Binding、URL、对象 URI/Key 或 Provider 原始错误；
- 同一路径现有 `PUT .../products` 契约保持 `{revision, product_ids[1..3]}`，不接受产品事实。

### 3.2 正式产品上下文

构造同项目与跨项目产品，分别覆盖：

- Confirmed + Published Current + `primary_detail` + `selection_projection v1` 可进入上下文；
- Inbox/Pending/Rejected/旧 Snapshot、非 Current Evidence、非 Confirmed Product、缺少
  Primary Detail 或 Projection v1 的 Product 均被排除；
- Project B 中更相关的 Product 也不得出现在 Project A 的允许集合或 Prompt；
- 上下文按稳定 Product ID 顺序和有界数量生成，不读取可变 `knowledge_products.metadata`；
- Provider 输入不包含网页 URL、图片 URL、对象 Key/URI、Secret 或附件正文。

### 3.3 Enqueue 原子性与固定身份

- 入队固定 Task Revision、Template Hash、Model Identity 和所有 Product/Source/Snapshot/
  Evidence Projection Binding；
- 可撤权授权事实、Task Revision 锁、Batch、Job、Requester 和安全 Audit 在一个事务；
- Audit 注入失败时不留下 Batch/Job；
- 旧 Revision 不创建 Job；并发相同请求不绕过单任务 Active Job 约束；
- Audit 只记录稳定 ID、Revision、候选数量或状态，不包含 Prompt、产品事实和私有绑定。

### 3.4 Worker、Provider 与漂移拒绝

- Claim 前和 Handler/Provider 前都重新要求 `article.edit`；入队后撤权时 Provider 不被调用；
- Handler 重载 Task、Template、Model 与 Product Evidence Binding，任一漂移都在 Provider 或
  Commit 前进入安全 Conflict/Failed；
- Provider 只接受安全有界 Context，只返回严格 JSON `product_ids`；
- 空输出、非法 JSON、额外字段、0 个、4 个、重复或允许集合外 ID 全部失败；
- Provider 未配置或抛出包含 Secret 的异常时，HTTP、Job、Audit 和日志只出现固定安全错误；
- 不回退 mock、Local Crawler、Tavily、SQLite、Customer Context 或文件 Artifact。

### 3.5 Task 提交不变量

- 成功后只按 Provider 顺序写 1–3 个 `product_candidate_ids`，Task Revision 加一；
- `task.products`、Workflow Status、标题、大纲、文章版本、Review、图片、DOCX、TDK 和
  Delivery 引用保持逐字段不变；
- Candidate Commit 与 `article.products.generated` Audit、Job Terminal 与
  `background_job.terminal` Audit 各自保持原子性；
- Task CAS、撤权、Lease 丢失或 Audit 失败不留下部分候选，也不重复追加成功 Audit；
- 选择标题、完全重写或人工 PUT 产品后清除旧候选；候选生成自身不调用下游失效；
- PUT 在提交时重新读取当前正式 Evidence；生成后 Snapshot 漂移时，旧候选不能绕过 PUT
  的 Published Current 门禁。

### 3.6 Cancel、Retry、Job Control 与停机

- `products` 出现在 Project-scoped Batch/Job Control，Viewer 可读但不能 Cancel/Retry；
- Cancel/Retry 权限映射为 `article.edit`，撤权后实时失败；
- Retry 只接受空 Body，并重放服务器保存的同一 Revision、Requester、Template、Model 与
  Evidence Binding；任何覆盖字段返回 422；
- Provider 前或 Commit 前取消均不写候选；Running Job 先标记 Cancel Requested；
- 停止时不再 Claim，新 Job 保持 queued；协作退出且没有用户取消的 Job 释放回 queued；
- 有界 drain 后仍有 Job 时报告 `remaining_jobs > 0`，不得提前释放 Engine；
- Terminal Audit 失败时 Job 终态回滚，等待恢复重试。

### 3.7 Local/Server 隔离回归

- Server Mode 不构造或调用 SQLite Product Job/Local Runner；
- 旧 Local `/api/tasks/{task}/generate/products`（以实际旧路由为准）仍使用原 SQLite/文件流，
  不读取 Server PostgreSQL Job；
- Local Mode 不新增 Project-scoped Server Router 的隐式兼容路径；
- `rewrite_article` 等未迁移 Operation 继续不进入 Server Job Control。

## 4. 前端验证计划

- 产品生成按钮只提交当前 Revision，并通过通用 Server Job 轮询读取安全状态；
- 推荐标记来自 `task.product_candidate_ids` 与当前安全 Catalog Product ID 的交集；
- 点击推荐只改变本地未保存选择，不能自动调用 PUT；
- 有未保存产品选择或 Conflict 时禁止开始新的 `products` Job；
- Task Revision 因其他安全操作变化但服务器产品基线未变时，保留本地 Dirty 草稿；
- 服务器产品基线变化且本地草稿 Dirty 时显示 Conflict，不静默覆盖；
- Project/Task 动态路由切换时重置候选轮询、选择草稿和 Rediscovery Category URL；
- Local Article Workbench、Local Product 流和 SQLite API 保持原组件树；
- 前端响应类型中不新增 Prompt、URL、对象 Key、Evidence Binding 或 Provider Error 字段。

## 5. 受控环境冒烟

### 5.1 `product_generation_smoke`

在正式候选 Commit、真实 Server PostgreSQL/pgvector、受控 Provider 与 Project-scoped
ObjectStore 配置完成后执行，至少保存：

```text
artifact_id:
release_commit:
project_id_digest:
task_id_digest:
job_id_digest:
template_hash:
model_identity_digest:
allowed_product_count:
candidate_count:
candidate_ids_digest:
claim_reauthorization:
handler_reauthorization:
revocation_case:
binding_drift_case:
public_dto_secret_scan:
drain_report_artifact:
result: pass | fail
```

Artifact 只保存稳定 ID 的受控 Digest、数量和布尔判断，不复制 Prompt、产品事实、URL、
对象 URI/Key、Requester、Token、Secret 或 Provider 原始响应。至少证明成功只写候选，
以及撤权、Evidence 漂移和取消时 `task.products/status/downstream` 全部保持不变。

### 5.2 `product_selection_smoke`

候选生成冒烟不能代替人工提交冒烟。使用现有 PUT 独立验证：

```text
artifact_id:
release_commit:
project_id_digest:
task_id_digest:
selected_product_count:
published_current_projection_verified:
candidate_advisory_only_verified:
task_products_updated:
downstream_invalidated:
cross_project_rejected:
stale_revision_rejected:
public_dto_secret_scan:
result: pass | fail
```

至少证明 PUT 重新验证 Confirmed + Published Current Primary Detail Projection v1，只把正式
服务端投影写入 `task.products` 并使下游失效；不要求选择等于最近候选，但不允许调用方提交
任何产品事实或 URL。

## 6. 整体回归与证据位

| Evidence | 当前值 | 通过标准 |
|---|---|---|
| `backend_regression` | `RECOVERED` | 单次完整回归运行 776 项、3 个新增测试报错；修复后只定向重跑新增文件，13/13 通过，未重复完整回归 |
| `frontend_lint` | `PASS` | 本地 ESLint CLI 退出码 0，56.4 秒 |
| `frontend_build` | `PASS` | Next.js 16.2.10 Production Build 退出码 0，36.3 秒 |
| `product_generation_smoke` | `PENDING` | 受控 Artifact 存在且结果为 pass |
| `product_selection_smoke` | `PENDING` | 与生成分开的受控 Artifact 存在且结果为 pass |
| `worker_drain_report_artifact` | `PENDING` | `products` 无在途 Job 或明确 no-go |
| `route_inventory_digest` | `PENDING` | 新 POST/GET/PUT 路由已纳入 Candidate Commit Inventory |
| `operation_inventory_digest` | `PENDING` | `products` Enqueue 到 drain 全链身份与状态已纳入 Inventory |
| `staged_secret_scan` | `PASS` | 精确暂存的 20 个文件通过高置信 Secret/Token/Private Key 扫描 |

执行时间：`2026-08-06T07:17:14Z`。完整后端回归共运行 776 项，除 3 个本切片新增测试的
错误外，其余结果完成；错误分别是测试 Fixture 缺少 permission，以及两处私有模板/Prompt
异常未转换为稳定公共错误。修复后只运行 `backend/tests/test_m7_server_product_generation.py`，
13 项全部通过。遵守“整体回归只跑一次”约束，没有伪称修复后又运行了第二次完整套件。
前端通过已安装的本地 ESLint/Next CLI 执行，未安装依赖、未修改 Lockfile。

填写时必须记录实际命令、UTC 时间、退出码、测试数量和 Artifact ID/Digest；不得用“代码存在”
替代运行证据，也不得因单个产品 Operation 通过而修改六项 Capability 或把发布结论改为 go。

## 7. 待验证风险

1. Task JSONB 兼容模型的新增候选字段是否被所有读取/序列化路径保留；
2. 产品选择 Dirty/Conflict 与其他 Job 导致的 Revision 变化是否会产生误覆盖；
3. Evidence Binding 数量上限和 Provider Context 长度是否在大目录项目中仍保持有界；
4. Cancel 恰好发生在 Provider 返回和 Task Commit 之间时是否仍无候选部分写入；
5. Retry 是否严格复用旧 Evidence Binding，而不是无提示切到最新目录；
6. `products` 加入 Job Control 后是否影响既有 Operation 的权限映射与公开 DTO；
7. 真实 Provider 的错误响应是否可能经第三方 SDK 字符串进入日志。

这些风险在对应测试或受控 Artifact 出现前均保持开放，不得从文档推断为已通过。
