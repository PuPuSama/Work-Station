# Knowledge Agent M3 架构与实施台账

> 状态：完成
>
> 分支：`feature/knowledge-agent-m3`
>
> M2 检查点：`572139c`

## 1. 文档目的

本文是 M3 的长期“结构痕迹”，记录混合检索、RetrievalPlan、SectionEvidencePack
和 EvidenceLink 的模块职责、数据流、排序公式、持久化边界与验收证据。后续重构
若改变接口责任或业务不变量，必须同步更新本文。

## 2. M3 目标与非目标

M3 交付：

1. PostgreSQL 全文检索与 pgvector 精确余弦检索的可解释融合；
2. 来源、信任层级、产品身份、标题路径和 JSONB 元数据过滤；
3. 大纲版本绑定的章节级 RetrievalPlan；
4. 每个 H2、产品事实和 FAQ 独立的 SectionEvidencePack；
5. `sufficient / weak / missing` 的确定性首版判断；
6. 段落级知识支撑比例和硬事实句子级 EvidenceLink。

M3 不执行官网补查循环，不引入 LangGraph，不让 Evidence Pack 直接修改文章正文。
这些编排职责属于 M4；研究过程前端和只读对话属于 M5。

M2 的真实 MinerU 性能对比仍是独立外部验收项。缺少服务或私有样本不改变 M3
检索接口，但也不能被标记为已实测。

## 3. 当前模块地图

| 模块 | 责任 | 明确不负责 |
|---|---|---|
| `knowledge_agent.hybrid_retriever` | 过滤当前项目已发布快照，分别取向量和全文候选，以 RRF 融合并输出逐项解释 | 不发布来源、不抓网页、不生成正文 |
| `knowledge_agent.retriever` | 保留 M1 向量-only 基线，供回归和后续评测对照 | 不承担 M3 混合排序 |
| `knowledge_agent.contracts` | 提供跨实现稳定的 Query、Hit、来源版本和 Evidence 契约 | 不执行 SQL |
| `knowledge_agent.schema` / Alembic | 保存全文索引及后续 Evidence 表结构 | 不在应用启动时迁移 |
| 后续 `knowledge_agent.evidence` | 生成 RetrievalPlan、Evidence Pack、充分度和覆盖率 | 不调用外部搜索 |
| 后续 `knowledge_agent.evidence_repository` | 幂等持久化计划、证据包和 EvidenceLink | 不判断文本是否被证据支撑 |

## 4. BasicHybridRetriever 排序

### 4.1 候选边界

两个检索通道共享完全相同的硬过滤：

- `project_id`；
- 来源为 `published`；
- Chunk 属于来源的 `current_snapshot_id`；
- Chunk 的 Embedding 非空且模型与当前 Provider 一致；
- 调用方提供的白名单过滤条件。

全文通道使用：

```sql
to_tsvector(
  'simple',
  text
)
```

`simple` 配置保留产品型号和多语言词形，不做英语词干替换。表达式 GIN 索引由
Alembic 迁移 `20260730_0004` 创建。`heading_path` 不拼进表达式索引，因为
PostgreSQL 的 `array_to_string/concat_ws` 不能满足索引表达式的 IMMUTABLE
要求；标题范围通过 `heading_contains` 数组过滤，正文相关性由 GIN 负责。

### 4.2 RRF 公式

默认权重：

- Vector：`0.55`
- PostgreSQL FTS：`0.45`
- `rrf_k = 60`

每个候选的原始融合分：

```text
vector_weight / (k + vector_rank)
+ lexical_weight / (k + lexical_rank)
```

再除以两个通道都排名第一时的理论最大值，得到便于 UI 展示的归一化分数。
缺失某一通道排名时，该通道贡献为零，不伪造相似度。最终同分按 `chunk_id`
稳定排序。

每个 `RetrievalHit` 同时返回：

- Vector/FTS 原始 rank；
- 余弦相似度与 FTS rank score；
- RRF 权重、`k` 和融合分；
- 来源类型、信任层级、Canonical URL、快照和抓取时间。

### 4.3 可选 reranker

Reranker 只能重排已通过项目与发布门禁的候选，不能注入新 Chunk ID。输出必须是
`0..1` 的有限数；未知 ID、NaN 或越界分数直接报错。未配置 reranker 时不产生
额外模型调用。

## 5. 过滤器

M3 首版显式支持：

- `source_ids`
- `source_kinds`
- `trust_tiers`
- `canonical_urls`
- `product_ids`
- `public_source`
- `fetched_after`
- `heading_contains`
- `chunk_metadata`
- `source_metadata`

未知过滤器直接报错，不能静默忽略。`product_ids` 通过同项目
`knowledge_product_source_evidence` 约束来源快照，不能只匹配名称字符串。

## 6. 关键不变量

- 混合检索不能扩大 M1 的项目、发布状态、当前快照或 Embedding 模型边界；
- Blog 不得进入任何 Evidence Pack 或段落证据链接；它只能由正文生成链路单独检索为可引用的写作素材；
- 排序解释必须和实际使用的分数一致；
- reranker 永远不能把未通过 SQL 门禁的 Chunk 带入结果；
- RetrievalPlan 和 Evidence Pack 必须绑定 `outline_version`；
- 大纲版本变化后，旧 Evidence Pack 不能供新正文使用；
- 硬事实 EvidenceLink 必须是句子级并指向 `hard_fact` 来源；
- 正文修改导致段落哈希变化时，旧链接必须进入待重校验状态。

## 7. 实施台账

| 日期 | 切片 | 状态 | 验收证据 |
|---|---|---|---|
| 2026-07-30 | M3 分支与架构台账 | 完成 | 分支从 M2 `572139c` 创建；本文记录职责与公式 |
| 2026-07-30 | BasicHybridRetriever | 完成 | 项目/模型隔离、RRF 双通道、元数据/产品过滤、可选 rerank 集成测试 |
| 2026-07-30 | PostgreSQL FTS GIN 索引 | 完成 | Alembic `20260730_0004` 已完成降级、升级与重复升级验收 |
| 2026-07-30 | RetrievalPlan 与 SectionEvidencePack | 完成 | `20260730_0005`；大纲版本、Scope 隔离、充分度、幂等持久化 |
| 2026-07-30 | EvidenceLink 与覆盖率 | 完成 | 段落比例、硬事实句子覆盖、当前发布快照门禁、段落哈希失效重校验 |
| 2026-07-30 | API 与章节证据视图 | 完成 | 每个 Scope 显示状态、来源快照、信任层和排序解释；正文编辑器绑定留给后续工作流接入 |
| 2026-07-30 | 完整回归与真实数据隔离 | 完成 | 后端 366 项通过、1 项跳过；前端 lint/build 通过；数据库测试改为项目级清理，qewitfastener 真实切片保留 |

## 8. 重构检查清单

1. 两个通道是否仍使用同一项目/发布/快照/模型门禁？
2. 是否仍能解释每个命中的 Vector、FTS、RRF 和 reranker 分数？
3. 产品过滤是否仍依赖产品证据关系，而不是模糊名称？
4. Evidence Pack 是否仍绑定确切大纲版本和 Scope？
5. 硬事实是否仍要求句子级 EvidenceLink？
6. 旧段落哈希的链接是否会被错误计入当前覆盖率？
