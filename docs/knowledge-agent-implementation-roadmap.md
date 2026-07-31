# 知识库与 LangGraph Agent 实施路线图

## 目标

先交付一个单项目、可运行、可评测、可演示的垂直切片，再扩展成公司多人共享工作台。每个里程碑都要产生前端可见结果或可自动验证的后端能力。

学习实验、正式模块的合入门槛和五周建议节奏见 `docs/agent-learning-and-delivery-plan.md`。第一条真实案例固定为 `www.qewitfastener.com / topic_006`，不能只用模拟数据证明流程可运行。

## 实施状态（2026-07-30）

| 里程碑 | 状态 | 结构记录 |
|---|---|---|
| M0 | 完成 | 基线接口与 feature flag 测试 |
| M1 | 完成 | `docs/knowledge-agent-m1-runbook.md` |
| M2 | 完成正式边界；真实 MinerU 私有样本对比待外部条件 | `docs/architecture/knowledge-agent-m2.md`、`docs/validation/knowledge-agent-m2-qewitfastener.md` |
| M3 | 完成 | `docs/architecture/knowledge-agent-m3.md`、`knowledge-agent-m3-evidence.md` |
| M4 | 完成 | `docs/architecture/knowledge-agent-m4.md` |
| M5 | 完成 | `docs/architecture/knowledge-agent-m5.md` |
| M6 | 完成评测框架；真实对照实验待外部条件 | `docs/architecture/knowledge-agent-m6.md` |
| M7 | 进行中：A/B/C1-C2、D1 与 D2 no-go 门禁底座完成 | `docs/architecture/knowledge-agent-m7.md` |

## M0：基线与接口边界

- 固定当前后端测试、前端 lint/build 和真实工作台回归基线。
- 定义 `KnowledgeRepository`、`KnowledgeRetriever`、`SourceDiscovery`、`EvidencePackBuilder`、`ResearchOrchestrator` 接口。
- 保留当前 SQLite Task Repository/Job Queue，不直接改写成熟工作流。
- 增加 feature flag：`knowledge_agent_enabled`。

验收：关闭 feature flag 时，当前工作台行为与已有测试保持一致。

## M1：PostgreSQL + pgvector 开发底座

- 增加 Docker Compose 开发环境。
- 建立 PostgreSQL、pgvector 和数据库迁移机制。
- 创建 Project、KnowledgeSource、SourceSnapshot、KnowledgeChunk 基础表。
- 所有表和查询显式带 `project_id`。
- Embedding Provider 使用接口封装，避免写死模型供应商。
- 第一版通过自建 OpenAI 兼容网关调用 `/v1/embeddings`，模型使用 `text-embedding-3-small` 默认 1536 维；聊天和 Embedding 允许复用网关地址与密钥，但配置项和调用客户端必须分离。

验收：能保存来源、生成向量并按单项目完成相似度检索。

## M2：资料入库与知识库页面

- 建立 `DocumentParser` 路由和规范化 ParsedDocument 数据结构。
- 用代表性客户资料对轻量解析器与 MinerU 做解析质量、速度和资源占用对比。
- 私有 DOCX/PDF/Excel 上传、解析摘要、信任层级建议和文件级确认。
- WordPress Site Probe、产品目录首次同步、Blog/Guide 分类和 Research Inbox。
- 内容哈希去重、SourceSnapshot 版本和图片资产对应。
- 增加项目级“知识库”页面。

验收：运营人员能够看到来源为什么被分类为产品、Blog 或待确认，并能打开原始证据。

## M3：分章节混合检索与证据包

- 先实现可解释的 `BasicHybridRetriever`：PostgreSQL 全文/关键词 + pgvector + 元数据过滤 + 可选 rerank。
- 确认大纲后生成 RetrievalPlan。
- 每个 H2、产品事实和 FAQ 分别执行全文/关键词 + pgvector 混合检索。
- 实现 SectionEvidencePack、证据充分度和知识支撑段落比例。
- 硬事实建立句子级 EvidenceLink。

验收：每个章节可以独立显示 `sufficient / weak / missing`，正文硬事实可以回溯来源。

## M4：LangGraph 资料研究子图

- 实现 `plan_scopes`、`retrieve_knowledge`、`assess_evidence`、`discover_official_sources`、`ingest_candidates`、`await_human_review`、`build_evidence_pack`、`finish_with_warning`。
- PostgreSQL Checkpointer、唯一 thread ID、节点重试和最多两轮补查。
- 模糊候选使用 interrupt 暂停，人工确认后恢复。
- LangGraph Run 继续由后台队列启动。

验收：网络或模型失败后无需重跑已完成节点；人工确认可以恢复同一 Graph Run。

## M5：资料研究前端与只读对话

- 在“写作”阶段增加“资料研究”子步骤。
- 展示 scope 列表、证据包、来源卡片、Graph 时间线、补查次数、费用和失败原因。
- 增加研究助手抽屉，第一版只支持带引用的只读问答。
- “研究记录”默认折叠，持久化结构化节点、检索和工具事件，默认保留 30 天；API Key 和不必要的客户全文不得进入轨迹。
- SSE 流式展示节点状态，断线后使用轮询恢复。

验收：刷新浏览器不会丢失 Agent 进度；对话回答能显示使用的 KnowledgeChunk 和官网来源。

## M6：评测与作品展示

- 通过统一 `KnowledgeRetriever` 接口增加实验性 `LightRAGRetriever`，不替换基础检索器。
- 使用同一检索测试集比较 Basic Hybrid RAG 与 LightRAG 的直接事实、多文档关系、引用准确率、索引成本和查询延迟。
- 建立产品页/Blog 分类、产品分类匹配、检索相关性、硬事实证据覆盖、成本和时延测试集。
- 首个评测集使用约 20 条 qewitfastener 话题，保存标准分类 URL、允许页面类型、禁止 URL 和标注证据；至少计算 Recall@5、MRR、页面分类准确率、错误来源率和正确拒绝率。
- 记录 Agent 补查前后证据提升情况。
- 准备一个可复现的客户项目演示数据集和演示脚本。
- 输出架构图、关键 ADR、运行截图和简历描述。

验收：不仅能展示“聊天”，还能展示准确率、证据覆盖、检查点恢复、人工中断和费用边界。

实施状态（2026-07-31）：

- 已完成统一 Retrieval/Evidence Improvement 指标、20 条 qewitfastener JSONL、
  Basic Hybrid Runner、LightRAG HTTP Candidate Provider 和 PostgreSQL 二次发布门；
- 已完成确定性单元测试及真实 PostgreSQL 跨项目/旧快照集成测试；
- `topic_006` 为唯一已批准标注，其余 19 条待人工审核；
- qewitfastener 真实来源仍为 Inbox，LightRAG 独立 Server 尚未运行，因此真实对照分数、
  索引成本和运行截图明确待执行；
- 结构、命令和重构不变量见
  `docs/architecture/knowledge-agent-m6.md`。

## M7：多人服务器版

- Task/Job 数据迁移 PostgreSQL。
- Organization、Team、User、ProjectMembership 和 RBAC。
- S3 兼容对象存储、审计日志、备份和密钥管理。
- 受控云端或公司私有部署。
- 先增加三个受限对话写操作：重新检索产品、确认后替换产品、版本快照后重写指定章节。再根据明确业务目标决定 GSC、Semrush 和更多写操作。

实施状态（2026-07-31）：

- 已新增 Organization、User、Team、TeamMembership、显式 Project Ownership、
  ProjectMembership 和 append-only Audit Event；
- 已实现统一权限矩阵、PostgreSQL 事实查询和事务内 Audit Writer；
- 已实现不携带 Role 的签名 Actor Session，以及授权/撤销/审计同事务的
  ProjectMembership Service；
- 已接入 Project-scoped ProjectMembership Roster、授权和撤销：Roster 只按 SQL Scope
  返回显式成员并稳定分页；Candidate 页只返回 Active 同组织普通成员，排除 Org Admin、
  Active Owning Team Lead 与已有显式成员，归档 Team 不再贡献继承访问；写请求只能提交
  `editor/reviewer/viewer`，读写事务均重新检查 `project.members.manage` 并锁定全部可
  撤权事实；跨组织目标不泄露，Audit 故障回滚；Project Membership Console 已接入；
- 已为 Actor Session 增加数据库版本绑定：OIDC Exchange 把当前 `session_version`
  写入 Cookie，每次 Server 请求在项目授权前重新校验 Active Organization/User 与版本；
  Org Admin 的 Organization-scoped HTTP 命令递增目标版本并与安全 Audit 同事务，请求
  不接受版本或角色字段；全会话撤销已接入 Organization Admin Console 并要求确认；
- 已接入 Organization-scoped Workspace User 后端目录与生命周期命令：仅同组织 Active
  Org Admin 可读写，稳定分页返回 Active/Disabled 账号、Team/Project 显式成员数和
  `login_linked` 布尔值，不公开 Session Version 或外部身份；创建只建立本地 Active
  User，更新显示名/状态/组织角色；禁用和恢复都使旧 Cookie 失效，最后一个 Active
  Org Admin 受保护，写入与 Audit 同事务；组织级前端控制台已实现；
- 已接入 Organization-scoped Team/TeamMembership 后端命令：仅 Active Org Admin
  可读写，Team 与成员 Roster 稳定分页；Manager 指针只接受同组织 Active User 且不产生
  权限，项目继承仍只来自 Active Team 的显式 `team_lead`；归档立即停止继承访问并保留
  既有成员供清理，Disabled User 不可新增/改角色但旧成员可撤销；全部变更与 Audit
  同事务；组织级前端控制台已实现；
- 已实现项目级 PostgreSQL Task Repository、SQLite Task 摘要校验导入、
  Revision CAS 和带 SKIP LOCKED/Worker Lease 的 PostgreSQL Job Queue；
- 已实现 Active Job 排空门和 SQLite Terminal Job 历史迁移，按稳定 ID、
  状态分布与内容摘要复核；
- 已验证跨组织拒绝、禁用用户、旧项目 fail-closed、复合外键、审计不可修改和
  Alembic 往返升级；
- 已实现私有 S3 兼容 ObjectStore、内容寻址 Key、M2 ArtifactStore 适配、
  产品/知识资产授权上传和短期签名下载，并通过本地真实 S3 往返测试；
- 已实现安全输出的部署 Preflight 和备份/恢复/轮换/回滚 Runbook；当前代码能力
  门禁明确 no-go，真实恢复演练与生产供应商仍待确定；
- 已接入 Server Mode 请求安全底座：Knowledge Router 全路由重新读取数据库权限，
  未迁移的旧 API、SQLite Research Queue 和本地对象入口明确返回 503；
- Server Mode 已停止构造和启动 SQLite Queue/Worker；全局本地 TaskStore/JobQueue
  调用 fail closed；产品重新发现已有独立 Project-scoped PostgreSQL Runner，其余
  通用 Batch/Worker 尚未接线；
- 已接入 Project-scoped PostgreSQL Batch/Job Control：列表、Batch 详情、取消和重试
  只展示 `product_rediscovery`，公开 DTO 不含私有 Request、Requester、Category URL、
  原始 Error 或对象 URI；读取要求 `project.view`，写入在同一事务锁定可撤权事实与
  Job 状态并追加安全 Audit；Retry 只重放服务端已保存命令，空 Body 之外的覆盖字段
  返回 422；旧无 Project 的 `/api/batches*` 继续关闭；
- 已开放显式 Project 路径的 PostgreSQL Task 只读列表/单条接口；每个请求重新读取
  RBAC 事实，跨项目不扫描全量数据，本地模式不增加该 API；
- 原计划三个受限操作已经逐项接通：`knowledge.edit` 的“重新发现产品”PostgreSQL Job、
  `article.edit` 的“从正式目录确认替换产品”，以及“版本快照后替换一个已审阅章节”；
  产品重新发现只写项目绑定的 S3 与不可变 Inbox 证据，不改 Task；后两者使用
  Project Scope 与 Revision CAS，且不创建本地 JSON/SQLite/Artifact；
- 另有一个辅助迁移入口“完全重写”，它不是原计划三个操作之一；其他写路径仍关闭；
- 已开放 SQL-scoped Project Directory，只返回 Active Actor 在当前 Organization
  可访问的 Active Project 及 Effective Role；归档 Project 立即 fail closed；
- 已接通私有知识/产品资产的 Server Mode 下载路由：路由授权与签名前授权各执行一次，
  Bucket 和 Organization/Project Key 前缀不一致时不签名；
- 已接通 Server 私有图片准备：Hero 由项目 Asset ID 指定，产品图只读取 Task 已确认的
  `selected_asset_id`；对象读取再次授权并复核哈希，在内存生成内容寻址 WebP，最多三张
  且视觉去重，Task 只保存 Asset 引用和文章锚点，不创建本地图片路径；
- 自动锚点失败时返回非 FAQ H2/H3 候选且不上传派生对象；人工锚点只能绑定当前 Task
  Product ID；
- 已接通 Server 文章 DOCX：`article.deliver` 下重新读取并复核 Task 的私有 WebP，
  复用现有排版逻辑在内存生成 Word，再保存内容寻址 DOCX Asset；Task 的
  `docx_path` 为空，专用下载路由再次授权，通用 Viewer Asset 下载不能取得交付文档；
- 已接通 Server TDK DOCX：从当前文章生成经过硬约束验证的 T/D/K，在内存生成
  `D.docx` 并保存内容寻址 `tdk_docx` Asset；Task 的 `tdk_path` 为空，专用下载再次
  授权，通用 Viewer Asset 下载与文章 DOCX 下载均不能取得 TDK；
- 已接通 Server 最终 AI-rate Review：Reviewer 上传的图片在内存规范化为无元数据 PNG，
  AICheck 只保存私有 Asset 身份且绑定当前 Humanized Article 哈希；专用下载重新授权，
  通用 Viewer Asset 下载不能取得截图；
- 已接通 Server Delivery ZIP：服务端只读取 Task 已绑定且逐项复核的文章 DOCX、
  `D.docx`、Prepared WebP 和已确认终审截图，在内存生成确定性扁平 ZIP；Task 只保存
  私有 `delivery_zip` Asset 身份，通用 Viewer 下载隐藏，专用下载重新授权；
- 已接通窄范围 Server 前端入口：认证状态先分流组件树，Server 首页只列 SQL-scoped
  Project Directory 并直达 Delivery Console；Console 按 Asset 身份识别产物、提交
  Revision 打包，并通过专用接口取得短期下载 URL；未迁移导航不挂载，Local UI 不变；
- 已接通 Project Membership Console：仅 `org_admin/team_lead` 显示入口，Roster 与
  Candidate 稳定分页，可添加、改 `editor/reviewer/viewer` 与撤销显式成员；模式失败不
  降级挂载 Local Settings，前端角色仅作导航提示，后端仍逐请求/事务授权；
- 已接入 `/organization` Organization Admin Console：仅 Server Auth Status 能提供
  已认证 Organization 时挂载；账号创建/资料/角色/状态、全会话撤销、Team 创建/归档、
  TeamMembership 授权/改角色/撤销均调用已验证的 Organization-scoped API；危险操作
  使用确认 Dialog，加载与分页状态有界，Manager 与 Team Lead 明确分离；
- 已接入 Organization-scoped External Identity 目录、关联和撤销：原始 Subject 只在
  Link 请求出现，公开目录/响应/Audit 不返回 Subject；稳定 Mapping ID 用于分页和撤销，
  同一 Active 映射重复 Link 幂等，跨组织/用户冲突统一拒绝，写入与 Audit 同事务；
  Organization Admin Console 只短暂持有 Subject 输入并在成功后清空；
- 已接入一次性 Workspace Invitation：数据库只存 Token SHA-256，邀请固定同组织
  Active User、预期 Issuer 与过期时间；签发响应只返回一次 Token，目录与 Audit 不返回
  Token/Hash；`/accept-invite` 从 Fragment 取 Token 并先清地址栏，再用短期 HttpOnly
  Cookie 与 HMAC State Hash 绑定 OIDC 流；验签后 External Identity、Invitation
  Accepted 与 Audit 同事务，过期/撤销/重放/跨组织冲突 fail closed；邮件投递未接入；
- 派生对象 orphan 对账与延迟清理安全门已完成；真实生产身份、对象供应商与恢复演练
  仍未验收，因此不能把当前可操作的 Server 交付界面描述成生产上线；
- 已让十二条迁移完成的 Server Task 写操作统一走 `PostgresAuditedTaskWriter`：锁定
  可撤权事实，按 Audit Action 固定最小权限，Task Revision CAS 与 append-only Audit
  同事务；审计失败、撤权或 CAS 冲突不留下 Task 写入，Details 不含正文或 URL；
- 已新增 PostgreSQL Task 标题候选选择命令：客户端只提交 Revision 与候选索引，
  服务端从当前 `title_candidates` 选择原值、失效下游状态并写安全 Audit；标题生成
  `titles` Job 仍依赖本地客户知识目录，因此在 Published Knowledge Context 接线前
  保持 Local Only；
- 已新增 PostgreSQL Task 大纲草稿/确认命令：草稿保留当前确认大纲和下游产物，确认
  才替换正式大纲并失效正文之后的派生状态；两者都追加内容哈希 Version，并以 CAS 与
  不含 Markdown 的 Audit 原子提交；大纲生成 `outline` Job 仍保持 Local Only；
- 已新增服务器历史大纲恢复命令：客户端只提交 Version Index，服务端只从当前 Task 的
  `outline/outline_draft` 历史恢复新草稿；Article Version、越界索引和客户端正文均
  fail closed，当前确认大纲与下游保持不变；
- 已新增 PostgreSQL Project Prompt Snapshot 底座：Head、不可变 Version 与精确
  Default 指针分表；编辑只追加新 Version，默认不会自动漂移；读取与写入分别要求
  `project.view/article.edit`，写操作重新锁定可撤权事实并与安全 Audit 同事务；
- Server Prompt HTTP、旧 SQLite Prompt 数据迁移和生成 Worker 消费尚未接线，因此旧
  Prompt API 在 Server Mode 继续关闭，不能把底座完成描述成生成链已迁移；
- 已为 PostgreSQL Job 增加可信 `requested_by_user_id`，并完成 Worker Claim 前最小
  元数据授权与 Handler 前二次授权；产品重新发现 Enqueue 的可撤权授权、Task Revision、
  Job/Batch 和安全 Audit 已在同一事务；该 Operation 的终态 Job/Audit 原子性和有界
  drain/join 报告也已完成，受控停机释放 Claim 而不伪装成用户取消；该 Operation 的
  Project-scoped Batch/Job Control 也已完成，但其他 Operation 的可信 Enqueue、
  Server-only Handler、通用 Runner 和正式排空演练尚未完成，所以整体 Preflight 能力
  仍保持 false；
- 已实现 Task/Job 冻结窗口只读双读报告，比较顺序、ID、状态分布和内容摘要，
  Active SQLite Job、重复/空 ID 或任意差异都会阻止单写切换；
- Server Mode 不接受旧 `APP_PASSWORD` 登录签发 Actor；已接通供应商无关 OIDC
  Authorization Code + PKCE、Discovery/JWKS RS256 验签、State/Nonce 与登录页，
  Provider 配置不完整或实时签名 Key 探测失败时 fail closed；
- 已新增 External Identity 映射和审计化 Link/Revoke：外部 Issuer/Subject 只能映射
  到同 Organization 的 Workspace User，外部 Role 不进入 Session；
- 当前单密码 Cookie 不具备 User Identity；OIDC 只把已验证 Issuer/Subject 映射为本地
  Actor，不信任外部 Email/Group/Role；`trusted_identity_source` 代码门禁已完成，
  具体生产 IdP 注册与 Conformance 冒烟仍待部署环境；
- 后续按“生产 IdP Conformance 与逐 Operation API/Worker 授权覆盖 -> 保存冻结窗口 matched 证据
  -> 服务器 PostgreSQL 单写切换
  -> 备份恢复与部署门禁”
  顺序推进，完整结构与重构检查清单见
  `docs/architecture/knowledge-agent-m7.md`。
