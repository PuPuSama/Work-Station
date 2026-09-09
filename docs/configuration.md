# 配置指南

适用于当前 Server 运行时。变量名、默认值和调用协议已按 `backend/config.py` 与各服务客户端核对；本文不包含生产密钥，也不代表供应商连通性已经通过验证。

## 1. 配置文件放在哪里

| 文件/变量 | 作用 |
| --- | --- |
| 根 `.env` | 推荐的密钥、连接地址和运行开关入口；从 [.env.example](../.env.example) 复制 |
| `config.yaml` | 非敏感默认值：模型、提示词路径、并发、Word 格式 |
| `config.docker.yaml` | 通过 `extends` 继承基线，覆盖容器路径与默认功能开关 |
| `config.ci.yaml` | CI 隔离测试配置；不是生产模板 |
| `config.local-debug.yaml` | 免登录调试脚本专用，不用于真实生产数据 |
| `ARTICLE_AGENT_CONFIG` | 选择应用 YAML；相对路径按仓库根目录解析 |
| `ARTICLE_AGENT_ENV_FILE` | 在启动进程环境中设置，令应用只读取指定 dotenv 文件 |
| `ARTICLE_ENV_FILE` | Compose 选择宿主机 `env_file`；不是应用内的文件路径 |

应用环境优先级：**进程已有变量 > 根 `.env` > `backend/.env` 中尚未定义的键**。指定 `ARTICLE_AGENT_ENV_FILE` 后不会再读两份默认文件。空值仍算已定义：根 `.env` 的空密钥会阻止读取旧 `backend/.env` 中的同名密钥。

YAML 先继承、再由对应环境变量覆盖。部分模型选择另有用户级数据库设置，见第 3 节。Compose 在 `environment` 中指定的值优先于 `env_file`，因此容器始终选用 `/app/config.docker.yaml`。

首次复制（不要覆盖已有文件）：

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

密钥只填到未跟踪的 `.env` 或运维秘密管理系统。不要提交真实值；不要用 `replace-me` 充当密钥，它会被某些客户端误判为已配置。

## 2. 基础服务与登录

| 配置 | 填写要求 |
| --- | --- |
| `ARTICLE_AGENT_DATABASE_URL` | `postgresql+psycopg://USER:PASSWORD@HOST:5432/DATABASE`；密码含 `@`、`:`、`/` 等字符时需要 URL 编码 |
| `ARTICLE_AGENT_SERVER_SESSION_SECRET` | 独立随机长字符串，至少 32 字节；持久保存，变更会影响已有会话 |
| `APP_COOKIE_SECURE` | 生产 HTTPS 为 `true`；仅受控本机 HTTP 调试使用 `false` |
| `ARTICLE_AGENT_SERVER_MODE` | 保持 `true`；不是启用旧 Local 存储的开关 |

PostgreSQL 要支持 pgvector。当前 CI 使用 PostgreSQL 17。数据库结构只由 Alembic 迁移，LangGraph checkpoint 表通过 `python -m knowledge_agent.checkpoint_setup` 显式初始化；应用启动不自动建表。

组织用户的密码哈希保存在 PostgreSQL，账号由有权限的管理员管理。以下是**已有用户尚无密码哈希时的一次性引导兼容项**，不是创建账号的命令：

| 配置 | 含义 |
| --- | --- |
| `ARTICLE_AGENT_LOGIN_USERNAME` / `ARTICLE_AGENT_LOGIN_PASSWORD` | 成对填写，用于该引导账号首次登录 |
| `ARTICLE_AGENT_LOGIN_ORGANIZATION_ID` | 限定已有组织；避免不同组织的同名用户产生歧义 |
| `ARTICLE_AGENT_LOGIN_USER_ID` | 对应已有用户 ID；省略时用 username |
| `ARTICLE_AGENT_LOGIN_SESSION_SECONDS` | 引导设置中的会话期限，默认 43200 秒；数据库专用设置使用内置默认期限 |

用户已有数据库密码哈希后，环境密码不再覆盖它；在 `.env` 改密码不会重置该用户。当前应用不要求 OIDC 的 issuer/client secret，也没有开放自助创建首个组织的安装页面。旧 `m7_first_admin_bootstrap` 仍依赖历史 OIDC 设计，不能当作密码登录的一键安装脚本。

## 3. 大模型 API：写文章、生成标题和研究

| 配置 | 含义与示例 |
| --- | --- |
| `LLM_API_KEY` | 供应商或自建网关签发的 API Key，不是网页账号密码 |
| `LLM_BASE_URL` | API 前缀，例如 `https://gateway.example.com/v1` |
| `LLM_MODEL` | 网关实际提供的模型 ID；模板沿用仓库默认名，使用前核实可用性 |
| `LLM_REASONING_EFFORT` | 模型支持的推理档位，例如 `medium`；必须与网关/模型匹配 |

当前 `backend/services/llm.py` 发送：

```text
POST {LLM_BASE_URL}/responses
Authorization: Bearer <LLM_API_KEY>
stream: true
```

因此 BASE_URL 不要带 `/responses`、`/chat/completions` 或网页管理后台路径。仅支持 Chat Completions 的“OpenAI 兼容”网关不能直接替代当前接口；网关还需要兼容 `input`、`reasoning.effort`、`max_output_tokens` 和 Responses 流式事件。

`OPENAI_API_KEY` 仅作为旧 LLM 密钥回退，建议统一使用 `LLM_API_KEY`。前端不会设置或接收模型 API Key；账号设置中的模型/推理档位保存为用户选择。接入 `ServerLlmClientFactory` 的任务优先使用这些用户选择，否则回退到部署默认；标题生成使用 `low` 档。修改默认模型不会自动替换用户已经保存的选择。

模型下拉候选来自 YAML 的 `llm.available_models` 和 `llm.available_reasoning_efforts`，环境指定的默认模型/档位也会加入候选。列表不是对供应商可用模型的实时查询。

## 4. Embedding API：知识入库与检索

| 配置 | 填写要求 |
| --- | --- |
| `EMBEDDING_BASE_URL` | 独立 API 前缀，例如 `https://embedding.example.com/v1` |
| `EMBEDDING_API_KEY` | Embedding 服务的 Key；即使与 LLM 相同也要单独填写 |
| `EMBEDDING_MODEL` | 提供方的向量模型 ID；仓库默认 `text-embedding-3-small` |
| `EMBEDDING_DIMENSIONS` | **1536**，与数据库和检索契约一致 |

客户端调用 `POST {EMBEDDING_BASE_URL}/embeddings`，发送 `model`、`input`、`encoding_format: float` 和 `dimensions: 1536`。**不会回退到 LLM 的地址或密钥。** 文档向量和查询向量必须来自同一模型与向量空间。

不能只改 `EMBEDDING_DIMENSIONS` 来切换到 1024/3072 维。即使维度相同，更换 Embedding 模型也需要评估并重建已有索引，不能混用旧向量。CLIProxyAPI 或其他写作网关是否提供此接口，需要单独核实。

填好后将 `KNOWLEDGE_AGENT_ENABLED=true`。只填写 Key 而未开启该功能，知识 runtime 仍不会启动；开关开启但必要 Embedding 配置缺失时启动会报错。

## 5. MinerU：PDF 解析

| 配置 | 当前默认/作用 |
| --- | --- |
| `ARTICLE_AGENT_MINERU_API_KEY` | MinerU API Token；为空时使用本地解析器 |
| `ARTICLE_AGENT_MINERU_BASE_URL` | `https://mineru.net`，服务根地址，不追加 `/api/v4` |
| `ARTICLE_AGENT_MINERU_MODEL_VERSION` | `vlm`；代码接受 `vlm` 或 `pipeline` |
| `ARTICLE_AGENT_MINERU_LANGUAGE` | `en`，按资料语言及提供方支持设置 |
| `ARTICLE_AGENT_MINERU_TIMEOUT_SECONDS` | 300 秒 |
| `ARTICLE_AGENT_MINERU_POLL_INTERVAL_SECONDS` | 3 秒 |

当前适配器申请 `/api/v4/file-urls/batch` 的上传地址，上传 PDF，再轮询 `/api/v4/extract-results/batch/{batch_id}` 并下载解析结果。Token 不是大模型 Key，也不是 Embedding Key。

启用后 **只有 PDF 使用 MinerU**；DOCX 和 XLSX/XLSM 仍使用本地解析器。未配置 MinerU 时，PDF 使用本地文本解析，复杂版面和扫描件效果受原文件可提取文本限制。已选中 MinerU 后请求失败不会静默换成本地解析。

使用该功能时，选中的 PDF 会传给配置的解析服务；只处理允许上传给该服务的资料。此配置并不自动将解析结果发布到知识库，仍需经过现有审核/发布流程。

## 6. 搜索、AI 检测与 WordPress

| 服务 | 配置 | 用途/注意事项 |
| --- | --- | --- |
| Tavily | `TAVILY_API_KEY` | 联网搜索；当前客户端使用 `https://api.tavily.com`，没有通用 `TAVILY_BASE_URL` 环境开关 |
| ZeroGPT | `ARTICLE_AGENT_ZEROGPT_API_KEY` | 自动检测 API；旧名 `ZEROGPT_API_KEY` 仅作回退 |
| ZeroGPT 地址 | `ARTICLE_AGENT_ZEROGPT_BASE_URL` | 默认 `https://api.zerogpt.com`，客户端追加 `/api/detect/detectText` |
| WordPress 凭据加密 | `ARTICLE_AGENT_WORDPRESS_CREDENTIALS_KEY` | 至少 32 字符的独立稳定秘密，用于加密项目 Application Password |

WordPress 优先在**项目设置**保存站点根地址、用户名和 Application Password，不在 `.env` 中维护每篇文章的账号。该加密密钥只保存在 Article Agent 服务器，WordPress 不需要配置它；丢失后已保存凭据无法解密，不能随意轮换。

可选运维回退：`ARTICLE_AGENT_WORDPRESS_PROJECT_CREDENTIALS` 按项目 ID 引用环境变量名；没有该映射时可使用全局 `ARTICLE_AGENT_WORDPRESS_USERNAME` / `ARTICLE_AGENT_WORDPRESS_APP_PASSWORD`。映射启用但缺少当前项目时会拒绝，不会借用其他项目账号。`ARTICLE_AGENT_WORDPRESS_URL` 是未配置项目站点地址时的回退。项目已保存凭据优先，细节见[WordPress 说明](wordpress.md)。

## 7. 私有对象存储与网络地址

| 配置 | 作用 |
| --- | --- |
| `ARTICLE_AGENT_OBJECT_STORE_BUCKET` | 已建立的私有 bucket |
| `ARTICLE_AGENT_OBJECT_STORE_REGION` | 服务所在 region，默认 `us-east-1` |
| `ARTICLE_AGENT_OBJECT_STORE_ENDPOINT` | 生成短期下载链接的地址，必须能被用户浏览器访问；AWS 默认端点可留空 |
| `ARTICLE_AGENT_OBJECT_STORE_INTERNAL_ENDPOINT` | 可选内网读写地址；留空则使用上面的地址 |
| `ARTICLE_AGENT_OBJECT_STORE_ACCESS_KEY` / `ARTICLE_AGENT_OBJECT_STORE_SECRET_KEY` | 成对填写；AWS IAM 场景可使用默认凭据链 |
| `ARTICLE_AGENT_OBJECT_STORE_FORCE_PATH_STYLE` | S3 兼容服务通常为 `true`，按提供方要求设置 |
| `ARTICLE_AGENT_OBJECT_STORE_SSE` | `AES256`、`aws:kms` 或 `none`；模板为 `AES256`，必须由存储服务实际支持 |
| `ARTICLE_AGENT_OBJECT_STORE_KMS_KEY_ID` | 选择 `aws:kms` 时必填 |

`none` 表示不请求服务端加密，可能用于不支持 SSE 的隔离本地 MinIO；它不满足严格部署 preflight 的加密要求。不能为了让检查变绿而声称实际未启用的加密已经配置。

地址必须从**发起请求的位置**判断：

- 你的电脑访问应用：生产域名；本地调试则是 `127.0.0.1:3108`。
- 后端容器访问模型/数据库：`127.0.0.1` 指该容器自身。共享 Docker 网络用服务名；宿主机服务需配置可达的宿主机地址。
- 浏览器下载对象：不能使用 Docker 内部服务名。可给后端配置内网 ENDPOINT，同时给签名链接配置可达的 HTTPS ENDPOINT；两者必须对应同一存储服务和 bucket。
- 同网络的模型网关地址例如 `http://cliproxyapi:8317/v1` 仅在该服务确实存在且已加入共享网络时有效；创建空网络不会提供 API 服务。

## 8. 功能开关、并发和前端 API

| 配置 | 作用 |
| --- | --- |
| `KNOWLEDGE_AGENT_ENABLED` | 知识库 runtime、向量检索和研究 |
| `WORKFLOW_ASSISTANT_ENABLED` | 工作流助手主开关 |
| `WORKFLOW_ASSISTANT_ATTACHMENTS_ENABLED` | 助手临时附件能力 |
| `WORKFLOW_ASSISTANT_PROJECT_CHANGES_ENABLED` | 助手项目配置变更能力 |
| `WORKFLOW_ASSISTANT_GAP_FILL_ENABLED` | 助手资料缺口补充能力 |
| `LANGGRAPH_STRICT_MSGPACK` | 保持 `true`，沿用受限序列化设置 |

模板开关保守地为 false；完整知识/批量流程要在依赖就绪后开启主开关及所需子开关。子开关不能替代主开关。`.env` 的 false 会覆盖 Docker YAML 的 true，不要只看 YAML 判断线上功能是否开启。

并发基线：`ARTICLE_AGENT_GLOBAL_JOB_CONCURRENCY=8`（范围 1–128）、`ARTICLE_AGENT_PROJECT_JOB_CONCURRENCY=5`（1–32）、`WORKFLOW_ASSISTANT_MAX_CONCURRENCY=5`（1–32）。全局额度是**后端进程级**，不等于所有副本总额度，也不等于模型的 RPM/TPM 限制；同篇文章仍保持活动 Job 互斥。

连接池变量为 `ARTICLE_AGENT_DB_POOL_SIZE=20`、`ARTICLE_AGENT_DB_MAX_OVERFLOW=20`、`ARTICLE_AGENT_DB_POOL_TIMEOUT_SECONDS=60`、`ARTICLE_AGENT_DB_POOL_RECYCLE_SECONDS=300`。调整前考虑所有 engine/副本总连接数，不能靠无限增大并发解决 429 或内存不足。

前端默认同源访问 `/api`，由 Next.js 转发到后端。应用 API 使用已验证会话及项目权限，不接受大模型 Key 作为应用登录凭据。

- `NEXT_PUBLIC_API_BASE_URL`：通常保持空，使用同源 Cookie；这是浏览器可见的构建配置，禁止存秘密。
- `ARTICLE_AGENT_API_PROXY_TARGET`：独立 Next.js 开发进程默认 `http://127.0.0.1:8000`，生产构建默认 `http://backend:8000`。
- 根 `.env` 不会自动被 `frontend/` 中启动的 Next.js 当成自己的 dotenv。独立启动时从进程传入；生产 rewrite 与公开变量需在构建时生效，不能只改已运行容器的环境变量就认为路由已重建。
- `docker-compose.local.yml` 依赖已有宿主机数据库/存储和 `ARTICLE_DB_*`，不是完整的一键环境；只想调试界面时使用 README 的隔离脚本。

## 9. 配置后怎么验收

配置读取和真实连通性分开检查；不要在真实项目上无上限生成测试文章。

1. `docker compose config --quiet` 检查 Compose 结构；检查 YAML 路径、功能开关及必填键是否缺失，不打印秘密。
2. 完成显式迁移，确认后端/前端健康；用已有测试账号登录，确认可见项目与权限正确。
3. 在独立测试项目做一次小规模标题/正文生成，确认 Responses 的模型、流式事件和输出兼容。HTTP 200 或 `/models` 列表不等于生成调用成功。
4. 上传一份非敏感短资料，确认解析、发布与查询均使用正确 Embedding 模型；检查返回向量维度和实际检索结果。
5. 如使用 MinerU，再单独测一份允许外传的 PDF；验证任务完成和解析内容，不仅检查 Token 非空。
6. 实际下载 Word/图片/交付包，确认签名链接在用户电脑可访问；WordPress 在项目设置测试连接后，用测试文章创建草稿并登录预览。

上述供应商操作可能产生实际费用；本轮文档整理没有执行这些调用。

| 现象 | 优先检查 |
| --- | --- |
| 大模型 404 | BASE_URL 是否多带接口路径，网关是否支持 `/responses` |
| 401/403 | 对应服务的 Key、账号权限和站点安全策略；不要混用 LLM、Embedding、MinerU Token |
| 429 或长时间排队 | 供应商额度、并发/RPM/TPM 和计划状态；避免叠加自动重试 |
| Embedding 报维度错误 | 模型是否实际输出 1536 维，已有索引是否用了不同模型 |
| 配了 Embedding 但研究不可用 | `KNOWLEDGE_AGENT_ENABLED`、依赖是否完整以及实际进程加载的配置 |
| PDF 解析失败 | Token、配额、模型版本、轮询超时和结果下载；DOCX 不走 MinerU |
| 容器 healthy 但不能登录 | 已有组织/用户状态、密码哈希、引导账号映射、Cookie HTTPS 配置 |
| 修改 `.env` 后无变化 | 是否仍被进程变量覆盖，是否重新创建了容器，前端配置是否需要重新构建 |
| 下载链接只有服务器能打开 | 公网下载 ENDPOINT 与内网 ENDPOINT 是否混用 |

历史 `m7_deployment_preflight` 还包含迁移期的恢复证据和引导环境密码检查；数据库已有密码账号可能正常登录但不满足其旧配置检查。不要删安全保障来迁就旧检查，也不要用它的单项结果代替当前端到端验收。现有生产 Actions 的实际步骤以工作流和部署脚本为准。
