# Workflow Assistant M2.3 导入执行验证

日期：2026-08-20

## 实现边界

- 确认后的 Import Proposal 只释放一个持久化 `execute_import_proposal` Job。
- Worker 在执行前重新校验 actor、项目权限、Proposal revision、附件 revision 和附件来源。
- 类型化适配器复用现有 Server 服务：知识文件进入候选快照并保持 `waiting_publication`，Prompt 使用确定性 ID，任务表和话题库使用既有幂等导入，项目注意事项使用 metadata revision/CAS。
- Proposal 的 `confirmed → running → completed/waiting_publication` 状态与附件的 `proposal_ready → importing → imported` 状态在 PostgreSQL 事务中推进，并写入 Audit。
- Worker 在业务写入后崩溃时，可通过执行幂等键重放，不重复创建正式记录；失败会回滚附件到 `proposal_ready` 并保留可审计错误码。

## 自动验证

使用主分支配置的 Python 环境运行：

```powershell
backend\.venv\Scripts\python.exe -m unittest discover -s backend\tests -q
```

本工作树没有独立 `.venv` 时，使用同仓库正式工作树的等价解释器运行上述测试。

已验证：

- 987 项 backend 单元/契约测试通过，296 项因缺少外部依赖或真实服务跳过。
- 0032 Alembic 迁移在干净 PostgreSQL 上成功升级。
- Proposal claim/complete/fail、execute Job 崩溃重放和 Prompt 导入幂等 PostgreSQL 测试通过。
- M2.3 聚焦适配器、HTTP 确认释放 Job、Proposal/Job 回归测试通过。

## 尚未包含

- 真实浏览器跨用户验收仍属于 M2.2/M2.6 验收项。
- 知识候选快照的人工发布仍是独立闸门；M2.3 不自动发布知识。
- M2.4 精准证据补全、M2.5 前端交互和 M2.6 上线验收未在本阶段实现。
