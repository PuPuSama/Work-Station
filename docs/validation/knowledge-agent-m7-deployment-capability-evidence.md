# Knowledge Agent M7：Deployment Capability Evidence 验证记录

## 状态

- 日期：2026-08-06
- 范围：`RecoveryEvidenceEnvelope V1` 消费、Ed25519 信任根和 fail-closed Deployment
  Preflight
- 当前状态：**Consumer 自动化验证完成；人工/生产恢复验证未执行**
- 发布判断：**no-go**

本切片已完成 Consumer 的自动化测试和项目整体回归，但没有执行真实 PostgreSQL/ObjectStore
恢复，没有 Capture/签发生产 Evidence，也没有选择生产 IdP/ObjectStore。测试生成的签名
Fixture 只能验证 Consumer，不能作为发布恢复证明。

结构准则见
`docs/architecture/m7-deployment-capability-evidence.md`，正式操作准源见
`docs/runbooks/knowledge-agent-m7-server-cutover.md`。

## 1. Consumer 单元测试（已由整体回归覆盖）

位置：Windows，`D:\Project\article\article-agent-formal\backend`。

```powershell
$env:ARTICLE_AGENT_CONFIG = `
  'D:\Project\article\article-agent-formal\config.ci.yaml'
.\.venv\Scripts\python.exe -m unittest `
  tests.test_recovery_evidence `
  tests.test_m7_deployment_readiness `
  tests.test_m7_deployment_preflight_cli
```

预期覆盖：

- V1 精确字段、重复 Key、未知/缺失字段、64 KiB 上限、UTF-8、NaN/Infinity 和
  Base64URL Signature 门禁；
- 唯一公钥环境契约为 Base64 32-byte Ed25519 raw public key；错误 Key、
  `signing_key_id` 指纹或签名 fail closed；
- 签名值使用 UTF-8、Key 排序、紧凑分隔符、`allow_nan=false` 的受限类型确定性 JSON，
  不把 `signature` 本身加入签名；
- `release_commit` 只接受完整 lowercase 40-hex，并与显式期望值相同；Alembic Head 必须
  精确匹配；
- 演练开始/完成/验证/过期顺序、24 小时演练上限、七天有效期、Operator/Reviewer 分离；
- 数据库九项固定 Check ID 为严格 Boolean；Source/Restored Manifest 相等且九项全为
  `true` 时门禁通过，单项 `false` 保留有效签名身份但使数据库恢复门禁失败；
- 对象四类样本各自非空，明细合计等于总数且全部 Hash 匹配；
- Observed RPO/RTO 不超过 Target；
- `VerifiedRecoveryEvidence` 不能由普通调用方伪造，公开值只包含安全布尔结果；
- 缺少/无效 Evidence 时 Preflight 的
  `recovery_evidence_identity/database_restore/object_restore/recovery_objectives` 全部失败，
  且错误不泄露 Secret、Hash、身份、时间或路径；
- 当前 Capability 常量允许
  `trusted_identity_source/project_routes_scoped/object_download_reauthorizes=true`；
  Task/Job Single Write 与 Worker Reauthorization 仍保持 false。

2026-08-06 的整体后端发现命令包含上述三个模块，未另行重复执行定向命令：

```text
covered_by: section 3 backend full regression
tests_run: tests.test_recovery_evidence, tests.test_m7_deployment_readiness,
           tests.test_m7_deployment_preflight_cli
result: passed as part of 791-test suite
artifact_id: local console output only
```

## 2. CLI fail-closed 验证（自动化完成；人工生产命令待执行）

`tests.test_m7_deployment_preflight_cli` 已验证缺参返回安全 no-go JSON、无效文件/公钥/解析
结果不会输出路径或错误正文，以及旧人工布尔参数已从 Parser 删除。以下命令模板继续保留给
受控候选环境人工验收；本地自动化结果不冒充生产 Evidence。

### 2.1 缺失恢复 Evidence

```powershell
.\.venv\Scripts\python.exe -m knowledge_agent.m7_deployment_preflight
```

预期：退出码 `2`；报告包含固定恢复 Check ID 且均为 `not verified`，不接受人工布尔
恢复声明。

### 2.2 无效或不匹配 Evidence

以下每次只改变一个条件并重新执行：错误签名、公钥、Key Fingerprint、短 Commit、不同
Commit、旧 Alembic Head、未来/过期时间、同一 Operator/Reviewer、数据库 Manifest 不同、
对象样本缺类/不匹配、Observed RPO/RTO 超标。

```powershell
$env:ARTICLE_AGENT_RECOVERY_EVIDENCE_PUBLIC_KEY = `
  '<测试用 Base64 raw public key>'
.\.venv\Scripts\python.exe -m knowledge_agent.m7_deployment_preflight `
  --recovery-evidence '<测试 Fixture 的绝对路径>' `
  --release-commit '<完整 40-hex 测试 Commit>'
```

预期：全部退出码 `2`；公开 JSON 不出现 Evidence 路径、Operator/Reviewer、Commit、Head、
Hash、签名、公钥或解析异常正文。

### 2.3 有效测试 Envelope

使用测试代码临时生成、由测试私钥签名并满足全部 V1 不变量的 Envelope。预期四项恢复
Check 可以为 true，但 `server_cutover` 仍因当前四项代码 Capability 为 false，整体退出码
仍为 `2`。不能把这一结果记录为真实恢复演练通过。

自动化结果：

```text
missing_evidence_exit_code: 2 (mocked live probes; safe report contract verified)
invalid_cases_checked: signature/key/commit/head/time/reviewer/schema/database/object/RPO-RTO
safe_output_reviewed_by: automated assertions plus code review
valid_fixture_recovery_checks: true
valid_fixture_server_cutover: false with current capability constants
artifact_id: local console output only
```

## 3. 数据库与完整回归（完成）

Windows 正式 worktree 根目录执行；数据库 URL 由本地测试环境注入，不写入仓库或验证记录：

```powershell
$env:ARTICLE_AGENT_CONFIG = `
  'D:\Project\article\article-agent-formal\config.ci.yaml'
.\backend\.venv\Scripts\python.exe -m unittest discover -s backend\tests -q

Set-Location frontend
npm.cmd run lint
npm.cmd run build
```

验收要求：现有 M0-M7 后端回归通过；真实 PostgreSQL Preflight Probe 仍要求
`EXPECTED_ALEMBIC_HEAD` 和 `vector` 扩展。该命令不执行备份恢复，不能填写 Runbook 的
数据库/对象恢复证据字段。

2026-08-06 结果：

```text
database_service: pgvector/pgvector:0.8.5-pg17-bookworm, healthy on loopback
alembic_head: 20260806_0019 (real PostgreSQL probe included)
backend: 791 tests in 93.814s, OK, skipped=2
frontend_lint: passed
frontend_build: Next.js 16.2.10 production build and TypeScript passed
artifact_id: local console output only
```

当前 Codex PowerShell 没有 `npm.cmd`；首次前端启动在执行 lint 前即停止。捆绑 `pnpm` 又在
脚本前被依赖策略检查拦截，因此未将它当作 lint/build 结果，也未保留其生成的 lock/workspace
文件。随后使用捆绑 Node 直接调用项目现有 `eslint` 和 `next` CLI，未安装或更新依赖，两个
正式前端检查均通过。后端没有因该环境问题重跑。

2026-08-07 的 Route Capability 收口新增完整 Inventory 不变量测试，并将
`project_routes_scoped` 改为代码事实 `true`。Candidate Inventory 与 Deployment Readiness
定向测试 `18/18` 通过；完整后端回归 `836` 项通过，`2` 项真实外部集成按门禁跳过。该结果
不改变恢复、Task/Job Single Write、Worker Drain 或生产外部证据状态。

## 4. 生产候选恢复验证（尚不可执行）

只有以下外部前置条件齐全后，才能执行 Runbook 的正式命令：

1. 候选 Commit 已冻结，Route/Operation Inventory Digest 已保存；
2. 每个目标 Project 的冻结窗口 Cutover Report 均 matched；
3. 隔离数据库和隔离 Bucket 已完成真实恢复与 Hash 抽样；
4. 独立 Reviewer 已复核外部不可变证据包，由受控 verifier 签发 V1 Envelope；
5. 生产 IdP Conformance、Bucket Policy/加密/版本/生命周期/异地策略和 Worker Drain
   Evidence 均可引用；
6. 六项代码 Capability 已由代码证据独立满足，而不是由 Envelope 翻转。

正式命令模板：

```powershell
$env:ARTICLE_AGENT_RECOVERY_EVIDENCE_PUBLIC_KEY = `
  '<由受控 Secret 注入的 Base64 raw public key>'
.\.venv\Scripts\python.exe -m knowledge_agent.m7_deployment_preflight `
  --recovery-evidence '<外部受控 V1 Envelope 的只读路径>' `
  --release-commit '<完整 40-hex 候选 Commit>'
```

本记录当前不得填写 `ready=true`、`go`、真实 RPO/RTO、恢复样本数或生产 Artifact ID。
