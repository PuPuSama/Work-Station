# M7 Candidate Route/Operation Inventory

## 1. 目的

本边界为冻结的发布候选提交生成两份可重复计算的清单：

- Route Inventory：覆盖 FastAPI/Starlette 当前注册的每个规范 method/path，包括框架生成的
  OpenAPI 与 Docs Route；
- Operation Inventory：覆盖 `SERVER_JOB_CONTROL_OPERATIONS` 的每个 Server Job Operation。

清单只证明代码图和声明语义已被完整枚举，不证明真实 IdP、PostgreSQL、ObjectStore、Provider、
备份恢复或生产停机已经验收。Route 全分类再结合精确 Server Gate、显式权限/重新授权元数据和
旧入口 fail-closed 行为测试，构成 `project_routes_scoped=true` 的代码证据；它不证明其余
Single Write、Worker 或外部发布 Capability。

## 2. Route Inventory

生成器直接读取 `app.routes` 中的 `starlette.routing.Route`，不维护完整路由的第二份副本。
因此 `/openapi.json`、`/docs`、`/docs/oauth2-redirect` 和 `/redoc` 也必须进入清单并保持
`local_only_fail_closed`。每个 Route Parameter 会替换成固定非敏感代表值，再依次应用运行时
相同的：

1. `server_http_route_available`；
2. Knowledge Route 额外应用 `server_knowledge_route_ready`。

每个规范 method/path 必须得到且只能得到一种正式状态：

- `server_ready`：Server Mode 门禁允许；
- `local_only_fail_closed`：仅 Local Mode 保留，Server Mode 在 Handler 前拒绝；
- `intentionally_unsupported`：明确不迁移的旧无 Project Batch/Job Control，或已被新 Metadata
  Route 替代的旧 Brand/Context/Domain/Delete Project Route。

每项同时记录稳定 `evidence_id`、Route Name、Gate、Path、Method、Scope、Permission、Storage、
Reauthorization 和状态，并全部进入 Digest。重复 method/path、空 Route Name、未知提交格式或
无法完整枚举时生成器 fail closed。

Starlette 为 GET 派生的 HEAD 复用相同 Handler 与 Gate，不单独形成规范 Entry。CORS Middleware
在业务 Router 前处理全局 CORS OPTIONS；它不是 `app.routes` 中的业务 Route，因此不计入 Route
Count。它必须继续只承担 Preflight、不得调用业务 Handler 或写存储，并由请求安全回归独立
覆盖。若未来显式注册业务 OPTIONS Route，则该 Route 必须进入 Inventory，不能被静默忽略。
上述两类派生 Surface 不能被描述为“没有考虑”。

## 3. Operation Inventory

Operation Inventory 与 `SERVER_JOB_CONTROL_OPERATIONS` 做精确集合比较。每项固定记录：

- Enqueue、Claim、Handler 授权；
- PostgreSQL Queue 和 Enqueue/Batch/Audit 原子边界；
- Operation-specific Commit Boundary 和真实 Audit Action；
- Cancel、Retry 和 bounded drain 语义。

新增 Operation 而未补齐上述字段会返回 `operation_inventory_incomplete`。`knowledge_research`
继续保留 domain-controlled Cancel/Retry，不能被清单错误描述为通用 Job Control。
Operation Entry 是绑定 Commit 的结构声明，不单独证明运行行为。授权、CAS/Audit、Cancel、
Retry 与 drain 的行为证据继续来自对应的 Operation 测试，尤其是
`backend/tests/test_m7_server_product_generation.py` 和
`backend/tests/test_m7_server_job_control.py`；Inventory 测试负责防止声明集合漂移。

## 4. Commit 和 Digest

CLI 只接受 lowercase 40-hex `release_commit`，并要求：

- 当前 Git `HEAD` 与该提交完全相同；
- tracked/untracked 工作树均为空；
- Artifact 输出到候选仓库之外；
- 目标文件不存在，禁止静默覆盖旧证据。
仓库检查使用命令级 `safe.directory=<resolved repository root>`，并要求该路径与
`git rev-parse --show-toplevel` 精确一致；它不修改用户全局 Git 配置。
CLI 在读取应用路由前、完成 Inventory 后以及 staging 文件写出后分别复核 HEAD 与干净工作树；
最后一次复核成功后以禁止覆盖的原子链接发布目标文件，任何失败均删除 staging 文件并 fail closed。
正式生成仍应在隔离、只读或 detached 的候选
Checkout 中执行，避免其他进程在验证窗口修改代码。

Route/Operation 分别以 `schema_version + release_commit + sorted entries` 的 canonical JSON 计算
SHA-256。Artifact 不写入生成时间，因此相同提交和代码图必须产生逐字节相同结果。Artifact
必须在候选提交完成之后生成；不能把 Digest 回写进同一提交造成自引用。

## 5. CLI

在干净候选提交上运行：

```powershell
# Windows，backend；先在隔离候选 Checkout 中确认工作树干净
$repo = 'D:\Project\article\article-agent-formal'
Push-Location "$repo\backend"
try {
  $commit = git -c "safe.directory=$($repo -replace '\\','/')" -C $repo rev-parse HEAD
  .\.venv\Scripts\python.exe -m knowledge_agent.m7_candidate_inventory `
    --release-commit $commit `
    --repository-root $repo `
    --output "D:\Project\article\controlled-evidence\m7-candidate-inventory-$commit.json"
} finally {
  Pop-Location
}
```

标准输出只包含 Commit、两项 Digest 和数量；错误只返回稳定错误码，不输出 Git 底层异常、
环境变量、Provider 内容或 Secret。
示例中的 sibling 文件只是本地 staging artifact。“路径名包含 controlled-evidence”、仓库外和
禁止覆盖都不等于不可变证据；只有外部受控不可变系统接收该文件、记录 Artifact ID/文件摘要
并由 Reviewer 绑定到本次候选 Evidence Bundle 后，才可作为发布 Evidence。

## 6. Capability 边界

候选提交内的验证文档必须让 `route_inventory_digest` 和 `operation_inventory_digest` 保持
`PENDING`。提交完成后生成的外部不可变 Receipt 可以把两项记录为已有 Artifact，但不能把
Digest 回写同一候选提交造成自引用；后续文档提交也只能引用被审计 Commit，不能冒充该 Commit
自身的证据。

外部 Reviewer 必须确认 Candidate Inventory 文件已包含在
`RecoveryEvidenceEnvelope V1.payload.evidence_bundle_sha256` 所绑定的 Evidence Bundle 中，并
核对内部两项 Digest。真实产品生成/选择冒烟、Worker Drain、PostgreSQL 单写、恢复演练和
生产 IdP/ObjectStore 仍须独立完成；`project_routes_scoped=true` 不替代这些证据，M7 继续
`no-go`。
