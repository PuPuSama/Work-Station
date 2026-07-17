# ADR-0001：采用 WordPress 优先的混合知识采集方式

- 状态：Accepted
- 日期：2026-07-17
- 范围：客户官网资料发现、产品选择、知识库沉淀

## 背景

客户官网大多基于 WordPress。现有系统已经通过 Tavily、WordPress REST、Sitemap、产品分类页和 HTML 抓取发现产品，但产品页与 Blog 页仍可能因 URL 和页面结构相似而混淆。抓取结果当前主要保存在单篇任务目录，无法稳定跨文章复用。

系统同时需要：

- 从精确产品分类中选择真实产品详情页及图片。
- 允许官网 Blog 作为正文参考和公开引用来源。
- 把验证后的官网资料沉淀为客户级知识。
- 在资料不足时使用官网/Tavily 补充，但不编造硬事实。

## 决策

采用混合采集策略：

1. 项目创建后自动执行少量只读请求组成的 Site Probe，不立即全站抓取。
2. Site Probe 展示 CMS/WordPress 识别结果、REST 与 WooCommerce 能力、预计产品规模、可用 Sitemap 和抓取风险。
3. 用户确认后，才集中同步客户产品分类和产品详情；用户可以取消或暂缓。
4. 生成单篇文章时增量发现相关 Blog、Guide 和缺失资料。
5. WordPress REST 路由作为优先发现入口。
6. WooCommerce 站点优先使用公开 Store API 获取产品、分类、图片和 Canonical 产品链接。
7. 普通 WordPress `posts` 进入参考素材候选；`pages` 与自定义 post type 必须继续分类。
8. REST 被关闭、插件未暴露路由或站点不是 WooCommerce 时，回退到 Sitemap、分类页和 HTML 解析。
9. 产品选择与正文参考分为两个候选池：Blog 不得进入产品列表，产品分类页不得冒充详情页。
10. 自动产品数量允许少于三个。相关分类产品只能作为待确认候选。
11. 抓取结果先进入 Research Inbox，经分类、去重和版本化后再进入知识索引。
12. 同域名且强验证的产品详情页自动发布到硬事实库。
13. 同域名且明确识别的官网 Blog、Guide、News 和案例文章自动发布到参考素材库。
14. 普通 Page、自定义 post type、模糊页面和仅由模型判断的候选进入待审核区，不自动作为事实来源。
15. 所有自动发布动作保留来源快照、分类证据和发布原因，并支持撤销或回滚。

## Agent 决策

暂不使用 Agent 替换爬虫。

页面请求、网络安全、REST/HTML 解析、页面类型硬规则、Canonical 去重和文件持久化保持为确定性实现。后续可以在外层增加受限的资料研究 Agent，负责查询规划、证据充足度判断和检索回退。只有该子流程需要多轮重试、暂停和恢复时，才引入 LangGraph。

## 原因

- WordPress 和 WooCommerce 提供结构化资源类型，比基于 URL 关键词猜测页面类型更可靠。
- WooCommerce Store API 的公开产品端点无需 API Key，适合读取公开产品目录。
- `wp/v2/search` 的 `subtype` 可以帮助区分文章、页面和其他 post type。
- 确定性分类和安全校验更容易测试、复现和审计。
- 分离产品与 Blog 后，既能避免错误选品，又能保留有价值的官网文章引用。

## 影响

正向影响：

- 产品详情页识别准确率提高。
- 产品分类与正文话题更接近。
- 官网 Blog 可以进入参考知识层，而不污染产品资产。
- 已抓资料可跨文章复用，并保留抓取时间和版本。
- 高置信度来源无需逐条人工审核，模糊来源仍有明确的人工控制点。

成本与风险：

- 不同 WordPress 主题和插件会暴露不同路由，仍需 HTML 回退。
- 非 WooCommerce 的 B2B 站点可能使用自定义 post type，需要从 REST 路由索引动态发现。
- Site Probe 只能提供估算，完整同步仍可能遇到分页、限流或动态渲染问题。
- 自动发布依赖页面类型信号质量，需要持续用真实客户网站回归测试。
- 需要新增 Research Inbox、来源快照、知识切块和证据映射数据结构。
- 全站首次同步需要请求预算、分页和刷新策略。

## 官方参考

- WordPress REST API Reference: https://developer.wordpress.org/rest-api/reference/
- WordPress Search Results: https://developer.wordpress.org/rest-api/reference/search-results/
- WooCommerce Store API: https://developer.woocommerce.com/docs/apis/store-api/
- WooCommerce Products API: https://developer.woocommerce.com/docs/apis/store-api/resources-endpoints/products/
