# Knowledge Agent M2 架构与实施台账

> 状态：进行中
>
> 分支：`feature/knowledge-agent-m2`
>
> 上游检查点：M1 合并提交 `150af2a`；已同步 main `05917ac`

## 1. 文档目的

这份文档记录 M2 的模块边界、数据流、关键不变量和实施进度。它不是一次性方案说明，而是后续重构时的“结构痕迹”：

- 代码放在哪里，为什么放在那里；
- 每个接口负责什么，不负责什么；
- 哪些约束属于业务不变量，重构时不能破坏；
- 哪些实现只是当前阶段选择，后续可以替换；
- 每个纵向切片的验收证据是什么。

代码变化如果改变了这里描述的职责或数据流，必须同步更新本文档。

## 2. M2 目标与非目标

M2 负责把真实资料变成可审阅、可发布、可追溯的知识：

1. 解析 DOCX、PDF、Excel 和官网页面；
2. 将不同格式统一为 `ParsedDocument`；
3. 保存原始文件、规范化产物、不可变快照和图片资产关系；
4. 让运营人员在项目级知识库页面确认来源分类和信任级别；
5. 发布后复用 M1 的 Embedding、快照激活和项目级检索。

M2 不实现混合检索、Evidence Pack、LangGraph、RBAC 或 S3。它们分别属于 M3、M4 和 M7。

## 3. 当前模块地图

| 模块 | 责任 | 明确不负责 |
|---|---|---|
| `knowledge_agent.ingestion.contracts` | 定义解析输入、规范化块、内嵌资产和解析结果 | 不读取文件、不访问数据库 |
| `knowledge_agent.ingestion.parsers` | 选择解析器并把文件转成 `ParsedDocument` | 不切知识块、不生成 Embedding、不发布来源 |
| `knowledge_agent.ingestion.mineru` | 把外部 MinerU `content_list.json` 和图片规范化为 `ParsedDocument` | 不在 FastAPI 进程安装/运行 MinerU 模型 |
| `knowledge_agent.ingestion.benchmark` | 对同一输入记录解析质量、耗时和 Python 峰值内存 | 不替代人工标注，不伪造 GPU/外部服务资源数据 |
| `knowledge_agent.repository` | 保存 M1 来源、快照、知识块和向量 | 不解析文档、不保存 Evidence Pack |
| `knowledge_agent.artifact_store` | 内容寻址地保存原始、规范化和图片文件 | 不决定发布状态、不保存业务元数据 |
| `knowledge_agent.assets` | 按项目去重不可变资产，并关联来源快照 | 不选择文章首图、不生成图片向量 |
| `knowledge_agent.catalog` | 保存稳定产品身份和不可变来源/图片证据 | 不执行网页抓取、不直接修改文章任务 |
| `knowledge_agent.wordpress` | 定义官网抓取边界、WordPress REST 探测、规则分类和分类页链接发现 | 不写数据库、不发布来源、不确认产品 |
| `knowledge_agent.web_ingestion` | 编排官网 HTML 快照、产品证据、原图下载和分类页同步 | 不把 Inbox 自动发布、不把候选产品自动确认 |
| `knowledge_agent.library` | 为知识库页面提供项目级只读汇总 | 不修改来源或产品状态 |
| `knowledge_agent.http` | 暴露入库、列表、证据打开和产品确认路由 | 不包含 SQL、不绕过 Repository |
| `knowledge_agent.runtime` | 组装长生命周期数据库和 Artifact Store 适配器 | 不创建或迁移 Schema |
| `knowledge_agent.embedding` | 调用独立的 Embedding Provider | 不决定资料是否可信或可发布 |
| `knowledge_agent.retriever` | 检索已发布的当前快照 | 不检索 Inbox 或未确认资料 |
| 现有 `services.product_*` | 任务级产品发现、官网事实提取和图片候选选择；M2 复用其经过测试的 DOM 产品解析器和安全出站请求 | 不是长期知识库的权威存储，不拥有 M2 来源/快照/产品身份 |

已经落地的编排模块：

| 模块 | 责任 |
|---|---|
| `knowledge_agent.ingestion.chunking` | 将规范化块稳定地转换为 `KnowledgeChunk` |
| `knowledge_agent.ingestion.service` | 编排上传、哈希去重、解析、快照保存和待审状态 |
| `/api/knowledge/...` | 提供上传、站点探测、分类页同步、来源审阅、Embedding 和发布动作 |
| `project-knowledge-library.tsx` | 提供 WordPress 探测/同步、私有上传、来源证据和发布操作 |

## 4. 规范化解析契约

### 4.1 `DocumentInput`

上传边界只接收文件名、媒体类型和原始字节。内容哈希由契约计算，不能信任客户端提交的哈希。

### 4.2 `ParsedBlock`

所有格式都转成有顺序的块：

- `heading`：标题；
- `paragraph`：普通段落；
- `table_row`：表格或工作表的一行；
- `page_text`：PDF 单页文字。

每个块携带 `ordinal`、`heading_path` 和格式相关 `locator`。未来重切 chunk 时，可以回到原页、原表格或原工作表行。

### 4.3 `ParsedAsset`

解析器只提取文件内部已有的资产及其原始字节，不决定资产属于哪个产品，也不决定是否可用于文章。内容哈希在本地计算。

### 4.4 `ParsedDocument`

解析结果包含：

- 原文件身份和 SHA-256；
- 实际使用的解析器名称与版本；
- 规范化块；
- 内嵌资产；
- 标题和非权威元数据。

`ParsedDocument` 仍然是“待审解析结果”，不是已发布知识。

## 5. 数据流

```text
Private upload
  -> DocumentInput
  -> DocumentParserRouter
  -> ParsedDocument
  -> PrivateDocumentIngestionService

Official site
  -> OfficialSiteFetcher (同项目域名 + DNS/重定向安全边界)
  -> WordPressSiteProbe / classify_web_page
  -> ClassifiedWebPage (标签 + 置信度 + 逐条理由)
  -> OfficialWebPageIngestionService
  -> ParsedDocument-compatible blocks

Both paths
  -> 稳定切块 + 图片资产登记
  -> KnowledgeSource(status=inbox)
  -> SourceSnapshot + KnowledgeChunk(no embedding)
  -> EmbeddingProvider
  -> store_embeddings
  -> 人工确认分类和信任级别
  -> activate_snapshot
  -> published/current snapshot retrieval
```

解析、Embedding 或人工确认任一步失败时，旧的已发布快照继续服务。

### 5.1 WordPress 分类页同步时序

```text
POST /wordpress/sync
  -> 验证 site_url 与 project_id 同域
  -> WordPress REST Probe (/wp-json/，失败再 ?rest_route=/)
  -> 抓取并确认 category_url == product_category
  -> 保存分类页原始 HTML、规范化 JSON、Inbox 快照
  -> 从产品容器发现同域详情候选（有数量上限）
  -> 逐页抓取和分类
      -> product_detail: 保存页面、产品身份、primary_detail 证据、原图证据
      -> Blog/Guide/unknown: 不创建产品；记录跳过原因
  -> 返回同步摘要；不自动发布、不自动确认产品
```

分类和同步是两个边界：`classify_web_page` 是无网络、无数据库的纯函数；
`OfficialWebPageIngestionService` 只接收已抓取资源和分类结果完成持久化。
未来把规则分类替换为模型分类时，仍需输出 `page_type + confidence + reasons`。

## 6. 产品与图片的长期模型

现有 `Product` 是文章任务的选择结果，不直接作为知识库权威数据。M2 的长期模型遵循：

1. 产品是稳定业务身份，可以关联详情页、分类页和私有规格书等多个来源；
2. 产品事实必须能追溯到具体 `source_id + snapshot_id + locator`；
3. 图片二进制不放 PostgreSQL，数据库只保存哈希、尺寸、媒体类型和产物 URI；
4. 同一图片按项目内 SHA-256 去重，另记录它在不同快照中的出现位置；
5. 原图不可变，文章使用的 WebP 是派生资产；
6. 文章任务保存产品、快照和资产选择的引用或副本，不能依赖会被覆盖的“当前图片”；
7. Alt、Caption 和邻近正文可进入文字检索；M2 不把图片向量写进 M1 的 `vector(1536)`。
8. 官网图片只有在成功下载并通过图片解码后才成为 `KnowledgeAsset`；外域 CDN、非图片响应和损坏字节只产生警告，不伪造资产记录；
9. 产品详情页图片按 `gallery -> JSON-LD -> body` 排序，首个有效 Gallery/JSON-LD 图片记录为 `primary` 证据，但仍需运营人员确认产品；
10. 当前页面最多下载 12 张原图，单图最多 12 MB；这是可调实现限制，不是长期业务不变量。

M2 本地阶段使用项目级持久目录保存资产，URI 抽象为后续迁移 S3 保留替换边界。

### 6.1 当前关系表

- `knowledge_assets`：项目内按 SHA-256 去重的不可变文件身份；
- `snapshot_assets`：图片在指定来源快照中的出现位置和上下文；
- `knowledge_products`：不随页面版本变化的产品身份；
- `knowledge_product_source_evidence`：产品与详情页、分类页、规格书快照的关系；
- `knowledge_product_asset_evidence`：产品与某次快照中图片证据的关系。

产品只有存在 `primary_detail` 来源证据后才能从 Inbox 确认为 `confirmed`。分类页或 Blog 单独出现不能满足这个门禁。

### 6.2 Artifact Store 的事务边界

文件先按内容哈希写入 Artifact Store，数据库随后登记 URI。数据库失败可能留下无引用的内容寻址文件，但不会覆盖旧资料；未来清理任务可以安全删除没有数据库引用且超过保留期的文件。不能为了模拟跨数据库/文件系统事务而先覆盖旧文件。

## 7. 关键不变量

- 所有正式知识对象必须带 `project_id`；
- 来源、快照、块、产品和资产关系必须通过复合外键防止跨项目串联；
- `published` 来源必须有完整、模型匹配的当前快照；
- 快照和原始资产不可原地覆盖；
- 相同输入和解析器版本必须得到稳定内容哈希和块顺序；
- 解析器不得因为文件名或文档内容泄露密钥；
- Blog/Guide 可以提供写作参考，但不能被自动当作产品详情页；
- 分类页候选链接即使出现在名为 `product` 的容器中，也必须经过详情页二次分类；
- 官网 URL、重定向和图片 URL 必须留在项目官方域名边界，跨域候选不得下载；
- 只有经过确认的产品详情证据才能投影到文章任务的产品选择。

## 8. 可替换点

- 轻量 PDF 解析器以后可替换为 MinerU，但必须保持 `ParsedDocument` 契约；
- 本地资产目录以后可替换为 S3，但数据库仍通过 URI 引用；
- 当前规则分类器以后可加入模型分类，但必须保留分类理由和原始证据；
- 当前同步调用以后可进入 Job Queue/Worker，但状态迁移和幂等键不能变化；
- MinerU 当前适配稳定 legacy `content_list.json`；V2 格式仍在开发变化中，升级时新增 Adapter 版本，不能原地改变旧快照解释；
- 前端页面可重构，API 返回的项目边界和证据定位不能丢失。

## 9. 实施台账

| 日期 | 切片 | 状态 | 验收证据 |
|---|---|---|---|
| 2026-07-30 | M2 分支与架构台账 | 完成 | 独立分支；本文档记录职责、数据流和不变量 |
| 2026-07-30 | 统一文档解析契约与轻量解析器 | 完成 | DOCX/PDF/Excel 路由与契约测试 |
| 2026-07-30 | 稳定切块与私有文档入库编排 | 完成 | 同一输入重试得到同一快照/块身份，默认保持 Inbox |
| 2026-07-30 | 产品与图片资产迁移 | 完成 | 复合 FK、哈希去重、快照证据、产品确认门禁测试 |
| 2026-07-30 | 私有文件 API 和知识库只读页 | 完成 | 上传、重复幂等、来源汇总、打开原文件和前端构建 |
| 2026-07-30 | 来源审阅、Embedding 和发布动作 | 完成 | 人工确认门禁、假向量集成测试、原子激活 |
| 2026-07-30 | WordPress Site Probe 与规则分类 | 完成（模拟站点验收） | 主/备用 REST 入口、跨域拦截、产品/分类/Blog 分类理由测试 |
| 2026-07-30 | 分类页同步、产品与原图证据 | 完成（模拟站点验收） | Inbox 门禁、幂等快照、`primary_detail`、图片哈希/尺寸/快照证据集成测试 |
| 2026-07-30 | 项目级知识库页面 | 完成 | 运营人员可探测/同步官网、上传资料、查看理由和原始证据 |
| 2026-07-30 | `www.qewitfastener.com` 真实切片 | 完成（M2 边界） | 4 个原始快照、36 个分类页 Chunk、3 个产品、19 张原图、真实 Blog 正确拒绝；见 `docs/validation/knowledge-agent-m2-qewitfastener.md` |
| 2026-07-30 | MinerU 规范化与对比框架 | 完成 | legacy `content_list.json` 页码/bbox/表格/图片适配；同输入质量/耗时/内存测试 |
| 待执行 | 代表性私有资料 MinerU 实测 | 等待资料和独立服务 | 同一非提交客户文件的质量、速度、CPU/GPU/服务峰值资源记录 |

## 10. 重构检查清单

后续重构前至少回答：

1. 是否仍能从检索结果追溯到原始来源和快照？
2. 是否仍能证明任意对象属于同一个项目？
3. 新快照失败时，旧快照是否继续服务？
4. 产品和图片是否仍保留选择理由与证据定位？
5. 是否把任务级临时文件误当成长期知识资产？
6. 是否有测试覆盖当前修改所触及的接口不变量？
