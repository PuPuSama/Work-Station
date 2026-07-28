# Knowledge Agent M1 本地运行手册

本文适用于 Windows PowerShell，所有命令都以正式 M1 工作树
`D:\Project\article\article-agent-formal` 为当前目录。M1 只新增
PostgreSQL + pgvector 知识库底座；原有文章任务和队列仍使用 SQLite。

## 运行边界

- `knowledge_agent_enabled` 默认关闭。只验证数据库、Repository 或检索时，不需要开启该开关。
- 启动应用或 PostgreSQL 容器都不会自动迁移 Schema。拉取包含新迁移的代码后，必须显式执行 Alembic。
- 不要执行 `docker compose -f .\compose.dev.yaml down -v`。该命令会删除 M1 的具名数据卷；日常停止只使用 `stop`。
- Embedding 密钥只放在当前 PowerShell 进程或被 Git 忽略的本机 `.env`
  中，不写入代码、文档、命令输出或测试夹具。

## 1. 创建 Python 3.12 本地环境

先确认 Docker Desktop 已启动，并确认 Python 3.12 可用：

```powershell
Set-Location 'D:\Project\article\article-agent-formal'
py -3.12 --version
docker compose version
```

若 `py -3.12` 报告没有已安装的 Python，请先安装 Python 3.12；若本机
没有 Windows Python Launcher，则把下面第一条命令中的 `py -3.12` 换成
Python 3.12 解释器的绝对路径。首次安装或需要重建环境时执行：

```powershell
Set-Location 'D:\Project\article\article-agent-formal'
py -3.12 -m venv .\backend\.venv
& .\backend\.venv\Scripts\python.exe -m pip install -r .\backend\requirements-dev.txt
```

后续命令都直接使用 `.\backend\.venv\Scripts\python.exe`，不依赖全局
`python` 或 PowerShell 的激活状态。

## 2. 启动并检查 PostgreSQL + pgvector

```powershell
Set-Location 'D:\Project\article\article-agent-formal'
docker compose -f .\compose.dev.yaml up -d --wait --wait-timeout 60
docker compose -f .\compose.dev.yaml ps
docker compose -f .\compose.dev.yaml exec -T postgres pg_isready -U article_agent -d article_agent
```

`ps` 应显示数据库为 `healthy`，`pg_isready` 应返回 `accepting connections`。
本地数据库只监听 `127.0.0.1:55433`。
本手册后续命令使用 `compose.dev.yaml` 的默认数据库名、用户和端口；若通过
`ARTICLE_DB_NAME`、`ARTICLE_DB_USER` 或 `ARTICLE_DB_PORT` 覆盖默认值，必须同步
调整 `pg_isready`、`psql` 和 `ARTICLE_AGENT_DATABASE_URL`。

在当前 PowerShell 会话设置连接 URL：

```powershell
$env:ARTICLE_AGENT_DATABASE_URL = 'postgresql+psycopg://article_agent:article_agent@127.0.0.1:55433/article_agent'
```

## 3. 显式执行 Alembic 迁移

从仓库根目录运行迁移，不要从应用启动流程中隐式调用：

```powershell
Set-Location 'D:\Project\article\article-agent-formal'
& .\backend\.venv\Scripts\python.exe -m alembic -c .\backend\alembic.ini upgrade head
& .\backend\.venv\Scripts\python.exe -m alembic -c .\backend\alembic.ini current
```

重复执行 `upgrade head` 应安全完成。迁移后可确认 pgvector 扩展已经启用：

```powershell
docker compose -f .\compose.dev.yaml exec -T postgres psql -U article_agent -d article_agent -c "SELECT extversion FROM pg_extension WHERE extname = 'vector';"
```

## 4. 运行单元测试和数据库集成测试

Embedding Provider 单元测试使用假传输，不访问外部网关，也不需要真实密钥：

```powershell
Set-Location 'D:\Project\article\article-agent-formal'
& .\backend\.venv\Scripts\python.exe -m unittest backend.tests.test_embedding_provider -v
```

PostgreSQL 集成测试以 `ARTICLE_AGENT_DATABASE_URL` 为门禁；未设置时会跳过，
设置后将针对上一步迁移完成的本地数据库运行：

```powershell
Set-Location 'D:\Project\article\article-agent-formal'
$env:ARTICLE_AGENT_DATABASE_URL = 'postgresql+psycopg://article_agent:article_agent@127.0.0.1:55433/article_agent'
& .\backend\.venv\Scripts\python.exe -m unittest backend.tests.test_knowledge_agent_m1_postgres -v
```

完整后端回归与 CI 使用相同的发现方式：

```powershell
Set-Location 'D:\Project\article\article-agent-formal'
& .\backend\.venv\Scripts\python.exe -m unittest discover -s .\backend\tests -q
```

## 5. 可选：真实 Embedding 网关冒烟

这一步会产生真实外部请求和可能的费用，绝不在默认测试或 CI 中运行。先在当前
PowerShell 会话提供独立的 Embedding 配置；它们不会回退到 `LLM_*`：

```powershell
Set-Location 'D:\Project\article\article-agent-formal'
$env:EMBEDDING_BASE_URL = 'https://your-compatible-gateway.example/v1'
$secureEmbeddingKey = Read-Host 'Embedding API key' -AsSecureString
$embeddingCredential = New-Object System.Management.Automation.PSCredential('embedding', $secureEmbeddingKey)
$env:EMBEDDING_API_KEY = $embeddingCredential.GetNetworkCredential().Password
Remove-Variable secureEmbeddingKey, embeddingCredential
$env:EMBEDDING_MODEL = 'text-embedding-3-small'
$env:EMBEDDING_DIMENSIONS = '1536'
```

保持 PostgreSQL 正常、Alembic 已到 `head`，然后从 `backend` 目录显式运行冒烟模块：

```powershell
Set-Location 'D:\Project\article\article-agent-formal\backend'
& .\.venv\Scripts\python.exe -m knowledge_agent.m1_smoke
Set-Location 'D:\Project\article\article-agent-formal'
```

冒烟输出只能包含 `model`、`dimensions`、`vector_count`、命中的 chunk ID 和分数。
不得输出请求正文、原始向量、响应正文、Base URL、请求头、API Key，也不要用
`Get-ChildItem Env:` 或类似命令导出整个环境。完成后立即从当前会话移除密钥：

```powershell
Remove-Item Env:EMBEDDING_API_KEY -ErrorAction SilentlyContinue
```

## 6. 日常停止与再次启动

保留数据卷并停止数据库：

```powershell
Set-Location 'D:\Project\article\article-agent-formal'
docker compose -f .\compose.dev.yaml stop
```

再次运行时重复第 2 节的 `up -d --wait`。只有出现新的 Alembic revision 时才需要
再次执行 `upgrade head`，但任何服务启动都不会替你执行迁移。不要使用
`down -v` 清理环境；如确需销毁测试数据卷，应先备份并作为单独的破坏性操作处理。
