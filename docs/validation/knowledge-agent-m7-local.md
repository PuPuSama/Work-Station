# Knowledge Agent M7 持续实施本地验证记录

- 日期：2026-07-31
- 分支：`feature/knowledge-agent-m7`
- 基线：`cc4bbf2 feat: add M6 retrieval evaluation framework`
- 范围：多租户 Schema、项目 RBAC、Actor Session、成员管理、Task/Job PostgreSQL、私有对象存储与 append-only 审计底座

## 环境

- Windows PowerShell
- Python：`backend/.venv/Scripts/python.exe`
- PostgreSQL/pgvector：`pgvector/pgvector:0.8.5-pg17-bookworm`
- 本地端口：`127.0.0.1:55433`
- Alembic Head：`20260731_0013`

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

- 22 tests；
- Actor Token 防篡改、过期、未来签发与 Secret 隔离；
- Token 只保存 Organization/User，不保存 Role 或 Permission；
- 纯权限矩阵通过；
- 真实 PostgreSQL 跨组织访问拒绝；
- 普通 Team Member 无隐式项目权限；
- 禁用 User、未绑定旧 Project fail closed；
- 归档 Project fail closed；
- ProjectMembership 复合外键拒绝跨组织组合；
- Audit Writer 在业务事务内追加；
- Trigger 拒绝 Audit Event 更新和删除。
- 成员授权/撤销与 Audit Event 同事务；
- 重复 Event ID 会回滚同事务内的成员角色更新。

### ProjectMembership HTTP 与授权事实锁

```powershell
$env:ARTICLE_AGENT_DATABASE_URL = '<由安全环境注入>'
.\.venv\Scripts\python.exe -m unittest `
  tests.test_m7_project_membership_http `
  tests.test_m7_access_control `
  tests.test_m7_access_control_postgres `
  tests.test_m7_server_request_security -v
```

结果：

- 29 tests；
- GET Roster 只返回当前 Project 的显式成员，按 `user_id` 稳定分页，1–100 条有界；
- Roster 不伪造继承角色，保留 Disabled User 的既有成员行供撤销；
- PUT 只接受 `editor/reviewer/viewer` 和无额外字段的 Body，DELETE 为项目级幂等撤销；
- 未认证 401，Editor 403，Owning Team Lead 可管理成员；
- 跨 Organization Project 返回 403，跨 Organization Target 返回 404；
- 授权/撤销只产生固定 Audit Action，重复撤销不伪造事件；
- Audit Writer 故障返回脱敏 503，Membership 与 Audit 同事务回滚；
- 成员写事务锁定 Actor 的可撤权授权事实；并发 Team Lead 降级在事务完成前因锁超时
  被拒绝，排除 check-then-revoke 竞态；
- Server Mode 白名单只开放精确 GET Roster 与 PUT/DELETE 成员路径，POST 和其他未迁移
  变体继续拒绝。

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

Job Requester 迁移新增后，在 `background_jobs` 为 0 行时执行：

```powershell
.\.venv\Scripts\alembic.exe downgrade 20260730_0010
.\.venv\Scripts\alembic.exe upgrade head
.\.venv\Scripts\alembic.exe upgrade head
.\.venv\Scripts\alembic.exe current
```

结果为 `20260730_0011 (head)`；降级、升级和重复升级均成功。

Object Orphan Observation 迁移新增后执行：

```powershell
.\.venv\Scripts\alembic.exe upgrade head
.\.venv\Scripts\alembic.exe upgrade head
.\.venv\Scripts\alembic.exe current
```

结果为 `20260730_0012 (head)`；空库升级与降级往返在最终回归前再次验证。

Actor Session Version 迁移新增后执行：

```powershell
.\.venv\Scripts\alembic.exe downgrade 20260730_0012
.\.venv\Scripts\alembic.exe upgrade head
.\.venv\Scripts\alembic.exe upgrade head
.\.venv\Scripts\alembic.exe current
```

结果为 `20260731_0013 (head)`；降级、升级和重复升级均成功。

### Task/Job PostgreSQL 定向测试

```powershell
.\.venv\Scripts\python.exe -m unittest `
  tests.test_m7_postgres_tasks -v
```

结果：

- 18 tests；
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
- 新 Job Requester 通过 `(organization_id, requested_by_user_id)` 复合外键锁在同一
  Organization；
- SQLite Terminal History 导入强制把 Requester 留空，不信任旧 Payload 的同名扩展字段；
- 无 Requester 的旧历史 Job、禁用 User 或已失权 User 在 Worker 获取私有 Request
  前变为通用 conflict；
- Claim 成功后再撤权，Handler 前的第二次授权仍阻止业务执行；
- Operation 分别映射 `article.edit`、`article.review`、`article.deliver`、
  `knowledge.edit`，不使用请求 Body 中的 Role；
- 两个授权 Worker 并发 Claim 仍得到互不重叠的 Job，保留 `SKIP LOCKED` 语义。

### 完整后端回归

```powershell
$env:ARTICLE_AGENT_CONFIG = `
  'D:\Project\article\article-agent-formal\config.ci.yaml'
# ARTICLE_AGENT_DATABASE_URL 由本地安全环境提供，不写入仓库或终端输出。
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

结果：

- 560 tests；
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

### 对象 Orphan 对账与延迟清理

```powershell
.\.venv\Scripts\python.exe -m unittest `
  tests.test_m7_object_store `
  tests.test_m7_object_orphan_reconciliation -v
```

结果：

- 12 tests，使用真实 PostgreSQL 和确定性内存 Object Inventory；
- S3 List 分页、排序和稳定异常通过；
- 未登记 Physical Orphan 与已登记但未引用 Asset 均能进入观察；
- Snapshot Raw/Normalized URI、Snapshot Asset Link 和 Task `*_asset_id` 组成联合存活
  集合，任一引用存在都不删除；
- 其他 Organization/Project 前缀不扫描，跨组织 Actor 被拒绝；
- 默认 7 天、硬性最短 24 小时、双观察和 Fingerprint 不变门禁通过；
- Cleanup 前重新授权并重算引用；观察后被 Task 复用的 Asset 保留；
- Provider Delete 失败不泄露供应商正文，并以新的一次观察重新开始保留窗口；
- Audit Details 只有数量和保留秒数，不含对象 Key 或 URI。

### Server 私有文章图片派生

```powershell
$env:ARTICLE_AGENT_DATABASE_URL = '<由安全环境注入>'
.\.venv\Scripts\python.exe -m unittest `
  tests.test_m7_server_article_images `
  tests.test_m7_knowledge_object_storage `
  tests.test_m7_server_project_tasks `
  tests.test_m7_server_request_security -v
```

结果：

- 27 tests，使用确定性内存图片和真实 PostgreSQL，不调用外部图片或模型服务；
- Hero 使用请求中的项目 Asset ID；产品图只能来自 Task 当前
  `Product.selected_asset_id`，客户端不能替换为任意项目图片；
- 源对象读取再次要求 `article.edit`，并验证 Bucket、Organization/Project Key、
  Byte Size 和 SHA-256；幂等写入命中已有资产时也重新验证 Key Scope；
- Pillow 在内存完成格式验证、EXIF 方向校正、动画首帧、像素门禁和确定性 WebP 派生，
  不创建本地图片目录；
- SHA-256 精确去重和 dHash + RGB RMS 视觉近重复门禁生效，含 Hero 最多三张；
- 自动锚点沿用最小 H2/H3 文章块规则；未解析时返回非 FAQ 候选且零派生上传，人工锚点
  只能引用当前 Task Product ID；
- 成功 Task 只保存 `source_asset_id`、`prepared_asset_id`、派生哈希、尺寸、Marker、
  `anchor_line/anchor_match` 等可重构诊断，两个 Path 为空且不泄露对象 URI；
- 共享派生资产元数据不保存文章角色、产品或来源关系，避免内容去重复用后残留第一次
  使用者的关系；这些关系只保存在 Task `ArticleImage`；
- Viewer 返回 403；旧 Revision 在对象读取前返回 409；派生对象仍经授权下载路由访问；
- Server TDK 与 Delivery ZIP 已对象化；派生 orphan 已进入双观察延迟对账；该 Task
  写操作已进入事务内 Audit。

### Server 私有文章 DOCX

```powershell
$env:ARTICLE_AGENT_DATABASE_URL = '<由安全环境注入>'
.\.venv\Scripts\python.exe -m unittest `
  tests.test_m7_server_docx_export `
  tests.test_m7_knowledge_object_storage `
  tests.test_m7_server_project_tasks `
  tests.test_m7_server_request_security -v
```

结果：

- 28 tests，含纯内存 DOCX 单元测试和真实 PostgreSQL + FastAPI 路由；
- `article.deliver` 在路由、私有 WebP 读取、DOCX 写入和专用下载签名前重新检查；
- Task 图片 Asset 的对象 Key、大小、哈希、类型、数据库/Task/实际尺寸全部复核；
- 现有 Word 排版器新增内存 WebP/字节输出边界，本地文件导出测试保持通过；
- 生成的 DOCX ZIP 结构含两张 WebP、正文和 Marker，Task 目录始终未创建；
- Task 只保存 `docx_asset_id/docx_content_hash/docx_filename`，`docx_path` 为空；
- Viewer 导出/专用下载返回 403；通用 Asset 下载对 `article_docx` 返回 404；
- 同哈希资产已属于其他访问类型时在 Task CAS 前 fail closed，不降级下载权限；
- 旧 Revision 在对象读取/写入前返回 409，未产生额外对象；
- Delivery ZIP 与窄范围 Server Delivery Console 已由后续切片接通。

### Server 私有 TDK DOCX

```powershell
$env:ARTICLE_AGENT_DATABASE_URL = '<由安全环境注入>'
.\.venv\Scripts\python.exe -m unittest `
  tests.test_tdk `
  tests.test_m7_server_tdk_export `
  tests.test_m7_knowledge_object_storage `
  tests.test_m7_server_project_tasks `
  tests.test_m7_server_request_security -v
```

结果：

- 32 tests，包含纯内存 TDK Word 单元测试和真实 PostgreSQL + FastAPI 路由；
- Local `export_tdk_docx()` 与 Server `build_tdk_docx_bytes()` 复用同一排版核心；
- Server 要求已有 `docx_asset_id`，从当前文章生成并验证 Title、Description 和六个
  Keyword，不接受客户端 TDK、Prompt、Asset ID、对象 URI 或输出路径；
- LLM/Provider Runtime Error 统一映射为不含供应商正文和 Key 的 503；
- `D.docx` 在内存生成并保存为内容寻址 `tdk_docx`，Task 只保存
  `tdk_asset_id/tdk_content_hash/tdk_filename`，`tdk_path` 为空；
- Viewer 生成/专用下载返回 403；通用 Asset 下载与文章 DOCX 专用下载不能取得 TDK；
- 旧 Revision 在 LLM 和对象写入前返回 409；成功 Task CAS 产生
  `article.tdk.generated` Audit，Details 只含字符数和关键词数量；
- Delivery ZIP 与窄范围 Server Delivery Console 已由后续切片接通；orphan 已进入
  双观察延迟对账。

### Server 最终 AI-rate Review

```powershell
$env:ARTICLE_AGENT_DATABASE_URL = '<由安全环境注入>'
.\.venv\Scripts\python.exe -m unittest `
  tests.test_m7_server_ai_screenshots `
  tests.test_m7_knowledge_object_storage `
  tests.test_m7_server_project_tasks `
  tests.test_m7_server_request_security `
  tests.test_state_machine `
  tests.test_workflow_api -v
```

结果：

- 77 tests，包含纯内存截图规范化、本地工作流兼容和真实 PostgreSQL + FastAPI 路由；
- `article.review` 与 `article.deliver` 分离：Reviewer 可上传、确认、查看 Review
  Screenshot，但不能导出文章 DOCX/TDK；
- 截图在内存执行大小、像素、解码和 EXIF 门禁，并重编码为无元数据 PNG；
- AICheck 只保存 `screenshot_asset_id/screenshot_content_hash/screenshot_filename` 和
  Width/Height，`screenshot_path` 为空；
- confirmed=true 必须已有 Screenshot Asset，成功确认把分数/报告绑定当前
  Humanized Article 哈希并推进 `final_ai_checked`；
- 通用 Asset 下载隐藏 `final_ai_rate_screenshot`；专用下载重新授权并复核 Task/Asset
  哈希、尺寸和访问分类；
- 旧 Revision 在读取 multipart 字节前返回 409，跨 Project 与 Viewer 均返回 403；
- 两个 Audit Action 只保存截图尺寸、confirmed 和是否有 score，不含 Report、score 值、
  文章正文、图片字节或 URL；
- Delivery ZIP 与窄范围 Server Delivery Console 已由后续两节接通；orphan 已进入
  双观察延迟对账。

### Server 私有 Delivery ZIP

```powershell
$env:ARTICLE_AGENT_DATABASE_URL = '<由安全环境注入>'
.\.venv\Scripts\python.exe -m unittest `
  tests.test_m7_server_project_tasks `
  tests.test_m7_server_request_security `
  tests.test_m7_server_delivery_package `
  tests.test_delivery_package `
  tests.test_m7_knowledge_object_storage `
  tests.test_state_machine
```

结果：

- 63 tests，使用确定性内存文件和真实 PostgreSQL，不调用外部模型或对象服务；
- `article.deliver` 在路由、全部私有对象读取、ZIP 上传和专用下载签名前重新检查；
- 打包只接受当前 Task 的文章 DOCX、TDK DOCX、Prepared WebP 和终审 Screenshot
  身份，不接受客户端 Asset ID、对象 URI、路径、文件名或文件字节；
- confirmed 终审的 `article_hash` 必须匹配当前 Humanized Article，正文改变后旧确认
  无法打包；
- ZIP 在内存生成，条目扁平且固定为文章 DOCX、`D.docx`、1–3 张 WebP 和
  `final-ai-rate.png`；固定顺序、时间戳、权限及压缩参数使同输入输出稳定；
- Task 只保存 `delivery_package_asset_id/delivery_package_content_hash/
  delivery_package_filename`，`delivery_package_path` 为空；
- 通用 Asset 下载对 `delivery_zip` 返回 404；专用下载要求 `article.deliver` 并复核
  Task Asset ID、内容哈希和访问类型；Viewer 返回 403；
- `article.delivery.packaged` Audit 只保存文件数和图片数，不含正文、文件名、对象 URI
  或签名 URL；
- 上游无效化会同时清空 Delivery 的路径与全部 Server Asset 身份；对象写入后发生 CAS
  冲突产生的内容寻址 orphan 仍待延迟对账。

### Server Project Directory 与 Delivery Console

```powershell
& '<工作区 Node 绝对路径>' node_modules/eslint/bin/eslint.js .
& '<工作区 Node 绝对路径>' node_modules/next/dist/bin/next build
```

结果：

- ESLint 通过；
- Next.js 16.2.10 Production Build 与 TypeScript 通过，静态/动态路由生成完成；
- 首页先读取 `/api/auth/status` 再挂载 Local 或 Server 组件树，Server 模式不会启动
  Local ProjectSelector 的 Dashboard/Config/SQLite Task 请求；
- Server Project Selector 只读取 `/api/projects`，显示 Effective Role，并以
  `project_id` 直达 Delivery；无项目、加载、失败与重试状态均有文字反馈；
- Project Shell 在 Server 模式只显示 Delivery，不挂载 Local Job Center、Article、
  Batch 或 Settings；Local 模式保持原导航；
- Delivery Console 以 `*_asset_id` 识别 Server DOCX/TDK/Review/ZIP，以 `*_path`
  识别 Local 产物；打包发送当前 Revision，成功后重载 Task；
- Reviewer/Viewer 的交付动作按角色隐藏或禁用，错误 Alert 可被辅助技术宣告；后端
  路由和对象服务仍是实际安全准源；
- Server 下载先调用 Task-scoped 接口取得短期 URL；前端不显示或拼接 Bucket、Object
  Key、URI 或通用 Asset 下载；
- 过滤器具备 Pressed/Focus 状态，异步按钮禁用并显示 Spinner，表格窄屏时可水平滚动；
- 浏览器级假 API 冒烟未计为通过证据：内置浏览器安全层对本地 API 路径返回
  `ERR_BLOCKED_BY_CLIENT`；测试用同源路由、环境文件和后台进程均已删除，未进入差异。

### Server Task CAS 与事务内 Audit

```powershell
$env:ARTICLE_AGENT_DATABASE_URL = '<由安全环境注入>'
.\.venv\Scripts\python.exe -m unittest `
  tests.test_m7_server_task_commands `
  tests.test_m7_postgres_tasks `
  tests.test_m7_server_project_tasks -v
```

结果：

- 34 tests，使用真实 PostgreSQL；
- `PostgresTaskRepository` 保留原 CAS 接口，并新增加入调用方事务的 CAS 边界；
- Writer 先锁 Organization/User/Project 与现有 Project/Team Membership 撤权事实，
  再按 Action 固定的 `article.edit/article.review/article.deliver` 权限决策；
- Task CAS 与 append-only Audit Event 在同一事务；注入 Audit 失败会同时回滚；
- 撤权和旧 Revision 不修改 Task、不产生 Audit；确定性 Event ID 不依赖客户端输入；
- 九条 HTTP Task 写操作分别记录 rewrite/products/section/images/docx/tdk/
  final-ai-screenshot/final-ai-check/delivery-package Action；
- Audit Details 只含 Revision、Status、产品/图片/TDK 数量、Heading 深度、截图尺寸
  或布尔门禁，不含正文、Report 或 score 值；
- 图片/文章 DOCX/TDK DOCX/Review PNG/Delivery ZIP 的 S3 Put 仍不属于 PostgreSQL
  事务，失败后的内容寻址 orphan 由双观察延迟对账清理。

### Product Rediscovery 受控停机与终态 Audit

```powershell
$env:ARTICLE_AGENT_DATABASE_URL = '<由安全环境注入>'
.\.venv\Scripts\python.exe -m unittest `
  tests.test_job_queue `
  tests.test_m7_postgres_tasks `
  tests.test_m7_server_project_tasks `
  tests.test_m7_server_request_security -q
```

结果：

- 45 tests，使用真实 PostgreSQL 和确定性 Handler；
- Runner 停止新 Claim，并把协作式停机从用户取消语义中分离：无取消请求的在途 Job
  释放为 `queued`，新 Registry 可继续执行；
- 非协作 Handler 超过有界等待时间时返回 `remaining_jobs > 0`，不得宣称已排空；
- `succeeded/failed/conflict/cancelled` 与 `background_job.terminal` Audit 同事务；
- 注入 Audit 失败会回滚终态，异常正文不写入 Job 或公开响应，Claim 可安全释放重试；
- Product Rediscovery Enqueue Audit 与 Terminal Audit 均不含 Category URL、Request、
  对象 URI、Provider 响应或原始异常。

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
- 当前缺少剩余路由 Scope、Task/Job 单写或通用 Worker 授权时 fail closed；
- `trusted_identity_source` 已由真实 OIDC/JWKS/PKCE/State/Nonce 登录链支撑为 true；
- OIDC 配置不完整或实时 Discovery/JWKS 探测失败时 Preflight 单独 fail closed；
- `object_download_reauthorizes` 已由真实 HTTP 路由与签名前二次授权支撑为 true，
  不再只是未接线的底层 Service；
- Alembic 不是 `20260731_0013` 时阻止发布；
- 远程对象存储 Endpoint 使用明文 HTTP 时阻止发布（localhost 开发目标除外）；
- 数据库 URL、Embedding Key、OIDC Client Secret、Token、S3 Key/Secret 和供应商
  错误正文不进入公开报告。

当前 `CURRENT_SERVER_CUTOVER_CAPABILITIES` 的预期结果仍是 no-go。本文没有把
Runbook 的存在描述成“备份已完成”；真实恢复证据、RPO/RTO 和生产供应商待外部环境。

### OIDC/JWKS 身份登录

```powershell
$env:ARTICLE_AGENT_DATABASE_URL = '<由安全环境注入>'
.\.venv\Scripts\python.exe -m unittest `
  tests.test_m7_oidc_identity `
  tests.test_m7_external_identity `
  tests.test_m7_server_auth -v
```

结果：

- 24 tests；
- 供应商配置 all-or-nothing，远程 HTTP Issuer 与外部 Post-login Redirect 被拒绝；
- Authorization Code + PKCE S256、HMAC State、Nonce 和本地 Next Path 端到端通过；
- RSA/JWKS 签名、Issuer、单一 Audience、exp/iat、Nonce、Subject 与 `azp` 门禁通过；
- HS256、错误 Claim、过期/篡改 State、Callback 重放和开放重定向 fail closed；
- Provider 取消/拒绝授权只返回统一失败并删除 State Cookie，不回显错误说明；
- 未知 `kid` 只强制刷新一次 JWKS，覆盖 Provider Signing Key 轮换；
- Provider 错误正文、ID Token 与 Client Secret 不进入公开异常或 HTTP 响应；
- 外部 Role/Group 不进入 `VerifiedExternalIdentity` 或 Actor Session；
- Server Lifespan 只构建惰性 OIDC Client，启动与 `/api/auth/status` 不依赖外部网络；
- 登录页 Server Mode 只显示组织身份按钮，本地模式保留密码，状态失败不降级；
- 具体生产 Provider 的真实 Redirect 注册与 Conformance 冒烟尚未执行。

### Actor Session Version 与全会话撤销

```powershell
$env:ARTICLE_AGENT_DATABASE_URL = '<由安全环境注入>'
.\.venv\Scripts\python.exe -m unittest `
  tests.test_m7_actor_sessions `
  tests.test_m7_server_auth `
  tests.test_m7_external_identity `
  tests.test_m7_server_request_security `
  tests.test_m7_oidc_identity -q
```

结果：

- 39 tests，使用真实 PostgreSQL 和确定性 OIDC/Session 依赖；
- v2 Cookie 只保存 Organization/User、时间和正整数 Session Version，不保存 Role、
  Permission、Email、Group 或外部 Token；
- 当前版本 Cookie 可用；Org Admin 递增版本后旧 Cookie 立即失效，新版本 Cookie 可用；
- User Disabled、Organization Suspended、版本不匹配或版本读取故障统一为 401，并在
  Project 权限查询前停止；
- 跨 Organization、普通 Member 和不存在目标的全会话撤销统一拒绝；
- Session Version 更新与真实 PostgreSQL Audit 在同一事务可见；事务回滚后二者均消失；
- 注入 Audit 故障会回滚版本，公开异常不包含底层 Secret 正文；
- 数据库 CHECK 拒绝零或负数 Session Version；
- Org Admin HTTP 命令只接受路径 Organization/User 与空 Body；传入
  `session_version` 返回 422，成功响应不回传内部版本；
- 非 Admin、跨 Organization 与缺失目标统一 403；旧 Cookie 在撤销后访问 Project
  Directory 返回 401；

### Server Request Security 与 Knowledge Router

```powershell
$env:ARTICLE_AGENT_DATABASE_URL = '<由安全环境注入>'
.\.venv\Scripts\python.exe -m unittest `
  tests.test_m7_server_request_security `
  tests.test_m7_server_auth `
  tests.test_auth -v
```

结果：

- 18 tests；
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

### Server Project Task Scoped API

```powershell
$env:ARTICLE_AGENT_DATABASE_URL = '<由安全环境注入>'
.\.venv\Scripts\python.exe -m unittest `
  tests.test_m7_server_project_tasks `
  tests.test_m7_server_request_security -v
```

结果：

- 18 tests；
- 无 Actor Cookie 返回 401，跨 Organization Project 返回统一 403；
- Project Directory 只返回 Actor 在当前 Organization 可见的 Active Project 和
  Effective Role；普通 Team Member 不会看到同 Team Project；
- Project A 的列表只返回 Project A 的 PostgreSQL Task；
- 用 Project A 路径请求 Project B Task 返回 404，不跨 Scope 查找；
- 归档 Project 从 Directory 消失且 Task 路由返回 403；禁用 User 的 Cookie 在
  Session Version/Active User 门返回 401；
- 私有资产下载同时验证 Actor、Project、数据库 Asset Scope、Bucket 和对象 Key
  的 Organization/Project 前缀；
- 正常资产只返回 30–3600 秒的签名 URL；缺失资产、伪造为另一 Organization Key 的
  URI 返回 404，跨项目请求返回 403；
- 旧 `/api/tasks` 在 Server Mode 继续返回 503；
- 五条受限操作均有精确白名单：“重新发现产品”“选择已确认产品”
  “快照后替换一个已审阅章节”“准备私有文章图片”和“导出私有文章 DOCX”；
  “完全重写”是额外迁移入口，尚未迁移的其他 POST/PUT 写方法不进入
  Server Project Task 白名单；
- “完全重写”要求 `article.edit`；Viewer 返回 403，Editor 成功后 PostgreSQL Revision
  从 0 增至 1，重复提交旧 Revision 返回 409；
- 产品替换请求只接受 Revision 和 1–3 个 Product ID，额外的客户端产品字段返回 422；
- 另一 Project 的产品、未确认产品或没有 Published Current Snapshot 主详情证据的产品
  不可选择；成功投影保留正式名称、Canonical URL、事实、规格与稳定 `asset_id`；
- 即使可变 `knowledge_products` 行被模拟成未审核刷新内容，Task 仍只读取当前已发布
  Evidence 的 `selection_projection v1`，不会带入刷新后的名称、URL 或事实；
- Task 不保存 S3 URI、源站图片 URL 或本地图片路径；重复 Product ID 返回 422，旧
  Revision 返回 409；
- 图片准备只接收 Hero Asset ID 和可选产品锚点；产品图固定读取 Task 已选择 Asset，
  派生 WebP 与视觉去重在内存完成，锚点未解析时不写对象，成功后只保存 Asset 引用；
- DOCX 导出只接收 Revision，读取 Task 已确认 WebP 后在内存排版；Task 不保存 DOCX
  路径，通用 Viewer 下载入口也不能绕过 `article.deliver`；
- 章节重写仅接受 Revision、Heading Path 和 Replacement Body；Viewer 返回 403；
- Fence-aware Parser 忽略代码块中的 Heading，目标不存在/重复以及同级或更高级标题
  注入均拒绝；
- 成功时目标章节以外内容保持不变，同一 Task CAS 原子追加修改前/后 ArticleVersion，
  下游 Humanized/Link/Image/Export 状态失效，旧 Revision 不会多写版本；
- 章节接口不调用 LLM、不写本地 Article Artifact；后续对话 Agent 只能把候选 Body
  送入该确定性提交边界；
- 产品重新发现要求 `knowledge.edit`，Job 固定 Organization/Project/Requester；公开
  状态不返回私有 Request、Requester、原始错误或对象 URI；
- Enqueue 在同一 PostgreSQL 事务内锁定可撤权授权事实和 Task Revision，创建
  Batch/Job 并追加不含 Category URL 的 Audit；Audit 失败会回滚 Batch/Job 且只返回
  通用 503；
- Project A 的 Job 不能从 Project B 访问；撤销 Requester 后，Worker 在读取/执行私有
  Request 前把 Job 标为通用 conflict；
- Worker 从 Active Project 的 `official_domain` 构造官网入口，并把正式同步绑定到
  Project-scoped S3 ArtifactStore；Task Revision 和已选产品在重新发现后保持不变；
- 对象存储未配置时，新重新发现 Job fail closed，但已有 Job 状态仍可读取；非本
  `product_rediscovery` Operation 的 Job 不能通过该状态路由读取；
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
- Preflight Head 已更新为 `20260731_0013`。

## 诊断记录

第一次完整回归未指定 CI Config，2 个既有 Humanize 测试读取本机默认
`D:\article\...` Prompt 路径而失败。第二次把 `config.ci.yaml` 写成相对路径，但命令工作目录为
`backend`，因此被解析为不存在的 `backend/config.ci.yaml`。

最终使用仓库根目录的绝对 Config 路径后，424 tests 全部通过。这两个失败属于验证命令环境，不是 M7 Schema、权限或审计代码失败。

## 当前未验证或未接入

- Actor Session、External Identity Mapping、OIDC/JWKS 验签、PKCE Callback 与登录页
  已接入；Session Version 校验、Org Admin 全会话撤销，以及 ProjectMembership
  授权/撤销 HTTP/事务服务已完成，但成员管理 UI、邀请/Team 管理、具体生产 Provider
  注册、Client Secret 轮换和 Conformance 冒烟尚未执行；
- Knowledge Router 与其内部 Retriever 已接入请求级 RBAC；Project/Article/Task/Batch
  旧路由和通用 Worker 尚未接入；新的项目级 PostgreSQL API 已支持读取、产品重新发现、
  “完全重写”“从正式目录选择已确认产品”“快照后替换一个已审阅章节”、私有图片准备
  和文章 DOCX 导出/下载，但不代表其余旧路由、对话式章节生成或完整写路径已经迁移；
- 私有 Knowledge/Product Asset 已有授权后的短期下载路由；现有 Raw Artifact HTTP
  路由仍是本地文件实现，因此 Server Mode 继续阻断该兼容入口；
- 本地模式的 Task/Job 仍以 SQLite 为准；Server Mode 只有明确迁移的 PostgreSQL
  Task 命令和 `product_rediscovery` Job 为单写，其余路径尚未成为 PostgreSQL 准源；
- Server Mode 已停止 SQLite Queue/Worker；产品重新发现已有项目级 PostgreSQL Runner
  和两阶段授权，Enqueue、该 Operation 的终态 Audit 与有界 drain/join 报告已完成；
  但通用 Server Batch/Runner、全部 Operation 和正式停机演练未完成，不能算作整体
  服务器 Job 单写；
- SQLite Terminal Job 历史导入和冻结窗口双读报告已实现；matched 证据留存流程与
  `app.py` PostgreSQL 单写切换尚未实现；
- S3 对象存储底层、产品资产桥接、文章 DOCX/TDK/Review/Delivery ZIP 私有对象、
  Orphan 双观察延迟清理和 no-go 部署门禁已实现；真实备份恢复演练与生产供应商尚未完成；
- 前端已新增 Server OIDC、SQL Project Directory 与窄范围 Delivery Console，并通过
  lint/build；Article、Batch、Settings 等其余 M7 管理界面尚未接入 Server API。

这些项目属于后续 M7-B/C/D，不得把本记录描述为“多人服务器版已上线”。
