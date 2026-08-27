-- Article Agent Performance Optimization Indexes
-- 执行时间：约 5-10 分钟（取决于数据量）
-- 使用 CONCURRENTLY 避免锁表

-- 1. 关注列表查询优化
-- 加速 workflow_assistant/repository.py:2505 的查询
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_plans_attention_query
ON workflow_plans(organization_id, creator_user_id, updated_at DESC)
WHERE attention_state != 'none';

-- 2. 步骤批量加载
-- 加速按 plan_id 加载所有步骤的操作
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_steps_plan_sequence
ON workflow_plan_steps(plan_id, sequence);

-- 3. 项目范围检查
-- 加速 workflow_assistant/repository.py:2492 的 EXISTS 子查询
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_plan_projects_lookup
ON workflow_plan_projects(organization_id, plan_id, project_id);

-- 4. 事件流查询
-- 加速 SSE 事件订阅的增量查询
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_plan_events_stream
ON workflow_plan_events(organization_id, plan_id, sequence DESC);

-- 5. 计划状态过滤
-- 加速按状态查询计划
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_plans_status
ON workflow_plans(organization_id, status, updated_at DESC);

-- 6. 后台任务关联
-- 加速通过 job_id 查找步骤
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_steps_job_lookup
ON workflow_plan_steps(organization_id, background_job_id)
WHERE background_job_id IS NOT NULL;

-- 验证索引创建成功
SELECT
    schemaname,
    tablename,
    indexname,
    pg_size_pretty(pg_relation_size(indexrelid)) as index_size
FROM pg_stat_user_indexes
WHERE indexname LIKE 'idx_%_attention%'
   OR indexname LIKE 'idx_%_plan_%'
   OR indexname LIKE 'idx_steps_%'
ORDER BY tablename, indexname;