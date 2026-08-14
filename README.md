# Article Agent Server

Article Agent 是面向团队的文章生产与知识库服务。当前代码库只提供 Server 运行形态：

- PostgreSQL 保存项目、文章任务、提示词、审核记录与后台作业。
- 私有对象存储保存上传文件、截图、图片和交付产物。
- OIDC/服务器会话负责登录，项目角色负责授权。
- 前端只调用 `/api/projects/{project}/...` 形式的项目级接口。

项目不再提供 Local 模式、SQLite Task/Job Queue、密码登录、旧 `/api/tasks*` 或 `/api/batches*` 接口，也不执行 SQLite 自动迁移或部署备份。

## 目录

- `backend/app.py`：FastAPI 入口和 Server lifespan。
- `backend/server_project_http.py`：项目级文章工作流 API。
- `backend/services/postgres_task_repository.py`：PostgreSQL 任务存储。
- `backend/services/postgres_job_queue.py`：PostgreSQL 作业队列。
- `backend/knowledge_agent/`：知识入库、检索、证据与研究流程。
- `frontend/src/components/server-*`：Server 工作台组件。
- `backend/migrations/`：Alembic 数据库迁移。
- `deploy/`：生产部署脚本。

## 启动

准备 PostgreSQL、对象存储、OIDC 和模型服务所需环境变量后运行：

```powershell
docker compose up -d --build --wait
```

默认入口：

- 前端：`http://127.0.0.1:3012`
- 后端健康检查：`GET http://127.0.0.1:8000/api/health`（容器网络内）

数据库结构由 Alembic 管理；应用启动不会创建或迁移表。部署前应显式执行相应 Alembic 升级。

## 关键业务边界

- ZeroGPT 始终是人工环节。
- 英文正文目标为 1000–1200 词；FAQ 必须是最后一个 H2，并包含三组问答。
- 每篇最多三张不同图片，包括首图。
- 产品自动发现只接受官网域名，并要求详情页和图片具有可核验对应关系。
- 官方博客只能作为正文写作参考，不能成为事实证据；证据必须来自已发布、当前快照且允许作证的知识来源。
- 所有写操作都绑定组织、项目、操作者和 revision；后台 Worker 提交前重新授权。
- 正式产物通过私有对象存储和短期签名 URL 交付，不依赖服务器本地任务目录。

## 验证

```powershell
backend\.venv\Scripts\python.exe -m unittest discover -s backend\tests -q

cd frontend
npm.cmd run lint
npm.cmd run build
```

提交前再运行：

```powershell
git diff --check
git status --short
```

密钥只放在未提交的环境配置中，禁止写入源码、日志或提交记录。
