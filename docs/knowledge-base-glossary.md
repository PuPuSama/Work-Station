# 知识库与官网采集术语表

## Canonical URL

来源内容的规范 URL。用于把带跟踪参数、Fragment 或不同入口指向的同一页面合并为一个 `KnowledgeSource`。

## Crawl Run

一次官网发现和抓取任务。可以是首次产品目录同步、单篇文章增量采集或定期刷新。

## Evidence Link

正文中的一个句子与其支撑知识片段之间的映射。用于计算知识支撑占比和检查硬事实是否有依据。

## Evidence Pack

为某篇文章或某个章节检索出的结构化资料包，包含知识片段、来源、可信层级和公开链接，而不是把整个知识库拼进提示词。

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

## Knowledge-supported Ratio

有 Evidence Link 支撑的可见正文词数占全部可见正文词数的比例。它不是复制率，也不要求原文照抄。

## Product Category

官网对产品的分类节点。产品自动选择应先找到与文章话题最相关的分类，再展开其产品详情。

## Product Detail Page

描述单个具体产品的页面。应具备产品结构化数据、产品模板、SKU、规格、图库或产品面包屑等强信号。

## Product Candidate

已经验证为产品详情页，并且与目标产品分类相关的候选。只有该类型可以进入文章产品列表和产品选图流程。

## Project Membership

用户对某个 Customer Project 的访问授权，包含项目经理、编辑、复核和只读等角色。

## Publication Gate

Research Candidate 发布到知识库前的规则边界。强验证产品页和明确官网 Blog 可以自动发布；普通 Page、自定义类型和模糊页面必须进入待审核区。发布决定需要保留证据并支持撤销。

## Official Blog

客户官网中的文章、新闻、Guide 或案例内容。属于公开参考素材，可以用于正文引用，但不能冒充产品详情页。

## Research Candidate

爬虫或 Tavily 新发现、尚未正式进入知识库的 URL 或文件。

## Research Inbox

Research Candidate 的暂存区。候选在这里完成页面类型分类、Canonical 去重、内容质量检查和发布决策。

## Source Snapshot

某个 Knowledge Source 在特定时间点的不可变内容版本，包含内容哈希、抓取时间和解析器版本。

## Site Probe

项目创建后自动执行的低成本只读官网探测。它识别 WordPress/WooCommerce 能力、公开 REST 路由、Sitemap、预计产品规模和抓取风险，但不会立即下载完整产品目录或图片。完整同步需要用户确认。

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
