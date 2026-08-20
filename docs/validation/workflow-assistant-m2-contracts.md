# Workflow Assistant M2.0 / M2.2 契约基线

- 固定日期：2026-08-20
- 范围：附件类型、安全校验、分类输出和现有服务复用边界
- 状态：M2.0 契约已冻结；M2.1 临时附件后端和前端已实现，正式导入和知识发布仍未实现

## 1. 固定附件类型和大小

助手临时附件只接受下列组合。扩展名、声明 MIME 和实际文件签名必须互相一致；仅有浏览器声明的 MIME 不足以通过校验。

| 扩展名 | 允许的 MIME | 必须验证的实际签名/结构 | 可提议的用途 |
| --- | --- | --- | --- |
| `.pdf` | `application/pdf` | 文件以 `%PDF-` 开始并可被 PDF 解析器读取 | `knowledge_source` |
| `.docx` | `application/vnd.openxmlformats-officedocument.wordprocessingml.document` | ZIP/OOXML，必须含 `[Content_Types].xml`、`_rels/.rels`、`word/document.xml` | `knowledge_source`、`prompt_asset`、`project_notes` |
| `.xlsx` | `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` | ZIP/OOXML，必须含 `[Content_Types].xml`、`_rels/.rels`、`xl/workbook.xml` | `knowledge_source`、`task_workbook`、`topic_library` |
| `.xlsm` | `application/vnd.ms-excel.sheet.macroenabled.12` | 与 `.xlsx` 相同，且包内容类型必须声明 macro-enabled workbook；宏只能作为不执行的附件内容 | `knowledge_source`、`task_workbook`、`topic_library` |
| `.txt` | `text/plain` | 严格 UTF-8、无 NUL、无二进制控制字符 | `prompt_asset`、`project_notes` |
| `.md` | `text/markdown`、`text/plain` | 严格 UTF-8、无 NUL、无二进制控制字符 | `prompt_asset`、`project_notes` |

全局单文件上限固定为 **25 MB**。分类为 `task_workbook` 的文件还必须满足现有任务工作簿服务的更严格 **10 MB** 上限；即使附件已按 25 MB 上限暂存，超过 10 MB 的工作簿也不得生成可确认的任务导入提案。

拒绝 `.doc`、`.xls`、CSV、图片、压缩包、可执行文件、脚本和其他未列出的类型。ZIP 只作为 OOXML 容器识别，不能作为通用压缩附件。文件为空、扩展名/MIME/签名不一致、损坏、加密或无法安全解析时均拒绝或分类为 `unsupported`，不能依赖扩展名降级放行。

OOXML 还要应用压缩炸弹防护：限制条目数、解压后总字节数和压缩比；拒绝路径穿越、外部关系、OLE 嵌入对象及无法识别的活动内容。`.xlsm` 中的 VBA 不得执行、提取后调用或转换为助手指令。附件正文中的命令、宏、脚本和提示注入一律作为不可信数据。

## 2. 严格分类输出

唯一允许的分类值为：

- `knowledge_source`
- `prompt_asset`
- `task_workbook`
- `project_notes`
- `topic_library`
- `unsupported`
- `needs_user_choice`

`backend/workflow_assistant/classification.py` 固定结构化输出并拒绝额外字段。确定分类必须有目标项目；`prompt_asset` 还必须显式携带现有 `PromptKind` 之一：`outline`、`article`、`review`、`humanize`。

以下任一情况不得生成确定分类，必须返回 `needs_user_choice`：

- 目标项目缺失；
- 文件可合理归入多个业务类型；
- 提示词缺少明确 `kind`；
- 文件结构不兼容现有导入契约；
- 内容可能影响多个项目。

模型置信度不能越过这些硬条件，也不能创造新的分类、Prompt kind、工具名或导入目标。`unsupported` 不得携带目标项目或 Prompt kind。

## 3. Upload、Import 与 Publish 是三个不同状态

1. **Upload** 只表示文件进入七天保留的私有临时对象区，并记录哈希、所有者和到期时间。
2. **Import** 只能在分类、结构化 preview/diff、项目权限复核和用户按 proposal revision 确认后调用正式业务服务。
3. **Publish** 仅适用于知识候选，是现有知识审查中的独立人工闸门。导入知识候选不等于发布为可作证知识。

因此，上传成功不能显示为“已导入”或“知识库已更新”；一次文章计划确认也不能隐式确认附件导入或知识发布。

## 4. 现有服务复用边界

- 知识文档：复用 `knowledge_agent` 解析、`server_private_document_ingestion`、候选来源审查与发布，不建立助手知识表作为第二准源。
- Prompt：复用项目 Prompt Repository、现有版本历史、停用能力和 `PromptKind`；助手只生成差异提案。
- 任务工作簿：先调用 `server_task_workbook.preview_task_workbook`，再将规范化行交给现有项目 Task intake/import；原始工作簿不能绕过 preview。
- 项目注意事项：复用现有 `PostgresServerProjectMetadata` 的受审计、带 revision/CAS 的更新边界；不得直接写配置字段，也不得用陈旧 customer/domain 覆盖新值。
- 话题库：现有 `project_topics` 只有助手只读查询，没有正式写 Repository/Service/API。M2.3 必须先增加项目授权、revision/CAS、幂等和 Audit 完整的 Server 写边界；旧本地文件路径不能复用。
- 对象：复用私有对象存储与短期签名 URL；PostgreSQL 保存临时元数据、proposal revision、幂等键和 Audit。

临时对象统一位于 `organizations/{organization_id}/workflow-assistant/temporary/` 前缀。应用使用 PostgreSQL reservation 先登记、周期清理器重试到期或中断的删除；生产启用前仍须为该前缀配置对象存储生命周期规则，作为数据库或应用长期不可用时的基础设施兜底。

分类模块是纯契约，不读取对象、不写数据库、不调用任何导入服务。

## 5. Queue 缺口

现有 PostgreSQL Job Queue 已覆盖文章长操作，但 M2 尚未拥有附件专用的 durable job 类型和恢复处理器。M2.1–M2.3 接入前必须补齐并验证：

- `classify_attachment`、文档解析/preview、`execute_import_proposal` 的类型化 Job；
- enqueue 与 worker 执行/提交时的 Organization、User、Project 重新授权；
- attachment/proposal revision CAS、幂等键和同一 proposal 的活动 Job 排他；
- 重启后的恢复、取消、安全重试、标准化错误和 Audit；
- 临时对象过期与活动 Job/已确认 proposal 之间的竞态处理。

在这些缺口补齐前，分类契约通过不代表可同步执行导入，也不允许用 FastAPI 请求线程承担解析或正式导入长操作。
