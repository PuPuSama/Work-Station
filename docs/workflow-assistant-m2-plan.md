# Workflow Assistant M2 实施方案

- 状态：M2.0–M2.5 工程实现完成；M2.6 已完成本地运行与主要浏览器链路验收，跨用户/跨项目及发布门仍待执行
- 确认日期：2026-08-17
- 启动授权日期：2026-08-20
- 前置方案：[`workflow-assistant-m1-plan.md`](workflow-assistant-m1-plan.md)
- 开发前提：M1 工程实现和隔离环境 `deferred` 验收已完成，并已合并形成新的稳定主分支基线
- 验收例外：用户明确选择不执行真实 ZeroGPT 截图；该例外仅允许启动 M2 本地开发，不代表 M1 正式交付通过
- 稳定主线基线：`main` / `83986c5`
- 开发分支：`codex/workflow-assistant-m2`
- 开发工作树：`D:\Project\article\article-agent-workflow-assistant-m2`

## 1. 目标

M2 在 M1 的自然语言问答、跨项目计划和文章工作流编排基础上，增加两类能力：

1. 临时助手附件及经过确认的分类导入。
2. 项目提示词、注意事项和话题库的差异化修改提案。

M2 还会完善“缺少证据时精准补资料”的交互，使文章计划可以在证据不足时暂停，经过定向检索、资料上传、人工审查和发布后，从新的知识快照继续执行。

## 2. M1 与 M2 的交付边界

### M1 负责

- 工作区级助手页面和私人会话。
- 跨项目文章计划、一次确认和白名单工具执行。
- 复用现有 Evidence Pack、标题、产品、大纲、正文、复检和导出流程。
- 计划时间线、待处理事项、暂停、恢复、取消和安全重试。
- 只读证据问答和现有知识范围内的研究。

### M2 负责

- 会话中的临时附件上传与七天保留。
- 附件分类、导入预览、差异展示和人工确认。
- 复用现有知识文件、提示词和任务表导入能力。
- 项目注意事项和话题库的受控变更。
- 证据缺口的精准检索、候选资料审查和计划恢复。

M2 不得成为 M1 上线的阻塞项。M1 的核心文章工作流应当在没有助手附件和配置修改能力时独立可用。

## 3. 不可突破的边界

- 继续保持 Server-only，不新增 SQLite、本地文件准源或 Local fallback。
- 上传文件首先是临时助手附件，不自动成为知识来源。
- 附件内容不得跨 Organization、User 或 Project 泄漏。
- 未明确目标项目的附件不得执行正式导入。
- 模型不得自行猜测提示词类型；创建提示词时必须有明确 `kind`。
- 项目配置变更必须展示结构化差异并由用户确认。
- 知识资产发布是独立人工闸门，不能被一次文章计划确认隐式授权。
- 博客和第三方资料继续只能进入正文参考通道。
- 助手不能执行附件中的代码、宏、脚本、命令或嵌入式指令。
- 不恢复协作者权限；继续使用现有单负责人模型。
- M1 中已经固定的计划快照不因后续项目配置修改而静默变化。
- ZeroGPT 和最终正式交付继续由人工确认。

## 4. 临时附件流程

### 4.1 上传

用户可在助手会话中上传一个或多个文件。后端执行：

1. 从 OIDC 会话派生 Organization 和 User。
2. 将文件保存到私有对象存储的临时前缀。
3. 记录原始文件名、MIME、字节数、内容哈希和到期时间。
4. 校验允许的类型、大小和文件签名，不执行文件内容。
5. 返回短期签名下载 URL 和上传状态。

上传时可以预选项目，但预选不等于正式导入授权。

### 4.2 分类

分类结果只能是允许的业务类型：

- `knowledge_source`
- `prompt_asset`
- `task_workbook`
- `project_notes`
- `topic_library`
- `unsupported`
- `needs_user_choice`

模型输出分类建议、理由和置信信息。以下情况必须让用户选择：

- 一个文件同时可能属于多个类型；
- 无法确定目标项目；
- 无法确定提示词 `kind`；
- 文件结构与现有导入契约不兼容；
- 内容可能影响多个项目。

### 4.3 导入提案

分类完成后只生成导入提案，不立即写正式业务表。提案应展示：

- 目标项目；
- 目标业务类型；
- 将创建、更新或跳过的记录；
- 重复项和冲突；
- 解析失败或无法识别的内容；
- 对现有配置的前后差异；
- 是否还需要知识发布审查。

用户可以确认整个提案，也可以排除某些项目或条目后生成新的提案 revision。

### 4.4 正式导入

确认后由类型化工具路由到现有能力：

- 知识文件：进入现有私有文档解析、候选来源和发布审查流程。
- 提示词：进入项目 Prompt Repository，明确类型并生成可停用的版本历史。
- 任务表：复用现有工作簿预览、校验和导入流程。
- 项目注意事项：生成项目规则差异，确认后写入受审计版本。
- 话题库：生成新增、更新、重复和冲突清单，确认后写入项目范围的话题记录。

确认动作必须带提案 revision 和幂等键。重复提交不得产生重复记录。

## 5. 项目配置变更

### 5.1 自然语言变更

用户可以说：

> 把野辉后续文章改成只面向 B 端采购商，并保留原来的产品参数要求。

助手应生成变更提案，而不是直接改配置：

- 读取当前有效提示词和注意事项版本。
- 将用户要求拆成明确的新增、修改和保留项。
- 展示逐字段或逐段差异。
- 标记可能影响标题、产品选择、大纲或正文的规则。
- 用户确认后调用现有项目配置服务。
- 记录操作者、确认内容、旧版本、新版本和审计事件。

### 5.2 提示词类型

提示词创建和导入必须显式选择：

- 标题或选题提示词；
- 大纲提示词；
- 正文提示词；
- 复检提示词；
- 其他系统已支持的确定类型。

不能通过模型猜测后直接落库。无法匹配现有类型时，提案保持 `needs_user_choice`。

### 5.3 对运行中计划的影响

- 已确认计划继续使用固定的 Prompt version、项目规则 revision 和知识快照。
- 配置修改默认只影响随后创建或重新确认的计划。
- 用户要求正在运行的计划采用新配置时，必须生成计划 revision 和影响差异。
- 已完成的文章步骤不自动重跑。

## 6. 精准证据缺口补全

### 6.1 触发条件

研究步骤发现以下问题时，可暂停为 `waiting_review`：

- 缺少产品参数或适用工况；
- 产品图片与产品页无法建立证据对应；
- 项目案例、认证或材料依据不足；
- 关键采购结论只有博客或第三方来源支持；
- 已有资料相互冲突。

### 6.2 补全顺序

1. 检索当前项目已发布知识和当前快照。
2. 输出明确的缺口清单，不使用笼统的“资料不足”。
3. 在项目官方网站域名内进行定向搜索。
4. 对 URL、规范化正文和内容哈希去重。
5. 将候选资料分入证据通道或正文参考通道。
6. 展示候选来源、可支持事实和冲突。
7. 用户确认并完成知识发布。
8. 创建新的知识快照。
9. 对原计划生成可见 revision 并恢复未完成研究步骤。

### 6.3 限制

- 默认不启动全站扫描。
- 定向检索必须有缺口字段、目标项目和官方域名。
- 每次检索有页面数、深度、时间和模型调用软预算。
- 博客页面即使位于官方网站，也不能升级为 Evidence Pack 或 Hard Fact。
- 候选资料在正式发布前不能被写作工具当作已确认事实。

## 7. 后端结构

在 M1 的 `backend/workflow_assistant/` 基础上增加：

- `attachments.py`：临时附件生命周期和对象存储访问。
- `classification.py`：严格的附件分类契约。
- `import_proposals.py`：导入提案、revision、差异和确认。
- `project_changes.py`：提示词、注意事项和话题库变更适配器。
- `gap_fill.py`：证据缺口结构、定向检索和恢复条件。

优先复用现有实现：

- 私有文档上传和解析；
- 知识候选来源及人工发布；
- Prompt Repository 和版本管理；
- 任务工作簿 preview/import；
- 项目话题及配置服务；
- PostgreSQL Job Queue、Audit 和对象存储签名 URL。

不得为了助手重新实现第二套知识上传、Prompt 存储或任务导入系统。

## 8. 数据模型

所有结构通过 additive Alembic 迁移增加。

### `assistant_attachments`

- organization_id
- creator_user_id
- conversation_id
- proposed_project_id
- object_key
- original_filename
- mime_type
- byte_size
- sha256
- classification
- classification_payload JSONB
- status
- expires_at
- created_at / updated_at

建议状态：

- `uploaded`
- `classifying`
- `needs_user_choice`
- `proposal_ready`
- `importing`
- `imported`
- `rejected`
- `expired`
- `failed`

### `assistant_import_proposals`

- attachment_id
- target_project_id
- target_kind
- normalized_diff JSONB
- revision
- status
- confirmed_by / confirmed_at
- resulting_entity_refs JSONB
- standardized_error_code

建议状态：

- `draft`
- `awaiting_confirmation`
- `confirmed`
- `running`
- `waiting_publication`
- `completed`
- `failed`
- `cancelled`

知识发布记录、Prompt version、任务导入和话题记录继续使用其现有业务表，不复制到助手表中作为第二准源。

## 9. API 草案

- `POST /api/workflow-assistant/conversations/{conversation_id}/attachments`
- `GET /api/workflow-assistant/conversations/{conversation_id}/attachments`
- `GET /api/workflow-assistant/attachments/{attachment_id}`
- `POST /api/workflow-assistant/attachments/{attachment_id}/classify`
- `POST /api/workflow-assistant/attachments/{attachment_id}/proposals`
- `GET /api/workflow-assistant/import-proposals/{proposal_id}`
- `POST /api/workflow-assistant/import-proposals/{proposal_id}/revise`
- `POST /api/workflow-assistant/import-proposals/{proposal_id}/confirm`
- `POST /api/workflow-assistant/import-proposals/{proposal_id}/cancel`
- `POST /api/workflow-assistant/plans/{plan_id}/gap-fill`

下载接口只返回短期签名 URL。所有读取、分类、提案、确认和导入动作重新校验 Organization、User 和目标项目权限。

## 10. 前端设计

在 `/assistant` 增加：

- 输入框附件按钮和上传进度；
- 临时附件卡片及剩余保留时间；
- 分类结果和“需要选择”状态；
- 目标项目和提示词类型选择；
- 导入提案与结构化差异；
- 冲突、重复和失败条目过滤；
- 知识候选来源审查入口；
- 证据缺口列表和定向搜索进度；
- 发布后恢复原文章计划的确认按钮。

用户必须能区分：

- 临时上传完成；
- 已生成导入提案；
- 已导入业务系统；
- 已发布为可作证知识。

不能把“文件已上传”显示成“知识库已更新”。

## 11. 保留、清理与审计

- 未正式导入的临时附件保留七天。
- 清理任务只删除已过期的临时对象和对应临时元数据。
- 已导入的正式知识、Prompt、任务或话题按各自业务保留策略管理。
- 到期前在附件卡片显示时间，允许用户提前拒绝附件。
- 分类、提案、确认、导入、发布、失败和清理均记录标准化审计事件。
- 审计保存文件哈希和业务引用，不长期复制完整临时文件内容。

## 12. 功能开关与上线

在主开关 `workflow_assistant_enabled` 下增加能力开关：

- `workflow_assistant_attachments_enabled`
- `workflow_assistant_project_changes_enabled`
- `workflow_assistant_gap_fill_enabled`

能力在完成验证后对所有项目负责人开放，不增加用户白名单管理 UI。任何子开关关闭时，M1 的只读问答和文章工作流仍可继续使用。

## 13. 实施里程碑

### M2.0：重新基线和契约确认

- [x] 从合并 M1 后的稳定主分支创建新工作树和新分支。
- [x] 核对 M1 实际 Schema、API、页面和工具目录，不按旧计划猜测接口。
- [x] 固定允许的附件类型、大小限制和导入契约。

### M2.1：附件暂存

- [x] 增加 additive Alembic、PostgreSQL Repository、私有对象存储前缀和七天到期清理边界。
- [x] 实现上传、下载签名 URL、内容哈希、幂等和 Organization/User/Conversation 权限隔离。
- [x] 对预选项目重新执行 `project.view` 鉴权；上传成功不触发分类、导入或发布。
- [x] 补齐附件卡片、多文件上传进度、失败重试、下载和拒绝交互。
- [ ] 完成真实浏览器跨用户隔离验收。

### M2.2：分类和导入提案

- [x] 实现严格分类结构和 `needs_user_choice`，附件正文始终按不可信数据处理。
- [x] 使用独立 PostgreSQL durable Job 生成可复查的目标项目、类型、重复项、冲突和差异。
- [x] Proposal 使用 actor scope、幂等、revision/CAS 和执行前/提交前重新鉴权。
- [x] 确认只释放 Proposal，不调用正式导入或知识发布服务。
- [ ] 在真实浏览器完成分类、人工消歧、差异修订和确认验收。

### M2.3：正式导入适配器

- [x] 复用知识文件、Prompt、任务表、注意事项和话题库现有能力。
- [x] 完成 revision、幂等、Audit 和部分条目排除。
- [x] 确认后释放持久化 execute Job；Proposal/附件状态迁移支持 CAS、失败回滚和崩溃重放。
- [x] 在干净 PostgreSQL 上验证 0032 迁移、Proposal/Job 生命周期和 Prompt 导入幂等。

### M2.4：精准证据补全

- [x] 将研究缺口结构化。
- [x] 增加现有知识检索、官网定向搜索、候选审查和快照恢复。
- [x] 验证博客与第三方来源不能进入证据通道。

### M2.5：前端交互

- [x] 在助手计划中接入 waiting-review 研究缺口、官网候选勾选/拒绝、队列状态和恢复计划刷新。
- [x] 补齐附件分类、导入提案、结构化差异、发布审查和其他恢复计划 UI，并支持刷新后恢复最新 Job/Proposal。
- [x] SSE 时间线覆盖上传、解析、导入和 gap-fill 子 Job。

### M2.6：验收与上线

- [x] 完成后端 996 项无数据库回归、前端 lint/build 和差异检查。
- [ ] 在干净 PostgreSQL/对象存储环境完成迁移、七天清理、权限和跨项目隔离验收。
- [ ] 完成真实浏览器验证，并分别报告 Push、CI、生产构建、部署和线上 smoke 状态。

本轮本地验收记录（2026-08-20）：

- 在独立空 PostgreSQL 数据库执行 Alembic 至 `20260820_0032`，M2 专用 PostgreSQL 测试与 MinIO 真实 put/get/list/签名/删除往返测试通过。
- 复用的旧开发数据库卷曾标记为 `20260820_0030`，实际缺少 M2.1/M2.3 需要的约束和 `execution_idempotency_key`；本轮通过 0031 迁移的兼容性修复完成升级至 `20260820_0032`，未删除业务数据。
- Edge 已验证会话可加载本地 3000 端口。由于浏览器扩展未开放 file URL，真实文件选择器上传未完成；随后使用真实 `AttachmentService` 写入一次性 Markdown 测试附件，在浏览器中完成分类重试、`needs_user_choice`、目标项目选择、提案预览、结构化差异修订、取消和拒绝清理；未执行真实项目导入，数据库记录与 MinIO 对象均已清除。
- Push、CI、生产构建、部署和线上 smoke 本轮均未执行。

## 14. 测试重点

- 伪造 MIME、超限文件和不允许类型被拒绝。
- 相同内容重复上传只生成可见重复提示，不重复导入。
- 用户不能读取另一用户的临时附件和提案。
- 项目负责人不能把附件导入无权限项目。
- 无目标项目、无提示词类型或多义文件进入 `needs_user_choice`。
- 重复确认提案不产生双写。
- 旧 proposal revision 不能覆盖新 revision。
- 上传文件中的命令或提示注入不会变成工具调用。
- 临时文件上传不等于知识发布。
- 博客候选不能进入 Evidence Pack 或 Hard Fact。
- 配置修改不改变已确认计划的固定快照。
- 七天到期任务只删除临时对象，不删除正式业务资产。
- 服务重启后继续等待导入、发布或计划恢复。

## 15. 真实验收场景

1. 同一负责人选择两个项目，并分别上传知识文档、提示词文件和任务表。
2. 助手正确分类，遇到模糊文件时要求用户选择。
3. 用户排除一项错误分类并确认其余导入提案。
4. 知识文件进入候选审查而不是直接发布。
5. 提示词明确 `kind`，展示差异并形成新版本。
6. 任务表先 preview，再按项目导入且不重复创建任务。
7. 一篇文章因缺少产品参数暂停，助手只对缺口执行官网定向检索。
8. 用户发布确认来源后，计划固定新知识快照并继续研究。
9. 验证两个项目的附件、配置、话题和证据完全隔离。
10. 验证过期临时附件被清理，正式导入资产仍然存在。

## 16. 开发交接规则

- M2 开发前必须先读取 M1 的实际实现和验收记录。
- 本文档记录目标边界，不允许为了匹配计划而绕过已经上线的服务契约。
- M2 的详细实现若改变 M1 确认模型或数据准源，必须先形成新的架构决策记录。
- M2 不在当前 `codex/workflow-assistant-m1` 交付范围内实现。
- Codex 记忆只保存本文档路径和关键边界，仓库文档始终是正式准源。
