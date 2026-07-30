# Knowledge Agent M7-A/B/C1-C2/D1 本地验证记录

- 日期：2026-07-30
- 分支：`feature/knowledge-agent-m7`
- 基线：`cc4bbf2 feat: add M6 retrieval evaluation framework`
- 范围：多租户 Schema、项目 RBAC、Actor Session、成员管理、Task/Job PostgreSQL、私有对象存储与 append-only 审计底座

## 环境

- Windows PowerShell
- Python：`backend/.venv/Scripts/python.exe`
- PostgreSQL/pgvector：`pgvector/pgvector:0.8.5-pg17-bookworm`
- 本地端口：`127.0.0.1:55433`
- Alembic Head：`20260730_0009`

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

- 463 tests；
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
- Alembic 不是 `20260730_0009` 时阻止发布；
- 远程对象存储 Endpoint 使用明文 HTTP 时阻止发布（localhost 开发目标除外）；
- 数据库 URL、Embedding Key、S3 Key/Secret 和供应商错误正文不进入公开报告。

当前 `CURRENT_SERVER_CUTOVER_CAPABILITIES` 的预期结果仍是 no-go。本文没有把
Runbook 的存在描述成“备份已完成”；真实恢复证据、RPO/RTO 和生产供应商待外部环境。

## 诊断记录

第一次完整回归未指定 CI Config，2 个既有 Humanize 测试读取本机默认
`D:\article\...` Prompt 路径而失败。第二次把 `config.ci.yaml` 写成相对路径，但命令工作目录为
`backend`，因此被解析为不存在的 `backend/config.ci.yaml`。

最终使用仓库根目录的绝对 Config 路径后，424 tests 全部通过。这两个失败属于验证命令环境，不是 M7 Schema、权限或审计代码失败。

## 当前未验证或未接入

- 已有 Actor Session Codec，但正式身份来源与登录签发入口尚未接入；
- RBAC 尚未接入 FastAPI 路由、Knowledge Retriever 或 Worker；对象下载服务底层已重新授权，但尚无公开 HTTP 路由；
- `app.py` 的 Task/Job 仍以 SQLite 为准；PostgreSQL 实现尚未成为服务器单写准源；
- SQLite Terminal Job 历史导入已实现；切换双读报告和 `app.py` 单写切换尚未实现；
- S3 对象存储底层与产品资产桥接已实现；备份恢复和部署门禁尚未实现；
- 本阶段未修改前端，因此没有新增 M7 前端验收项。

这些项目属于后续 M7-B/C/D，不得把本记录描述为“多人服务器版已上线”。
