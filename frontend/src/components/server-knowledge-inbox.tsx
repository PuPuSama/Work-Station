"use client";

/* eslint-disable react-hooks/set-state-in-effect */

import {
  AlertCircle,
  BookOpenText,
  CheckCircle2,
  Download,
  Eye,
  FileStack,
  Globe2,
  ImageIcon,
  Inbox,
  Loader2,
  PackageSearch,
  RefreshCw,
  ScanSearch,
  ShieldCheck,
  Trash2,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { KnowledgeSourceFilters } from "@/components/knowledge-source-filters";
import { KnowledgeSectionNav } from "@/components/knowledge-section-nav";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ServerPrivateDocumentUpload } from "@/components/server-private-document-upload";
import { apiDelete, apiGet, apiPost } from "@/lib/api";
import {
  countKnowledgeSourceOrigins,
  filterKnowledgeSources,
  type KnowledgeSourceSort,
} from "@/lib/knowledge-source-filters";
import { sameProjectId } from "@/lib/project-id";
import type {
  AccessibleProject,
  KnowledgeLibrary,
  KnowledgeSnapshotEvidenceManifest,
  KnowledgeSnapshotEvidencePreview,
  KnowledgeSnapshotRawDownload,
  KnowledgeSourceSummary,
  KnowledgeUploadResult,
  OfficialSiteScanStart,
  OfficialSiteScanStatus,
} from "@/types";

const statusLabels: Record<string, string> = {
  inbox: "待发布",
  needs_review: "需复核",
  rejected: "已撤下",
  published: "已发布",
  candidate: "待发布",
  confirmed: "已发布",
};

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "知识库操作失败，请重试。";
}

function canEditKnowledge(
  role: AccessibleProject["effective_role"] | null,
  isProjectOwner: boolean,
) {
  return (
    role === "org_admin" ||
    role === "editor" ||
    (role === "team_lead" && isProjectOwner)
  );
}

function canDeleteKnowledge(
  role: AccessibleProject["effective_role"] | null,
) {
  return role === "org_admin";
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
          {previewVisible ? "收起正文预览" : "查看正文预览"}
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
  deletable,
  onChanged,
  pending,
  projectPath,
  source,
}: {
  deletable: boolean;
  onChanged: (
    key: string,
    action: Promise<unknown>,
    message: string,
  ) => Promise<boolean>;
  pending: string;
  projectPath: string;
  source: KnowledgeSourceSummary;
}) {
  const sourcePath = `${projectPath}/sources/${encodeURIComponent(source.source_id)}`;
  const pendingSnapshotId = source.pending_snapshot_id;

  function withdrawSource() {
    if (!window.confirm(`确定撤下「${source.display_name}」吗？原文件和历史版本会保留。`)) {
      return;
    }
    void onChanged(
      `withdraw:${source.source_id}`,
      apiDelete(sourcePath),
      `${source.display_name} 已从当前检索中撤下，历史版本仍保留。`,
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
          <div className="flex items-center gap-2">
            <Badge variant={source.status === "published" ? "default" : "secondary"}>
              {pendingSnapshotId ? "自动发布中" : statusLabels[source.status] || source.status}
            </Badge>
            {deletable && (source.current_snapshot_id || pendingSnapshotId) ? (
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={Boolean(pending)}
                onClick={withdrawSource}
              >
                <Trash2 />
                撤下
              </Button>
            ) : null}
          </div>
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
                : "当前没有已发布版本；解析成功后系统会自动发布。"}
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
                本次解析版本
              </span>
              <Badge variant={pendingSnapshotId ? "secondary" : "outline"}>
                {pendingSnapshotId ? "自动发布中" : "已处理"}
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
                  系统会自动完成发布，不需要人工审核或点击确认。
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
                当前没有待处理版本。
                {source.latest_snapshot_id
                  ? " 最近一次入库已完成。"
                  : " 尚未入库任何资料版本。"}
              </p>
            )}
          </div>
        </div>

        <div className="flex items-center gap-2 rounded-xl border bg-muted/20 px-4 py-3 text-sm text-muted-foreground">
          <CheckCircle2 className="size-4" />
          资料与资产解析成功后直接发布；这里只保留证据预览和撤下操作。
        </div>
      </CardContent>
    </Card>
  );
}

export function ServerKnowledgeInbox({ customer }: { customer: string }) {
  const [library, setLibrary] = useState<KnowledgeLibrary | null>(null);
  const [role, setRole] = useState<AccessibleProject["effective_role"] | null>(
    null,
  );
  const [isProjectOwner, setIsProjectOwner] = useState(false);
  const [loading, setLoading] = useState(true);
  const [pending, setPending] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [startUrl, setStartUrl] = useState("");
  const [maxPages, setMaxPages] = useState("100");
  const [scanMessage, setScanMessage] = useState("");
  const [scanStatus, setScanStatus] = useState<OfficialSiteScanStatus | null>(null);
  const [sourceQuery, setSourceQuery] = useState("");
  const [sourceSort, setSourceSort] = useState<KnowledgeSourceSort>("newest");
  const [includeLocalSources, setIncludeLocalSources] = useState(true);
  const [includeWebsiteSources, setIncludeWebsiteSources] = useState(true);
  const projectPath = `/api/knowledge/${encodeURIComponent(customer)}`;
  const editable = canEditKnowledge(role, isProjectOwner);

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
        projects.find((project) => sameProjectId(project.project_id, customer))
          ?.effective_role ?? null,
      );
      const project = projects.find((item) => sameProjectId(item.project_id, customer));
      setIsProjectOwner(project?.is_project_owner === true);
      setStartUrl((current) => current || (project ? `https://${project.official_domain}/` : ""));
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setLoading(false);
    }
  }, [customer, projectPath]);

  useEffect(() => {
    void load();
  }, [load]);

  const loadScanStatus = useCallback(async () => {
    try {
      setScanStatus(
        await apiGet<OfficialSiteScanStatus>(
          `${projectPath}/official-site/scan/status`,
        ),
      );
    } catch {
      // The main knowledge view remains usable if status polling is unavailable.
    }
  }, [projectPath]);

  useEffect(() => {
    void loadScanStatus();
  }, [loadScanStatus]);

  useEffect(() => {
    if (scanStatus?.status !== "running") return;
    const timer = window.setInterval(() => {
      void loadScanStatus();
    }, 2000);
    return () => window.clearInterval(timer);
  }, [loadScanStatus, scanStatus?.status]);

  useEffect(() => {
    if (!scanStatus) return;
    if (scanStatus.status === "running") {
      setScanMessage(`扫描进行中 · 开始于 ${formatDate(scanStatus.started_at)}`);
      return;
    }
    if (scanStatus.status === "succeeded") {
      setScanMessage(
        `扫描完成 · 本次收录 ${scanStatus.processed_pages} 个页面，其中 ${scanStatus.processed_products} 个产品；知识库现有 ${scanStatus.source_count} 个来源，产品库现有 ${scanStatus.product_count} 个产品。`,
      );
      void load();
      return;
    }
    if (scanStatus.status === "failed") {
      setScanMessage(`扫描失败 · ${scanStatus.error || "上一版资料继续可用。"}`);
    }
  }, [load, scanStatus]);

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
    const parserLabel =
      result.parser_name === "mineru-content-list"
        ? "MinerU"
        : result.parser_name;
    setMessage(
      result.created
        ? `${result.message} 解析器：${parserLabel}；已生成 ${result.chunk_count} 个知识块、登记 ${result.asset_count} 个内嵌资产。`
        : `${result.message} 解析器：${parserLabel}；本次没有重复创建资料版本或审核记录。`,
    );
    await load();
  }

  async function startOfficialSiteScan() {
    setPending("official-site-scan");
    setError("");
    setMessage("");
    setScanMessage("正在提交官网扫描任务…");
    try {
      const result = await apiPost<OfficialSiteScanStart>(`${projectPath}/official-site/scan`, {
        start_url: startUrl.trim(),
        max_pages: Number(maxPages),
      });
      setScanStatus({
        scan_id: result.scan_id,
        status: "running",
        started_at: new Date().toISOString(),
        finished_at: null,
        processed_pages: 0,
        skipped_pages: 0,
        processed_products: 0,
        skipped_products: 0,
        source_count: library?.published_count || 0,
        product_count: library?.confirmed_product_count || 0,
        error: "",
      });
      const accepted = "扫描已启动；页面会持续显示状态，完成后自动刷新产品数量。";
      setScanMessage(accepted);
      setMessage(accepted);
      await load();
    } catch (reason) {
      setScanMessage("扫描启动失败，请查看错误提示。");
      setError(errorMessage(reason));
    } finally {
      setPending("");
    }
  }

  const visibleSources = useMemo(
    () =>
      filterKnowledgeSources(library?.sources ?? [], {
        query: sourceQuery,
        sort: sourceSort,
        includeLocal: includeLocalSources,
        includeWebsite: includeWebsiteSources,
      }),
    [
      includeLocalSources,
      includeWebsiteSources,
      library?.sources,
      sourceQuery,
      sourceSort,
    ],
  );
  const sourceOriginCounts = useMemo(
    () => countKnowledgeSourceOrigins(library?.sources ?? []),
    [library?.sources],
  );
  const summaries = [
    { label: "全部来源", value: library?.source_count || 0, icon: FileStack },
    {
      label: "处理中版本",
      value: library?.pending_count || 0,
      icon: Inbox,
    },
    {
      label: "已发布来源",
      value: library?.published_count || 0,
      icon: CheckCircle2,
    },
    {
      label: "已发布产品",
      value: library?.confirmed_product_count || 0,
      icon: PackageSearch,
    },
    { label: "去重资产", value: library?.asset_count || 0, icon: ImageIcon },
  ];

  return (
    <main className="mx-auto grid w-full max-w-[1480px] gap-5 px-5 py-6">
      <KnowledgeSectionNav customer={customer} />
      <div className="flex flex-col gap-4 rounded-2xl border bg-card p-5 md:flex-row md:items-end md:justify-between">
        <div>
          <div className="mb-2 flex items-center gap-2 text-xs font-medium uppercase tracking-[0.16em] text-muted-foreground">
            <BookOpenText className="size-4" />
            Published knowledge
          </div>
          <h1 className="text-2xl font-semibold">知识来源</h1>
          <p className="mt-2 max-w-3xl text-sm leading-6 text-muted-foreground">
            官网和上传资料解析成功后直接发布，产品与图片会自动进入产品库；这里负责扫描、
            查看来源预览和撤下异常内容。扫描失败时，上一版继续参与检索。
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
          刷新来源
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

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Globe2 className="size-5" />
            扫描官网知识
          </CardTitle>
          <CardDescription>
            从官网起始页自动读取 Sitemap、WordPress 公共接口和站内链接；产品、公司介绍、联系方式、博客与知识页面都会按内容分类后直接发布。
          </CardDescription>
        </CardHeader>
        <CardContent className="grid gap-3 md:grid-cols-[minmax(0,1fr)_140px_auto] md:items-end">
          <div className="grid gap-2">
            <Label htmlFor="official-site-start-url">官网起始页 URL</Label>
            <Input
              id="official-site-start-url"
              type="url"
              value={startUrl}
              disabled={!editable || Boolean(pending)}
              placeholder="https://www.example.com/"
              onChange={(event) => setStartUrl(event.target.value)}
            />
          </div>
          <div className="grid gap-2">
            <Label htmlFor="official-site-max-pages">最多扫描页面</Label>
            <Input
              id="official-site-max-pages"
              type="number"
              min="1"
              max="500"
              value={maxPages}
              disabled={!editable || Boolean(pending)}
              onChange={(event) => setMaxPages(event.target.value)}
            />
          </div>
          <Button
            type="button"
            className="min-h-11"
            disabled={
              !editable ||
              Boolean(pending) ||
              scanStatus?.status === "running" ||
              !startUrl.trim() ||
              !Number.isInteger(Number(maxPages)) ||
              Number(maxPages) < 1 ||
              Number(maxPages) > 500
            }
            onClick={() => void startOfficialSiteScan()}
          >
            {pending === "official-site-scan" || scanStatus?.status === "running" ? (
              <Loader2 className="animate-spin" />
            ) : (
              <ScanSearch />
            )}
            {scanStatus?.status === "running" ? "扫描中" : "开始扫描"}
          </Button>
          {scanMessage ? (
            <p className="text-sm text-muted-foreground md:col-span-3" role="status">
              {scanMessage}
            </p>
          ) : null}
        </CardContent>
      </Card>

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
          <AlertTitle>当前角色为只读视图</AlertTitle>
          <AlertDescription>
            Reviewer/Viewer 可以查看来源与产品；只有 Editor、Team Lead 或
            Organization Admin 可以扫描、上传或撤下内容。
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
                发布记录放进同一个事务。权限在上传期间被撤销时，不会留下可查询的半成品。
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
            解析成功的资料会直接发布；抓取或解析失败时，原来的在线资料仍然可用。
          </p>
        </div>
        <KnowledgeSourceFilters
          query={sourceQuery}
          sort={sourceSort}
          includeLocal={includeLocalSources}
          includeWebsite={includeWebsiteSources}
          localCount={sourceOriginCounts.local}
          websiteCount={sourceOriginCounts.website}
          totalCount={library?.sources.length ?? 0}
          visibleCount={visibleSources.length}
          onQueryChange={setSourceQuery}
          onSortChange={setSourceSort}
          onIncludeLocalChange={setIncludeLocalSources}
          onIncludeWebsiteChange={setIncludeWebsiteSources}
        />
        {loading ? (
          <div className="flex min-h-44 items-center justify-center rounded-xl border text-sm text-muted-foreground">
            <Loader2 className="mr-2 size-4 animate-spin" />
            正在读取当前项目的知识库…
          </div>
        ) : visibleSources.length ? (
          <div className="grid gap-4 xl:grid-cols-2">
            {visibleSources.map((source) => (
              <SourceReviewCard
                key={`${source.source_id}:${source.pending_snapshot_id || "none"}`}
                source={source}
                projectPath={projectPath}
                deletable={canDeleteKnowledge(role)}
                pending={pending}
                onChanged={run}
              />
            ))}
          </div>
        ) : (
          <div className="flex min-h-44 flex-col items-center justify-center rounded-xl border border-dashed px-6 text-center">
            <Inbox className="mb-3 size-8 text-muted-foreground" />
            <div className="font-medium">
              {library?.sources.length
                ? "没有符合当前筛选条件的来源"
                : "当前项目还没有知识来源"}
            </div>
            <p className="mt-1 max-w-xl text-sm text-muted-foreground">
              {library?.sources.length
                ? "可以调整搜索词、时间排序或来源类型筛选。"
                : "上传私有资料，或从文章 Setup 发起 Product Rediscovery。解析成功后会自动进入检索；异常内容可在来源卡片中撤下，不会改变 Task 产品。"}
            </p>
          </div>
        )}
      </section>

    </main>
  );
}
