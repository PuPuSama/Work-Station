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
| M6 | 下一里程碑 | 评测与作品展示 |
| M7 | 未开始 | 多人服务器版 |

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

实施状态（2026-07-30）：

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
