# Agent 学习进度

> 这是跨会话恢复的唯一学习进度准源。每次学习结束都要更新本文件，并在可能时创建独立 Git 提交。聊天记忆不能替代本文件。

## 当前状态

- 日期：2026-07-20
- 阶段：第 1 周 / Day 1
- 当前实验：Lab 01 文档变成规范化文本
- 当前任务：`normalize_text()` 已通过测试，等待学习理解确认；不要提前实现后续 TODO
- 正式工作台接入：尚未开始
- 工作树说明：开始学习前已经存在未提交的文档和前端改动，学习代码不得覆盖或顺手提交这些改动

## 已完成

- [x] 冻结知识库、Basic RAG、LangGraph 和 LightRAG 的边界。
- [x] 确定学习实验与正式工程双轨推进。
- [x] 确定前两个向量实验使用玩具向量，之后接真实 Embedding API。
- [x] 确定真实 Embedding 使用自建 OpenAI 兼容网关和 `text-embedding-3-small`。
- [x] 确定第一条真实案例为 `www.qewitfastener.com / topic_006`。
- [x] 确定建立约 20 条产品分类与来源类型评测集。
- [x] 创建 Lab 01–04 的 TODO 和测试脚手架。
- [x] 创建 qewitfastener 五条起始评测模板。

## 当前作业

打开：

- `learning_labs/lab01_document_to_text/README.md`
- `learning_labs/lab01_document_to_text/starter.py`
- `learning_labs/lab01_document_to_text/test_starter.py`

只实现 `normalize_text()`，目标行为由 README 和测试共同定义。不要调用大模型，不使用第三方库，也不要查看或创建参考答案。

脚手架初始检查：

- Python 语法编译：通过。
- 评测集 JSONL：5 行均可解析。
- `git diff --check`：通过。
- `NormalizeTextTests`：4 个测试均因 TODO 的 `NotImplementedError` 失败，这是等待学习者实现的预期红灯。

只运行当前 TODO 的测试：

```powershell
cd D:\article\article-agent
backend\.venv\Scripts\python.exe -m unittest learning_labs.lab01_document_to_text.test_starter.NormalizeTextTests -v
```

## 本关通过条件

- `NormalizeTextTests` 全部通过。
- 能用自己的话解释为什么知识入库前要统一换行符和空行。
- 能说明“规范化”和“切块”为什么是两个不同步骤。
- Codex 完成代码审查后，才能进入 `extract_title()`。

## NEXT_STEP

学习者用自己的话解释文本规范化的作用，以及它与文本切块的区别；确认理解后再进入 `extract_title()`。

## 下次会话恢复指令

```text
继续 D:\article\article-agent 的 Agent 学习项目。先读取 AGENTS.md、docs/agent-learning-and-delivery-plan.md 和 docs/agent-learning-progress.md，运行 git status --short，只从 NEXT_STEP 继续，不重新设计已确认架构，不提前给参考答案。
```

## 学习记录

完成当前 TODO 后在这里填写：

- 测试结果：2026-07-20，`NormalizeTextTests` 4/4 通过；`starter.py` 语法编译通过。
- 遇到的问题：仓库未包含计划中记录的 `backend/.venv`，改用 Codex 工作区自带 Python 运行同一测试命令。
- 自己的理解：等待学习者用自己的话确认。
- Codex 评审结论：实现满足换行统一、逐行去除空格/Tab、空行折叠、首尾空行删除和非字符串拒绝；未修改 TODO 2/3，可以进入理解确认环节。
- 对应 Git 提交：未创建；学习目录和进度文档在开始前即为未跟踪状态，避免与既有工作树改动混合提交。
