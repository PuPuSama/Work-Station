# 知识库与官网采集术语表

## Canonical URL

来源内容的规范 URL。用于把带跟踪参数、Fragment 或不同入口指向的同一页面合并为一个 `KnowledgeSource`。

## Crawl Run

一次官网发现和抓取任务。可以是首次产品目录同步、单篇文章增量采集或定期刷新。

## Freshness Window

一个官网来源在无需生成前复查的情况下可被视为新鲜的时间范围，项目默认 7 天。超过窗口不代表内容一定错误，但在相关写作任务中应优先检查是否变化。

## Incremental Refresh

只检查新增、变化、过期或失败来源的官网刷新方式。内容哈希没有变化时不重新切块、生成 Embedding 或下载相同图片。

## Graph RAG

在普通文本块和向量检索之外，从资料中抽取实体与关系并构建图结构，再结合图和向量完成检索的方法。它适合跨文档关系和多跳问题，但索引成本、模型依赖和数据治理复杂度通常高于基础 RAG。

## Evidence Link

正文中的一个段落或具体句子与其支撑知识片段之间的映射。段落级映射用于计算知识支撑占比；句子级映射用于检查硬事实是否有依据。

## Evidence Pack

为某篇文章或某个章节检索出的结构化资料包，包含知识片段、来源、可信层级和公开链接，而不是把整个知识库拼进提示词。

## Gap Fill Attempt

某个章节、产品事实或 FAQ 的证据不足时执行的一次受控补查。它只能在配置的轮数和请求预算内查询客户官网或使用 Tavily 发现官网页面。

## Hard Fact

产品规格、材料、认证、产能、交期、公司能力等可验证事实。相关正文必须有明确来源。

## Knowledge Chunk

从来源快照中按标题、段落、表格或语义边界切出的最小检索单元。

## Knowledge Source

一个逻辑来源，例如私有 DOCX、官网产品详情页或官网 Blog。一个来源可以随时间产生多个快照。

## Customer Project

一个客户官网及其文章任务、产品资料、知识库和导出记录组成的协作空间。它是主要的权限和知识隔离单位。

## Data Residency

客户资料被允许存储和处理的地理位置与基础设施边界，例如受控公有云、私有云、公司内网服务器或员工本机。

## Deployment Profile

工作台的基础设施连接配置，可选择受控公有云、私有云或公司内网部署。部署环境可以变化，但租户、项目权限和知识隔离规则必须保持一致。

## Knowledge-supported Ratio

有效知识支撑段落占全部合格正文段落的比例。它不是复制率，也不表示支撑段落中的每句话都已得到证实；硬事实另行执行句子级证据校验。

## Private Upload Review

私有 DOCX、PDF、Excel 等文件解析后的文件级人工确认。系统建议标题、摘要和信任层级，员工确认或修改后才发布到项目知识库，不要求逐个知识片段审核。

## Product Category

官网对产品的分类节点。产品自动选择应先找到与文章话题最相关的分类，再展开其产品详情。

## Product Detail Page

描述单个具体产品的页面。应具备产品结构化数据、产品模板、SKU、规格、图库或产品面包屑等强信号。

## Product Candidate

已经验证为产品详情页，并且与目标产品分类相关的候选。只有该类型可以进入文章产品列表和产品选图流程。

## Project Membership

用户对某个 Customer Project 的访问授权，包含项目经理、编辑、复核和只读等角色。

## Role-based Access Control (RBAC)

按组织、团队和客户项目角色决定访问能力的权限模型。编辑仅访问明确分配的项目，组长访问本组项目，组织管理员访问全部项目；服务端必须在每次操作时验证权限。

## Team Membership

用户在运营小组中的身份。`team_lead` 可以访问本组负责的项目并分配项目成员；普通 `member` 仍需单独的 Project Membership 才能访问客户项目。

## Publication Gate

Research Candidate 发布到知识库前的规则边界。强验证产品页和明确官网 Blog 可以自动发布；普通 Page、自定义类型和模糊页面必须进入待审核区。发布决定需要保留证据并支持撤销。

## Official Blog

客户官网中的文章、新闻、Guide 或案例内容。属于公开参考素材，可以用于正文引用，但不能冒充产品详情页。

## Research Candidate

爬虫或 Tavily 新发现、尚未正式进入知识库的 URL 或文件。

## Research Inbox

Research Candidate 的暂存区。候选在这里完成页面类型分类、Canonical 去重、内容质量检查和发布决策。

## Retrieval Plan

大纲确认后按 H2、产品事实和 FAQ 划分的结构化检索任务集合。每个范围独立检索、判断证据充足度并生成 Section Evidence Pack。

## Retriever Adapter

把 Basic Hybrid RAG、LightRAG 或未来其他检索实现转换为统一候选结果和 Evidence Pack 的接口。业务层不直接依赖某个检索框架的存储结构或响应格式。

## Section Evidence Pack

某个 H2、产品事实或 FAQ 专用的证据包，包含选中的知识片段、来源快照、硬事实候选、公开链接和证据充足度状态。

## Source Snapshot

某个 Knowledge Source 在特定时间点的不可变内容版本，包含内容哈希、抓取时间和解析器版本。

## Site Probe

项目创建后自动执行的低成本只读官网探测。它识别 WordPress/WooCommerce 能力、公开 REST 路由、Sitemap、预计产品规模和抓取风险，但不会立即下载完整产品目录或图片。完整同步需要用户确认。

## Self-service Delivery

编辑可以在被分配的项目中自行完成人工检查确认、Word 导出和交付打包，不需要另一位用户强制批准。系统仍保留版本、证据和审计记录，复核人员可以作为可选质量支持参与。

## Server Source of Truth

生产环境中任务、知识、文件、版本和审计记录的唯一权威状态。服务器数据发生冲突时优先于本地开发、演示或紧急导出副本。

## Local Standalone Mode

保留在员工电脑上的单机运行方式，仅用于开发、演示和服务器不可用时的紧急导出。它不保存生产数据的权威版本，不与服务器自动双向同步；需要回传的结果必须经过显式导入并形成审计记录。

## Scale Trigger

证明系统需要扩容或拆分服务的可观测指标，例如队列等待时间、Worker 饱和度、数据库延迟、知识片段数量和模型限流。没有达到 Scale Trigger 时不提前增加基础设施复杂度。

## SEO Observation

从 GSC、Semrush 或其他 SEO 数据源取得的、带项目、时间范围、采集时间和供应商来源的指标或研究结果。它用于选题和表现分析，不自动成为产品硬事实证据。

## Tool Registry

按项目、权限和部署配置向 Agent 暴露可用工具的后端注册表。LangGraph 只依赖统一工具接口，不直接持有 GSC、Semrush、Tavily 等供应商密钥或把供应商实现写进图节点。

## Supported Paragraph

至少存在一条有效段落级 Evidence Link 的合格正文段落。证据必须来自当前项目中已发布、未失效的知识片段，并能支撑该段至少一个实质性观点；仅含无关链接的段落不算。硬事实仍需额外满足句子级证据规则。

## Trust Tier

知识来源的用途和约束等级：`hard_fact`、`reference_material`、`writing_instruction`。

## Tenant

系统中的公司级隔离边界，对应 Organization。不同 Tenant 的用户、项目、知识和文件不能互相访问。

## Unknown Page

缺少足够产品、文章或分类证据的页面。它不能自动进入产品列表或硬事实知识层。

## WordPress REST Route Index

通常位于 `/wp-json/`，用于发现网站实际公开的命名空间和资源路由，避免盲猜固定接口。

## WooCommerce Store API

WooCommerce 提供的公开客户侧 REST API。产品和分类读取通常位于 `/wp-json/wc/store/v1/`，不等同于需要认证的后台 WooCommerce REST API。
