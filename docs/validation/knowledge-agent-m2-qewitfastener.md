# M2 真实垂直切片：qewitfastener / topic_006

> 验收日期：2026-07-30
>
> 项目：`qewitfastener.com`
>
> 目标分类：[Woodscrews & Dry Wall Screws](https://www.qewitfastener.com/category/fasteners/screws/woodscrews-dry-wall-screws/)

## 验收边界

本次只验收 M2 的真实资料入库，不提前声称 M3 检索已完成：

- WordPress Site Probe；
- 产品分类页与详情页分类；
- Blog 正确拒绝为产品；
- 原始 HTML、规范化产物、Chunk、产品身份和原图证据入库；
- 所有对象保持 Inbox，产品保持未确认；
- 相同页面再次同步复用同一快照和内容哈希。

`topic_006` 的精确话题文本在评测集里仍为空；M2 使用已经确认的目标分类 URL
证明资料已准备好。按话题执行混合检索属于 M3。

## 执行命令

在 `backend` 目录设置本地数据库和 Artifact Root 后执行：

```powershell
.\.venv\Scripts\python.exe -m knowledge_agent.m2_site_smoke `
  --project qewitfastener.com `
  --site-url https://www.qewitfastener.com `
  --category-url https://www.qewitfastener.com/category/fasteners/screws/woodscrews-dry-wall-screws/ `
  --max-products 3
```

命令只输出项目、类型、数量和 ID，不输出客户正文或密钥。

## 结果

| 检查项 | 结果 |
|---|---:|
| WordPress REST | 已识别 |
| 分类页类型 | `product_category` |
| 分类页 Chunk | 36 |
| 产品详情页 | 3 |
| 稳定产品身份 | 3 |
| 去重原图 | 19 |
| 跳过候选 | 0 |
| 下载/解析警告 | 0 |
| 原始 HTML 快照 | 4 |
| 已发布来源 | 0 |
| 已确认产品 | 0 |

真实产品 ID：

- `product_640-2_2334d15e`
- `product_black-gray-phosphate-collated-drywall-screw-for-_1fa68d82`
- `product_carbon-steel-hexagon-head-coach-screws-din-571_df3fec40`

真实页面没有 Schema.org Product，也不是 WooCommerce 模板。第一次只读验收因此把
详情页判成普通知识页。修正后的规则保留强信号优先级，并复用现有经过测试的保守
B2B 产品页判定器作为回退：详情页必须同时具备产品相关身份、实质正文和可信图片，
而分类页和 Blog 仍优先排除。

## Blog 正确拒绝

真实 Blog：

[Common Washer Defects and How Rigorous Manufacturing Quality Control Prevents Them](https://www.qewitfastener.com/blog/common-washer-defects-and-how-rigorous-manufacturing-quality-control-prevents-them/)

只读分类结果：

| 字段 | 结果 |
|---|---|
| 页面类型 | `official_blog` |
| 置信度 | `0.72` |
| 文本块 | 69 |
| 产品投影 | `false` |

因此 Blog 可以作为后续写作参考来源，但没有创建 `KnowledgeProduct` 或
`primary_detail` 证据。

## 仍待 M3 验收

- 从工作台补齐 `topic_006` 精确话题文本；
- 发布人工确认后的来源；
- 用同一 Embedding 模型执行项目级检索；
- 证明分类页/产品进入候选，而 Blog 不成为产品结果；
- 记录 Recall@5、MRR 和正确拒绝率。
