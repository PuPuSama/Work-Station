# Agent 学习与正式交付双轨计划

## 项目定位

这个项目同时承担两个目标：

1. 改进现有文章工作台，让产品选择、知识检索、证据引用和官网补查更可靠。
2. 形成一个能在面试中讲清楚的 AI 应用工程项目，重点体现 Python 后端、RAG、Agent 编排、评测和 Human-in-the-loop，而不是只展示一个聊天框。

目标岗位定位为“具备 Python 后端能力的 AI 应用工程师”。第一阶段不追求一次性实现完整多租户 SaaS，也不把整条文章生产流程改造成 Agent。

## 双轨学习规则

正式代码和学习实验放在同一个仓库，但相互隔离：

```text
article-agent/
├─ backend/                 正式后端
├─ frontend/                正式前端
└─ learning_labs/           学习实验，不进入正式打包
```

协作方式：

- Codex 负责正式工程骨架、接口边界、迁移、安全约束、测试框架和代码审查。
- 学习者先完成实验中的核心 TODO，再由 Codex 评审、提问和给出参考实现。
- 对应实验没有通过测试、学习者不能解释数据流之前，正式模块不合入主工作流。
- `learning_labs/` 不读取真实密钥，不修改客户任务，不进入便携版或交付包。

每个实验目录包含：

```text
README.md
starter.py
test_*.py
notes.md
interview.md
reference_solution.py   # 评审后再提供
```

## 学习实验顺序

### Lab 01：文档变成规范化文本

读取简单 TXT/Markdown/DOCX，输出统一的 `ParsedDocument`，理解解析器和知识库不是同一层。

### Lab 02：文本切块与元数据

自己实现按标题、段落和重叠窗口切块，为每个 chunk 保留来源、标题路径和位置。

### Lab 03：玩具向量与相似度

使用手工提供的 3 维向量计算余弦相似度。这里故意不调用 API，目标是看懂“语义接近”如何变成数值排序。

### Lab 04：手写 Top-K 检索

在少量玩具向量上实现排序、Top-K 和简单元数据过滤，观察过滤发生在召回前后会有什么差别。

### Lab 05：真实 Embedding API

调用自建 OpenAI 兼容网关的 `/v1/embeddings`，使用 `text-embedding-3-small` 把真实英文产品资料转换为向量，并替换 Lab 03/04 的玩具向量。

配置逻辑分离，但可复用同一个网关地址和密钥：

```env
LLM_BASE_URL=https://api.pu0.me/v1
LLM_MODEL=gpt-5.6-sol
EMBEDDING_BASE_URL=https://api.pu0.me/v1
EMBEDDING_MODEL=text-embedding-3-small
```

密钥只放 `.env`，不能写入实验、日志或 Git。首版使用模型默认 1536 维，不提前降维。

### Lab 06：PostgreSQL + pgvector

把 chunk 和 Embedding 写入 pgvector，完成单项目相似度查询、索引和 `project_id` 隔离。

### Lab 07：混合检索与评测

组合关键词/全文检索、向量检索、元数据过滤和结果融合，计算 Recall@5、MRR、错误来源率和查询延迟。

### Lab 08：Evidence Pack 与引用

把检索结果整理成章节证据包，让生成内容中的硬事实能回到具体 KnowledgeChunk 和官网来源。

### Lab 09：不使用 LangGraph 的 Agent 循环

先用普通 Python 写出“检索 -> 判断证据 -> 官网补查 -> 再检索 -> 停止”的有界循环，理解状态、工具、条件分支和最大轮次。

### Lab 10：迁移到 LangGraph

把 Lab 09 迁移为 State、Node、Conditional Edge、Checkpointer 和 interrupt，加入失败恢复与人工确认。完成后再进入正式研究 Agent。

## 第一个真实垂直切片

固定使用 `www.qewitfastener.com / topic_006`：

- 话题与 woodscrews 有关。
- 优先知识范围为官网分类：`/category/fasteners/screws/woodscrews-dry-wall-screws/`。
- Product 详情页可以成为候选产品；Blog/Guide 可以作为写作参考，但不能作为产品。
- 保存 `page_type`、`canonical_url`、面包屑、分类路径、产品名、正文片段和图片证据。
- 检索时先约束项目和页面类型，再结合精确分类、关键词和向量相关性排序。
- 相邻分类产品只有在证据充分且人工确认后才能替代精确分类产品。

垂直切片的演示闭环：

```text
同步 WordPress 官网
  -> 识别分类页、产品页和 Blog
  -> 切块并生成 Embedding
  -> topic_006 检索 woodscrews 分类与产品
  -> 构建产品/章节 Evidence Pack
  -> 证据不足时由 LangGraph 受限补查
  -> 人工确认模糊候选
  -> 生成可回溯来源的正文
```

## 20 条产品检索评测集

不能只以 topic_006 成功作为验收。建立约 20 条 qewitfastener 话题的标准答案，覆盖：

- 分类明确的话题。
- 容易把 Blog 当成 Product 的话题。
- 相邻产品分类容易混淆的话题。
- 官网资料不足、应该告警而不是猜测的话题。

每条至少保存：

```text
topic_id
topic_text
expected_category_url
allowed_page_types
forbidden_urls
expected_product_urls       # 可为空或后补
label_evidence
notes
```

人工只需确认正确官网分类和明显错误页面；具体产品可以利用官网面包屑、分类关系和产品页证据辅助标注。评测至少输出：

- 分类页 Recall@5。
- 产品页/Blog 分类准确率。
- MRR。
- 错误来源率。
- 无证据时的正确拒绝率。
- Agent 补查前后的证据提升。

## Agent 轨迹和日志

前端增加默认折叠的“研究记录”面板，展示：

- 当前 LangGraph 节点和状态变化。
- 检索词、命中的文档 ID、分数和页面类型。
- 工具调用、补查轮次、人工确认点和失败原因。
- Evidence Pack 使用的必要片段和来源链接。
- 耗时、重试次数和费用估算。

轨迹保存结构化事件，不永久复制完整客户文档和全部 Prompt。API Key 必须脱敏；默认保留 30 天，后续由项目级策略覆盖。日志过期不影响正式 KnowledgeSource、Evidence Pack、正文版本和审计结果。

## 对话分两阶段实施

### 第一阶段：只读研究对话

允许询问：

- 为什么选择这个产品？
- 为什么 Blog 没有被当成产品？
- 这条规格的证据在哪里？
- 哪些章节资料不足？
- 还有哪些同分类产品？

回答必须带来源；资料不足时明确拒绝。对话不能修改任务、大纲、产品或正文。

### 第二阶段：受限执行对话

只开放白名单动作：

1. 重新检索当前文章的产品。
2. 展示影响范围并经人工确认后替换产品。
3. 创建正文版本快照后重写指定章节。

每个动作都经过“意图解析 -> 命令预览 -> 权限/状态检查 -> 人工确认 -> 确定性 API -> 结果验证 -> 审计记录”。模型不能直接执行任意 SQL，也不能绕过 ZeroGPT、版本冲突、链接恢复和人工审核。

## 建议迭代节奏

### 第 1 周：把 RAG 基础学明白

- 创建 Lab 01–04 的 TODO、测试和说明。
- 完成文档规范化、切块、余弦相似度和 Top-K。
- 建立 20 条评测集模板，先标注 topic_006 和另外 4 条。
- 正式工程只建立接口与 feature flag，不接管现有工作流。

### 第 2 周：真实向量与存储

- 完成 Lab 05–06。
- 验证自建网关的 Embedding 请求。
- 启动 PostgreSQL + pgvector，完成 qewitfastener 来源、chunk 和向量入库。

### 第 3 周：混合检索和产品选择

- 完成 Lab 07–08。
- 跑完约 20 条评测集，记录基线指标。
- 解决 Product/Blog 分类、精确产品分类约束和 Evidence Pack。

### 第 4 周：真正理解 Agent

- 完成 Lab 09，再迁移 Lab 10。
- 正式接入有界 LangGraph 研究子图、检查点和人工中断。
- 验证请求失败后恢复，而不是整条流程重跑。

### 第 5 周：前端解释与只读对话

- 加入资料研究步骤、研究记录和证据查看器。
- 加入带引用的只读研究对话。
- 完成可复现演示脚本、架构图和指标截图。

### 后续：受限执行和多人化

- 增加三个白名单对话动作及回滚/审计。
- 再评估 LightRAG 对照实验、MinerU 服务、RBAC、云端部署、GSC 和 Semrush。

## 第一阶段完成标准

- 能解释从文档、chunk、Embedding、Top-K、混合检索到 RAG 回答的完整数据流。
- topic_006 能检索到正确 woodscrews 分类，Blog 不会成为产品。
- 约 20 条评测集能输出可重复指标，而不是只凭人工观感。
- LangGraph 的每个节点、分支、检查点和人工暂停都能在前端回放。
- 正文硬事实有证据，资料不足时会告警并省略 unsupported claim。
- 现有标题、大纲、正文、ZeroGPT、图片和交付流程在 feature flag 关闭时完全不受影响。

