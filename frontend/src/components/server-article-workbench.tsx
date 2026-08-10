"use client";

/* eslint-disable react-hooks/set-state-in-effect */

import {
  AlertCircle,
  ArrowLeft,
  CheckCircle2,
  Download,
  FileCheck2,
  FileText,
  ImageIcon,
  Loader2,
  Package,
  RefreshCw,
  ShieldCheck,
  Sparkles,
} from "lucide-react";
import Link from "next/link";
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
import { Separator } from "@/components/ui/separator";
import { Textarea } from "@/components/ui/textarea";
import { ServerOutlineHistory } from "@/components/server-outline-history";
import { ServerHeroAssetPicker } from "@/components/server-hero-asset-picker";
import { ServerProductRediscoveryPanel } from "@/components/server-product-rediscovery-panel";
import { ServerResearchWorkspace } from "@/components/server-research-workspace";
import { ServerSectionRewritePanel } from "@/components/server-section-rewrite-panel";
import { ServerSeoReviewPanel } from "@/components/server-seo-review-panel";
import { ServerTaskResetPanel } from "@/components/server-task-reset-panel";
import { ServerWritingRequirementsPanel } from "@/components/server-writing-requirements-panel";
import { apiGet, apiPost, apiPut, apiUpload } from "@/lib/api";
import { cn } from "@/lib/utils";
import type {
  AccessibleProject,
  ProjectAssetDownload,
  ServerProjectCatalog,
  TaskRecord,
  WorkflowStatus,
} from "@/types";

type WorkbenchStep = "setup" | "outline" | "draft" | "review" | "delivery";

type ServerProjectJob = {
  job_id: string;
  task_id: string;
  operation: string;
  status: string;
  result_revision: number | null;
  has_error: boolean;
};

const STEPS: Array<{
  id: WorkbenchStep;
  label: string;
  description: string;
}> = [
  { id: "setup", label: "1. 内容准备", description: "标题、产品与本篇写作要求" },
  { id: "outline", label: "2. 大纲", description: "生成草稿并人工确认" },
  { id: "draft", label: "3. 初稿", description: "正文生成与 AI-rate 初检" },
  { id: "review", label: "4. 审阅", description: "人化、终检和链接恢复" },
  { id: "delivery", label: "5. 图片与交付", description: "私有图片、Word、TDK、ZIP" },
];

const STATUS_LABELS: Record<WorkflowStatus, string> = {
  new: "待生成标题",
  titles_ready: "待选择标题",
  title_selected: "待确认产品",
  outline_ready: "待审阅大纲",
  outline_confirmed: "待生成初稿",
  draft_ready: "待初检",
  initial_ai_checked: "待人化",
  humanized_ready: "待终检",
  final_ai_checked: "待恢复链接",
  links_verified: "待准备图片",
  images_ready: "待导出",
  docx_exported: "已导出",
};

const TERMINAL_JOB_STATUSES = new Set([
  "succeeded",
  "failed",
  "cancelled",
  "canceled",
  "conflict",
]);

function isWorkbenchStep(value: string | undefined): value is WorkbenchStep {
  return STEPS.some((step) => step.id === value);
}

function recommendedStep(status: WorkflowStatus): WorkbenchStep {
  if (status === "new" || status === "titles_ready") return "setup";
  if (status === "title_selected" || status === "outline_ready") {
    return "outline";
  }
  if (status === "outline_confirmed" || status === "draft_ready") {
    return "draft";
  }
  if (
    status === "initial_ai_checked" ||
    status === "humanized_ready" ||
    status === "final_ai_checked"
  ) {
    return "review";
  }
  return "delivery";
}

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "操作失败，请重试。";
}

function canEdit(role: AccessibleProject["effective_role"] | null) {
  return role === "org_admin" || role === "team_lead" || role === "editor";
}

function canReview(role: AccessibleProject["effective_role"] | null) {
  return role !== null && role !== "viewer";
}

function roleLabel(role: AccessibleProject["effective_role"] | null) {
  if (role === "org_admin") return "组织管理员";
  if (role === "team_lead") return "团队负责人";
  if (role === "editor") return "编辑";
  if (role === "reviewer") return "复核员";
  if (role === "viewer") return "只读成员";
  return "权限待确认";
}

function wait(milliseconds: number) {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

function articleFor(task: TaskRecord) {
  return (
    task.final_article ||
    task.linked_article ||
    task.humanized_article ||
    task.initial_article ||
    task.article ||
    ""
  );
}

function taskProductIds(task: TaskRecord) {
  return task.products.flatMap((product) =>
    product.product_id ? [product.product_id] : [],
  );
}

function productSelectionKey(productIds: string[]) {
  return JSON.stringify([...productIds].sort());
}

export function ServerArticleWorkbench({
  customer,
  taskId,
  initialStep,
}: {
  customer: string;
  taskId: string;
  initialStep?: string;
}) {
  const [task, setTask] = useState<TaskRecord | null>(null);
  const [catalog, setCatalog] = useState<ServerProjectCatalog | null>(null);
  const [role, setRole] = useState<AccessibleProject["effective_role"] | null>(
    null,
  );
  const [step, setStep] = useState<WorkbenchStep>(
    isWorkbenchStep(initialStep) ? initialStep : "setup",
  );
  const [loading, setLoading] = useState(true);
  const [pending, setPending] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [selectedProductIds, setSelectedProductIds] = useState<string[]>([]);
  const [productSelectionConflict, setProductSelectionConflict] =
    useState(false);
  const [outlineDraft, setOutlineDraft] = useState("");
  const [humanizedDraft, setHumanizedDraft] = useState("");
  const [initialScore, setInitialScore] = useState("");
  const [initialReport, setInitialReport] = useState("");
  const [initialScreenshot, setInitialScreenshot] = useState<File | null>(null);
  const [finalScore, setFinalScore] = useState("");
  const [finalReport, setFinalReport] = useState("");
  const [finalScreenshot, setFinalScreenshot] = useState<File | null>(null);
  const [heroAssetId, setHeroAssetId] = useState("");
  const [productAssetIds, setProductAssetIds] = useState<Record<string, string>>(
    {},
  );
  const [productAnchors, setProductAnchors] = useState<Record<string, string>>(
    {},
  );
  const [writingSettingsDirty, setWritingSettingsDirty] = useState(false);
  const [writingSettingsPromptBlocked, setWritingSettingsPromptBlocked] =
    useState(true);
  const preserveDraftsForRevisionRef = useRef<number | null>(null);
  const recommendedTaskIdRef = useRef("");
  const loadRequestRef = useRef(0);
  const productBaselineRef = useRef<string | null>(null);
  const productDraftIdsRef = useRef<string[]>([]);

  const projectApi = `/api/projects/${encodeURIComponent(customer)}`;
  const taskApi = `${projectApi}/tasks/${encodeURIComponent(taskId)}`;
  const workbenchScope = `${customer}\n${taskId}`;
  const activeWorkbenchScopeRef = useRef("");

  useEffect(() => {
    activeWorkbenchScopeRef.current = workbenchScope;
    return () => {
      if (activeWorkbenchScopeRef.current === workbenchScope) {
        activeWorkbenchScopeRef.current = "";
      }
    };
  }, [workbenchScope]);

  const load = useCallback(async () => {
    const requestScope = workbenchScope;
    if (activeWorkbenchScopeRef.current !== requestScope) return;
    const requestId = loadRequestRef.current + 1;
    loadRequestRef.current = requestId;
    setLoading(true);
    setError("");
    try {
      const [nextTask, projects] = await Promise.all([
        apiGet<TaskRecord>(`${taskApi}`),
        apiGet<AccessibleProject[]>("/api/projects"),
      ]);
      const imageProductIds = taskProductIds(nextTask).join(",");
      const nextCatalog = await apiGet<ServerProjectCatalog>(
        `${projectApi}/catalog?image_limit=100&image_product_ids=${encodeURIComponent(imageProductIds)}`,
      );
      if (
        activeWorkbenchScopeRef.current !== requestScope ||
        loadRequestRef.current !== requestId
      ) {
        return;
      }
      const project = projects.find((item) => item.project_id === customer);
      setTask(nextTask);
      setRole(project?.effective_role ?? null);
      setCatalog(nextCatalog);
    } catch (reason) {
      if (
        activeWorkbenchScopeRef.current !== requestScope ||
        loadRequestRef.current !== requestId
      ) {
        return;
      }
      setError(errorMessage(reason));
    } finally {
      if (
        activeWorkbenchScopeRef.current === requestScope &&
        loadRequestRef.current === requestId
      ) {
        setLoading(false);
      }
    }
  }, [customer, projectApi, taskApi, workbenchScope]);

  useEffect(() => {
    setTask(null);
    setSelectedProductIds([]);
    productDraftIdsRef.current = [];
    productBaselineRef.current = null;
    setProductSelectionConflict(false);
    setPending("");
    setMessage("");
    setWritingSettingsDirty(false);
    setWritingSettingsPromptBlocked(true);
    void load();
    return () => {
      loadRequestRef.current += 1;
    };
  }, [load]);

  useEffect(() => {
    if (!task) return;
    const preserveDrafts =
      preserveDraftsForRevisionRef.current === (task.revision ?? 0);
    preserveDraftsForRevisionRef.current = null;
    const officialProductIds = taskProductIds(task);
    const nextProductBaseline = productSelectionKey(officialProductIds);
    const previousProductBaseline = productBaselineRef.current;
    const currentProductDraft = productDraftIdsRef.current;
    const currentProductDraftKey = productSelectionKey(currentProductDraft);
    const productDraftWasDirty =
      previousProductBaseline !== null &&
      currentProductDraftKey !== previousProductBaseline;
    if (
      !productDraftWasDirty ||
      currentProductDraftKey === nextProductBaseline
    ) {
      productDraftIdsRef.current = officialProductIds;
      setSelectedProductIds(officialProductIds);
      setProductSelectionConflict(false);
    } else if (previousProductBaseline !== nextProductBaseline) {
      setProductSelectionConflict(true);
    }
    productBaselineRef.current = nextProductBaseline;
    if (!preserveDrafts) {
      setOutlineDraft(task.outline_draft || task.outline || "");
      setHumanizedDraft(task.humanized_article || task.initial_article || "");
      setInitialScore(
        task.initial_ai_check?.score === null ||
          task.initial_ai_check?.score === undefined
          ? ""
          : String(task.initial_ai_check.score),
      );
      setInitialReport(task.initial_ai_check?.report || "");
      setFinalScore(
        task.final_ai_check?.score === null ||
          task.final_ai_check?.score === undefined
          ? ""
          : String(task.final_ai_check.score),
      );
      setFinalReport(task.final_ai_check?.report || "");
      setProductAnchors(
        Object.fromEntries(
          task.products.flatMap((product) =>
            product.product_id
              ? [[product.product_id, product.name] as const]
              : [],
          ),
        ),
      );
      setHeroAssetId(
        task.images?.find((image) => image.role === "hero")
          ?.source_asset_id || "",
      );
      setProductAssetIds(
        Object.fromEntries(
          task.products.flatMap((product) => {
            if (!product.product_id) return [];
            const prepared = task.images?.find(
              (image) =>
                image.role === "product" &&
                image.product_id === product.product_id,
            );
            const assetId =
              prepared?.source_asset_id || product.selected_asset_id || "";
            return assetId ? [[product.product_id, assetId] as const] : [];
          }),
        ),
      );
    }
    if (recommendedTaskIdRef.current !== task.id) {
      recommendedTaskIdRef.current = task.id;
      if (!isWorkbenchStep(initialStep)) {
        setStep(recommendedStep(task.status));
      }
    }
  }, [initialStep, task]);

  useEffect(() => {
    if (!catalog?.image_assets.length) return;
    setHeroAssetId((current) =>
      catalog.image_assets.some((asset) => asset.asset_id === current)
        ? current
        : catalog.image_assets[0].asset_id,
    );
    setProductAssetIds((current) =>
      Object.fromEntries(
        (task?.products || []).flatMap((product) => {
          if (!product.product_id) return [];
          const options = catalog.image_assets.filter(
            (asset) => asset.product_id === product.product_id,
          );
          if (!options.length) return [];
          const selected = current[product.product_id];
          return [[
            product.product_id,
            options.some((asset) => asset.asset_id === selected)
              ? selected
              : options[0].asset_id,
          ] as const];
        }),
      ),
    );
  }, [catalog, task?.products]);

  function acceptWritingSettingsTask(updated: TaskRecord) {
    preserveDraftsForRevisionRef.current = updated.revision ?? 0;
    setTask(updated);
  }

  async function runAction(
    label: string,
    action: () => Promise<unknown>,
    successMessage = `${label}完成。`,
  ): Promise<boolean> {
    const actionScope = workbenchScope;
    if (activeWorkbenchScopeRef.current !== actionScope) return false;
    setPending(label);
    setError("");
    setMessage("");
    try {
      await action();
      if (activeWorkbenchScopeRef.current !== actionScope) return false;
      setMessage(successMessage);
      await load();
      return true;
    } catch (reason) {
      if (activeWorkbenchScopeRef.current !== actionScope) return false;
      setError(errorMessage(reason));
      return false;
    } finally {
      if (activeWorkbenchScopeRef.current === actionScope) setPending("");
    }
  }

  async function runJob(
    label: string,
    endpoint: string,
    payload: Record<string, unknown> = {},
  ) {
    if (!task) return;
    const jobScope = workbenchScope;
    if (
      (writingSettingsDirty || writingSettingsPromptBlocked) &&
      (endpoint === "outline" ||
        endpoint === "article" ||
        endpoint === "article/rewrite")
    ) {
      setMessage("");
      setError(
        writingSettingsDirty
          ? "写作要求有未保存修改，请先在“内容准备”中保存，再开始生成。"
          : "写作要求中的 Prompt 尚未通过可用性检查，请先返回“内容准备”处理。",
      );
      return;
    }
    await runAction(
      label,
      async () => {
        const queued = await apiPost<ServerProjectJob>(`${taskApi}/${endpoint}`, {
          revision: task.revision ?? 0,
          ...payload,
        });
        if (activeWorkbenchScopeRef.current !== jobScope) return;
        const statusPath = `${taskApi}/${endpoint}/jobs/${encodeURIComponent(queued.job_id)}`;
        for (let attempt = 0; attempt < 180; attempt += 1) {
          if (activeWorkbenchScopeRef.current !== jobScope) return;
          const current = await apiGet<ServerProjectJob>(statusPath);
          if (TERMINAL_JOB_STATUSES.has(current.status)) {
            if (current.status !== "succeeded") {
              throw new Error(
                `${label}未成功完成。Job ${current.job_id} 已进入 ${current.status} 状态。`,
              );
            }
            return;
          }
          await wait(1000);
        }
        throw new Error(
          `${label}仍在后台运行。请稍后刷新任务；Job 不会因页面等待超时而取消。`,
        );
      },
      `${label}完成，已读取最新 Task Revision。`,
    );
  }

  async function uploadScreenshot(kind: "initial" | "final", file: File) {
    if (!task) return;
    const form = new FormData();
    form.append("file", file);
    await apiUpload<TaskRecord>(
      `${taskApi}/checks/${kind}-ai/screenshot?revision=${task.revision ?? 0}`,
      form,
    );
  }

  async function download(label: string, endpoint: string) {
    await runAction(label, async () => {
      const asset = await apiGet<ProjectAssetDownload>(`${taskApi}/${endpoint}`);
      if (!asset.url) throw new Error("Server 未返回可用的短期下载地址。");
      window.location.assign(asset.url);
    });
  }

  const confirmedProducts = useMemo(() => catalog?.products || [], [catalog]);
  const recommendedProductIds = useMemo(() => {
    const available = new Set(
      confirmedProducts.map((product) => product.product_id),
    );
    return new Set(
      (task?.product_candidate_ids || []).filter((productId) =>
        available.has(productId),
      ),
    );
  }, [confirmedProducts, task?.product_candidate_ids]);
  const productSelectionDirty =
    task !== null &&
    productSelectionKey(selectedProductIds) !==
    productSelectionKey(taskProductIds(task));

  function resetProductDraftToServer() {
    if (!task) return;
    const officialProductIds = taskProductIds(task);
    productDraftIdsRef.current = officialProductIds;
    productBaselineRef.current = productSelectionKey(officialProductIds);
    setSelectedProductIds(officialProductIds);
    setProductSelectionConflict(false);
  }

  async function saveProductSelection(payload: {
    revision: number;
    product_ids: string[];
  }) {
    const updated = await apiPut<TaskRecord>(
      `${taskApi}/products`,
      payload,
    );
    const officialProductIds = taskProductIds(updated);
    productDraftIdsRef.current = officialProductIds;
    productBaselineRef.current = productSelectionKey(officialProductIds);
    setSelectedProductIds(officialProductIds);
    setProductSelectionConflict(false);
    setTask(updated);
    return updated;
  }

  const allowed = new Set(task?.allowed_actions || []);
  const editAllowed = canEdit(role);
  const reviewAllowed = canReview(role);
  const hasArticleDraft = Boolean(
    (
      task?.initial_article ||
      task?.raw_draft_article ||
      task?.article ||
      ""
    ).trim(),
  );
  const articleJobLabel = hasArticleDraft ? "重新生成正文" : "生成文章初稿";
  const articleJobEndpoint = hasArticleDraft ? "article/rewrite" : "article";
  const hasConfirmedOutline = Boolean(
    task?.outline?.trim() &&
      task.article_versions?.some(
        (version) =>
          version.kind === "outline" &&
          version.source_kind === "manual_confirmed",
      ),
  );
  const titleGenerationBlockedReason = !editAllowed
    ? `当前账号是“${roleLabel(role)}”，需要“编辑”或更高项目权限才能生成标题。`
    : !allowed.has("generate_titles")
      ? "当前任务状态暂不允许生成标题，请先刷新任务状态。"
      : "";

  if (loading && !task) {
    return (
      <main className="flex min-h-[55dvh] items-center justify-center px-5">
        <div
          className="flex items-center gap-3 rounded-xl border bg-card px-5 py-4 text-sm text-muted-foreground"
          role="status"
          aria-live="polite"
        >
          <Loader2 className="size-4 animate-spin" />
          正在读取 Project-scoped Task…
        </div>
      </main>
    );
  }

  if (!task) {
    return (
      <main className="mx-auto max-w-3xl px-5 py-8">
        <Alert variant="destructive">
          <AlertCircle />
          <AlertTitle>无法打开文章工作台</AlertTitle>
          <AlertDescription>{error || "Task 不存在或无权访问。"}</AlertDescription>
        </Alert>
      </main>
    );
  }

  return (
    <main className="min-h-screen bg-background text-foreground">
      <header className="border-b bg-card">
        <div className="mx-auto grid max-w-[1480px] gap-4 px-5 py-5">
          <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
            <div className="min-w-0">
              <Button
                nativeButton={false}
                variant="ghost"
                size="sm"
                className="-ml-2 mb-2"
                render={
                  <Link
                    href={`/projects/${encodeURIComponent(customer)}/articles`}
                  />
                }
              >
                <ArrowLeft />
                返回文章任务
              </Button>
              <div className="flex flex-wrap items-center gap-2">
                <h1 className="max-w-4xl text-xl font-semibold">
                  {task.selected_title || task.topic}
                </h1>
                <Badge variant="outline">
                  <ShieldCheck />
                  {roleLabel(role)}
                </Badge>
                <Badge>{STATUS_LABELS[task.status]}</Badge>
              </div>
              <p className="mt-2 text-sm leading-6 text-muted-foreground">
                topic_{String(task.topic_index).padStart(3, "0")} · Revision{" "}
                <span className="font-mono">{task.revision ?? 0}</span> ·{" "}
                {task.topic}
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
              刷新
            </Button>
          </div>
        </div>
      </header>

      <div className="mx-auto grid max-w-[1480px] gap-4 px-5 py-5">
        {error && (
          <Alert variant="destructive">
            <AlertCircle />
            <AlertTitle>操作失败</AlertTitle>
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
        {message && (
          <Alert>
            <CheckCircle2 />
            <AlertTitle>操作完成</AlertTitle>
            <AlertDescription>{message}</AlertDescription>
          </Alert>
        )}
        {task.workflow_error && (
          <Alert variant="destructive">
            <AlertCircle />
            <AlertTitle>{task.workflow_error.stage} 阶段需要处理</AlertTitle>
            <AlertDescription>{task.workflow_error.message}</AlertDescription>
          </Alert>
        )}
        {!editAllowed && (
          <Alert>
            <ShieldCheck />
            <AlertTitle>
              {reviewAllowed ? "当前为复核权限" : "当前为只读权限"}
            </AlertTitle>
            <AlertDescription>
              页面状态只用于可用性提示；每个命令仍由后端在事务内重新检查实时角色。
            </AlertDescription>
          </Alert>
        )}

        <nav
          className="grid gap-2 sm:grid-cols-2 xl:grid-cols-5"
          aria-label="Server 文章工作流"
        >
          {STEPS.map((item) => (
            <button
              key={item.id}
              type="button"
              onClick={() => setStep(item.id)}
              aria-current={step === item.id ? "step" : undefined}
              className={cn(
                "min-h-16 cursor-pointer rounded-xl border bg-card px-4 py-3 text-left outline-none transition-colors hover:bg-accent/45 focus-visible:ring-2 focus-visible:ring-ring",
                step === item.id && "border-primary bg-accent/55 ring-1 ring-primary/20",
              )}
            >
              <span className="block text-sm font-semibold">{item.label}</span>
              <span className="mt-1 block text-xs leading-5 text-muted-foreground">
                {item.description}
              </span>
            </button>
          ))}
        </nav>

        {step === "setup" && (
          <div className="grid items-start gap-4 xl:grid-cols-2">
            <Card>
              <CardHeader className="border-b">
                <CardTitle>标题候选</CardTitle>
                <CardDescription>
                  生成 Job 固定服务端模板与 Published Chunk；选择只提交候选索引。
                </CardDescription>
              </CardHeader>
              <CardContent className="grid gap-3">
                {task.title_candidates.length ? (
                  task.title_candidates.map((title, index) => (
                    <button
                      key={`${index}-${title}`}
                      type="button"
                      disabled={
                        Boolean(pending) ||
                        !editAllowed ||
                        !allowed.has("select_title")
                      }
                      onClick={() =>
                        void runAction("选择标题", () =>
                          apiPut<TaskRecord>(`${taskApi}/selected-title`, {
                            revision: task.revision ?? 0,
                            candidate_index: index,
                          }),
                        )
                      }
                      className={cn(
                        "min-h-12 cursor-pointer rounded-lg border px-4 py-3 text-left text-sm outline-none transition-colors hover:border-primary hover:bg-accent/40 focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50",
                        task.selected_title === title &&
                          "border-primary bg-accent/55",
                      )}
                    >
                      <span className="mr-2 font-mono text-xs text-muted-foreground">
                        {String(index + 1).padStart(2, "0")}
                      </span>
                      {title}
                    </button>
                  ))
                ) : (
                  <p className="rounded-lg border border-dashed px-4 py-8 text-center text-sm text-muted-foreground">
                    尚无标题候选
                  </p>
                )}
                <Button
                  type="button"
                  className="min-h-11"
                  aria-describedby={
                    titleGenerationBlockedReason
                      ? `title-generation-help-${task.id}`
                      : undefined
                  }
                  disabled={
                    Boolean(pending) ||
                    !editAllowed ||
                    !allowed.has("generate_titles")
                  }
                  onClick={() => void runJob("生成标题候选", "titles")}
                >
                  {pending === "生成标题候选" ? (
                    <Loader2 className="animate-spin" />
                  ) : (
                    <Sparkles />
                  )}
                  {task.title_candidates.length ? "重新生成候选" : "生成候选"}
                </Button>
                {titleGenerationBlockedReason ? (
                  <p
                    id={`title-generation-help-${task.id}`}
                    className="text-xs leading-5 text-amber-700 dark:text-amber-300"
                    role="alert"
                  >
                    {titleGenerationBlockedReason}
                  </p>
                ) : null}
              </CardContent>
            </Card>

            <Card>
              <CardHeader className="border-b">
                <CardTitle>正式产品选择</CardTitle>
                <CardDescription>
                  只列出 Server Knowledge Library 中 confirmed 产品；保存时只提交最多
                  3 个 Product ID。
                </CardDescription>
              </CardHeader>
              <CardContent className="grid gap-3">
                <Button
                  type="button"
                  variant="secondary"
                  className="min-h-11"
                  disabled={
                    Boolean(pending) ||
                    !editAllowed ||
                    !allowed.has("generate_products") ||
                    !confirmedProducts.length ||
                    productSelectionDirty ||
                    productSelectionConflict
                  }
                  onClick={() => void runJob("生成产品候选", "products")}
                >
                  {pending === "生成产品候选" ? (
                    <Loader2 className="animate-spin" />
                  ) : (
                    <Sparkles />
                  )}
                  {recommendedProductIds.size
                    ? "重新生成产品候选"
                    : "生成产品候选"}
                </Button>
                {recommendedProductIds.size > 0 && (
                  <p className="text-sm text-muted-foreground">
                    已生成 {recommendedProductIds.size}
                    个当前目录内的推荐；点击勾选只修改本地草稿，仍需显式保存。
                  </p>
                )}
                {productSelectionConflict && (
                  <Alert variant="destructive">
                    <AlertCircle />
                    <AlertTitle>服务器产品选择已变化</AlertTitle>
                    <AlertDescription className="grid gap-3">
                      <span>
                        本地未保存选择未被覆盖。请先载入最新服务器选择，再重新勾选并保存。
                      </span>
                      <Button
                        type="button"
                        size="sm"
                        variant="outline"
                        className="w-fit"
                        disabled={Boolean(pending)}
                        onClick={resetProductDraftToServer}
                      >
                        载入服务器选择
                      </Button>
                    </AlertDescription>
                  </Alert>
                )}
                {!productSelectionConflict && productSelectionDirty && (
                  <Alert>
                    <AlertCircle />
                    <AlertTitle>产品选择尚未保存</AlertTitle>
                    <AlertDescription>
                      生成新候选已暂停；保存或还原当前选择后可继续。
                    </AlertDescription>
                  </Alert>
                )}
                {confirmedProducts.length ? (
                  confirmedProducts.map((product) => {
                    const checked = selectedProductIds.includes(
                      product.product_id,
                    );
                    const recommended = recommendedProductIds.has(
                      product.product_id,
                    );
                    return (
                      <label
                        key={product.product_id}
                        className="flex min-h-14 cursor-pointer items-start gap-3 rounded-lg border px-4 py-3 transition-colors hover:bg-accent/40"
                      >
                        <input
                          type="checkbox"
                          className="mt-1 size-4"
                          checked={checked}
                          disabled={
                            Boolean(pending) ||
                            !editAllowed ||
                            productSelectionConflict ||
                            (!checked && selectedProductIds.length >= 3)
                          }
                          onChange={() =>
                            setSelectedProductIds((current) => {
                              const next = checked
                                ? current.filter(
                                    (value) => value !== product.product_id,
                                  )
                                : [...current, product.product_id];
                              productDraftIdsRef.current = next;
                              return next;
                            })
                          }
                        />
                        <span className="min-w-0">
                          <span className="flex flex-wrap items-center gap-2 font-medium">
                            {product.name}
                            {recommended && (
                              <Badge variant="secondary">AI 推荐</Badge>
                            )}
                          </span>
                          <span className="mt-1 block break-all font-mono text-xs text-muted-foreground">
                            {product.product_id}
                          </span>
                        </span>
                      </label>
                    );
                  })
                ) : (
                  <p className="rounded-lg border border-dashed px-4 py-8 text-center text-sm text-muted-foreground">
                    当前知识库没有已确认产品。请先在正式知识库完成产品确认。
                  </p>
                )}
                <Button
                  type="button"
                  className="min-h-11"
                  disabled={
                    Boolean(pending) ||
                    !editAllowed ||
                    !allowed.has("update_products") ||
                    productSelectionConflict ||
                    selectedProductIds.length < 1
                  }
                  onClick={() =>
                    void runAction("保存产品选择", () =>
                      saveProductSelection({
                        revision: task.revision ?? 0,
                        product_ids: selectedProductIds,
                      }),
                    )
                  }
                >
                  <FileCheck2 />
                  保存 {selectedProductIds.length} 个产品
                </Button>
              </CardContent>
            </Card>

            <ServerProductRediscoveryPanel
              key={`${customer}:${taskId}:product-rediscovery`}
              customer={customer}
              pending={pending}
              knowledgeEditAllowed={editAllowed}
              runJob={runJob}
            />

            <ServerTaskResetPanel
              task={task}
              taskApi={taskApi}
              pending={pending}
              editAllowed={editAllowed}
              resetAllowed={allowed.has("rewrite_from_scratch")}
              runAction={runAction}
              onCompleted={() => setStep("setup")}
            />
          </div>
        )}

        <div hidden={step !== "setup"}>
          <ServerWritingRequirementsPanel
            key={`${customer}:${taskId}`}
            task={task}
            projectApi={projectApi}
            taskApi={taskApi}
            canEdit={editAllowed}
            onTaskUpdated={acceptWritingSettingsTask}
            onDirtyChange={setWritingSettingsDirty}
            onGenerationBlockedChange={setWritingSettingsPromptBlocked}
          />
        </div>

        {step === "outline" && (
          <div className="grid gap-4">
            <Card>
            <CardHeader className="border-b">
              <CardTitle>大纲生成与人工确认</CardTitle>
              <CardDescription>
                Job 只写可审阅草稿；只有“确认大纲”才推进状态并使旧正文失效。
              </CardDescription>
            </CardHeader>
            <CardContent className="grid gap-4">
              {writingSettingsDirty || writingSettingsPromptBlocked ? (
                <Alert>
                  <AlertCircle />
                  <AlertTitle>写作要求尚未保存</AlertTitle>
                  <AlertDescription>
                    {writingSettingsDirty
                      ? "返回“内容准备”保存后才能生成大纲；当前草稿仍保留在页面中。"
                      : "返回“内容准备”确认 Prompt Directory 与当前选择可用后再生成大纲。"}
                  </AlertDescription>
                </Alert>
              ) : null}
              <div className="grid gap-2">
                <Label htmlFor="server-outline">Markdown 大纲</Label>
                <Textarea
                  id="server-outline"
                  value={outlineDraft}
                  onChange={(event) => setOutlineDraft(event.target.value)}
                  disabled={Boolean(pending) || !editAllowed}
                  className="min-h-80 resize-y font-mono text-sm leading-6"
                  placeholder="生成后在这里审阅和修改大纲。"
                />
              </div>
              <div className="flex flex-wrap justify-end gap-2">
                <Button
                  type="button"
                  variant="outline"
                  className="min-h-11"
                  disabled={
                    Boolean(pending) ||
                    writingSettingsDirty ||
                    writingSettingsPromptBlocked ||
                    !editAllowed ||
                    !allowed.has("generate_outline")
                  }
                  onClick={() => void runJob("生成大纲草稿", "outline")}
                >
                  {pending === "生成大纲草稿" ? (
                    <Loader2 className="animate-spin" />
                  ) : (
                    <Sparkles />
                  )}
                  生成草稿
                </Button>
                <Button
                  type="button"
                  variant="outline"
                  className="min-h-11"
                  disabled={
                    Boolean(pending) ||
                    !editAllowed ||
                    !allowed.has("update_outline") ||
                    !outlineDraft.trim()
                  }
                  onClick={() =>
                    void runAction("保存大纲草稿", () =>
                      apiPut<TaskRecord>(`${taskApi}/outline`, {
                        revision: task.revision ?? 0,
                        outline: outlineDraft,
                        confirmed: false,
                      }),
                    )
                  }
                >
                  保存草稿
                </Button>
                <Button
                  type="button"
                  className="min-h-11"
                  disabled={
                    Boolean(pending) ||
                    !editAllowed ||
                    !allowed.has("update_outline") ||
                    !outlineDraft.trim()
                  }
                  onClick={() =>
                    void runAction("确认大纲", () =>
                      apiPut<TaskRecord>(`${taskApi}/outline`, {
                        revision: task.revision ?? 0,
                        outline: outlineDraft,
                        confirmed: true,
                      }),
                    )
                  }
                >
                  <CheckCircle2 />
                  确认大纲
                </Button>
              </div>
              <ServerOutlineHistory
                task={task}
                taskApi={taskApi}
                pending={pending}
                editAllowed={editAllowed}
                updateAllowed={allowed.has("update_outline")}
                runAction={runAction}
              />
            </CardContent>
            </Card>
            {hasConfirmedOutline ? (
              <ServerResearchWorkspace
                customer={customer}
                taskId={taskId}
                embedded
              />
            ) : (
              <Alert>
                <FileCheck2 />
                <AlertTitle>确认大纲后可研究资料</AlertTitle>
                <AlertDescription>
                  Research Agent 会按当前大纲逐节检索并生成 Evidence Pack；完成后再生成正文。
                </AlertDescription>
              </Alert>
            )}
          </div>
        )}

        {step === "draft" && (
          <div className="grid items-start gap-4 xl:grid-cols-[minmax(0,1.4fr)_minmax(340px,0.6fr)]">
            <Card>
              <CardHeader className="border-b">
                <CardTitle>初稿正文</CardTitle>
                <CardDescription>
                  Server Article Job 固定 Prompt Version 和 Published Context。
                </CardDescription>
              </CardHeader>
              <CardContent className="grid gap-4">
                {writingSettingsDirty || writingSettingsPromptBlocked ? (
                  <Alert>
                    <AlertCircle />
                    <AlertTitle>写作要求尚未保存</AlertTitle>
                    <AlertDescription>
                      {writingSettingsDirty
                        ? "返回“内容准备”保存后才能生成文章；当前草稿仍保留在页面中。"
                        : "返回“内容准备”确认 Prompt Directory 与当前选择可用后再生成文章。"}
                    </AlertDescription>
                  </Alert>
                ) : null}
                <div className="max-h-[64dvh] overflow-auto rounded-lg border bg-muted/20 p-4">
                  <pre className="whitespace-pre-wrap font-sans text-sm leading-7">
                    {task.initial_article || task.article || "尚未生成初稿。"}
                  </pre>
                </div>
                <Button
                  type="button"
                  className="min-h-11"
                  disabled={
                    Boolean(pending) ||
                    writingSettingsDirty ||
                    writingSettingsPromptBlocked ||
                    !editAllowed ||
                    !allowed.has("generate_article")
                  }
                  onClick={() =>
                    void runJob(articleJobLabel, articleJobEndpoint)
                  }
                >
                  {pending === articleJobLabel ? (
                    <Loader2 className="animate-spin" />
                  ) : (
                    <Sparkles />
                  )}
                  {articleJobLabel}
                </Button>
              </CardContent>
            </Card>

            <Card>
              <CardHeader className="border-b">
                <CardTitle>AI-rate 初检</CardTitle>
                <CardDescription>
                  截图作为私有 Asset 保存，并与当前 Initial Article Hash 绑定。
                </CardDescription>
              </CardHeader>
              <CardContent className="grid gap-4">
                <div className="grid gap-2">
                  <Label htmlFor="initial-screenshot">初检截图（PNG）</Label>
                  <Input
                    id="initial-screenshot"
                    type="file"
                    accept="image/png"
                    className="h-11"
                    disabled={Boolean(pending) || !reviewAllowed}
                    onChange={(event) =>
                      setInitialScreenshot(event.target.files?.[0] ?? null)
                    }
                  />
                </div>
                <div className="grid gap-2">
                  <Label htmlFor="initial-score">AI-rate 分数（可选）</Label>
                  <Input
                    id="initial-score"
                    type="number"
                    min="0"
                    max="100"
                    value={initialScore}
                    disabled={Boolean(pending) || !reviewAllowed}
                    onChange={(event) => setInitialScore(event.target.value)}
                  />
                </div>
                <div className="grid gap-2">
                  <Label htmlFor="initial-report">初检说明</Label>
                  <Textarea
                    id="initial-report"
                    value={initialReport}
                    disabled={Boolean(pending) || !reviewAllowed}
                    className="min-h-28 resize-y"
                    onChange={(event) => setInitialReport(event.target.value)}
                  />
                </div>
                <Button
                  type="button"
                  className="min-h-11"
                  disabled={
                    Boolean(pending) ||
                    !reviewAllowed ||
                    !allowed.has("confirm_initial_ai_check") ||
                    (!initialScreenshot &&
                      !task.initial_ai_check?.screenshot_asset_id)
                  }
                  onClick={() =>
                    void runAction("确认初检", async () => {
                      if (initialScreenshot) {
                        await uploadScreenshot("initial", initialScreenshot);
                        const latest = await apiGet<TaskRecord>(taskApi);
                        await apiPut<TaskRecord>(`${taskApi}/checks/initial-ai`, {
                          revision: latest.revision ?? 0,
                          score: initialScore ? Number(initialScore) : null,
                          report: initialReport,
                          confirmed: true,
                        });
                        return;
                      }
                      await apiPut<TaskRecord>(`${taskApi}/checks/initial-ai`, {
                        revision: task.revision ?? 0,
                        score: initialScore ? Number(initialScore) : null,
                        report: initialReport,
                        confirmed: true,
                      });
                    })
                  }
                >
                  {pending === "确认初检" ? (
                    <Loader2 className="animate-spin" />
                  ) : (
                    <CheckCircle2 />
                  )}
                  上传并确认
                </Button>
              </CardContent>
            </Card>

            <ServerSectionRewritePanel
              task={task}
              taskApi={taskApi}
              pending={pending}
              editAllowed={editAllowed}
              updateAllowed={allowed.has("update_article")}
              runAction={runAction}
            />
          </div>
        )}

        {step === "review" && (
          <div className="grid items-start gap-4 xl:grid-cols-[minmax(0,1.3fr)_minmax(360px,0.7fr)]">
            <Card>
              <CardHeader className="border-b">
                <CardTitle>Humanized Article</CardTitle>
                <CardDescription>
                  自动 Job 固定 Project Humanize Prompt；人工保存走独立 Version
                  来源和结构事实门禁。
                </CardDescription>
              </CardHeader>
              <CardContent className="grid gap-4">
                <Textarea
                  value={humanizedDraft}
                  onChange={(event) => setHumanizedDraft(event.target.value)}
                  disabled={Boolean(pending) || !editAllowed}
                  aria-label="Humanized Article Markdown"
                  className="min-h-[52dvh] resize-y font-mono text-sm leading-6"
                />
                <div className="flex flex-wrap justify-end gap-2">
                  <Button
                    type="button"
                    variant="outline"
                    className="min-h-11"
                    disabled={
                      Boolean(pending) ||
                      !editAllowed ||
                      !allowed.has("humanize_article")
                    }
                    onClick={() => void runJob("自动人化", "humanize")}
                  >
                    {pending === "自动人化" ? (
                      <Loader2 className="animate-spin" />
                    ) : (
                      <Sparkles />
                    )}
                    自动人化
                  </Button>
                  <Button
                    type="button"
                    className="min-h-11"
                    disabled={
                      Boolean(pending) ||
                      !editAllowed ||
                      !allowed.has("update_humanized_article") ||
                      !humanizedDraft.trim()
                    }
                    onClick={() =>
                      void runAction("保存人工人化稿", () =>
                        apiPut<TaskRecord>(`${taskApi}/humanized-article`, {
                          revision: task.revision ?? 0,
                          article: humanizedDraft,
                        }),
                      )
                    }
                  >
                    保存人工审阅稿
                  </Button>
                </div>
              </CardContent>
            </Card>

            <div className="grid gap-4">
              <Card>
                <CardHeader className="border-b">
                  <CardTitle>AI-rate 终检</CardTitle>
                  <CardDescription>
                    终检截图和分数绑定当前 Humanized Article。
                  </CardDescription>
                </CardHeader>
                <CardContent className="grid gap-3">
                  <div className="grid gap-2">
                    <Label htmlFor="final-screenshot">终检截图（PNG）</Label>
                    <Input
                      id="final-screenshot"
                      type="file"
                      accept="image/png"
                      className="h-11"
                      disabled={Boolean(pending) || !reviewAllowed}
                      onChange={(event) =>
                        setFinalScreenshot(event.target.files?.[0] ?? null)
                      }
                    />
                  </div>
                  <div className="grid gap-2">
                    <Label htmlFor="final-score">AI-rate 分数（可选）</Label>
                    <Input
                      id="final-score"
                      type="number"
                      min="0"
                      max="100"
                      value={finalScore}
                      disabled={Boolean(pending) || !reviewAllowed}
                      onChange={(event) => setFinalScore(event.target.value)}
                    />
                  </div>
                  <div className="grid gap-2">
                    <Label htmlFor="final-report">终检说明</Label>
                    <Textarea
                      id="final-report"
                      value={finalReport}
                      disabled={Boolean(pending) || !reviewAllowed}
                      className="min-h-24 resize-y"
                      onChange={(event) => setFinalReport(event.target.value)}
                    />
                  </div>
                  <Button
                    type="button"
                    className="min-h-11"
                    disabled={
                      Boolean(pending) ||
                      !reviewAllowed ||
                      !allowed.has("confirm_final_ai_check") ||
                      (!finalScreenshot &&
                        !task.final_ai_check?.screenshot_asset_id)
                    }
                    onClick={() =>
                      void runAction("确认终检", async () => {
                        if (finalScreenshot) {
                          await uploadScreenshot("final", finalScreenshot);
                          const latest = await apiGet<TaskRecord>(taskApi);
                          await apiPut<TaskRecord>(`${taskApi}/checks/final-ai`, {
                            revision: latest.revision ?? 0,
                            score: finalScore ? Number(finalScore) : null,
                            report: finalReport,
                            confirmed: true,
                          });
                          return;
                        }
                        await apiPut<TaskRecord>(`${taskApi}/checks/final-ai`, {
                          revision: task.revision ?? 0,
                          score: finalScore ? Number(finalScore) : null,
                          report: finalReport,
                          confirmed: true,
                        });
                      })
                    }
                  >
                    {pending === "确认终检" ? (
                      <Loader2 className="animate-spin" />
                    ) : (
                      <CheckCircle2 />
                    )}
                    上传并确认
                  </Button>
                </CardContent>
              </Card>

              <Card>
                <CardHeader className="border-b">
                  <CardTitle>链接恢复</CardTitle>
                  <CardDescription>
                    Provider 只产候选；提交前验证 URL 多重集合和可见正文。
                  </CardDescription>
                </CardHeader>
                <CardContent>
                  <Button
                    type="button"
                    className="min-h-11 w-full"
                    disabled={
                      Boolean(pending) ||
                      !editAllowed ||
                      !allowed.has("restore_links")
                    }
                    onClick={() => void runJob("恢复首稿链接", "restore-links")}
                  >
                    {pending === "恢复首稿链接" ? (
                      <Loader2 className="animate-spin" />
                    ) : (
                      <FileCheck2 />
                    )}
                    恢复并验证链接
                  </Button>
                </CardContent>
              </Card>

              <ServerSeoReviewPanel
                task={task}
                taskApi={taskApi}
                pending={pending}
                editAllowed={editAllowed}
                reviewAllowed={reviewAllowed}
                runAction={runAction}
                runJob={runJob}
              />
            </div>
          </div>
        )}

        {step === "delivery" && (
          <div className="grid items-start gap-4 xl:grid-cols-2">
            <Card>
              <CardHeader className="border-b">
                <CardTitle>私有图片准备</CardTitle>
                <CardDescription>
                  Hero 从本篇已选产品图片中选择；每个产品也可单独选择自己的正文图。
                </CardDescription>
              </CardHeader>
              <CardContent className="grid gap-4">
                <div className="grid gap-2">
                  <Label>Hero 图片</Label>
                  <ServerHeroAssetPicker
                    assets={catalog?.image_assets || []}
                    description={`仅显示本篇 ${task.products.length} 个已选产品的图片。`}
                    projectApi={projectApi}
                    selectedAssetId={heroAssetId}
                    disabled={Boolean(pending) || !editAllowed}
                    onSelect={setHeroAssetId}
                  />
                  <p className="text-xs leading-5 text-muted-foreground">
                    浏览器只提交选中的 Asset ID。Bucket、Object Key、文件路径、内容哈希和
                    来源 URL 均不进入 Catalog DTO；服务端会再次授权并生成 WebP
                    派生图。
                  </p>
                </div>
                {task.products.map((product) =>
                  product.product_id ? (
                    <div
                      key={product.product_id}
                      className="grid gap-3 rounded-xl border p-3"
                    >
                      <div className="grid gap-1">
                        <Label>{product.name} 的正文图片</Label>
                        <p className="text-xs text-muted-foreground">
                          只显示该产品当前已发布证据中的图片。
                        </p>
                      </div>
                      <ServerHeroAssetPicker
                        assets={(catalog?.image_assets || []).filter(
                          (asset) => asset.product_id === product.product_id,
                        )}
                        description=""
                        projectApi={projectApi}
                        selectedAssetId={
                          productAssetIds[product.product_id] || ""
                        }
                        disabled={Boolean(pending) || !editAllowed}
                        onSelect={(assetId) =>
                          setProductAssetIds((current) => ({
                            ...current,
                            [product.product_id as string]: assetId,
                          }))
                        }
                      />
                      <Label htmlFor={`anchor-${product.product_id}`}>
                        {product.name} 的 H2 锚点
                      </Label>
                      <Input
                        id={`anchor-${product.product_id}`}
                        value={productAnchors[product.product_id] || ""}
                        disabled={Boolean(pending) || !editAllowed}
                        placeholder="正文中唯一的 H2 标题"
                        onChange={(event) =>
                          setProductAnchors((current) => ({
                            ...current,
                            [product.product_id as string]: event.target.value,
                          }))
                        }
                      />
                    </div>
                  ) : null,
                )}
                <Button
                  type="button"
                  className="min-h-11"
                  disabled={
                    Boolean(pending) ||
                    !editAllowed ||
                    !allowed.has("prepare_images") ||
                    !heroAssetId.trim()
                  }
                  onClick={() =>
                    void runAction("准备文章图片", () =>
                      apiPost<TaskRecord>(`${taskApi}/prepare-images`, {
                        revision: task.revision ?? 0,
                        hero_asset_id: heroAssetId,
                        product_asset_ids: productAssetIds,
                        product_anchors: Object.fromEntries(
                          Object.entries(productAnchors)
                            .map(([productId, heading]) => [
                              productId,
                              heading.trim(),
                            ])
                            .filter(([, heading]) => Boolean(heading)),
                        ),
                      }),
                    )
                  }
                >
                  {pending === "准备文章图片" ? (
                    <Loader2 className="animate-spin" />
                  ) : (
                    <ImageIcon />
                  )}
                  校验锚点并准备图片
                </Button>
              </CardContent>
            </Card>

            <Card>
              <CardHeader className="border-b">
                <CardTitle>交付产物</CardTitle>
                <CardDescription>
                  生成命令使用 Task Revision；下载先取得重新授权的短期 URL。
                </CardDescription>
              </CardHeader>
              <CardContent className="grid gap-4">
                <div className="grid grid-cols-2 gap-2">
                  <ArtifactState
                    label="文章正文"
                    ready={Boolean(articleFor(task))}
                  />
                  <ArtifactState
                    label="链接验证"
                    ready={Boolean(task.link_validation?.passed)}
                  />
                  <ArtifactState
                    label="准备图片"
                    ready={Boolean(task.images?.length)}
                  />
                  <ArtifactState
                    label="终检截图"
                    ready={Boolean(task.final_ai_check?.screenshot_asset_id)}
                  />
                  <ArtifactState
                    label="Word"
                    ready={Boolean(task.docx_asset_id)}
                  />
                  <ArtifactState
                    label="TDK"
                    ready={Boolean(task.tdk_asset_id)}
                  />
                  <ArtifactState
                    label="交付 ZIP"
                    ready={Boolean(task.delivery_package_asset_id)}
                  />
                </div>
                <Separator />
                <div className="grid gap-2 sm:grid-cols-2">
                  <Button
                    type="button"
                    variant="outline"
                    className="min-h-11"
                    disabled={
                      Boolean(pending) ||
                      !editAllowed ||
                      !allowed.has("export_docx")
                    }
                    onClick={() =>
                      void runAction("导出 Word", () =>
                        apiPost<TaskRecord>(`${taskApi}/export-docx`, {
                          revision: task.revision ?? 0,
                        }),
                      )
                    }
                  >
                    <FileText />
                    导出 Word
                  </Button>
                  <Button
                    type="button"
                    variant="outline"
                    className="min-h-11"
                    disabled={
                      Boolean(pending) ||
                      !editAllowed ||
                      !allowed.has("generate_tdk")
                    }
                    onClick={() =>
                      void runAction("生成 TDK", () =>
                        apiPost<TaskRecord>(`${taskApi}/generate-tdk`, {
                          revision: task.revision ?? 0,
                        }),
                      )
                    }
                  >
                    <FileCheck2 />
                    生成 TDK
                  </Button>
                  <Button
                    type="button"
                    className="min-h-11 sm:col-span-2"
                    disabled={
                      Boolean(pending) ||
                      !editAllowed ||
                      !allowed.has("package_delivery")
                    }
                    onClick={() =>
                      void runAction("生成交付 ZIP", () =>
                        apiPost<TaskRecord>(`${taskApi}/package-delivery`, {
                          revision: task.revision ?? 0,
                        }),
                      )
                    }
                  >
                    <Package />
                    生成交付 ZIP
                  </Button>
                </div>
                <div className="grid gap-2 sm:grid-cols-3">
                  <Button
                    type="button"
                    variant="outline"
                    className="min-h-11"
                    disabled={Boolean(pending) || !task.docx_asset_id}
                    onClick={() => void download("下载 Word", "docx/download")}
                  >
                    <Download />
                    Word
                  </Button>
                  <Button
                    type="button"
                    variant="outline"
                    className="min-h-11"
                    disabled={Boolean(pending) || !task.tdk_asset_id}
                    onClick={() => void download("下载 TDK", "tdk/download")}
                  >
                    <Download />
                    TDK
                  </Button>
                  <Button
                    type="button"
                    variant="outline"
                    className="min-h-11"
                    disabled={
                      Boolean(pending) || !task.delivery_package_asset_id
                    }
                    onClick={() =>
                      void download(
                        "下载交付 ZIP",
                        "delivery-package/download",
                      )
                    }
                  >
                    <Download />
                    ZIP
                  </Button>
                </div>
              </CardContent>
            </Card>
          </div>
        )}
      </div>
    </main>
  );
}

function ArtifactState({ label, ready }: { label: string; ready: boolean }) {
  return (
    <div className="flex min-h-11 items-center justify-between gap-2 rounded-lg border px-3 py-2 text-sm">
      <span>{label}</span>
      <Badge variant={ready ? "default" : "outline"}>
        {ready ? "已就绪" : "缺失"}
      </Badge>
    </div>
  );
}
