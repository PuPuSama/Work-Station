# M7 Server Knowledge 写边界收口

## 1. 结论

M3/M5 的通用 Knowledge API 证明了 Retrieval Plan、Evidence Pack、Evidence Link 和
Research Chat 的领域模型可用，但“数据写进 PostgreSQL”不等于“已经完成 M7 Server
迁移”。Server Mode 只开放具备可信 Actor、Project/Task 身份、提交事务内复核和安全
Audit 的窄命令；其余兼容接口继续保留给 Local Mode。

本切片不删除旧能力，也不改变 Local 数据。它只修正 Server Mode 的公开边界：

- 旧 Research Chat 写入 fail closed；
- 通用 Evidence Pack Build fail closed；
- 客户端自定义 Evidence Link 写入 fail closed；
- Evidence Link Stale Review 写入 fail closed；
- Server Retrieval Plan 读取只展示由已确认 PostgreSQL Task 大纲生成的 Plan；
- Server Knowledge Library 不再返回必然被安全门拒绝的 Raw Artifact URL。

## 2. 为什么这些路由不能继续标为 Server Ready

以下通用写模型缺少 M7 的关键身份：

| 数据 | 当前通用身份 | Server 缺口 |
|---|---|---|
| Research Conversation | Project、Conversation、可选 Article | Organization、Actor、真实 Task、Task Revision、Audit |
| Evidence Pack | Project、Plan、Scope | Organization、真实 Task、Task Revision、生成 Job、Audit |
| Evidence Link | Project、调用方提供的 Article/Paragraph/Chunk | PostgreSQL Task FK、Article Version Hash、可信 Citation 投影、Audit |
| Stale Review | 调用方提供的 Article/Paragraph Hash | 当前 Task Article Version、Revision CAS、事务内撤权复核、Audit |

这些接口即使在请求开始时经过 Project RBAC，提交时仍没有重新锁定可撤权事实，也无法把
业务写入与 append-only Audit 放在同一事务。因此 Server Mode 的正确语义是明确 503，
而不是以较弱约束继续写入。

`POST .../knowledge-coverage` 是纯计算接口：它读取当前 Project 的 Evidence Link 并返回
覆盖率，不修改状态，所以仍可使用 `project.view`。读取 Evidence Pack、Evidence Link
和 Research Conversation 也暂时保持 Project-scoped 只读；后续若专用 Server 写模型
改变公开身份，应一并收紧对应读取投影。

## 3. 请求路径

```text
Server Knowledge request
  -> server_http_route_available: 只允许 /api/knowledge/{project}/...
  -> require_knowledge_project_access
       -> 验证 Server Actor Session + Session Version
       -> SQL 读取 Organization/Project 权限
       -> server_knowledge_route_ready
            -> 已迁移窄命令：继续
            -> 未迁移通用写：503，业务 Repository 不执行
  -> Route Handler
```

`server_knowledge_route_ready` 是部署边界，不是权限替代品。未来开放某条写路径时，必须先
新增专用 Server Command/Job，再删除对应 fail-closed 条件；不得因为通用 Repository 已
支持 PostgreSQL 就直接放行。

## 4. Retrieval Plan 可见性

Server Plan 的来源标记为：

```json
{
  "generated_from": "confirmed_task_outline",
  "task_id": "server-owned-task-id",
  "outline_hash": "sha256"
}
```

列表与单条读取先使用该标记隐藏旧 Local/客户端 Plan。这个标记只控制读模型，不是执行
授权。Start/Resume 在创建 Job 前仍必须重新读取 PostgreSQL Task，并验证：

- Task 属于当前 Organization/Project；
- Task 已确认大纲；
- Article ID、Task ID、Outline Version 和 Outline Hash 全部匹配；
- `knowledge.publish` 在事务内仍然有效。

这样避免把“列表可见”误当成“可执行”。后续可把 Plan 增加真实 Task 复合外键和
Organization 身份；在 Schema 完成前，不把 Metadata 标记提升为准源。

## 5. Raw Artifact 投影

Local Raw Artifact Handler 解析本地 Artifact URI；Server 上传则使用 Project-scoped
ObjectStore，而且 Raw/Normalized 对象尚未接入通用 `knowledge_assets` 下载准源。因此
Server Library 返回 `raw_evidence_url = null`，前端不会渲染一个必然 503 的入口。

以后开放 Raw Artifact 必须新增独立 Asset 身份与授权下载命令，复核 Organization、
Project、Source 和 Snapshot 的复合归属；不能让浏览器接收 Object URI/Key，也不能复用
Local 文件路径 Handler。

## 6. 后续专用命令的重构接缝

按业务价值逐项开放，不恢复通用写接口：

1. `ServerEvidencePackJob`：固定 Task Revision、Plan/Scope、Embedding Model 和当前
   Published Chunk ID，成功后原子写 Pack 与 Audit。
2. `ServerEvidenceLinkCommand`：只接受服务端候选 ID/Paragraph Identity，不接受调用方
   自定义 Citation URL、Validation Status 或 Metadata。
3. `ServerEvidenceReviewCommand`：绑定当前 Article Version Hash 与 Task Revision，以
   CAS + Audit 标记失效引用。
4. `ServerResearchAssistantJob`：绑定 Actor、Task/Article Version、Published Chunk ID，
   Provider 错误脱敏，Conversation/Message/Audit 原子提交。
5. `ServerRawArtifactDownload`：使用私有 Asset ID 和短期下载授权，不公开存储身份。

Web 页面入库还有一个独立原子性缺口：Product Rediscovery 与 Research Candidate 当前
复用多事务 M2 Ingestion。后续应拆成无数据库副作用的 Prepare，以及在来源级锁、事务内
复核权限后一次提交 Source/Snapshot/Chunk/Product/Asset/Evidence/Audit 的 Commit。该工作
不能与本次“关闭错误公开面”混写成已完成。

## 7. 验收契约

- Server Mode 上述四类 POST 在解析业务 Body 或调用 Provider/Repository 前返回 503；
- Local Mode 路由和现有 M3/M5 测试不变；
- Server Plan 列表/单条读取不展示通用客户端 Plan；
- Plan 来源标记不能替代执行期 Task/Outline/RBAC 校验；
- Server Knowledge Library 的 `raw_evidence_url` 为 `null`，Local 仍可读取原始证据；
- 未新增迁移、密钥、外部 API 调用或 Local SQLite 写入。
