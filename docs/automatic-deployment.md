# GitHub push 自动部署

推送 `main` 后，GitHub Actions 会先运行完整后端测试、前端 lint 和生产构建。全部通过后，工作流通过 SSH 把对应 commit 部署到 `/root/article`，构建 Docker 镜像并等待两个容器通过健康检查。

## 服务器前置条件

服务器需要满足：

- 项目位于 `/root/article`，当前分支是 `main`，并能执行 `git fetch origin main`。
- 已安装 Git、Python 3、Docker 和 Docker Compose v2。
- `/root/article/.env` 已配置，不能提交到 Git。
- `/root/article/workspace` 保存正式数据，不能放进 Git 或部署临时目录。
- 外部 Docker 网络 `new-api_new-api-network` 已存在。
- 服务器 checkout 不保留 tracked 手工修改；检测到修改时部署会停止，不会覆盖。

## 建立专用 SSH 密钥

在可信电脑上创建一对只用于部署的密钥：

```bash
ssh-keygen -t ed25519 -C "github-actions-article" -f article_deploy
```

把 `article_deploy.pub` 的内容追加到服务器 `/root/.ssh/authorized_keys`。私钥 `article_deploy` 只放入 GitHub Secret，不要发送到聊天、提交到仓库或写入服务器项目目录。

## 配置 GitHub Environment

在仓库 `Settings → Environments` 新建 `production`，添加：

| Secret | 内容 |
| --- | --- |
| `DEPLOY_HOST` | 服务器 IP 或域名，不带协议和端口 |
| `DEPLOY_SSH_PRIVATE_KEY` | `article_deploy` 私钥全文 |
| `DEPLOY_KNOWN_HOSTS` | 可信环境中执行 `ssh-keyscan -H <服务器IP或域名>` 得到的整行 |

如果希望每次上线前人工确认，可以为 `production` Environment 配置 required reviewer。

## 首次检查

在服务器执行：

```bash
cd /root/article
git status --short
git branch --show-current
git fetch origin main
docker compose version
docker network inspect new-api_new-api-network
docker compose up -d --build --wait --wait-timeout 180
```

`git status --short` 不应显示 tracked 修改。`.env`、`workspace/data` 等被 `.gitignore` 排除的运行数据可以保留。

## 部署行为

- 自动触发：推送到 `main`。
- 手动触发：GitHub 仓库 `Actions → Test and deploy production → Run workflow`。
- 同一时间只运行一个生产部署。
- 服务器只允许 fast-forward，不执行 `git reset --hard`。
- 新镜像构建失败时，现有容器不会被替换。
- 替换容器后健康检查失败时，会从服务器原 checkout 重建并恢复旧版本。
- 每次更新前使用 SQLite Backup API 把数据库快照保存到 `workspace/backups/deploy-<时间>-<commit>`。

工作流文件：`.github/workflows/deploy-production.yml`
服务器部署脚本：`deploy/deploy-production.sh`
