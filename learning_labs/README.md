# Learning Labs

这里是知识库、RAG 和 Agent 的学习区，不是正式业务代码。实验顺序和正式交付的对应关系见 `docs/agent-learning-and-delivery-plan.md`。

## 规则

1. 一次只做 `docs/agent-learning-progress.md` 指定的一个 TODO。
2. 先写代码、运行测试、接受审查，再看参考实现。
3. 不读取 `.env`，不复制真实 API Key，不修改 `data/` 或客户任务。
4. Lab 01–04 只使用 Python 标准库。
5. `learning_labs/` 不进入正式便携版和客户交付包。

## 当前目录

```text
lab01_document_to_text/     文档规范化
lab02_text_chunking/        标题层级、段落与切块
lab03_vector_similarity/    玩具向量与余弦相似度
lab04_top_k_retrieval/      Top-K 和元数据过滤
```

每个实验都有 README、TODO 代码、测试、学习笔记和口头检查题。`reference_solution.py` 只在作业评审后提供。

