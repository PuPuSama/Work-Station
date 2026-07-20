# ADR-0010：GSC、Semrush 等 SEO 数据源作为可选扩展

- 状态：Deferred
- 日期：2026-07-17
- 范围：外部 SEO API、Agent 工具注册、指标存储和未来子图

## 背景

未来可能接入 Google Search Console、Semrush 或其他 SEO 数据服务，用于关键词研究、内容表现诊断、竞品差距和文章更新建议。目前尚未确定最先解决的业务问题，也未确定公司可使用的数据账户、额度和部署方式。

如果现在把某个供应商直接写进资料研究图，后续容易把产品事实、内容证据和搜索表现指标混在一起，并让 LangGraph 与单一 API 紧耦合。

## 当前决定

1. GSC、Semrush 不作为第一版知识库与资料研究 Agent 的前置依赖。
2. 后端预留 `SeoDataProvider` 和 `ToolRegistry` 接口，LangGraph 节点不直接依赖供应商 SDK。
3. 外部 SEO 指标保存为 `SeoObservation`，必须包含 Organization、CustomerProject、供应商、指标维度、数据时间范围、采集时间和过期时间。
4. SEO Observation 与产品硬事实 KnowledgeChunk 分开存储；搜索表现或第三方关键词估算不能证明产品规格。
5. API Key 和 OAuth Token 只存放在服务器端密钥存储中，不能进入图状态、对话历史或前端日志。
6. 每个项目显式启用允许的 Provider 和能力；Agent 只能调用 ToolRegistry 返回的工具。
7. 每个 Provider 记录速率限制、调用额度、数据新鲜度、失败状态和审计事件。
8. 不构建一个统一“超级 Agent”。不同 SEO 目标使用独立、有限状态的 LangGraph 子图，共享工具和数据接口。

## 候选子图

### Keyword Opportunity Graph

结合项目搜索表现、关键词信号、客户产品分类和现有内容覆盖，形成可人工确认的选题候选。

### Content Refresh Graph

识别搜索表现下降、来源过期或事实变更的已发布内容，给出需要更新的章节和证据缺口。

### Competitor Gap Graph

比较客户与明确竞品的主题覆盖，输出候选内容缺口；第三方数据只能作为研究信号，具体产品事实仍需回到官方来源验证。

### Post-publication Review Graph

把文章任务、发布日期、后续查询表现和改版记录关联，用于判断写作与更新是否产生效果。

## 接口草案

```text
SeoDataProvider
  - capabilities(project)
  - connection_status(project)
  - query(project, request)
  - usage(project, period)

ToolRegistry
  - list_tools(user, project, graph_type)
  - authorize(tool, action)
  - invoke(tool, request)
  - audit(result)
```

## 为什么延后

- 尚未确定最优先的 SEO 业务目标。
- GSC 需要项目级站点授权，Semrush 涉及账户套餐和调用额度。
- 先完成可追溯知识库和资料研究图，才能判断外部 SEO 数据应该进入哪个决策点。
- 延后业务接入不妨碍现在建立 Provider 和 ToolRegistry 的边界。

## 触发重新决策的条件

满足以下任一条件后重新打开本 ADR：

- 已确定第一个要实现的 SEO 场景和验收指标。
- 已获得可用的 GSC Property 授权或 Semrush API 账户。
- 已有真实运营人员愿意试用并提供目标项目。
- 第一版资料研究 Agent 已稳定运行，能够复用权限、工具审计和 Graph Run 基础设施。
