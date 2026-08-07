# M7 旧 Knowledge Artifact 到 ObjectStore 的受控迁移

## 1. 问题与边界

M2/M6 在 Local Mode 产生的 `source_snapshots.*_artifact_uri` 和
`knowledge_assets.artifact_uri` 可能仍是 `file://`。M7 Server Mode 不允许 HTTP 请求读取
服务器本地路径，因此这些旧 Snapshot 可以出现在 Inbox，但 Evidence Manifest 必须
fail closed。

本切片提供一次性离线迁移，不放宽 Server HTTP：

```text
Org Admin + knowledge.delete
  -> 精确 Project
  -> 只解析 ARTICLE_AGENT_KNOWLEDGE_ROOT/<project_id> 下的 file://
  -> 校验 <project>/<namespace>/<sha-prefix>/<sha>/<filename> 内容寻址布局
  -> 校验 Snapshot Raw / KnowledgeAsset 已保存 SHA-256
  -> 校验 KnowledgeAsset byte_size
  -> 复制到 organizations/{org}/projects/{project}/blobs/{hash-prefix}/{hash}
  -> Head + 有界 Get 重新校验 SHA-256 与大小
  -> 事务内锁定 Active Project、权限事实和全部 Artifact 引用
  -> 拒绝检查后新增、删除或修改的并发引用
  -> 以旧 URI 为 CAS 条件切换 Artifact URI
  -> append-only 脱敏 Audit
```

Snapshot 的内容哈希、Parser 身份、Chunk、产品证据、Current/Pending 指针和发布状态均不
改变；只迁移存储位置。旧文件不删除。对象上传发生在数据库事务前，若权限、CAS 或 Audit
失败，数据库保持原值，内容寻址对象由现有 Orphan Reconciler 延迟对账。

## 2. 失败策略

- 默认 `inspect` 只读，不上传对象、不写数据库或 Audit；
- `apply` 必须同时提供完全一致的 `--confirm-project-id`；
- 只允许具有 `knowledge.delete` 的 Active User 操作同一 Active Organization 的 Active
  Project；
- 本地 URI 必须证明仍位于精确项目子目录，并符合 LocalArtifactStore 的四级内容寻址布局；
  越界、布局伪造、缺失、空文件、超限、哈希或大小不符全部 fail closed；
- 上传后切换 URI 前必须在同一事务内锁定 Active Project 并重新读取完整 Artifact 引用集合；
  任何并发新增、删除或修改都会中止数据库切换；
- 已迁移 URI 必须属于当前 Bucket 和精确 Organization/Project 内容寻址前缀，其他 S3
  Bucket 不会被静默导入；
- CLI 结果只返回 Project、数量、布尔值和稳定错误代码，不返回文件路径、对象 URI、
  Bucket、Hash、客户正文或供应商错误。

## 3. 运行

`apply` 前必须先建立可恢复的 PostgreSQL 回滚点，例如经过验证的数据库备份，或至少导出目标
Project 的 `source_snapshots.raw_artifact_uri`、`source_snapshots.normalized_artifact_uri` 和
`knowledge_assets.artifact_uri`。本工具保留旧文件，但数据库切换后不会另建旧 URI 映射表；只有
在 Evidence Preview、Raw Download 和 Product Asset 验收通过后，才能按保留策略清理旧文件。

先只读检查：

```powershell
cd D:\Project\article\article-agent-formal\backend
.\.venv\Scripts\python.exe -m knowledge_agent.m7_legacy_artifact_migration inspect `
  --organization-id <organization> `
  --user-id <active-org-admin> `
  --project-id <project>
```

核对数量后显式应用：

```powershell
.\.venv\Scripts\python.exe -m knowledge_agent.m7_legacy_artifact_migration apply `
  --organization-id <organization> `
  --user-id <active-org-admin> `
  --project-id <project> `
  --confirm-project-id <project>
```

成功后重复运行 `inspect`，`migrated_reference_count` 必须为 `0`，所有引用应进入
`already_managed_count`。迁移只解决旧开发数据的对象位置，不代表生产 Bucket Policy、备份
恢复、Route/Operation Digest 或整体 M7 部署门禁通过。
