# M7 Server 私有资料上传结构记录

> 状态：Server Narrow。本文记录代码职责、事务边界和后续重构接缝。
> 运行时准源仍是路由依赖、`PostgresServerPrivateDocumentIngestion` 与数据库约束，
> 本文不替代授权或 Schema。

## 1. 本切片解决的问题

Local Knowledge Upload 已能解析 DOCX/PDF/XLSX/XLSM，但它把产物写入本地目录，并让
Source、Snapshot、Chunk、Asset 和 Link 分多次提交。Server Mode 不能复用该持久化路径，
否则会出现本地文件依赖、跨事务半成品和 Router 授权后撤权仍继续提交的问题。

Server 路径继续使用同一个 HTTP 契约：

```text
POST /api/knowledge/{project}/sources/upload
multipart/form-data:
  file          required, DOCX/PDF/XLSX/XLSM, <= 25 MiB
  source_id     optional, otherwise server derives retry-stable opaque ID
  display_name  optional, <= 255 normalized characters
  trust_tier    hard_fact | reference_material | writing_instruction
```

成功始终返回 `201`。`created=true` 表示创建了新 Snapshot 和成功 Audit；
完全相同的不可变重试返回 `created=false`，不重复追加成功 Audit。响应只含 Project、
Source/Snapshot ID、Parser、Chunk/Asset 数量和通用消息，不返回文件名、正文、Hash、
Bucket、Object Key、Artifact URI 或底层 Provider 错误。

## 2. 代码职责地图

| 文件 | 作用 | 不负责 |
|---|---|---|
| `backend/knowledge_agent/http.py` | Multipart 大小门禁、Local/Server 分流、公开错误和最小 DTO | 不直接写数据库或 S3 |
| `backend/services/server_private_document_ingestion.py` | Server 两阶段编排、双重授权、Source 串行化、原子数据库/Audit 提交 | 不实现具体解析器或对象 Provider |
| `backend/knowledge_agent/ingestion/service.py` | `prepare()` 解析并生成 Source/Snapshot/Chunk/Asset 契约；`ingest()` 保留 Local 兼容持久化 | 不做 Server RBAC |
| `backend/knowledge_agent/object_storage.py` | 把 M2 `ArtifactStore` 绑定到一个 Organization/Project 的内容寻址 S3 前缀 | 不信任 Parser 传入的 Project |
| `backend/knowledge_agent/repository.py` | Snapshot/Chunk 不可变写入与完全重试验证；提供 caller-owned transaction 入口 | 不决定授权和 Audit |
| `backend/knowledge_agent/assets.py` | Asset 去重、Snapshot Evidence Link 与 caller-owned transaction 入口 | 不决定显示/发布状态 |
| `backend/services/server_request_security.py` | Router 层 Cookie、Project Scope、`knowledge.edit` 初次授权和路由白名单 | 不替代业务事务内重验 |
| `frontend/src/components/server-private-document-upload.tsx` | 文件、名称、Trust Tier、25 MiB 客户端提示、加载/错误/只读状态 | 不生成 Source/Snapshot 身份 |
| `frontend/src/components/server-knowledge-inbox.tsx` | Server Inbox 组合、上传成功反馈与 Library 刷新 | 不挂 Local WordPress/Research/Raw Evidence |

`backend/app.py` 只在 Server ObjectStore 已配置时构造
`PostgresServerPrivateDocumentIngestion`。缺少对象存储时路由返回脱敏 503，不回退到
Local ArtifactStore。

## 3. 两阶段数据流

```text
Router dependency
  -> 验签 Server Session
  -> SQL-scoped project + knowledge.edit
  -> 读取 <= 25 MiB multipart

Phase 1: prepare
  -> 再次读取当前权限，拒绝无权 Actor 后才允许对象写
  -> Parser Router 解析文档
  -> Chunker 生成 snapshot-prefixed Chunk ID
  -> ScopedS3ArtifactStore 写 raw / normalized / embedded assets
  -> 仅返回待提交的不可变契约，不写业务表

Phase 2: commit
  -> 开启 PostgreSQL transaction
  -> 锁 Actor/User/Org/Project/Membership/Team 等可撤权事实
  -> 再判 knowledge.edit
  -> 锁 Active Project
  -> 以 org + project + source 取得 transaction advisory lock
  -> upsert Source（仅新 Snapshot）
  -> insert/verify Snapshot + Chunks
  -> insert/reuse Assets，并把 Link 映射到数据库实际保留的 Asset ID
  -> insert/verify SnapshotAsset links
  -> append redacted knowledge.source.uploaded Audit
  -> 一次提交
```

外部对象上传不能成为 PostgreSQL 事务的一部分。Phase 2 拒绝或失败时，数据库与 Audit
全部回滚；Phase 1 已写的内容寻址对象可能成为 orphan，由现有延迟对账和安全年龄门清理。
不得在请求失败路径立即删除对象，因为并发请求可能已复用相同内容。

## 4. 核心不变量

1. Server Mode 永不写本地 ArtifactStore，也不启动 SQLite Queue。
2. Router 授权只是第一次门禁；对象写前和数据库提交内都重新读取权限。
3. Source、Snapshot、Chunk、Asset Link 和 Audit 要么全部可见，要么全部不可见。
4. Snapshot/Chunk/Asset 是不可变身份；相同重试必须逐字段验证，冲突统一 409。
5. 同一 Source 的并发提交串行化，避免旧重试改写 Source Metadata。
6. 已存在同 Hash Asset 可以保留历史 Asset ID；Snapshot Link 必须使用 Repository
   实际返回的 ID，不能假设 Parser 计算 ID 一定是准源。该历史 Asset 还必须属于当前
   Server Bucket、Organization 和 Project；`file://` 或错误 S3 Scope 一律冲突。
7. 上传只产生 `inbox` Source，`current_snapshot_id` 仍为空；Review、Embedding 和
   Publish 是后续独立命令，旧 Published Snapshot 不受上传失败影响。
8. Audit 只保存 Snapshot ID、Parser 身份和数量；不保存文件名、正文、URL、Hash、
   Artifact URI、Reason 或 Secret。
9. Viewer/Reviewer 在 Server UI 中可看 Inbox，但上传控件禁用；后端仍以实时权限为准。
10. Local `PrivateDocumentIngestionService.ingest()` 接口与旧 HTTP 路径保持兼容。

## 5. 失败语义

| 失败点 | 公开结果 | 数据库 | 对象存储 |
|---|---|---|---|
| 文件为空/过大/格式不支持 | 413/422 | 不变 | 不写 |
| 初次 `knowledge.edit` 拒绝 | 403 | 不变 | 不写 |
| Parser/ObjectStore 失败 | 脱敏 422/503 | 不变 | 可能已有同请求前序对象 |
| Phase 1 后权限撤销 | 403 | 不变 | 保留 orphan 候选 |
| 不可变身份冲突 | 通用 409 | 整笔回滚 | 保留内容寻址对象 |
| 复用 Published Source ID 上传新内容 | 通用 409 | 旧 Source/Snapshot 不变 | 保留 orphan 候选 |
| 去重 Asset 属于 Local/错误 S3 Scope | 通用 409 | 整笔回滚 | 新正确对象保留 orphan 候选 |
| Audit/数据库不可用 | 通用 503 | 整笔回滚 | 保留 orphan 候选 |
| 完全相同重试 | 201 + `created=false` | 不重复写成功事实 | Provider 可幂等覆盖同 Key |

## 6. 后续重构接缝

- `prepare()` 目前同步解析并把最多 25 MiB 文件放在请求内存；若真实 PDF/XLSX 延迟或
  并发规模超出预算，应把“Upload Receipt + Background Parse”作为新 Job，而不是在
  当前 Handler 内偷偷异步。
- 默认 Source ID 由文件名和内容生成，保证同一上传重试稳定；如果产品需要“同一逻辑
  Source 的新版本”，必须先引入 Snapshot 级 Pending/Review 状态。当前 Source 级审阅
  无法同时表达“旧 Snapshot 已发布、新 Snapshot 待审”，因此显式复用已有 Published
  Source ID 提交新内容会返回 409；默认 UI 会为不同内容生成新的 Source ID。
- Raw Artifact 下载在 Server 仍关闭。开放时必须复用 Project Scope、Bucket/Key 前缀
  复核和短期签名，不可直接暴露 `raw_artifact_uri`。
- `knowledge_research` 可以把已审阅/发布的当前 Snapshot 当输入，但不能绕过 Inbox
  Publication Gate，也不能直接读取 Phase 1 orphan。
- 如果以后把 Asset、Snapshot 和 Link 写入合并成 Unit of Work，应保留 caller-owned
  transaction 接口和“实际 Asset ID 回映射”测试。
- Object orphan reconciliation 是本流程的补偿事务；修改 Key 结构或引用表时必须同步
  更新其引用扫描、最小年龄和 no-delete 门禁。

## 7. 验证入口

专用测试：

```powershell
$env:ARTICLE_AGENT_DATABASE_URL = '<由安全环境注入>'
.\backend\.venv\Scripts\python.exe -m unittest `
  backend.tests.test_m7_server_private_document_ingestion `
  backend.tests.test_knowledge_agent_m2_ingestion `
  backend.tests.test_knowledge_agent_m2_http -v
```

其中必须保留：相同重试、Viewer 零对象写、对象写后撤权、Audit 回滚、错误脱敏、
跨 Project Scope、Published Source ID 新内容拒绝、历史 Asset ID 去重回映射、
Local/错误 S3 Scope 拒绝，以及 Local M2 兼容回归。
