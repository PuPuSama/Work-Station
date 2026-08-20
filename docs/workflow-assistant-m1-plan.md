# Workflow Assistant M1 实施方案

- 状态：工程实现与隔离环境延期交付验收完成；真实 ZeroGPT 与正式交付未执行
- 确认日期：2026-08-17
- 验收日期：2026-08-20
- 验收记录：[`validation/workflow-assistant-m1-acceptance.md`](validation/workflow-assistant-m1-acceptance.md)
- 初始稳定基线：`origin/main` / `f795884`
- 最新同步主线：`main` / `76dfe5c`
- 开发分支：`codex/workflow-assistant-m1`
- 开发工作树：`D:\Project\article\article-agent-workflow-assistant`
- 稳定写作目录：`D:\Project\article\article-agent-formal`

## 1. 目标

在 Article Agent Server-only 应用中增加一个工作区级自然语言助手。用户可以用自然语言查询项目资料、生成跨项目文章计划，并在一次确认后让助手调用现有标题、产品、研究、写作、复检和导出能力完成文章流程。

第一版解决的是文章生产编排，不是通用编程 Agent，也不是能够修改自身代码的运维 Agent。

## 2. 不可突破的边界

- 保持纯 Server-only，不恢复 Local、SQLite、密码登录或旧无项目作用域 API。
- PostgreSQL 中的业务状态机、Task revision、Job Queue 和 Audit 继续作为准源。
- LangGraph 只负责跨步骤编排、暂停和恢复，不替代业务状态机。
- 助手只能调用显式白名单业务工具，不能执行 Shell、Git、部署或任意 SQL。
- Worker 在执行和提交时重新校验项目权限、任务状态和 revision。
- 同一文章不能同时存在两个活动写作任务。
- ZeroGPT 始终由人工操作，助手不得伪造 AI 率或截图结果。
- 官方博客和第三方文章只能作为正文引用资料，不能进入 Evidence Pack、Hard Fact、产品证明或证据引用链。
- 最终正式交付必须由用户确认；缺少 AI 率截图的包只能标记为“待确认交付包”。
- 现有手动文章工作台和 `ServerResearchWorkspace` 保持可用。

## 3. 已确认的产品决策

### 3.1 助手定位

- 一个独立的工作区级 `/assistant` 页面。
- 不做全局悬浮聊天窗，不在每个业务页面放快捷对话框。
- 页面允许选择项目和文章范围，默认范围为当前用户可访问的工作区。
- 一个会话可以讨论多个项目，但计划中的每一步必须绑定确定的项目和文章。
- 会话对创建者私有；项目计划和执行结果仍遵循现有项目权限。

### 3.2 用户与项目

- 保持当前单负责人权限模型，不在 M1 引入协作者。
- 一个用户可以负责多个项目，并能在一个确认计划中跨项目执行。
- 每个项目分别加载自己的提示词、注意事项、产品、话题和知识快照。
- 禁止把某个项目的提示词、产品或证据带入另一个项目。

### 3.3 确认与自主执行

- 写操作先生成完整结构化计划，由用户一次确认。
- 计划确认后，模型只能在已确认范围和软预算内自主调用白名单工具。
- 只读问答不需要确认。
- 如果计划明确委托，助手可自动选择标题、产品和大纲。
- 以下情况必须暂停并重新获得确认：
  - 发布新的知识资产或产品资料；
  - 增加原计划之外的项目、文章或动作；
  - 修改项目提示词、注意事项或话题库；
  - 正式最终交付。
- 已确认计划保存不可变计划哈希；任何实质变更都会使旧确认失效。

### 3.4 任务选择

当用户要求“每个项目写 N 篇”时：

1. 优先使用已有且未开始的文章任务。
2. 数量不足时，从已发布话题库提出补充任务。
3. 创建前检查重复主题、主关键词和近似搜索意图。
4. 新任务必须在计划预览中展示，确认后才能创建。

### 3.5 模型与预算

- 助手规划与文章生成复用当前全局/项目文章模型配置。
- 规划使用独立系统提示词和严格结构化输出，不让模型猜测任意工具或提示词类型。
- 按用户、项目和计划记录调用次数、token 与可得的成本估算。
- 预算为软限制：接近限制时提醒，项目负责人可以继续，不做硬停止。
- 已知临时模型故障使用同一模型最多重试两次，不自动切换模型。

### 3.6 并发和浏览器关闭

- 不同文章可并行，团队默认最大并发数为 3，后端配置可调整。
- 同一文章继续受现有活动 Job 和 revision/CAS 约束。
- 计划和子 Job 持久化到 PostgreSQL；关闭浏览器不会停止任务。
- 服务重启后从 Job lease、步骤状态和 LangGraph checkpoint 恢复。

### 3.7 运行中调整

- 暂停和取消立即生效。
- 其他自然语言调整生成计划 revision 和差异，确认后只影响未完成步骤。
- 已完成步骤不自动回滚。
- 权限变化、revision 冲突、数据库错误、未知程序错误直接停止并提示人工处理。
- 只有明确分类为可恢复的临时故障才允许最多两次重试。

### 3.8 可见进度

展示：

- 计划和步骤状态；
- 当前执行动作；
- 子 Job 状态；
- 重试次数和标准化错误；
- 等待用户确认的事项；
- 最终结果与产物链接。

不展示：

- 模型思维链；
- 密钥；
- 未脱敏供应商响应；
- 完整底层系统提示词。

助手页面提供自己的待处理收件箱及侧边角标，不在 M1 建设通用通知平台。

## 4. 证据与研究设计

### 4.1 统一入口

新助手同时提供：

- 项目知识和证据的只读问答；
- 通过自然语言触发现有研究与 Evidence Pack 流程；
- 文章工作流的整体执行。

旧的孤立只读研究助手 UI 在新入口达到功能等价后移除。其检索、引用校验和证据边界继续作为底层能力复用。

### 4.2 精准缺口补全

缺少事实支撑时，不启动默认全站扫描：

1. 先检索当前项目已发布知识库。
2. 明确缺少的事实类型和目标字段。
3. 只在项目官方网站域名内定向搜索。
4. 对 URL、内容哈希和现有来源去重。
5. 生成待审查候选来源及影响说明。
6. 用户确认后发布到项目知识库。
7. 固定新知识快照并恢复原文章步骤。

### 4.3 两条来源通道

证据通道：

- 官方产品详情页；
- 官网企业资料；
- 已发布项目文档、参数手册、检验报告和案例；
- 经审核允许作证的项目知识资产。

正文参考通道：

- 官方博客；
- 第三方博客或行业文章；
- 其他只适合作为正文背景的资料。

正文参考通道的内容不得升级为 Hard Fact、产品证明或 Evidence Pack 引用。

## 5. 后端结构

新增目录 `backend/workflow_assistant/`：

- `contracts.py`：带动作判别字段的 Pydantic 请求、计划、步骤和结果结构。
- `context.py`：用户、组织、项目、任务、提示词和知识快照上下文解析。
- `planner.py`：使用现有模型生成严格 JSON 计划，不直接执行动作。
- `policy.py`：动作白名单、项目权限、计划哈希、预算、人工闸门和 revision 校验。
- `repository.py`：助手会话、计划、步骤、事件和使用量的 PostgreSQL Repository。
- `tools.py`：对现有标题、产品、研究、文章和导出服务的类型化封装。
- `graph.py`：LangGraph 节点、interrupt、checkpoint 和恢复规则。
- `execution.py`：提交现有 PostgreSQL Job，等待结果并恢复上层计划。
- `retention.py`：会话和临时资产生命周期清理。
- `http.py`：项目作用域和工作区作用域 API。

`backend/app.py` 只负责注册路由和启动恢复，不在应用启动阶段建表或改表。

## 6. 数据模型

所有结构通过 Alembic 迁移增加，M1 迁移保持 additive。

### `assistant_conversations`

- organization_id
- creator_user_id
- title
- created_at / updated_at / expires_at
- last_project_ids

### `assistant_messages`

- conversation_id
- sequence
- role
- sanitized_content
- request_id / idempotency_key
- created_at

### `workflow_plans`

- organization_id
- creator_user_id
- conversation_id
- natural_language_request
- normalized_plan JSONB
- plan_hash
- revision
- status
- concurrency_limit
- budget_warning
- approved_by / approved_at
- attention_state

计划状态：

- `draft`
- `awaiting_confirmation`
- `queued`
- `running`
- `waiting_review`
- `paused`
- `completed`
- `failed`
- `cancelled`

### `workflow_plan_projects`

- plan_id
- project_id
- authorization_snapshot

### `workflow_plan_steps`

- plan_id
- sequence
- action_kind
- project_id
- article_task_id
- expected_task_revision
- pinned_prompt_version
- pinned_knowledge_snapshot
- status
- background_job_id
- retry_count
- hard_gate
- input_summary JSONB
- output_summary JSONB
- standardized_error_code

步骤状态：

- `pending`
- `running`
- `waiting_job`
- `waiting_review`
- `succeeded`
- `failed`
- `skipped`
- `cancelled`

### `workflow_plan_events`

- plan_id
- monotonically_increasing_sequence
- event_kind
- public_payload JSONB
- created_at

用于 SSE 重连、时间线和审计，不保存模型思维链。

### `assistant_usage_events`

- user_id / project_id / plan_id
- provider / model
- operation_kind
- input_tokens / output_tokens
- estimated_cost
- created_at

## 7. API 草案

- `POST /api/workflow-assistant/conversations`
- `GET /api/workflow-assistant/conversations`
- `GET /api/workflow-assistant/conversations/{conversation_id}`
- `POST /api/workflow-assistant/conversations/{conversation_id}/messages`
- `GET /api/workflow-assistant/plans/{plan_id}`
- `POST /api/workflow-assistant/plans/{plan_id}/confirm`
- `POST /api/workflow-assistant/plans/{plan_id}/pause`
- `POST /api/workflow-assistant/plans/{plan_id}/resume`
- `POST /api/workflow-assistant/plans/{plan_id}/cancel`
- `POST /api/workflow-assistant/plans/{plan_id}/revise`
- `GET /api/workflow-assistant/plans/{plan_id}/events/stream`
- `GET /api/workflow-assistant/attention-count`

所有接口从已验证 OIDC 会话派生 Organization、User 和角色。确认和执行时重新授权计划涉及的每一个项目。

## 8. M1 工具目录

### 只读工具

- 列出当前用户负责的项目。
- 列出和读取项目文章任务。
- 读取项目提示词和注意事项摘要。
- 读取已发布话题和确认产品。
- 进行项目隔离的证据问答。
- 查询计划、步骤和 Job 状态。

### 确认计划后允许的写工具

- 从已发布话题创建文章任务。
- 生成标题并选择标题。
- 生成产品候选并确认最多三个产品。
- 生成并确认大纲。
- 启动或恢复研究并生成 Evidence Pack。
- 生成正文、自动人化、复检和应用修订。
- 处理品牌链接、产品链接和图片位置。
- 生成 DOCX、TDK 和待确认交付包。

工具封装必须调用现有 Service/Repository，不通过任意 URL、任意 SQL 或内部 HTTP 绕过权限层。

## 9. 前端结构

新增：

- `frontend/src/app/assistant/page.tsx`
- `frontend/src/components/workflow-assistant-workspace.tsx`
- 会话列表与新会话组件
- 项目/文章范围选择组件
- 计划预览与确认卡片
- SSE 执行时间线
- 待处理、失败和未读完成列表

只在工作区入口增加一个助手导航入口和角标。现有文章工作台、项目设置和研究工作台不进行大规模重构。

## 10. 配置与保留策略

新增：

- `workflow_assistant_enabled`
- `WORKFLOW_ASSISTANT_ENABLED`
- 默认并发数 3
- 软预算提醒阈值

发布完成并通过验收后，对所有项目负责人开放，不做用户白名单或配套管理 UI。保留全局开关作为紧急停用手段。

数据保留：

- 私人聊天：30 天。
- M2 临时助手附件：7 天。
- 已确认计划、配置变更和审计：长期保留。

## 11. 第二阶段范围

M2 是独立交付阶段，不与 M1 同批实现。完整方案见
[`workflow-assistant-m2-plan.md`](workflow-assistant-m2-plan.md)。2026-08-20 用户明确接受
M1 的隔离环境 `deferred` 验收作为 M2 本地开发前提；M2 仍须在 M1 工程候选合并到稳定
主分支后创建独立分支。该决定不把缺少 ZeroGPT 截图的待确认交付包升级为正式交付。

M2 增加：

- 临时助手附件区；
- 复用现有上传和文档解析代码；
- 附件分类为知识导入、提示词导入、任务表导入或项目注意事项；
- 用户确认导入方案后再进入正式业务存储；
- 项目提示词、注意事项和话题库的差异预览与确认修改；
- 更丰富的定向证据缺口补全交互。

上传文件默认不是知识来源，未经确认不得直接发布到项目知识库。

## 12. 实施里程碑

### M0：隔离开发基线

- 从稳定 `origin/main` 创建独立工作树和分支。
- 带入 `.codex/config.toml`。
- 当前稳定写作目录不切分支、不复制产物和密钥。
- 新工作树使用独立开发数据库、对象存储和本地端口。

### M1.1：Schema、Repository 与权限骨架

- 增加 Alembic 迁移和 ORM。
- 实现会话、计划、步骤、事件和用量 Repository。
- 增加功能开关、项目重新授权、计划 CAS 和幂等键。

### M1.2：只读助手和计划草稿

- 会话 API。
- 项目隔离的只读证据问答。
- 结构化 planner、动作白名单和计划预览。
- 计划哈希、软预算和任务/话题选择规则。

### M1.3：确认、编排与写作工具

- 实现确认、暂停、恢复、取消和修订。
- 封装现有文章工作流服务。
- LangGraph 等待子 Job、恢复和可恢复重试。
- 实现并发 3、同文单活动任务和 revision 冲突处理。

### M1.4：助手页面

- `/assistant` 工作区。
- 计划确认卡片。
- SSE 时间线。
- 待处理收件箱和侧边角标。
- 保持手动页面回归兼容。

### M1.5：验收、合并和上线

- 完成后端测试、前端 lint/build、迁移检查和 `git diff --check`。
- 运行真实双项目四文章验收。
- 分别验证 Git push、CI、生产构建、部署和线上 smoke。
- 所有状态明确区分，不用“已推送”代替“已部署”。

## 13. 测试与验收

### 自动测试

- 拒绝未知动作、任意工具名和越权项目 ID。
- 计划哈希稳定，计划变更使旧确认失效。
- 同一负责人跨多个项目可执行，其他用户不能读取私人会话。
- 项目提示词、产品、知识快照和文章任务完全隔离。
- 幂等消息、重复确认和重复 Job 不造成双写。
- revision/CAS 冲突返回可见差异，不静默覆盖。
- 权限在确认后被撤销时，Worker 停止提交。
- 同一文章活动 Job 冲突。
- 并发数为 3。
- 服务重启后恢复等待中的计划。
- 博客和第三方资料不能进入证据通道。
- 缺少 AI 率截图时不能标记正式交付。

### 前端验证

- 会话和计划页面正常加载。
- SSE 断线重连不丢事件。
- 暂停、取消、失败和等待确认状态清晰。
- 不显示原始提示词、密钥或思维链。
- 执行后验证真实浏览器 hydration、控制台和交互，不只检查 HTTP 200。

### 真实验收场景

使用一个负责两个项目的账号：

1. 两个项目各生成两篇文章。
2. 现有未开始任务优先，不足时从话题库补充。
3. 并发上限为 3。
4. 每篇经过大纲研究和 Evidence Pack。
5. 中途暂停其中一个项目。
6. 关闭浏览器，确认后端继续执行。
7. 验证项目提示词、产品、话题和知识不串用。
8. 生成缺少 AI 率截图的“待确认交付包”。
9. 人工补图并确认后才形成正式交付。

## 14. 回滚策略

- 关闭 `workflow_assistant_enabled` 即可停止新入口和新计划。
- 现有手动文章及研究页面始终保留。
- 停止助手 runner 时保留计划、步骤和审计记录。
- 数据库迁移为 additive，上线回滚不自动降级生产 Schema。
- 助手失败不得破坏现有文章 Task、产品、知识快照或交付包。

## 15. 开发交接规则

- 本文档是 Workflow Assistant M1 的正式实施准源。
- 后续新对话先阅读本文档和仓库根目录 `AGENTS.md`。
- 若产品决策变化，先更新本文档并记录原因，再修改实现。
- 不把详细方案只放在聊天历史或 Codex 记忆中。
- Codex 记忆只记录本文档路径、稳定基线、工作树路径和关键边界，避免记忆摘要替代仓库事实。
