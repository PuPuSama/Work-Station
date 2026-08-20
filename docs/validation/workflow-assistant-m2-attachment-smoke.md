# Workflow Assistant M2.1 附件暂存验证记录

- 验证日期：2026-08-20
- 分支：`codex/workflow-assistant-m2`
- 基线：`main` / `83986c5`
- 能力开关：默认关闭；仅在主开关和附件子开关同时开启时暴露 HTTP/UI

## 已验证

- 允许类型、MIME、签名/OOXML/PDF 安全解析及 25 MiB 上限。
- PostgreSQL reservation → 私有对象写入 → finalize；相同幂等键并发请求收敛到同一记录。
- Reject/expiry 先 CAS claim，再删除对象，最后同事务写 Audit 并清除临时元数据。
- `rejecting` / `expiring` 中断状态可由 15 分钟周期清理器重试；`importing` / `imported` 不会被到期任务误删。
- Organization、User、Conversation 使用复合外键和 actor-scoped 查询隔离；预选项目重新校验 `project.view`。
- 上传成功只表示临时保存，不触发分类、导入或知识发布。
- 前端包含能力 gate、附件按钮、多文件进度、稳定幂等重试、到期提示、短签下载和拒绝清理。

## 验证证据

- 后端完整回归：`909` 项通过，`280` 项按环境跳过。
- M2 focused：`48` 项整体为 OK，其中 `6` 项真实 PostgreSQL 测试在无数据库环境时跳过；另在一次性 PostgreSQL 数据库执行 Repository 集成 `6/6` 通过。
- Alembic：空库升级到 `20260820_0030` 成功；`0030` downgrade → upgrade → current=head 成功。
- 应用启动：一次性 M2 数据库、附件双开关开启时，health `200`、auth status 正确公开能力、未登录附件路由 `401`，关闭时干净退出。
- 真实私有对象存储：TXT/MD 上传、短期签名下载内容校验、拒绝删除、临时元数据归零、uploaded/rejected Audit 均通过。
- 前端：ESLint 和 Next.js production build（webpack，含 TypeScript）通过。
- `git diff --check` 通过，仅有工作树既有 CRLF 转换提示。

验证使用的一次性 PostgreSQL 数据库已删除；真实对象 smoke 的临时对象已通过拒绝流程删除。

## 尚未声称完成

- 尚未执行两个真实登录用户之间的浏览器跨用户隔离验收。
- M2.2 分类 Job、导入 proposal、正式导入和知识发布尚未实现。
- 未 Push、未运行 CI、未部署。
