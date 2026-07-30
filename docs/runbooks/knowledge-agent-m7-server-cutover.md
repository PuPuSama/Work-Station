# Knowledge Agent M7：服务器切换、备份恢复与回滚 Runbook

## 1. 目的和硬门禁

本文是 M7 正式服务器切换的操作准源。它不表示当前系统已经可以上线。
任何一项未满足都停止切换：

1. `CURRENT_SERVER_CUTOVER_CAPABILITIES` 没有缺项；
2. 正式身份来源已完成 Subject -> Workspace User 映射；
3. 所有项目级 HTTP 路由、Retriever、Worker 和对象下载均重新授权；
4. Task/Job 已完成双读比对并准备 PostgreSQL 单写；
5. PostgreSQL 与对象存储的备份恢复演练已有本次发布对应的日期、操作者和证据；
6. 对象 Bucket 私有、启用服务端加密，并已确定版本、生命周期和异地备份策略；
7. 回滚负责人、维护窗口、RPO 和 RTO 已明确。

当前代码会让 `server_cutover` 检查失败。这是有意的 fail-closed 状态，不能通过
修改前端或传入请求字段绕过。

## 2. Preflight 接口和安全输出

位置：服务器发布候选目录的 `backend`。

```powershell
$env:ARTICLE_AGENT_CONFIG = `
  'D:\Project\article\article-agent-formal\config.ci.yaml'
.\.venv\Scripts\python.exe -m knowledge_agent.m7_deployment_preflight
```

只有在本次数据库和对象恢复演练证据已经人工复核后，才允许增加：

```powershell
.\.venv\Scripts\python.exe -m knowledge_agent.m7_deployment_preflight `
  --backup-restore-drill-passed
```

退出码：

- `0`：全部门禁通过；
- `2`：至少一项不通过；
- 其他非零值：命令环境或程序异常，同样停止发布。

输出只允许包含 `ready`、Check ID、布尔状态和固定说明。不得输出数据库 URL、
Session Secret、Embedding Key、对象存储 Key、供应商响应正文或客户内容。

## 3. PostgreSQL 备份与恢复演练

以下命令在持有备份权限的服务器终端执行。连接凭据通过受控 Secret 注入，
不要写进命令历史、仓库或报告。

### 3.1 备份

1. 记录发布候选 Commit、Alembic Head、UTC 时间和数据库实例标识。
2. 在一致性快照窗口暂停写入口和 Worker Claim。
3. 使用与 PostgreSQL 17 兼容的 `pg_dump`：

```powershell
pg_dump --format=custom --no-owner --no-acl `
  --file=article-agent-before-m7.dump `
  $env:ARTICLE_AGENT_BACKUP_DATABASE_URL
```

4. 对 Dump 文件计算 SHA-256，保存到受控发布证据，不在聊天或普通日志中上传数据库内容。
5. 恢复写入口前，记录对象存储 Inventory/版本水位，形成同一恢复点的证据对。

### 3.2 恢复验证

必须恢复到新的隔离数据库，禁止覆盖开发库或生产库：

```powershell
createdb article_agent_restore_drill
pg_restore --clean --if-exists --no-owner --no-acl `
  --dbname=$env:ARTICLE_AGENT_RESTORE_DATABASE_URL `
  article-agent-before-m7.dump
```

恢复后至少验证：

- `alembic_version = 20260730_0011`；
- `vector` 扩展存在；
- Organization、Project Ownership、Membership、Audit、Knowledge、
  External Identity、Task、Batch、Job 表均可读取；
- 复合租户外键仍存在；
- Audit Event 更新和删除仍被 Trigger 拒绝；
- Task/Job 迁移工具的数量、状态分布和内容 SHA-256 摘要一致；
- 抽样知识资产 URI 能在恢复用对象存储中读取，下载字节 SHA-256 与数据库一致。

演练完成后删除隔离恢复环境时，先再次确认目标实例标识；不要对未确认路径或共享实例
执行清理命令。

## 4. 对象存储备份与恢复

正式 Bucket 必须：

- 阻止公共访问，不使用长期公共 URL；
- 默认服务端加密，生产不使用 `ARTICLE_AGENT_OBJECT_STORE_SSE=none`；
- 启用供应商支持的对象版本或不可变备份；
- 设置异地复制/备份与生命周期，生命周期不得早于数据库证据保留期；
- 保存 Bucket Policy、加密 Key 策略和 Inventory 配置的版本化副本。

产品图片对象使用：

```text
organizations/{organization_id}/projects/{project_id}/blobs/{prefix}/{sha256}
```

恢复演练不能只验证“对象数量”。从 PostgreSQL 抽取一组
`knowledge_assets.artifact_uri + content_hash`，在隔离 Bucket 恢复并重新计算
SHA-256。至少覆盖产品主图、Gallery 图、私有文档和标准化产物。

因为 S3 与 PostgreSQL 没有跨系统事务，备份窗口必须记录数据库快照时间和对象
Inventory/版本水位。恢复时先保持应用离线，完成 URI 存在性和哈希抽样，再开放读流量。

## 5. Secret 与加密 Key 轮换

### Actor Session Secret

当前 Codec 只接受一个签名 Secret。轮换会让旧 Session 全部失效：

1. 公告重新登录窗口；
2. 注入新的 `ARTICLE_AGENT_SERVER_SESSION_SECRET`；
3. 滚动重启全部实例；
4. 确认旧 Token 被拒绝、新 Token 可解析；
5. 从 Secret Manager 撤销旧版本。

在实现双 Key 验签前，不做无感轮换承诺。

### S3 Access Key

1. 创建权限相同的新 Key，不立即删除旧 Key；
2. 用新 Key 执行 `HeadBucket`、测试前缀 Put/Get/Delete；
3. 更新 Secret Manager 并滚动重启；
4. 检查错误率和对象访问审计；
5. 撤销旧 Key，再运行一次只读 Preflight。

KMS Key 轮换遵循供应商策略；先验证旧对象仍可解密。不得把删除旧 Key 当作普通
应用回滚动作。

## 6. 发布和回滚顺序

发布顺序：

1. 备份与恢复演练；
2. `alembic upgrade head`；
3. 停止 SQLite 写入口和 Worker，完成一次性迁移；
4. 对每个项目运行 `m7_cutover_report` 并保存 matched JSON；
5. 只读 Preflight；
6. API/Worker 部署但保持流量关闭；
7. 身份、项目 Scope、对象下载、Retriever、Task/Job 冒烟；
8. 小流量开放；
9. 观察期结束后才关闭旧服务器写路径。

对象下载冒烟必须通过
`GET /api/projects/{project}/assets/{asset_id}/download`，验证 URL 过期时间不超过
3600 秒；同时使用另一 Project 的 Actor 和一条错误 Organization Key 前缀的测试资产，
分别确认 403 与 404。不要把签名 URL 写入长期发布证据或普通日志。

产品重新发现冒烟必须通过
`POST /api/projects/{project}/tasks/{task_id}/product-rediscovery`，随后只使用响应中的
Job ID 调用
`GET /api/projects/{project}/tasks/{task_id}/product-rediscovery/jobs/{job_id}`。
至少验证：

- Viewer 返回 403；具备 `knowledge.edit` 的 Editor 才能入队；
- 请求只包含当前 Task Revision、官网内 Category URL 和 1–50 的抓取上限；
- Job 行保存可信 `requested_by_user_id`，公开响应不包含 Request、Requester、原始错误
  或对象 URI；
- 撤销 Requester 后，尚未执行的 Job 进入通用 conflict，Worker 不读取/执行私有请求；
- 抓取只使用 Active Project 的 `official_domain` 和 Organization/Project 绑定的私有
  S3 前缀，不创建本地 JSON、SQLite 或 Artifact 目录；
- 成功、失败或取消都不修改 Task Revision 和当前产品；新证据留在 Inbox，审核发布后
  才能由产品替换接口选择；
- 对象存储配置缺失时新 Job 返回 503，但已有 Job 的状态仍可读取；
- 重启恢复只处理 Active `product_rediscovery` Job，不得把旧无 Requester 历史重新执行。

当前 Runner 只在整次官网同步前后检查取消，产品明细循环中没有逐项取消点。发布窗口必须
允许在途抓取自然结束；在补齐 drain/join 证明前，不把进程 `stop()` 当作强制中断，也不把
这一条 Operation 的接线写成整体 Worker Cutover 完成。

产品替换冒烟必须通过
`PUT /api/projects/{project}/tasks/{task_id}/products`，请求只包含当前 Task Revision
和 1–3 个 Product ID。至少验证：

- 候选产品已经重新抓取并审核，当前 Primary Detail Evidence 含
  `selection_projection.schema_version=1`；旧 Evidence 不允许回退读取可变目录 Metadata；
- Viewer 返回 403，Editor 可提交；
- 未确认产品、另一 Project 产品、没有 Published Current Snapshot 主详情证据的产品
  返回同类不可选择错误；
- 成功响应只保存正式目录事实和 `selected_asset_id`，不含对象 URI、源站图片 URL 或
  本地路径；
- 模拟未发布刷新修改 `knowledge_products.metadata` 后，Task 仍只得到已发布快照的
  Evidence Projection；
- 重复提交旧 Revision 返回 409；
- 图片展示继续单独调用授权下载路由，不能把签名 URL 回写 Task。

章节替换冒烟必须通过
`PUT /api/projects/{project}/tasks/{task_id}/article/sections`。至少验证：

- Viewer 返回 403，Editor 只在 `ACTION_UPDATE_ARTICLE` 允许时提交；
- 不存在或重复的 Heading Path、同级/更高级 Heading 注入返回 422；
- fenced code block 中的 `##` 不被识别为章节边界；
- 成功后只有目标 Section Body 变化，相邻章节和目标 Heading 保持不变；
- `article_versions` 原子追加 `before_section_rewrite` 与 `section_rewrite`，下游人化、
  链接、图片和导出状态失效；
- 重复提交旧 Revision 返回 409，且不多追加版本；
- 本接口不调用 LLM；对话式生成只能提供候选 Body，不能直接写完整 Article。

回滚原则：

- 代码回滚优先，数据库迁移只在确认新表没有新业务数据时降级；
- 已写入 PostgreSQL 的服务器 Task/Job 不回灌 SQLite；
- 已写入的内容寻址对象保留，依靠引用对账后延迟清理；
- 身份或 Scope 异常立即关闭服务器入口，不退化为默认 Actor/默认 Project；
- 数据损坏才使用已验证的数据库和对象恢复点，不能用未经演练的备份覆盖生产。

## 7. 发布证据模板

每次候选发布保留：

```text
commit:
operator:
utc_started:
utc_completed:
database_backup_sha256:
database_restore_target:
database_restore_checks:
object_inventory_or_version_watermark:
object_restore_sample_count:
object_restore_hash_matches:
preflight_report_artifact:
rpo:
rto:
rollback_owner:
decision: go | no-go
```

模板中的空值代表门禁未完成，不能写“默认通过”。
