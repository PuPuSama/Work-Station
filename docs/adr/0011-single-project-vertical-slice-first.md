# ADR-0011：先交付单项目知识 Agent 垂直切片

- 状态：Accepted
- 日期：2026-07-17
- 范围：实施顺序、数据库迁移、RBAC 和第一版演示边界

## 背景

目标既包括改进公司文章工作台，也包括形成可以展示的实习项目。一次性完成云端多人部署、全部 SQLite 迁移、RBAC、对象存储、知识库和 LangGraph，范围过大，较长时间内无法形成可运行成果。只在原有 SQLite 中实现知识 Agent 又会弱化向量检索、检查点和后续云端扩展能力。

## Decision

1. 第一阶段交付一个真实客户项目的端到端垂直切片。
2. 使用 Docker 启动 PostgreSQL + pgvector，保存知识来源、快照、切块、Embedding、检索计划、Evidence Pack、证据映射和 LangGraph Checkpoint。
3. 当前文章 Task Repository 和 Job Queue 暂时继续使用 SQLite，保持现有文章生产功能稳定。
4. 新知识/Agent 数据通过 `customer + task_id + outline_version` 与现有任务关联。
5. 第一阶段不实现登录、团队管理和完整 RBAC，但所有新表和查询必须包含稳定 `project_id`，避免以后跨项目迁移时重写检索层。
6. 第一阶段实现带引用的只读研究对话；第二阶段只增加“重新检索产品、确认后替换产品、版本快照后重写指定章节”三个白名单写操作，其余对话写操作继续延后。
7. 垂直切片通过验收后，再迁移文章任务和作业到 PostgreSQL，增加 Organization、Team、User、ProjectMembership、对象存储和服务器部署。
8. SQLite 与 PostgreSQL 的阶段性并存必须封装在 Repository 接口后，业务代码不能到处直接连接两个数据库。

## 第一阶段验收

- 第一条真实案例固定为 `www.qewitfastener.com / topic_006`，能定位 woodscrews 精确分类，且 Blog 只能成为参考来源、不能成为产品。
- 一个项目可以上传并确认私有文件。
- 可以首次同步客户 WordPress 产品页与 Blog，并保存来源快照。
- 确认大纲后为每个 H2、产品事实和 FAQ 生成独立 Evidence Pack。
- LangGraph 能在知识不足时执行最多两轮官网/Tavily 补查。
- 遇到模糊页面时可以暂停并等待人工确认后恢复。
- Graph Run 在请求失败或进程重启后可以从 PostgreSQL Checkpoint 恢复。
- 前端可以查看节点时间线、章节证据、来源和知识支撑段落比例。
- 生成正文中的硬事实可以回溯到具体来源快照。
- 使用约 20 条 qewitfastener 话题的人工标准答案输出 Recall@5、MRR、页面分类准确率、错误来源率和正确拒绝率。

## 第二阶段

- 将 Task Repository 和 Job Queue 迁移到 PostgreSQL。
- 增加 Organization、Team、User、ProjectMembership 与 RBAC。
- 将原始文件、图片、截图和导出物迁移到 S3 兼容对象存储。
- 部署到受控云端或公司私有服务器。
- 增加审计、备份、密钥管理和生产监控。

## Consequences

### 正面影响

- 较早形成可演示、可评测的 Agent 产品闭环。
- 不破坏当前已经稳定的文章生产流程。
- PostgreSQL、pgvector 和 LangGraph 检查点不是一次性演示实现。
- 后续多人化有明确迁移边界。

### 代价

- 第一阶段暂时存在 SQLite 与 PostgreSQL 两套存储。
- 单用户演示不能证明完整权限系统已经实现。
- 第二阶段仍需迁移现有任务与作业数据。

## 防止临时架构固化

- 新知识表从第一天使用不可为空的 `project_id`。
- 数据访问必须经过 Repository/Service，禁止组件直接拼 SQL。
- LangGraph State 只保存 ID 和小型结构化状态，大文件与正文仍保存在正式存储中。
- 演示环境明确标记为 Single-project Mode，不能宣称已经实现多租户生产部署。
