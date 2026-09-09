# Article Agent 前端

Next.js 工作台，负责项目、知识库、文章流程、批量任务与交付页面。启动整个隔离调试环境请使用根 [README](../README.md) 中的 `scripts/local_debug.py`，入口为 `http://127.0.0.1:3108`。

## 单独开发

先准备正常运行的后端，再在本目录执行（Windows PowerShell）：

```powershell
npm.cmd ci
$env:ARTICLE_AGENT_API_PROXY_TARGET = 'http://127.0.0.1:8000'
npm.cmd run dev
```

默认开发入口为 `http://127.0.0.1:3000`。单独启动 Next.js 不会初始化数据库、创建用户或取消后端登录要求；不要同时占用隔离脚本使用的端口。

前端默认通过同源 `/api` 访问后端。公开 API 地址与 rewrite 的构建时机见[配置指南](../docs/configuration.md)；不要将模型、Embedding、MinerU 或存储密钥写入前端环境变量。根目录 `.env` 不是 Next.js 自动加载的 `frontend/.env`。

## 入口与检查

- 路由：`src/app/`；项目组件：`src/components/server-*`。
- API 客户端：`src/lib/api.ts`；共享类型：`src/types.ts`。
- 生产构建和发布使用仓库 Dockerfile/GitHub Actions，不使用脚手架默认的 Vercel 发布流程。

```powershell
npm.cmd run lint
npm.cmd run build
```

修改约定见 [AGENTS.md](AGENTS.md)。纯文档调整无需运行前端构建；界面变更要实际检查对应操作与错误状态。
