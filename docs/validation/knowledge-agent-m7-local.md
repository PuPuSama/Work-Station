# Knowledge Agent M7 持续实施本地验证记录

- 日期：2026-07-30
- 分支：`feature/knowledge-agent-m7`
- 基线：`cc4bbf2 feat: add M6 retrieval evaluation framework`
- 范围：多租户 Schema、项目 RBAC、Actor Session、成员管理、Task/Job PostgreSQL、私有对象存储与 append-only 审计底座

## 环境

- Windows PowerShell
- Python：`backend/.venv/Scripts/python.exe`
- PostgreSQL/pgvector：`pgvector/pgvector:0.8.5-pg17-bookworm`
- 本地端口：`127.0.0.1:55433`
- Alembic Head：`20260730_0010`

## 已通过验证

### M7 定向测试

```powershell
Set-Location D:\Project\article\article-agent-formal\backend
# ARTICLE_AGENT_DATABASE_URL 由本地安全环境提供，不写入仓库或终端输出。
.\.venv\Scripts\python.exe -m unittest `
  tests.test_m7_server_auth `
  tests.test_m7_access_control `
  tests.test_m7_access_control_postgres -v
```

结果：

- 20 tests；
- Actor Token 防篡改、过期、未来签发与 Secret 隔离；
- Token 只保存 Organization/User，不保存 Role 或 Permission；
- 纯权限矩阵通过；
- 真实 PostgreSQL 跨组织访问拒绝；
- 普通 Team Member 无隐式项目权限；
- 禁用 User、未绑定旧 Project fail closed；
- ProjectMembership 复合外键拒绝跨组织组合；
- Audit Writer 在业务事务内追加；
- Trigger 拒绝 Audit Event 更新和删除。
- 成员授权/撤销与 Audit Event 同事务；
- 重复 Event ID 会回滚同事务内的成员角色更新。

### Alembic 往返和重复升级

在确认 M7 新表均为 0 行后执行：

```powershell
.\.venv\Scripts\alembic.exe downgrade 20260730_0007
.\.venv\Scripts\alembic.exe upgrade head
.\.venv\Scripts\alembic.exe upgrade head
.\.venv\Scripts\alembic.exe current
```

结果：

- `0008 -> 0007` 成功；
- `0007 -> 0008` 成功；
- 重复 `upgrade head` 成功；
- 当前为 `20260730_0008 (head)`。

Task/Job 迁移新增后，再在四张新表均为 0 行时执行：

```powershell
.\.venv\Scripts\alembic.exe downgrade 20260730_0008
.\.venv\Scripts\alembic.exe upgrade head
.\.venv\Scripts\alembic.exe upgrade head
.\.venv\Scripts\alembic.exe current
```

结果为 `20260730_0009 (head)`。

External Identity 迁移新增后，在测试映射均已回滚/清理时执行：

```powershell
.\.venv\Scripts\alembic.exe downgrade 20260730_0009
.\.venv\Scripts\alembic.exe upgrade head
.\.venv\Scripts\alembic.exe upgrade head
.\.venv\Scripts\alembic.exe current
```

结果为 `20260730_0010 (head)`；降级和两次升级均成功。

### Task/Job PostgreSQL 定向测试

```powershell
.\.venv\Scripts\python.exe -m unittest `
  tests.test_m7_postgres_tasks -v
```

结果：

- 13 tests；
- 同 Task ID 跨 Project 隔离；
- JSON 扩展字段、顺序和 `TaskStore` Revision 语义保留；
- SQLite Task 导入数量与 SHA-256 摘要复核，差异目标不覆盖；
- 两个并发 Writer 只有一个 Revision CAS 成功；
- 两个 Worker 的并发 Claim 结果不重叠；
- 过期 Lease 可接管，旧 Worker 不能写回结果；
- Retry、Conflict、Cancel 和 Batch 汇总契约通过；
- Active SQLite Job 会阻止切换；
- Terminal Batch/Job 保留稳定 ID，并复核数量、状态分布和内容摘要；
- Task/Job 复合外键、Lease CHECK 和活跃 Job 部分唯一索引通过。

### 完整后端回归

```powershell
$env:ARTICLE_AGENT_CONFIG = `
  'D:\Project\article\article-agent-formal\config.ci.yaml'
# ARTICLE_AGENT_DATABASE_URL 由本地安全环境提供，不写入仓库或终端输出。
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

结果：

- 487 tests；
- 全部通过；
- 2 skipped（真实 S3 与真实外部 LightRAG 默认显式跳过）；
- 未调用真实外部 LLM、Embedding 或 LightRAG 服务。

### 对象存储定向与真实兼容测试

无网络单元契约：

```powershell
.\.venv\Scripts\python.exe -m unittest `
  tests.test_m7_object_store `
  tests.test_m7_knowledge_object_storage `
  tests.test_m7_object_store_s3 -v
```

覆盖：

- Key 为 `organization_id + project_id + SHA-256` 内容寻址；
- 路径穿越、跨项目 Adapter 调用和内容哈希不匹配被拒绝；
- Put 不设置公共 ACL，生产默认带 `AES256` 服务端加密参数；
- Get 有大小上限并关闭响应流；
- 下载 URL 有效期最多一小时；
- Object Store Key/Secret 不复用 LLM/Embedding Secret，`repr` 和稳定异常不泄露；
- 上传要求 `knowledge.edit`，下载重新要求 `project.view`；
- 数据库资产 URI 的 Bucket 或 Organization/Project 前缀不匹配时不签名。

真实 S3 兼容往返使用 `compose.dev.yaml` 的显式 `object-store` profile，
一次性随机开发凭据和专用 `article-agent-test-*` Bucket。由于该本地 MinIO
未配置 KMS，测试显式使用 `ARTICLE_AGENT_OBJECT_STORE_SSE=none`；生产默认不变。

```powershell
$env:ARTICLE_AGENT_OBJECT_STORE_INTEGRATION = '1'
.\.venv\Scripts\python.exe -m unittest `
  tests.test_m7_object_store_s3 -v
```

结果：

- 1 test；
- Put/Get/Presigned URL/Delete 全部通过；
- 测试对象在 `finally` 中删除；
- MinIO API/Console 仅绑定 `127.0.0.1:59000/59001`；
- 此镜像只作为开发兼容目标，不代表生产供应商已选定。

### 部署门禁单元测试

```powershell
.\.venv\Scripts\python.exe -m unittest `
  tests.test_m7_deployment_readiness -v
```

结果：

- 6 tests（含真实 PostgreSQL Preflight Probe）；
- 全部显式能力、数据库、S3 和恢复演练证明齐全时才返回 ready；
- 当前缺少正式身份、路由 Scope、Task/Job 单写或 Worker 授权时 fail closed；
- Alembic 不是 `20260730_0010` 时阻止发布；
- 远程对象存储 Endpoint 使用明文 HTTP 时阻止发布（localhost 开发目标除外）；
- 数据库 URL、Embedding Key、S3 Key/Secret 和供应商错误正文不进入公开报告。

当前 `CURRENT_SERVER_CUTOVER_CAPABILITIES` 的预期结果仍是 no-go。本文没有把
Runbook 的存在描述成“备份已完成”；真实恢复证据、RPO/RTO 和生产供应商待外部环境。

### Server Request Security 与 Knowledge Router

```powershell
$env:ARTICLE_AGENT_DATABASE_URL = '<由安全环境注入>'
.\.venv\Scripts\python.exe -m unittest `
  tests.test_m7_server_request_security `
  tests.test_m7_server_auth `
  tests.test_auth -v
```

结果：

- 15 tests；
- Actor Cookie 篡改/缺失统一为未认证；
- Project 按官网域名规则规范化后才查询 PostgreSQL 权限事实；
- Viewer 可通过 Knowledge GET，但不能发布来源；
- Publish/Product Confirm 映射 `knowledge.publish`，普通写操作映射
  `knowledge.edit`，只读对话/覆盖率等映射 `project.view`；
- Server Mode 下 `/api/tasks` 等未迁移旧入口返回 503；
- WordPress、私有上传、Research Start/Resume 和本地 Raw Artifact 暂不开放；
- Server Mode 不创建 `job_queue.sqlite3`、不启动 SQLite Runner，直接调用全局
  `store()/batch_queue()` 也会拒绝；
- `app.py` 直接声明的 retrieval-plan 兼容路由同样先执行 401/403 授权门，随后因
  仍依赖旧 TaskStore 而保持 503；
- 旧 `APP_PASSWORD` 登录在 Server Mode 返回 503；
- 真实 Lifespan 使用 PostgreSQL Engine 构建请求安全服务并正常清理；
- Local Mode 原密码认证测试保持通过。

### Task/Job C3 只读双读报告

```powershell
$env:ARTICLE_AGENT_DATABASE_URL = '<由安全环境注入>'
.\.venv\Scripts\python.exe -m unittest `
  tests.test_m7_server_cutover_report `
  tests.test_m7_postgres_tasks -v
```

结果：

- 20 tests；
- Read-only SQLite Source 与现有 Repository 导出语义一致；
- 匹配时只各读取一次，不执行 Import、Claim、Recover 或状态变更；
- Task 顺序变化、仅源/仅目标 ID、内容变化、空 ID 和重复 ID 均可定位；
- Active SQLite Job 即使两边数据相同也阻止切换；
- Task Target 与 Job Target 的 Organization/Project 不一致直接拒绝；
- 真实 PostgreSQL 测试先证明 matched，再只修改 PG Task，报告准确定位
  `changed_ids`；
- 对外报告不包含测试用文章正文。

冻结窗口 CLI：

```powershell
.\.venv\Scripts\python.exe -m knowledge_agent.m7_cutover_report `
  --organization-id '<organization_id>' `
  --project-id '<project_id>' `
  --task-database '<tasks.sqlite3>' `
  --job-database '<job_queue.sqlite3>'
```

返回 0 才表示该次冻结快照可进入下一门；返回 2 表示差异。报告后 SQLite 再发生
任何写入都会使证据失效，CLI 本身不是同步器。

### Server Project Task 只读 API

```powershell
$env:ARTICLE_AGENT_DATABASE_URL = '<由安全环境注入>'
.\.venv\Scripts\python.exe -m unittest `
  tests.test_m7_server_project_tasks `
  tests.test_m7_server_request_security -v
```

结果：

- 9 tests；
- 无 Actor Cookie 返回 401，跨 Organization Project 返回统一 403；
- Project A 的列表只返回 Project A 的 PostgreSQL Task；
- 用 Project A 路径请求 Project B Task 返回 404，不跨 Scope 查找；
- 旧 `/api/tasks` 在 Server Mode 继续返回 503；
- POST 等尚未迁移的写方法不进入 Server Project Task 白名单；
- Local Mode 不增加这组服务器专用 API；
- 使用不存在的本地数据目录启动并读取后，该目录仍不存在，证明没有 SQLite/JSON 回退。

### External Identity 映射与交换

```powershell
$env:ARTICLE_AGENT_DATABASE_URL = '<由安全环境注入>'
.\.venv\Scripts\python.exe -m unittest `
  tests.test_m7_external_identity `
  tests.test_m7_deployment_readiness -v
```

结果：

- 14 tests；
- Issuer 必须是 HTTPS；仅 localhost/loopback 开发 Issuer 可使用 HTTP；
- Session Exchange 只接收已验证的 Issuer/Subject，不接收或信任外部 Role；
- `(issuer, subject)` 不能映射到两个 Organization；
- 复合 FK 拒绝把 Organization A 映射到 Organization B 的 User；
- Mapping Revoked、User Disabled、Organization Suspended 均不能解析 Actor；
- Link/Revoke 只有 Active Org Admin 可执行，且与 Audit Event 同事务；
- 审计目标使用 Subject 哈希，审计 Details 不保存原始 Subject；
- Preflight Head 已更新为 `20260730_0010`。

## 诊断记录

第一次完整回归未指定 CI Config，2 个既有 Humanize 测试读取本机默认
`D:\article\...` Prompt 路径而失败。第二次把 `config.ci.yaml` 写成相对路径，但命令工作目录为
`backend`，因此被解析为不存在的 `backend/config.ci.yaml`。

最终使用仓库根目录的绝对 Config 路径后，424 tests 全部通过。这两个失败属于验证命令环境，不是 M7 Schema、权限或审计代码失败。

## 当前未验证或未接入

- 已有 Actor Session Codec 和供应商无关 Identity Mapping，但具体 IdP Token 验证与
  登录签发入口尚未接入；
- Knowledge Router 与其内部 Retriever 已接入请求级 RBAC；Project/Article/Task/Batch
  旧路由和 Worker 尚未接入；另有新的项目级 PostgreSQL Task 只读接口，不代表旧路由
  或写路径已经迁移；
- 对象下载服务底层已重新授权，但现有 Raw Artifact HTTP 路由仍是本地文件实现，
  因此 Server Mode 明确阻断；
- `app.py` 的 Task/Job 仍以 SQLite 为准；PostgreSQL 实现尚未成为服务器单写准源；
- 上一条只适用于本地模式；Server Mode 已停止 SQLite Queue/Worker，但新的项目级
  PostgreSQL Worker 仍未接线；
- SQLite Terminal Job 历史导入和冻结窗口双读报告已实现；matched 证据留存流程与
  `app.py` PostgreSQL 单写切换尚未实现；
- S3 对象存储底层、产品资产桥接和 no-go 部署门禁已实现；真实备份恢复演练
  与生产供应商尚未完成；
- 本阶段未修改前端，因此没有新增 M7 前端验收项。

这些项目属于后续 M7-B/C/D，不得把本记录描述为“多人服务器版已上线”。
