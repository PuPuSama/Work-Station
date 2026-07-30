# qewitfastener 检索评测集

`retrieval-cases.jsonl` 是正式 M6 评测输入。它只保存工作台话题、来源身份和标注状态，
不复制客户正文、Embedding、API Key 或数据库凭据。

## 当前状态

- 20 条真实工作台话题；
- `topic_006` 使用用户已确认的
  `Woodscrews & Dry Wall Screws` 分类来源，状态为 `approved`；
- 其余 19 条保持 `pending`，不能进入正式指标；
- 当前 qewitfastener 四个真实来源仍是 `inbox`，所以在人工发布和生成 Embedding 前，
  不应声称 Basic Hybrid 已取得真实 Recall 或 MRR。

查看标注准备度，不连接数据库或网关：

```powershell
Set-Location backend
.\.venv\Scripts\python.exe -m knowledge_agent.evaluation_runner `
  --cases ..\evaluation\knowledge-agent\qewitfastener\retrieval-cases.jsonl `
  --inspect-only
```

## 标注规则

1. `case_id` 必须对应稳定的话题编号，`query` 必须保留工作台原文。
2. `expected_source_ids` 使用 PostgreSQL 中稳定的项目内来源 ID，不使用临时 Chunk ID。
3. `allowed_source_kinds` 表示该检索任务允许返回的页面类型。
4. `forbidden_canonical_urls` 用于记录明确错误的产品来源，例如被误当产品的 Blog。
5. 官网资料不足时应设置 `expects_refusal=true`，不得为了让指标更好而猜来源。
6. 只有人工检查过标准来源和拒绝边界后，才能把
   `annotation_status` 从 `pending` 改为 `approved`。

## 运行 Basic Hybrid 基线

先人工发布已批准来源，并保证其 Chunk 使用当前 `EMBEDDING_MODEL` 完成 Embedding。
配置专用数据库和 Embedding 变量后，显式指定报告路径：

```powershell
Set-Location backend
.\.venv\Scripts\python.exe -m knowledge_agent.evaluation_runner `
  --cases ..\evaluation\knowledge-agent\qewitfastener\retrieval-cases.jsonl `
  --output ..\artifacts\evaluation\qewitfastener-basic-hybrid.json `
  --k 5
```

命令输出只包含 Retriever、条数、K 和报告路径。报告包含来源/Chunk ID、分数和指标，
不包含 Chunk 正文或密钥。
