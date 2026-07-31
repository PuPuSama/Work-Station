# M7 Server Knowledge Research：结构与接口痕迹

## 1. 目的与范围

本文记录 Server Mode 资料研究链路的代码职责、事务边界、安全输出和后续重构接缝。
它用于后期重构导航，不替代数据库约束、RBAC 或部署 Runbook。

本切片完成：

- 从已确认的 PostgreSQL Task 大纲生成不可变 Retrieval Plan；
- 以真实 `article_tasks.task_id` 创建 PostgreSQL Batch/Job；
- 通过 Server-only Handler 执行 Research Graph Start/Resume；
- 用候选 ID 而不是 URL 作为浏览器审批契约；
- 把获批官网页面写入 Project-scoped S3、Knowledge Repository 和发布状态机；
- 在独立 Server Research 工作区展示 Run、候选、事件和 Evidence Pack；
- 保留 Local SQLite Research Queue、UI 和接口行为。

本切片不完成 WordPress Sync、Raw Artifact 下载、通用 Research Cancel/Retry、生产
Tavily/Embedding/S3 冒烟或完整 M7 上线。

## 2. 组件地图

| 组件 | 作用 | 不负责 |
|---|---|---|
| `server_knowledge_research.py` | Server Plan、Start/Resume、私有 Job、Worker 与 S3 入库装配 | HTTP DTO、前端状态 |
| `research_runs.py` | Run/Event/Gap Attempt PostgreSQL Repository 与事务内写入口 | 授权、对象存储 |
| `evidence_repository.py` | Retrieval Plan/Evidence Pack PostgreSQL Repository 与事务内写入口 | Task 大纲确认 |
| `research_adapters.py` | Discovery、Evidence、候选抓取/分类/发布适配 | Server Actor 来源 |
| `research_execution.py` | 打开 LangGraph PostgreSQL Checkpointer Session 并执行图 | 运行时 `setup()` |
| `authorized_job_queue.py` | Claim 前按 Operation 重新授权 | 业务 Plan/Run 校验 |
| `server_job_control.py` | 公共 Job 列表；对 Research 通用 Cancel/Retry fail closed | Resume 语义 |
| `knowledge_agent/http.py` | Local/Server 分流、安全 DTO、SSE 与候选 ID Resume | 私有 URL 写入 Audit |
| `server-research-workspace.tsx` | Server 研究 UI、SSE/轮询、候选审批、Evidence Pack | Local Queue、手工 URL |

`backend/app.py` 是运行模式装配点。Server Mode 只在 PostgreSQL Knowledge Runtime、
Embedding Provider、Tavily 和 S3 ObjectStore 均可用时构造 Research Execution；它不会
启动 SQLite Queue/Runner。进程关闭时先停止领取新 Research Job，并在关闭 Engine 前
排空。

## 3. Local 与 Server 的硬边界

```text
Local
  TaskStore(SQLite)
  -> Local Retrieval Plan compatibility
  -> Local Research Queue
  -> LocalKnowledgeArtifactStore

Server
  article_tasks(PostgreSQL)
  -> immutable Retrieval Plan(PostgreSQL)
  -> background_batches/background_jobs(PostgreSQL)
  -> Server-only Handler + PostgreSQL Checkpointer
  -> ScopedS3ArtifactStore
  -> Knowledge Source/Snapshot/Chunk(PostgreSQL)
```

Server 不读取 Local TaskStore、SQLite Research Queue、本地客户文件或本地 Artifact
目录；Server 失败也不回退 Local。浏览器不能提交 Organization、Requester、Task ID
替代值、URL、对象位置或 Worker Request。

## 4. Plan 创建

入口：

```text
POST /api/knowledge/{project}/tasks/{task_id}/retrieval-plan
```

同一路径保留 Local 兼容行为；Server 分支由 `ServerKnowledgeResearchRegistry` 读取当前
Actor Scope 下的 PostgreSQL Task，并要求：

- Task 属于 URL 中的 Project；
- 大纲已确认；
- 当前 `outline_version`、大纲 Hash 与 Task Revision 可确定；
- 每个 Scope 由服务端从确认大纲生成；
- Plan Metadata 标记服务端来源和真实 Task 身份。

事务顺序：

```text
lock Project authorization facts (knowledge.edit)
-> lock/read PostgreSQL Task
-> validate confirmed outline identity
-> insert immutable Retrieval Plan
-> append redacted retrieval_plan.created Audit
-> commit
```

Audit 失败时 Plan 一起回滚。通用
`POST /api/knowledge/{project}/retrieval-plans` 在 Server Mode 继续关闭，因为它允许
浏览器直接定义 Scope，不能证明来自已确认 Task。

## 5. Start 的原子边界

浏览器只提交 `retrieval_plan_id`、有界查询预算和一次请求内稳定的 `request_id`。
Organization 从已验证 Session 取得。

```text
POST /api/knowledge/{project}/research-runs
-> lock knowledge.publish facts
-> lock/read Plan + real PostgreSQL Task
-> revalidate Task revision / outline version / outline hash
-> create deterministic thread identity
-> insert Run(status=queued)
-> append Run queued Event
-> create Batch + private Job(operation=knowledge_research)
-> append redacted research.started Audit
-> one PostgreSQL commit
-> wake project runner
```

同一 Project、Plan、Actor 和 `request_id` 的网络重试返回同一个 Job。Job 的
`task_id` 必须是真实 `article_tasks.task_id`，不能使用 `research:{thread}` 伪身份，
因此复合外键、项目隔离和公共 Job Center 都继续成立。

私有 Job Request 包含执行所需的 Thread/Plan/Outline 身份和预算；公共 Job DTO 与 Audit
都不返回 Requester、URL、Prompt、Chunk、对象 URI 或原始异常。

## 6. Worker 与两阶段授权

```text
PostgresJobQueue claim
-> worker_permission_for(knowledge_research) = knowledge.publish
-> Claim 前重新读取 requester 的当前权限
-> ServerKnowledgeResearchHandler
-> 再次读取 Task / Plan / Run 身份
-> bind active requester to execution context
-> Start or Resume Research Graph
```

Research Graph 失败时先把 Run 和安全错误投影为终态；Handler 再把 Job 终止为
`conflict`。它不会让通用 Queue 自动重试终态 Checkpoint。业务继续只能创建新的 Resume
Job，避免把 Start/Resume 混成基础设施 Retry。

Server 候选入库在每个获批页面抓取前重新执行 `knowledge.publish`，且最终
Review/Publish 命令还会在自己的事务中重新锁定权限事实。权限撤销后不能继续抓取后续
候选，也不能激活来源。

## 7. Resume 与候选身份

浏览器读取的是有界候选 DTO：

```text
candidate_id
url
page_type
needs_review
evidence.reason / channel / same_site / score / reused_attempt
```

Resume 只提交：

```text
request_id
approved_candidate_ids[]
```

服务端在指定 Project/Thread/当前等待审批的 Gap Attempt 中把 Candidate ID 解析为私有
URL，再把 URL 放入私有 Job Request。未知、重复边界异常或不属于当前 Attempt 的 ID
返回冲突；Audit 只记录候选 ID 和数量。

每次 Resume 创建一个新 Job，而不是修改旧 Job 或调用 Retry：

```text
lock knowledge.publish facts
-> lock Run and validate waiting_for_review
-> resolve candidate IDs inside this Run
-> create new Batch/Job(action=resume)
-> append research.resumed Audit
-> commit
-> Handler validates checkpoint again
```

零选择是显式决定，表示不批准当前候选；UI 不把它伪装成取消。

## 8. 候选页面、对象与发布

```text
approved candidate ID
-> private URL resolution
-> trusted Actor + cancellation checkpoint
-> same-site URL normalization
-> CheckpointingOfficialSiteFetcher
-> OfficialWebPageIngestionService.prepare_url
-> ScopedS3ArtifactStore(org/project prefix + exact hashes)
-> PostgresServerWebEvidenceIngestion
-> one-page Source/Snapshot/Chunk/Product/Asset/Evidence + Audit transaction
-> deterministic confidence gate
-> cancellation check + audited Review
-> cancellation check + Embedding Prepare
-> audited Publish + current snapshot activation
```

低置信度候选保留在 Research Inbox，不自动发布。已发布且快照相同的重试复用现状。
Embedding、撤权或发布失败时旧 Current Snapshot 继续服务。

网络取数和对象 Put 不能伪装成 PostgreSQL 单事务。当前实现允许晚期失败留下可对账的
内容寻址对象，或已入 Inbox 但未发布的不可变证据；不会把它们当成 Published Current
Evidence。

受控取消从 Candidate Checkpoint 原样穿过 LangGraph Execution：Run 投影恢复到本次执行前
的可重试状态并追加 `interrupted` Event，不会写成 terminal `failed`；随后 `JobCancelled`
继续交给 Batch Runner 的 interrupted/requeue 机制。Provider 或业务异常仍转换为脱敏的
`ResearchExecutionError`，不能借取消通道绕过失败记录。

## 9. HTTP 公共/私有字段

| 位置 | 允许 | 禁止 |
|---|---|---|
| Run 列表/详情 | ID、状态、预算、计数、时间、Warnings | Worker Request、Requester、Secret |
| Event | 类型、序号、有界状态字段和计数 | 原始 URL、Query、Provider 正文、异常堆栈 |
| Gap Attempt | Attempt ID、Scope、Round、结果、候选 ID | 私有发现 URL、原始 Query |
| Candidate Review | Candidate ID、标题、审核所需 URL/摘要/分数 | Bucket、Object Key、Secret |
| Evidence Pack | Evidence ID、引用、摘要、分数 | Raw Artifact、内部 URI |
| Audit | 稳定 ID、Action、数量、预算 | URL、正文、Prompt、Hash、Provider 错误 |

Server List/Detail/SSE 都按当前 Actor Organization 和 URL Project 过滤。前端 Role
只控制提示和按钮可见性；后端仍逐请求和逐事务授权。

## 10. 前端数据流

```text
ProjectKnowledgeWorkspace
-> Auth Status
   -> Local: existing Library + Research + Evidence
   -> Server: ServerKnowledgeWorkspace
              -> 来源 Inbox
              -> 资料研究
                  -> select confirmed Task
                  -> create immutable Plan
                  -> start Run
                  -> ?tab=research&thread={thread_id}
                  -> SSE cursor
                  -> 3-second polling fallback
                  -> approve candidate IDs
                  -> Resume
                  -> Evidence Pack
```

URL 中只保存 Tab 和 Thread ID，不保存候选 URL、正文或 Secret。SSE 断开时轮询公共
Run DTO；页面重载可由 Thread ID 恢复当前研究上下文。

`knowledge_research` Job 在 Batch/Header 中可见，但链接回 Knowledge Research 页面。
通用 Cancel/Retry 按钮不显示，后端接口也返回冲突，避免 UI 约定成为唯一门禁。

## 11. 失败语义

| 失败点 | 结果 |
|---|---|
| Plan/Start/Resume Audit 失败 | 同事务 Plan/Run/Event/Batch/Job 全部回滚 |
| Task Revision/Outline 漂移 | 不创建或不执行 Job，返回冲突 |
| Claim 前或 Handler 前撤权 | Job 不执行或终止冲突 |
| 候选循环中撤权 | 后续页面不抓取，来源不发布 |
| Discovery/Provider/Graph 失败 | Run 保存脱敏终态，Job 不自动重放 |
| Embedding/Publish 失败 | 旧 Current Snapshot 继续服务 |
| SSE 失败 | UI 使用有界轮询，不切换 Local |
| 通用 Cancel/Retry | 前后端均 fail closed，要求业务 Resume 语义 |

## 12. 后续重构接缝

以下是有意保留的结构痕迹，不应在无等价约束时“简化”：

1. 增加 `server_research_job_links`，把 `request_id/action/thread_id/job_id` 作为耐久
   关系，进一步强化跨进程幂等、领域取消和可观测性。
2. Start 实际只读已发布证据，未来可把权限拆为 `knowledge.research`；Resume 的抓取/
   发布继续要求 `knowledge.publish`。当前统一用保守的 `knowledge.publish`。
3. Web Ingestion 已拆为显式 Prepare/Commit，当前结构与限制见
   `docs/architecture/m7-server-web-evidence-ingestion.md`；下一步是 Snapshot-bound Review
   Receipt，使新版本 Review/Embedding/Activate 绑定精确 Snapshot。
4. 用显式 `ResearchExecutionContext(actor, scope, cancelled)` 替换当前承载 Actor 与取消
   Callback 的 `_ACTIVE_RESEARCH_EXECUTION` ContextVar，让 Execution 生命周期
   可测试并适合跨进程 Worker。
5. 抽取前端 `useResearchRunStream`，统一 SSE Cursor、重连退避和轮询恢复。
6. Inbox 与 Research 共用 Project Role/Capability Query，避免重复读取但仍不缓存为
   授权准源。
7. 若未来允许跨 Organization 重复 `project_id`，所有 Run/Plan/Candidate 查询需把
   Organization 变成数据库复合身份，而不是只在 HTTP 投影层复核。

## 13. 回归不变量

- Server 不构造或启动 SQLite Research Queue；
- Plan 只能来自当前已确认的 PostgreSQL Task 大纲；
- Job 使用真实 Task ID，私有 Request 不进入公共 DTO/Audit；
- Start/Resume 创建与 Audit 同事务；
- Resume 只接受 Candidate ID，并创建新 Job；
- Claim、Handler、每候选抓取和最终发布都有对应授权复核；
- Research Job 不走通用 Cancel/Retry 或自动基础设施重放；
- 候选对象只写 Project-scoped S3，不写本地 Artifact；
- Local UI/API/Queue 行为保持不变；
- 通用 WordPress Sync HTTP 与 Raw Artifact HTTP 在 Server Mode 继续关闭；受控 Product
  Rediscovery/Research 只通过 Server Web Evidence Unit of Work 使用内部页面准备能力。
