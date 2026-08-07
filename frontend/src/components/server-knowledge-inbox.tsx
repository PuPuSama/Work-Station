"use client";

/* eslint-disable react-hooks/set-state-in-effect */

import {
  AlertCircle,
  BookOpenText,
  CheckCircle2,
  Download,
  Eye,
  FileStack,
  ImageIcon,
  Inbox,
  Loader2,
  PackageCheck,
  PackageSearch,
  RefreshCw,
  ShieldCheck,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { KnowledgeReviewGuide } from "@/components/knowledge-review-guide";
import { ServerPrivateDocumentUpload } from "@/components/server-private-document-upload";
import { apiGet, apiPost, apiPut } from "@/lib/api";
import type {
  AccessibleProject,
  KnowledgeLibrary,
  KnowledgeProductSummary,
  KnowledgeSnapshotEvidenceManifest,
  KnowledgeSnapshotEvidencePreview,
  KnowledgeSnapshotRawDownload,
  KnowledgeSourceSummary,
  KnowledgeUploadResult,
} from "@/types";

type ReviewDecision = "approve" | "needs_review" | "reject";

const sourceKindLabels: Record<string, string> = {
  private_file: "私有资料",
  product_detail: "产品详情",
  product_category: "产品分类",
  official_blog: "官网博客",
  knowledge_page: "知识页面",
};

const trustTierLabels: Record<string, string> = {
  hard_fact: "硬事实",
  reference_material: "参考资料",
  writing_instruction: "写作指令",
};

const statusLabels: Record<string, string> = {
  inbox: "待发布",
  needs_review: "需复核",
  rejected: "已拒绝",
  published: "已发布",
  candidate: "待确认",
  confirmed: "已确认",
};

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "知识库操作失败，请重试。";
}

function canEditKnowledge(
  role: AccessibleProject["effective_role"] | null,
) {
  return role === "org_admin" || role === "team_lead" || role === "editor";
}

function formatDate(value: string | null) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? "—"
    : new Intl.DateTimeFormat("zh-CN", {
        dateStyle: "medium",
        timeStyle: "short",
      }).format(date);
}

function formatByteSize(value: number | null) {
  if (value === null) return "—";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function hasExactSnapshotIdentity(
  value: { project_id: string; source_id: string; snapshot_id: string },
  projectId: string,
  sourceId: string,
  snapshotId: string,
) {
  return (
    value.project_id === projectId &&
    value.source_id === sourceId &&
    value.snapshot_id === snapshotId
  );
}

function SnapshotEvidencePanel({
  projectId,
  snapshotId,
  sourceId,
  sourcePath,
}: {
  projectId: string;
  snapshotId: string;
  sourceId: string;
  sourcePath: string;
}) {
  const [manifest, setManifest] =
    useState<KnowledgeSnapshotEvidenceManifest | null>(null);
  const [manifestLoading, setManifestLoading] = useState(true);
  const [manifestError, setManifestError] = useState("");
  const [preview, setPreview] =
    useState<KnowledgeSnapshotEvidencePreview | null>(null);
  const [previewVisible, setPreviewVisible] = useState(false);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState("");
  const [downloadLoading, setDownloadLoading] = useState(false);
  const [downloadError, setDownloadError] = useState("");
  const manifestRequestId = useRef(0);
  const evidencePath = `${sourcePath}/snapshots/${encodeURIComponent(snapshotId)}/evidence`;

  const loadManifest = useCallback(async () => {
    const requestId = manifestRequestId.current + 1;
    manifestRequestId.current = requestId;
    setManifestLoading(true);
    setManifestError("");
    try {
      const result = await apiGet<KnowledgeSnapshotEvidenceManifest>(
        evidencePath,
      );
      if (
        !hasExactSnapshotIdentity(result, projectId, sourceId, snapshotId)
      ) {
        throw new Error("证据清单与当前资料版本不匹配。");
      }
      if (manifestRequestId.current === requestId) {
        setManifest(result);
      }
    } catch (error) {
      if (manifestRequestId.current === requestId) {
        setManifest(null);
        setManifestError(errorMessage(error));
      }
    } finally {
      if (manifestRequestId.current === requestId) {
        setManifestLoading(false);
      }
    }
  }, [evidencePath, projectId, snapshotId, sourceId]);

  useEffect(() => {
    void loadManifest();
    return () => {
      manifestRequestId.current += 1;
    };
  }, [loadManifest]);

  async function togglePreview() {
    if (preview) {
      setPreviewVisible((visible) => !visible);
      return;
    }
    setPreviewLoading(true);
    setPreviewError("");
    try {
      const result = await apiGet<KnowledgeSnapshotEvidencePreview>(
        `${evidencePath}/preview`,
      );
      if (
        !hasExactSnapshotIdentity(result, projectId, sourceId, snapshotId) ||
        result.slot !== manifest?.slot
      ) {
        throw new Error("内容预览与当前资料版本不匹配。");
      }
      setPreview(result);
      setPreviewVisible(true);
    } catch (error) {
      setPreviewError(errorMessage(error));
    } finally {
      setPreviewLoading(false);
    }
  }

  async function downloadRawEvidence() {
    setDownloadLoading(true);
    setDownloadError("");
    try {
      const result = await apiPost<KnowledgeSnapshotRawDownload>(
        `${evidencePath}/raw-download`,
      );
      if (
        !hasExactSnapshotIdentity(result, projectId, sourceId, snapshotId) ||
        result.slot !== manifest?.slot
      ) {
        throw new Error("原始文件与当前资料版本不匹配。");
      }
      window.open(result.download_url, "_blank", "noopener,noreferrer");
    } catch (error) {
      setDownloadError(errorMessage(error));
    } finally {
      setDownloadLoading(false);
    }
  }

  if (manifestLoading) {
    return (
      <div className="flex items-center gap-2 text-xs text-muted-foreground">
        <Loader2 className="size-3.5 animate-spin" />
        正在读取此版本的证据清单…
      </div>
    );
  }

  if (!manifest) {
    return (
      <Alert variant="destructive" className="py-3">
        <AlertCircle className="size-4" />
        <AlertTitle>证据清单读取失败</AlertTitle>
        <AlertDescription className="grid gap-2">
          <span>{manifestError || "请重试。"}</span>
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="w-fit"
            onClick={() => void loadManifest()}
          >
            重试
          </Button>
        </AlertDescription>
      </Alert>
    );
  }

  return (
    <div className="grid gap-3 border-t pt-3">
      <div className="grid gap-2 text-xs text-muted-foreground sm:grid-cols-2">
        <div>
          <span className="block text-foreground">规范化证据</span>
          {manifest.normalized_available
            ? `${manifest.normalized_content_type || "未知类型"} · ${formatByteSize(manifest.normalized_byte_size)}`
            : "不可用"}
        </div>
        <div>
          <span className="block text-foreground">原始证据</span>
          {manifest.raw_available
            ? `${manifest.raw_content_type || "未知类型"} · ${formatByteSize(manifest.raw_byte_size)}`
            : "不可用"}
        </div>
      </div>
      <div className="flex flex-wrap gap-2">
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={
            previewLoading ||
            !manifest.normalized_available ||
            !manifest.preview_supported
          }
          onClick={() => void togglePreview()}
        >
          {previewLoading ? (
            <Loader2 className="animate-spin" />
          ) : (
            <Eye />
          )}
          {previewVisible ? "收起规范化文本" : "查看规范化文本"}
        </Button>
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={downloadLoading || !manifest.raw_available}
          onClick={() => void downloadRawEvidence()}
        >
          {downloadLoading ? (
            <Loader2 className="animate-spin" />
          ) : (
            <Download />
          )}
          下载原始证据
        </Button>
      </div>
      {previewError ? (
        <p className="text-xs text-destructive">{previewError}</p>
      ) : null}
      {downloadError ? (
        <p className="text-xs text-destructive">{downloadError}</p>
      ) : null}
      {preview && previewVisible ? (
        <div className="grid gap-2 rounded-lg border bg-background p-3">
          <div className="flex flex-wrap items-center justify-between gap-2 text-[11px] text-muted-foreground">
            <span>{preview.block_count} 个文本块</span>
            {preview.truncated ? <Badge variant="outline">已截断</Badge> : null}
          </div>
          <pre className="max-h-80 overflow-auto whitespace-pre-wrap break-words font-sans text-xs leading-5">
            {preview.text}
          </pre>
        </div>
      ) : null}
    </div>
  );
}

function SourceReviewCard({
  editable,
  onChanged,
  pending,
  projectPath,
  publishable,
  source,
}: {
  editable: boolean;
  onChanged: (
    key: string,
    action: Promise<unknown>,
    message: string,
  ) => Promise<boolean>;
  pending: string;
  projectPath: string;
  publishable: boolean;
  source: KnowledgeSourceSummary;
}) {
  const [sourceKind, setSourceKind] = useState(source.source_kind);
  const [trustTier, setTrustTier] = useState(source.trust_tier);
  const [decision, setDecision] = useState<ReviewDecision>(
    source.pending_review_decision || "approve",
  );
  const [reason, setReason] = useState("");
  const reviewReceiptId = useRef<string | null>(null);
  const sourcePath = `${projectPath}/sources/${encodeURIComponent(source.source_id)}`;
  const pendingSnapshotId = source.pending_snapshot_id;
  const pendingSnapshotPath = pendingSnapshotId
    ? `${sourcePath}/snapshots/${encodeURIComponent(pendingSnapshotId)}`
    : "";
  const reviewKey = `review:${source.source_id}:${pendingSnapshotId || "none"}`;
  const publishKey = `publish:${source.source_id}:${pendingSnapshotId || "none"}`;
  const reviewing = pending === reviewKey;
  const publishing = pending === publishKey;

  async function reviewPendingSnapshot() {
    if (!pendingSnapshotId) return;
    const receiptId = reviewReceiptId.current || crypto.randomUUID();
    reviewReceiptId.current = receiptId;
    const reviewPromise = apiPut(pendingSnapshotPath + "/review", {
      receipt_id: receiptId,
      source_kind: sourceKind,
      trust_tier: trustTier,
      decision,
      reason: reason.trim(),
    });
    const saved = await onChanged(
      reviewKey,
      reviewPromise,
      `${source.display_name} 的审核结果已保存。`,
    );
    if (saved) reviewReceiptId.current = null;
  }

  function publishPendingSnapshot() {
    if (!pendingSnapshotId) return;
    const publishPromise = apiPost(pendingSnapshotPath + "/publish");
    void onChanged(
      publishKey,
      publishPromise,
      `${source.display_name} 已发布，新版本开始参与文章检索。`,
    );
  }

  return (
    <Card>
      <CardHeader className="gap-3 border-b">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <CardTitle className="break-words text-base">
              {source.display_name}
            </CardTitle>
            <CardDescription className="mt-1 break-all text-[11px]">
              内部编号：{source.source_id}
            </CardDescription>
          </div>
          <Badge variant={source.status === "published" ? "default" : "secondary"}>
            {statusLabels[source.status] || source.status}
          </Badge>
        </div>
        {source.classification_reason ? (
          <p className="text-xs leading-5 text-muted-foreground">
            系统判断：{source.classification_reason}
          </p>
        ) : null}
      </CardHeader>
      <CardContent className="grid gap-4">
        <div className="grid gap-3 md:grid-cols-2">
          <div className="grid gap-2 rounded-xl border bg-muted/10 p-4">
            <div className="flex items-center justify-between gap-3">
              <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                当前在线版本
              </span>
              <Badge variant={source.current_snapshot_id ? "default" : "secondary"}>
                {source.current_snapshot_id ? "检索中" : "尚未发布"}
              </Badge>
            </div>
            <p className="text-xs leading-5 text-muted-foreground">
              {source.current_snapshot_id
                ? "文章检索目前正在使用这个版本。"
                : "当前没有已发布版本；待审核资料不会参与检索。"}
            </p>
            {source.current_snapshot_id ? (
              <details className="text-xs text-muted-foreground">
                <summary className="cursor-pointer">技术信息</summary>
                <div className="mt-1 break-all font-mono">{source.current_snapshot_id}</div>
              </details>
            ) : null}
            {source.current_snapshot_id ? (
              <SnapshotEvidencePanel
                key={source.current_snapshot_id}
                projectId={source.project_id}
                sourceId={source.source_id}
                sourcePath={sourcePath}
                snapshotId={source.current_snapshot_id}
              />
            ) : null}
          </div>

          <div className="grid gap-2 rounded-xl border bg-muted/10 p-4">
            <div className="flex items-center justify-between gap-3">
              <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                本次待审核版本
              </span>
              <Badge variant={pendingSnapshotId ? "secondary" : "outline"}>
                {pendingSnapshotId ? "待审" : "无待审版本"}
              </Badge>
            </div>
            {pendingSnapshotId ? (
              <>
                <details className="text-xs text-muted-foreground">
                  <summary className="cursor-pointer">技术信息</summary>
                  <div className="mt-1 break-all font-mono">{pendingSnapshotId}</div>
                </details>
                <div className="grid grid-cols-3 gap-2 text-xs text-muted-foreground">
                  <div>
                    <span className="block text-foreground">
                      {source.pending_chunk_count}
                    </span>
                    可检索段落
                  </div>
                  <div>
                    <span className="block text-foreground">
                      {source.pending_asset_count}
                    </span>
                    图片/附件
                  </div>
                  <div>
                    <span className="block text-foreground">
                      {formatDate(source.pending_fetched_at)}
                    </span>
                    入库时间
                  </div>
                </div>
                <p className="text-xs leading-5 text-muted-foreground">
                  最近审核：
                  {source.pending_review_decision === "approve"
                    ? "已通过"
                    : source.pending_review_decision === "needs_review"
                      ? "需负责人复核"
                      : source.pending_review_decision === "reject"
                        ? "不采用"
                        : "尚未审阅"}
                  {source.pending_review_version !== null
                    ? ` · v${source.pending_review_version}`
                    : ""}
                  {source.pending_reviewed_at
                    ? ` · ${formatDate(source.pending_reviewed_at)}`
                    : ""}
                </p>
                <SnapshotEvidencePanel
                  key={pendingSnapshotId}
                  projectId={source.project_id}
                  sourceId={source.source_id}
                  sourcePath={sourcePath}
                  snapshotId={pendingSnapshotId}
                />
              </>
            ) : (
              <p className="text-xs leading-5 text-muted-foreground">
                当前没有待审核版本。
                {source.latest_snapshot_id
                  ? " 最近一次入库已完成。"
                  : " 尚未入库任何资料版本。"}
              </p>
            )}
          </div>
        </div>

        {pendingSnapshotId ? (
          <div className="grid gap-4 rounded-xl border bg-muted/20 p-4">
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="grid gap-2">
                <Label htmlFor={`kind-${source.source_id}`}>来源类型</Label>
                <select
                  id={`kind-${source.source_id}`}
                  value={sourceKind}
                  disabled={!editable || Boolean(pending)}
                  className="min-h-11 rounded-lg border bg-background px-3 text-sm"
                  onChange={(event) => {
                    reviewReceiptId.current = null;
                    setSourceKind(event.target.value);
                  }}
                >
                  {Object.entries(sourceKindLabels).map(([value, label]) => (
                    <option key={value} value={value}>
                      {label}
                    </option>
                  ))}
                </select>
              </div>
              <div className="grid gap-2">
                <Label htmlFor={`trust-${source.source_id}`}>信息用途</Label>
                <select
                  id={`trust-${source.source_id}`}
                  value={trustTier}
                  disabled={!editable || Boolean(pending)}
                  className="min-h-11 rounded-lg border bg-background px-3 text-sm"
                  onChange={(event) => {
                    reviewReceiptId.current = null;
                    setTrustTier(event.target.value);
                  }}
                >
                  {Object.entries(trustTierLabels).map(([value, label]) => (
                    <option key={value} value={value}>
                      {label}
                    </option>
                  ))}
                </select>
              </div>
            </div>
            <div className="grid gap-2">
              <Label htmlFor={`decision-${source.source_id}`}>审核结果</Label>
              <select
                id={`decision-${source.source_id}`}
                value={decision}
                disabled={!editable || Boolean(pending)}
                className="min-h-11 rounded-lg border bg-background px-3 text-sm"
                onChange={(event) => {
                  reviewReceiptId.current = null;
                  setDecision(event.target.value as ReviewDecision);
                }}
              >
                <option value="approve">通过，进入待发布</option>
                <option value="needs_review">暂不发布，交给负责人复核</option>
                <option value="reject">不采用这份资料</option>
              </select>
            </div>
            <div className="grid gap-2">
              <Label htmlFor={`reason-${source.source_id}`}>判断依据</Label>
              <Input
                id={`reason-${source.source_id}`}
                value={reason}
                disabled={!editable || Boolean(pending)}
                maxLength={500}
                placeholder="例如：官网产品详情页，规格和图片与产品一致"
                onChange={(event) => {
                  reviewReceiptId.current = null;
                  setReason(event.target.value);
                }}
              />
            </div>
            <Button
              type="button"
              variant="outline"
              className="min-h-11"
              disabled={
                !editable || Boolean(pending) || !reason.trim()
              }
              onClick={() => void reviewPendingSnapshot()}
            >
              {reviewing ? (
                <Loader2 className="animate-spin" />
              ) : (
                <ShieldCheck />
              )}
              保存审核结果
            </Button>
          </div>
        ) : (
          <div
            className={
              "flex items-center gap-2 rounded-xl border bg-muted/20 " +
              "px-4 py-3 text-sm text-muted-foreground"
            }
          >
            <CheckCircle2 className="size-4" />
            当前没有待审核版本，来源信息与在线版本仅供查看。
          </div>
        )}

        {pendingSnapshotId && source.pending_review_decision === "approve" ? (
          <Button
            type="button"
            className="min-h-11"
            disabled={
              !publishable ||
              Boolean(pending) ||
              source.pending_chunk_count === 0
            }
            onClick={publishPendingSnapshot}
          >
            {publishing ? (
              <Loader2 className="animate-spin" />
            ) : (
              <PackageCheck />
            )}
            发布这份资料
          </Button>
        ) : pendingSnapshotId ? (
          <p
            className={
              "rounded-xl border border-dashed px-4 py-3 text-xs " +
              "leading-5 text-muted-foreground"
            }
          >
            审核通过后还要点击“发布这份资料”，它才会参与文章检索；原来的在线版本不会受影响。
          </p>
        ) : null}
      </CardContent>
    </Card>
  );
}

function ProductCard({
  onChanged,
  pending,
  product,
  projectPath,
  publishable,
}: {
  onChanged: (
    key: string,
    action: Promise<unknown>,
    message: string,
  ) => Promise<boolean>;
  pending: string;
  product: KnowledgeProductSummary;
  projectPath: string;
  publishable: boolean;
}) {
  const confirming = pending === `product:${product.product_id}`;
  return (
    <div className="grid gap-3 rounded-xl border bg-card p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="font-medium">{product.name}</div>
          <div className="mt-1 break-all font-mono text-[11px] text-muted-foreground">
            {product.product_id}
          </div>
        </div>
        <Badge variant={product.status === "confirmed" ? "default" : "secondary"}>
          {statusLabels[product.status] || product.status}
        </Badge>
      </div>
      <p className="text-xs text-muted-foreground">
        {product.category_path.length
          ? product.category_path.join(" / ")
          : "尚未确认分类路径"}
      </p>
      {product.status !== "confirmed" ? (
        <Button
          type="button"
          variant="outline"
          className="min-h-11"
          disabled={!publishable || Boolean(pending)}
          onClick={() =>
            void onChanged(
              `product:${product.product_id}`,
              apiPost(
                `${projectPath}/products/${encodeURIComponent(product.product_id)}/confirm`,
              ),
              `${product.name} 已确认；只有 Published Current Evidence 完整时才会出现在文章选择器。`,
            )
          }
        >
          {confirming ? (
            <Loader2 className="animate-spin" />
          ) : (
            <PackageSearch />
          )}
          确认产品身份
        </Button>
      ) : null}
    </div>
  );
}

export function ServerKnowledgeInbox({ customer }: { customer: string }) {
  const [library, setLibrary] = useState<KnowledgeLibrary | null>(null);
  const [role, setRole] = useState<AccessibleProject["effective_role"] | null>(
    null,
  );
  const [loading, setLoading] = useState(true);
  const [pending, setPending] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const projectPath = `/api/knowledge/${encodeURIComponent(customer)}`;

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [nextLibrary, projects] = await Promise.all([
        apiGet<KnowledgeLibrary>(projectPath),
        apiGet<AccessibleProject[]>("/api/projects"),
      ]);
      setLibrary(nextLibrary);
      setRole(
        projects.find((project) => project.project_id === customer)
          ?.effective_role ?? null,
      );
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setLoading(false);
    }
  }, [customer, projectPath]);

  useEffect(() => {
    void load();
  }, [load]);

  async function run(
    key: string,
    action: Promise<unknown>,
    successMessage: string,
  ): Promise<boolean> {
    setPending(key);
    setError("");
    setMessage("");
    try {
      await action;
      setMessage(successMessage);
      await load();
      return true;
    } catch (reason) {
      setError(errorMessage(reason));
      return false;
    } finally {
      setPending("");
    }
  }

  async function uploaded(result: KnowledgeUploadResult) {
    setError("");
    setMessage(
      result.created
        ? `${result.message} 已生成 ${result.chunk_count} 个知识块、登记 ${result.asset_count} 个内嵌资产。`
        : `${result.message} 本次没有重复创建资料版本或审核记录。`,
    );
    await load();
  }

  const sortedSources = useMemo(
    () =>
      [...(library?.sources || [])].sort((left, right) => {
        const rank = (source: KnowledgeSourceSummary) =>
          source.pending_snapshot_id
            ? 0
            : source.status === "needs_review"
              ? 1
              : source.status === "rejected"
                ? 2
                : 3;
        return (
          rank(left) - rank(right) ||
          left.display_name.localeCompare(right.display_name)
        );
      }),
    [library],
  );
  const editable = canEditKnowledge(role);
  const summaries = [
    { label: "全部来源", value: library?.source_count || 0, icon: FileStack },
    {
      label: "待审核版本",
      value: library?.pending_count || 0,
      icon: Inbox,
    },
    {
      label: "已发布来源",
      value: library?.published_count || 0,
      icon: CheckCircle2,
    },
    {
      label: "已确认产品",
      value: library?.confirmed_product_count || 0,
      icon: PackageSearch,
    },
    { label: "去重资产", value: library?.asset_count || 0, icon: ImageIcon },
  ];

  return (
    <main className="mx-auto grid w-full max-w-[1480px] gap-5 px-5 py-6">
      <div className="flex flex-col gap-4 rounded-2xl border bg-card p-5 md:flex-row md:items-end md:justify-between">
        <div>
          <div className="mb-2 flex items-center gap-2 text-xs font-medium uppercase tracking-[0.16em] text-muted-foreground">
            <BookOpenText className="size-4" />
            知识库审核
          </div>
          <h1 className="text-2xl font-semibold">知识来源审阅</h1>
          <p className="mt-2 max-w-3xl text-sm leading-6 text-muted-foreground">
            私有资料上传与 Rediscovery 都只把来源、产品和图片证据放入
            待审核区。这里负责人工分类、发布资料与确认产品身份；WordPress
            Sync 和原始对象打开仍保持 Server 关闭。Research Run 已迁移到独立的
            PostgreSQL Worker，可在“资料研究”页签中运行。
          </p>
        </div>
        <Button
          type="button"
          variant="outline"
          className="min-h-11"
          disabled={loading || Boolean(pending)}
          onClick={() => void load()}
        >
          {loading ? <Loader2 className="animate-spin" /> : <RefreshCw />}
          刷新 Inbox
        </Button>
      </div>

      {error ? (
        <Alert variant="destructive">
          <AlertCircle />
          <AlertTitle>知识库操作失败</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      ) : null}
      {message ? (
        <Alert>
          <CheckCircle2 />
          <AlertTitle>服务端状态已更新</AlertTitle>
          <AlertDescription>{message}</AlertDescription>
        </Alert>
      ) : null}

      <KnowledgeReviewGuide />

      <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
        {summaries.map((item) => (
          <Card key={item.label}>
            <CardContent className="flex items-center justify-between gap-3 py-4">
              <div>
                <div className="text-2xl font-semibold tabular-nums">
                  {loading ? "—" : item.value}
                </div>
                <div className="mt-1 text-xs text-muted-foreground">
                  {item.label}
                </div>
              </div>
              <span className="grid size-9 place-items-center rounded-lg bg-muted text-muted-foreground">
                <item.icon className="size-4" />
              </span>
            </CardContent>
          </Card>
        ))}
      </section>

      {!editable ? (
        <Alert>
          <ShieldCheck />
          <AlertTitle>当前角色为只读审阅视图</AlertTitle>
          <AlertDescription>
            Reviewer/Viewer 可以核对来源与产品状态，但只有
            Editor、Team Lead 或 Organization Admin 可以审阅、发布或确认。
          </AlertDescription>
        </Alert>
      ) : null}

      <section className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_380px]">
        <div className="rounded-2xl border border-dashed bg-muted/10 p-5">
          <div className="flex items-start gap-3">
            <span className="grid size-10 shrink-0 place-items-center rounded-xl bg-background text-muted-foreground shadow-sm">
              <ShieldCheck className="size-5" />
            </span>
            <div>
              <h2 className="font-semibold">私有资料安全边界</h2>
              <p className="mt-1 max-w-3xl text-sm leading-6 text-muted-foreground">
                上传先写入项目隔离、内容寻址的私有对象；数据库提交前会重新检查
                knowledge.edit，并把来源、版本、段落、图片关系和脱敏
                审核记录放进同一个事务。权限在上传期间被撤销时，不会留下可查询的半成品。
              </p>
            </div>
          </div>
        </div>
        <ServerPrivateDocumentUpload
          editable={editable}
          projectPath={projectPath}
          onUploaded={uploaded}
        />
      </section>

      <section className="grid gap-4">
        <div>
          <h2 className="text-lg font-semibold">来源队列</h2>
          <p className="mt-1 text-sm text-muted-foreground">
            先保存审核结果，再单独发布；处理失败时原来的在线资料仍然可用。
          </p>
        </div>
        {loading ? (
          <div className="flex min-h-44 items-center justify-center rounded-xl border text-sm text-muted-foreground">
            <Loader2 className="mr-2 size-4 animate-spin" />
            正在读取当前项目的知识库…
          </div>
        ) : sortedSources.length ? (
          <div className="grid gap-4 xl:grid-cols-2">
            {sortedSources.map((source) => (
              <SourceReviewCard
                key={`${source.source_id}:${source.pending_snapshot_id || "none"}`}
                source={source}
                projectPath={projectPath}
                editable={editable}
                publishable={editable}
                pending={pending}
                onChanged={run}
              />
            ))}
          </div>
        ) : (
          <div className="flex min-h-44 flex-col items-center justify-center rounded-xl border border-dashed px-6 text-center">
            <Inbox className="mb-3 size-8 text-muted-foreground" />
            <div className="font-medium">当前 Project 的 Inbox 为空</div>
            <p className="mt-1 max-w-xl text-sm text-muted-foreground">
              上传私有资料，或从文章 Setup 发起 Product Rediscovery。成功上传和
              Job 都只进入 Inbox，不会自动发布知识或改变 Task 产品。
            </p>
          </div>
        )}
      </section>

      {library?.products.length ? (
        <section className="grid gap-4">
          <div>
            <h2 className="text-lg font-semibold">产品身份</h2>
            <p className="mt-1 text-sm text-muted-foreground">
              确认只改变产品目录身份；文章工作台仍要求当前已发布资料版本
              Evidence 完整，不能把候选产品事实直接写入 Task。
            </p>
          </div>
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            {library.products.map((product) => (
              <ProductCard
                key={product.product_id}
                product={product}
                projectPath={projectPath}
                publishable={editable}
                pending={pending}
                onChanged={run}
              />
            ))}
          </div>
        </section>
      ) : null}
    </main>
  );
}
