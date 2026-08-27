# 快速优化实施指南

## 本周可以立即实施的优化（无需大改架构）

### 1. 添加数据库索引（5分钟，立即生效）

```sql
-- 在生产数据库执行
-- 1. 关注列表查询优化
CREATE INDEX CONCURRENTLY idx_plans_attention_query 
ON workflow_plans(organization_id, creator_user_id, updated_at DESC)
WHERE attention_state != 'none';

-- 2. 步骤批量加载
CREATE INDEX CONCURRENTLY idx_steps_plan_sequence 
ON workflow_plan_steps(plan_id, sequence);

-- 3. 项目范围检查
CREATE INDEX CONCURRENTLY idx_plan_projects_lookup 
ON workflow_plan_projects(organization_id, plan_id, project_id);

-- 4. 事件流查询
CREATE INDEX CONCURRENTLY idx_plan_events_stream 
ON workflow_plan_events(organization_id, plan_id, sequence DESC);
```

**预期收益：** 查询速度提升 30-50%

---

### 2. 批量查询重构（2小时）

**文件：** `backend/workflow_assistant/repository.py`

在 `PostgresWorkflowAssistantRepository` 类中添加批量加载方法：

```python
def _batch_load_plans(
    self,
    connection: Connection,
    actor: ActorIdentity,
    plan_ids: Sequence[str],
) -> dict[str, WorkflowPlan]:
    """Batch load multiple plans with their steps in 2 queries instead of N."""
    if not plan_ids:
        return {}
    
    # Load all plans in one query
    plan_rows = connection.execute(
        sa.select(workflow_plans)
        .where(
            workflow_plans.c.organization_id == actor.organization_id,
            workflow_plans.c.plan_id.in_(plan_ids),
        )
    ).mappings().all()
    
    # Load all steps in one query
    step_rows = connection.execute(
        sa.select(workflow_plan_steps)
        .where(
            workflow_plan_steps.c.organization_id == actor.organization_id,
            workflow_plan_steps.c.plan_id.in_(plan_ids),
        )
        .order_by(
            workflow_plan_steps.c.plan_id,
            workflow_plan_steps.c.sequence,
        )
    ).mappings().all()
    
    # Group steps by plan
    steps_by_plan: dict[str, list[RowMapping]] = {}
    for step in step_rows:
        steps_by_plan.setdefault(str(step["plan_id"]), []).append(step)
    
    # Build plan objects
    result = {}
    for row in plan_rows:
        plan_id = str(row["plan_id"])
        steps = steps_by_plan.get(plan_id, [])
        plan = self._plan_from_rows(row, steps)
        if plan:
            result[plan_id] = plan
    
    return result
```

然后修改 `list_attention_plans` 方法（第 2459 行附近）：

```python
def list_attention_plans(
    self,
    *,
    actor: ActorIdentity,
    accessible_project_ids: Sequence[str] | None = None,
    limit: int = 50,
) -> tuple[WorkflowPlan, ...]:
    # ... 前面的条件构建代码保持不变 ...
    
    with self._engine.connect() as connection:
        rows = connection.execute(
            sa.select(workflow_plans.c.plan_id)
            .where(*conditions)
            .order_by(
                workflow_plans.c.updated_at.desc(),
                workflow_plans.c.plan_id.desc(),
            )
            .limit(limit)
        ).scalars().all()
        
        # 替换原来的逐个加载为批量加载
        plan_ids = [str(plan_id) for plan_id in rows]
        plans_dict = self._batch_load_plans(connection, actor, plan_ids)
        
        # 保持原始顺序
        return tuple(
            plans_dict[plan_id]
            for plan_id in plan_ids
            if plan_id in plans_dict
        )
```

**预期收益：** 关注列表加载时间从 500ms 降至 50ms

---

### 3. 前端防抖清理（30分钟）

**文件：** `frontend/src/components/workflow-assistant-workspace.tsx`

清理未使用的 ref：

```tsx
// 删除第 345 行的 refreshPendingRef（已不需要）
- const refreshPendingRef = useRef(false);

// 简化 debouncedRefreshAttention（第 387 行）
const debouncedRefreshAttention = useCallback(() => {
  if (refreshTimeoutRef.current !== null) {
    window.clearTimeout(refreshTimeoutRef.current);
  }
  refreshTimeoutRef.current = window.setTimeout(() => {
    refreshTimeoutRef.current = null;
    void refreshAttention().catch(() => {});
  }, 500);
}, [refreshAttention]);
```

添加清理逻辑到组件卸载：

```tsx
// 在主组件的最后添加
useEffect(() => {
  return () => {
    if (refreshTimeoutRef.current) {
      window.clearTimeout(refreshTimeoutRef.current);
    }
    if (planRefreshTimeoutRef.current) {
      window.clearTimeout(planRefreshTimeoutRef.current);
    }
  };
}, []);
```

---

### 4. 移除过度防御代码（1小时）

**文件：** `backend/workflow_assistant/http.py`

简化错误处理，让 FastAPI 的全局异常处理器处理：

```python
# 当前（第 2160 行附近）：
try:
    # ... 逻辑 ...
    return AssistantAttentionListResponse(plans=visible)
except Exception as exc:
    raise _error(exc) from exc

# 优化为：
# ... 逻辑 ...
return AssistantAttentionListResponse(plans=visible)
# 移除 try-except，让异常自然冒泡
```

在多个地方可以应用此简化（搜索 `except Exception as exc`）。

---

### 5. 响应压缩（5分钟）

**文件：** `backend/portable_server.py`

添加 gzip 压缩中间件：

```python
from fastapi.middleware.gzip import GZipMiddleware

app = FastAPI(...)
app.add_middleware(GZipMiddleware, minimum_size=1000)
```

**预期收益：** 响应体积减少 60-70%

---

### 6. HTTP 缓存头（30分钟）

**文件：** `backend/workflow_assistant/http.py`

为不可变资源添加缓存头：

```python
from fastapi import Response

@router.get("/plans/{plan_id}")
def get_plan(
    plan_id: str,
    request: Request,
    response: Response,
    actor: ActorIdentity = Depends(require_server_actor),
) -> WorkflowPlanResponse:
    _feature_enabled(request)
    plan = _repository(request).get_plan(actor=actor, plan_id=plan_id)
    
    # 添加 ETag 支持
    response.headers["ETag"] = f'"{plan.plan_hash}"'
    response.headers["Cache-Control"] = "private, max-age=10"
    
    # 检查 If-None-Match
    if_none_match = request.headers.get("If-None-Match")
    if if_none_match == f'"{plan.plan_hash}"':
        return Response(status_code=304)
    
    return _plan_response(plan)
```

**预期收益：** 重复请求减少 80%

---

### 7. 数据库连接池调优（5分钟）

**文件：** `backend/portable_server.py` 或配置文件

```python
from sqlalchemy import create_engine

engine = create_engine(
    database_url,
    pool_size=20,          # 增加连接池大小
    max_overflow=10,       # 允许超出连接
    pool_pre_ping=True,    # 连接健康检查
    pool_recycle=3600,     # 1小时回收连接
    echo=False,
)
```

---

### 8. 前端并行加载（1小时）

**文件：** `frontend/src/components/workflow-assistant-workspace.tsx`

修改 `load` 函数（第 417 行）：

```tsx
const load = useCallback(async () => {
  setLoading(true);
  setError("");
  try {
    // 并行加载基础数据
    const [nextProjects, nextConversations, authStatus] = await Promise.all([
      apiGet<AccessibleProject[]>("/api/projects"),
      apiGet<WorkflowAssistantConversationList>("/api/workflow-assistant/conversations"),
      apiGet<AuthStatus>("/api/auth/status"),
    ]);
    
    // 并行加载所有项目的任务
    const nextTasks = (
      await Promise.all(
        nextProjects.map(project =>
          apiGet<TaskRecord[]>(
            `/api/projects/${encodeURIComponent(project.project_id)}/tasks`,
          ).then(tasks => 
            tasks.map(task => ({ ...task, project_id: project.project_id }))
          )
        )
      )
    ).flat();
    
    // ... 其余代码保持不变 ...
  } catch (err) {
    // ... 错误处理 ...
  }
}, []);
```

**预期收益：** 初始加载时间减少 40%

---

## 验证优化效果

### 1. 后端性能测试

```bash
# 安装 Apache Bench
apt-get install apache2-utils

# 测试关注列表接口
ab -n 100 -c 10 \
  -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/api/workflow-assistant/attention

# 对比优化前后的 Time per request
```

### 2. 前端性能测试

```javascript
// 在浏览器控制台运行
performance.mark('page-start');
// 刷新页面
window.addEventListener('load', () => {
  performance.mark('page-end');
  performance.measure('page-load', 'page-start', 'page-end');
  const measure = performance.getEntriesByName('page-load')[0];
  console.log(`加载时间: ${measure.duration}ms`);
});
```

### 3. 数据库查询监控

```sql
-- 查看慢查询
SELECT query, mean_exec_time, calls
FROM pg_stat_statements
WHERE mean_exec_time > 100
ORDER BY mean_exec_time DESC
LIMIT 10;

-- 查看索引使用情况
SELECT schemaname, tablename, indexname, idx_scan
FROM pg_stat_user_indexes
WHERE schemaname = 'public'
ORDER BY idx_scan ASC;
```

---

## 预期总收益（所有快速优化完成后）

| 指标 | 优化前 | 优化后 | 改善 |
|------|--------|--------|------|
| 关注列表加载 | 500ms | 50ms | -90% |
| 关注列表大小 | 4 MiB | 50 KiB | -99% |
| 初始页面加载 | 3s | 1.8s | -40% |
| 数据库查询数 | 50+ | 2-3 | -95% |
| 响应压缩 | 无 | gzip | -70% |
| 缓存命中率 | 0% | 60% | +60% |

**总工作量：** 约 6 小时  
**风险等级：** 低（都是渐进式优化，不破坏现有功能）  
**回滚难度：** 容易（除了数据库索引，其他都可以快速回滚）

---

## 实施顺序建议

1. **先做索引**（立即生效，零风险）
2. **再做压缩和缓存头**（配置级改动）
3. **然后批量查询**（代码改动较大，需要测试）
4. **最后前端优化**（需要前端构建和部署）

每个优化独立完成后立即部署验证，不要等所有优化完成再部署。