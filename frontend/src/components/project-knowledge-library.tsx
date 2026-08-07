"use client";

/* eslint-disable react-hooks/set-state-in-effect */

import {
  AlertCircle,
  BookOpenText,
  CheckCircle2,
  ExternalLink,
  FileStack,
  Globe2,
  ImageIcon,
  Inbox,
  Loader2,
  PackageSearch,
  RefreshCw,
  ScanSearch,
  Upload,
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
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { apiFileUrl, apiGet, apiPost, apiPut, apiUpload } from "@/lib/api";
import type {
  KnowledgeLibrary,
  KnowledgeSourceSummary,
  KnowledgeUploadResult,
  WordPressProbeResult,
  WordPressSyncResult,
} from "@/types";

type ProjectKnowledgeLibraryProps = {
  customer: string;
};

type TrustTier =
  | "hard_fact"
  | "reference_material"
  | "writing_instruction";

const sourceKindLabels: Record<string, string> = {
  private_file: "私有文件",
  product_detail: "产品详情",
  product_category: "产品分类",
  official_blog: "Blog / Guide",
  knowledge_page: "知识页面",
};

const trustTierLabels: Record<string, string> = {
  hard_fact: "硬事实",
  reference_material: "参考资料",
  writing_instruction: "写作指令",
};

const statusLabels: Record<string, string> = {
  inbox: "待确认",
  published: "已发布",
  needs_review: "需复核",
  rejected: "已拒绝",
  stale: "已过期",
};

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "未知错误";
}

function formatDate(value: string | null) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function statusVariant(status: string) {
  if (status === "published" || status === "confirmed") return "default";
  if (status === "rejected" || status === "stale") return "destructive";
  return "secondary";
}

function SourceEvidenceLink({ source }: { source: KnowledgeSourceSummary }) {
  if (source.canonical_url) {
    return (
      <Button
        size="sm"
        variant="ghost"
        render={
          <a
            href={source.canonical_url}
            target="_blank"
            rel="noreferrer"
            aria-label={`打开 ${source.display_name} 官网证据`}
          />
        }
      >
        官网
        <ExternalLink />
      </Button>
    );
  }
  if (source.raw_evidence_url) {
    return (
      <Button
        size="sm"
        variant="ghost"
        render={
          <a
            href={apiFileUrl(source.raw_evidence_url)}
            target="_blank"
            rel="noreferrer"
            aria-label={`打开 ${source.display_name} 原始文件`}
          />
        }
      >
        原文件
        <ExternalLink />
      </Button>
    );
  }
  return <span className="text-xs text-muted-foreground">无原始产物</span>;
}

export function ProjectKnowledgeLibrary({
  customer,
}: ProjectKnowledgeLibraryProps) {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [library, setLibrary] = useState<KnowledgeLibrary | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [publishingSourceId, setPublishingSourceId] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [displayName, setDisplayName] = useState("");
  const [trustTier, setTrustTier] =
    useState<TrustTier>("reference_material");
  const [siteUrl, setSiteUrl] = useState(`https://${customer}`);
  const [categoryUrl, setCategoryUrl] = useState("");
  const [probing, setProbing] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [probeResult, setProbeResult] =
    useState<WordPressProbeResult | null>(null);

  const loadLibrary = useCallback(
    async (background = false) => {
      if (background) setRefreshing(true);
      else setLoading(true);
      setError("");
      try {
        setLibrary(
          await apiGet<KnowledgeLibrary>(
            `/api/knowledge/${encodeURIComponent(customer)}`,
          ),
        );
      } catch (loadError) {
        setError(errorMessage(loadError));
      } finally {
        setLoading(false);
        setRefreshing(false);
      }
    },
    [customer],
  );

  useEffect(() => {
    void loadLibrary();
  }, [loadLibrary]);

  const sortedSources = useMemo(
    () =>
      [...(library?.sources ?? [])].sort((left, right) => {
        if (left.status === right.status) {
          return left.display_name.localeCompare(right.display_name);
        }
        if (left.status === "inbox") return -1;
        if (right.status === "inbox") return 1;
        return left.status.localeCompare(right.status);
      }),
    [library?.sources],
  );

  function selectFile(file: File | null) {
    setSelectedFile(file);
    if (file && !displayName.trim()) {
      setDisplayName(file.name.replace(/\.[^.]+$/, ""));
    }
    setError("");
    setMessage("");
  }

  async function uploadFile() {
    if (!selectedFile) {
      setError("请先选择 DOCX、PDF 或 Excel 文件。");
      return;
    }
    setUploading(true);
    setError("");
    setMessage("");
    try {
      const body = new FormData();
      body.append("file", selectedFile);
      body.append("display_name", displayName.trim() || selectedFile.name);
      body.append("trust_tier", trustTier);
      const result = await apiUpload<KnowledgeUploadResult>(
        `/api/knowledge/${encodeURIComponent(customer)}/sources/upload`,
        body,
      );
      setMessage(
        `${result.message} 已生成 ${result.chunk_count} 个知识块、登记 ${result.asset_count} 个内嵌资产。`,
      );
      setSelectedFile(null);
      setDisplayName("");
      if (fileInputRef.current) fileInputRef.current.value = "";
      await loadLibrary(true);
    } catch (uploadError) {
      setError(errorMessage(uploadError));
    } finally {
      setUploading(false);
    }
  }

  async function approveAndPublish(source: KnowledgeSourceSummary) {
    setPublishingSourceId(source.source_id);
    setError("");
    setMessage("");
    try {
      await apiPut(
        `/api/knowledge/${encodeURIComponent(customer)}/sources/${encodeURIComponent(source.source_id)}/review`,
        {
          source_kind: source.source_kind,
          trust_tier: source.trust_tier,
          decision: "approve",
          reason: "Operator confirmed classification in the project knowledge page.",
        },
      );
      const result = await apiPost<{
        embedding_model: string;
        chunk_count: number;
      }>(
        `/api/knowledge/${encodeURIComponent(customer)}/sources/${encodeURIComponent(source.source_id)}/publish`,
      );
      setMessage(
        `${source.display_name} 已发布：${result.chunk_count} 个知识块，模型 ${result.embedding_model}。`,
      );
      await loadLibrary(true);
    } catch (publishError) {
      setError(errorMessage(publishError));
    } finally {
      setPublishingSourceId("");
    }
  }

  async function probeWordPress() {
    setProbing(true);
    setError("");
    setMessage("");
    try {
      const result = await apiPost<WordPressProbeResult>(
        `/api/knowledge/${encodeURIComponent(customer)}/wordpress/probe`,
        { site_url: siteUrl.trim() || null },
      );
      setProbeResult(result);
      setSiteUrl(result.site_url);
      setMessage(
        result.detected
          ? `已识别 WordPress：${result.route_count} 条 REST 路由。`
          : "未识别到 WordPress REST API；仍可使用官网 HTML 分类与入库。",
      );
    } catch (probeError) {
      setError(errorMessage(probeError));
    } finally {
      setProbing(false);
    }
  }

  async function syncWordPressCategory() {
    if (!categoryUrl.trim()) {
      setError("请填写官网产品分类页 URL。");
      return;
    }
    setSyncing(true);
    setError("");
    setMessage("");
    try {
      const result = await apiPost<WordPressSyncResult>(
        `/api/knowledge/${encodeURIComponent(customer)}/wordpress/sync`,
        {
          site_url: siteUrl.trim() || null,
          category_url: categoryUrl.trim(),
          max_products: 12,
        },
      );
      const assetCount = result.products.reduce(
        (total, item) => total + item.asset_count,
        0,
      );
      setMessage(
        `官网同步完成：1 个分类页、${result.products.length} 个产品详情页、${assetCount} 张去重原图进入 Inbox。${result.warnings.length ? ` 另有 ${result.warnings.length} 条警告。` : ""}`,
      );
      await loadLibrary(true);
    } catch (syncError) {
      setError(errorMessage(syncError));
    } finally {
      setSyncing(false);
    }
  }

  return (
    <main className="min-h-screen bg-background text-foreground">
      <div className="border-b bg-card">
        <div className="mx-auto flex max-w-[1480px] flex-col gap-4 px-5 py-5 md:flex-row md:items-end md:justify-between">
          <div className="px-1">
            <div className="mb-2 flex items-center gap-2 text-xs font-medium uppercase tracking-[0.16em] text-muted-foreground">
              <BookOpenText className="size-4" />
              Project knowledge
            </div>
            <h1 className="text-xl font-semibold">知识库</h1>
            <p className="mt-1 max-w-3xl text-sm text-muted-foreground">
              管理私有资料、官网来源、产品证据和图片资产。新资料先进入 Research
              Inbox，确认并完成向量后才会参与正式检索。
            </p>
          </div>
          <Button
            variant="outline"
            onClick={() => void loadLibrary(true)}
            disabled={refreshing || loading}
          >
            {refreshing ? <Loader2 className="animate-spin" /> : <RefreshCw />}
            刷新
          </Button>
        </div>
      </div>

      <div className="mx-auto grid max-w-[1480px] gap-5 px-5 py-6">
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
            <AlertTitle>资料已进入待确认区</AlertTitle>
            <AlertDescription>{message}</AlertDescription>
          </Alert>
        ) : null}

        <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
          {[
            {
              label: "全部来源",
              value: library?.source_count ?? 0,
              icon: FileStack,
            },
            {
              label: "待审核资料",
              value: library?.inbox_count ?? 0,
              icon: Inbox,
            },
            {
              label: "已发布来源",
              value: library?.published_count ?? 0,
              icon: CheckCircle2,
            },
            {
              label: "已确认产品",
              value: library?.confirmed_product_count ?? 0,
              icon: PackageSearch,
            },
            {
              label: "去重资产",
              value: library?.asset_count ?? 0,
              icon: ImageIcon,
            },
          ].map((item) => (
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
                <span className="flex size-9 items-center justify-center rounded-lg bg-muted text-muted-foreground">
                  <item.icon className="size-4" />
                </span>
              </CardContent>
            </Card>
          ))}
        </section>

        <KnowledgeReviewGuide />

        <section className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_360px]">
          <Card className="min-w-0">
            <CardHeader>
              <CardTitle>来源与分类证据</CardTitle>
              <CardDescription>
                展示来源为什么被归类、当前发布状态，以及可回溯的原始证据。
              </CardDescription>
            </CardHeader>
            <CardContent>
              {loading ? (
                <div className="flex min-h-48 items-center justify-center text-sm text-muted-foreground">
                  <Loader2 className="mr-2 size-4 animate-spin" />
                  正在读取项目知识库…
                </div>
              ) : sortedSources.length ? (
                <div className="overflow-x-auto rounded-lg border">
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>来源</TableHead>
                        <TableHead>分类与可信度</TableHead>
                        <TableHead>状态</TableHead>
                        <TableHead>版本 / 段落 / 图片</TableHead>
                        <TableHead>最近入库</TableHead>
                        <TableHead className="text-right">证据</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {sortedSources.map((source) => (
                        <TableRow key={source.source_id}>
                          <TableCell className="min-w-64 align-top">
                            <div className="font-medium">{source.display_name}</div>
                            <div className="mt-1 break-all font-mono text-[11px] text-muted-foreground">
                              {source.source_id}
                            </div>
                            {source.classification_reason ? (
                              <div className="mt-2 text-xs leading-5 text-muted-foreground">
                                系统判断：{source.classification_reason}
                              </div>
                            ) : null}
                          </TableCell>
                          <TableCell className="align-top">
                            <div className="flex flex-wrap gap-1.5">
                              <Badge variant="outline">
                                {sourceKindLabels[source.source_kind] ??
                                  source.source_kind}
                              </Badge>
                              <Badge variant="secondary">
                                {trustTierLabels[source.trust_tier] ??
                                  source.trust_tier}
                              </Badge>
                            </div>
                          </TableCell>
                          <TableCell className="align-top">
                            <Badge variant={statusVariant(source.status)}>
                              {statusLabels[source.status] ?? source.status}
                            </Badge>
                          </TableCell>
                          <TableCell className="whitespace-nowrap align-top text-xs text-muted-foreground">
                            {source.snapshot_count} / {source.chunk_count} /{" "}
                            {source.asset_count}
                          </TableCell>
                          <TableCell className="whitespace-nowrap align-top text-xs text-muted-foreground">
                            {formatDate(source.latest_fetched_at)}
                          </TableCell>
                          <TableCell className="text-right align-top">
                            <div className="flex justify-end gap-1">
                              {source.status === "inbox" ? (
                                <Button
                                  size="sm"
                                  onClick={() => void approveAndPublish(source)}
                                  disabled={
                                    Boolean(publishingSourceId) || source.chunk_count === 0
                                  }
                                >
                                  {publishingSourceId === source.source_id ? (
                                    <Loader2 className="animate-spin" />
                                  ) : (
                                    <CheckCircle2 />
                                  )}
                                  通过并发布
                                </Button>
                              ) : null}
                              <SourceEvidenceLink source={source} />
                            </div>
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </div>
              ) : (
                <div className="flex min-h-52 flex-col items-center justify-center rounded-lg border border-dashed px-6 text-center">
                  <Inbox className="mb-3 size-8 text-muted-foreground" />
                  <div className="font-medium">待审核资料还是空的</div>
                  <p className="mt-1 max-w-md text-sm text-muted-foreground">
                    上传私有资料，或从右侧探测并同步 WordPress 官网产品分类页。
                  </p>
                </div>
              )}
            </CardContent>
          </Card>

          <div className="grid h-fit gap-5">
            <Card>
              <CardHeader>
                <CardTitle>同步 WordPress 官网</CardTitle>
                <CardDescription>
                  先探测站点，再从一个产品分类页同步详情、事实与原图。
                </CardDescription>
              </CardHeader>
              <CardContent className="grid gap-4">
                <div className="grid gap-2">
                  <Label htmlFor="knowledge-site-url">官网地址</Label>
                  <Input
                    id="knowledge-site-url"
                    value={siteUrl}
                    onChange={(event) => setSiteUrl(event.target.value)}
                    placeholder="https://www.example.com"
                  />
                </div>
                <Button
                  variant="outline"
                  onClick={() => void probeWordPress()}
                  disabled={probing || syncing}
                >
                  {probing ? (
                    <Loader2 className="animate-spin" />
                  ) : (
                    <ScanSearch />
                  )}
                  {probing ? "探测中…" : "探测 WordPress"}
                </Button>
                {probeResult ? (
                  <div className="rounded-lg border bg-muted/40 p-3 text-xs leading-5">
                    <div className="flex items-center gap-2 font-medium">
                      <Globe2 className="size-4" />
                      {probeResult.detected ? "已识别 WordPress" : "未识别 REST API"}
                    </div>
                    <p className="mt-1 text-muted-foreground">
                      {probeResult.reason}
                    </p>
                  </div>
                ) : null}
                <div className="grid gap-2">
                  <Label htmlFor="knowledge-category-url">产品分类页 URL</Label>
                  <Input
                    id="knowledge-category-url"
                    value={categoryUrl}
                    onChange={(event) => setCategoryUrl(event.target.value)}
                    placeholder="https://www.example.com/category/products/"
                  />
                  <p className="text-xs leading-5 text-muted-foreground">
                    同步结果只进入 Inbox，不会自动发布或确认产品。
                  </p>
                </div>
                <Button
                  onClick={() => void syncWordPressCategory()}
                  disabled={!categoryUrl.trim() || probing || syncing}
                >
                  {syncing ? (
                    <Loader2 className="animate-spin" />
                  ) : (
                    <PackageSearch />
                  )}
                  {syncing ? "抓取、分类并入库中…" : "同步分类与产品"}
                </Button>
              </CardContent>
            </Card>

            <Card>
              <CardHeader>
                <CardTitle>上传私有资料</CardTitle>
                <CardDescription>
                  支持 DOCX、PDF、XLSX 和 XLSM，单文件最大 25 MB。
                </CardDescription>
              </CardHeader>
              <CardContent className="grid gap-4">
                <div className="grid gap-2">
                  <Label htmlFor="knowledge-file">资料文件</Label>
                  <Input
                    ref={fileInputRef}
                    id="knowledge-file"
                    type="file"
                    accept=".docx,.pdf,.xlsx,.xlsm"
                    onChange={(event) =>
                      selectFile(event.target.files?.[0] ?? null)
                    }
                  />
                </div>
                <div className="grid gap-2">
                  <Label htmlFor="knowledge-name">显示名称</Label>
                  <Input
                    id="knowledge-name"
                    value={displayName}
                    onChange={(event) => setDisplayName(event.target.value)}
                    placeholder="例如：2026 产品规格表"
                  />
                </div>
                <div className="grid gap-2">
                  <Label htmlFor="knowledge-trust">建议信任层级</Label>
                  <select
                    id="knowledge-trust"
                    value={trustTier}
                    onChange={(event) =>
                      setTrustTier(event.target.value as TrustTier)
                    }
                    className="h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm outline-none focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50"
                  >
                    <option value="reference_material">参考资料</option>
                    <option value="hard_fact">硬事实</option>
                    <option value="writing_instruction">写作指令</option>
                  </select>
                  <p className="text-xs leading-5 text-muted-foreground">
                    这里只是运营建议。资料仍会保持待确认，不会自动参与检索。
                  </p>
                </div>
                <Button
                  onClick={() => void uploadFile()}
                  disabled={!selectedFile || uploading}
                >
                  {uploading ? <Loader2 className="animate-spin" /> : <Upload />}
                  {uploading ? "解析并入库中…" : "解析并加入 Inbox"}
                </Button>
              </CardContent>
            </Card>
          </div>
        </section>

        {library?.products.length ? (
          <Card>
            <CardHeader>
              <CardTitle>产品目录</CardTitle>
              <CardDescription>
                产品身份与页面版本分离；只有详情页证据充分的产品才能确认。
              </CardDescription>
            </CardHeader>
            <CardContent className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
              {library.products.map((product) => (
                <div
                  key={product.product_id}
                  className="rounded-lg border bg-card p-4"
                >
                  <div className="flex items-start justify-between gap-3">
                    <div>
                      <div className="font-medium">{product.name}</div>
                      <div className="mt-1 font-mono text-[11px] text-muted-foreground">
                        {product.product_id}
                      </div>
                    </div>
                    <Badge variant={statusVariant(product.status)}>
                      {product.status === "confirmed"
                        ? "已确认"
                        : statusLabels[product.status] ?? product.status}
                    </Badge>
                  </div>
                  <div className="mt-3 text-xs text-muted-foreground">
                    {product.category_path.length
                      ? product.category_path.join(" / ")
                      : "尚未确认分类路径"}
                  </div>
                </div>
              ))}
            </CardContent>
          </Card>
        ) : null}
      </div>
    </main>
  );
}
