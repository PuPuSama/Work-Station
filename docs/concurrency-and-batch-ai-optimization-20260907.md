# 大计划内存与批量一次润色优化

日期：2026-09-07。基线：6c6a5293。本地实现与隔离验证完成，未提交或部署。

## 本轮改动

| 问题 | 修改后的行为 |
| --- | --- |
| 执行轮询与 SSE 反复加载整份计划及所有私有快照 | 执行读取不含 normalized_plan 和私有快照；仅执行当前被领取步骤时按作用域读取其快照。SSE 使用 overview，计划行锁不加载大 JSON。 |
| 批量润色内部多轮改写、校正再叠加队列重试 | 服务器为批量润色固定 single_pass=true、max_attempts=1。内容不合格直接失败并保留原文；不再自动再次改写。租约恢复无检查点时停止重复生成。 |
| 有效正文等待外部检测完成后才保存 | 先授权/CAS 保存生成结果，再并行 AI 检测和知识库检查；中断恢复复用同一 Job 对应的保存版本。检查写回再次授权/CAS。 |

批量策略：首次有效正文 AI 率 **>40%**，复用首次正文、跳过润色、继续后续交付，卡片显示 **AI 率过高**。**=40% 或更低**最多润色一次；无有效得分不当作高分。高分跳过不设置人工 confirmed，使用 deferred。卡片警告来源于持久化步骤结果，刷新和后续交付不丢失。手动工作台仍保留原有操作规则。显式人工重试不属于自动重复执行。

主要文件：
- `backend/workflow_assistant/repository.py`、`execution.py`、`http.py`：轻量读取与单步骤快照加载。
- `backend/services/ai_rate_policy.py`、`backend/workflow_assistant/adapters.py`：批量阈值、单次执行选择与审计。
- `backend/services/server_humanize_generation.py`、`job_queue.py`、`postgres_job_queue.py`：调用与重试次数限制。
- `backend/models.py`、`backend/services/server_generation_checks.py`、`server_article_generation.py`、`server_task_commands.py`：持久化恢复点、并行检查、授权/CAS 审计。
- `frontend/src/components/workflow-article-cards.tsx`、`batch-writing-workspace.tsx`、`frontend/src/lib/batch-ai-policy.ts`：批量规则说明与卡片警告。

## 合成基准

同一隔离 PostgreSQL、339 个步骤，每步附带合成资料；无真实文章或外部模型。对比当前完整读取路径与新执行/SSE 读取路径，每种路径测量两次。以下为 tracemalloc 的 Python 分配峰值，不是生产进程 RSS。

| 读取路径 | 两次耗时（秒） | Python 峰值 MiB |
| --- | --- | --- |
| 完整计划 | 0.610 / 0.727 | 72.85 / 72.86 |
| 新执行视图 | 0.072 / 0.043 | 0.56 / 0.50 |
| SSE overview | 0.048 / 0.049 | 0.46 / 0.41 |

此结果说明大计划重复读取造成的分配量明显减少，不构成“已排除所有内存泄漏”的证明。生产需部署后在相同文章负载下持续观察。全局/项目 Job 并发上限未增加。

## 验证结果

- 独立 PostgreSQL 容器，Alembic head 和 LangGraph checkpoint schema 就绪。全量 `python -m unittest discover -s backend/tests -q`：1158 项，1156 通过、2 跳过，42.470 秒。跳过项是历史 Local 模式及未启用的 S3 opt-in；后者随后在独立 MinIO 单独通过。
- 新覆盖：40% 严格边界、无得分/NaN/过期正文哈希、低分仍执行一次、无效/超长输出不再次调用、队列瞬时错误不自动重试、被中断 Job 不重新润色、已保存正文恢复、重复终态幂等、用户修改后冲突、取消/可选检查失败保留正文、两个检查并行、私有计划隔离与单步骤快照加载。
- `node --test src/lib/batch-ai-policy.test.mjs`：2 项通过。`npm.cmd run lint`、`npm.cmd run build` 通过。临时卡片验证页面已移除，不进入构建。
- 真实 Next 页面组件配合虚构数据：高分警告与已完成状态并存，展开显示润色已跳过，刷新后仍显示，40%/20% 不误标；当前浏览器页面无控制台错误。
- 本地 MinIO 实际上传及签名下载 `Buyer Guide.docx`、`D.docx`、`delivery.zip`，下载 SHA-256 与保存对象一致，未签名访问均为 403。ZIP 内正文与 TDK 字节一致，正文含 1 个表格、1 张嵌入图片和有效链接关系，metadata AI 率为 72%，未伪造最终 AI 截图或人工确认。
- 可复查结果保存在忽略目录 `outputs/concurrency-20260907/`：benchmark.json、backend-regression.log、frontend-build.log、frontend-lint.log、s3-integration.log、download-smoke.json、batch-card.png 和下载文件。

## 范围与限制

生成调用次数和并发测试使用固定提供方，不是文章质量或线上真实付费模型性能测试。未修改服务器真实文章、数据库、对象、凭据或交付包。并行检查仍占用原 Job 并发槽直到结束，没有引入新后处理队列；真正拆队列涉及活动任务排他和交付等待协议，应独立设计和验证。进程在模型已返回但保存前退出时，无法恢复尚未落盘的文本；批量润色会停止自动再次调用，需人工处理。

后续优先项：部署后的持续内存/并发观察；必要时增加独立后处理任务及分阶段耗时指标。上线前必须由用户授权提交/部署。测试通过降低回归风险，不能承诺不存在任何新缺陷。
