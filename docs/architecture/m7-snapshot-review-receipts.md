# M7 Snapshot Review Receipt：Current/Pending 发布边界

## 1. 状态与范围

本切片把 Server Knowledge 的 Review 授权从可变的 Source Metadata 移到精确、不可变的
Source Snapshot。数据库 Schema、Server Review/Publish 命令、Research 回调、Knowledge
Library 读模型和 Server Inbox UI 已按 Snapshot 身份接线；当前 Alembic Head 为
`20260806_0019`。

Receipt 切片本身没有交付 Server Raw Evidence Preview；后续
`M7 Server Snapshot Evidence Preview v1` 已用 Current/Pending 精确 Snapshot 路径补齐有界
Normalized Text Preview 和短时 Raw Attachment Download。Server Library DTO 仍把 Source-level
`raw_evidence_url` 固定为 `null`，不能按 Latest 猜测对象地址。详细边界见
`docs/architecture/m7-server-snapshot-evidence-preview.md`。

## 2. Current/Pending 双指针

`knowledge_sources` 是稳定 Source 聚合，并保存两个不同用途的复合外键指针：

| 指针 | 含义 | 是否进入 Retriever/Catalog |
|---|---|---|
| `current_snapshot_id` | 当前正式服务的不可变 Snapshot | 是，仅当 Source 为 `published` |
| `pending_snapshot_id` | 当前唯一待审或待发布候选 Snapshot | 否 |

两个指针都通过 `(project_id, source_id, snapshot_id)` 指向 `source_snapshots`，不能跨
Project 或 Source。数据库约束禁止两个非空指针指向同一 Snapshot。

M7 当前只允许一个 Pending：

- 新 Source/首个 Snapshot 入库后设置 Pending；
- 已 Published Source 的新字节成为 Pending，Current 与 `published` 状态保持不变；
- Pending 的完全相同内容可以精确重试；
- 已存在 Pending 时提交另一份新字节返回 Conflict；
- Reject 清空 Pending，但保留不可变 Snapshot 与其 Evidence Graph；
- Activate 把 Pending 原子切换成 Current，并清空 Pending。

Source `status` 是服务/列表投影，不再是 Review 授权准源。有 Current 时，新 Pending 的
`approve / needs_review / reject` 都不能把旧 Current 从 Published Retriever 撤下；没有
Current 时，Review 决定可以把 Source 投影为 `inbox / needs_review / rejected`。

## 3. 不可变 Receipt 表

`source_snapshot_review_receipts` 字段为：

```text
project_id
source_id
snapshot_id
review_version
receipt_id
decision
source_kind
trust_tier
reason
reviewer_kind
reviewer_id
reviewed_at
created_at
```

数据库不变量：

- 主键为 `(project_id, source_id, snapshot_id, review_version)`；Version 必须大于 0；
- `(project_id, receipt_id)` 唯一；
- 复合外键把 Receipt 固定到精确 Project/Source/Snapshot；
- Decision 只允许 `approve / needs_review / reject`；
- Source Kind 与 Trust Tier 使用正式枚举；
- Reason 去除首尾空白后长度为 1–500；Reason 是私有业务字段，不进入公共 DTO/Audit；
- Reviewer Kind 只允许 `user / automation / legacy_migration`；User/Automation 必须有
  Reviewer ID，Legacy Migration 必须没有 Reviewer ID；
- `trg_snapshot_review_receipts_append_only` 拒绝 UPDATE 和 DELETE。

Receipt 修改不是原地覆盖。对同一 Snapshot 的新裁决在锁定 Snapshot 后使用
`max(review_version) + 1` 追加；最新 Version 是该 Snapshot 当前有效裁决。

## 4. 幂等身份

Review 请求携带稳定 `receipt_id`：

- Server Inbox UI 为一次人工提交生成 UUID；失败重试复用同一 ID；
- Research 使用 Organization、Project、Thread、Attempt、Snapshot 和 Decision 派生稳定 ID；
- 同 Project、同 Receipt ID、同业务 Payload 返回原 Receipt，`created=false`，不重复 Audit；
- 幂等比较忽略 `reviewed_at`、`created_at` 和数据库分配的 Review Version；
- 同 Receipt ID 的 Project/Source/Snapshot、Decision、分类、Reason 或 Reviewer 任一不同，
  返回 Conflict；
- 新 Receipt ID 表示新的裁决事件并追加新 Version。

不能使用 Source ID 或 Payload Hash 代替命令身份：前者无法区分不同裁决，后者无法表达
同一内容被再次人工裁决。

## 5. Alembic 0019 Backfill 边界

`20260806_0019_snapshot_review_receipts` 是在既有迁移链之后追加的 Head，没有改写 M1–M7
历史 Revision 的语义。它新增 Pending 指针、Receipt 表、索引、约束、Backfill 和
append-only Trigger。

Backfill 规则是保守且 Snapshot-bound 的：

1. 旧 `published` Source 只为 `current_snapshot_id` 创建一条 `approve` Legacy Receipt；
   这只是 grandfather 已经在服务的精确字节，不授权其他 Snapshot。
2. 无 Current、恰好一个 Snapshot、且 `metadata.review.decision` 合法时，把旧决定绑定到
   唯一 Snapshot；非法/超长 Reason 使用固定安全迁移说明。
3. 无 Current 且恰好一个 Snapshot 时设置 Pending，使其可通过新路由继续审阅/发布。
4. 无 Current、多个 Snapshot 时绝不猜测 Latest；旧 Source-level `approve` 的 Inbox
   投影改为 `needs_review`，且不会生成授权新 Snapshot 的 Receipt。
5. 零 Snapshot 不生成 Receipt。
6. `metadata.review` 原 JSON 被保留用于历史/Local 兼容，但 Server Publish 不再读取它。

迁移期间旧进程仍可能只写 Source Metadata，因此正式切换必须在暂停 Review/Publish 写入和
Worker Claim 后执行 `alembic upgrade head`，再一次性部署 Receipt-aware API/Worker。

## 6. Review、Embedding、Activate

### 6.1 Review 事务

Server 路由：

```text
PUT /api/knowledge/{project}/sources/{source}/snapshots/{snapshot}/review
```

Body 只包含 `receipt_id`、`source_kind`、`trust_tier`、`decision` 和 `reason`。事务顺序：

```text
lock and re-evaluate project access
-> FOR UPDATE Source
-> lock exact Snapshot
-> resolve receipt retry or next Version
-> append immutable Receipt
-> verify requested Snapshot is Pending
-> update safe Source projection / clear rejected Pending
-> append redacted knowledge.snapshot.reviewed Audit
-> commit
```

完全相同的 Receipt 重试直接返回旧 Receipt。Audit、投影或任一约束失败时 Receipt 与 Source
变化一起回滚。

### 6.2 Embedding

Server Publish 只接受显式 Snapshot。第一次短事务重验 `knowledge.publish`、锁 Source、确认
目标仍是 Pending，并固定最新 `approve` Receipt 与 Expected Current。之后在数据库长事务外
调用 Embedding Provider；向量写入仍只属于 Candidate Snapshot，不改变 Current。

Provider 失败、进程终止或此时撤权都可能留下已写 Candidate Vector，但不会让它进入服务。

### 6.3 Activate 事务

Embedding 后重新开始事务：

```text
lock and re-evaluate project access
-> FOR UPDATE Source
-> re-read latest Receipt FOR UPDATE
-> require same receipt_id + review_version + approve
-> require same Pending and Expected Current
-> verify every Chunk has the matching embedding model
-> activate exact Snapshot
-> apply reviewed Source Kind/Trust Tier and immutable Snapshot projection
-> clear Pending
-> append redacted knowledge.snapshot.published Audit
-> commit
```

Receipt、Pending 或 Current 在 Embedding 期间漂移时 Activate 返回 Conflict。旧 Current、旧
Product/Image Catalog 和 Retriever 继续服务。

## 7. HTTP、Library 与 UI 投影

Server 精确路由为：

```text
PUT  /api/knowledge/{project}/sources/{source}/snapshots/{snapshot}/review
POST /api/knowledge/{project}/sources/{source}/snapshots/{snapshot}/publish
```

旧 Source-only Review/Publish 路由只保留给 Local façade；Server 调用会返回 Conflict，不能
通过“Latest Snapshot”猜测目标。

Knowledge Library 返回 Current/Pending 双身份，以及 Pending 的 fetched time、Chunk/Asset
计数、最新 Decision、Review Version 和 Reviewed At。Server Inbox 同时显示：

- Current Snapshot：当前检索版本；
- Pending Snapshot：待审版本及其 Receipt 投影；
- 只有 Pending 才显示 Snapshot Review/Publish 操作；
- 发布操作始终携带 Pending Snapshot ID；
- Server `raw_evidence_url` 仍为 `null`；Evidence 操作使用精确
  `/snapshots/{snapshot}/evidence*` 路径。

旧 `metadata.review` 只在 Local 或没有 Pending 的历史展示路径中保留兼容读取；Server 的
Review/Publish 授权只读取 Receipt。

## 8. Product 与 Asset 可见性

Pending Snapshot 可以带完整 Product Source Evidence、Snapshot Asset 和 Product Asset
Evidence。这些记录是不可变历史证据，但不会自动进入文章选择目录。

正式可见性仍要求：

```text
source.status = published
AND evidence.snapshot_id = source.current_snapshot_id
```

所以：

- Pending/Needs Review/Rejected/旧 Snapshot 的产品与图片不进入 Project Catalog；
- 新 Snapshot Activate 前，旧 Current 的产品与图片继续服务；
- Activate 后，只有新 Current 支撑的 Evidence 成为目录投影；
- Product `confirmed` 只是聚合状态，不能替代 Current Published Snapshot Evidence；
- Reject 不物理删除 Snapshot/Product/Asset Evidence，也不把有数据库引用的对象当作 Orphan。

## 9. 取消、撤权、并发与失败恢复

| 场景 | 结果 |
|---|---|
| 相同新 Snapshot 并发 Commit | Source Lock 串行；一个创建，另一个精确重试 |
| 不同新 Snapshot 并发 Commit | 一个占用 Pending；另一个 Conflict |
| Review 并发 Review | Snapshot Row Lock 串行分配 Version |
| Review 发生在 Embedding 期间 | Activate 检测 Receipt Version 漂移并拒绝 |
| 另一 Publish 切换 Current/Pending | Expected Current/Pending 检查拒绝陈旧 Candidate |
| Review Audit 失败 | Receipt 与 Source 投影回滚 |
| Embedding 部分失败 | Candidate Vector 可保留；Current/Pending 不变 |
| Activate/Audit/撤权失败 | Current 继续服务；Pending 与 Candidate 可供安全重试 |
| Reject Pending | Current 继续服务；Pending 清空；历史 Evidence 保留 |
| Job 取消发生在页面 Commit 后 | 已提交 Pending 保留；不再开始后续 Review/Publish |

Research 的取消仍使用可信 Callback/Checkpoint；`JobCancelled` 不伪装成 Provider `failed`。
Claim、Handler、逐 Fetch/Put/Commit、Review 与 Publish 的分层授权不能合并或省略。

## 10. Local 兼容边界

Local façade 继续使用既有 Source-scoped `metadata.review` 和 Source-only Publish，保持 SQLite/
文件工作流兼容。Snapshot Review 路由在 Local Mode 明确拒绝，Server Source-only 路由也
明确拒绝。两种模式不双写，也不互相回退 Repository、Artifact Store 或 Actor。

这是一条有意的兼容缝，不表示 Source-scoped Review 仍适合 Server。未来若迁移 Local，必须
单独提供 Local 数据升级和 UI 切换，不能直接删除旧 Metadata 读取。

## 11. 后续重构清单

以下均未因本切片自动完成：

1. Raw/Normalized Evidence 的 v1 授权预览已由后续切片完成；PDF/Image Range Inline、历史
   Snapshot、访问日志与即时撤权仍未完成；
2. 将 Research `_ACTIVE_RESEARCH_EXECUTION` ContextVar 替换为显式执行上下文；
3. 把 Product Rediscovery 与 Research 的 Web Evidence 装配收敛为共享工厂；
4. 是否支持多个并存 Pending Snapshot；当前仍只允许一个；
5. Snapshot 级 Review 历史 UI、撤销/关闭 Pending 的独立命令；
6. Local façade 的 Snapshot Receipt 迁移；
7. Receipt/Publish 运维指标与长期归档策略；
8. 对 Published Snapshot 的受控 Evidence Reconciliation；当前仍要求显式后续设计。

这些后续项不得弱化 exact Project/Source/Snapshot FK、append-only Receipt、Receipt Version
重验、Current-only 检索/目录投影或失败时旧 Current 继续服务的不变量。
