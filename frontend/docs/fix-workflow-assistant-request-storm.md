# 工作流助手页面请求风暴修复

## 问题诊断

根据服务器诊断结果，工作流助手页面在打开时会产生请求风暴，导致内存暴涨和服务卡死。

### 根本原因

1. **SSE 历史回放触发大量请求**
   - 页面打开后，SSE 一次回放了 53 条历史事件
   - 前端对每条事件都立即执行 3 次请求：
     - 刷新完整计划
     - 刷新关注数量
     - 刷新完整关注列表
   - 瞬间产生约 53 × 3 = 159 个请求

2. **响应体过大**
   - 单个计划响应约 673 KiB
   - 关注列表响应约 4.08 MiB
   - 11 个计划的数据库内容合计约 9.3 MiB
   - 最大单个计划约 2.78 MiB

3. **知识快照重复存储**
   - 同一份约 39 KiB 知识快照，被重复保存在 28 个步骤中，累计约 1.1 MiB
   - 每次请求经过 PostgreSQL JSONB 解码、Python 对象构建、Pydantic 复制及 JSON 编码
   - 几十个请求并发时，内存被放大到约 3.57 GiB

### 实际影响

- Nginx 日志记录到 167 个工作流请求，其中 138 个返回 500
- 2 GiB 容器内存限制被突破，导致后端崩溃
- 工作流助手页面下载交付包项目时直接卡死

## 修复方案

### 1. 后端：关注列表返回精简摘要

**修改文件**: `backend/workflow_assistant/contracts.py`, `backend/workflow_assistant/http.py`

创建新的 `WorkflowPlanSummary` 模型，只包含摘要信息，不包含完整步骤和知识快照：

```python
class WorkflowPlanSummary(BaseModel):
    """Lightweight plan summary for attention lists, without full steps and snapshots."""

    plan_id: str
    conversation_id: str
    title: str
    natural_language_request: str
    plan_hash: str
    revision: int
    status: PlanStatus
    project_ids: list[str]
    paused_project_ids: list[str] = Field(default_factory=list)
    step_count: int = Field(ge=0)
    pending_step_count: int = Field(ge=0)
    concurrency_limit: int
    budget_warning: bool
    attention_state: str
    approved_by: str | None = None
    approved_at: str | None = None
```

修改关注列表接口使用 `_plan_summary()` 而非 `_plan_response()`。

**效果**:
- 关注列表响应大小从 4.08 MiB 降至约 50-100 KiB
- 避免传输重复的知识快照数据

### 2. 前端：批量处理 SSE 事件

**修改文件**: `frontend/src/components/workflow-assistant-workspace.tsx`

#### 2.1 添加防抖机制

```typescript
const debouncedRefreshAttention = useCallback(() => {
  refreshPendingRef.current = true;

  if (refreshTimeoutRef.current !== null) {
    window.clearTimeout(refreshTimeoutRef.current);
  }

  // 500ms 后才刷新
  refreshTimeoutRef.current = window.setTimeout(() => {
    refreshTimeoutRef.current = null;
    refreshPendingRef.current = false;
    void refreshAttention().catch(() => {});
  }, 500);
}, [refreshAttention]);
```

#### 2.2 批量处理事件

修改 SSE 事件处理逻辑：

```typescript
let eventBatchTimeout: number | null = null;
let batchedEventCount = 0;

const debouncedRefreshPlan = () => {
  if (planRefreshTimeoutRef.current !== null) {
    window.clearTimeout(planRefreshTimeoutRef.current);
  }

  // 300ms 后刷新计划
  planRefreshTimeoutRef.current = window.setTimeout(() => {
    planRefreshTimeoutRef.current = null;
    void apiGet<WorkflowAssistantPlan>(...).then(setPlan);
  }, 300);
};

const handleEvent = (event: MessageEvent<string>) => {
  // ... 处理事件

  batchedEventCount++;
  debouncedRefreshPlan();

  if (eventBatchTimeout !== null) {
    window.clearTimeout(eventBatchTimeout);
  }

  // 1秒后批量刷新关注列表
  eventBatchTimeout = window.setTimeout(() => {
    eventBatchTimeout = null;
    if (batchedEventCount > 0) {
      batchedEventCount = 0;
      debouncedRefreshAttention();
    }
  }, 1000);
};
```

**效果**:
- 53 个历史事件只触发 1 次关注列表刷新，而非 53 次
- 计划刷新也只在事件停止 300ms 后执行一次
- 请求数量从 159 降至约 3-5 个

### 3. 前端：更新类型定义

**修改文件**: `frontend/src/types.ts`

添加 `WorkflowAssistantPlanSummary` 类型，与后端保持一致。

更新 `WorkflowAssistantAttentionList` 使用精简摘要：

```typescript
export type WorkflowAssistantAttentionList = {
  plans: WorkflowAssistantPlanSummary[];
};
```

## 性能对比

### 修复前
- 页面加载触发请求数: ~159 个
- 关注列表响应大小: 4.08 MiB
- 并发请求峰值内存: 3.57 GiB
- 服务器状态: 频繁 500 错误，容器重启

### 修复后（预期）
- 页面加载触发请求数: ~3-5 个
- 关注列表响应大小: 50-100 KiB
- 并发请求峰值内存: <500 MiB
- 服务器状态: 稳定运行在 267 MiB

## 未来优化建议

1. **知识快照改为计划级存储**
   - 当前：每个步骤重复存储相同快照
   - 建议：计划级存储一次，步骤通过引用访问
   - 预期收益：单个计划大小从 2.78 MiB 降至 <500 KiB

2. **SSE 历史事件分页**
   - 当前：一次回放所有历史事件
   - 建议：首次只回放最近 10 条，按需加载更多
   - 预期收益：减少初始加载时间和请求量

3. **计划详情按需加载**
   - 当前：关注列表点击后加载完整计划
   - 建议：已经合理，保持不变

## 验证步骤

1. 构建前端: `cd frontend && npm.cmd run build`
2. 启动服务: `docker compose up -d --build`
3. 打开工作流助手页面，观察网络请求
4. 确认关注列表响应大小 <100 KiB
5. 确认不会触发大量并发请求
6. 测试下载交付包功能正常

## 修改文件清单

- `backend/workflow_assistant/contracts.py` - 添加 WorkflowPlanSummary
- `backend/workflow_assistant/http.py` - 修改关注列表接口
- `frontend/src/types.ts` - 添加前端类型
- `frontend/src/components/workflow-assistant-workspace.tsx` - 批量处理和防抖
- `docs/fix-workflow-assistant-request-storm.md` - 本文档
