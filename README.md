# Article Agent

面向团队的英文文章生产系统：管理项目资料、选题、产品、大纲、研究、正文、复检、图片和交付，并将成品上传为 WordPress 草稿。

当前维护 **Server 版本**。Next.js 提供工作台，FastAPI 执行业务，PostgreSQL/pgvector 保存业务数据与知识索引，私有 S3 兼容对象存储保存资料和交付文件。登录使用组织账号密码及服务端会话，项目权限、后台任务重授权和版本冲突保护贯穿全流程。

## 从哪里开始

| 目的 | 入口 |
| --- | --- |
| 配置大模型、Embedding、MinerU 等服务 | [配置指南](docs/configuration.md) · [.env.example](.env.example) |
| 在本机预览界面、调试操作 | 下方“本地隔离调试” |
| 维护现有生产服务 | 下方“服务器部署与维护” |
| 上传 WordPress 草稿 | [WordPress 使用说明](docs/wordpress.md) |
| 查架构、历史决策或验收记录 | [文档索引](docs/README.md) |
| 修改代码 | [AGENTS.md](AGENTS.md) · [前端补充](frontend/AGENTS.md) |

## 文章生产流程

项目配置与知识资料 → 选题/标题 → 产品确认 → 大纲 → 资料研究 → 正文 → 复检/润色 → 图片 → Word、TDK、交付包或 WordPress 草稿。

- 项目资料、提示词与站点设置可以复用；文章保留自己的要求和结果版本。
- 工作流助手支持计划和批量执行；执行状态与已完成结果保存在服务端。
- 自动 AI 检测和知识复检是辅助结果，不等于人工确认。
- 产品参数等事实使用项目内已发布、当前快照且允许作证的资料；官方博客只能作参考材料。
- WordPress 上传入口位于**交付记录页**。上传只创建草稿，用户在 WordPress 后台检查后发布。

字数、图片数量、AI 阈值和批量策略不在 README 中重复维护；修改这些行为时检查相关配置、调用链和专项测试。

## 必需配置一览

先将 `.env.example` 复制为根目录 `.env`，再填入真实值。模板中的空密钥表示尚未配置，不是可以直接投产的默认账号。

| 能力 | 需要准备 | 缺失时的影响 |
| --- | --- | --- |
| 基础运行 | PostgreSQL + pgvector、私有对象存储、会话密钥、已初始化的组织用户 | 无法完整启动或登录、保存和交付 |
| 文章生成 | `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL` | 不能调用模型生成内容 |
| 知识检索与入库 | 独立的 `EMBEDDING_*` 配置，开启 `KNOWLEDGE_AGENT_ENABLED` | 知识研究与向量检索不可用 |
| PDF 版面/OCR 解析 | `ARTICLE_AGENT_MINERU_API_KEY` | 改用本地 PDF 文本解析；扫描件可能无有效文本 |
| 联网搜索 | `TAVILY_API_KEY` | 需要外部搜索的研究步骤不可用 |
| 自动 AI 率检测 | `ARTICLE_AGENT_ZEROGPT_API_KEY` | 自动检测不可用，不应将缺失结果视为 0% |
| WordPress 上传 | 服务端凭据加密密钥，以及项目设置里的站点、账号、Application Password | 无法保存项目凭据或上传草稿 |

**LLM 接口必须支持流式 Responses API；Embedding 必须提供独立的 `/embeddings` 接口和 1536 维向量。** 同一个供应商能生成文章，不代表它也提供 Embedding。完整变量、地址格式、功能开关及故障判断见[配置指南](docs/configuration.md)。

## 本地隔离调试

适用于 Windows PowerShell；先安装 Python 3.12、Node.js 22 和可运行 Linux 容器的 Docker Compose。命令在仓库根目录执行：

```powershell
py -3.12 -m venv backend/.venv
backend/.venv/Scripts/python.exe -m pip install -r backend/requirements-dev.txt
npm.cmd ci --prefix frontend
backend/.venv/Scripts/python.exe scripts/local_debug.py
```

打开 `http://127.0.0.1:3108`。脚本启动独立 PostgreSQL/MinIO、执行迁移、建立测试账号并自动进入工作台；后端为 `127.0.0.1:8108`，日志位于被忽略的 `outputs/local-debug/`。

这是**免登录的隔离界面调试环境**：主模型和搜索密钥被清空，不读取生产 `.env`，不用于验证真实文章生成。不要将此入口暴露到公网。Ctrl+C 停止前后端；测试数据卷保留。

需要停止测试基础设施时，在同一仓库执行：

```powershell
docker compose -f docker-compose.debug.yml stop
```

需要调用真实供应商时使用正常 Server 环境与独立测试数据，按照配置指南配置，不修改免登录入口来连接生产数据库。

## 服务器部署与维护

当前服务器可通过本机 SSH 别名连接：

```powershell
ssh myserver
```

以下命令在 **myserver** 上执行：

```bash
cd /home/ubuntu/Work-Station
git status --short
git rev-parse --short HEAD
sudo docker compose ps
sudo docker compose config --quiet
```

生产应用地址为 `https://ar.xpyaigc.com`；基础 Compose 的前端映射端口是 `3012`，后端只在容器网络内提供 `8000`。WordPress 测试站为独立服务，见[测试站说明](deploy/wordpress-test/README.md)。

### 首次准备

`docker-compose.yml` 只负责前后端，**不会创建生产 PostgreSQL、对象存储或首个组织账号**。部署前必须准备：

1. 可达的 PostgreSQL/pgvector 和私有存储桶；容器内连接地址与浏览器下载地址分别正确。
2. 根目录 `.env` 中的会话、模型、知识库及存储配置，按需补充 MinerU、Tavily、ZeroGPT、WordPress。
3. 持久目录 `workspace/`，以及 `config.docker.yaml` 指向的 `workspace/humanize.txt`。首次可从 `backend/prompts/humanize_ci.txt` 复制；保留已有自定义版本。
4. 已初始化的组织与管理员账号。当前密码登录只能验证已有用户；旧 OIDC bootstrap CLI 不是当前密码登录的安装工具。全新数据库的首个账号初始化需要单独完成，不能仅填环境密码就认为账号已经创建。
5. 域名、HTTPS 反向代理和数据库/对象存储备份。会话密钥与 WordPress 凭据加密密钥需要持久保存。

不要将现有生产 `.env`、数据库或对象存储复制给免登录调试环境。

### 发布流程

当前 [GitHub Actions](.github/workflows/deploy-production.yml) 在 PR 上执行后端测试与前端 lint/build；只有 `main` 的 push 才会继续运行生产部署。GitHub `production` 环境需要 `DEPLOY_SSH_PRIVATE_KEY` 和经过核验的 `DEPLOY_KNOWN_HOSTS`。

[部署脚本](deploy/deploy-production.sh) 按指定提交构建、执行 Alembic 迁移和 LangGraph checkpoint 初始化、替换应用容器并等待健康检查。它拒绝覆盖服务器已跟踪的本地改动；代码回滚不会自动回退数据库结构或数据。

仅修改现有服务器 `.env` 时，需重新创建相应应用容器才能注入新值；`docker compose restart` 不会更新容器环境。配置变更后分别验证启动、登录、生成和文件下载，不能只看容器 healthy。

首次部署的显式数据库步骤为下列命令；仅在目标数据库、备份和镜像均已准备好时执行：

```bash
sudo docker compose build
sudo docker compose run --rm --no-deps backend python -m alembic -c /app/backend/alembic.ini upgrade head
sudo docker compose run --rm --no-deps backend python -m knowledge_agent.checkpoint_setup
sudo docker compose up -d --wait
```

不要在排查配置时打印完整 `docker compose config` 或 `.env`；只使用 `config --quiet`、服务状态和脱敏日志。

## 代码目录与验证

| 目录/文件 | 职责 |
| --- | --- |
| `backend/app.py`、`backend/server_project_http.py` | API 入口、运行时、项目文章工作流 |
| `backend/services/` | 权限、会话、队列、生成、导出与第三方服务 |
| `backend/knowledge_agent/` | 文件解析、知识发布、检索、证据与研究 |
| `backend/workflow_assistant/` | 计划、批量编排与状态恢复 |
| `backend/migrations/` | 唯一的数据库结构迁移入口 |
| `frontend/src/` | 项目工作台、知识库、批量任务及交付界面 |
| `scripts/local_debug.py` | 独立本地调试入口 |
| `deploy/` | 生产发布和独立 WordPress 测试站 |

按改动运行相关专项测试。整体回归参考：

```powershell
backend/.venv/Scripts/python.exe -m unittest discover -s backend/tests -q
npm.cmd run lint --prefix frontend
npm.cmd run build --prefix frontend
git diff --check
```

数据库集成测试只能连接隔离测试库；环境缺失导致的 skip 不等于通过。CI 的隔离环境与执行顺序以工作流文件为准。导出/上传验收要实际下载或预览文件，检查图片、链接和内容版本。
