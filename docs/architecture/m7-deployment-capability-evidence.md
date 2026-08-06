# M7 Deployment Capability Evidence 与签名恢复证明

## 1. 目的与当前状态

本文定义 M7 如何消费代码能力证据和受控恢复演练证明。当前切片只增加
`RecoveryEvidenceEnvelope V1` 的受信验证边界与 fail-closed Preflight 接口，不负责创建
证据，也不执行 PostgreSQL/ObjectStore 备份或恢复。

以下边界必须保持：

- Preflight 只消费由独立受信 verifier 签发的不可变 Envelope；签名通过不等于本仓库执行了
  真实恢复；
- `CURRENT_SERVER_CUTOVER_CAPABILITIES` 是代码事实，不能由 Evidence 文件、命令参数或
  环境变量翻转；
- 当前仍为明确 `no-go`：真实受控环境恢复演练、生产 IdP/ObjectStore、Route/Operation
  完整覆盖和 PostgreSQL 单写切换均未完成；
- 本切片没有前端、HTTP、Evidence Capture/签发工具或新的业务 Operation。

## 2. 六项代码能力证据矩阵

| Capability | 当前值 | 允许变为 true 的最小代码证据 | 当前结论 |
|---|---:|---|---|
| `trusted_identity_source` | true | OIDC Authorization Code + PKCE、精确 Issuer/Audience、RS256/JWKS、State/Nonce、本地 External Identity 映射和数据库 Session Version | 代码链已接通；生产 IdP Conformance 是独立发布证据 |
| `project_routes_scoped` | false | Route Inventory 无未分类入口；Server 项目路由以路径 Project 为唯一身份准源并重新授权；旧无 Project/未迁移路由 fail closed | 尚未完成全部项目路由覆盖 |
| `postgres_task_single_write` | false | Server Task 写入口只写 PostgreSQL；冻结窗口内每个项目的 Task/Job Cutover Report 均 matched；切换后无 SQLite 双写或回灌 | 尚未完成整体单写切换 |
| `postgres_job_single_write` | false | 所有可运行 Server Operation 只创建、Claim 和提交 PostgreSQL Job；旧 SQLite Queue 不在 Server 启动；无可信 Requester 的历史 Job 只作 Terminal History | 尚未完成全部 Operation 与正式切换 |
| `worker_reauthorizes` | false | 每个可运行 Operation 在 Claim 前只读最小元数据授权，Handler/Provider/提交前再次授权；终态与安全 Audit 原子；有界 drain/join 可证明停机结果 | 只完成部分 Operation，尚无完整清单与正式排空证据 |
| `object_download_reauthorizes` | true | 私有下载 HTTP 入口和签名前分别授权，并校验 Bucket 与 Organization/Project Key Prefix；公开 DTO 不返回长期 URL/URI/Key/Hash | 代码链已接通；生产 Bucket Policy/加密/版本策略仍须外部证据 |

Boolean 只能在 Inventory、自动验证和结构记录全部完成的代码提交中修改。Runbook 存在、
Envelope 签名有效或恢复 Reviewer 给出通过结论，都不能代替代码能力证据。

## 3. Route 与 Operation Inventory 后续要求

`docs/architecture/m7-server-route-migration-matrix.md` 是当前人工可读准源；后续需要生成可
验证的 Inventory Manifest，并绑定完整 Release Commit：

1. 每条 Server 路由记录 Method、规范化 Path、Scope、权限、存储准源、二次授权点、
   Local/Server 状态和稳定 Evidence ID；
2. 每个入口只能是 `server_ready`、`local_only_fail_closed` 或
   `intentionally_unsupported`，不能留空或用“其余均完成”代替；
3. 每个可运行 Operation 记录 Enqueue、Queue、Claim、Handler、Commit、Audit、Cancel、
   Retry 和 drain 语义；`knowledge_research` 等特殊 Operation 必须保留专用语义；
4. Manifest 生成稳定 `route_inventory_digest` 和 `operation_inventory_digest`。新增路由或
   Operation 会使旧 Digest 失效，必须重新审计；
5. `products` 主生成链必须先进入 Inventory，并具备候选/提交分离、Current Published
   Context、Provider 脱敏、Task CAS/Audit、两阶段授权和有界停机。完成这一项不自动证明
   全部 Single Write/Worker Capability 已完成。

## 4. RecoveryEvidenceEnvelope V1

### 4.1 严格 Envelope

Evidence 是最多 64 KiB 的单个 UTF-8 JSON 对象。未知/缺失字段、重复 Key、NaN/Infinity、
非法 Base64URL 或非规范值全部 fail closed。V1 字段逐项如下：

```json
{
  "schema_version": "article-agent.recovery-evidence.v1",
  "signature_algorithm": "Ed25519",
  "signing_key_id": "ed25519:<32-byte raw public key 的 sha256 hex>",
  "payload": {
    "release_commit": "<完整 40-hex Git commit>",
    "alembic_head": "20260806_0019",
    "started_at": "2026-08-06T08:00:00Z",
    "completed_at": "2026-08-06T10:00:00Z",
    "expires_at": "2026-08-13T10:00:00Z",
    "operator": "release-operator-id",
    "reviewer": "independent-reviewer-id",
    "evidence_bundle_sha256": "<外部不可变证据包 sha256 hex>",
    "database_restore": {
      "dump_sha256": "<database dump sha256 hex>",
      "source_manifest_sha256": "<源库 manifest sha256 hex>",
      "restored_manifest_sha256": "<恢复库 manifest sha256 hex>",
      "checks": {
        "schema_and_vector": true,
        "required_relations": true,
        "workspace_session_versions": true,
        "tenant_foreign_keys": true,
        "audit_append_only": true,
        "snapshot_pointer_constraints": true,
        "snapshot_review_receipts": true,
        "snapshot_review_append_only": true,
        "task_job_manifest": true
      }
    },
    "object_restore": {
      "inventory_sha256": "<inventory 或 version watermark manifest sha256 hex>",
      "sample_manifest_sha256": "<对象抽样 manifest sha256 hex>",
      "sample_count": 4,
      "matched_count": 4,
      "samples_by_kind": {
        "product_primary": {"sample_count": 1, "matched_count": 1},
        "product_gallery": {"sample_count": 1, "matched_count": 1},
        "private_document": {"sample_count": 1, "matched_count": 1},
        "normalized_artifact": {"sample_count": 1, "matched_count": 1}
      }
    },
    "recovery_objectives": {
      "target_rpo_seconds": 3600,
      "observed_rpo_seconds": 1200,
      "target_rto_seconds": 7200,
      "observed_rto_seconds": 3600
    }
  },
  "signature": "<无 padding 的 Base64URL Ed25519 signature>"
}
```

真实 Envelope 只携带摘要、稳定身份、时间、数量和目标，不嵌入数据库内容、客户正文、对象
URI、Bucket/Key、连接 URL、Token、Secret 或 Provider 错误正文。

### 4.2 签名与信任根

- 算法固定为 Ed25519，不接受算法协商、对称 MAC 或 Envelope 自带公钥；
- `signature` 外的四项 `schema_version/signature_algorithm/signing_key_id/payload` 组成
  签名值；当前受限 JSON 类型使用 UTF-8、Key 排序、紧凑分隔符和 `allow_nan=false` 的
  确定性编码，不宣称实现完整 RFC 8785；
- 唯一信任根是环境变量 `ARTICLE_AGENT_RECOVERY_EVIDENCE_PUBLIC_KEY`，值为 Base64 编码
  的 32-byte Ed25519 raw public key；不读取 Evidence 自带公钥，也不回退到 Actor Session、
  LLM、Embedding 或 ObjectStore Secret；
- `signing_key_id` 必须等于 `ed25519:` 加受信 raw public key 的 SHA-256 hex；不另设可由
  操作者覆盖的 Key ID 环境变量；
- 公钥缺失/非法、指纹不符或签名非法都只返回固定失败，不输出 Evidence、
  签名、公钥或底层异常。

### 4.3 Commit、Head、时间和 Reviewer 不变量

1. Preflight 必须显式接收 `--release-commit`；参数和 `payload.release_commit` 都是完整
   lowercase 40-hex 并精确相同，不接受短 Commit、工作树猜测或“最新 Commit”；
2. `payload.alembic_head` 与代码 `EXPECTED_ALEMBIC_HEAD` 精确相同；Head 变化立即使旧
   Evidence 失效；
3. `started_at < completed_at <= 当前 UTC < expires_at`；演练最长 24 小时，Evidence 从
  完成时起最多有效七天；时间必须带明确 UTC Offset，不接受 naive datetime；
4. `operator/reviewer` 是有界稳定身份且大小写不敏感地互不相同；执行与复核职责分离；
5. `evidence_bundle_sha256` 绑定外部受控不可变证据包。Consumer 不联网查询或生成该包，
   发布流程必须禁止跨 Commit/窗口复用。

### 4.4 数据库、对象和 RPO/RTO 不变量

- `dump_sha256` 与 Source/Restored Manifest 均是完整 lowercase SHA-256。九项固定数据库
  Check ID 必须逐项存在且值为严格 Boolean，不接受 `1`、字符串或自由文本“通过”；仅当
  两个 Manifest 相同且九项均为 `true` 时 `database_restore` 才通过。`false` 是身份有效
  Evidence 的独立 no-go，不会伪装成 Envelope 解析失败；详细检查产物由
  `evidence_bundle_sha256` 绑定在外部证据包；
- `task_job_manifest` 必须对应冻结窗口内所有目标 Project 的 matched Cutover Report；任一
  Active SQLite Job、重复/空 ID、顺序/状态/摘要差异都会使签发流程停止；
- 对象 `sample_count/matched_count` 必须等于四类明细之和，总数和各类均全部匹配；四类都
  至少一个样本，只核对对象数量不能通过；
- RPO/RTO Target 是正整数秒，Observed 是非负整数秒，且 Observed 不得超过 Target；
- Bucket 私有、服务端加密、版本/不可变备份、生命周期和异地策略属于独立发布证据，不能
  从对象 Hash 抽样反推；
- Consumer 只验证受信 Reviewer 对这些摘要与结果的签名声明，不连接恢复库、不读取对象
  明细，也不能据此声称当前真实恢复演练已经执行。

## 5. Preflight 消费接口

Windows 本地候选验收示例，位置为正式 worktree 的 `backend`：

```powershell
$env:ARTICLE_AGENT_RECOVERY_EVIDENCE_PUBLIC_KEY = '<由受控 Secret 注入的 Base64 raw public key>'
.\.venv\Scripts\python.exe -m knowledge_agent.m7_deployment_preflight `
  --recovery-evidence 'D:\controlled-evidence\recovery-envelope-v1.json' `
  --release-commit '<完整 40-hex 候选 Commit>'
```

缺少 Evidence、Release Commit、公钥或任一 Envelope 不变量时，恢复 Evidence Check 必须失败
并返回非零退出码；其他独立 Check 仍可显示安全状态。只有配置、数据库、实时 IdP、
ObjectStore、六项 Capability 和签名恢复 Evidence 全部通过时，报告才可 `ready=true`。

## 6. Evidence 存放与生命周期

- Envelope、数据库 Dump、Inventory、对象抽样明细、OIDC Conformance、Bucket Policy 和恢复
  日志放在外部受控不可变系统；仓库与普通日志只记录非敏感 Artifact ID/Digest/验证结果；
- Capture/签发程序、Reviewer 工作流和真实恢复不属于当前切片。未来 Capture 必须与
  Consumer 分权，部署身份不得持有签名私钥；
- Candidate Commit、Alembic Head、Route/Operation Inventory Digest、受信公钥或恢复目标
  变化都要求重新 Capture、Review 和签名；
- 签名恢复 Evidence 只满足恢复相关 Check，不覆盖 `server_cutover`、实时 IdP、ObjectStore
  或数据库 Check；任一其他门禁失败仍为 no-go。

## 7. 后续顺序

1. 先完成 Evidence Consumer 的严格 Schema、Ed25519、Commit/Head/Time 与安全输出验证；
2. 再迁移 `products` 主生成链并补齐 Route/Operation Inventory；
3. 完成其余 API/Worker 覆盖、每项目冻结窗口 matched 报告和 PostgreSQL Task/Job 单写；
4. 代码证据完整后才逐项修改 Capability 常量；
5. 候选 Commit 冻结后，在真实环境执行 Capture、独立 Review、恢复演练和生产
   IdP/ObjectStore 验收；
6. Preflight、冒烟、回归、前端构建和发布模板全部有证据后，才允许作出 `go` 决策。
