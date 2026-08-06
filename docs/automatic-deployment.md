# 现有 GitHub push 生产部署流程（Legacy）

本文只说明当前 `.github/workflows/deploy-production.yml` 与
`deploy/deploy-production.sh` 已在使用的生产部署流程。它是现有 Article Agent 的
Legacy 部署说明，不是 Knowledge Agent M7 的 PostgreSQL/Object Store 备份恢复证明，
也不能替代 M7 的发布门禁或签名 Evidence。

M7 正式切换、恢复演练、RPO/RTO、Preflight 和发布证据以
`docs/runbooks/knowledge-agent-m7-server-cutover.md` 及按该 Runbook 保存的受控签名
Evidence 为准。

## 工作流触发与分支

- `pull_request` 指向 `main`：只运行 `Test and build`，不部署。
- 推送到 `main`：先运行 `Test and build`；成功后才运行生产 deploy job，并部署该次
  push 的精确 commit。
- `workflow_dispatch`：按当前 deploy job 的 `if` 条件只运行质量检查，不会部署。
- 同一 Git ref 的工作流不相互取消；`main` 的推送部署按队列串行执行。

质量检查使用临时 PostgreSQL/pgvector 服务，连续执行两次 Alembic `upgrade head`，
然后运行完整后端测试、前端 lint 和生产构建。这里验证的是 CI 数据库；当前 Legacy
部署脚本不会因此自动迁移或备份正式 PostgreSQL。

## 服务器前置条件

服务器需要满足：

- 项目 checkout 位于 `/home/ubuntu/Work-Station`，能够获取 `origin/main`，并能从当前
  commit fast-forward 到工作流传入的目标 commit；脚本不会切换分支或执行
  `git reset --hard`。
- 已安装 Git、Python 3、Docker、Docker Compose v2 和 `sudo`；部署账号必须能以
  非交互方式运行脚本所需的 Docker 命令。
- `/home/ubuntu/Work-Station/.env` 已在服务器受控配置，不能提交到 Git。
- `/home/ubuntu/Work-Station/workspace` 保存现有持久化数据，不能放进 Git 或部署临时
  worktree。
- 服务器 checkout 不得包含 tracked 工作区或暂存区修改；检测到时部署会停止且不会
  覆盖。被忽略的运行数据可继续保留。

## GitHub Environment 与 SSH

生产 deploy job 使用 GitHub `production` Environment。部署目标和远程账号由当前
Workflow 的受控配置决定；本文不复制具体主机或账号值。

Environment 必须提供部署专用 SSH 私钥和可信 Known Hosts。密钥材料只保存在受保护的
GitHub Secret 和目标服务器的授权配置中，不得发送到聊天、提交到仓库、写入项目目录
或保存到发布 Evidence。

如需上线前人工确认，可为 `production` Environment 配置 required reviewer。

## 首次部署前检查（不启动容器）

在服务器执行：

```bash
cd /home/ubuntu/Work-Station
git status --short
git branch --show-current
git fetch origin main
docker compose version
```

`git status --short` 不应显示 tracked 修改。`git fetch` 会更新远端跟踪引用和
`FETCH_HEAD`，但不会修改当前 worktree。首次启动或任何会改变容器状态的操作，应按
当前变更窗口和回滚安排另行执行，不能把这组部署前检查当作 M7 Preflight。

## 当前部署行为

1. Workflow 通过 SSH 把本次 `main` push 的精确 commit 传给服务器脚本。
2. 脚本检查依赖、环境文件、持久化 workspace 和 tracked checkout 状态，然后获取
   `origin/main` 并验证目标 commit。
3. 如果服务器已经位于目标 commit，脚本会重新构建并启动当前 checkout，等待 Compose
   健康检查通过。
4. 如果目标 commit 已被服务器当前 commit 超越，脚本安全退出，不回退版本。
5. 其他情况只允许 fast-forward；目标与当前 checkout 分叉时部署失败。
6. 对新的 fast-forward 目标，脚本在服务器缓存目录创建 detached release worktree，使用
   Python SQLite Backup API 备份 `workspace/data` 下现有 SQLite 数据库，然后从 release
   worktree 构建镜像并替换容器。
7. Compose 等待后端和前端容器健康检查通过后，服务器主 checkout 才以 `git merge
   --ff-only` 前进到目标 commit；最后清理 release worktree 并输出容器状态。

当前后端容器健康检查只调用浅层 `/api/health`。它证明进程可响应，不等于 M7 数据库、
OIDC、Object Store、代码切换能力或恢复演练门禁已通过。

## 当前回滚边界

- release 镜像构建失败发生在容器替换前，现有容器继续运行。
- 容器替换开始后若部署失败，错误处理会回到服务器原 checkout，尝试重新构建并启动旧代码
  容器、等待 Compose 健康检查；当前脚本不会把这次恢复尝试的失败升级为新的退出状态，
  因此运维人员仍须人工确认旧容器健康。服务器 Git checkout 不会被强制重置。
- 此回滚尝试只面向旧代码和容器，不会恢复 PostgreSQL、Object Store 或 SQLite 数据。
- 更新前的自动备份只覆盖现有 `workspace/data` SQLite 数据库，并保存到
  `workspace/backups/deploy-<UTC时间>-<原commit>`；它不是 PostgreSQL/Object Store
  恢复点，也没有自动执行恢复。
- 数据损坏、跨存储恢复或 M7 切换失败必须遵循 Server Cutover Runbook，使用已经演练并
  受控保存的数据库与对象恢复证据；不得把 Legacy SQLite 备份或 Compose 健康检查记录
  作为 M7 go-live Evidence。

## 准源文件

- 当前 GitHub Workflow：`.github/workflows/deploy-production.yml`
- 当前服务器部署脚本：`deploy/deploy-production.sh`
- 当前 SQLite 备份脚本：`deploy/backup_sqlite.py`
- M7 正式切换与证据准源：`docs/runbooks/knowledge-agent-m7-server-cutover.md`
