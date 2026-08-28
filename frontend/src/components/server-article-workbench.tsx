"use client";

/* eslint-disable react-hooks/set-state-in-effect */

import {
  AlertCircle,
  ArrowLeft,
  ArrowRight,
  Bell,
  BellRing,
  BookOpenCheck,
  CheckCircle2,
  Download,
  FileCheck2,
  FileText,
  ImageIcon,
  Loader2,
  Package,
  ClipboardPaste,
  RefreshCw,
  Save,
  ShieldCheck,
  Sparkles,
} from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ClipboardEvent,
} from "react";

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
import { ServerResearchWorkspace } from "@/components/server-research-workspace";
import { ServerSectionRewritePanel } from "@/components/server-section-rewrite-panel";
import { ServerSeoReviewPanel } from "@/components/server-seo-review-panel";
import { ServerTaskResetPanel } from "@/components/server-task-reset-panel";
import { ServerWritingRequirementsPanel } from "@/components/server-writing-requirements-panel";
import { apiGet, apiPost, apiPut, apiUpload } from "@/lib/api";
import { triggerBrowserDownload } from "@/lib/browser-download";
import { sameProjectId } from "@/lib/project-id";
import {
  getTaskCompletionReminderStatus,
  notifyTaskCompletion,
  prepareTaskCompletionReminders,
  type TaskCompletionReminderStatus,
} from "@/lib/task-completion-reminder";
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
  { id: "draft", label: "3. 初稿", description: "正文生成与 AI-rate" },
  { id: "review", label: "4. 审阅", description: "人化、终检和链接恢复" },
  { id: "delivery", label: "5. 图片与交付", description: "私有图片、Word、TDK、ZIP" },
];

const STATUS_LABELS: Record<WorkflowStatus, string> = {
  new: "待生成标题",
  titles_ready: "待选择标题",
  title_selected: "待确认产品",
  outline_ready: "待审阅大纲",
  outline_confirmed: "待生成初稿",
  draft_ready: "待填写 AI-rate",
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

const COMPLETION_REMINDER_BY_ENDPOINT: Record<
  string,
  { kind: "outline" | "article" | "review"; title: string }
> = {
  outline: { kind: "outline", title: "大纲生成完成" },
  article: { kind: "article", title: "正文生成完成" },
  "article/rewrite": { kind: "article", title: "正文重写完成" },
  "seo-reviews": { kind: "review", title: "复检完成" },
};

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

function canEdit(
  role: AccessibleProject["effective_role"] | null,
  isProjectOwner: boolean,
) {
  return (
    role === "org_admin" ||
    role === "editor" ||
    (role === "team_lead" && isProjectOwner)
  );
}

function canReview(
  role: AccessibleProject["effective_role"] | null,
  isProjectOwner: boolean,
) {
  return (
    role === "org_admin" ||
    role === "editor" ||
    role === "reviewer" ||
    (role === "team_lead" && isProjectOwner)
  );
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

function isTaskCompleted(task: Pick<TaskRecord, "manual_completed" | "status">) {
  return task.manual_completed === true || task.status === "docx_exported";
}

function taskProductIds(task: TaskRecord) {
  return task.products.flatMap((product) =>
    product.product_id ? [product.product_id] : [],
  );
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
  const router = useRouter();
  const [task, setTask] = useState<TaskRecord | null>(null);
  const [catalog, setCatalog] = useState<ServerProjectCatalog | null>(null);
  const [role, setRole] = useState<AccessibleProject["effective_role"] | null>(
    null,
  );
  const [isProjectOwner, setIsProjectOwner] = useState(false);
  const [step, setStep] = useState<WorkbenchStep>(
    isWorkbenchStep(initialStep) ? initialStep : "setup",
  );
  const [loading, setLoading] = useState(true);
  const [pending, setPending] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [reminderStatus, setReminderStatus] =
    useState<TaskCompletionReminderStatus>("unsupported");
  const [customTitleDraft, setCustomTitleDraft] = useState("");
  const [outlineDraft, setOutlineDraft] = useState("");
  const [humanizedDraft, setHumanizedDraft] = useState("");
  const [initialScore, setInitialScore] = useState("");
  const [finalScore, setFinalScore] = useState("");
  const [finalScreenshot, setFinalScreenshot] = useState<File | null>(null);
  const [finalScreenshotPreview, setFinalScreenshotPreview] = useState("");
  const [useEvidencePack, setUseEvidencePack] = useState(true);
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
  const finalScreenshotInputRef = useRef<HTMLInputElement>(null);

  const projectApi = `/api/projects/${encodeURIComponent(customer)}`;
  const taskApi = `${projectApi}/tasks/${encodeURIComponent(taskId)}`;
  const productSelectionHref = `/projects/${encodeURIComponent(customer)}/articles/${encodeURIComponent(taskId)}/products`;
  const knowledgeCoverageHref = `/projects/${encodeURIComponent(customer)}/articles/${encodeURIComponent(taskId)}/knowledge-coverage`;
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

  useEffect(() => {
    setReminderStatus(getTaskCompletionReminderStatus());
  }, []);

  useEffect(() => {
    if (!finalScreenshot) {
      setFinalScreenshotPreview("");
      return;
    }
    const previewUrl = URL.createObjectURL(finalScreenshot);
    setFinalScreenshotPreview(previewUrl);
    return () => URL.revokeObjectURL(previewUrl);
  }, [finalScreenshot]);

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
        `${projectApi}/catalog?product_limit=200&image_limit=100&image_product_ids=${encodeURIComponent(imageProductIds)}`,
      );
      if (
        activeWorkbenchScopeRef.current !== requestScope ||
        loadRequestRef.current !== requestId
      ) {
        return;
      }
      const project = projects.find((item) =>
        sameProjectId(item.project_id, customer),
      );
      setTask(nextTask);
      setRole(project?.effective_role ?? null);
      setIsProjectOwner(project?.is_project_owner === true);
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
    setIsProjectOwner(false);
    setFinalScreenshot(null);
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
    if (!preserveDrafts) {
      setCustomTitleDraft(task.selected_title || "");
      setOutlineDraft(task.outline_draft || task.outline || "");
      setHumanizedDraft(task.humanized_article || task.initial_article || "");
      setInitialScore(
        task.initial_ai_check?.score === null ||
          task.initial_ai_check?.score === undefined
          ? ""
          : String(task.initial_ai_check.score),
      );
      setFinalScore(
        task.final_ai_check?.score === null ||
          task.final_ai_check?.score === undefined
          ? ""
          : String(task.final_ai_check.score),
      );
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
    successMessage: string | (() => string) = `${label}完成。`,
  ): Promise<boolean> {
    const actionScope = workbenchScope;
    if (activeWorkbenchScopeRef.current !== actionScope) return false;
    setPending(label);
    setError("");
    setMessage("");
    try {
      await action();
      if (activeWorkbenchScopeRef.current !== actionScope) return false;
      setMessage(
        typeof successMessage === "function" ? successMessage() : successMessage,
      );
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
    successMessage = `${label}完成，已读取最新 Task Revision。`,
  ): Promise<boolean> {
    if (!task) return false;
    const jobScope = workbenchScope;
    const completionReminder = COMPLETION_REMINDER_BY_ENDPOINT[endpoint];
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
      return false;
    }
    if (completionReminder) {
      setReminderStatus(await prepareTaskCompletionReminders());
    }
    const succeeded = await runAction(
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
      successMessage,
    );
    if (
      succeeded &&
      completionReminder &&
      activeWorkbenchScopeRef.current === jobScope
    ) {
      await notifyTaskCompletion({
        kind: completionReminder.kind,
        taskId,
        title: completionReminder.title,
        body: `文章“${task.selected_title || task.topic}”的${
          completionReminder.kind === "outline"
            ? "大纲"
            : completionReminder.kind === "article"
              ? "正文"
              : "SEO 复检"
        }已完成。`,
      });
    }
    return succeeded;
  }

  async function enableCompletionReminders() {
    setReminderStatus(await prepareTaskCompletionReminders());
  }

  async function recommendProducts() {
    const completed = await runJob(
      "推荐产品",
      "products",
      {},
      "产品推荐已完成，正在打开选择页。",
    );
    if (completed) router.push(productSelectionHref);
  }

  async function uploadFinalScreenshot(file: File) {
    if (!task) return;
    const form = new FormData();
    form.append("file", file);
    await apiUpload<TaskRecord>(
      `${taskApi}/checks/final-ai/screenshot?revision=${task.revision ?? 0}`,
      form,
    );
  }

  function selectFinalScreenshot(file: File | null) {
    if (!file) return;
    setFinalScreenshot(file);
    setMessage("已选中图片；再次粘贴或选择文件会替换它。");
    setError("");
  }

  function pasteFinalScreenshot(event: ClipboardEvent<HTMLDivElement>) {
    const image =
      Array.from(event.clipboardData.files).find((file) =>
        file.type.startsWith("image/"),
      ) ||
      Array.from(event.clipboardData.items)
        .find(
          (item) =>
            item.kind === "file" && item.type.startsWith("image/"),
        )
        ?.getAsFile();
    if (!image) return;
    event.preventDefault();
    const extension = image.type.split("/")[1]?.replace("jpeg", "jpg") || "png";
    selectFinalScreenshot(
      new File([image], `pasted-ai-rate-${Date.now()}.${extension}`, {
        type: image.type,
      }),
    );
  }

  async function download(label: string, endpoint: string) {
    const actionScope = workbenchScope;
    if (activeWorkbenchScopeRef.current !== actionScope) return;
    setPending(label);
    setError("");
    setMessage("");
    try {
      const asset = await apiGet<ProjectAssetDownload>(
        `${taskApi}/${endpoint}`,
        30_000,
      );
      if (!asset.url) throw new Error("Server 未返回可用的短期下载地址。");
      if (activeWorkbenchScopeRef.current !== actionScope) return;
      const fallback = endpoint.includes("delivery-package")
        ? "delivery.zip"
        : endpoint.includes("tdk/")
          ? "D.docx"
          : endpoint.includes("screenshot")
            ? "ai-rate.png"
            : "article.docx";
      triggerBrowserDownload(asset.url, asset.filename || fallback);
      setMessage(`${label}已开始，请查看浏览器下载列表。`);
    } catch (reason) {
      if (activeWorkbenchScopeRef.current !== actionScope) return;
      setError(errorMessage(reason));
    } finally {
      if (activeWorkbenchScopeRef.current === actionScope) setPending("");
    }
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
  const recommendedProducts = useMemo(
    () =>
      confirmedProducts.filter((product) =>
        recommendedProductIds.has(product.product_id),
      ),
    [confirmedProducts, recommendedProductIds],
  );
  const humanizedDirty = Boolean(
    task && humanizedDraft.trim() !== (task.humanized_article || "").trim(),
  );

  const allowed = new Set(task?.allowed_actions || []);
  const editAllowed = canEdit(role, isProjectOwner);
  const reviewAllowed = canReview(role, isProjectOwner);
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
                <Badge>
                  {isTaskCompleted(task)
                    ? "已完成"
                    : task.status === "title_selected" && task.products.length
                    ? "产品已保存 · 待生成大纲"
                    : STATUS_LABELS[task.status]}
                </Badge>
                <label className="inline-flex min-h-9 items-center gap-2 rounded-md border px-3 text-sm">
                  <input
                    type="checkbox"
                    className="size-4 accent-primary"
                    checked={isTaskCompleted(task)}
                    disabled={
                      loading ||
                      Boolean(pending) ||
                      !editAllowed ||
                      task.status === "docx_exported"
                    }
                    onChange={(event) =>
                      void runAction("更新完成标记", () =>
                        apiPut<TaskRecord>(`${taskApi}/manual-completion`, {
                          revision: task.revision ?? 0,
                          completed: event.target.checked,
                        }),
                      )
                    }
                    aria-label="手动标记文章任务为已完成"
                  />
                  <span>已完成</span>
                </label>
              </div>
              <p className="mt-2 text-sm leading-6 text-muted-foreground">
                topic_{String(task.topic_index).padStart(3, "0")} · Revision{" "}
                <span className="font-mono">{task.revision ?? 0}</span> ·{" "}
                {task.topic}
              </p>
            </div>
            <div className="flex flex-wrap gap-2">
              <ServerTaskResetPanel
                task={task}
                taskApi={taskApi}
                pending={pending}
                editAllowed={editAllowed}
                resetAllowed={allowed.has("rewrite_from_scratch")}
                runAction={runAction}
                onCompleted={() => setStep("setup")}
              />
              <Button
                type="button"
                variant="outline"
                className="min-h-11"
                disabled={
                  reminderStatus !== "default" || Boolean(pending)
                }
                title={
                  reminderStatus === "denied"
                    ? "请在浏览器网站设置中允许通知"
                    : undefined
                }
                onClick={() => void enableCompletionReminders()}
              >
                {reminderStatus === "granted" ? <BellRing /> : <Bell />}
                {reminderStatus === "granted"
                  ? "完成提醒已开启"
                  : reminderStatus === "denied"
                    ? "通知已被阻止"
                    : reminderStatus === "unsupported"
                      ? "浏览器不支持通知"
                      : "开启完成提醒"}
              </Button>
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
                <div className="grid gap-2 border-t pt-4">
                  <div className="flex flex-wrap items-baseline justify-between gap-2">
                    <Label htmlFor={`custom-title-${task.id}`}>
                      自定义标题
                    </Label>
                    <span className="text-xs text-muted-foreground">
                      可跳过 AI 推荐
                    </span>
                  </div>
                  <Input
                    id={`custom-title-${task.id}`}
                    value={customTitleDraft}
                    onChange={(event) => setCustomTitleDraft(event.target.value)}
                    placeholder={task.topic || "输入文章标题"}
                    maxLength={300}
                    disabled={Boolean(pending) || !editAllowed}
                  />
                  <p className="text-xs leading-5 text-muted-foreground">
                    直接输入最终标题并确认后，即可进入产品选择，不必先生成标题候选。
                  </p>
                  <Button
                    type="button"
                    variant="outline"
                    className="min-h-11"
                    disabled={
                      Boolean(pending) ||
                      !editAllowed ||
                      !allowed.has("select_title") ||
                      !customTitleDraft.trim()
                    }
                    onClick={() =>
                      void runAction("保存自定义标题", () =>
                        apiPut<TaskRecord>(`${taskApi}/selected-title`, {
                          revision: task.revision ?? 0,
                          title: customTitleDraft.trim(),
                        }),
                      )
                    }
                  >
                    使用自定义标题
                  </Button>
                </div>
              </CardContent>
            </Card>

            <Card>
              <CardHeader className="border-b">
                <CardTitle>产品推荐与选择</CardTitle>
                <CardDescription>
                  AI 从全部 confirmed 产品中推荐最多 3 个；手动调整在独立页面完成。
                </CardDescription>
              </CardHeader>
              <CardContent className="grid gap-4">
                <div className="grid gap-2 sm:grid-cols-2">
                  <Button
                    type="button"
                    variant="secondary"
                    className="min-h-11"
                    disabled={
                      Boolean(pending) ||
                      !editAllowed ||
                      !allowed.has("generate_products") ||
                      !confirmedProducts.length
                    }
                    onClick={() => void recommendProducts()}
                  >
                    {pending === "推荐产品" ? (
                      <Loader2 className="animate-spin" />
                    ) : (
                      <Sparkles />
                    )}
                    {recommendedProductIds.size ? "重新推荐产品" : "推荐产品"}
                  </Button>
                  <Button
                    nativeButton={false}
                    variant="outline"
                    className="min-h-11"
                    render={<Link href={productSelectionHref} />}
                  >
                    <FileCheck2 />
                    手动选择产品
                  </Button>
                </div>

                {recommendedProducts.length ? (
                  <div className="grid gap-2">
                    <p className="text-sm font-medium">
                      已推荐 {recommendedProducts.length} 个产品
                    </p>
                    {recommendedProducts.map((product) => (
                      <div
                        key={product.product_id}
                        className="rounded-lg border bg-accent/25 px-3 py-3"
                      >
                        <div className="flex flex-wrap items-center gap-2 text-sm font-medium">
                          {product.name}
                          <Badge variant="secondary">AI 推荐</Badge>
                        </div>
                        <p className="mt-2 text-sm leading-6 text-muted-foreground">
                          {task.product_candidate_reasons?.[product.product_id] ||
                            "该候选来自旧版推荐，请重新推荐以生成具体理由。"}
                        </p>
                      </div>
                    ))}
                    <p className="text-xs leading-5 text-muted-foreground">
                      推荐结果已自动带入选择页并勾选；进入后可调整并显式保存。
                    </p>
                  </div>
                ) : confirmedProducts.length ? (
                  <p className="rounded-lg border border-dashed px-4 py-6 text-center text-sm text-muted-foreground">
                    尚未生成推荐。也可以直接进入手动选择页。
                  </p>
                ) : (
                  <p className="rounded-lg border border-dashed px-4 py-8 text-center text-sm text-muted-foreground">
                    当前知识库没有已确认产品。请先在正式知识库完成产品确认。
                  </p>
                )}

                {task.products.length > 0 && (
                  <p className="text-sm text-muted-foreground">
                    当前已保存 {task.products.length} 个产品；重新推荐不会直接覆盖它们。
                  </p>
                )}
              </CardContent>
            </Card>

            {task.article_brief && (
              <Card className="xl:col-span-2">
                <CardHeader className="border-b">
                  <CardTitle className="flex items-center gap-2">
                    <BookOpenCheck className="size-4" />
                    Article Brief
                  </CardTitle>
                  <CardDescription>
                    产品推荐和大纲生成共用的文章意图与已检索事实；标题或项目注意事项变化后会自动失效并重建。
                  </CardDescription>
                </CardHeader>
                <CardContent className="grid gap-4 lg:grid-cols-2">
                  <div className="grid gap-3">
                    <div>
                      <p className="text-xs font-medium text-muted-foreground">采购意图</p>
                      <p className="mt-1 text-sm leading-6">{task.article_brief.article_intent}</p>
                    </div>
                    <div className="flex flex-wrap gap-2 text-xs">
                      {task.article_brief.target_buyers.map((buyer) => (
                        <Badge key={buyer} variant="secondary">{buyer}</Badge>
                      ))}
                      {task.article_brief.selection_dimensions.map((dimension) => (
                        <Badge key={dimension} variant="outline">选型：{dimension}</Badge>
                      ))}
                    </div>
                  </div>
                  <div className="grid gap-2 rounded-lg border bg-muted/20 p-3 text-sm">
                    <p>已绑定事实：{task.article_brief.available_facts.length} 条</p>
                    <p>检索 Chunk：{task.article_brief.context_chunk_ids.length} 个</p>
                    {task.article_brief.missing_evidence.length > 0 && (
                      <p className="text-amber-700 dark:text-amber-300">
                        待补资料：{task.article_brief.missing_evidence.join("；")}
                      </p>
                    )}
                  </div>
                </CardContent>
              </Card>
            )}

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
                <div className="rounded-lg border bg-muted/20 p-3">
                  <label className="flex min-h-11 cursor-pointer items-center gap-3 text-sm font-medium">
                    <input
                      type="checkbox"
                      className="size-4 accent-primary"
                      checked={useEvidencePack}
                      disabled={Boolean(pending) || !editAllowed}
                      onChange={(event) =>
                        setUseEvidencePack(event.target.checked)
                      }
                    />
                    <span>引用大纲页生成的 Evidence Pack</span>
                  </label>
                  <p className="mt-1 pl-7 text-xs leading-5 text-muted-foreground">
                    开启时优先使用上一步资料研究固定的证据；没有匹配的 Evidence Pack 时自动回退到普通检索。关闭时完全不检索知识库，仅按当前 Task 内容和 Prompt 生成正文。
                  </p>
                </div>
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
                    void runJob(articleJobLabel, articleJobEndpoint, {
                      use_evidence_pack: useEvidencePack,
                    })
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
                <CardTitle>AI-rate</CardTitle>
                <CardDescription>
                  只需填写一次当前初稿的 AI-rate；低于 30% 时直接沿用初稿，跳过人化和第二次检测。
                </CardDescription>
              </CardHeader>
              <CardContent className="grid gap-4">
                <div className="grid gap-2">
                  <Label htmlFor="initial-score">AI-rate（%）</Label>
                  <Input
                    id="initial-score"
                    type="number"
                    min="0"
                    max="100"
                    value={initialScore}
                    disabled={Boolean(pending) || !reviewAllowed}
                    onChange={(event) => setInitialScore(event.target.value)}
                  />
                  <p className="text-xs leading-5 text-muted-foreground">
                    {task.initial_ai_check?.provider === "zerogpt" &&
                    task.initial_ai_check.score != null
                      ? "已带入自动检测结果，请核对后确认。"
                      : "请填写人工检测得到的 AI-rate。"}
                  </p>
                </div>
                <Button
                  type="button"
                  className="min-h-11"
                  disabled={
                    Boolean(pending) ||
                    !reviewAllowed ||
                    !allowed.has("confirm_initial_ai_check") ||
                    !initialScore
                  }
                  onClick={() => {
                    void (async () => {
                      const succeeded = await runAction(
                        "确认 AI-rate",
                        () =>
                          apiPut<TaskRecord>(`${taskApi}/checks/initial-ai`, {
                            revision: task.revision ?? 0,
                            score: Number(initialScore),
                            report: "",
                            confirmed: true,
                          }),
                        Number(initialScore) < 30
                          ? "AI-rate 低于 30%，已跳过人化和第二次检测。"
                          : "AI-rate 已确认，请继续处理人化稿。",
                      );
                      if (succeeded) setStep("review");
                    })();
                  }}
                >
                  {pending === "确认 AI-rate" ? (
                    <Loader2 className="animate-spin" />
                  ) : (
                    <CheckCircle2 />
                  )}
                  确认 AI-rate
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
          <div className="grid items-start gap-4">
            <div className="grid items-start gap-4 xl:grid-cols-[minmax(0,1.3fr)_minmax(360px,0.7fr)]">
            <Card>
              <CardHeader className="border-b">
                <CardTitle>
                  {task.humanization_skipped
                    ? "Article（初稿已达标）"
                    : "Humanized Article"}
                </CardTitle>
                <CardDescription>
                  {task.humanization_skipped
                    ? "当前正文直接沿用已达标初稿；如手动修改，保存后会重新进入 AI-rate 检查。"
                    : "自动 Job 固定 Project Humanize Prompt；人工保存走独立 Version 来源和结构事实门禁。"}
                </CardDescription>
              </CardHeader>
              <CardContent className="grid gap-4">
                {task.humanization_skipped && (
                  <Alert>
                    <ShieldCheck />
                    <AlertTitle>已跳过人化</AlertTitle>
                    <AlertDescription>
                      初稿 AI-rate 低于 30%，系统已直接采用该版本，并跳过第二次检测。
                    </AlertDescription>
                  </Alert>
                )}
                <Textarea
                  value={humanizedDraft}
                  onChange={(event) => setHumanizedDraft(event.target.value)}
                  disabled={Boolean(pending) || !editAllowed}
                  aria-label="Humanized Article Markdown"
                  className="min-h-[52dvh] resize-y font-mono text-sm leading-6"
                />
                {humanizedDirty && (
                  <p className="text-sm text-amber-700 dark:text-amber-300">
                    正文有未保存修改。保存后系统会自动重新检测 AI 率，当前终检结果不再代表这份正文。
                  </p>
                )}
                <div className="flex flex-wrap justify-end gap-2">
                  {!task.humanization_skipped && (
                    <Button
                      type="button"
                      variant="outline"
                      className="min-h-11"
                      disabled={
                        Boolean(pending) ||
                        !editAllowed ||
                        !allowed.has("humanize_article")
                      }
                      onClick={() =>
                        void runJob(
                          "自动人化",
                          "humanize",
                          {},
                          "自动人化完成，已自动复检 AI 率并读取最新 Task Revision。",
                        )
                      }
                    >
                      {pending === "自动人化" ? (
                        <Loader2 className="animate-spin" />
                      ) : (
                        <Sparkles />
                      )}
                      自动人化
                    </Button>
                  )}
                  <Button
                    type="button"
                    className="min-h-11"
                    disabled={
                      Boolean(pending) ||
                      !editAllowed ||
                      !allowed.has("update_humanized_article") ||
                      !humanizedDraft.trim() ||
                      !humanizedDirty
                    }
                    onClick={() => {
                      let automaticCheck: TaskRecord["final_ai_check"] | undefined;
                      void runAction(
                        "保存并复检 AI 率",
                        async () => {
                          const updated = await apiPut<TaskRecord>(
                            `${taskApi}/humanized-article`,
                            {
                              revision: task.revision ?? 0,
                              article: humanizedDraft,
                              recheck_ai_rate: true,
                            },
                          );
                          automaticCheck = updated.final_ai_check;
                        },
                        () =>
                          automaticCheck?.score == null
                            ? `人工审阅稿已保存。${automaticCheck?.report || "ZeroGPT 自动复检未返回结果。"}`
                            : `人工审阅稿已保存，ZeroGPT 自动复检为 ${automaticCheck.score}%。`,
                      );
                    }}
                  >
                    {pending === "保存并复检 AI 率" ? (
                      <Loader2 className="animate-spin" />
                    ) : (
                      <Save />
                    )}
                    {task.humanization_skipped
                      ? "保存修改并重新检测"
                      : "保存人工审阅稿并复检"}
                  </Button>
                </div>
              </CardContent>
            </Card>

            <div className="grid gap-4">
              <Card>
                <CardHeader className="border-b">
                  <CardTitle>AI-rate 与截图凭证</CardTitle>
                  <CardDescription>
                    当前文章只保留一组 AI-rate 与截图；低于 30% 只跳过人化，不再隐藏截图上传。
                  </CardDescription>
                </CardHeader>
                <CardContent className="grid gap-4">
                  <div className="grid gap-3 sm:grid-cols-[minmax(0,0.45fr)_minmax(0,0.55fr)] xl:grid-cols-1 2xl:grid-cols-[minmax(0,0.45fr)_minmax(0,0.55fr)]">
                    <div className="rounded-xl border bg-accent/35 px-4 py-5 text-center">
                      <p className="text-sm text-muted-foreground">当前文章 AI-rate</p>
                      <p className="mt-2 text-3xl font-semibold tabular-nums">
                        {task.final_ai_check?.score ??
                          task.initial_ai_check?.score ??
                          "—"}
                        {task.final_ai_check?.score != null ||
                        task.initial_ai_check?.score != null
                          ? "%"
                          : ""}
                      </p>
                    </div>
                    <div className="grid content-center gap-2 rounded-xl border px-4 py-4">
                      <div className="flex items-center justify-between gap-3">
                        <span className="text-sm text-muted-foreground">处理方式</span>
                        <Badge variant="outline">
                          {task.humanization_skipped ? "沿用初稿" : "人化后终检"}
                        </Badge>
                      </div>
                      <div className="flex items-center justify-between gap-3">
                        <span className="text-sm text-muted-foreground">截图凭证</span>
                        <span className="text-sm font-medium">
                          {task.final_ai_check?.screenshot_asset_id ? "已保存" : "待上传"}
                        </span>
                      </div>
                    </div>
                  </div>
                  {task.humanization_skipped && (
                    <Alert>
                      <ShieldCheck />
                      <AlertTitle>初稿低于 30%，已跳过人化</AlertTitle>
                      <AlertDescription>
                        截图仍可在下方上传，并会作为当前文章的 AI-rate 凭证进入交付包。
                      </AlertDescription>
                    </Alert>
                  )}
                  {humanizedDirty ? (
                    <Alert>
                      <AlertCircle />
                      <AlertTitle>等待保存后复检</AlertTitle>
                      <AlertDescription>
                        编辑内容或应用 SEO 修改后，请先保存人工审阅稿；旧 AI 率不会作为当前正文结果展示。
                      </AlertDescription>
                    </Alert>
                  ) : !task.humanization_skipped ? (
                    task.final_ai_check?.provider === "zerogpt" &&
                    task.final_ai_check.article_hash ===
                      task.humanized_article_hash && (
                      <Alert>
                        <ShieldCheck />
                        <AlertTitle>
                          {task.final_ai_check.score == null
                            ? "ZeroGPT 自动复检未完成"
                            : `ZeroGPT 自动复检：${task.final_ai_check.score}%`}
                        </AlertTitle>
                        <AlertDescription>
                          {task.final_ai_check.report ||
                            "仍可上传截图，保留人工确认凭证。"}
                        </AlertDescription>
                      </Alert>
                    )
                  ) : null}
                  <div className="grid gap-2">
                    <Label htmlFor="final-screenshot">AI-rate 截图</Label>
                    <div
                      role="group"
                      tabIndex={0}
                      aria-label="粘贴或更换 AI-rate 截图"
                      className={cn(
                        "grid min-h-32 gap-3 rounded-lg border border-dashed p-3 outline-none transition-colors",
                        "focus-visible:border-ring focus-visible:ring-ring/50 focus-visible:ring-[3px]",
                        reviewAllowed && !pending
                          ? "cursor-pointer hover:bg-muted/40"
                          : "cursor-not-allowed opacity-60",
                      )}
                      onClick={(event) => event.currentTarget.focus()}
                      onPaste={pasteFinalScreenshot}
                    >
                      {finalScreenshotPreview ? (
                        <div className="flex items-center gap-3">
                          {/* eslint-disable-next-line @next/next/no-img-element */}
                          <img
                            src={finalScreenshotPreview}
                            alt="待上传 AI-rate 截图预览"
                            className="h-24 w-32 shrink-0 rounded-md border object-contain"
                          />
                          <div className="min-w-0">
                            <p className="truncate text-sm font-medium">
                              {finalScreenshot?.name}
                            </p>
                            <p className="mt-1 text-sm text-muted-foreground">
                              已选中；点击选择或再次粘贴即可更换。
                            </p>
                          </div>
                        </div>
                      ) : (
                        <div className="flex min-h-24 flex-col items-center justify-center gap-2 text-center">
                          <ClipboardPaste className="size-5" />
                          <p className="text-sm font-medium">
                            在这里按 Ctrl+V 粘贴截图
                          </p>
                          <p className="text-sm text-muted-foreground">
                            也可以点击选择；新图片会替换当前图片。
                          </p>
                        </div>
                      )}
                      <Button
                        type="button"
                        variant="outline"
                        className="w-full"
                        disabled={Boolean(pending) || !reviewAllowed}
                        onClick={(event) => {
                          event.stopPropagation();
                          finalScreenshotInputRef.current?.click();
                        }}
                      >
                        <ImageIcon />
                        {finalScreenshot || task.final_ai_check?.screenshot_asset_id
                          ? "更换截图"
                          : "选择截图"}
                      </Button>
                    </div>
                    <Input
                      ref={finalScreenshotInputRef}
                      id="final-screenshot"
                      type="file"
                      accept="image/*"
                      className="sr-only"
                      disabled={Boolean(pending) || !reviewAllowed}
                      onChange={(event) => {
                        selectFinalScreenshot(event.target.files?.[0] ?? null);
                        event.target.value = "";
                      }}
                    />
                    {task.final_ai_check?.screenshot_asset_id && !finalScreenshot && (
                      <p className="text-sm text-muted-foreground">
                        已保存一张 AI-rate 截图；粘贴或选择新图片即可更换。
                      </p>
                    )}
                    <p className="text-xs text-muted-foreground">
                      系统只保存图片，不检查图片内容、格式或尺寸；单张最多 25MB。
                    </p>
                  </div>
                  <div className="grid gap-2">
                    <Label htmlFor="final-score">AI-rate（%）</Label>
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
                  <Button
                    type="button"
                    className="min-h-11"
                    disabled={
                      Boolean(pending) ||
                      !reviewAllowed ||
                      !allowed.has("confirm_final_ai_check") ||
                      humanizedDirty ||
                      !finalScore ||
                      (!task.humanization_skipped &&
                        !finalScreenshot &&
                        !task.final_ai_check?.screenshot_asset_id)
                    }
                    onClick={() => {
                      void (async () => {
                        const succeeded = await runAction(
                          "保存 AI-rate 凭证",
                          async () => {
                            if (finalScreenshot) {
                              await uploadFinalScreenshot(finalScreenshot);
                              const latest = await apiGet<TaskRecord>(taskApi);
                              await apiPut<TaskRecord>(
                                `${taskApi}/checks/final-ai`,
                                {
                                  revision: latest.revision ?? 0,
                                  score: Number(finalScore),
                                  report:
                                    task.final_ai_check?.report ||
                                    "人工确认当前文章的 AI-rate。",
                                  confirmed: true,
                                },
                              );
                              return;
                            }
                            await apiPut<TaskRecord>(
                              `${taskApi}/checks/final-ai`,
                              {
                                revision: task.revision ?? 0,
                                score: Number(finalScore),
                                report:
                                  task.final_ai_check?.report ||
                                  "人工确认当前文章的 AI-rate。",
                                confirmed: true,
                              },
                            );
                          },
                          "AI-rate 与截图凭证已保存。",
                        );
                        if (succeeded) setFinalScreenshot(null);
                      })();
                    }}
                  >
                    {pending === "保存 AI-rate 凭证" ? (
                      <Loader2 className="animate-spin" />
                    ) : (
                      <CheckCircle2 />
                    )}
                    {finalScreenshot
                      ? "上传截图并保存"
                      : task.final_ai_check?.screenshot_asset_id
                        ? "更新 AI-rate"
                        : task.humanization_skipped
                          ? "保存 AI-rate"
                          : "上传截图并确认"}
                  </Button>
                </CardContent>
              </Card>

              <Card>
                <CardHeader className="border-b">
                  <div className="flex items-center gap-2">
                    <BookOpenCheck className="size-5 text-sky-700 dark:text-sky-300" />
                    <CardTitle>知识库支撑率</CardTitle>
                  </div>
                  <CardDescription>
                    这里显示摘要；正文高亮、证据摘录和来源链接放在独立详情页。
                  </CardDescription>
                </CardHeader>
                <CardContent className="grid gap-3">
                  {task.knowledge_coverage?.status === "available" ? (
                    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-1 2xl:grid-cols-2">
                      <div className="rounded-xl border bg-emerald-50 px-4 py-4 dark:bg-emerald-950/30">
                        <p className="text-sm text-emerald-800 dark:text-emerald-200">正文支撑率</p>
                        <p className="mt-1 text-3xl font-semibold tabular-nums text-emerald-950 dark:text-emerald-50">
                          {Math.round(task.knowledge_coverage.sentence_coverage * 100)}%
                        </p>
                        <p className="mt-1 text-sm text-emerald-800 dark:text-emerald-200">
                          {task.knowledge_coverage.supported_sentences}/{task.knowledge_coverage.eligible_sentences} 句
                        </p>
                      </div>
                      <div className="rounded-xl border px-4 py-4">
                        <p className="text-sm text-muted-foreground">硬事实证据</p>
                        <p className="mt-1 text-2xl font-semibold tabular-nums">
                          {task.knowledge_coverage.supported_hard_fact_sentences}/{task.knowledge_coverage.hard_fact_sentences}
                        </p>
                        <p className="mt-2 text-sm text-muted-foreground">
                          共 {task.knowledge_coverage.evidence_link_count} 条证据链接
                        </p>
                      </div>
                    </div>
                  ) : (
                    <Alert>
                      <AlertCircle />
                      <AlertTitle>
                        {task.knowledge_coverage?.status === "stale"
                          ? "正文已变化，结果已失效"
                          : task.knowledge_coverage?.status === "unavailable"
                            ? "本次检查未完成"
                            : "尚未检查知识库支撑率"}
                      </AlertTitle>
                      <AlertDescription>
                        {task.knowledge_coverage?.message ||
                          "检查后会显示有项目知识支撑的正文句比例。"}
                      </AlertDescription>
                    </Alert>
                  )}
                  <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-1 2xl:grid-cols-2">
                    <Button
                      nativeButton={false}
                      className="min-h-11"
                      render={<Link href={knowledgeCoverageHref} />}
                    >
                      <BookOpenCheck />
                      查看逐句详情
                      <ArrowRight />
                    </Button>
                    <Button
                      type="button"
                      variant="outline"
                      className="min-h-11"
                      disabled={Boolean(pending) || !reviewAllowed || humanizedDirty}
                      onClick={() =>
                        void runAction(
                          "检查知识库支撑率",
                          () =>
                            apiPut<TaskRecord>(
                              `${taskApi}/checks/knowledge-coverage`,
                              { revision: task.revision ?? 0 },
                            ),
                          "知识库支撑率已按当前正文句重新计算。",
                        )
                      }
                    >
                      {pending === "检查知识库支撑率" ? (
                        <Loader2 className="animate-spin" />
                      ) : (
                        <RefreshCw />
                      )}
                      重新检查
                    </Button>
                  </div>
                  {humanizedDirty && (
                    <p className="text-xs text-muted-foreground">
                      请先保存正文，再检查当前版本的知识库支撑率。
                    </p>
                  )}
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

            </div>

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
              <CardContent className="grid gap-4 max-h-[72dvh] overflow-y-auto overscroll-contain pr-3">
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
                    {pending === "下载 Word" ? (
                      <Loader2 className="animate-spin" />
                    ) : (
                      <Download />
                    )}
                    Word
                  </Button>
                  <Button
                    type="button"
                    variant="outline"
                    className="min-h-11"
                    disabled={Boolean(pending) || !task.tdk_asset_id}
                    onClick={() => void download("下载 TDK", "tdk/download")}
                  >
                    {pending === "下载 TDK" ? (
                      <Loader2 className="animate-spin" />
                    ) : (
                      <Download />
                    )}
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
                    {pending === "下载交付 ZIP" ? (
                      <Loader2 className="animate-spin" />
                    ) : (
                      <Download />
                    )}
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
