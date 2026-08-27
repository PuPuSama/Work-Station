# 工作流助手请求风暴修复

## 问题描述

工作流助手页面在打开时会触发请求风暴，导致后端内存暴涨至 3.57 GiB，最终 OOM。

### 根本原因

1. **SSE 历史事件回放**：页面打开后一次回放 53 条历史事件
2. **每事件多次刷新**：每个 SSE 事件都触发：
   - 刷新完整计划（~673 KiB）
   - 刷新关注数量
   - 刷新关注列表（~4.08 MiB，包含完整步骤和知识快照）
3. **超大响应体**：
   - 单个计划约 673 KiB
   - 关注列表约 4.08 MiB
   - 知识快照在每个步骤中重复保存（39 KiB × 28 步 = 1.1 MiB）
4. **请求风暴**：53 事件 × 3 请求 = 159 个瞬时并发请求

### 实际影响

- 2 分钟内 167 个工作流请求
- 138 个请求返回 500
- 后端内存从 267 MiB 暴涨至 3.57 GiB
- 下载交付包时直接卡死

## 修复方案

### 1. 后端：精简关注列表响应

**新增 `WorkflowPlanSummary` 类型**（`backend/workflow_assistant/contracts.py`）：
- 移除 `steps` 数组（包含重复的知识快照）
- 添加 `step_count` 和 `pending_step_count` 汇总字段
- 保留所有元数据字段

**修改关注列表接口**（`backend/workflow_assistant/http.py`）：
- `/api/workflow-assistant/attention` 现在返回 `WorkflowPlanSummary[]`
- 响应体从 ~4 MiB 降至 ~50 KiB（约 80 倍缩减）

### 2. 前端：批量处理和防抖

**SSE 事件批处理**（`frontend/src/components/workflow-assistant-workspace.tsx`）：
- 计划刷新防抖 300ms
- 关注列表刷新防抖 500ms，并在批量事件结束后才触发
- 历史回放期间，53 个事件只触发 1-2 次实际刷新

**类型更新**（`frontend/src/types.ts`）：
- 添加 `WorkflowAssistantPlanSummary` 类型
- `WorkflowAssistantAttentionList.plans` 改为精简摘要类型

## 性能提升

### 前端请求量
- 修复前：53 事件 × 3 = 159 个瞬时请求
- 修复后：1-2 个计划刷新 + 1 个关注刷新 = 2-3 个请求
- **减少 98%**

### 网络传输
- 修复前：53 × (673 KiB + 4.08 MiB) ≈ 220 MiB
- 修复后：673 KiB + 50 KiB ≈ 723 KiB
- **减少 99.7%**

### 后端内存
- 修复前：峰值 3.57 GiB（OOM）
- 修复后：预期 < 500 MiB
- **减少 ~85%**

## 后续优化建议

1. **知识快照去重**：改为计划级存储或引用，避免每步重复
2. **计划响应优化**：考虑只返回变更的步骤，而非完整步骤数组
3. **SSE 增量更新**：发送步骤 diff 而非触发完整刷新
4. **数据库优化**：减少 1,402 个检查点和 10,525 条 checkpoint writes

## 相关文件

- `backend/workflow_assistant/contracts.py`
- `backend/workflow_assistant/http.py`
- `frontend/src/components/workflow-assistant-workspace.tsx`
- `frontend/src/types.ts`
