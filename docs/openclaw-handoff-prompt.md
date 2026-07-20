# OpenClaw 交接说明：文章工作台知识库与 LangGraph Agent

## 你需要先知道什么

这是一个已经投入真实文章生产的本地工作台，不是绿地 Demo。项目路径为 `D:\article\article-agent`，当前技术栈是 FastAPI + Next.js，文章任务和后台队列使用 SQLite。已有功能包括标题、产品抓取、大纲、正文、人工 ZeroGPT、降 AI、链接恢复、图片、Word、TDK 和交付打包。

请保留已有确定性文章状态机，不要为了接入 Agent 重写整个工作台。ZeroGPT 永远是人工步骤，不能自动操作外部检测网站。

## 这次要做什么

为工作台增加可追溯的客户项目知识库和资料研究 Agent：

- 导入客户私有 DOCX、PDF、Excel 等资料。
- 抓取客户 WordPress/WooCommerce 官网的产品详情页、分类、Blog/Guide 和图片资产。
- 区分产品硬事实、参考素材和写作规则。
- 按每个 H2、产品事实和 FAQ 分别检索。
- 资料不足时由 LangGraph 最多补查两轮官网/Tavily。
- 模糊来源暂停等待人工确认。
- 正文知识支撑占比按段落统计。
- 产品规格、材料、认证、产能等硬事实要求句子级证据。
- 在前端展示 Evidence Pack、来源、Graph 节点、补查次数和证据状态。

## 已确认的关键决策

1. 第一版先做单项目垂直切片，不一次性完成云端多租户系统。
2. 现有文章 Task Repository/Job Queue 暂时保留 SQLite。
3. 新知识、向量、Evidence Pack 和 LangGraph Checkpoint 使用 Docker PostgreSQL + pgvector。
4. 所有知识表和查询从第一天携带 `project_id`，为后续 RBAC 做准备。
5. 文档解析采用分层路由：普通 TXT/DOCX/XLSX/文本 PDF 使用轻量解析器；扫描件、复杂 PDF、图片和 PPTX 使用独立 MinerU 服务。
6. 项目自己实现 KnowledgeSource、SourceSnapshot、KnowledgeChunk、EvidenceLink、PublicationGate、检索和前端；不引入 Dify/RAGFlow/FastGPT 作为核心知识库。
7. 第一版实现 BasicHybridRetriever：PostgreSQL 全文/关键词 + pgvector + 元数据过滤 + 可选 rerank。
8. LightRAG 不进入第一版核心。后期通过统一 KnowledgeRetriever 接口作为对照实验，必须与基础 RAG 使用同一评测集。
9. LangGraph 只编排资料研究子图，不接管文章状态机、爬虫安全、权限、PublicationGate、ZeroGPT、图片或导出。
10. LangGraph 节点包括：`plan_scopes`、`retrieve_knowledge`、`assess_evidence`、`discover_official_sources`、`ingest_candidates`、`await_human_review`、`build_evidence_pack`、`finish_with_warning`。
11. 每个 H2、产品事实和 FAQ 使用独立 SectionEvidencePack；默认最多两轮 GapFillAttempt。
12. Tavily 只发现客户官网候选 URL，第三方搜索结果不能直接成为客户产品硬事实。
13. 官网默认每周增量刷新，写文章时只补抓缺失、过期、失败或新发现的强相关页面。
14. 私有文件自动解析、摘要和建议分类，但员工必须按文件确认后才能发布。
15. 第一版研究助手对话只读；第二阶段仅开放“重新检索产品、确认后替换产品、版本快照后重写指定章节”三个白名单写操作。
16. 第一条真实垂直切片使用 `www.qewitfastener.com / topic_006`，并以约 20 条 qewitfastener 话题建立产品分类与来源类型评测集。
17. Agent 研究记录默认折叠，保存结构化事件 30 天，不记录密钥和不必要的客户全文。
18. GSC、Semrush 是未来可选 SeoDataProvider，不是第一版依赖。

## 第一版前端形态

保留现有五阶段：

```text
内容准备 -> 写作 -> 人工质检 -> 图片 -> 交付
```

“写作”阶段变为：

```text
大纲 -> 资料研究 -> 第一版
```

项目导航增加“知识库”。资料研究页显示每个 H2/产品事实/FAQ 的 `sufficient / weak / missing / running / waiting_for_review`，并展示证据来源、Graph 时间线和人工中断。

## 第一阶段不做

- 不迁移完整 Task Repository/Job Queue。
- 不做正式登录和 RBAC 页面。
- 不做 S3 对象存储迁移。
- 不接 GSC、Semrush。
- 不允许 Agent 自动执行 ZeroGPT。
- 不让 LightRAG 替代项目知识准源。
- 不构建无限自主循环或“超级 Agent”。

## 希望你做的事情

请基于提供的仓库和设计文档进行审查，不要从通用 RAG 教程重新规划。输出：

1. 指出当前架构中最可能失败的 5 个地方，并给出具体修正建议。
2. 审查 PostgreSQL/pgvector 数据模型、Repository 边界、迁移方式和 project_id 隔离。
3. 审查 BasicHybridRetriever 的检索、融合、rerank、引用和评测设计。
4. 审查 LangGraph State、Node、Conditional Edge、Checkpointer、interrupt 和后台队列结合方式。
5. 审查 MinerU Adapter 与 ParsedDocument 规范化设计。
6. 给出 M0 到 M5 的可实施任务拆分、测试策略和每个里程碑的可见验收结果。
7. 明确哪些地方应自己实现，哪些地方应复用现成库。
8. 如果你不同意某个 ADR，请引用 ADR 编号、说明具体风险，并给出替代方案，不要静默推翻已确认业务规则。

如果随后要求你实现，请先执行 M0/M1，不要直接大规模重构现有 `backend/app.py` 或 `frontend/src/components/article-workbench.tsx`。所有新增大块前端放到独立组件，所有新存储通过 Repository/Service 接口访问。

## 建议阅读顺序

1. `AGENTS.md`：当前真实项目地图和不可破坏的业务约束。
2. `docs/agent-learning-and-delivery-plan.md`：学习实验、真实案例和正式交付双轨计划。
3. `docs/knowledge-agent-implementation-roadmap.md`：实施顺序。
4. `docs/knowledge-base-domain-model.md`：领域实体和已确认规则。
5. `docs/langgraph-research-agent-ui.md`：前端和对话边界。
6. `docs/adr/0011-single-project-vertical-slice-first.md`：第一版范围。
7. `docs/adr/0012-custom-knowledge-domain-with-mineru-parser.md`：MinerU 与解析层。
8. `docs/adr/0013-basic-rag-first-lightrag-as-evaluated-adapter.md`：基础 RAG 与 LightRAG 定位。
9. `docs/adr/0009-section-scoped-retrieval-and-bounded-research.md`：LangGraph 研究子图。
10. 其余 ADR：来源采集、部署、RBAC、知识占比、刷新和私有文件发布。

## 安全提醒

不要要求或输出 `.env`、真实 API Key、客户私有原始文件、`data/` 数据库、运行日志或真实交付资产。需要验证外部服务时，使用环境变量是否已配置的布尔状态，不回显密钥值。
