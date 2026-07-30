# Knowledge Agent M5：研究工作台、状态流与只读引用问答

> 状态：完成
> 分支：`feature/knowledge-agent-m5`
> M4 检查点：`01cc88c`

## 1. 目标与非目标

M5 把 M3/M4 已有的持久化对象变成可恢复、可理解的产品流程：

1. 在文章“写作”阶段加入“资料研究”子步骤；
2. 展示 Retrieval Scope、Evidence Pack、来源卡片、Graph Run 时间线、补查轮次、
   查询成本摘要和脱敏失败；
3. 使用 SSE 推送结构化事件，断线后从最后 sequence 轮询恢复；
4. 提供只读研究助手，回答只能引用当前项目已发布的 KnowledgeChunk；
5. 结构化轨迹和对话默认保留 30 天。

M5 不允许对话改文章、改产品、批准来源、发布知识、调用任意 URL 或绕过
PublicationGate。写操作仍只能走现有确定性 API 与人工确认。

## 2. 信息架构

M5 不把所有内容塞进一个长页面。主视图采用三层渐进披露：

```text
文章工作台 / 写作
  ├─ 大纲
  ├─ 资料研究
  │    ├─ 顶部：当前 Plan + 启动/状态/预算
  │    ├─ 左侧：Run 历史与状态
  │    ├─ 主区：Scope / Evidence Pack / 来源卡片
  │    ├─ 折叠区：Graph 时间线与 GapFill attempts
  │    └─ 人工中断：候选 URL 复选 + 恢复
  └─ 第一版

右侧 Sheet：研究助手（只读、逐条引用）
```

独立“知识库”页面继续保留项目级 M2/M3 运营工具，并复用同一个
`ProjectResearchWorkspace`。文章页传入固定 article identity；知识库页允许查看项目
全部 Run。这样不会在 `article-workbench.tsx` 再复制一套状态逻辑。

## 3. 视觉与交互规则

沿用 `design-system/article-agent/MASTER.md`，不生成第二套主题：

- Newsreader 只用于页标题，正文和数据继续使用 Roboto/系统 sans；
- 状态同时显示文字、图标和 Badge，不只依赖红黄绿；
- 时间线为紧凑列表而非装饰性图表；attempt、耗时和节点名使用等宽小字；
- 300ms 以上操作显示 Spinner/Skeleton；提交期间按钮禁用；
- 人工恢复候选有可见 label、URL 和分类证据，未知 URL 不允许手输；
- 交互目标至少 44px，键盘 focus ring 保留；
- 动效限制在 150–300ms 的颜色/透明度变化，并尊重 reduced motion；
- 375px 使用单列，768px 以上再展开 Run 列表与详情，不产生横向滚动。

## 4. SSE 契约

端点：

```text
GET /api/knowledge/{project}/research-runs/{thread_id}/events/stream
    ?after_sequence=<last_seen>
```

事件：

| SSE event | id | data |
|---|---|---|
| `research_event` | `sequence` | 单个 `ResearchEventResponse` |
| `heartbeat` | 无 | 当前服务器时间 |
| `run_state` | 最后 sequence | 脱敏 Run 摘要 |
| `done` | 最后 sequence | 终态状态 |

重连使用浏览器 `Last-Event-ID` 或显式 `after_sequence`。SSE 只通知“什么变了”，
Evidence Pack/来源正文仍通过普通 GET 按 ID 获取。EventSource 失败后关闭连接，以
3 秒轮询详情；浏览器重新在线时再尝试 SSE，避免同时存在两个更新循环。

## 5. Outline 到 Retrieval Plan

文章工作台不要求运营人员手填 Plan ID。确认大纲后，通过应用层端点把 SQLite
Task 的只读快照转为 PostgreSQL 不可变 Plan：

- article identity：项目内 `topic_{topic_index:03d}`；
- outline version：已确认 outline 版本数，旧数据最小为 1；
- 每个 H2 生成 `h2_section`；
- FAQ 标题生成 `faq`；
- 已选择产品生成独立 `product_fact`；
- Plan metadata 保存 task ID 和 outline hash，不保存第二份全文；
- 相同 outline version 重试幂等，内容变化必须形成新 version。

TaskStore 只提供输入快照，Plan 的准源和后续查询仍是 PostgreSQL。

## 6. 只读研究助手

稳定接口：

```text
ResearchAnswerProvider
  answer(question, evidence_hits, recent_messages) -> answer text + cited chunk IDs
```

服务端数据流：

```text
question
  -> project-scoped BasicHybridRetriever
  -> published/current/model-matched chunks only
  -> bounded prompt with numbered chunk IDs
  -> provider answer
  -> validate every citation against supplied hit IDs
  -> persist message + chunk FK citations
```

模型不能自行声明 URL 或 Source。公开链接由服务端根据被引用 Chunk 的 Provenance
补回；未被 Evidence 命中的 ID 被拒绝。Provider 失败不持久化伪答案，异常正文不返回。

## 7. 保留与安全

- `research_graph_events`、`gap_fill_attempts`、研究对话和消息默认保留 30 天；
- Run 摘要、Evidence Pack 和 EvidenceLink 按业务审计边界保留，不随 UI 轨迹清理；
- 清理使用项目表/时间戳范围 SQL，不全表无条件删除；
- 不保存 API Key、Cookie、连接串、Embedding、网页全文副本或 Provider 原始异常；
- 对话只保存用户问题、最终答案和 Chunk FK；Prompt 拼装文本不落库。

## 8. 数据表与保留策略

迁移 `20260730_0007_research_chat.py` 新增：

| 表 | 身份与用途 |
|---|---|
| `research_conversations` | `(project_id, conversation_id)`；可选绑定 article，记录 30 天到期时间 |
| `research_messages` | 会话内稳定 sequence；只保存 user/assistant 最终文本和幂等 request ID |
| `research_message_citations` | assistant message 到 `(project_id, chunk_id)` 的复合外键和引用顺序 |

引用表不会保存模型自行生成的 URL；读取对话时，通过 Chunk 的 Source 身份补回显示名和
Canonical URL。跨项目 Chunk 因复合外键在同一事务内回滚。

应用启动时执行时间范围清理：

- 对话按 `expires_at < now` 删除，并级联删除消息和引用；
- Graph event 按 `created_at < now - 30 days` 删除；
- GapFill attempt 按 `updated_at < now - 30 days` 删除；
- `research_graph_runs`、Retrieval Plan、Evidence Pack 和 KnowledgeChunk 不在此清理范围。

## 9. 代码地图

| 模块 | 作用 |
|---|---|
| `research_stream.py` | SSE 增量事件与 heartbeat |
| `retrieval_plan_generation.py` | Task outline 到不可变 Scope/Plan 的纯函数 |
| `research_chat.py` | 只读检索问答、引用验证与脱敏错误 |
| `research_chat_repository.py` | 会话、消息和引用持久化/30 天清理 |
| `project-research-workspace.tsx` | Run/SSE/Plan/Pack 的共享前端状态容器 |
| `research-run-timeline.tsx` | 折叠事件与补证记录 |
| `research-assistant-sheet.tsx` | 只读引用问答抽屉 |
| `retention.py` | 30 天到期策略的单一应用入口 |
| `20260730_0007_research_chat.py` | 对话、消息和 Chunk 引用的数据库准源 |
| `app.py#create_task_retrieval_plan` | SQLite Task 到 PostgreSQL Plan 的应用层边界 |

后续若拆成独立服务，前端只依赖 HTTP/SSE 契约；Outline adapter、Answer Provider 和
Repository 可分别替换，不能把 Transport 状态塞回 LangGraph State。

## 10. HTTP 接口

| 方法与路径 | 作用 |
|---|---|
| `GET /api/knowledge/{project}/retrieval-plans` | 按项目/文章列出不可变 Plan |
| `POST /api/knowledge/{project}/tasks/{task_id}/retrieval-plan` | 从已确认 Task 大纲生成 Plan |
| `GET /api/knowledge/{project}/research-runs/{thread_id}/events/stream` | 增量 SSE 事件 |
| `POST /api/knowledge/{project}/research-assistant/messages` | 检索、回答、校验引用并原子保存 |
| `GET /api/knowledge/{project}/research-conversations/{conversation_id}` | 恢复 30 天内的对话和引用 |

这些路径都先规范化并固定 `project_id`。SSE、普通 GET 和对话写入不共享客户端内存作为
准源，因此浏览器刷新后仍可从 PostgreSQL 恢复。

## 11. 最终验收

- Alembic `0006 -> 0007 -> 0006 -> 0007` 通过，重复 `upgrade head` 通过；
- M5 定向后端：24 tests；
- 完整后端：398 tests，1 skipped；
- 前端全量 ESLint、TypeScript 和 Next.js 16.2.10 webpack production build 通过；
- 浏览器实测桌面与 375px：无横向溢出，研究助手 Sheet 可打开，关键交互至少 44px；
- 未调用真实 Embedding/LLM 网关；测试使用确定性假 Provider；
- 本地 build trace 和临时 `node_modules` Junction 已删除，可由构建重新生成；
- M2 qewitfastener 正式数据仍保留，未复制为 M5 对话测试夹具。
