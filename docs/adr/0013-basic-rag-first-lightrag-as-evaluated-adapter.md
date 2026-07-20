# ADR-0013：先实现基础混合 RAG，再把 LightRAG 作为评测型适配器

- 状态：Accepted
- 日期：2026-07-17
- 范围：RAG 学习路线、LightRAG 定位、LangGraph 顺序和检索评测

## 背景

LightRAG 是图增强 RAG 系统，会在索引阶段抽取实体和关系，并结合 KV、向量、图和文档状态存储进行查询。它能够处理跨文档关系和更全局的问题，也已经提供引用、不同查询模式和多种存储后端。

当前文章工作台最重要的检索目标首先是：按客户项目隔离、找到准确产品分类和产品详情、返回可定位原文的段落、验证硬事实，以及稳定重建 H2 证据包。这些需求需要强元数据过滤和来源版本控制，不等同于通用知识图谱问答。

同时，本项目包含学习和作品展示目标。如果直接把全部文本交给 LightRAG Server，再调用 query API，能够快速集成功能，但难以解释 chunking、embedding、混合检索、召回、rerank、引用映射和评测的具体实现。

## Decision

1. 第一版知识领域层继续由本项目实现，不把 LightRAG 数据库当作项目准源。
2. M3 自己实现一个范围受控的 `BasicHybridRetriever`，而不是从零实现向量算法或数据库。
3. BasicHybridRetriever 使用 PostgreSQL 全文/关键词检索、pgvector、项目/来源/分类/新鲜度过滤、结果融合和可选 rerank。
4. 所有检索器实现统一 `KnowledgeRetriever` 接口，输出带 chunk_id、snapshot_id、相关性和来源定位的候选。
5. LangGraph 只依赖 KnowledgeRetriever 接口；先让基础检索器通过测试，再开发 Graph 的循环与人工中断。
6. 在 M6 增加实验性 `LightRAGRetriever`，优先通过独立 LightRAG Server REST API 集成，不嵌入和修改其内部 Core。
7. LightRAG 的 KV/Vector/Graph/DocStatus 存储不替代项目的 KnowledgeSource、SourceSnapshot、PublicationGate 和 EvidenceLink。
8. LightRAG 查询返回的引用与 chunk 内容必须重新映射到项目 SourceSnapshot；无法映射的内容不能用于硬事实证据。
9. 使用同一评测集比较基础 RAG 与 LightRAG，只有证明跨文档/关系问题明显受益且引用、租户隔离和成本满足要求时，才考虑进入正式检索链路。
10. 第一版不同时学习和改造 LightRAG、LangGraph、MinerU 与完整多租户部署；按“基础 RAG -> 评测 -> LangGraph -> LightRAG 对照实验”的顺序逐步增加复杂度。

## 不是什么“重写知识库”

需要自己实现的是应用层 RAG 管道：

- 文档规范化、切块和元数据。
- Embedding 调用和 pgvector 写入。
- 全文/向量混合召回。
- 项目、产品分类、来源状态和新鲜度过滤。
- 结果融合、rerank 和 Evidence Pack。
- 引用与硬事实证据映射。
- 评测、日志和前端解释。

不需要自己实现：

- 向量索引算法。
- 数据库引擎。
- OCR/版面模型。
- LLM 推理服务。
- Agent 检查点底层协议。

## 学习顺序

详细 TODO 实验、测试门槛和正式模块合入规则见 `docs/agent-learning-and-delivery-plan.md`。

### 1. Basic RAG

先使用玩具向量手写余弦相似度和 Top-K，再通过自建 OpenAI 兼容网关调用 `text-embedding-3-small`。随后理解 chunk、真实 Embedding、关键词召回、元数据过滤和引用。

### 2. Hybrid 与评测

加入全文检索、结果融合、rerank，并建立 Recall@K、MRR、引用准确率、延迟和费用测试。

### 3. LangGraph

把检索器封装为工具，学习 State、Node、Conditional Edge、Checkpointer、interrupt 和有界循环。

### 4. LightRAG 实验

学习实体/关系抽取、图检索与 local/global/hybrid/mix 查询，分析什么问题真正需要图。

## 统一接口草案

```text
KnowledgeRetriever.retrieve(
  project_id,
  query,
  scope,
  filters,
  top_k
) -> RetrievalResult

RetrievalResult
  candidates[]
    chunk_id
    snapshot_id
    source_id
    score
    retrieval_channels[]
    text
    page_or_sheet
  diagnostics
    query_variants
    timing
    token_usage
```

LightRAG Adapter 必须把自身 reference/file_path/chunk content 映射到上述结构，不能把框架生成的答案直接当作证据。

## 对照评测集

至少包含：

- 单一产品规格的直接事实问题。
- 产品页与 Blog 页面类型区分。
- 话题到精确产品分类匹配。
- 两个产品的有来源比较。
- 跨多个手册或页面的关系问题。
- 官网没有依据的陷阱问题。
- 资料更新后旧证据失效问题。

指标：Recall@5、MRR、硬事实引用准确率、错误来源率、索引模型 Token、增量更新时间、查询延迟和存储复杂度。

## 何时让 LightRAG 进入正式链路

- 多文档或多跳问题相对 BasicHybridRetriever 有稳定提升。
- 返回引用可以可靠映射到 SourceSnapshot 和 KnowledgeChunk。
- 项目隔离与元数据过滤通过自动测试。
- 增量更新不会破坏历史 EvidenceLink。
- 索引模型费用和运维复杂度能够接受。

## 风险

- LightRAG 的实体/关系抽取依赖 LLM，索引费用和结果稳定性需要单独评估。
- 它包含 KV、Vector、Graph、DocStatus 四类存储，直接作为核心会扩大第一版运维范围。
- 客户资料中可能有大量型号、缩写和近似产品名，自动实体合并可能产生错误关系。
- 工作区隔离和复杂元数据过滤需要真实多项目测试，不能只依赖配置名称。

## 官方参考

- LightRAG Repository: https://github.com/HKUDS/LightRAG
- Programming with LightRAG Core: https://github.com/HKUDS/LightRAG/blob/main/docs/ProgramingWithCore.md
- LightRAG API Server: https://github.com/HKUDS/LightRAG/blob/main/docs/LightRAG-API-Server.md
- LightRAG Paper: https://arxiv.org/abs/2410.05779
