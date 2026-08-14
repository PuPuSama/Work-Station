# Knowledge Agent M2 本地运行与验证

本说明对应 `feature/knowledge-agent-m2`。架构职责和重构不变量见 `docs/architecture/knowledge-agent-m2.md`。

## 1. 当前可运行能力

- DOCX、PDF、XLSX、XLSM 轻量解析；
- 私有文件内容哈希、规范化 JSON、稳定 Chunk ID；
- 内嵌图片内容寻址存储和项目内去重；
- 稳定产品身份、来源证据和图片证据；
- `/api/knowledge/{project}` 项目级读模型；
- 私有资料上传到 Research Inbox；
- WordPress REST Site Probe、官网产品分类页同步和 Blog/产品二次分类；
- 官网页面原始 HTML、规范化 JSON、产品事实与原图证据入库；
- MinerU 精准解析 API v4：PDF 签名上传、异步轮询、结果 ZIP 安全解包，
  并通过 `content_list.json` 归一化标题、表格、定位信息和图片；
- 打开 Inbox 或已发布来源的原始证据；
- `/projects/{customer}/knowledge` 项目级知识库页面。

`www.qewitfastener.com / topic_006` 的 M2 真实入库已验收，证据见
`docs/validation/knowledge-agent-m2-qewitfastener.md`。仍需提供不入 Git
的代表性私有资料并启动独立 MinerU 服务，才能填写真实质量/速度/CPU/GPU 对比值。
上传或官网同步成功只表示进入 Inbox；运营人员点击“确认并发布”后，系统才生成
Embedding 并原子激活快照。

## 2. 环境变量

复制 `.env.example` 并配置：

```dotenv
KNOWLEDGE_AGENT_ENABLED=true
ARTICLE_AGENT_DATABASE_URL=postgresql+psycopg://article_agent:article_agent@127.0.0.1:55433/article_agent
ARTICLE_AGENT_KNOWLEDGE_ROOT=D:/Project/article/runtime/knowledge-agent

EMBEDDING_BASE_URL=https://your-gateway.example/v1
EMBEDDING_API_KEY=replace-locally
EMBEDDING_MODEL=text-embedding-3-small
EMBEDDING_DIMENSIONS=1536

# 配置后 PDF/DOCX 使用 MinerU；未配置时继续使用本地解析器。
ARTICLE_AGENT_MINERU_API_KEY=replace-locally
ARTICLE_AGENT_MINERU_BASE_URL=https://mineru.net
ARTICLE_AGENT_MINERU_MODEL_VERSION=vlm
ARTICLE_AGENT_MINERU_LANGUAGE=en
```

`ARTICLE_AGENT_KNOWLEDGE_ROOT` 可省略。省略时使用当前 `data_file` 同级的 `knowledge-agent` 目录。

密钥只写本机 `.env`，不要写进本文档、配置 YAML、测试夹具或提交。
MinerU Key 配置后，PDF/DOCX 解析失败会明确返回错误，不静默回退到本地解析器；
这样运营人员不会误以为 OCR、表格或图片已经由 MinerU 完成。可选超时配置为
`ARTICLE_AGENT_MINERU_TIMEOUT_SECONDS` 和
`ARTICLE_AGENT_MINERU_POLL_INTERVAL_SECONDS`。

## 3. 启动数据库并迁移

在 `D:\Project\article\article-agent-formal`：

```powershell
docker compose -f compose.dev.yaml up -d --wait

$env:ARTICLE_AGENT_DATABASE_URL = 'postgresql+psycopg://article_agent:article_agent@127.0.0.1:55433/article_agent'
backend\.venv\Scripts\python.exe -m alembic -c backend\alembic.ini upgrade head
backend\.venv\Scripts\python.exe -m alembic -c backend\alembic.ini current
```

当前 M2 Head：

```text
20260730_0003
```

不要用应用启动替代 Alembic，也不要为了重跑迁移执行 `docker compose down -v`。

## 4. 页面操作

启动前后端后：

1. 打开任意项目；
2. 功能开关开启时，左侧出现“知识库”；
3. 进入 `/projects/{customer}/knowledge`；
4. 选择 DOCX、PDF、XLSX 或 XLSM；
5. 输入显示名称和建议信任层级；
6. 点击“解析并加入 Inbox”；
7. 在来源列表检查分类理由、版本数、Chunk 数和资产数；
8. 点击“原文件”验证原始证据。
9. 确认分类和信任级别后，点击“确认并发布”生成向量并激活。

WordPress 官网同步：

1. 填写项目官网地址，点击“探测 WordPress”；
2. 查看 REST 探测结果和理由；未发现 REST API 时，HTML 同步仍可工作；
3. 填写官网产品分类页 URL；
4. 点击“同步分类与产品”；
5. 在来源表确认 `product_category`、`product_detail` 和逐条分类理由；
6. 打开官网证据，检查产品名称、分类路径和图片数量；
7. 产品和来源分别人工确认。同步动作本身不会发布来源或确认产品。

重复上传同一个来源和相同内容时复用首次快照，不新增重复快照；相同内嵌图片按项目内 SHA-256 去重。

## 5. API

读取项目知识库：

```http
GET /api/knowledge/{project}
```

上传私有资料：

```http
POST /api/knowledge/{project}/sources/upload
Content-Type: multipart/form-data

file=<binary>
display_name=2026 Product Specification
trust_tier=reference_material
```

打开原始证据：

```http
GET /api/knowledge/{project}/sources/{source_id}/snapshots/{snapshot_id}/raw
```

确认产品：

```http
POST /api/knowledge/{project}/products/{product_id}/confirm
```

审阅并发布来源：

```http
PUT  /api/knowledge/{project}/sources/{source_id}/review
POST /api/knowledge/{project}/sources/{source_id}/publish
```

探测 WordPress：

```http
POST /api/knowledge/{project}/wordpress/probe
Content-Type: application/json

{
  "site_url": "https://www.example.com"
}
```

同步一个产品分类页（当前同步执行，最多 50 个候选）：

```http
POST /api/knowledge/{project}/wordpress/sync
Content-Type: application/json

{
  "site_url": "https://www.example.com",
  "category_url": "https://www.example.com/category/products/",
  "max_products": 12
}
```

安全边界：

- `site_url` 必须属于 URL 路径中的 `{project}` 域名；
- 分类页、重定向和产品页必须留在官方域名或其子域；
- 图片外域、非图片响应、损坏图片和超限资源会被跳过并返回警告；
- REST 探测失败不等于 HTML 同步失败；
- 同步只写 Inbox，不自动发布、不自动确认产品。

真实 qewitfastener 冒烟（在 `backend` 目录）：

```powershell
.\.venv\Scripts\python.exe -m knowledge_agent.m2_site_smoke `
  --project qewitfastener.com `
  --site-url https://www.qewitfastener.com `
  --category-url https://www.qewitfastener.com/category/fasteners/screws/woodscrews-dry-wall-screws/ `
  --max-products 3
```

该命令会写真实 Inbox 数据和 Artifact Root，不会自动清理；重复执行应保持幂等。
输出只含数量和 ID。

产品确认必须已经有 `primary_detail` 来源证据。只有分类页、Blog 或图片候选时会返回冲突，不会将其伪装成已确认产品。

## 6. 验证

后端完整回归：

```powershell
$env:ARTICLE_AGENT_CONFIG = 'config.ci.yaml'
$env:ARTICLE_AGENT_DATABASE_URL = 'postgresql+psycopg://article_agent:article_agent@127.0.0.1:55433/article_agent'
backend\.venv\Scripts\python.exe -m unittest discover -s backend\tests -q
```

前端：

```powershell
cd frontend
npm.cmd run lint
npm.cmd run build -- --webpack
```

重点 M2 测试：

- `test_knowledge_agent_m2_parsers.py`
- `test_knowledge_agent_m2_ingestion.py`
- `test_knowledge_agent_m2_assets.py`
- `test_knowledge_agent_m2_catalog.py`
- `test_knowledge_agent_m2_http.py`
- `test_knowledge_agent_m2_wordpress.py`
- `test_knowledge_agent_m2_web_ingestion.py`
- `test_knowledge_agent_m2_mineru.py`

## 7. 故障判断

- 页面返回 `Knowledge Agent is disabled`：检查 feature flag 和后端是否重启；
- 启动时报 Embedding 配置缺失：M1 配置门禁生效，补齐独立 `EMBEDDING_*`；
- 上传返回不支持格式：轻量路由当前只接受 DOCX/PDF/XLSX/XLSM；
- PDF 没有可提取文本：保留 Inbox 失败信息，后续用 MinerU 对比流程处理；
- 原文件 404：检查数据库 URI 是否仍位于配置的 Artifact Root；路由不会读取 Root 外任意文件；
- 产品不能确认：先检查是否存在同项目、同快照的 `primary_detail` 证据。
- WordPress 探测未识别：检查 `/wp-json/` 或 `?rest_route=/` 是否公开；仍可尝试分类页 HTML 同步；
- 分类页返回 422：该 URL 没有被识别为产品分类页，先检查是否误填 Blog、详情页或跨域 URL；
- 图片数量为 0：查看同步警告，常见原因是图片位于外域 CDN、响应不是图片或原图损坏。
