# M7 Server Task 写作要求与 Effective Prompt Preview

## 1. 目的和边界

正式 Server 的 Outline/Article Worker 已经读取 Task 上的写作字段，但此前只有 Local
SQLite/文件接口可以修改这些字段。这个切片补齐 PostgreSQL Task 的正式操作面，并保持
Local 与 Server 两套数据边界互不回退。

本能力只管理一篇文章未来生成时使用的配置：

- 话题备注；
- 大纲/正文单篇补充提示词及启用开关；
- 大纲/正文 Project Prompt 选择；
- 是否带入项目介绍、项目注意事项和话题注意事项。

它不修改 Project Prompt 正文或默认指针，不接受客户端提供的 Prompt Snapshot、Knowledge
Chunk、模型、Provider、Project/Task 身份，也不迁移 Task/Job Schema。Project Prompt 继续由
不可变版本目录管理；客户事实继续来自 Published Knowledge。

## 2. Server-only HTTP 契约

```text
PUT  /api/projects/{project}/tasks/{task_id}/writing-settings
POST /api/projects/{project}/tasks/{task_id}/writing-settings/preview
```

两条请求都使用严格 Body，未知字段返回 `422`。路径是唯一的 Project/Task 身份准源，Body
不得重复提交身份。旧 Local 路径继续只在 Local 模式工作：

```text
PUT  /api/tasks/{task_id}/writing-settings
POST /api/tasks/{task_id}/prompt-preview
```

Server Middleware 只开放上面的两条精确 Project-scoped 路径；旧路径在 Server 模式继续
fail closed，不提供兼容别名。

### 写入

写入请求必须携带 Task `revision` 和完整的十个写作字段。HTTP 层先要求 `article.edit`，业务
事务内再次锁定 Project 权限并执行 Task Revision CAS。成功只增加 Task Revision；不改变
Workflow Status，不清除既有 Outline/Article/Delivery，不修改 `last_outline_prompt_snapshot`
或 `last_article_prompt_snapshot`。

| 字段 | 类型与上限 | 作用 |
|---|---|---|
| `topic_notes` | Text，最多 30,000 字符 | 本话题专属要求 |
| `outline_custom_prompt` / `article_custom_prompt` | Text，各最多 40,000 字符 | 单篇大纲/正文补充要求 |
| `use_outline_custom_prompt` / `use_article_custom_prompt` | Boolean | 是否把相应单篇补充要求带入 Builder |
| `outline_prompt_selection` / `article_prompt_selection` | Text，各最多 255 字符 | `system`、`project_default` 或 Active Prompt ID |
| `include_project_introduction` / `include_project_notes` / `include_topic_notes` | Boolean | 是否带入 Task 已捕获的三个上下文字段 |

Preview 使用相同十字段，并额外要求 `kind=outline|article`；它没有“部分更新”语义。

保存时会解析并验证 Outline/Article 的当前 Prompt 选择，但 Task 只保存选择意图：

```text
system | project_default | <active project prompt id>
```

特别地，`project_default` 不在设置保存时固定成某个版本。Preview 在预览时解析一次；真正
生成在 Job 入队时重新解析，并把当时的精确 Prompt ID、Version、Source、Content Hash 以及
Published Chunk IDs 固定到私有 Job Request。这样既允许项目默认值继续演进，又保证已经入队
的 Job 可复现。

### 预览

Preview 接收 `revision`、`kind=outline|article` 和完整的十个表单草稿字段，因此用户无需先
保存即可查看结果。它：

1. 要求 `project.view`，读取路径指定的 PostgreSQL Task，并验证 Revision；
2. 在 Task 深拷贝上应用草稿，不写 Task；
3. 从 PostgreSQL Project Prompt 解析当前精确 Snapshot；
4. 使用与真实 Outline/Article 入队相同的查询文本，选择当前 Published Knowledge；
5. 调用与 Worker 相同的确定性 Prompt Builder；
6. 返回前再次授权，然后输出 `Cache-Control: no-store`。

Preview 不调用 LLM，不创建 Job，不写 Audit，不读 Local TaskStore/PromptStore/客户目录，也不
回退 Mock。Article 缺少已确认标题或大纲时返回 Conflict，而不是构造一个真实生成不会使用的
伪 Prompt。

公开响应包含：

```text
project_id
task_id
task_revision
kind
prompt_snapshot   # 安全身份，无 content/hash
effective_prompt
context_chunk_count
target_words
warnings
```

`prompt_snapshot` 不单独返回 Prompt `content/hash`；组合后的 `effective_prompt` 是授权成员
主动请求的完整当前时点预览，可能包含 Task 私有备注与当前
Published Knowledge 文本，因此不得缓存、写日志、写 Audit 或持久化到前端存储。响应不提供
可由客户端回传的结构化 Chunk ID 列表；生成仍由服务端重新选择并固定上下文。

固定 Warning 文本是：

```text
Preview resolves the current Project Prompt and Published Knowledge; generation pins exact inputs when the Job is enqueued.
```

稳定错误语义：无权访问为 `403`，Task 不在精确 Project Scope 为 `404`，Revision 或生成前置
状态冲突为 `409`，缺字段/未知字段/非法 Prompt 为 `422`，数据库、Audit 或 Prompt 服务不能
安全完成时统一为不含底层异常的 `503`。

## 3. 代码职责

| 组件 | 职责 | 禁止承担的职责 |
|---|---|---|
| `server_task_writing_settings.py` | 规范化十个字段、读取 Project-scoped Task、协调 Prompt 锁 + Task CAS/Audit 同一事务、Preview 渲染与二次授权 | HTTP Cookie、Local 文件、LLM 调用、前端 DTO |
| `server_task_commands.py` | Task CAS、事务内撤权复核、Task 与 Audit 原子提交、Audit 字段白名单 | Prompt 解析、正文/提示词持久化到 Audit |
| `server_project_prompts.py` | 解析当前 Project Prompt 选择与不可变 Snapshot | 保存 Task 选择、生成 Job |
| `server_outline_generation.py` / `server_article_generation.py` | 唯一正式 Prompt Builder、Published Context 选择与 Job 入队固定 | 写作设置 HTTP、Local Context 回退 |
| `server_project_http.py` | 严格请求/安全响应、权限和稳定 HTTP 错误映射、`no-store` | 复制 Prompt 组装逻辑、直接操作 SQL |
| `server_request_security.py` | 只开放精确 Project-scoped Method + Segment，旧 Local 路径在 Server fail closed | 业务授权、Task/Prompt 读取 |
| `app.py` | 只在 Server Lifespan 注入 Factory，Local 为 `None`，teardown 恢复旧状态 | 在请求间持有 Task 草稿或 Preview 内容 |
| `server-writing-requirements-panel.tsx` | 十字段草稿、显式保存、Prompt 目录、折叠 Preview、409 草稿保护 | 自动保存、静默覆盖、Local API 复用 |
| `server-article-workbench.tsx` | 承载面板并在草稿未保存时阻止 Outline/Article 生成 | 再实现一份写作表单 |
| `project-article-workspace.tsx` | 以 Project + Task Key 隔离动态 Workbench 生命周期 | 复用上一 Task 的异步状态或草稿 |

## 4. 事务和审计不变量

写入路径的数据流：

```text
HTTP article.edit
-> load PostgreSQL Task in exact organization/project
-> verify request Revision
-> normalize fields
-> one PostgreSQL business transaction
   -> lock and re-check article.edit
   -> acquire stable Project Prompt transaction lock
   -> resolve and lock current outline/article Prompt heads/default pointers
   -> Task Revision CAS
   -> append article.writing_settings.updated Audit
-> commit Task and Audit together
```

显式 Library Prompt 在事务内锁定 Head，归档/换版不能穿过“验证成功、Task 尚未提交”的
窗口。Project Default 还使用按 Organization + Project 命名的 PostgreSQL transaction-level
advisory lock；Default 行尚不存在时也能阻止并发插入/切换穿过验证窗口。`project_default` 仍
保存为选择意图，事务提交后的正常切换不会改写已经入队的 Job。

Audit 只允许记录：字段是否变化、当前 Use/Include 布尔值、解析到的 Prompt Source 与 Version。
以下内容永远不能进入 Audit、公开错误或日志：topic notes、custom/effective prompt 正文、Prompt
ID/Hash、Knowledge 正文/URL/Chunk 列表、密钥和底层数据库/Provider 异常。

Audit 失败必须回滚 Task；Revision 冲突必须保留浏览器草稿并返回 `409`。写作设置发生变化时
不主动失效下游产物，但 Revision 会递增，因此基于旧 Revision 入队或正在提交的生成 Job 会
安全冲突，不能覆盖新设置。

## 5. 前端状态语义

- 保存是显式操作，不自动保存；Viewer 可查看和预览，但不能保存。
- 面板在 Workbench 步骤切换时保持挂载，未保存草稿不会因为查看 Outline/Article 而丢失。
- 表单 Dirty 时阻止 Outline/Article 生成，先要求保存，避免 UI 预览与 Job 输入不一致。
- `409` 不自动重载、不自动合并；保留草稿并提供明确的重新载入操作。
- 已归档但仍被 Task 选择的 Prompt 继续显示为 Disabled + Warning，不静默改成系统默认。
- `project_default` 明确标注“生成入队时读取项目当前默认并固定精确版本”。
- Preview、Prompt Directory 和 Save 各自维护 Loading/Error，完整 Prompt 默认折叠。
- Project/Task 路由变化会重建 Workbench；内部 Request ID 与 Scope Guard 还会阻止已启动的
  旧 Task Load/Action/Job Continuation 覆盖新页面。
- Preview 显示 Task Revision、Prompt Source/Version、上下文段数和目标词数，并提示它是当前
  时点结果，不是未来 Job 的长期票据。

## 6. 后续重构接缝

后续可以替换 HTTP 框架、前端状态库或把 Task 聚合拆出独立服务，但必须保留以下边界：

1. Local 与 Server 路径/存储不互相回退；
2. Project/Task 身份只来自授权路径；
3. Task Revision CAS 和事务内撤权复核；
4. Task 与脱敏 Audit 原子提交；
5. 设置只保存选择意图，Job 入队固定精确 Prompt/Context；
6. Preview 复用生产 Prompt Builder，不复制模板逻辑；
7. Preview 不调用模型、不写业务状态、不可缓存；
8. 未保存草稿不能被生成按钮静默忽略。

如果未来需要“预览与生成完全同一输入”，应新增短期、一次性、服务端持有的 Preview Token，
在入队事务中消费并固定 Prompt/Context；不能让客户端回传 Prompt 正文、Snapshot 或 Chunk
ID 来冒充已验证输入。
