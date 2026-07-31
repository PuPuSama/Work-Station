# M7 Server Article Console：结构与接口痕迹

## 1. 目的

本文记录 Server 文章目录与单篇工作台的组件边界、API 作用和重构不变量。它是后期
重构导航，不是后端授权准源，也不表示 M7 已达到生产上线条件。

本切片解决的具体问题是：

- `/projects/{project}/articles` 和详情页此前总是挂载 Local SQLite 组件；
- Server Project Shell 只开放 Delivery，已完成的 Project-scoped Task 命令没有操作面；
- 如果直接复用 Local Workbench，会把 `/api/tasks*`、`/api/dashboard`、Local Prompt
  和本地文件路径重新带入 Server Mode。

## 2. 组件拓扑

```text
articles/page.tsx
  -> ProjectArticleDirectory
       -> GET /api/auth/status
       -> local  -> ProjectArticleList
       -> server -> ServerProjectArticleList

articles/[taskId]/page.tsx
  -> ProjectArticleWorkspace
       -> GET /api/auth/status
       -> local  -> ArticleWorkbench
       -> server -> ServerArticleWorkbench

ProjectShell
  -> server: Article + Delivery + authorized Settings
  -> local:  existing Article + Knowledge flag + Batch + Delivery + Settings
```

`ProjectArticleDirectory` 和 `ProjectArticleWorkspace` 是唯一模式分流点。Server 组件不接收
Local Store、Dashboard 或 Config 作为 Props，也不在 Server 请求失败后回退 Local。

## 3. 主要代码职责

| 文件 | 作用 | 重构时不得丢失的边界 |
|---|---|---|
| `project-article-directory.tsx` | 列表页 Local/Server 组件树分流 | Auth 失败不挂载任一数据组件 |
| `project-article-workspace.tsx` | 详情页 Local/Server 组件树分流 | Server Task 不得进入 `ArticleWorkbench` |
| `server-project-article-list.tsx` | 读取 Project-scoped Task、搜索和状态定位 | 只调用 `/api/projects/{project}/tasks` |
| `server-article-workbench.tsx` | 编排已迁移的人工命令与异步 Job | 每次写入提交当前 Revision；Job 只按公开 ID 轮询 |
| `project-shell.tsx` | Server 导航开放 Article/Delivery | 导航是可用性提示，不是授权准源 |
| `server-project-selector.tsx` | SQL Project Directory 的默认入口 | 只跳转当前返回的 `project_id` |

## 4. Server 工作台数据流

首次加载并行读取：

1. `GET /api/projects/{project}/tasks/{task_id}`：Task 正文和当前 Revision；
2. `GET /api/projects`：当前 Actor 的 Effective Role，仅用于按钮提示；
3. `GET /api/knowledge/{project}`：confirmed 产品目录。该读失败不阻止查看 Task，但产品
   选择区为空并解释需要先完成正式产品确认。

后端仍是权限准源。前端按钮隐藏或禁用不能替代以下事实：

- Project Scope 由 Cookie Actor 和路径共同解析；
- 写操作在事务内重新锁定可撤权权限；
- Task 更新使用 Revision CAS；
- Worker 在 Claim 前和 Handler 前再次授权。

## 5. 主链接口映射

| UI 阶段 | 接口 | 客户端允许提交的内容 |
|---|---|---|
| 标题候选 | `POST .../titles` + `GET .../titles/jobs/{job}` | Revision |
| 选择标题 | `PUT .../selected-title` | Revision、Candidate Index |
| 选择产品 | `PUT .../products` | Revision、1–3 个 confirmed Product ID |
| 大纲生成 | `POST .../outline` + Job GET | Revision |
| 大纲保存/确认 | `PUT .../outline` | Revision、Markdown、Confirmed |
| 初稿生成 | `POST .../article` + Job GET | Revision |
| 初检截图/确认 | `POST .../checks/initial-ai/screenshot`、`PUT .../checks/initial-ai` | PNG、Revision、分数/报告 |
| 自动人化 | `POST .../humanize` + Job GET | Revision |
| 人工人化稿 | `PUT .../humanized-article` | Revision、有界 Markdown |
| 终检截图/确认 | `POST .../checks/final-ai/screenshot`、`PUT .../checks/final-ai` | PNG、Revision、分数/报告 |
| 链接恢复 | `POST .../restore-links` + Job GET | Revision |
| SEO Review | `PUT .../seo-review-settings`、`POST .../seo-reviews` + Job GET | Keyword、Project Default 选择、Revision |
| 图片准备 | `POST .../prepare-images` | Revision、Hero Asset ID、Product ID 到 H2 的锚点 |
| Word/TDK/ZIP | `POST .../export-docx`、`generate-tdk`、`package-delivery` | Revision |
| 产物下载 | Task-scoped `.../download` | 无对象路径；响应为短期 URL |

异步 Job 的浏览器等待不是 Worker 生命周期。页面只轮询公开状态
`queued/retry_wait/running/succeeded/failed/cancelled/conflict`；等待超时只提示刷新，不发送
取消命令。响应不读取 Request、Requester、Prompt、Chunk、正文、URL 或原始错误。

## 6. 产品与图片

产品选择和产品图片是两个独立身份层：

```text
Knowledge Product (confirmed)
  -> Task 只选择 product_id
  -> Server 投影 Published Current Snapshot 的事实与 selected_asset_id
  -> 图片准备时浏览器只提交 product_id -> heading
  -> Server 重新读取 Task Product 和私有 Asset
  -> 校验 Organization/Project、字节数、SHA-256
  -> 内存生成内容寻址 WebP
  -> Task ArticleImage 只保存 Asset 身份与正文锚点
```

Hero 图同样只通过 Project 私有 `asset_id` 指定。浏览器不提交 Bucket、Object Key、本地路径、
图片 URL、产品描述或产品图片事实。以后增加图片选择器时，它只能把现有 Asset ID 可视化，
不能改变该命令契约。

## 7. 当前明确未接入的控制

本切片是现有 Task 的主链操作面，不是完整 Local UI 等价迁移。以下后端能力仍需专用面板：

- Product Rediscovery 的 Category URL、进度与结果审阅；
- Outline Version 恢复；
- 指定章节重写；
- SEO Review Change 的逐条裁决、Preview、Apply、Complete；
- Rewrite From Scratch；
- Project-scoped Job Control 的全局抽屉；
- Hero/产品 Asset 的可视化选择器；
- Server Task 导入/创建。

这些入口不能通过把 Local `ArticleWorkbench` 的 Handler 改个 URL 来补齐；每个面板都必须
只提交 Server 契约允许的字段，并保留 Revision、权限和私有对象边界。

## 8. 重构检查清单

1. Auth Status 失败时是否仍不会猜测 Local/Server？
2. Server 列表和详情是否仍只使用显式 Project 路径？
3. Server 页面是否仍不会请求 `/api/tasks*`、`/api/dashboard` 或 `/api/config`？
4. Role 是否仍只影响界面提示，后端是否仍逐请求授权？
5. 每个 Task 写操作是否仍提交最新 Revision？
6. 截图上传使 Revision 增加后，确认命令是否先重新读取 Task？
7. Job 超时是否仍不等于取消？
8. 产品选择是否仍只提交 confirmed Product ID？
9. 图片准备是否仍不接受产品事实、Bucket、Key、URL 或本地路径？
10. 下载是否仍先取得 Task-scoped 短期 URL？
11. Local 页面、API 和导航是否仍可独立工作？
12. 新增命令面板时是否同步更新路由迁移矩阵与本文件？
