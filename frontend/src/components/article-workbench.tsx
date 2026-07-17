"use client";

/* eslint-disable react-hooks/set-state-in-effect */

import {
  ArrowLeft,
  ExternalLink,
  FileText,
  FolderOpen,
  Loader2,
  Package,
  RefreshCw,
  RotateCcw,
  Sparkles,
  WandSparkles,
  X,
} from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { ProjectNavigation } from "@/components/project-navigation";
import { ArticleDeliveryStep } from "@/components/article-delivery-step";
import { ArticleReviewStep } from "@/components/article-review-step";
import { ArticleMediaStep } from "@/components/article-media-step";
import { ArticleDraftStep } from "@/components/article-draft-step";
import { ArticleOutlineStep } from "@/components/article-outline-step";
import { ArticleProductsStep } from "@/components/article-products-step";
import { ArticleTitleStep } from "@/components/article-title-step";
import { ArticleWritingRequirementsStep } from "@/components/article-writing-requirements-step";
import { RevisionConflictDialog } from "@/components/revision-conflict-dialog";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Progress } from "@/components/ui/progress";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Tabs, TabsContent } from "@/components/ui/tabs";
import { ApiError, apiFileUrl, apiGet, apiPost, apiPut, apiUpload } from "@/lib/api";
import { cn } from "@/lib/utils";
import type {
  ApiMessage,
  ArticleImage,
  BatchCreateResponse,
  BatchJobRecord,
  BatchOperation,
  BatchRecord,
  DashboardSummary,
  Product,
  PublicConfig,
  TaskRecord,
  WorkflowStatus,
} from "@/types";

const STATUS_LABELS: Record<WorkflowStatus, string> = {
  new: "待生成标题",
  titles_ready: "待选标题",
  title_selected: "已选标题",
  outline_ready: "大纲待确认",
  outline_confirmed: "大纲已确认",
  draft_ready: "待 ZeroGPT 初检",
  initial_ai_checked: "初检已完成",
  humanized_ready: "待 ZeroGPT 复检",
  final_ai_checked: "待恢复链接",
  links_verified: "待准备图片",
  images_ready: "可导出 Word",
  docx_exported: "已导出 Word",
};

const STATUS_FILTERS: Array<"all" | WorkflowStatus> = [
  "all",
  "new",
  "titles_ready",
  "title_selected",
  "outline_ready",
  "outline_confirmed",
  "draft_ready",
  "initial_ai_checked",
  "humanized_ready",
  "final_ai_checked",
  "links_verified",
  "images_ready",
  "docx_exported",
];

const PRODUCT_ASSET_TIMEOUT_MS = 15 * 60 * 1000;
const QUICK_SAVE_TIMEOUT_MS = 20_000;

type WorkbenchTab =
  | "titles"
  | "products"
  | "requirements"
  | "outline"
  | "article"
  | "review"
  | "media"
  | "files";

function isWorkbenchTab(value: string | undefined): value is WorkbenchTab {
  return Boolean(value && WORKBENCH_TABS.some((tab) => tab.value === value));
}

const WORKBENCH_TABS: Array<{
  value: WorkbenchTab;
  step: number;
  label: string;
}> = [
  { value: "titles", step: 1, label: "标题" },
  { value: "products", step: 2, label: "产品" },
  { value: "requirements", step: 3, label: "写作要求" },
  { value: "outline", step: 4, label: "大纲" },
  { value: "article", step: 5, label: "第一版" },
  { value: "review", step: 6, label: "人工处理" },
  { value: "media", step: 7, label: "图片" },
  { value: "files", step: 8, label: "交付" },
];

type WorkflowStage = "prepare" | "writing" | "review" | "media" | "delivery";

const WORKFLOW_STAGES: Array<{
  value: WorkflowStage;
  step: number;
  label: string;
  tabs: WorkbenchTab[];
}> = [
  { value: "prepare", step: 1, label: "内容准备", tabs: ["titles", "products", "requirements"] },
  { value: "writing", step: 2, label: "写作", tabs: ["outline", "article"] },
  { value: "review", step: 3, label: "人工质检", tabs: ["review"] },
  { value: "media", step: 4, label: "图片", tabs: ["media"] },
  { value: "delivery", step: 5, label: "交付", tabs: ["files"] },
];

const STATUS_PHASE: Record<WorkflowStatus, number> = {
  new: 0,
  titles_ready: 1,
  title_selected: 2,
  outline_ready: 3,
  outline_confirmed: 4,
  draft_ready: 5,
  initial_ai_checked: 6,
  humanized_ready: 7,
  final_ai_checked: 8,
  links_verified: 9,
  images_ready: 10,
  docx_exported: 11,
};

function emptyProduct(): Product {
  return { name: "", url: "", image_path: "", description: "" };
}

function normalizeImagePath(value: string) {
  return value.trim().replaceAll("\\", "/").toLowerCase();
}

type EditableProductField = "name" | "url" | "image_path" | "description";

type RunActionOptions = {
  scope?: "task" | "app";
  ownerId?: string;
  key?: string;
  refresh?: "none" | "all";
};

type EditableSection =
  | "titles"
  | "products"
  | "requirements"
  | "outline"
  | "article"
  | "review"
  | "media"
  | "files";

type ServerConflict = {
  section: EditableSection;
  latest: TaskRecord;
  message: string;
};

function actionSection(label: string): EditableSection {
  if (label.includes("标题")) return "titles";
  if (label.includes("产品") || label.includes("官网资产")) return "products";
  if (label.includes("写作要求")) return "requirements";
  if (label.includes("大纲")) return "outline";
  if (label.includes("正文")) return "article";
  if (label.includes("AI") || label.includes("链接")) return "review";
  if (label.includes("图片") || label.includes("首图") || label.includes("锚点")) return "media";
  return "files";
}

function taskSectionText(task: TaskRecord, section: EditableSection) {
  if (section === "titles") {
    return JSON.stringify(
      { selected_title: task.selected_title, candidates: task.title_candidates },
      null,
      2,
    );
  }
  if (section === "products") return JSON.stringify(task.products || [], null, 2);
  if (section === "requirements") {
    return JSON.stringify(
      {
        topic_notes: task.topic_notes || "",
        outline_custom_prompt: task.outline_custom_prompt || "",
        article_custom_prompt: task.article_custom_prompt || "",
        use_outline_custom_prompt: task.use_outline_custom_prompt ?? false,
        use_article_custom_prompt: task.use_article_custom_prompt ?? false,
        include_project_introduction: task.include_project_introduction ?? true,
        include_project_notes: task.include_project_notes ?? true,
        include_topic_notes: task.include_topic_notes ?? true,
      },
      null,
      2,
    );
  }
  if (section === "outline") return currentOutlineDraft(task);
  if (section === "article") return currentFirstVersion(task);
  if (section === "review") {
    return JSON.stringify(
      {
        humanized_article: currentHumanizedVersion(task),
        initial_ai_check: task.initial_ai_check,
        final_ai_check: task.final_ai_check,
      },
      null,
      2,
    );
  }
  if (section === "media") {
    return JSON.stringify(
      { hero_image: task.hero_image || "", images: task.images || [] },
      null,
      2,
    );
  }
  return JSON.stringify(
    {
      docx_path: task.docx_path || "",
      tdk_path: task.tdk_path || "",
      delivery_package_path: task.delivery_package_path || "",
    },
    null,
    2,
  );
}

function statusLabel(status: string) {
  return STATUS_LABELS[status as WorkflowStatus] ?? status;
}

function statusVariant(status: WorkflowStatus) {
  if (status === "docx_exported") return "default";
  if (status === "images_ready" || status === "links_verified") return "default";
  if (
    status === "draft_ready" ||
    status === "outline_ready" ||
    status === "outline_confirmed" ||
    status === "humanized_ready" ||
    status === "final_ai_checked"
  ) {
    return "secondary";
  }
  if (status === "titles_ready" || status === "title_selected") return "outline";
  return "ghost";
}

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "Unknown error";
}

function englishWordCount(value: string) {
  const visible = value
    .replace(/^\s*img\.[^\r\n]+\.webp\s*$/gim, "")
    .replace(/!\[[^\]]*\]\([^)]+\)/g, "")
    .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1");
  return visible.match(/[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)*/g)?.length ?? 0;
}

function recommendedTab(task: TaskRecord): WorkbenchTab {
  if (task.status === "new" || task.status === "titles_ready") return "titles";
  if (task.status === "title_selected") return "products";
  if (task.status === "outline_ready") return "outline";
  if (task.status === "outline_confirmed") return "article";
  if (
    task.status === "draft_ready" ||
    task.status === "initial_ai_checked" ||
    task.status === "humanized_ready" ||
    task.status === "final_ai_checked"
  ) {
    return "review";
  }
  if (task.status === "links_verified" || task.status === "images_ready") {
    return "media";
  }
  return "files";
}

function isTaskRecordResult(value: unknown): value is TaskRecord {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<TaskRecord>;
  return Boolean(candidate.id && candidate.customer && candidate.status);
}

function currentFirstVersion(task: TaskRecord | null): string {
  if (!task) return "";
  return task.initial_article || task.raw_draft_article || task.article || "";
}

function currentOutlineDraft(task: TaskRecord | null): string {
  if (!task) return "";
  return task.outline_draft || task.outline || "";
}

function currentHumanizedVersion(task: TaskRecord | null): string {
  if (!task) return "";
  return task.linked_article || task.humanized_article || task.final_article || "";
}

export function ArticleWorkbench({
  customer,
  initialTaskId,
  initialStep,
  focusMode = false,
}: {
  customer?: string;
  initialTaskId?: string;
  initialStep?: string;
  focusMode?: boolean;
}) {
  const [dashboard, setDashboard] = useState<DashboardSummary | null>(null);
  const [config, setConfig] = useState<PublicConfig | null>(null);
  const [tasks, setTasks] = useState<TaskRecord[]>([]);
  const [selectedTask, setSelectedTask] = useState<TaskRecord | null>(null);
  const [statusFilter, setStatusFilter] = useState<"all" | WorkflowStatus>("all");
  const [query, setQuery] = useState("");
  const [titleChoice, setTitleChoice] = useState("");
  const [outlineText, setOutlineText] = useState("");
  const [articleText, setArticleText] = useState("");
  const [topicNotes, setTopicNotes] = useState("");
  const [outlineCustomPrompt, setOutlineCustomPrompt] = useState("");
  const [articleCustomPrompt, setArticleCustomPrompt] = useState("");
  const [useOutlineCustomPrompt, setUseOutlineCustomPrompt] = useState(false);
  const [useArticleCustomPrompt, setUseArticleCustomPrompt] = useState(false);
  const [includeProjectIntroduction, setIncludeProjectIntroduction] = useState(true);
  const [includeProjectNotes, setIncludeProjectNotes] = useState(true);
  const [includeTopicNotes, setIncludeTopicNotes] = useState(true);
  const [humanizedText, setHumanizedText] = useState("");
  const [initialAiScore, setInitialAiScore] = useState("");
  const [initialAiReport, setInitialAiReport] = useState("");
  const [finalAiScore, setFinalAiScore] = useState("");
  const [finalAiReport, setFinalAiReport] = useState("");
  const [heroImage, setHeroImage] = useState("");
  const [heroUpload, setHeroUpload] = useState<File | null>(null);
  const [heroUploadPreview, setHeroUploadPreview] = useState("");
  const [heroPreviewFailed, setHeroPreviewFailed] = useState(false);
  const [products, setProducts] = useState<Product[]>([emptyProduct()]);
  const [activeTab, setActiveTab] = useState<WorkbenchTab>(
    isWorkbenchTab(initialStep) ? initialStep : "titles",
  );
  const [pendingActions, setPendingActions] = useState<Record<string, string>>({});
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [serverConflict, setServerConflict] = useState<ServerConflict | null>(null);
  const [batchSelectedIds, setBatchSelectedIds] = useState<Set<string>>(new Set());
  const [batches, setBatches] = useState<BatchRecord[]>([]);
  const [batchBusy, setBatchBusy] = useState("");
  const lastBatchUpdate = useRef("");
  const lastTabTaskId = useRef("");
  const dirtySectionsRef = useRef<Set<EditableSection>>(new Set());
  const hydratedTaskIdRef = useRef("");

  const projectName = customer ? decodeURIComponent(customer) : "";
  const taskListPath = projectName
    ? `/api/tasks?customer=${encodeURIComponent(projectName)}`
    : "/api/tasks";

  const selectTab = useCallback(
    (tab: WorkbenchTab) => {
      setActiveTab(tab);
      if (!focusMode || typeof window === "undefined") return;
      const url = new URL(window.location.href);
      url.searchParams.set("step", tab);
      window.history.replaceState(window.history.state, "", url);
    },
    [focusMode],
  );

  const loadData = useCallback(async (preferredTaskId?: string) => {
    const focusedTaskId = preferredTaskId ?? initialTaskId;
    const taskRequest = focusMode && focusedTaskId
      ? apiGet<TaskRecord>(`/api/tasks/${focusedTaskId}`).then((task) => [task])
      : apiGet<TaskRecord[]>(taskListPath);
    const [nextDashboard, nextConfig, nextTasks] = await Promise.all([
      apiGet<DashboardSummary>("/api/dashboard"),
      apiGet<PublicConfig>("/api/config"),
      taskRequest,
    ]);
    setDashboard(nextDashboard);
    setConfig(nextConfig);
    setTasks(nextTasks);
    setSelectedTask((current) => {
      const preferred =
        preferredTaskId ?? current?.id ?? (focusMode ? initialTaskId : undefined);
      if (focusMode && preferred) {
        return nextTasks.find((task) => task.id === preferred) ?? null;
      }
      return (
        nextTasks.find((task) => task.id === preferred) ??
        nextTasks[0] ??
        null
      );
    });
  }, [focusMode, initialTaskId, taskListPath]);

  const refreshTaskListInBackground = useCallback(async () => {
    const taskRequest = focusMode && initialTaskId
      ? apiGet<TaskRecord>(`/api/tasks/${initialTaskId}`).then((task) => [task])
      : apiGet<TaskRecord[]>(taskListPath);
    const [nextDashboard, nextTasks] = await Promise.all([
      apiGet<DashboardSummary>("/api/dashboard"),
      taskRequest,
    ]);
    setDashboard(nextDashboard);
    setTasks(nextTasks);
    // Batch polling may refresh a clean task, but must not replace text being edited.
    setSelectedTask((current) => {
      if (!current) return nextTasks[0] ?? null;
      return nextTasks.find((task) => task.id === current.id) ?? current;
    });
  }, [focusMode, initialTaskId, taskListPath]);

  const refreshBatches = useCallback(async () => {
    const path = projectName
      ? `/api/batches?customer=${encodeURIComponent(projectName)}&limit=8`
      : "/api/batches?limit=8";
    const nextBatches = await apiGet<BatchRecord[]>(path);
    const updateKey = nextBatches
      .map((batch) => `${batch.id}:${batch.updated_at}:${batch.completed}`)
      .join("|");
    if (lastBatchUpdate.current && lastBatchUpdate.current !== updateKey) {
      await refreshTaskListInBackground();
    }
    lastBatchUpdate.current = updateKey;
    setBatches(nextBatches);
    return nextBatches;
  }, [projectName, refreshTaskListInBackground]);

  useEffect(() => {
    loadData(initialTaskId).catch((err) => setError(errorMessage(err)));
  }, [initialTaskId, loadData]);

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    const poll = async () => {
      let delay = 12_000;
      try {
        const nextBatches = await refreshBatches();
        const hasActive = nextBatches.some(
          (batch) => batch.status === "queued" || batch.status === "running",
        );
        delay = hasActive ? 2500 : 12_000;
      } catch (err) {
        if (!cancelled) setError(errorMessage(err));
      } finally {
        if (!cancelled) timer = window.setTimeout(poll, delay);
      }
    };
    void poll();
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [refreshBatches]);

  useEffect(() => {
    const availableIds = new Set(tasks.map((task) => task.id));
    setBatchSelectedIds(
      (current) => new Set([...current].filter((taskId) => availableIds.has(taskId))),
    );
  }, [tasks]);

  useEffect(() => {
    const taskChanged = hydratedTaskIdRef.current !== (selectedTask?.id || "");
    const dirty = dirtySectionsRef.current;
    if (taskChanged || !dirty.has("titles")) {
      setTitleChoice(selectedTask?.selected_title || "");
    }
    if (taskChanged || !dirty.has("outline")) {
      setOutlineText(currentOutlineDraft(selectedTask));
    }
    if (taskChanged || !dirty.has("requirements")) {
      setTopicNotes(selectedTask?.topic_notes || "");
      setOutlineCustomPrompt(selectedTask?.outline_custom_prompt || "");
      setArticleCustomPrompt(selectedTask?.article_custom_prompt || "");
      setUseOutlineCustomPrompt(selectedTask?.use_outline_custom_prompt ?? false);
      setUseArticleCustomPrompt(selectedTask?.use_article_custom_prompt ?? false);
      setIncludeProjectIntroduction(selectedTask?.include_project_introduction ?? true);
      setIncludeProjectNotes(selectedTask?.include_project_notes ?? true);
      setIncludeTopicNotes(selectedTask?.include_topic_notes ?? true);
    }
    if (taskChanged || !dirty.has("article")) {
      setArticleText(currentFirstVersion(selectedTask));
    }
    if (taskChanged || !dirty.has("review")) {
      setHumanizedText(currentHumanizedVersion(selectedTask));
      setInitialAiScore(
        selectedTask?.initial_ai_check?.score == null
          ? ""
          : String(selectedTask.initial_ai_check.score),
      );
      setInitialAiReport(
        selectedTask?.initial_ai_check?.report || selectedTask?.zero_gpt_report || "",
      );
      setFinalAiScore(
        selectedTask?.final_ai_check?.score == null
          ? ""
          : String(selectedTask.final_ai_check.score),
      );
      setFinalAiReport(selectedTask?.final_ai_check?.report || "");
    }
    if (taskChanged || !dirty.has("media")) {
      setHeroImage(selectedTask?.hero_image || "");
      setHeroUpload(null);
    }
    if (taskChanged || !dirty.has("products")) {
      setProducts(selectedTask?.products?.length ? selectedTask.products : [emptyProduct()]);
    }
    hydratedTaskIdRef.current = selectedTask?.id || "";
  }, [selectedTask]);

  useEffect(() => {
    if (selectedTask && lastTabTaskId.current !== selectedTask.id) {
      const firstFocusedTask = focusMode && lastTabTaskId.current === "";
      lastTabTaskId.current = selectedTask.id;
      selectTab(
        firstFocusedTask && isWorkbenchTab(initialStep)
          ? initialStep
          : recommendedTab(selectedTask),
      );
    }
  }, [focusMode, initialStep, selectedTask, selectTab]);

  useEffect(() => {
    if (!heroUpload) {
      setHeroUploadPreview("");
      return;
    }
    const previewUrl = URL.createObjectURL(heroUpload);
    setHeroUploadPreview(previewUrl);
    return () => URL.revokeObjectURL(previewUrl);
  }, [heroUpload]);

  const filteredTasks = useMemo(() => {
    const normalizedQuery = query.trim().toLowerCase();
    return tasks.filter((task) => {
      const matchesStatus =
        statusFilter === "all" || task.status === statusFilter;
      const haystack = [
        task.customer,
        task.topic,
        task.competitor_keyword,
        task.competitor_blog,
        task.selected_title,
      ]
        .join(" ")
        .toLowerCase();
      return matchesStatus && (!normalizedQuery || haystack.includes(normalizedQuery));
    });
  }, [query, statusFilter, tasks]);
  const allFilteredBatchTasksSelected =
    filteredTasks.length > 0 &&
    filteredTasks.every((task) => batchSelectedIds.has(task.id));

  const completedCount = useMemo(
    () => tasks.filter((task) => task.status === "docx_exported").length,
    [tasks],
  );
  const pendingCount = tasks.length - completedCount;
  const completion = tasks.length
    ? Math.round((completedCount / tasks.length) * 100)
    : 0;

  async function runAction<T>(
    label: string,
    action: () => Promise<T>,
    after?: (result: T) => void,
    options: RunActionOptions = {},
  ) {
    const scope = options.scope ?? "task";
    const ownerId = options.ownerId ?? selectedTask?.id ?? "unselected";
    const section = actionSection(label);
    const pendingKey = options.key ??
      `${scope === "app" ? "app" : `task:${ownerId}`}:${section}`;
    setPendingActions((current) => ({ ...current, [pendingKey]: label }));
    setError("");
    setMessage("");
    try {
      const result = await action();
      if (isTaskRecordResult(result)) {
        setTasks((current) => {
          const exists = current.some((task) => task.id === result.id);
          return exists
            ? current.map((task) => (task.id === result.id ? result : task))
            : [...current, result];
        });
        setSelectedTask((current) =>
          current?.id === result.id ? result : current,
        );
        void apiGet<DashboardSummary>("/api/dashboard")
          .then(setDashboard)
          .catch(() => undefined);
      } else if (options.refresh === "all") {
        await loadData(ownerId === "unselected" ? undefined : ownerId);
      }
      setMessage(label);
      after?.(result);
    } catch (err) {
      if (
        err instanceof ApiError &&
        err.status === 409 &&
        /revision conflict/i.test(err.message) &&
        scope === "task" &&
        ownerId !== "unselected"
      ) {
        try {
          const latest = await apiGet<TaskRecord>(`/api/tasks/${ownerId}`);
          setTasks((current) =>
            current.map((task) => (task.id === latest.id ? latest : task)),
          );
          setSelectedTask((current) => (current?.id === latest.id ? latest : current));
          setServerConflict({ section, latest, message: err.message });
          setError("");
        } catch (refreshError) {
          setError(`${err.message} 无法加载服务器最新版本：${errorMessage(refreshError)}`);
        }
      } else {
        setError(errorMessage(err));
      }
    } finally {
      setPendingActions((current) => {
        const next = { ...current };
        delete next[pendingKey];
        return next;
      });
    }
  }

  async function syncTasks() {
    await runAction<ApiMessage>(
      "话题库已同步",
      () => apiPost<ApiMessage>("/api/sync-tasks"),
      (result) => setMessage(result.message),
      { scope: "app", refresh: "all" },
    );
  }

  function toggleBatchTask(taskId: string, checked: boolean) {
    setBatchSelectedIds((current) => {
      const next = new Set(current);
      if (checked) next.add(taskId);
      else next.delete(taskId);
      return next;
    });
  }

  function toggleFilteredBatchTasks(checked: boolean) {
    setBatchSelectedIds((current) => {
      const next = new Set(current);
      for (const task of filteredTasks) {
        if (checked) next.add(task.id);
        else next.delete(task.id);
      }
      return next;
    });
  }

  async function startBatch(operation: BatchOperation) {
    if (!batchSelectedIds.size) {
      setError("请先勾选要批量处理的文章。");
      return;
    }
    const selectedBatchTasks = tasks.filter((task) => batchSelectedIds.has(task.id));
    if (operation === "outline") {
      const replacementCount = selectedBatchTasks.filter((task) => task.outline.trim()).length;
      const downstreamCount = selectedBatchTasks.filter(
        (task) => STATUS_PHASE[task.status] > STATUS_PHASE.outline_confirmed,
      ).length;
      if (
        replacementCount > 0 &&
        !window.confirm(
          `选中的任务中有 ${replacementCount} 篇已有大纲。重新生成会替换这些大纲；其中 ${downstreamCount} 篇已有正文或后续结果，这些结果会清空。确定加入批量队列吗？`,
        )
      ) {
        return;
      }
    }
    if (operation === "products") {
      const downstreamCount = selectedBatchTasks.filter(
        (task) => STATUS_PHASE[task.status] >= STATUS_PHASE.outline_ready,
      ).length;
      if (
        downstreamCount > 0 &&
        !window.confirm(
          `选中的任务中有 ${downstreamCount} 篇已经进入大纲或后续阶段。重新查找产品会清空这些任务的大纲和后续结果。确定加入批量队列吗？`,
        )
      ) {
        return;
      }
    }
    if (
      operation === "rewrite_article" &&
      !window.confirm(
        `确定仅重写选中的 ${batchSelectedIds.size} 篇正文吗？每篇文章的标题、产品、大纲和写作要求会保留，正文之后的人工检测、链接、图片和导出结果会失效。`,
      )
    ) {
      return;
    }
    setBatchBusy(operation);
    setError("");
    setMessage("");
    try {
      const result = await apiPost<BatchCreateResponse>("/api/batches", {
        operation,
        task_ids: [...batchSelectedIds],
        word_count: config?.article.default_word_count ?? 1200,
      });
      const acceptedIds = new Set(result.batch?.jobs.map((job) => job.task_id) ?? []);
      setBatchSelectedIds(
        (current) => new Set([...current].filter((taskId) => !acceptedIds.has(taskId))),
      );
      if (result.batch) {
        const rejectedSummary = result.rejected
          .slice(0, 3)
          .map((item) => `${item.task_id}: ${item.message}`)
          .join("；");
        setMessage(
          `已加入 ${result.batch.total} 篇；后端并行处理，关闭或刷新页面不会中断。${
            result.rejected.length
              ? ` 另有 ${result.rejected.length} 篇未加入：${rejectedSummary}${
                  result.rejected.length > 3 ? "；其余仍保留勾选" : ""
                }`
              : ""
          }`,
        );
      } else {
        setError(result.rejected.map((item) => item.message).join("；") || "没有可加入队列的任务。");
      }
      await refreshBatches();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBatchBusy("");
    }
  }

  async function enqueueSingleOperation(operation: BatchOperation, successLabel: string) {
    if (!selectedTask) return;
    const blockingSections: Partial<Record<BatchOperation, EditableSection[]>> = {
      titles: ["titles"],
      products: ["products"],
      outline: ["outline", "requirements"],
      article: ["outline", "article", "requirements"],
      rewrite_article: ["outline", "article", "requirements"],
      humanize: ["review"],
      restore_links: ["review"],
      export_docx: ["article", "review", "media"],
      generate_tdk: ["article", "review", "media"],
      package_delivery: ["article", "review", "media"],
    };
    const dirtyBlockers = (blockingSections[operation] || []).filter((section) =>
      dirtySectionsRef.current.has(section),
    );
    if (dirtyBlockers.length) {
      setError("当前步骤存在未保存修改，请先保存或撤销修改，再加入后台队列。");
      return;
    }
    setBatchBusy(`single:${operation}`);
    setError("");
    setMessage("");
    try {
      const result = await apiPost<BatchCreateResponse>("/api/batches", {
        operation,
        task_ids: [selectedTask.id],
        word_count: config?.article.default_word_count ?? 1200,
      });
      if (!result.batch) {
        setError(result.rejected.map((item) => item.message).join("；") || "任务未能加入后台队列。");
        return;
      }
      setMessage(`${successLabel}已加入后台队列。现在可以切换页面或处理其他文章。`);
      await refreshBatches();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBatchBusy("");
    }
  }

  async function cancelBatch(batchId: string) {
    setBatchBusy(batchId);
    setError("");
    try {
      await apiPost<BatchRecord>(`/api/batches/${batchId}/cancel`);
      setMessage("已请求取消该批次；正在调用模型的条目会在返回后停止保存。");
      await refreshBatches();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBatchBusy("");
    }
  }

  async function retryBatchJob(jobId: string) {
    setBatchBusy(jobId);
    setError("");
    try {
      await apiPost<BatchJobRecord>(`/api/batch-jobs/${jobId}/retry`);
      setMessage("失败条目已按当前文章版本重新加入队列。");
      await refreshBatches();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBatchBusy("");
    }
  }

  function selectTask(task: TaskRecord): boolean {
    if (selectedTask?.id === task.id) return true;
    if (
      unsavedSections.length &&
      !window.confirm(
        `当前任务还有未保存内容：${unsavedSections.join("、")}。\n\n确定放弃这些修改并切换任务吗？`,
      )
    ) {
      return false;
    }
    setSelectedTask(task);
    setError("");
    setMessage("");
    return true;
  }

  function openBatchTask(taskId: string) {
    const task = tasks.find((candidate) => candidate.id === taskId);
    if (!task) {
      setError("当前任务列表中找不到这篇文章，请先刷新任务数据。");
      return;
    }
    setStatusFilter("all");
    setQuery("");
    if (!selectTask(task)) return;
    setError("");
    setMessage(
      `已定位到 ${task.customer} / topic_${String(task.topic_index).padStart(3, "0")}`,
    );
    window.requestAnimationFrame(() => {
      document
        .getElementById("task-workbench")
        ?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  }

  async function copyArticle(value: string, label: string) {
    if (!value.trim()) {
      setError("没有可复制的正文");
      return;
    }
    await navigator.clipboard.writeText(value);
    setError("");
    setMessage(label);
  }

  function optionalScore(value: string) {
    if (!value.trim()) return null;
    const score = Number(value);
    return Number.isFinite(score) ? score : null;
  }

  function uploadAiScreenshot(stage: "initial" | "final", file: File) {
    if (!selectedId) return;
    const body = new FormData();
    body.append("file", file);
    void runAction(
      stage === "initial" ? "初检 AI 率截图已保存" : "复检 AI 率截图已保存",
      () =>
        apiUpload<TaskRecord>(
          `/api/tasks/${selectedId}/checks/${stage}-ai/screenshot?revision=${selectedTask?.revision ?? 0}`,
          body,
        ),
    );
  }

  function generateOrRegenerateArticle() {
    if (!selectedId || !selectedTask) return;
    if (writingSettingsDirty) {
      setError("写作要求有未保存修改，请先保存写作要求，再生成正文。");
      return;
    }
    if (hasGeneratedFirstVersion) {
      const confirmed = window.confirm(
        "确定只重写正文吗？当前第一版将被替换，AI 检测、降 AI 文本、链接、图片、Word、TDK 和交付状态都会清空；标题、产品、已确认大纲和写作要求都会保留。",
      );
      if (!confirmed) return;
    }
    void enqueueSingleOperation(
      hasGeneratedFirstVersion ? "rewrite_article" : "article",
      hasGeneratedFirstVersion ? "正文重写" : "正文生成",
    );
  }

  function generateOrRegenerateOutline() {
    if (!selectedId || !selectedTask) return;
    if (writingSettingsDirty) {
      setError("写作要求有未保存修改，请先保存写作要求，再生成大纲。");
      return;
    }
    if (selectedTask.outline.trim() || outlineDirty) {
      const impact = outlineHasDownstream
        ? "标题、产品和写作要求会保留；当前正文、AI 检测、链接、图片及导出结果会清空。"
        : "当前大纲会被新生成的大纲替换。";
      if (
        !window.confirm(
          `确定重新生成大纲吗？${outlineDirty ? "当前未保存的大纲修改也会丢失。" : ""}\n\n${impact}`,
        )
      ) {
        return;
      }
    }
    void enqueueSingleOperation("outline", selectedTask.outline.trim() ? "大纲重新生成" : "大纲生成");
  }

  function saveOutline(confirmed: boolean) {
    if (!selectedId || !selectedTask || !canSaveOutline) return;
    if (
      confirmed &&
      outlineDirty &&
      outlineHasDownstream &&
      !window.confirm(
        "保存修改后的大纲会保留标题、产品和写作要求，并清空正文、AI 检测、链接、图片及导出结果。确定继续吗？",
      )
    ) {
      return;
    }
    void runAction(
      confirmed ? "大纲已保存并确认" : "大纲草稿已保存",
      () =>
        apiPut<TaskRecord>(
          `/api/tasks/${selectedId}/outline`,
          {
            revision: selectedTask.revision,
            outline: outlineText,
            confirmed,
          },
          QUICK_SAVE_TIMEOUT_MS,
        ),
      undefined,
      { key: `task:${selectedId}:outline` },
    );
  }

  function restoreVersion(versionIndex: number, kind: string) {
    if (!selectedId || !selectedTask) return;
    const version = selectedTask.article_versions?.[versionIndex];
    if (!version) return;
    const restoringOutline = kind === "outline" || kind === "outline_draft";
    const impact = restoringOutline
      ? "该版本会恢复到大纲草稿，不会立即替换已确认大纲，也不会清空正文。检查后可再点击“保存并确认”。"
      : "该版本会替换当前第一版，并清空 AI 检测、降 AI 稿、链接、图片、Word、D 文档和交付包。";
    if (!window.confirm(`确定恢复这个版本吗？\n\n${impact}`)) return;

    if (restoringOutline) {
      dirtySectionsRef.current.delete("outline");
      setOutlineText(version.content);
    } else {
      dirtySectionsRef.current.delete("article");
      setArticleText(version.content);
    }
    void runAction(
      restoringOutline ? "大纲版本已恢复为草稿" : "第一版正文已恢复",
      () =>
        apiPost<TaskRecord>(`/api/tasks/${selectedId}/versions/restore`, {
          revision: selectedTask.revision,
          version_index: versionIndex,
        }),
      undefined,
      { key: `task:${selectedId}:${restoringOutline ? "outline" : "article"}` },
    );
  }

  function localSectionText(section: EditableSection) {
    if (section === "titles") {
      return JSON.stringify(
        { selected_title: titleChoice, candidates: selectedTask?.title_candidates || [] },
        null,
        2,
      );
    }
    if (section === "products") return JSON.stringify(persistedProducts(), null, 2);
    if (section === "requirements") {
      return JSON.stringify(
        {
          topic_notes: topicNotes,
          outline_custom_prompt: outlineCustomPrompt,
          article_custom_prompt: articleCustomPrompt,
          use_outline_custom_prompt: useOutlineCustomPrompt,
          use_article_custom_prompt: useArticleCustomPrompt,
          include_project_introduction: includeProjectIntroduction,
          include_project_notes: includeProjectNotes,
          include_topic_notes: includeTopicNotes,
        },
        null,
        2,
      );
    }
    if (section === "outline") return outlineText;
    if (section === "article") return articleText;
    if (section === "review") {
      return JSON.stringify(
        {
          humanized_article: humanizedText,
          initial_ai_score: initialAiScore,
          initial_ai_report: initialAiReport,
          final_ai_score: finalAiScore,
          final_ai_report: finalAiReport,
        },
        null,
        2,
      );
    }
    if (section === "media") {
      return JSON.stringify(
        { hero_image: heroImage, images: selectedTask?.images || [] },
        null,
        2,
      );
    }
    return selectedTask ? taskSectionText(selectedTask, "files") : "";
  }

  function adoptServerConflict() {
    if (!serverConflict) return;
    const latest = serverConflict.latest;
    const section = serverConflict.section;
    if (section === "titles") setTitleChoice(latest.selected_title || "");
    if (section === "products") {
      setProducts(latest.products?.length ? latest.products : [emptyProduct()]);
    }
    if (section === "requirements") {
      setTopicNotes(latest.topic_notes || "");
      setOutlineCustomPrompt(latest.outline_custom_prompt || "");
      setArticleCustomPrompt(latest.article_custom_prompt || "");
      setUseOutlineCustomPrompt(latest.use_outline_custom_prompt ?? false);
      setUseArticleCustomPrompt(latest.use_article_custom_prompt ?? false);
      setIncludeProjectIntroduction(latest.include_project_introduction ?? true);
      setIncludeProjectNotes(latest.include_project_notes ?? true);
      setIncludeTopicNotes(latest.include_topic_notes ?? true);
    }
    if (section === "outline") setOutlineText(currentOutlineDraft(latest));
    if (section === "article") setArticleText(currentFirstVersion(latest));
    if (section === "review") {
      setHumanizedText(currentHumanizedVersion(latest));
      setInitialAiScore(
        latest.initial_ai_check?.score == null ? "" : String(latest.initial_ai_check.score),
      );
      setInitialAiReport(
        latest.initial_ai_check?.report || latest.zero_gpt_report || "",
      );
      setFinalAiScore(
        latest.final_ai_check?.score == null ? "" : String(latest.final_ai_check.score),
      );
      setFinalAiReport(latest.final_ai_check?.report || "");
    }
    if (section === "media") {
      setHeroImage(latest.hero_image || "");
      setHeroUpload(null);
    }
    dirtySectionsRef.current.delete(section);
    setSelectedTask(latest);
    setServerConflict(null);
    setMessage("已采用服务器版本，本地冲突草稿已放弃。");
  }

  function keepLocalConflict() {
    if (!serverConflict) return;
    setSelectedTask(serverConflict.latest);
    setServerConflict(null);
    setMessage("已保留本地修改并更新到服务器最新修订号，请检查后重新保存。");
  }

  function saveHeroPath() {
    if (!selectedId || !selectedTask || !heroImage.trim()) return;
    if (
      heroDirty &&
      ["images_ready", "docx_exported"].includes(selectedTask.status) &&
      !window.confirm(
        "更换首图后需要重新准备图片并重新导出 Word。确定保存新的首图吗？",
      )
    ) {
      return;
    }
    void runAction(
      "首图设置已保存",
      () =>
        apiPut<TaskRecord>(
          `/api/tasks/${selectedId}/images`,
          {
            revision: selectedTask.revision,
            hero_image: heroImage,
          },
          QUICK_SAVE_TIMEOUT_MS,
        ),
      undefined,
      { key: `task:${selectedId}:media` },
    );
  }

  function saveHeroThenPrepare() {
    if (!selectedId || !selectedTask || (!heroUpload && !heroImage.trim())) return;
    if (
      heroDirty &&
      ["images_ready", "docx_exported"].includes(selectedTask.status) &&
      !window.confirm(
        "更换首图后会重新准备图片，现有 Word 需要重新导出。确定继续吗？",
      )
    ) {
      return;
    }
    void runAction(
      "首图已保存，图片准备已加入后台队列",
      async () => {
        let saved = selectedTask;
        if (heroUpload) {
          const body = new FormData();
          body.append("file", heroUpload);
          saved = await apiUpload<TaskRecord>(
            `/api/tasks/${selectedId}/images/upload?role=hero&revision=${saved.revision ?? 0}`,
            body,
          );
        } else if (heroImage.trim() !== (selectedTask.hero_image || "").trim()) {
          saved = await apiPut<TaskRecord>(
            `/api/tasks/${selectedId}/images`,
            {
              revision: saved.revision,
              hero_image: heroImage.trim(),
            },
            QUICK_SAVE_TIMEOUT_MS,
          );
        }
        const queued = await apiPost<BatchCreateResponse>("/api/batches", {
          operation: "prepare_images",
          task_ids: [selectedId],
        });
        if (!queued.batch) {
          throw new Error(
            queued.rejected.map((item) => item.message).join("；") ||
              "图片准备任务未能加入后台队列。",
          );
        }
        void refreshBatches();
        return saved;
      },
      (result) => {
        setHeroImage(result.hero_image || heroImage.trim());
        setHeroUpload(null);
      },
      { key: `task:${selectedId}:media` },
    );
  }

  function saveBodyImageOverrides(nextImages: ArticleImage[], label: string) {
    if (!selectedId || !selectedTask) return;
    if (activeTaskJob) {
      setError("当前文章已有后台任务，请等待完成或先取消，再调整图片槽位。");
      return;
    }
    void runAction(
      `${label}，图片准备已加入后台队列`,
      async () => {
        const saved = await apiPut<TaskRecord>(
          `/api/tasks/${selectedId}/images`,
          {
            revision: selectedTask.revision,
            hero_image: heroImage.trim() || selectedTask.hero_image || "",
            images: nextImages.slice(0, 3),
          },
          QUICK_SAVE_TIMEOUT_MS,
        );
        const queued = await apiPost<BatchCreateResponse>("/api/batches", {
          operation: "prepare_images",
          task_ids: [selectedId],
        });
        if (!queued.batch) {
          throw new Error(
            queued.rejected.map((item) => item.message).join("；") ||
              "图片准备任务未能加入后台队列。",
          );
        }
        void refreshBatches();
        return saved;
      },
      undefined,
      { key: `task:${selectedId}:media` },
    );
  }

  function selectBodyImage(product: Product, slotIndex: number) {
    if (!selectedTask || !product.image_path.trim()) return;
    const existing = selectedTask.images || [];
    const hero = existing.find((image) => image.role === "hero");
    const body = existing.filter((image) => image.role !== "hero");
    if (slotIndex > body.length) {
      setError("请先选择前一个正文图片槽位。");
      return;
    }
    const selectedKey = normalizeImagePath(product.image_path);
    const duplicateSlot = body.findIndex(
      (image, index) =>
        index !== slotIndex && normalizeImagePath(image.source_path) === selectedKey,
    );
    if (duplicateSlot >= 0) {
      setError(`该图片已经用于正文图槽位 ${duplicateSlot + 2}，不能重复使用。`);
      return;
    }
    const previous = body[slotIndex];
    body[slotIndex] = {
      id: previous?.id || `manual-product-${slotIndex + 1}`,
      role: "product",
      source_path: product.image_path,
      prepared_path: "",
      filename: "",
      marker: "",
      product_name: product.name,
      product_url: product.url,
      anchor_heading: previous?.anchor_heading || "",
      anchor_text: previous?.anchor_text || "",
      anchor_after: previous?.anchor_after || "",
      status: "pending",
      error: "",
    };
    saveBodyImageOverrides(
      [...(hero ? [hero] : []), ...body],
      `正文图槽位 ${slotIndex + 2} 已更新`,
    );
  }

  function moveBodyImage(slotIndex: number, direction: -1 | 1) {
    if (!selectedTask) return;
    const existing = selectedTask.images || [];
    const hero = existing.find((image) => image.role === "hero");
    const body = existing.filter((image) => image.role !== "hero");
    const target = slotIndex + direction;
    if (target < 0 || target >= body.length) return;
    [body[slotIndex], body[target]] = [body[target], body[slotIndex]];
    saveBodyImageOverrides(
      [...(hero ? [hero] : []), ...body],
      "正文图片顺序已调整",
    );
  }

  function confirmProductChanges(action: string): boolean {
    if (!selectedTask || STATUS_PHASE[selectedTask.status] < STATUS_PHASE.outline_ready) {
      return true;
    }
    return window.confirm(
      `${action}可能改变文章引用的产品事实或图片，因此会清空当前大纲和后续正文、检测、图片及导出结果。标题和写作要求会保留。确定继续吗？`,
    );
  }

  function saveProducts() {
    if (!selectedId || !selectedTask || !productsDirty) return;
    if (!confirmProductChanges("保存产品修改")) return;
    void runAction(
      "产品已保存",
      () =>
        apiPut<TaskRecord>(
          `/api/tasks/${selectedId}/products`,
          {
            revision: selectedTask.revision,
            products: persistedProducts(),
          },
          QUICK_SAVE_TIMEOUT_MS,
        ),
      undefined,
      { key: `task:${selectedId}:products` },
    );
  }

  function saveWritingSettings() {
    if (!selectedId || !selectedTask) return;
    void runAction("写作要求已保存", () =>
      apiPut<TaskRecord>(
        `/api/tasks/${selectedId}/writing-settings`,
        {
          revision: selectedTask.revision,
          topic_notes: topicNotes,
          outline_custom_prompt: outlineCustomPrompt,
          article_custom_prompt: articleCustomPrompt,
          use_outline_custom_prompt: useOutlineCustomPrompt,
          use_article_custom_prompt: useArticleCustomPrompt,
          include_project_introduction: includeProjectIntroduction,
          include_project_notes: includeProjectNotes,
          include_topic_notes: includeTopicNotes,
        },
        QUICK_SAVE_TIMEOUT_MS,
      ),
    );
  }

  function rewriteFromScratch() {
    if (!selectedId || !selectedTask) return;
    const confirmed = window.confirm(
      "确定要完全重写这篇文章吗？当前标题、产品、大纲、正文、AI 检测、链接、图片及导出状态都会清空，并回到“待生成标题”。项目目录中的旧文件不会直接删除。",
    );
    if (!confirmed) return;
    void runAction("任务已回滚，可从标题开始完全重写", () =>
      apiPost<TaskRecord>(`/api/tasks/${selectedId}/rewrite-from-scratch`, {
        revision: selectedTask.revision,
      }),
    );
  }

  function persistedProducts() {
    return products.filter((product) =>
      [product.name, product.url, product.image_path, product.description].some(
        (value) => value.trim(),
      ),
    );
  }

  function updateProduct(index: number, key: EditableProductField, value: string) {
    setProducts((current) =>
      current.map((product, productIndex) =>
        productIndex === index
          ? key === "url" && product.url.trim() !== value.trim()
            ? {
                ...product,
                url: value,
                product_id: "",
                canonical_url: "",
                image_path: "",
                reference_summary: "",
                reference_facts: [],
                specifications: {},
                reference_path: "",
                asset_manifest_path: "",
                asset_count: 0,
                selected_asset_id: "",
                selection_confidence: null,
                selection_reason: "",
                discovery_source: "",
                detail_page_verified: false,
                asset_status: "",
                asset_error: "",
              }
            : { ...product, [key]: value }
          : product,
      ),
    );
  }

  const selectedId = selectedTask?.id;
  const activeTaskJob = batches
    .flatMap((batch) => batch.jobs)
    .find(
      (job) =>
        job.task_id === selectedId &&
        ["queued", "running", "retry_wait"].includes(job.status),
    );
  const savedHeroPreview =
    selectedId && heroImage.trim()
      ? apiFileUrl(
          `/api/tasks/${selectedId}/images/preview?path=${encodeURIComponent(heroImage.trim())}`,
        )
      : "";
  const heroPreviewUrl = heroUploadPreview || savedHeroPreview;
  const pendingEntries = Object.entries(pendingActions);
  const currentTaskPrefix = selectedId ? `task:${selectedId}:` : "";
  const sectionPending = (section: EditableSection) =>
    currentTaskPrefix ? pendingActions[`${currentTaskPrefix}${section}`] : undefined;
  const appPending = pendingEntries.find(([key]) => key.startsWith("app:"));
  const isCurrentTaskBusy = Boolean(
    currentTaskPrefix && pendingEntries.some(([key]) => key.startsWith(currentTaskPrefix)),
  );
  const currentPending = pendingEntries.find(
    ([key]) =>
      key.startsWith("app:") ||
      (currentTaskPrefix && key === `${currentTaskPrefix}${activeTab}`),
  );
  const busy = currentPending?.[1] ?? "";
  const isBusy = Boolean(currentPending);
  const isAnyBusy = pendingEntries.length > 0;

  useEffect(() => {
    setHeroPreviewFailed(false);
  }, [heroPreviewUrl]);
  const canAction = (action: string) =>
    selectedTask?.allowed_actions == null ||
    selectedTask.allowed_actions.includes(action);
  const articleTarget = config?.article.default_word_count ?? 1200;
  const articleCharacterTarget = Math.round((articleTarget * 6.67) / 100) * 100;
  const articleWords = englishWordCount(articleText);
  const humanizedWords = englishWordCount(humanizedText);
  const humanizedEditRollsBack = selectedTask
    ? ["final_ai_checked", "links_verified", "images_ready", "docx_exported"].includes(
        selectedTask.status,
      )
    : false;
  const hasGeneratedFirstVersion = Boolean(
    selectedTask?.initial_article ||
      selectedTask?.raw_draft_article ||
      (selectedTask?.status !== "outline_confirmed" && selectedTask?.article),
  );
  const outlineDirty = Boolean(
    selectedTask && outlineText !== currentOutlineDraft(selectedTask),
  );
  const articleDirty = Boolean(
    selectedTask && articleText !== currentFirstVersion(selectedTask),
  );
  const humanizedDirty = Boolean(
    selectedTask && humanizedText !== currentHumanizedVersion(selectedTask),
  );
  const heroDirty = Boolean(
    selectedTask &&
      (heroUpload || heroImage.trim() !== (selectedTask.hero_image || "").trim()),
  );
  const productsDirty = Boolean(
    selectedTask &&
      JSON.stringify(persistedProducts()) !== JSON.stringify(selectedTask.products || []),
  );
  const writingSettingsDirty = Boolean(
    selectedTask &&
      (topicNotes !== (selectedTask.topic_notes || "") ||
        outlineCustomPrompt !== (selectedTask.outline_custom_prompt || "") ||
        articleCustomPrompt !== (selectedTask.article_custom_prompt || "") ||
        useOutlineCustomPrompt !== (selectedTask.use_outline_custom_prompt ?? false) ||
        useArticleCustomPrompt !== (selectedTask.use_article_custom_prompt ?? false) ||
        includeProjectIntroduction !==
          (selectedTask.include_project_introduction ?? true) ||
        includeProjectNotes !== (selectedTask.include_project_notes ?? true) ||
        includeTopicNotes !== (selectedTask.include_topic_notes ?? true)),
  );
  const titleDirty = Boolean(
    selectedTask && titleChoice && titleChoice !== selectedTask.selected_title,
  );
  const initialReviewDirty = Boolean(
    selectedTask &&
      (initialAiScore !==
        (selectedTask.initial_ai_check?.score == null
          ? ""
          : String(selectedTask.initial_ai_check.score)) ||
        initialAiReport !==
          (selectedTask.initial_ai_check?.report || selectedTask.zero_gpt_report || "")),
  );
  const finalReviewDirty = Boolean(
    selectedTask &&
      (finalAiScore !==
        (selectedTask.final_ai_check?.score == null
          ? ""
          : String(selectedTask.final_ai_check.score)) ||
        finalAiReport !== (selectedTask.final_ai_check?.report || "")),
  );
  const unsavedSections = [
    titleDirty && "标题选择",
    productsDirty && "产品",
    writingSettingsDirty && "写作要求",
    outlineDirty && "大纲",
    articleDirty && "第一版",
    (humanizedDirty || initialReviewDirty || finalReviewDirty) && "人工处理",
    heroDirty && "首图",
  ].filter((label): label is string => Boolean(label));
  const dirtyTabs = new Set<WorkbenchTab>([
    ...(titleDirty ? (["titles"] as WorkbenchTab[]) : []),
    ...(productsDirty ? (["products"] as WorkbenchTab[]) : []),
    ...(writingSettingsDirty ? (["requirements"] as WorkbenchTab[]) : []),
    ...(outlineDirty ? (["outline"] as WorkbenchTab[]) : []),
    ...(articleDirty ? (["article"] as WorkbenchTab[]) : []),
    ...(humanizedDirty || initialReviewDirty || finalReviewDirty
      ? (["review"] as WorkbenchTab[])
      : []),
    ...(heroDirty ? (["media"] as WorkbenchTab[]) : []),
  ]);
  const unsavedKey = unsavedSections.join("、");
  const suggestedTab = selectedTask ? recommendedTab(selectedTask) : "titles";
  const suggestedTabLabel =
    WORKBENCH_TABS.find((tab) => tab.value === suggestedTab)?.label || "标题";
  const activeStage =
    WORKFLOW_STAGES.find((stage) => stage.tabs.includes(activeTab)) ?? WORKFLOW_STAGES[0];
  const outlineHasDownstream = Boolean(
    selectedTask && STATUS_PHASE[selectedTask.status] > STATUS_PHASE.outline_confirmed,
  );
  const outlineNeedsConfirmation = Boolean(
    selectedTask &&
      (selectedTask.status === "outline_ready" ||
        currentOutlineDraft(selectedTask) !== selectedTask.outline),
  );
  const canSaveOutline = Boolean(
    outlineText.trim() && (outlineDirty || outlineNeedsConfirmation),
  );

  useEffect(() => {
    const next = new Set<EditableSection>();
    if (titleDirty) next.add("titles");
    if (productsDirty) next.add("products");
    if (writingSettingsDirty) next.add("requirements");
    if (outlineDirty) next.add("outline");
    if (articleDirty) next.add("article");
    if (humanizedDirty || initialReviewDirty || finalReviewDirty) next.add("review");
    if (heroDirty) next.add("media");
    dirtySectionsRef.current = next;
  }, [
    articleDirty,
    finalReviewDirty,
    heroDirty,
    humanizedDirty,
    initialReviewDirty,
    outlineDirty,
    productsDirty,
    titleDirty,
    writingSettingsDirty,
  ]);

  useEffect(() => {
    if (!unsavedKey) return;
    const warnBeforeLeave = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warnBeforeLeave);
    return () => window.removeEventListener("beforeunload", warnBeforeLeave);
  }, [unsavedKey]);

  return (
    <main className="min-h-screen bg-background text-foreground">
      <RevisionConflictDialog
        open={Boolean(serverConflict)}
        message={serverConflict?.message || ""}
        localValue={serverConflict ? localSectionText(serverConflict.section) : ""}
        serverValue={serverConflict ? taskSectionText(serverConflict.latest, serverConflict.section) : ""}
        onAdoptServer={adoptServerConflict}
        onKeepLocal={keepLocalConflict}
      />
      <div className="border-b bg-[color-mix(in_oklch,var(--background),var(--accent)_22%)]">
        <div className="mx-auto max-w-[1500px] px-5 py-5">
          {projectName && focusMode && (
            <ProjectNavigation customer={projectName} className="mb-4" />
          )}
          <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              {projectName && !focusMode && (
                <Button
                  variant="outline"
                  size="icon-sm"
                  nativeButton={false}
                  render={<Link href="/" />}
                >
                  <ArrowLeft />
                </Button>
              )}
              <h1 className="text-xl font-semibold tracking-normal">
                {projectName || "Article Workflow Agent"}
              </h1>
              <Badge variant={dashboard?.llm_ready ? "default" : "outline"}>
                {dashboard?.llm_ready ? "LLM Ready" : "Mock LLM"}
              </Badge>
              <Badge variant={config?.integrations?.tavily_ready ? "default" : "outline"}>
                {config?.integrations?.tavily_ready
                  ? "官网搜索已连接"
                  : "官网搜索未配置"}
              </Badge>
            </div>
            <div className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-sm text-muted-foreground">
              <span>长期任务，不按周重复创建</span>
              <span>{config?.output_root}</span>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Button
              variant="outline"
              onClick={() => {
                if (
                  unsavedSections.length &&
                  !window.confirm(
                    `刷新会放弃未保存内容：${unsavedSections.join("、")}。确定刷新吗？`,
                  )
                ) {
                  return;
                }
                void runAction(
                  "数据已刷新",
                  () => loadData(),
                  undefined,
                  { scope: "app", key: "app:refresh" },
                );
              }}
              disabled={isAnyBusy}
            >
              <RefreshCw />
              刷新
            </Button>
            {!focusMode && (
              <Button onClick={syncTasks} disabled={isAnyBusy}>
                {isAnyBusy ? <Loader2 className="animate-spin" /> : <Sparkles />}
                同步话题库
              </Button>
            )}
          </div>
          </div>
        </div>
      </div>

      <div className="mx-auto grid max-w-[1500px] gap-4 px-5 py-5">
        {(error || message) && (
          <Alert
            className={cn(
              "rounded-lg",
              error
                ? "border-destructive/40 bg-destructive/5"
                : "border-emerald-600/30 bg-emerald-50",
            )}
          >
            <AlertTitle>{error ? "操作失败" : "状态"}</AlertTitle>
            <AlertDescription>{error || message}</AlertDescription>
          </Alert>
        )}

        {!focusMode && <section className="grid gap-3 md:grid-cols-4">
          <SummaryCard
            title={projectName ? "项目任务" : "客户"}
            value={projectName ? tasks.length : dashboard?.customer_count ?? 0}
          />
          <SummaryCard
            title={projectName ? "待处理" : "任务"}
            value={projectName ? pendingCount : dashboard?.task_count ?? 0}
          />
          <SummaryCard title="已导出" value={completedCount} />
          <Card className="rounded-lg">
            <CardHeader>
              <CardTitle>完成率</CardTitle>
              <CardDescription>{completion}%</CardDescription>
            </CardHeader>
            <CardContent>
              <Progress value={completion} />
            </CardContent>
          </Card>
        </section>}

        <section
          className={cn(
            "grid min-h-[720px] gap-4",
            !focusMode &&
              "xl:grid-cols-[minmax(460px,0.95fr)_minmax(0,1.05fr)]",
          )}
        >
          {!focusMode && <Card className="min-w-0 rounded-lg">
            <CardHeader className="border-b">
              <CardTitle>任务队列</CardTitle>
              <CardDescription>
                {filteredTasks.length} / {tasks.length}
              </CardDescription>
              <CardAction>
                <Input
                  className="h-8 w-[220px]"
                  placeholder={projectName ? "搜索话题、标题、竞品" : "搜索客户、话题、标题"}
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                />
              </CardAction>
            </CardHeader>
            <CardContent className="grid gap-3">
              <div className="grid gap-3 rounded-lg border bg-muted/20 p-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div>
                    <div className="text-sm font-medium">批量生成中心</div>
                    <div className="text-xs text-muted-foreground">
                      已选 {batchSelectedIds.size} 篇；写作并发 3、找产品并发 2，同一文章不会重复执行。
                    </div>
                  </div>
                  {batchSelectedIds.size > 0 && (
                    <Button
                      size="xs"
                      variant="ghost"
                      onClick={() => setBatchSelectedIds(new Set())}
                    >
                      清空选择
                    </Button>
                  )}
                </div>
                <div className="flex flex-wrap gap-2">
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={Boolean(batchBusy) || !batchSelectedIds.size}
                    onClick={() => void startBatch("titles")}
                  >
                    {batchBusy === "titles" ? <Loader2 className="animate-spin" /> : <Sparkles />}
                    批量生成标题
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={Boolean(batchBusy) || !batchSelectedIds.size}
                    onClick={() => void startBatch("products")}
                  >
                    {batchBusy === "products" ? <Loader2 className="animate-spin" /> : <Package />}
                    批量找产品
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={Boolean(batchBusy) || !batchSelectedIds.size}
                    onClick={() => void startBatch("outline")}
                  >
                    {batchBusy === "outline" ? <Loader2 className="animate-spin" /> : <WandSparkles />}
                    批量生成大纲
                  </Button>
                  <Button
                    size="sm"
                    disabled={Boolean(batchBusy) || !batchSelectedIds.size}
                    onClick={() => void startBatch("article")}
                  >
                    {batchBusy === "article" ? <Loader2 className="animate-spin" /> : <FileText />}
                    批量生成正文
                  </Button>
                  <Button
                    size="sm"
                    variant="secondary"
                    disabled={Boolean(batchBusy) || !batchSelectedIds.size}
                    onClick={() => void startBatch("rewrite_article")}
                  >
                    {batchBusy === "rewrite_article" ? <Loader2 className="animate-spin" /> : <RefreshCw />}
                    批量仅重写正文
                  </Button>
                </div>
                {batches.length > 0 && (
                  <BatchQueuePanel
                    batches={batches}
                    busy={batchBusy}
                    onCancel={(batchId) => void cancelBatch(batchId)}
                    onRetry={(jobId) => void retryBatchJob(jobId)}
                    onOpenTask={openBatchTask}
                  />
                )}
              </div>
              <div className="flex flex-wrap gap-2">
                {STATUS_FILTERS.map((status) => (
                  <Button
                    key={status}
                    size="sm"
                    variant={statusFilter === status ? "default" : "outline"}
                    onClick={() => setStatusFilter(status)}
                  >
                    {status === "all" ? "全部" : statusLabel(status)}
                  </Button>
                ))}
              </div>
              <ScrollArea
                className={cn(
                  "rounded-lg border",
                  batches.length ? "h-[410px]" : "h-[585px]",
                )}
              >
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead className="w-[42px]">
                        <input
                          type="checkbox"
                          className="h-4 w-4 accent-emerald-700"
                          aria-label="选择当前筛选结果"
                          checked={allFilteredBatchTasksSelected}
                          onChange={(event) =>
                            toggleFilteredBatchTasks(event.target.checked)
                          }
                        />
                      </TableHead>
                      {projectName && <TableHead className="w-[100px]">编号</TableHead>}
                      {!projectName && <TableHead className="w-[170px]">客户</TableHead>}
                      <TableHead>话题</TableHead>
                      <TableHead className="w-[120px]">状态</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {filteredTasks.map((task) => (
                      <TableRow
                        key={task.id}
                        className={cn(
                          "cursor-pointer",
                          selectedTask?.id === task.id && "bg-accent/60",
                        )}
                        onClick={() => selectTask(task)}
                      >
                        <TableCell
                          onClick={(event) => event.stopPropagation()}
                        >
                          <input
                            type="checkbox"
                            className="h-4 w-4 accent-emerald-700"
                            aria-label={`选择 topic_${String(task.topic_index).padStart(3, "0")}`}
                            checked={batchSelectedIds.has(task.id)}
                            onChange={(event) =>
                              toggleBatchTask(task.id, event.target.checked)
                            }
                          />
                        </TableCell>
                        {projectName ? (
                          <TableCell className="text-xs text-muted-foreground">
                            topic_{String(task.topic_index).padStart(3, "0")}
                          </TableCell>
                        ) : (
                          <TableCell>
                            <div className="font-medium">{task.customer}</div>
                            <div className="text-xs text-muted-foreground">
                              topic_{String(task.topic_index).padStart(3, "0")}
                            </div>
                          </TableCell>
                        )}
                        <TableCell>
                          <div className="line-clamp-2 max-w-[360px]">
                            {task.topic}
                          </div>
                          {task.selected_title && (
                            <div className="mt-1 line-clamp-1 text-xs text-muted-foreground">
                              {task.selected_title}
                            </div>
                          )}
                        </TableCell>
                        <TableCell>
                          <Badge variant={statusVariant(task.status)}>
                            {statusLabel(task.status)}
                          </Badge>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </ScrollArea>
            </CardContent>
          </Card>}

          <Card id="task-workbench" className="min-w-0 rounded-lg scroll-mt-4">
            <CardHeader className="border-b">
              <CardTitle>文章工作台</CardTitle>
              <CardDescription>
                {selectedTask
                  ? `${selectedTask.customer} / topic_${String(
                      selectedTask.topic_index,
                    ).padStart(3, "0")}`
                  : "未选择任务"}
              </CardDescription>
              {selectedTask && (
                <CardAction className="flex items-center gap-2">
                  {selectedTask.status !== "new" && (
                    <Button
                      size="sm"
                      variant="destructive"
                      onClick={rewriteFromScratch}
                      disabled={
                        Boolean(appPending) ||
                        isCurrentTaskBusy ||
                        !canAction("rewrite_from_scratch")
                      }
                    >
                      <RotateCcw />
                      完全重写
                    </Button>
                  )}
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() =>
                      selectedId &&
                      runAction("当前项目目录已打开", () =>
                        apiPost<ApiMessage>(`/api/tasks/${selectedId}/open-folder`),
                      )
                    }
                    disabled={Boolean(appPending) || Boolean(sectionPending("files"))}
                  >
                    <FolderOpen />
                    打开项目目录
                  </Button>
                  <Badge variant={statusVariant(selectedTask.status)}>
                    {statusLabel(selectedTask.status)}
                  </Badge>
                </CardAction>
              )}
            </CardHeader>
            <CardContent>
              {!selectedTask ? (
                <div className="flex h-[610px] items-center justify-center rounded-lg border border-dashed text-sm text-muted-foreground">
                  {focusMode ? "找不到指定文章，请返回文章列表重新进入。" : "同步话题库后选择一行"}
                </div>
              ) : (
                <Tabs
                  value={activeTab}
                  onValueChange={(value) => selectTab(value as WorkbenchTab)}
                  className="h-full min-w-0"
                >
                  <div className="grid grid-cols-2 gap-2 rounded-lg bg-muted p-1 sm:grid-cols-5">
                    {WORKFLOW_STAGES.map((stage) => {
                      const stageBusy = stage.tabs.some((tab) => sectionPending(tab));
                      const stageDirty = stage.tabs.some((tab) => dirtyTabs.has(tab));
                      return (
                        <Button
                          key={stage.value}
                          type="button"
                          size="sm"
                          variant={activeStage.value === stage.value ? "default" : "ghost"}
                          className="gap-2"
                          onClick={() => selectTab(stage.tabs[0])}
                        >
                          <span className="text-[10px] opacity-70">{stage.step}</span>
                          {stage.label}
                          {stageBusy && <Loader2 className="size-3 animate-spin" />}
                          {stageDirty && (
                            <span className="size-1.5 rounded-full bg-amber-500" />
                          )}
                        </Button>
                      );
                    })}
                  </div>

                  {activeStage.tabs.length > 1 && (
                    <div className="mt-2 flex flex-wrap gap-2 border-b pb-2">
                      {activeStage.tabs.map((tabValue) => {
                        const tab = WORKBENCH_TABS.find((item) => item.value === tabValue);
                        if (!tab) return null;
                        return (
                          <Button
                            key={tab.value}
                            type="button"
                            size="sm"
                            variant={activeTab === tab.value ? "secondary" : "ghost"}
                            onClick={() => selectTab(tab.value)}
                          >
                            {tab.label}
                            {sectionPending(tab.value) && (
                              <Loader2 className="size-3 animate-spin" />
                            )}
                            {dirtyTabs.has(tab.value) && (
                              <span className="size-1.5 rounded-full bg-amber-500" />
                            )}
                          </Button>
                        );
                      })}
                    </div>
                  )}

                  <div className="mt-3 flex flex-wrap items-center gap-2 rounded-lg border bg-muted/20 px-3 py-2 text-sm">
                    <span className="text-muted-foreground">建议下一步：</span>
                    <Button
                      size="xs"
                      variant={activeTab === suggestedTab ? "secondary" : "outline"}
                      onClick={() => selectTab(suggestedTab)}
                    >
                      {suggestedTabLabel}
                    </Button>
                    {unsavedSections.length > 0 && (
                      <Badge variant="outline" className="border-amber-300 text-amber-800">
                        未保存：{unsavedSections.join("、")}
                      </Badge>
                    )}
                    {busy && (
                      <Badge variant="secondary" className="ml-auto gap-1">
                        <Loader2 className="size-3 animate-spin" />
                        {busy}
                      </Badge>
                    )}
                    {activeTaskJob && (
                      <Badge variant="secondary" className="ml-auto gap-1">
                        <Loader2 className="size-3 animate-spin" />
                        后台{batchOperationLabel(activeTaskJob.operation)}：
                        {batchJobStatusLabel(activeTaskJob.status)}
                      </Badge>
                    )}
                  </div>

                  <TabsContent value="titles" className="min-w-0 pt-4">

                    <ArticleTitleStep

                      task={selectedTask}

                      titleChoice={titleChoice}

                      titleDirty={titleDirty}

                      busy={isBusy}

                      hasActiveJob={Boolean(activeTaskJob)}

                      canGenerate={canAction("generate_titles")}

                      canSelect={canAction("select_title")}

                      onGenerate={() => void enqueueSingleOperation("titles", "标题生成")}

                      onSelect={() => {

                        if (!selectedId) return;

                        void runAction("标题已保存", () =>

                          apiPost<TaskRecord>(

                            `/api/tasks/${selectedId}/select-title`,

                            {

                              revision: selectedTask.revision,

                              title: titleChoice,

                            },

                          ),

                        );

                      }}

                      onTitleChoiceChange={setTitleChoice}

                    />

                  </TabsContent>

                  <TabsContent value="products" className="min-w-0 pt-4">

                    <ArticleProductsStep

                      products={products}

                      productsDirty={productsDirty}

                      busy={isBusy}

                      hasActiveJob={Boolean(activeTaskJob)}

                      canUpdate={Boolean(selectedId) && canAction("update_products")}

                      canRevalidateAssets={persistedProducts().some((product) =>

                        product.url.trim(),

                      )}

                      onAdd={() => setProducts((current) => [...current, emptyProduct()])}

                      onRemove={(index) =>

                        setProducts((current) =>

                          current.filter((_, productIndex) => productIndex !== index),

                        )

                      }

                      onUpdate={updateProduct}

                      onAutoFetch={() => {

                        if (!selectedId || !selectedTask) return;

                        if (!confirmProductChanges("自动重新查找产品")) return;

                        void enqueueSingleOperation("products", "官网产品与资产抓取");

                      }}

                      onRevalidateAssets={() => {

                        if (!selectedId || !selectedTask) return;

                        if (!confirmProductChanges("重新核验官网资产")) return;

                        void runAction("官网资产已重新抓取并选图", async () => {

                          let revision = selectedTask.revision ?? 0;

                          if (productsDirty) {

                            const saved = await apiPut<TaskRecord>(

                              `/api/tasks/${selectedId}/products`,

                              {

                                revision,

                                products: persistedProducts(),

                              },

                              QUICK_SAVE_TIMEOUT_MS,

                            );

                            revision = saved.revision ?? revision + 1;

                          }

                          return apiPost<TaskRecord>(

                            `/api/tasks/${selectedId}/products/assets?revision=${revision}`,

                            undefined,

                            PRODUCT_ASSET_TIMEOUT_MS,

                          );

                        });

                      }}

                      onSave={saveProducts}

                    />

                  </TabsContent>

                  <TabsContent value="requirements" className="min-w-0 pt-4">

                    <ArticleWritingRequirementsStep

                      task={selectedTask}

                      topicNotes={topicNotes}

                      includeProjectIntroduction={includeProjectIntroduction}

                      includeProjectNotes={includeProjectNotes}

                      includeTopicNotes={includeTopicNotes}

                      useOutlineCustomPrompt={useOutlineCustomPrompt}

                      outlineCustomPrompt={outlineCustomPrompt}

                      useArticleCustomPrompt={useArticleCustomPrompt}

                      articleCustomPrompt={articleCustomPrompt}

                      dirty={writingSettingsDirty}

                      busy={isBusy}

                      canSave={Boolean(selectedId)}

                      onTopicNotesChange={setTopicNotes}

                      onIncludeProjectIntroductionChange={setIncludeProjectIntroduction}

                      onIncludeProjectNotesChange={setIncludeProjectNotes}

                      onIncludeTopicNotesChange={setIncludeTopicNotes}

                      onUseOutlineCustomPromptChange={setUseOutlineCustomPrompt}

                      onOutlineCustomPromptChange={setOutlineCustomPrompt}

                      onUseArticleCustomPromptChange={setUseArticleCustomPrompt}

                      onArticleCustomPromptChange={setArticleCustomPrompt}

                      onSave={saveWritingSettings}

                    />

                  </TabsContent>

<TabsContent value="outline" className="min-w-0 pt-4">
                    <ArticleOutlineStep
                      task={selectedTask}
                      outlineText={outlineText}
                      outlineDirty={outlineDirty}
                      outlineNeedsConfirmation={outlineNeedsConfirmation}
                      outlineHasDownstream={outlineHasDownstream}
                      canSaveOutline={canSaveOutline}
                      busy={isBusy}
                      hasActiveJob={Boolean(activeTaskJob)}
                      canAction={canAction}
                      onOutlineChange={setOutlineText}
                      onGenerate={generateOrRegenerateOutline}
                      onSaveDraft={() => saveOutline(false)}
                      onSaveAndConfirm={() => saveOutline(true)}
                      onRestore={restoreVersion}
                    />
                  </TabsContent>

                  <TabsContent value="article" className="min-w-0 pt-4">
                    <ArticleDraftStep
                      task={selectedTask}
                      articleText={articleText}
                      articleTarget={articleTarget}
                      articleCharacterTarget={articleCharacterTarget}
                      articleWords={articleWords}
                      hasGeneratedFirstVersion={hasGeneratedFirstVersion}
                      articleDirty={articleDirty}
                      busy={isBusy}
                      hasActiveJob={Boolean(activeTaskJob)}
                      canAction={canAction}
                      onArticleChange={setArticleText}
                      onGenerate={generateOrRegenerateArticle}
                      onSave={() => {
                        if (!selectedId) return;
                        void runAction("正文已保存", () =>
                          apiPut<TaskRecord>(
                            `/api/tasks/${selectedId}/article`,
                            {
                              revision: selectedTask.revision,
                              article: articleText,
                            },
                            QUICK_SAVE_TIMEOUT_MS,
                          ),
                        );
                      }}
                      onRestore={restoreVersion}
                    />
                  </TabsContent>

                  <TabsContent value="review" className="min-w-0 pt-4">
                    <ArticleReviewStep
                      task={selectedTask}
                      config={config}
                      initialArticle={selectedTask.initial_article || articleText}
                      initialAiScore={initialAiScore}
                      initialAiReport={initialAiReport}
                      finalAiScore={finalAiScore}
                      finalAiReport={finalAiReport}
                      humanizedText={humanizedText}
                      humanizedWords={humanizedWords}
                      humanizedDirty={humanizedDirty}
                      humanizedEditRollsBack={humanizedEditRollsBack}
                      busy={isBusy}
                      hasActiveJob={Boolean(activeTaskJob)}
                      canAction={canAction}
                      onInitialAiScoreChange={setInitialAiScore}
                      onInitialAiReportChange={setInitialAiReport}
                      onFinalAiScoreChange={setFinalAiScore}
                      onFinalAiReportChange={setFinalAiReport}
                      onHumanizedTextChange={setHumanizedText}
                      onCopy={(content, label) => {
                        void copyArticle(content, label).catch((err) =>
                          setError(errorMessage(err)),
                        );
                      }}
                      onUploadScreenshot={uploadAiScreenshot}
                      onConfirmInitial={() => {
                        if (!selectedId) return;
                        void runAction("ZeroGPT 初检已确认", () =>
                          apiPut<TaskRecord>(
                            `/api/tasks/${selectedId}/checks/initial-ai`,
                            {
                              revision: selectedTask.revision,
                              score: optionalScore(initialAiScore),
                              report: initialAiReport,
                            },
                            QUICK_SAVE_TIMEOUT_MS,
                          ),
                        );
                      }}
                      onHumanize={() =>
                        void enqueueSingleOperation("humanize", "降 AI 改写")
                      }
                      onSaveHumanized={() => {
                        if (!selectedId) return;
                        void runAction(
                          humanizedEditRollsBack
                            ? "正文修改已保存，后续步骤已回退"
                            : "外部降 AI 稿已保存",
                          () =>
                            apiPut<TaskRecord>(
                              `/api/tasks/${selectedId}/humanized-article`,
                              {
                                revision: selectedTask.revision,
                                article: humanizedText,
                              },
                              QUICK_SAVE_TIMEOUT_MS,
                            ),
                        );
                      }}
                      onConfirmFinal={() => {
                        if (!selectedId) return;
                        void runAction("ZeroGPT 复检已确认", () =>
                          apiPut<TaskRecord>(
                            `/api/tasks/${selectedId}/checks/final-ai`,
                            {
                              revision: selectedTask.revision,
                              score: optionalScore(finalAiScore),
                              report: finalAiReport,
                            },
                            QUICK_SAVE_TIMEOUT_MS,
                          ),
                        );
                      }}
                      onRestoreLinks={() =>
                        void enqueueSingleOperation("restore_links", "链接恢复")
                      }
                    />
                  </TabsContent>

                  <TabsContent value="media" className="min-w-0 pt-4">
                    <ArticleMediaStep
                      task={selectedTask}
                      heroImage={heroImage}
                      products={products}
                      heroUpload={heroUpload}
                      heroPreviewUrl={heroPreviewUrl}
                      heroPreviewFailed={heroPreviewFailed}
                      heroDirty={heroDirty}
                      busy={isBusy}
                      hasActiveJob={Boolean(activeTaskJob)}
                      canAction={canAction}
                      onHeroChange={(path) => {
                        setHeroImage(path);
                        setHeroUpload(null);
                      }}
                      onHeroUploadChange={setHeroUpload}
                      onHeroPreviewError={() => setHeroPreviewFailed(true)}
                      onSelectBody={selectBodyImage}
                      onMoveBody={moveBodyImage}
                      onSaveHero={saveHeroPath}
                      onUploadHero={() => {
                        if (!selectedId || !heroUpload) return;
                        const body = new FormData();
                        body.append("file", heroUpload);
                        void runAction(
                          "首图已上传",
                          () =>
                            apiUpload<TaskRecord>(
                              `/api/tasks/${selectedId}/images/upload?role=hero&revision=${selectedTask.revision ?? 0}`,
                              body,
                            ),
                          undefined,
                          { key: `task:${selectedId}:media` },
                        );
                      }}
                      onPrepareImages={saveHeroThenPrepare}
                      onSaveAnchor={(item, candidate) => {
                        if (!selectedId) return;
                        const nextImages = (selectedTask.images ?? []).map((image) =>
                          image.id === item.id
                            ? {
                                ...image,
                                anchor_heading: candidate.anchor_heading,
                                status: "pending",
                                error: "",
                              }
                            : image,
                        );
                        void runAction("图片锚点已保存", () =>
                          apiPut<TaskRecord>(
                            `/api/tasks/${selectedId}/images`,
                            {
                              revision: selectedTask.revision,
                              hero_image: heroImage,
                              images: nextImages,
                            },
                            QUICK_SAVE_TIMEOUT_MS,
                          ),
                        );
                      }}
                    />
                  </TabsContent>

                  <TabsContent value="files" className="min-w-0 pt-4">
                    <ArticleDeliveryStep
                      task={selectedTask}
                      config={config}
                      busy={isBusy}
                      hasActiveJob={Boolean(activeTaskJob)}
                      canAction={canAction}
                      onEnqueue={(operation, label) =>
                        void enqueueSingleOperation(operation, label)
                      }
                    />
                  </TabsContent>
                </Tabs>
              )}
            </CardContent>
          </Card>
        </section>
      </div>
    </main>
  );
}

function batchOperationLabel(operation: BatchOperation) {
  const labels: Record<BatchOperation, string> = {
    titles: "生成标题",
    products: "查找产品",
    outline: "生成大纲",
    article: "生成正文",
    rewrite_article: "仅重写正文",
    humanize: "降 AI 改写",
    restore_links: "恢复链接",
    prepare_images: "准备图片",
    export_docx: "导出 Word",
    generate_tdk: "生成 TDK",
    package_delivery: "交付打包",
  };
  return labels[operation];
}

function batchJobStatusLabel(status: BatchJobRecord["status"]) {
  const labels: Record<BatchJobRecord["status"], string> = {
    queued: "排队中",
    running: "生成中",
    retry_wait: "等待重试",
    succeeded: "已完成",
    failed: "失败",
    cancelled: "已取消",
    conflict: "内容已变化",
  };
  return labels[status];
}

function BatchQueuePanel({
  batches,
  busy,
  onCancel,
  onRetry,
  onOpenTask,
}: {
  batches: BatchRecord[];
  busy: string;
  onCancel: (batchId: string) => void;
  onRetry: (jobId: string) => void;
  onOpenTask: (taskId: string) => void;
}) {
  return (
    <div className="grid gap-2 border-t pt-3">
      {batches.slice(0, 2).map((batch) => {
        const active = batch.status === "queued" || batch.status === "running";
        const progress = batch.total
          ? Math.round((batch.completed / batch.total) * 100)
          : 0;
        return (
          <div key={batch.id} className="grid gap-2 rounded-md bg-background p-2 text-xs">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex items-center gap-2">
                <span className="font-medium">{batchOperationLabel(batch.operation)}</span>
                <Badge variant={active ? "secondary" : "outline"}>
                  {batch.completed}/{batch.total}
                </Badge>
                {batch.status_counts.failed ? (
                  <span className="text-destructive">失败 {batch.status_counts.failed}</span>
                ) : null}
                {batch.status_counts.conflict ? (
                  <span className="text-amber-700">
                    内容变化 {batch.status_counts.conflict}
                  </span>
                ) : null}
              </div>
              {active && (
                <Button
                  size="xs"
                  variant="ghost"
                  disabled={Boolean(busy)}
                  onClick={() => onCancel(batch.id)}
                >
                  {busy === batch.id ? <Loader2 className="animate-spin" /> : <X />}
                  取消批次
                </Button>
              )}
            </div>
            <Progress value={progress} className="h-1.5" />
            <details className="group">
              <summary className="cursor-pointer select-none font-medium text-primary hover:underline">
                查看本批次 {batch.jobs.length} 篇任务
              </summary>
              <div className="mt-2 grid max-h-52 gap-1 overflow-y-auto pr-1">
                {batch.jobs.map((job) => {
                  const canRetry = ["failed", "cancelled", "conflict"].includes(
                    job.status,
                  );
                  return (
                    <div
                      key={job.id}
                      className="flex min-w-0 items-start justify-between gap-2 rounded border bg-muted/20 px-2 py-1.5 text-muted-foreground"
                    >
                      <div className="min-w-0">
                        <div>
                          <span className="font-medium text-foreground">
                            topic_{String(job.topic_index).padStart(3, "0")}
                          </span>{" "}
                          <span>{batchJobStatusLabel(job.status)}</span>
                        </div>
                        <div className="line-clamp-1">{job.topic}</div>
                        {job.error && (
                          <div className="line-clamp-2 text-destructive">{job.error}</div>
                        )}
                      </div>
                      <div className="flex shrink-0 items-center gap-1">
                        <Button
                          size="xs"
                          variant="ghost"
                          onClick={() => onOpenTask(job.task_id)}
                        >
                          <ExternalLink />
                          查看任务
                        </Button>
                        {canRetry && (
                          <Button
                            size="xs"
                            variant="outline"
                            disabled={Boolean(busy)}
                            onClick={() => onRetry(job.id)}
                          >
                            {busy === job.id ? (
                              <Loader2 className="animate-spin" />
                            ) : (
                              <RefreshCw />
                            )}
                            重试
                          </Button>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            </details>
          </div>
        );
      })}
    </div>
  );
}

function SummaryCard({ title, value }: { title: string; value: number }) {
  return (
    <Card className="rounded-lg">
      <CardHeader>
        <CardTitle>{title}</CardTitle>
        <CardDescription className="text-2xl font-semibold text-foreground">
          {value}
        </CardDescription>
      </CardHeader>
    </Card>
  );
}
