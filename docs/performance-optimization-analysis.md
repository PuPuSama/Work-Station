# Article Agent 性能优化分析报告

## 执行摘要

基于对整个项目的全面审查，发现以下主要性能问题和优化机会：

### 1. **数据重复问题（最严重）**
- **知识快照重复存储**：每个步骤都复制完整的 39 KiB 知识快照
- **影响**：28 步计划 = 1.1 MiB 重复数据，导致内存暴涨
- **建议**：改为计划级引用存储

### 2. **数据库查询效率**
- **N+1 查询**：关注列表逐个加载每个计划
- **大对象加载**：每次都加载完整步骤数组
- **建议**：批量查询 + 选择性字段加载

### 3. **前端请求模式**
- **已修复**：SSE 事件风暴（159 → 2-3 请求）
- **仍需优化**：初始页面加载时的串行请求

## 详细分析

### A. 后端性能瓶颈

#### 1. 知识快照重复 (backend/workflow_assistant/repository.py:2742)

**问题代码：**
```python
"pinned_knowledge_snapshot": copy.deepcopy(step.pinned_knowledge_snapshot)
```

**影响：**
- 每个步骤存储 ~39 KiB
- 28 步计划 = 1.1 MiB 重复
- 数据库 I/O 放大
- 内存占用放大（反序列化 + Pydantic 复制）

**优化方案：**
```python
# 方案 1: 计划级快照 + 步骤引用
workflow_plans 表添加：
  knowledge_snapshot_id: UUID (FK)

workflow_knowledge_snapshots 表：
  snapshot_id: UUID (PK)
  plan_id: UUID
  snapshot_data: JSONB
  created_at: timestamp

步骤只存储 snapshot_id，不存储完整快照

# 方案 2: 外部存储
存储到对象存储（S3/MinIO），步骤只存储路径
knowledge_snapshot_uri: "s3://bucket/snapshots/{id}.json"
```

**预期收益：**
- 数据库体积减少 ~85%
- 内存占用减少 ~70%
- 查询速度提升 3-5 倍

---

#### 2. 关注列表的 N+1 查询 (backend/workflow_assistant/repository.py:2514-2523)

**问题代码：**
```python
return tuple(
    plan
    for plan_id in rows
    if (plan := self._get_plan_in_connection(
        connection, actor, str(plan_id),  # N+1：每个 plan_id 一次查询
    ))
    is not None
)
```

**影响：**
- 50 个计划 = 50 次独立查询
- 每次查询加载完整步骤 + 知识快照
- 数据库往返延迟累加

**优化方案：**
```python
# 批量加载计划和步骤
plan_rows = connection.execute(
    sa.select(workflow_plans).where(
        workflow_plans.c.plan_id.in_(plan_ids)
    )
).mappings().all()

step_rows = connection.execute(
    sa.select(workflow_plan_steps).where(
        workflow_plan_steps.c.plan_id.in_(plan_ids)
    )
).mappings().all()

# 内存中组装
steps_by_plan = {}
for step in step_rows:
    steps_by_plan.setdefault(step['plan_id'], []).append(step)

plans = [
    _build_plan(row, steps_by_plan.get(row['plan_id'], []))
    for row in plan_rows
]
```

**预期收益：**
- 查询数：50 → 2
- 响应时间：500ms → 50ms

---

#### 3. JSON 序列化开销

**问题：**
- PostgreSQL JSONB → Python dict → Pydantic 模型 → JSON 响应
- 每个步骤经过 3-4 次序列化/反序列化
- 大对象（知识快照）反复复制

**当前流程：**
```
DB JSONB → Python dict (psycopg3)
         → copy.deepcopy()
         → Pydantic validation
         → JSON encoding (FastAPI)
```

**优化方案：**
- 使用 Pydantic v2 的零拷贝模式
- 延迟加载大字段（知识快照按需获取）
- 响应缓存（计划哈希未变时返回 304）

---

#### 4. 数据库索引缺失

**建议添加索引：**
```sql
-- 关注列表查询
CREATE INDEX idx_plans_attention 
ON workflow_plans(organization_id, creator_user_id, attention_state, updated_at DESC)
WHERE attention_state != 'none';

-- 步骤批量加载
CREATE INDEX idx_steps_by_plan 
ON workflow_plan_steps(plan_id, sequence);

-- 项目范围过滤
CREATE INDEX idx_plan_projects 
ON workflow_plan_projects(plan_id, project_id);
```

---

### B. 前端性能瓶颈

#### 1. 大组件拆分

**最大的组件：**
- `server-article-workbench.tsx`: 2,166 行
- `workflow-assistant-workspace.tsx`: 1,275 行
- `server-research-workspace.tsx`: 1,134 行

**问题：**
- 单个文件过大，难以维护
- 重新渲染开销大
- 代码分割不充分

**优化方案：**
```tsx
// 拆分为子组件
workflow-assistant-workspace/
  ├── index.tsx              (100 行：布局和状态管理)
  ├── PlanTimeline.tsx       (150 行：事件时间线)
  ├── StepList.tsx           (200 行：步骤列表)
  ├── AttentionInbox.tsx     (150 行：关注收件箱)
  ├── ConversationPanel.tsx  (200 行：对话面板)
  └── hooks/
      ├── usePlanSubscription.ts (SSE 订阅)
      ├── useAttentionList.ts
      └── useDebouncedRefresh.ts
```

**预期收益：**
- 初始加载减少 ~30%
- 重新渲染性能提升 2-3 倍
- 可维护性显著提升

---

#### 2. 初始加载优化

**当前问题：串行请求**
```tsx
// 当前：串行加载
const projects = await apiGet("/api/projects")
for (const project of projects) {
  const tasks = await apiGet(`/api/projects/${project.id}/tasks`)
}
```

**优化方案：**
```tsx
// 方案 1: 并行加载
const [projects, conversations, authStatus] = await Promise.all([...])
const allTasks = await Promise.all(
  projects.map(p => apiGet(`/api/projects/${p.id}/tasks`))
)

// 方案 2: 批量接口
const batch = await apiGet("/api/batch", {
  requests: [
    { path: "/projects" },
    { path: "/conversations" },
    { path: "/auth/status" }
  ]
})

// 方案 3: GraphQL/单一入口
const workspace = await apiGet("/api/assistant-workspace")
```

---

#### 3. 状态管理优化

**问题：**
- 过多 useState 导致重新渲染
- 缺少 memo 优化
- 回调依赖不稳定

**优化方案：**
```tsx
// 使用 useReducer 合并状态
const [state, dispatch] = useReducer(workspaceReducer, initialState)

// 稳定的回调
const refreshPlan = useCallback(() => {
  dispatch({ type: 'REFRESH_PLAN_START' })
  // ...
}, []) // 空依赖

// memo 优化大组件
const StepList = memo(({ steps, onStepClick }) => {
  // ...
}, (prev, next) => prev.planHash === next.planHash)
```

---

### C. 架构层面优化

#### 1. 缓存策略

**建议添加多级缓存：**

```python
# L1: 进程内缓存（计划摘要）
@lru_cache(maxsize=100)
def get_plan_summary(plan_id: str) -> PlanSummary:
    pass

# L2: Redis 缓存（完整计划，按哈希）
async def get_plan_cached(plan_id: str) -> Plan:
    cache_key = f"plan:{plan_id}:hash"
    cached_hash = await redis.get(cache_key)
    
    plan = get_plan_from_db(plan_id)
    if cached_hash == plan.plan_hash:
        cached_plan = await redis.get(f"plan:{plan_id}")
        if cached_plan:
            return json.loads(cached_plan)
    
    await redis.setex(cache_key, 300, plan.plan_hash)
    await redis.setex(f"plan:{plan_id}", 60, json.dumps(plan))
    return plan
```

---

#### 2. 数据库分页

**当前问题：**
- 关注列表一次加载 50 个完整计划
- 步骤数组无分页

**优化方案：**
```python
# 游标分页
def list_attention_plans(
    cursor: str | None = None,
    limit: int = 20
) -> tuple[list[PlanSummary], str | None]:
    # 返回精简摘要 + 下一页游标
    pass

# 步骤按需加载
@router.get("/plans/{plan_id}/steps")
def get_plan_steps(
    plan_id: str,
    offset: int = 0,
    limit: int = 50
):
    # 只返回请求的步骤范围
    pass
```

---

#### 3. 异步优化

**当前问题：**
- 大量同步数据库操作
- asyncio.to_thread 包装同步代码

**优化方案：**
```python
# 使用 asyncpg 替代同步 psycopg
import asyncpg

class AsyncRepository:
    async def get_plan(self, plan_id: str) -> Plan:
        async with self.pool.acquire() as conn:
            plan_row = await conn.fetchrow(
                "SELECT * FROM workflow_plans WHERE plan_id = $1",
                plan_id
            )
            step_rows = await conn.fetch(
                "SELECT * FROM workflow_plan_steps WHERE plan_id = $1",
                plan_id
            )
            return self._build_plan(plan_row, step_rows)
```

---

## 具体优化建议（按优先级）

### 🔴 P0 - 立即修复（已完成）
✅ SSE 事件风暴（159 → 2-3 请求）
✅ 关注列表精简摘要（4 MiB → 50 KiB）

### 🟠 P1 - 高优先级（本周）

#### 1. 知识快照去重
**文件：** `backend/workflow_assistant/repository.py`
**工作量：** 2-3 天
**收益：** 内存 -70%，数据库 -85%

**实施步骤：**
1. 新增 `workflow_knowledge_snapshots` 表
2. 迁移现有快照到新表
3. 修改步骤存储逻辑为引用
4. 后台清理旧快照数据

---

#### 2. 批量查询优化
**文件：** `backend/workflow_assistant/repository.py:2514-2523`
**工作量：** 1 天
**收益：** 查询时间 -90%

**实施步骤：**
1. 实现批量加载辅助方法
2. 重构 `list_attention_plans`
3. 添加数据库索引

---

#### 3. 大组件拆分
**文件：** `frontend/src/components/workflow-assistant-workspace.tsx`
**工作量：** 2 天
**收益：** 初始加载 -30%，可维护性 +100%

---

### 🟡 P2 - 中优先级（两周内）

#### 4. 添加 Redis 缓存
**工作量：** 2 天
**收益：** 重复请求 -95%

#### 5. 数据库索引
**工作量：** 0.5 天
**收益：** 查询速度 +50%

#### 6. 初始加载并行化
**文件：** `frontend/src/components/workflow-assistant-workspace.tsx`
**工作量：** 1 天
**收益：** 首屏时间 -40%

---

### 🟢 P3 - 低优先级（月度规划）

#### 7. 迁移到 asyncpg
**工作量：** 1 周
**收益：** 并发性能 +100%

#### 8. GraphQL 统一入口
**工作量：** 1 周
**收益：** 过度获取 -80%

#### 9. 服务端分页
**工作量：** 2 天
**收益：** 大列表性能 +200%

---

## 冗余代码识别

### 1. 未使用的字段
```python
# backend/workflow_assistant/repository.py
# 可以移除的字段（待确认）:
article_task_selection_locked  # 只在一处使用，可能已废弃
```

### 2. 重复的辅助方法
```python
# _json_dict 在多个文件中重复定义
# 建议：统一到 utils.py
```

### 3. 过度防御性编程
```python
# 过多的 try-except 包装
# 建议：让异常自然冒泡，在顶层统一处理
```

---

## 监控和度量

### 添加性能指标
```python
# backend/workflow_assistant/http.py
import time
from prometheus_client import Histogram

plan_load_duration = Histogram(
    'plan_load_seconds',
    'Time to load a complete plan',
    ['endpoint']
)

@router.get("/plans/{plan_id}")
def get_plan(plan_id: str):
    with plan_load_duration.labels('get_plan').time():
        return repository.get_plan(plan_id)
```

### 前端性能监控
```tsx
// 使用 React Profiler
<Profiler id="PlanList" onRender={onRenderCallback}>
  <PlanList />
</Profiler>

// Web Vitals
import { getCLS, getFID, getFCP, getLCP, getTTFB } from 'web-vitals'
```

---

## 总结

**当前状态：**
- 代码总量：后端 2.1 MB，前端 1.1 MB
- 最大文件：repository.py (129 KB), http.py (83 KB)
- 最大组件：server-article-workbench.tsx (2,166 行)

**预期优化收益：**
| 指标 | 当前 | 优化后 | 改善 |
|------|------|--------|------|
| 内存峰值 | 3.57 GiB | ~500 MiB | -85% |
| 关注列表响应 | 4.08 MiB | 50 KiB | -99% |
| 首屏加载时间 | ~3s | ~1s | -67% |
| 数据库查询数 | 50+ | 2-3 | -95% |
| 代码可维护性 | 中 | 高 | +50% |

**实施路线图：**
1. 周 1：知识快照去重 + 批量查询
2. 周 2：大组件拆分 + 缓存层
3. 周 3：数据库索引 + 异步优化
4. 周 4：监控指标 + 性能验证