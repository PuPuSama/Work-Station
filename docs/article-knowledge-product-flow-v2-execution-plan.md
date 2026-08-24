# Article Agent 知识库与产品推荐流程 V2 执行方案

## 目标

把“标题/产品选择 → 大纲 → 证据包 → 正文 → 知识库引用率”改成同一条可追踪链路：先确定文章要回答的采购问题，再按 H2/H3 和产品角色检索事实，最后把证据定向交给正文生成。

核心结果：

- 产品推荐不再只看标题，而是结合采购意图、文章大纲、产品角色和可用产品证据。
- 事实检索不再只有一次全局向量 Top-K，而是使用 Article Brief、Claim Requirements、混合检索、重排和证据充分性判断。
- 正文生成拿到 Section Evidence Map，优先使用对应 H2/H3 或产品 scope 的片段。
- 知识库引用率按句子/段落复检，但博客只允许作为正文写作参考，不能进入 Evidence Pack、Hard Fact 或证据链。
- 所有计划、证据包、任务和队列身份仍以 PostgreSQL 为准；旧 Plan 只在新大纲确认后被替换，不静默覆盖活动版本。

## 已完成的 M0–M5

### M0：边界和基线

- Server-only；不恢复 SQLite、Local fallback、旧无项目作用域 API 或双写。
- 非博客已发布当前快照才能作证。
- 官方博客只能作为正文引用资料，不进入 Evidence Pack、Hard Fact 或句子证据链。
- ZeroGPT 仍由人工操作；不把 AI 率检测混入知识检索。
- 产品最多三个，图片最多三张；产品 ID 是人工确认的权威身份。

### M1：共享 Article Brief

在产品推荐、大纲、证据计划和正文之间共享一个按任务快照计算的 Brief，包含：

- 文章意图和目标采购人；
- 采购痛点、选型维度和所需能力；
- 推荐产品角色；
- 可用事实及其 Chunk ID；
- 缺失证据提示；
- 输入 hash、标题 hash、知识快照指纹和 Brief ID。

Brief 不把来源叙事带入成文；官网、产品页和详情页中的已确认信息直接作为事实陈述。

### M2：产品推荐 V2

推荐输入由“标题”升级为：Article Brief + 任务资料 + 当前项目已确认产品 + 当前发布知识。

模型只能输出严格 JSON：`product_id`、`reason`，以及可选的 `article_role`、`suggested_section`。服务端再次校验产品 ID、官网域名和证据资产，并保存推荐理由、产品角色、建议位置和证据状态。

推荐结果只作为候选；产品确认仍由人工完成。正文必须使用人工确认的产品，不使用未确认候选。

### M3：Claim Requirements

从确认的大纲解析 H2/H3，给每个 H3 建立要求：

- requirement ID、H2/H3 标题；
- `hard_fact`、`selection_logic`、`application`、`reference` 等 claim type；
- 查询变体；
- 是否要求硬事实；
- 最小支撑数量；
- 需要覆盖的产品 ID。

产品 scope 使用产品 ID 过滤；只有历史数据没有产品 ID 时才退回 canonical URL。

### M4：混合检索和证据充分性

检索顺序：

1. 项目、已发布状态、当前快照、允许作证 source kind 过滤；
2. 向量/全文混合召回；
3. 使用查询变体、heading、短语和 token overlap 做轻量重排；
4. 按 claim requirement 判断是否缺少支撑或硬事实；
5. 对不足项生成 gap reason，而不是用相邻片段冒充满足。

重排只在已通过权限、项目和快照过滤的候选中工作，不扩大数据边界，也不把博客提升为证据。

### M5：Section Evidence Map

研究完成后由服务端根据不可变 Evidence Pack 计算路由：

- `global_context`：用于兼容和全局背景的有界片段集合；
- `sections`：每个 H2 scope 对应的 Chunk、H3 标题和 requirement ID；
- `product_facts`：产品 ID 到产品事实 Chunk 的映射；
- warnings：缺片段或产品 scope 缺 ID 的诊断。

队列保存这份私有 map。Worker 执行时重新从 PostgreSQL 计算并做 CAS/身份校验，浏览器不能修改证据归属。正文提示词要求优先使用对应 section/product 的片段，global context 只作 fallback，且不输出 Chunk ID、scope 或检索过程。

## M6：精准缺口补检

目标是只补缺失支撑，不重复全量扫描。

1. 复检正文句子，区分普通支撑缺失、硬事实缺失、产品事实缺失和选型逻辑缺失。
2. 将缺失句绑定到 Article Brief、H2/H3 requirement、产品 ID 和当前证据快照。
3. 由服务端生成少量定向 query variant；优先查当前已发布知识，只有确实缺失时才进入官网同域候选发现。
4. 只重跑受影响的 scope/requirement，生成新的候选 Evidence Pack；不修改旧 Pack，不自动改正文。
5. 前端展示“缺口句 → 查询 → 新证据候选 → 支撑前后差异”，用户确认后才发布新快照/重新生成证据。
6. 新证据完成后重建 Section Evidence Map，再由用户决定是否重新生成正文或只更新引用率。

禁止：自动把低相似度片段标成已支撑、跨项目借证、把博客当硬事实、未经确认覆盖正文或旧 Evidence Pack。

## M7：前端、指标和灰度

前端按阶段展示：

- Brief 摘要和缺失证据；
- 产品推荐理由、角色和证据状态；
- H2/H3 Claim Requirements；
- 每个 scope 的 Evidence Pack 充分性；
- Section Evidence Map 和产品事实映射；
- 句子级知识库引用率、硬事实引用率和 unsupported examples；
- 精准补检按钮及人工确认差异。

先在一个项目、一个主题和一个产品推荐任务上灰度，保留旧流程回退读取能力；确认数据、提示词、实际导出和引用率没有回归后，再合并到 main。

## 验收标准

- 同一任务的 Brief、产品推荐、大纲、证据计划、Evidence Pack 和正文使用同一标题/大纲/知识快照身份。
- 至少两个产品被确认时，产品 scope 和正文锚点不会只保留第一个产品。
- H2/H3 缺硬事实时，Evidence Pack 明确显示 gap，不以博客或普通描述补齐。
- 正文生成不会暴露“知识库、客户文档、产品页提供了……”等元叙事，也不会修改 `img` 开头的索引块。
- 文章重写或大纲变化后，旧研究结果不能被误用于新正文；用户可以用新大纲生成新 Plan 和新 Evidence Pack。
- 多人并发时以 PostgreSQL project scope、队列、revision/CAS 和重新授权为准。
- 合并前必须通过：

```powershell
backend\.venv\Scripts\python.exe -m unittest discover -s backend\tests -q
cd frontend
npm.cmd run lint
npm.cmd run build
cd ..
git diff --check
```

## 当前分支状态

- 当前分支：`codex/knowledge-product-flow-v2`
- 已实现：M0、M1、M2、M3、M4、M5、M6，以及 M7 的 Brief/产品/Claim Requirements/Section Evidence Map/句子覆盖率展示。
- M6 的定向补检会从知识库覆盖详情页选择最多 12 个缺口句，服务端把缺口绑定到 H3 requirement、产品 ID、Article Brief 和知识快照身份，生成定向 query variants，创建新的 repair Plan，仅重跑受影响 scope，并复用未受影响的当前 Evidence Pack；旧 Plan 和旧 Pack 保持不可变。
- repair Plan 会保留缺口句、查询、执行中的 gap-fill attempt 和待人工确认候选，覆盖详情页可查看补检前后支撑率差异；候选仍需在资料研究工作区人工确认。
- M7 的灰度仍需在真实服务端用一个项目、一个主题和一个产品推荐任务验证；本分支没有执行生产部署，也没有改变 main。
- 当前不合并、不推送、不部署；待用户确认后再处理。
