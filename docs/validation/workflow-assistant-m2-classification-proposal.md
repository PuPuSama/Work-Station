# Workflow Assistant M2.2 分类与导入提案验证

验证日期：2026-08-20

## 已实现边界

- 附件分类复用现有 PDF、DOCX、XLSX/XLSM 解析器；TXT/Markdown 只接受严格 UTF-8。
- 模型输入有字符、行数和表格行上限；附件内命令仅作为不可信正文，不允许触发工具、导入或发布。
- 分类只接受闭集 Pydantic 结构。缺少项目、提示词 kind、多义、多项目或结构不兼容时进入 `needs_user_choice`。
- 人工消歧按附件 revision/CAS 保存并审计；只有具体类型、目标项目以及必要的 prompt kind 完整后才可生成 preview。
- Preview Job 先读取当前项目快照并生成 `create/update/skip/conflicts/invalid` 差异，成功后才创建 Proposal；不存在空差异被提前确认的窗口。
- `assistant_attachment_jobs` 独立于文章 Task，提供幂等、活动任务排他、lease、恢复、取消、重试、revision CAS 和标准错误。
- Proposal 的读取、修订、确认和取消均限定 Organization 与 creator；目标项目在 preview、confirm、Job execute 和 Job commit 阶段重新鉴权。
- M2.2 的确认只将 Proposal 置为 `confirmed`，不写正式业务表、不导入知识、不发布知识。正式导入属于 M2.3。

## 本地自动验证

- M2.2 聚焦测试：68 项通过。
- 后端完整回归：977 项通过，293 项因未提供外部/数据库环境而跳过。
- 独立 PostgreSQL 验证：一次性数据库完整 upgrade 到 0031；附件、分类 CAS、Job 和 Proposal 共 19 项 PostgreSQL 集成测试通过。0031 downgrade 到 0030、重新 upgrade head 也已由独立子任务验证。
- `git diff --check` 通过，仅显示 Windows CRLF 提示。

## 尚未完成

- 真实浏览器跨用户隔离以及分类、人工消歧、差异修订、确认的端到端验收。
- M2.3 正式导入适配器、知识候选发布和正式业务实体写入。
- Push、CI、部署及线上 smoke；当前均未执行。
