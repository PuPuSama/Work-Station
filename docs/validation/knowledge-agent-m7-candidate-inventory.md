# Knowledge Agent M7 Candidate Inventory 验证记录

## 1. 当前结论

- Route/Operation Inventory 生成与 fail-closed 契约已实现，自动验证结果在最终回归后填写；
- 代码测试使用固定假 Commit，只证明确定性、结构覆盖和声明集合不漂移，不冒充候选 Artifact
  或真实 Operation 行为；
- 真实 Digest 只能在本切片提交完成、隔离 Checkout 的 HEAD 精确匹配且工作树干净后生成；
- 当前没有产品生成、产品选择或 Worker Drain 的外部受控运行 Artifact，因此三项继续
  `PENDING`；不使用易变化的本机 Docker/Provider 状态解释 Evidence 结论；
- Route 全分类、精确 Project Gate、显式权限/重新授权元数据与旧入口 fail closed 已形成
  `project_routes_scoped=true` 的代码证据；其他 Capability 不变，M7 保持 `no-go`。

## 2. 自动验证

`backend/tests/test_m7_candidate_inventory.py` 至少验证：

1. 当前 185 个 FastAPI/Starlette 规范 method/path 全部且只被分类一次，包含
   `/openapi.json`、`/docs`、`/docs/oauth2-redirect` 与 `/redoc`；
2. 产品候选 POST、Job GET 与正式产品 PUT 都是 `server_ready`；
3. 旧 Local 自动产品 Route 是 `local_only_fail_closed`，旧无 Project Batch 是
   `intentionally_unsupported`；
4. Knowledge Route 同时使用全局和 Knowledge 子门禁；
5. 每条 Route 含 Evidence ID、Scope、Permission、Storage、Reauthorization 和正式状态；框架
   Docs Route 保持 fail closed，派生 HEAD 与 CORS OPTIONS 的策略有明确结构边界；
6. Operation Entries 与 `SERVER_JOB_CONTROL_OPERATIONS` 精确相等；
7. `products` 的三阶段授权、Task CAS/Audit、Cancel/Retry/Drain 声明与真实准源交叉核对；
8. `knowledge_research` 继续使用 domain-controlled Cancel/Retry；
9. Route 顺序变化不改变 JSON 或 Digest，Commit 变化必须改变两项 Digest；
10. 非 lowercase 40-hex、重复 Route、HEAD 漂移、脏工作树、dubious ownership 与写出后漂移
    全部 fail closed，公共错误不泄露 Git/应用底层异常；
11. 所有 Server-ready Project/Knowledge 路由均为 Project Scope，具有显式 Permission 与
    Reauthorization，旧 `/api/tasks*`、`/api/batches*`、`/api/batch-jobs*` 不可 Server-ready。

第 7 项的运行行为不由清单字符串断言代替；必须同时保留
`backend/tests/test_m7_server_product_generation.py`、
`backend/tests/test_m7_server_job_control.py` 和 Server Request Security 的既有行为覆盖。

### 2.1 本地回归结果（2026-08-06）

- Candidate Inventory 定向测试：`11/11` 通过；
- 后端整体回归：共运行 `787` 项，Candidate Inventory 新增覆盖修正后通过；整体仍有 `2 errors + 1 failure`，
  三项均来自既有 Humanize 测试读取本机不存在的
  `D:\article\降ai提示词-未测试效果版.txt`，其中后台任务失败是同一根因；
- 前端 ESLint：通过；
- Next.js `16.2.10` 生产构建：通过。

按“整体测试只跑一次、环境失败只补跑未执行或失败部分”的约束，没有再次运行完整后端套件；仅补跑
`backend.tests.test_m7_candidate_inventory` 并确认 `11/11` 通过。上述 Humanize 环境问题不转换为
Candidate Inventory 通过证据，也不在本切片混入无关配置修复。

后续独立修复已把 Local Humanize 配置与默认值统一到仓库内
`backend/prompts/humanize_ci.txt`，并只补跑最初失败的两个 Humanize 用例，结果 `2/2` 通过。
完整 `787` 项套件未重复执行，因此本记录仍保留原始整体退出结果，不把定向补跑改写成完整回归退出码 0。

### 2.2 Project Route Capability 收口（2026-08-07）

- Candidate Inventory 定向测试现为 `12/12` 通过，其中新增一项完整 Route 图不变量；
- 新测试要求每条 Server-ready Project/Knowledge Route 均为 Project Scope，具有显式
  Permission 与 Reauthorization，并要求旧 Task/Batch/Batch Job 入口不可 Server-ready；
- Deployment Readiness 定向测试与 Candidate Inventory 合计 `18/18` 通过；
- 完整后端回归 `836` 项通过，`2` 项真实外部集成按显式门禁跳过；
- 上述代码证据允许 `project_routes_scoped=true`，但真实 Candidate Digest、受控冒烟、
  Worker Drain、Task/Job 单写和恢复证据仍为 `PENDING`。

## 3. Artifact 验证

候选提交完成后，在隔离 Checkout 运行 CLI 两次：第一次写本地 staging artifact；第二次写
不同临时外部路径并比较文件 SHA-256。两份文件必须逐字节一致。随后核对：

```text
schema_version: 1
release_commit: <当前完整 HEAD>
route_count: 185
route_counts.server_ready + local_only_fail_closed + intentionally_unsupported: 185
operation_count: 10
products.state: server_ready
products.commit_boundary: postgres_task_cas_and_audit
knowledge_research.cancel/retry: domain_controlled_only
```

Artifact 只保存 Route Template、稳定状态和 Operation 语义，不保存 Concrete Project、Task、
User、URL、Prompt、Evidence Binding、Provider 原文、环境值或 Secret。
本地文件只有被外部受控不可变系统接收、取得 Artifact ID/文件摘要，并纳入
`evidence_bundle_sha256` 所绑定的 Evidence Bundle 后才是正式 Evidence。

## 4. Evidence 位

| Evidence | 当前值 | 通过标准 |
|---|---|---|
| `candidate_inventory_tests` | `PENDING` | 定向测试和最终整体回归退出码 0 |
| `candidate_commit_clean` | `PENDING` | Artifact 生成时 HEAD 精确匹配且工作树为空 |
| `route_inventory_digest` | `PENDING` | 外部 Artifact 含 185 个 Route Entry 与稳定 SHA-256 |
| `operation_inventory_digest` | `PENDING` | 外部 Artifact 含 10 个完整 Operation Entry 与稳定 SHA-256 |
| `artifact_reproducible` | `PENDING` | 同一 Commit 二次生成逐字节相同 |
| `product_generation_smoke` | `PENDING` | 真实受控环境 Artifact 通过 |
| `product_selection_smoke` | `PENDING` | 与候选生成分离的正式选择 Artifact 通过 |
| `worker_drain_report_artifact` | `PENDING` | 真实 `products` Worker 有界排空 |

候选提交内本表保持上述 Digest 为 `PENDING`，避免将生成后的 Digest 回写同一提交造成自引用。
实际通过状态只进入外部不可变 Receipt；后续仓库文档若引用 Receipt，必须同时写明被审计的完整
Commit，不能把后续文档提交当成该 Commit 自身 Evidence。空值不得转换为通过，Inventory
Digest 也不得替代后三项受控运行证据。
