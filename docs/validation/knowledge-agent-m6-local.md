# Knowledge Agent M6 本地验收记录

> 日期：2026-07-30
>
> 分支：`feature/knowledge-agent-m6`
>
> 外部 LightRAG / Embedding 调用：未执行

## 已验证

| 范围 | 结果 |
|---|---:|
| M6 定向单元 + PostgreSQL 集成 | 14 tests |
| 完整后端回归 | 412 tests，1 skipped |
| Alembic 重复 `upgrade head` | 通过，当前 `20260730_0007 (head)` |
| 前端 ESLint | 通过 |
| 前端 TypeScript `--noEmit` | 通过 |
| Next.js 16.2.10 webpack production build | 通过 |
| qewitfastener 正式评测集 | 20 条 |
| 已批准标注 | 1 条（`topic_006`） |
| 待人工标注 | 19 条 |

测试使用确定性 Fake Provider、`httpx.MockTransport` 和本地真实
PostgreSQL/pgvector，不访问真实 Embedding、LLM 或 LightRAG Server。

## 安全与隔离结果

- LightRAG HTTP 模拟返回外项目和无法映射的 file path 时被丢弃；
- LightRAG 候选包含项目 B、更高分旧快照时，项目 A 只返回当前已发布快照；
- LightRAG API 错误响应包含测试密钥时，公开异常没有密钥和响应正文；
- 非空 metadata filter 明确失败；
- JSONL 的字符串布尔值和错误页面类型不能被静默强制转换；
- pending 标注不进入正式指标；
- 报告不包含 Chunk 正文、Embedding 或连接凭据。

## 前端依赖说明

formal worktree 不提交 `node_modules`。验收期间临时使用指向稳定 main worktree
依赖目录的 Junction，并直接调用已安装的 ESLint、TypeScript 和 Next.js 二进制。
构建完成后 Junction 和 Next output-file-tracing 生成的
`frontend/article-agent` 目录均已删除。

## 未验证

- 真实 LightRAG Server 的索引与 `/query/data`；
- Basic Hybrid 与 LightRAG 的同集业务指标；
- LightRAG 索引 Token、增量更新时间和存储成本；
- qewitfastener 来源发布后的真实 Recall@5/MRR；
- 真实运行截图。

这些项目依赖人工标注、来源发布和独立 LightRAG Workspace，不能由模拟测试替代。
