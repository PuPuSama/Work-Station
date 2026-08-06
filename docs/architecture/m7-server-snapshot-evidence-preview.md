# M7 Server Snapshot Evidence Preview v1

## 1. 目的与边界

Snapshot Review Receipt 已把审核和发布绑定到精确、不可变的 Snapshot，但上一切片的
Server Inbox 只能展示 Snapshot ID、Chunk/Asset 数量和审核状态，操作者无法查看自己正在
审批的字节。本切片补齐以下闭环：

```text
查看精确 Pending 的规范化证据
-> 下载同一个 Pending 的原始证据
-> 为同一个 Pending 写 Review Receipt
-> 发布同一个 Pending
```

本能力只开放 Source 当前的 `current_snapshot_id` 和 `pending_snapshot_id`。Rejected、普通
历史 Snapshot 和“Latest”猜测都不属于 v1。它不新增表或迁移，也不把 Raw/Normalized
Artifact 伪装成 `knowledge_assets`。

## 2. 接口作用

Server-only 路径：

```text
GET  /api/knowledge/{project}/sources/{source}/snapshots/{snapshot}/evidence
GET  /api/knowledge/{project}/sources/{source}/snapshots/{snapshot}/evidence/preview
POST /api/knowledge/{project}/sources/{source}/snapshots/{snapshot}/evidence/raw-download
```

`evidence` 返回安全 Manifest：精确身份、`current | pending` Slot、Raw/Normalized 是否存在、
安全 Content-Type、Byte Size 和是否支持规范化预览。它不返回 S3 URI、Bucket、Object Key、
Hash、ETag、原文件路径、Canonical URL、Receipt Reason 或供应商正文。

`evidence/preview` 返回从受限 JSON 中提取的 Title 和 Block Text、Block Count 与 Truncated
标记。它不把完整 Normalized JSON、图片来源 URL、Parser Metadata 或 HTML 交给浏览器。

`evidence/raw-download` 使用空 Body，返回固定 60 秒的下载 URL。签名强制：

```text
Content-Type: application/octet-stream
Content-Disposition: attachment; filename="snapshot-evidence.bin"
```

因此 HTML、SVG 或未知二进制不能以原 Content-Type 在 Article Agent 页面上下文内执行。
签名 URL 只存在于本次响应和前端函数局部变量，不进入 Library DTO、组件状态、日志或 Audit。

旧 Local 路径：

```text
GET /api/knowledge/{project}/sources/{source}/snapshots/{snapshot}/raw
```

继续使用 Local `ArtifactStore + FileResponse`。Server 仍拒绝这条旧路径；两种模式不互相回退。

## 3. 组件与职责

### `services.object_store`

- `ObjectStore.head(key)` 返回 provider-neutral `ObjectStat`；
- S3 Adapter 校验 Content-Type、Byte Size 和上传时保存的 SHA-256 Metadata；
- `create_download_url` 可设置经过单行、ASCII 和长度校验的响应 Header；
- Provider Error 统一折叠为稳定错误，不返回供应商响应或 Secret。

该层不知道 Organization、Project、Source 或 Snapshot，只处理安全 Object Key 和供应商调用。

### `PostgresServerSnapshotEvidenceService`

- 每个 Manifest、Preview、Download 请求重新要求 `project.view`；
- 使用 `(project_id, source_id, snapshot_id)` 精确查询 Source/Snapshot；
- 只接受 Published Current 或当前 Pending；
- 从数据库里的 Artifact URI 解析 S3 Key，并验证配置 Bucket 与完整
  `organizations/{organization}/projects/{project}/` 前缀；
- 对象访问前执行 HEAD；Normalized Preview 受 Byte Limit、UTF-8、JSON Shape、文本长度和
  SHA-256 完整性约束；
- Raw 只签发固定短 TTL 的 Attachment URL；
- 所有持久化身份和 Provider 错误对 HTTP 层保持脱敏。

该服务不修改 Source、Snapshot、Receipt 或 Audit。只读 Evidence Access 若未来需要合规访问
日志，应进入独立访问日志系统，不能伪装成与业务事务原子提交的 append-only Audit Event。

### Knowledge HTTP 与请求安全

Middleware 首次把精确 Project 路径映射到 `project.view`。服务在数据库查询/对象读取或签名前
再次授权，防止仅依赖页面加载时的旧权限。Manifest 和实际 Preview/Download 是独立请求，
后两者会重新验证 Current/Pending 指针，因此 Pending 在页面打开后被 Reject、Publish 或替换
时，旧按钮不能继续访问历史对象。

所有响应使用 `Cache-Control: no-store`。404 不区分错误 Project、Source、Snapshot、历史 Slot
或缺少证据；503 不包含 Bucket、Key、URI、Provider Body 或 Secret。

### Server Inbox

Current 与 Pending 卡片分别以自己的 Source ID + Snapshot ID 获取 Manifest。两张卡的 Loading、
Preview、Download 与 Error State 相互独立；响应身份或 Slot 与卡片不一致时前端拒绝展示。

Viewer 可以核对证据，但不能因看到证据获得 Review/Publish 权限。Review 仍要求
`knowledge.edit`，Publish 仍要求 `knowledge.publish`。下载 URL 不写入 React State、
Local Storage 或公共 DTO，并使用 `noopener,noreferrer` 打开。

## 4. 数据流与失败语义

```text
Request middleware project.view
-> ServerSnapshotEvidenceService project.view
-> exact Project + Source + Snapshot query
-> require Published Current OR Pending
-> validate s3://bucket/scoped-key
-> HEAD object and validate provider-neutral metadata
-> Preview: bounded GET -> SHA-256 -> UTF-8 JSON -> text-only projection
   OR
   Download: fixed 60s attachment signature
-> no-store safe response
```

| 失败场景 | 对外结果 | 不变量 |
|---|---|---|
| 跨 Organization/Project/Source | 403 或统一 404 | 不扫描或返回其他 Scope |
| 历史/Rejected Snapshot | 404 | 不用 Latest 猜测 |
| Pending 在两次请求间变化 | 第二次 404 | Manifest 不是长期授权票据 |
| 错误 Bucket/Prefix/Scheme/Query/Fragment | 503 | 不回退 Local，不返回 URI |
| 对象不存在或 HEAD/GET/Sign 失败 | 503 | 不返回 Provider Body |
| Normalized 过大、非 JSON、坏 UTF-8/Hash | 503 | 不部分解释不可信结构 |
| 文本超过展示上限 | 200 + `truncated=true` | 不扩大后端/浏览器内存边界 |
| Raw 为 HTML/SVG/未知类型 | Attachment | 不以内联主动内容执行 |

S3 签名签发后无法即时撤销，因此 v1 使用 60 秒 TTL。若将来要求下载中的即时撤权或 Range
审计，应实现专用后端 Range Streaming；不能简单延长签名 TTL 或把公开 CDN URL写入 DTO。

## 5. 测试证据范围

确定性测试至少覆盖：

1. S3 HEAD、Content-Type/Disposition Override、TTL 与错误脱敏；
2. Current/Pending 精确身份、历史/Rejected 拒绝和 Source/Project 隔离；
3. Viewer 可读、撤权后拒绝，以及 Manifest 后指针变化的二次检查；
4. 错误 Scheme/Bucket/Prefix、路径穿越、Query/Fragment、对象缺失；
5. Normalized Byte Limit、UTF-8、JSON Shape、SHA-256、文本截断；
6. Raw 固定 Attachment Header 与安全文件名；
7. HTTP Route 权限、`no-store`、安全 404/503 和不泄露对象身份；
8. Current/Pending UI 不串位，旧 Source-level `raw_evidence_url` 在 Server 继续为 `null`；
9. Local `/raw`、私有上传、Web Evidence、Review/Publish、普通 Asset Download 和 Orphan
   Reconciliation 回归保持不变。

## 6. 后续重构接缝

以下能力未包含在 v1：

1. PDF/Image 的 Range Inline Preview；
2. Normalized Artifact 的完整 JSON 下载；
3. 历史/Rejected Snapshot 的受控 Evidence History；
4. Snapshot Review 历史、撤销或关闭 Pending 命令；
5. 独立的只读访问日志、合规留存和下载次数指标；
6. 多 Pending Snapshot；
7. 后端 Range Streaming 与即时撤权；
8. 对真实 S3 的 Content-Disposition、Range 206 和反向代理缓存行为冒烟。

后续替换对象供应商、HTTP 框架或前端组件时，必须保留：精确 Snapshot 身份、Current/Pending
可见性、请求级二次授权、完整 Scope Prefix、主动内容 Attachment、短 TTL、公共 DTO 不含对象
身份，以及 Local/Server 不回退这八项不变量。
