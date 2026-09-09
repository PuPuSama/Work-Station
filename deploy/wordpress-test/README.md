# 远端 WordPress 草稿测试站

适用范围：`myserver` 上独立的 Article Agent 上传测试站，不是 Article Agent 生产部署脚本。

- 目录：`/home/ubuntu/article-agent-wordpress-test`；后台：`https://43.154.92.36/wp-admin/`。
- Compose 项目：`article-agent-wordpress-sandbox`；独立 MariaDB 和 WordPress volumes，不连接生产 PostgreSQL、MinIO。
- WordPress 仅绑定 `127.0.0.1:18088`，由 Nginx 的独立 IP 虚拟主机提供 HTTPS。为支持没有 DNS SNI 的 IP 客户端，该站是 IPv4 443 的默认虚拟主机；已有域名仍使用各自的 SNI 配置和证书。
- `provision.py` 在此目录运行，随机生成独立数据库密码、管理员和编辑账号，重复执行保留密码和 Application Password。`.env` 和 `credentials.json` 权限为 600，位于网站文件卷之外，不提交到 Git。
- `credentials.json` 的 `web_password` 用于编辑账号网页登录；`application_password` 用于 Article Agent 项目设置。正式业务站点不得复用这些测试凭据。
- WordPress 使用 `staging` 环境并保留登录；不启用 Article Agent 免登录，不放宽正式上传的 HTTPS/公网地址校验。站点和响应标记 noindex；上传仍为草稿。
- `article-agent-meta.php` 注册来源身份和 TDK 字段。TDK 保存不代表已适配 Yoast/Rank Math 或已渲染为页面 SEO 标签。

## HTTPS 与运行维护

证书位于 `/etc/letsencrypt-article-agent-wp/`，与其他站点隔离。独立 Certbot 5.8 虚拟环境位于 `certbot-venv/`，未升级系统 Certbot。

Nginx 配置：

- `/www/server/panel/vhost/nginx/article-agent-wp-ip-http.conf`：HTTP 跳转与 `/var/lib/article-agent-wp-acme` 验证目录。
- `/www/server/panel/vhost/nginx/article-agent-wp-ip-https.conf`：HTTPS 反向代理。

IP 证书有效期较短，`article-agent-wp-cert-renew.timer` 每天检查两次，续期成功后检查并平滑重载 Nginx。机制参考 [Let's Encrypt 的 IP 证书说明](https://letsencrypt.org/2026/03/11/shorter-certs-certbot)。

在 myserver 上检查：

```bash
cd /home/ubuntu/article-agent-wordpress-test
sudo docker compose -f compose.yml ps
sudo systemctl status article-agent-wp-cert-renew.timer
curl -I https://43.154.92.36/wp-login.php
```

停止测试站使用 `docker compose -f compose.yml stop`；保留数据卷和凭据，不执行 `down -v`。这些配置只部署测试站，不提交、合并或部署 Article Agent 应用。

## 2026-09-09 验证

- WordPress 7.1 / PHP 8.3，独立测试账号登录成功；HTTPS 使用受信任 IP 证书。
- 使用仓库的 WordPressPublisher 上传合成文章：草稿 ID 6、WebP 图片 ID 5。回读验证草稿状态、表格、链接、来源哈希和 TDK 字段；图片实际可下载。
- 重复上传返回 `already_exists`，同 slug 查询仅一篇草稿；未发布测试文章。
- 后台显示草稿与特色图片；登录态草稿预览显示正文、表格、链接和图片。应用内浏览器的编辑器 iframe 未显示正文，编辑画布兼容性尚待普通浏览器复核。
- 证书续期 dry-run 成功；定时器已启用。Article Agent 原有前后端仍 healthy，原有域名正常跳转到登录入口。
- 本机到服务器的 SSH/TLS 连接出现过间歇性断开，后续重试成功；未据此宣称网络已稳定。固定 WordPress 镜像摘要并重建容器后，服务器回读及客户端重复上传检查再次通过，草稿和图片保留。
- 本地测试凭据和不含秘密的回读报告保存在被忽略的 `outputs/wordpress-remote-test/`。
- 交付记录页的 UI fixture 验证了无正文禁用、失败后自动刷新状态、重试、成功后的后台链接和页面刷新保留状态；不是生产后端端到端测试。本机 Docker Desktop 引擎不可用，未完成真实本地 Server API 到远端站点的整条链路验证。
- WordPress 客户端和凭据专项 13 项通过；前端 lint/build 与 `git diff --check` 通过。交付页改动未提交、合并或部署。

此记录证明独立 WordPress 站点和客户端上传链路；线上 Article Agent 的功能分支仍需单独审查、合并、迁移与部署。
