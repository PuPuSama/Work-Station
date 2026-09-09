# Article Agent 协作记忆

本文件只保留跨任务有用的已核对决策，不是执行清单或线上状态报告。使用入口见 [README](README.md)，配置见[配置指南](docs/configuration.md)，其他资料见[文档索引](docs/README.md)。用户当前要求优先；历史设计与当前行为冲突时先核实，不凭历史记录恢复旧行为。

## 部署与数据边界

- 生产运行在 `ssh myserver` 可达的 `/home/ubuntu/Work-Station`。本地构建、Git 推送、CI 和线上服务是不同验证环节；操作前读取目标环境实际版本。
- Compose 项目为 `work-station`，应用使用 `work-station-network`；生产数据库和对象存储独立维护。不要因重命名或重新建环境而新建空卷替代已有数据。
- 只维护 Server：PostgreSQL 是业务数据准源，文件使用私有对象存储及短期签名链接。不得恢复 SQLite、任务 JSON 双写或无项目作用域旧接口。
- 当前登录使用组织账号密码和已验证会话；数据库密码哈希优先于一次性环境密码引导。OIDC 文档保留为历史设计，不是当前必需配置。
- 组织/项目权限、后台执行及提交重授权、revision/CAS、同文章活动 Job 互斥、显式 Alembic 迁移必须保留。

## 配置中容易混淆的点

- dotenv 统一由 `backend/config.py::initialize_environment()` 在应用/CLI 边界读取，业务模块不自行加载环境文件。
- `ARTICLE_AGENT_ENV_FILE` 选择应用 dotenv；`ARTICLE_ENV_FILE` 选择 Compose 宿主机文件。二者不是同一变量，容器路径也不能直接照抄宿主机路径。
- LLM 使用流式 Responses；Embedding 独立配置、固定 1536 维。换 Embedding 模型需要评估索引重建，不能只换 Key/模型名继续混用旧向量。
- MinerU 当前只用于配置后的 PDF 解析，DOCX/Excel 使用本地解析。具体变量和默认值统一维护在配置指南与 `.env.example`。
- 对象存储公开下载 ENDPOINT 和后端内网 ENDPOINT 必须指向同一存储/bucket；浏览器不能使用容器内部服务名下载。
- WordPress 账号按项目保存；Application Password 服务端加密，加密密钥需稳定保存。测试站与业务站使用独立凭据，上传只生成草稿。
- `scripts/local_debug.py` 使用隔离数据和测试身份，主模型密钥禁用；不得连接生产数据或暴露到公网。

## 业务核对入口

| 主题 | 实现/验证入口 |
| --- | --- |
| 批量 AI 阈值、一次润色与生成检查点 | `backend/services/ai_rate_policy.py`、`backend/workflow_assistant/adapters.py`、`backend/tests/test_batch_generation_optimization.py` |
| 产品确认和大纲前置条件 | `backend/services/server_task_commands.py`、`frontend/src/components/server-article-product-selection.tsx` |
| 知识事实与研究证据 | `backend/knowledge_agent/`、`backend/services/server_knowledge_research.py` |
| 大计划读取、SSE 与并发 | `backend/workflow_assistant/repository.py`、`backend/config.py` |
| WordPress 上传 | `backend/services/wordpress_publisher.py`、`backend/tests/test_server_wordpress_upload.py` |

自动检测不等于人工确认。知识证据必须属于当前项目、已发布、当前快照且允许作证；官方博客不进入硬事实证据链。字数、FAQ、图片数量和 AI 阈值以相关实现/配置为核对入口，不在交接记录中维护另一套标准。

## 协作与验证

- 开始先看 `git status --short` 和目标文件 diff，保留其他 worktree/任务的改动。常规整理不删除用户数据、私有配置、学习进度或未完成工作。
- 按范围验证；纯文档和示例配置检查引用、解析及相关配置测试，不机械触发全量生成或真实付费 API 调用。
- 全量测试使用隔离数据库，统计 skip；下载、WordPress 和部署验收区分接口成功与实际可用结果。
- 提交、推送、合并、部署按用户授权执行；`outputs/`、`tmp/`、真实 `.env` 和密钥保持未跟踪。
- 不再追加固定“当前 SHA”、普通调试流水和重复测试总结；有长期价值的新决策应更新对应主题及现行文档。
