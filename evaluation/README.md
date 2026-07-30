# Retrieval Evaluation

这里保存可提交、可复现的检索评测标注，不保存客户私有文件或 API Key。

正式 M6 数据集位于
`knowledge-agent/qewitfastener/retrieval-cases.jsonl`，格式和人工标注流程见同目录
`README.md`。

根目录的 `qewitfastener_product_retrieval.jsonl` 是早期学习阶段模板，保留用于追溯，
不再作为正式评测 Runner 的输入。

早期模板一行是一个话题，每行至少包含：

- `topic_id`：工作台任务编号。
- `topic_text`：原始英文话题，必须从工作台复制，不能凭印象改写。
- `expected_category_url`：人工确认的官网精确分类。
- `allowed_page_types`：可以成为候选产品或分类证据的页面类型。
- `forbidden_urls`：明确不能作为产品结果的 URL。
- `expected_product_urls`：有充分官网证据时再填写，可以为空。
- `label_evidence`：为什么这样标注。
- `label_status`：`todo`、`partial` 或 `confirmed`。

## Day 1 标注任务

1. 从当前工作台复制 `topic_006` 的精确话题文本，替换第一行的 `null`。
2. 在官网确认目标分类 URL 仍然有效。
3. 再选择 4 条覆盖不同难度的话题，替换四个 `seed_*` 占位行。
4. 不懂具体产品时不用猜；先标正确分类和明显错误的 Blog URL。

