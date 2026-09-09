# 文档索引

日常使用从根 [README](../README.md) 开始。历史方案用于解释背景，不作为当前安装步骤、生产状态或下一轮任务清单；代码、配置与测试用于核对实现，业务合理性仍需结合当前目标判断。

## 当前使用与运维

| 文档 | 用途 |
| --- | --- |
| [配置指南](configuration.md) | LLM、Embedding、MinerU、存储、登录、开关、网络与验收 |
| [WordPress 草稿上传](wordpress.md) | 项目站点、账号、上传入口与当前限制 |
| [远端 WordPress 测试站](../deploy/wordpress-test/README.md) | 独立测试站服务、证书与测试记录；不是 Article Agent 生产部署脚本 |
| [前端开发入口](../frontend/README.md) | 启动方式、代理和验证 |
| [项目协作记忆](../PROJECT_MEMORY.md) | 少量可复用决策与代码入口；不记录日常操作流水 |
| [知识库术语表](knowledge-base-glossary.md) | 理解领域名词；默认策略需核对当前实现 |

## 架构与历史证据（按需查阅）

| 位置 | 保留原因与适用范围 |
| --- | --- |
| [ADR](adr/) | 架构决策及取舍。旧决策可能被后续 ADR 或实现替代，例如句子覆盖口径替代早期段落口径 |
| [架构记录](architecture/) | M2–M7 的接口、权限、证据、状态机与存储设计；不保证文中的固定版本或未来设计全部适用 |
| [验收记录](validation/) | 当次测试范围与限制；不能把旧“通过/未部署”当作当前线上状态 |
| [M7 切换历史](runbooks/knowledge-agent-m7-server-cutover.md) | 首次 Server 迁移和恢复证据设计，保留备份/权限保障背景；当前运行方式看根 README |
| [知识库路线图](knowledge-agent-implementation-roadmap.md) | 里程碑背景；不再是逐项执行的开发清单 |
| [知识领域模型](knowledge-base-domain-model.md) | 原始建模背景；当前表结构以 Alembic/schema 为准 |
| [工作流助手 M1](workflow-assistant-m1-plan.md) / [M2](workflow-assistant-m2-plan.md) | 已实施阶段与未验收范围的记录，不是当前部署指南 |
| [知识/产品 V2 实施记录](article-knowledge-product-flow-v2-execution-plan.md) | 保留 Article Brief、证据路由等实现思路及后续未完成部分 |
| [并发与批量润色记录](concurrency-and-batch-ai-optimization-20260907.md) | 保留当次合成基准和边界，不能当作生产性能承诺 |
| [WordPress 图片采集](wordpress-image-crawling.md) | 官网图片发现的设计参考；与文章上传是两条链路 |

## 学习与评测

[学习计划](agent-learning-and-delivery-plan.md)、[学习进度](agent-learning-progress.md)和[评测数据说明](../evaluation/README.md)属于学习/标注资料。本轮没有删除或改写学习进度，也没有把历史实验结论当作正式功能验收。

`packaging/` 是历史便携包目录，不属于当前 Server 部署入口。保留文件用于追溯，不能据此恢复旧存储或传播包含环境密钥的打包产物。

## 本次移除的文档

2026-09-09 清理基于 `main` 的 `1a05a1d6`；原文可从 Git 历史查阅，没有删除运行代码、测试或用户数据。

| 移除文件 | 原因/替代入口 |
| --- | --- |
| `openclaw-handoff-prompt.md` | 旧本地路径、SQLite 与固定人工 ZeroGPT 指令已不符现状；使用 AGENTS/README |
| `workflow-v2-plan.md`、`frontend-workflow-restructure-plan.md`、`langgraph-research-agent-ui.md` | 早期改造方案含已被替代的入口、流程与固定规则；当前流程见 README，设计背景保留于架构记录 |
| `article-knowledge-product-flow-v2-plan.md` | 仍标“待实施”的长篇任务指令；已有 V2 实施记录与现行源码 |
| `knowledge-agent-m1-runbook.md`、`knowledge-agent-m2-runbook.md` | 混用 SQLite/旧 API/OIDC、本地目录与旧 MinerU 行为；配置和启动步骤已并入当前指南 |
| `performance-optimization-analysis.md`、`quick-wins-implementation.md` | 旧行号、已处理问题和直接执行生产建索引的建议；当前结构变更只走 Alembic |
| `optimization-summary.md`、`work-summary-cn.md` | 重复总结，将估计收益混入结果；保留有明确条件的并发验证记录 |
| `frontend/docs/bugfix-workflow-assistant-request-storm.md`、`frontend/docs/fix-workflow-assistant-request-storm.md`、`frontend/docs/optimization-implementation-complete.md` | 重复的请求风暴描述、预估收益和旧部署步骤；使用当前源码与专项测试 |
| `wordpress-upload-plan.md` | 已实现功能与拟议排队/批量行为混排；改为 `wordpress.md` 的当前操作说明 |

## 维护约定

- 新增功能优先更新现有使用说明，避免再次出现多份“完成总结”和同义入口。
- 配置变更同步检查 `.env.example`、配置指南、实际读取点及相关测试。
- 新增历史记录须注明当次范围与限制，不使用“当前线上”长期指代固定 SHA 或测试统计。
- 删除文档前检查引用；保留涉及权限、证据、迁移、备份恢复的有用决策，不因过时而自动取消安全边界。
