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
- Alembic Head：`20260731_0015`

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

- 30 tests；
- GET Roster 只返回当前 Project 的显式成员，按 `user_id` 稳定分页，1–100 条有界；
- Roster 不伪造继承角色，保留 Disabled User 的既有成员行供撤销；
- Candidate GET 只返回 Active 同组织普通成员，排除 Org Admin、Active Owning Team
  Lead、Disabled User、已有显式成员与跨 Organization 用户；授权后目标立即移出候选，
  Team 归档后原 Lead 重新成为候选；
- PUT 只接受 `editor/reviewer/viewer` 和无额外字段的 Body，DELETE 为项目级幂等撤销；
- 未认证 401，Editor 403，Owning Team Lead 可管理成员；
- 跨 Organization Project 返回 403，跨 Organization Target 返回 404；
- 授权/撤销只产生固定 Audit Action，重复撤销不伪造事件；
- Audit Writer 故障返回脱敏 503，Membership 与 Audit 同事务回滚；
- 成员写事务锁定 Actor 的可撤权授权事实；并发 Team Lead 降级在事务完成前因锁超时
  被拒绝，排除 check-then-revoke 竞态；
- Server Mode 白名单只开放精确 GET Roster 与 PUT/DELETE 成员路径，POST 和其他未迁移
  变体继续拒绝。

### Server Project Membership Console

```powershell
Set-Location D:\Project\article\article-agent-formal\frontend
node .\node_modules\eslint\bin\eslint.js .
node .\node_modules\next\dist\bin\next build
```

结果：

- lint 通过；
- Next.js 16.2.10 production build 与 TypeScript 通过；
- `/settings` 先读取 Auth Status：Local 继续挂载原 Project Settings，Server 只挂载
  Project Membership Console，状态失败不降级；
- Server 侧栏只为 Directory 中的 `org_admin/team_lead` 显示成员入口，后端仍是权限准源；
- Roster/Candidate 支持稳定游标追加，添加、改角色、撤销后重新读取两份第一页；
- Disabled 成员只能撤销；角色选择有可见 Label，关键控件至少 44px，撤销使用可聚焦
  Dialog，错误在对应 Card 就近显示；
- 源码使用移动优先纵向卡片，`md` 起切换三列信息/角色/动作布局；Local UI 未改写。

受控浏览器可以打开本地页面，但其 URL 策略会拦截本机 `/api/*` 请求并返回
`ERR_BLOCKED_BY_CLIENT`，因此本轮没有把 375px 数据态、添加/改角色/撤销点击链路或
Dark Mode 记为浏览器实测通过。这些仍需在允许同源 API 的真实 Server 会话中按 Runbook
冒烟；当前已确认的是静态源码审查、ESLint、TypeScript 和 production build。

### Workspace User Directory 与生命周期命令

```powershell
$env:ARTICLE_AGENT_DATABASE_URL = '<由安全环境注入>'
.\.venv\Scripts\python.exe -m unittest `
  tests.test_m7_workspace_user_http -v
```

结果：

- 5 tests；
- GET/POST/PATCH 只对同 Organization 的 Active Org Admin 开放，未认证为 401、普通
  Member 与跨 Organization 路径为统一 403；
- Directory 按 `user_id` 稳定游标分页，返回 Active/Disabled User、Team/Project
  显式成员计数与 `login_linked` 布尔值，不返回 Session Version 或外部身份内容；
- 创建只接受 User ID、显示名和组织角色，并固定建立 Active 本地 User；重复 ID 为
  409，客户端提交 Session Version 等额外字段为 422；
- 更新显示名、状态和组织角色；禁用与恢复均递增 Session Version，禁用前 Cookie 在
  恢复后仍不能重新使用；
- 最后一个 Active Org Admin 不能被禁用或降级；
- 创建/更新与 Audit Event 同事务；Audit Writer 故障返回脱敏 503，User 状态与版本
  一并回滚，审计 Details 不保存显示名或内部版本；
- Server Mode 白名单只开放精确 Directory GET/POST 与 User PATCH 路径。

### Team Directory、生命周期与 TeamMembership

```powershell
$env:ARTICLE_AGENT_DATABASE_URL = '<由安全环境注入>'
.\.venv\Scripts\python.exe -m unittest `
  tests.test_m7_team_administration_http -v
```

结果：

- 5 tests；
- Team Directory、创建/更新和成员 Roster/PUT/DELETE 只允许同 Organization 的 Active
  Org Admin；普通 Member、跨 Organization 路径和跨 Organization User 均拒绝；
- Team 列表按 `team_id` 稳定分页，返回成员数、Team Lead 数和归属项目数；成员按
  `user_id` 稳定分页并保留 Disabled User 旧成员行供清理；
- `manager_user_id` 只接受同组织 Active User，但不授予项目访问；只有显式
  `team_lead` Membership 在 Active Team 上产生继承权限；
- Team 归档后 Team Lead 立即失去继承访问；归档 Team 不允许新增或修改成员角色，但
  既有成员仍可查看和幂等撤销；
- Team 与 TeamMembership 的创建、更新、归档、授权、改角色和撤销使用固定 Audit
  Action；Audit Writer 故障返回脱敏 503 并回滚 Team/成员写入；
- Server Mode 白名单只开放精确 Team GET/POST、Team PATCH、Member GET 与
  PUT/DELETE 路径。

### Organization Admin Console

```powershell
Set-Location D:\Project\article\article-agent-formal\frontend
node .\node_modules\eslint\bin\eslint.js .
node .\node_modules\next\dist\bin\next build
```

结果：

- ESLint、TypeScript 与 Next.js 16.2.10 production build 通过；
- 新增 `/organization` 静态入口；只有 Auth Status 明确返回已认证 Server
  Organization 时才挂载控制台，失败不回退到 Local API；
- 项目侧栏和 Server Project Directory 只在 SQL Project Role 明确为 `org_admin` 时
  显示组织管理入口；直接访问仍由后端逐请求授权；
- 账号页支持创建本地账号、修改显示名/组织角色、停用/恢复和撤销全部会话；
- Team 页支持创建/归档/恢复、成员 Roster、授予/修改 `team_lead/member` 与撤销；
- Manager 与 Team Lead 文案及操作分离；停用、撤销会话、归档和成员撤销均使用确认
  Dialog，Pending 时禁重，表单均有可见 Label，关键控件至少 44px；
- User/Team/Member 列表继续使用服务端稳定 Cursor 分页，不把前端已加载集合当作完整准源。

当前受控浏览器策略仍会拦截本机 `/api/*`，因此本轮没有把 375px 真实数据态、Dark Mode
对比度或账号/团队完整点击链路记为浏览器实测通过；已确认的是响应式源码审查、
ESLint、TypeScript、production build，以及对应 Organization API 的真实 PostgreSQL
集成回归。正式会话仍需按本 Runbook 冒烟。

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

结果为 `20260731_0015 (head)`；降级、升级和重复升级均成功。

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

- 587 tests；
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
- 十二条 HTTP Task 写操作分别记录 rewrite/title-selection/outline/outline-restore/products/section/images/docx/tdk/
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

### Project-scoped Batch/Job Control

```powershell
$env:ARTICLE_AGENT_DATABASE_URL = '<由安全环境注入>'
$env:ARTICLE_AGENT_CONFIG = '<仓库根目录>\config.ci.yaml'
.\.venv\Scripts\python.exe -m unittest `
  tests.test_m7_server_job_control `
  tests.test_m7_postgres_tasks `
  tests.test_m7_server_request_security -q
```

结果：

- 35 tests，使用真实 PostgreSQL；其中 9 条为新增 Job Control 集成测试；
- 列表与详情在 SQL 中固定 Organization/Project，并只返回
  `product_rediscovery`；Project B、其他 Organization 和未迁移 Operation 不可见；
- Keyset 分页以 `created_at + batch_id` 稳定排序，非法或跨 Scope Cursor 返回 404；
- Viewer 可以读取但不能取消/重试；Editor 的 Project Membership 在路由后被撤销时，
  事务内授权仍返回 403，Job 保持原状态；
- 单 Job/整 Batch 取消、终态 Audit、操作者命令 Audit 与状态变化同一事务；注入 Audit
  故障会回滚且公开异常不含私有故障正文；
- Retry 只重放数据库中原 Request 与 Source Revision；携带覆盖字段的 HTTP Body 返回
  422，合法空 Body 重试不会修改私有命令；
- 公开响应只含稳定 ID、状态、Revision、Attempt、时间戳、取消标记和 `has_error`
  布尔值，不含 Request、Requester、Category URL、原始 Error、Worker Lease 或对象 URI；
- Method + Segment 白名单只开放五条 Project-scoped 路径；旧 `/api/batches`、通用
  Job Detail、任意 Run/Delete 变体继续关闭。

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
- Alembic 不是 `20260731_0015` 时阻止发布；
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
- Preflight Head 已更新为 `20260731_0015`。

### External Identity 管理 HTTP 与组织控制台

```powershell
$env:ARTICLE_AGENT_DATABASE_URL = '<由安全环境注入>'
.\.venv\Scripts\python.exe -m unittest `
  tests.test_m7_external_identity_http `
  tests.test_m7_external_identity -v
```

结果：

- 13 tests，全部通过；
- 列表只允许同 Organization 的 Active Org Admin，按 64 字符稳定 Mapping ID 游标分页；
- 列表、Link/Revoke 响应和公开错误不返回原始 Subject；
- Link 目标必须是同组织 Active User；额外 Role 字段、非法 Issuer、普通 Member 和跨
  Organization 请求 fail closed；
- 同一 Active 映射重复 Link 幂等且不追加第二条 Audit；撤销只接收 Mapping ID，重复
  撤销返回 404；
- Audit Writer 故障返回脱敏 503，并回滚新映射；
- Organization Admin Console 已新增外部身份 Tab；Subject 使用非回显输入，成功后清空，
  列表只保存 Mapping ID，撤销使用确认 Dialog；
- ESLint、TypeScript 与 Next.js production build 通过，`/organization` 路由正常生成。

### Workspace Invitation 与 OIDC 兑换

```powershell
$env:ARTICLE_AGENT_DATABASE_URL = '<由安全环境注入>'
.\.venv\Scripts\python.exe -m unittest `
  tests.test_m7_workspace_invitations `
  tests.test_m7_oidc_identity -v
```

结果：

- 22 tests，全部通过；
- Alembic `20260731_0014` 创建 Invitation 表、状态/接受/过期 CHECK、目标/创建者复合
  租户 FK、Token Hash 唯一约束和每 User/Issuer 单 Pending 索引；
- Admin 签发响应唯一一次返回高熵 Token；数据库只存 SHA-256，列表、撤销、Audit 和
  公开错误不返回 Token/Hash；
- 邀请目录稳定分页；普通 Member、跨 Organization、Disabled Target、非法 Issuer、
  额外 Role 字段和重复 Pending 邀请 fail closed；
- 兑换只接受通过 OIDC 验签的 Issuer/Subject，并要求 Pending、未过期、Active
  Organization/User；过期、撤销、重放、错误 Issuer 和跨组织已有 Mapping 均拒绝；
- External Identity 写入、Invitation Accepted 与 Audit 同一事务；注入 Audit 故障时
  Mapping 与 Invitation 状态共同回滚，公开异常不含私有正文；
- `/api/auth/invitations/prepare` 把 Token 写入短期 HttpOnly/SameSite Cookie；HMAC
  State 只绑定 Token Hash，Callback 前替换 Cookie 会在调用 Token Endpoint 前失败；
- `/accept-invite` 支持粘贴 Token 或 `#token=...` Fragment；Fragment 在首个网络请求前
  从地址栏移除，Token 不进入查询参数或 IdP URL；
- Organization Admin Console 新增邀请签发、一次复制、稳定分页与确认撤销；四个 Tab
  在窄屏换行，ESLint、TypeScript 与 Next.js production build 通过，新增
  `/accept-invite` 静态路由。

当前受控浏览器仍会拦截本机 `/api/*`，因此没有把真实 375px 数据态、剪贴板权限、
Fragment 自动清理后的完整 OIDC 重定向或 Dark Mode 记为浏览器实测通过；这些需在允许
同源 API 的 Server 会话中按 Runbook 冒烟。当前证据为真实 PostgreSQL/HTTP 集成测试、
OIDC MockTransport、源码审查、ESLint、TypeScript 和 production build。

### M7-C Job Control 完整回归

```powershell
$env:ARTICLE_AGENT_CONFIG = '<仓库根目录>\config.ci.yaml'
$env:ARTICLE_AGENT_DATABASE_URL = '<由安全环境注入>'
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
```

结果：

- 596 tests 全部通过，2 tests 按显式外部环境门禁跳过；
- 前端 ESLint、TypeScript 和 Next.js production build 全部通过；
- 首次完整回归被一次更早的故障注入清理失败遗留的 `m7-jobctl-*` Active Test Job
  污染，目标撤权 Job 本身已正确进入 conflict；只清除该精确测试前缀的 15 条 Job 与
  14 条 Batch 后，完整回归通过。没有删除正式或其他测试 Scope 数据。

### M7 Task 标题候选选择

```powershell
$env:ARTICLE_AGENT_CONFIG = '<仓库根目录>\config.ci.yaml'
$env:ARTICLE_AGENT_DATABASE_URL = '<由安全环境注入>'
.\.venv\Scripts\python.exe -m unittest `
  tests.test_m7_server_project_tasks.ServerProjectTaskApiTests.test_server_selects_only_current_title_candidate_with_cas `
  tests.test_m7_server_project_tasks.ServerProjectTaskApiTests.test_server_task_api_is_not_added_to_local_mode `
  tests.test_m7_server_task_commands `
  tests.test_m7_server_request_security -q
```

结果：

- 14 tests 全部通过；
- `PUT /api/projects/{project}/tasks/{task_id}/selected-title` 只接受 Revision 与
  Candidate Index，额外标题字段返回 422；
- 服务端只从当前 PostgreSQL Task 候选取值，Viewer、跨项目、越界与旧 Revision
  fail closed；
- 成功选择会执行 Revision CAS、清空 Outline/Article 下游状态并追加
  `article.title.selected`；Audit 只含 Candidate Count/Index，不含标题正文；
- Local Mode 不挂载该接口；标题生成 `titles` Job 仍因本地客户知识目录依赖保持
  Local Only；
- 完整后端回归 597 tests 全部通过，2 tests 按显式外部环境门禁跳过；
- 前端 ESLint、TypeScript 和 Next.js production build 全部通过；
- Alembic Current 与 Head 均为 `20260731_0015`。

### M7 Task 历史大纲恢复

```powershell
$env:ARTICLE_AGENT_CONFIG = '<仓库根目录>\config.ci.yaml'
$env:ARTICLE_AGENT_DATABASE_URL = '<由安全环境注入>'
.\.venv\Scripts\python.exe -m unittest `
  tests.test_m7_server_project_tasks.ServerProjectTaskApiTests.test_server_restores_only_owned_outline_version_to_draft `
  tests.test_m7_server_project_tasks.ServerProjectTaskApiTests.test_server_task_api_is_not_added_to_local_mode `
  tests.test_m7_server_task_commands `
  tests.test_m7_server_request_security -q
```

结果：

- 14 tests 全部通过；
- 恢复命令只接受 Revision 与 Version Index，额外 Outline 正文返回 422；
- 越界索引返回 404，Article Version 返回 422，Viewer 与跨项目请求 fail closed；
- 成功时只把服务器已有 Outline Version 恢复为新 `outline_draft` Version；当前确认大纲、
  正文、Status 与下游产物保持不变；
- `article.outline_version.restored` Audit 只记录来源类型和索引，不含历史正文；
- 旧 Revision 返回 409 且不追加 Version/Audit；Local Mode 不挂载该接口；
- 完整后端回归 599 tests 全部通过，2 tests 按显式外部环境门禁跳过；
- 前端 Next.js production build、ESLint 与 TypeScript 串行复核全部通过；
- Alembic Current 与 Head 均为 `20260731_0015`。

### M7 Task 大纲草稿与确认

```powershell
$env:ARTICLE_AGENT_CONFIG = '<仓库根目录>\config.ci.yaml'
$env:ARTICLE_AGENT_DATABASE_URL = '<由安全环境注入>'
.\.venv\Scripts\python.exe -m unittest `
  tests.test_m7_server_project_tasks.ServerProjectTaskApiTests.test_server_saves_outline_draft_and_confirmation_with_cas `
  tests.test_m7_server_project_tasks.ServerProjectTaskApiTests.test_server_task_api_is_not_added_to_local_mode `
  tests.test_m7_server_task_commands `
  tests.test_m7_server_request_security -q
```

结果：

- 14 tests 全部通过；
- `PUT /api/projects/{project}/tasks/{task_id}/outline` 只接受 Revision、有界 Markdown
  与 Confirmed 标志，额外 Workflow 字段和空白大纲返回 422；
- Viewer 与跨项目请求 fail closed；草稿保留当前确认大纲/正文，确认后才清空下游；
- 草稿与确认分别追加 `outline_draft` / `outline` 内容哈希 Version，并通过同一
  `article.outline.updated` Audit Action 记录安全计数；
- 旧 Revision 返回 409 且不增加 Version/Audit；Local Mode 不挂载该接口；
- `POST .../outline` 生成端点仍未开放，避免在 Server Mode 复用本地 Prompt/LLM 链；
- 完整后端回归 598 tests 全部通过，2 tests 按显式外部环境门禁跳过；
- 前端 Next.js production build、ESLint 与 TypeScript 串行复核全部通过；
- 首次把 TypeScript 与 Next build 并行运行时，两者竞争 `.next/types` 导致临时
  `routes.js` 缺失；按 build -> lint/typecheck 复核后通过，不属于代码失败；
- Alembic Current 与 Head 均为 `20260731_0015`。

### M7 Project Prompt Snapshot 底座

```powershell
$env:ARTICLE_AGENT_DATABASE_URL = '<由安全环境注入>'
.\.venv\Scripts\python.exe -m unittest `
  tests.test_m7_server_project_prompts `
  tests.test_m7_deployment_readiness `
  tests.test_m7_object_orphan_reconciliation -q
```

结果：

- 18 tests 全部通过；
- Alembic `0015 -> 0014 -> head -> head` 降级、升级与重复升级成功；
- Prompt Head、不可变 Version、精确 Default 指针及其 Project/User 复合 FK 已验证；
- Version UPDATE/DELETE 被数据库 Trigger 拒绝，跨项目 Default Pointer 被 FK 拒绝；
- V1 设为默认后创建 V2，默认仍解析 V1；显式切换后才解析 V2；
- Viewer 可解析但不能写，跨 Project/Organization、旧 Expected Version、Kind 不匹配
  均 fail closed；
- 创建、追加版本、归档/恢复与默认切换写入安全 Audit，Details 不含 Prompt 名称/正文；
- Audit 故障返回脱敏错误并回滚 Prompt Head/Version/Default；
- 完整后端回归 607 tests 全部通过，2 tests 按显式外部环境门禁跳过；
- 前端 Next.js production build、ESLint 与 TypeScript 串行复核全部通过；
- Alembic Current 与 Head 均为 `20260731_0015`。

## 诊断记录

第一次完整回归未指定 CI Config，2 个既有 Humanize 测试读取本机默认
`D:\article\...` Prompt 路径而失败。第二次把 `config.ci.yaml` 写成相对路径，但命令工作目录为
`backend`，因此被解析为不存在的 `backend/config.ci.yaml`。

最终使用仓库根目录的绝对 Config 路径后，424 tests 全部通过。这两个失败属于验证命令环境，不是 M7 Schema、权限或审计代码失败。

## 当前未验证或未接入

- Actor Session、External Identity Mapping、OIDC/JWKS 验签、PKCE Callback 与登录页
  已接入；Session Version 校验、Org Admin 全会话撤销，以及 ProjectMembership
  Roster/Candidate/授权/撤销 HTTP、事务服务与 Project Console 已完成；Workspace User
  和 Team/TeamMembership 后端目录、创建与生命周期命令及 Organization Admin Console
  也已完成，External Identity 与 Workspace Invitation 的目录/管理/OIDC 兑换及 UI
  已接入；但邀请邮件投递、具体生产 Provider 注册、Client Secret 轮换和 Conformance
  冒烟尚未执行；
- Knowledge Router 与其内部 Retriever 已接入请求级 RBAC；Project/Article/Task/Batch
  旧路由和通用 Worker 尚未接入；新的项目级 PostgreSQL API 已支持读取、产品重新发现、
  “完全重写”“从正式目录选择已确认产品”“快照后替换一个已审阅章节”、私有图片准备
  和文章 DOCX 导出/下载，并为 `product_rediscovery` 提供窄范围 Batch/Job 控制；但不
  代表其余旧路由、Operation、对话式章节生成或完整写路径已经迁移；
- 私有 Knowledge/Product Asset 已有授权后的短期下载路由；现有 Raw Artifact HTTP
  路由仍是本地文件实现，因此 Server Mode 继续阻断该兼容入口；
- 本地模式的 Task/Job 仍以 SQLite 为准；Server Mode 只有明确迁移的 PostgreSQL
  Task 命令和 `product_rediscovery` Job 为单写，其余路径尚未成为 PostgreSQL 准源；
- Server Mode 已停止 SQLite Queue/Worker；产品重新发现已有项目级 PostgreSQL Runner
  和两阶段授权，Enqueue、该 Operation 的终态 Audit 与有界 drain/join 报告已完成；
  该 Operation 的 Project-scoped 列表、取消和重试也已完成；但其他 Operation 的
  Server Runner、可信 Enqueue 和正式停机演练未完成，不能算作整体服务器 Job 单写；
- SQLite Terminal Job 历史导入和冻结窗口双读报告已实现；matched 证据留存流程与
  `app.py` PostgreSQL 单写切换尚未实现；
- S3 对象存储底层、产品资产桥接、文章 DOCX/TDK/Review/Delivery ZIP 私有对象、
  Orphan 双观察延迟清理和 no-go 部署门禁已实现；真实备份恢复演练与生产供应商尚未完成；
- 前端已新增 Server OIDC、SQL Project Directory、Project Membership Console、窄范围
  Delivery Console，以及 Organization/Team/User/Session/External Identity/Invitation
  管理和 `/accept-invite`，并通过 lint/build；Article、Batch 与邮件投递等其余 M7
  界面或外部集成尚未接入 Server API。

这些项目属于后续 M7-B/C/D，不得把本记录描述为“多人服务器版已上线”。
