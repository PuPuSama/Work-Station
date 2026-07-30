# Knowledge Agent M3：证据计划、证据包与覆盖率结构记录

> 状态：核心后端已实现，API 和前端视图待接入
> 迁移：`20260730_0005`
> 代码入口：`knowledge_agent.contracts`、`knowledge_agent.evidence`、
> `knowledge_agent.evidence_repository`

## 1. 为什么分成三层

M3 不把一次搜索结果直接交给正文生成，而是保留三个不同生命周期的对象：

1. `RetrievalPlan` 是“这版大纲需要查什么”。它绑定 `article_id` 和
   `outline_version`，并把 introduction、每个 H2、产品事实和 FAQ 拆成独立 Scope。
2. `SectionEvidencePack` 是“某个 Scope 当时实际查到了什么”。它冻结排序后的
   Chunk、来源快照、信任层级、公开引用 URL、充分度和缺口原因。
3. `EvidenceLink` 是“正文里的哪个段落或句子最终使用了哪个 Chunk”。它服务于
   覆盖率、硬事实校验和编辑后的失效检查。

这样做的关键价值是：大纲变化、知识刷新和正文编辑是三种不同事件，不会因为复用
同一个模糊的 `evidence` JSON 而相互污染。

## 2. 代码职责

| 模块 | 负责 | 不负责 |
|---|---|---|
| `contracts.py` | 跨模块不可变对象、项目/版本/粒度校验 | SQL、检索和 HTTP |
| `evidence.py` | 确定性 Evidence Pack ID、充分度、公开引用、覆盖率计算 | 数据库、外部搜索 |
| `evidence_repository.py` | RetrievalPlan、Pack、Link 的 PostgreSQL 持久化和发布门禁 | 判断自然语言是否真的被证据支持 |
| `hybrid_retriever.py` | 生成带来源快照与排序解释的候选 Hit | 决定正文怎样引用 |
| `schema.py` / Alembic | 复合外键、唯一约束、粒度 CHECK | 应用启动时自动建表 |

## 3. 数据流

```mermaid
flowchart LR
    O["已确认大纲与版本"] --> P["RetrievalPlan"]
    P --> S["RetrievalScope"]
    S --> R["BasicHybridRetriever"]
    R --> B["DefaultEvidencePackBuilder"]
    B --> E["SectionEvidencePack"]
    E --> W["正文生成或人工写作"]
    W --> L["EvidenceLink"]
    L --> C["KnowledgeCoverageReport"]
```

### 3.1 RetrievalPlan

- 同一项目、文章和大纲版本只允许一个 Plan。
- Plan 内 `scope_id`、`ordinal` 以及 `(scope_type, scope_key)` 都必须唯一。
- 每个 Scope 保存查询变体、过滤器、最低命中数、最低不同来源数以及是否要求
  hard-fact 证据。
- `max_gap_fill_rounds` 只能是 0 到 2。M3 只保存边界，M4 才执行补查编排。

### 3.2 SectionEvidencePack

- Pack 必须同时匹配项目、Plan、Scope、文章和大纲版本；这些关系由复合外键保证。
- `DefaultEvidencePackBuilder` 不调用模型。相同请求和相同有序 Chunk 身份会生成
  相同 `ep_<sha256>`，便于幂等重试和缓存。
- 没有命中为 `missing`；有命中但未满足最少命中、不同来源或 hard-fact 要求为
  `weak`；全部满足才是 `sufficient`。
- `evidence_pack_hits` 保存实际排名、分数、来源快照和检索解释。它保存的是当时的
  审计快照，不因以后重排而改写。
- Pack 不提供 EvidenceLink 的伪实现。候选证据和正文实际使用证据仍是两件事。

### 3.3 EvidenceLink

- Link 只能指向同项目中已发布来源的当前快照 Chunk。
- 普通知识支持可绑定段落；硬事实必须绑定句子，并且来源的 `trust_tier` 必须是
  `hard_fact`。
- 公开来源的 `public_citation_url` 必须与当前来源 Canonical URL 完全一致；私有
  来源不得伪造公开 URL。
- Link 保存正文段落的 SHA-256。正文变化后，Repository 将旧哈希的 `valid`
  Link 批量标记为 `needs_review`，不能沿用旧覆盖率。

## 4. 两种覆盖率不能合并

正文知识支持率：

```text
有至少一个当前有效 Link 的合格段落数 / 合格正文段落数
```

合格段落由正文解析层给出；当前首版还会排除少于 5 个可见词的片段。

硬事实覆盖率：

```text
有有效 sentence + hard_fact Link 的硬事实句子数 / 全部硬事实句子数
```

一个段落被计入知识支持率，不代表其中每个数字都已验证。硬事实默认要求 100%，
所以 UI 和发布门禁必须分别展示、分别判断这两个指标。

## 5. PostgreSQL 约束

迁移 `20260730_0005` 新增：

- `retrieval_plans`
- `retrieval_scopes`
- `evidence_packs`
- `evidence_pack_hits`
- `evidence_links`

所有主身份都包含 `project_id`。Pack 到 Plan/Scope、Pack Hit 到 Chunk、Evidence
Link 到 Chunk 均使用项目级复合外键，不能把另一个客户的对象拼进当前文章。

数据库只能验证结构关系。以下动态规则在 Repository 事务中检查：

- Chunk 是否仍属于已发布来源的当前快照；
- hard-fact Link 的来源信任层是否为 `hard_fact`；
- 公开引用 URL 是否与来源可见性和 Canonical URL 一致。

## 6. 已覆盖的验收场景

- Plan/Scope 跨项目拒绝、重复序号拒绝、最多两轮补查。
- Plan 和 Pack 相同内容幂等重试；相同 ID 不同内容冲突。
- Pack 大纲版本与 Plan 不匹配时由复合外键拒绝。
- Pack 完整往返保留 Hit 顺序、来源快照、检索解释和公开引用。
- 旧快照 Chunk、未发布 Chunk 和 reference-material 硬事实 Link 拒绝。
- 段落哈希变化后旧 Link 进入 `needs_review`。
- 覆盖率忽略短片段、旧哈希和非句子级硬事实 Link。

## 7. 后续重构时必须保持的不变量

1. Plan、Pack 和 Link 的生命周期仍然分离。
2. Pack 仍绑定明确的大纲版本，不能按 `article_id` 模糊取“最新一个”。
3. Pack 中的 Hit 必须保留来源和快照身份，不能退化成无来源文本数组。
4. EvidenceLink 只认当前已发布 Chunk；知识刷新后必须重新校验。
5. 段落支持率与硬事实句子覆盖率仍然是两个指标。
6. hard-fact 仍要求句子级 Link 和 `hard_fact` 信任层。
7. 所有读取和写入仍以 `project_id` 为第一层边界。

## 8. 尚未完成

- 后端 HTTP：创建/读取 Plan、执行 Scope 检索、保存/读取 Pack、保存 Link、查询覆盖率。
- 前端：按 Scope 查看充分度、命中来源和排序解释；分别显示两个覆盖率。
- M4：LangGraph 有界补查、检查点和人工中断。
- 自然语言相关性校验：M3 目前保证链接结构合法，不声称自动证明文本语义正确。
