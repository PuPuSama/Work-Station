"use client";

/* eslint-disable react-hooks/set-state-in-effect */

import {
  CheckCircle2,
  ExternalLink,
  FileText,
  FolderOpen,
  Loader2,
  Package,
  RefreshCw,
  RotateCcw,
  Search,
  Sparkles,
  WandSparkles,
  X,
} from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { BatchOutlineReview } from "@/components/batch-outline-review";
import { BatchTitleReview } from "@/components/batch-title-review";
import { ProjectNavigation } from "@/components/project-navigation";
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
import { apiGet, apiPost } from "@/lib/api";
import { cn } from "@/lib/utils";
import type {
  ApiMessage,
  BatchCreateResponse,
  BatchJobRecord,
  BatchOperation,
  BatchRecord,
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

const OPERATIONS: Array<{
  value: BatchOperation;
  label: string;
  description: string;
}> = [
  { value: "titles", label: "批量生成标题", description: "为尚未确定标题的任务生成候选标题。" },
  { value: "products", label: "批量找产品", description: "抓取最贴近话题的官网产品与图片资产。" },
  { value: "outline", label: "批量生成大纲", description: "生成或替换大纲；已有正文的任务会回退后续结果。" },
  { value: "article", label: "批量生成正文", description: "只处理尚无第一版且已确认大纲的任务。" },
  { value: "rewrite_article", label: "批量仅重写正文", description: "保留标题、产品、大纲与写作要求，重写已有第一版。" },
];

type Assessment = {
  task: TaskRecord;
  reason: string;
};

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "Unknown error";
}

export function operationLabel(operation: BatchOperation) {
  const backgroundLabels: Partial<Record<BatchOperation, string>> = {
    humanize: "降 AI 改写",
    restore_links: "恢复链接",
    prepare_images: "准备图片",
    export_docx: "导出 Word",
    generate_tdk: "生成 TDK",
    package_delivery: "交付打包",
  };
  return OPERATIONS.find((item) => item.value === operation)?.label ?? backgroundLabels[operation] ?? operation;
}

export function operationStep(operation: BatchOperation) {
  if (operation === "titles") return "titles";
  if (operation === "products") return "products";
  if (operation === "outline") return "outline";
  if (operation === "article" || operation === "rewrite_article") return "article";
  if (operation === "humanize" || operation === "restore_links") return "review";
  if (operation === "prepare_images") return "media";
  return "files";
}

function OperationIcon({ operation }: { operation: BatchOperation }) {
  if (operation === "products") return <Package />;
  if (operation === "outline") return <WandSparkles />;
  if (operation === "article") return <FileText />;
  if (operation === "rewrite_article") return <RotateCcw />;
  return <Sparkles />;
}

function statusLabel(status: WorkflowStatus) {
  return STATUS_LABELS[status] ?? status;
}

function statusVariant(status: WorkflowStatus): "default" | "secondary" | "outline" | "ghost" {
  if (status === "docx_exported" || status === "images_ready" || status === "links_verified") {
    return "default";
  }
  if (["outline_ready", "outline_confirmed", "draft_ready", "humanized_ready", "final_ai_checked"].includes(status)) {
    return "secondary";
  }
  if (status === "titles_ready" || status === "title_selected") return "outline";
  return "ghost";
}

export function batchStatusLabel(status: BatchRecord["status"]) {
  const labels: Record<BatchRecord["status"], string> = {
    queued: "排队中",
    running: "处理中",
    succeeded: "已完成",
    cancelled: "已取消",
    completed_with_errors: "部分失败",
  };
  return labels[status];
}

export function jobStatusLabel(status: BatchJobRecord["status"]) {
  const labels: Record<BatchJobRecord["status"], string> = {
    queued: "排队中",
    running: "处理中",
    retry_wait: "等待重试",
    succeeded: "已完成",
    failed: "失败",
    cancelled: "已取消",
    conflict: "内容已变化",
  };
  return labels[status];
}

function taskHasArticle(task: TaskRecord) {
  return Boolean(task.initial_article?.trim() || task.raw_draft_article?.trim());
}

function preflightReason(
  task: TaskRecord,
  operation: BatchOperation,
  activeTaskIds: Set<string>,
) {
  if (activeTaskIds.has(task.id)) return "已有排队或执行中的批量任务";
  const requiredAction = operation === "products" ? "update_products" : operation === "titles" ? "generate_titles" : operation === "outline" ? "generate_outline" : "generate_article";
  if (!task.allowed_actions?.includes(requiredAction)) {
    return `当前状态“${statusLabel(task.status)}”不能执行此操作`;
  }
  const hasArticle = taskHasArticle(task);
  if (operation === "article" && hasArticle) return "已经存在第一版，请改用“批量仅重写正文”";
  if (operation === "rewrite_article" && !hasArticle) return "还没有第一版，请改用“批量生成正文”";
  return "";
}

function formatTime(value: string) {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : date.toLocaleString("zh-CN", { hour12: false });
}

function taskWorkbenchStep(status: WorkflowStatus) {
  if (status === "new" || status === "titles_ready") return "titles";
  if (status === "title_selected") return "products";
  if (status === "outline_ready") return "outline";
  if (status === "outline_confirmed") return "article";
  if (["draft_ready", "initial_ai_checked", "humanized_ready", "final_ai_checked"].includes(status)) {
    return "review";
  }
  if (status === "links_verified") return "media";
  return "files";
}

export function ProjectBatchCenter({ customer }: { customer: string }) {
  const projectName = customer;
  const taskPath = `/api/tasks?customer=${encodeURIComponent(projectName)}`;
  const batchPath = `/api/batches?customer=${encodeURIComponent(projectName)}&limit=30`;

  const [tasks, setTasks] = useState<TaskRecord[]>([]);
  const [batches, setBatches] = useState<BatchRecord[]>([]);
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState<"all" | WorkflowStatus>("all");
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [operation, setOperation] = useState<BatchOperation>("titles");
  const [tasksPending, setTasksPending] = useState(true);
  const [batchesPending, setBatchesPending] = useState(true);
  const [createPending, setCreatePending] = useState<BatchOperation | "">("");
  const [cancelPending, setCancelPending] = useState("");
  const [retryPending, setRetryPending] = useState<Set<string>>(new Set());
  const [titleReviewOpen, setTitleReviewOpen] = useState(false);
  const [outlineReviewOpen, setOutlineReviewOpen] = useState(false);
  const [openFolderPending, setOpenFolderPending] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const batchUpdateKey = useRef("");
  const batchesLoaded = useRef(false);

  const refreshTasks = useCallback(async (showPending = true) => {
    if (showPending) setTasksPending(true);
    try {
      const nextTasks = await apiGet<TaskRecord[]>(taskPath);
      setTasks(nextTasks);
      setSelectedIds((current) => {
        const available = new Set(nextTasks.map((task) => task.id));
        return new Set([...current].filter((taskId) => available.has(taskId)));
      });
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      if (showPending) setTasksPending(false);
    }
  }, [taskPath]);

  useEffect(() => {
    void refreshTasks();
  }, [refreshTasks]);

  useEffect(() => {
    let stopped = false;
    let timer: number | undefined;

    async function poll() {
      let delay = 12_000;
      try {
        const nextBatches = await apiGet<BatchRecord[]>(batchPath);
        if (stopped) return;
        const nextKey = nextBatches
          .map((batch) => `${batch.id}:${batch.updated_at}:${batch.completed}:${batch.status}`)
          .join("|");
        const changed = Boolean(batchUpdateKey.current && batchUpdateKey.current !== nextKey);
        batchUpdateKey.current = nextKey;
        batchesLoaded.current = true;
        setBatches(nextBatches);
        setBatchesPending(false);
        const hasActive = nextBatches.some(
          (batch) => batch.status === "queued" || batch.status === "running",
        );
        delay = hasActive ? 2500 : 12_000;
        if (changed) await refreshTasks(false);
      } catch (err) {
        if (!stopped) {
          setError(errorMessage(err));
          setBatchesPending(false);
        }
      } finally {
        if (!stopped) timer = window.setTimeout(poll, delay);
      }
    }

    if (!batchesLoaded.current) setBatchesPending(true);
    void poll();
    return () => {
      stopped = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [batchPath, refreshTasks]);

  const activeBatches = useMemo(
    () => batches.filter((batch) => batch.status === "queued" || batch.status === "running"),
    [batches],
  );
  const historyBatches = useMemo(
    () => batches.filter((batch) => batch.status !== "queued" && batch.status !== "running"),
    [batches],
  );
  const activeTaskIds = useMemo(() => {
    const ids = new Set<string>();
    for (const batch of activeBatches) {
      for (const job of batch.jobs) {
        if (["queued", "running", "retry_wait"].includes(job.status)) ids.add(job.task_id);
      }
    }
    return ids;
  }, [activeBatches]);
  const pendingTitleTasks = useMemo(
    () =>
      tasks.filter(
        (task) => task.status === "titles_ready" && task.title_candidates.length > 0,
      ),
    [tasks],
  );
  const pendingOutlineTasks = useMemo(
    () =>
      tasks.filter(
        (task) =>
          task.status === "outline_ready" &&
          Boolean((task.outline_draft || task.outline || "").trim()),
      ),
    [tasks],
  );

  const filteredTasks = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    return tasks.filter((task) => {
      if (statusFilter !== "all" && task.status !== statusFilter) return false;
      if (!normalized) return true;
      return [
        task.topic,
        task.selected_title,
        task.competitor_keyword,
        task.competitor_blog,
        `topic_${String(task.topic_index).padStart(3, "0")}`,
      ]
        .join(" ")
        .toLowerCase()
        .includes(normalized);
    });
  }, [query, statusFilter, tasks]);

  const selectedAssessments = useMemo<Assessment[]>(
    () =>
      tasks
        .filter((task) => selectedIds.has(task.id))
        .map((task) => ({ task, reason: preflightReason(task, operation, activeTaskIds) })),
    [activeTaskIds, operation, selectedIds, tasks],
  );
  const executable = selectedAssessments.filter((item) => !item.reason);
  const skipped = selectedAssessments.filter((item) => item.reason);
  const allFilteredSelected =
    filteredTasks.length > 0 && filteredTasks.every((task) => selectedIds.has(task.id));
  const filteredExecutable = filteredTasks.filter(
    (task) => !preflightReason(task, operation, activeTaskIds),
  );

  function toggleTask(taskId: string, checked: boolean) {
    setSelectedIds((current) => {
      const next = new Set(current);
      if (checked) next.add(taskId);
      else next.delete(taskId);
      return next;
    });
  }

  function toggleFiltered(checked: boolean) {
    setSelectedIds((current) => {
      const next = new Set(current);
      for (const task of filteredTasks) {
        if (checked) next.add(task.id);
        else next.delete(task.id);
      }
      return next;
    });
  }

  function selectFilteredExecutable() {
    setSelectedIds((current) => {
      const next = new Set(current);
      for (const task of filteredExecutable) next.add(task.id);
      return next;
    });
  }

  async function startBatch() {
    if (!selectedIds.size) {
      setError("请先勾选要处理的文章。");
      return;
    }
    if (!executable.length) {
      setError("当前勾选任务都不满足此操作的执行条件，请查看跳过原因。");
      return;
    }
    const operationInfo = OPERATIONS.find((item) => item.value === operation);
    const confirmed = window.confirm(
      `${operationLabel(operation)}\n\n将执行 ${executable.length} 篇，跳过 ${skipped.length} 篇。${operationInfo?.description ?? ""}\n\n确定创建批次吗？`,
    );
    if (!confirmed) return;

    setCreatePending(operation);
    setError("");
    setMessage("");
    try {
      const result = await apiPost<BatchCreateResponse>("/api/batches", {
        operation,
        task_ids: executable.map((item) => item.task.id),
      });
      if (result.batch) {
        setBatches((current) => [
          result.batch!,
          ...current.filter((batch) => batch.id !== result.batch!.id),
        ]);
        const accepted = new Set(result.batch.jobs.map((job) => job.task_id));
        setSelectedIds((current) =>
          new Set([...current].filter((taskId) => !accepted.has(taskId))),
        );
      }
      const serverRejected = result.rejected.length;
      setMessage(
        result.batch
          ? `已加入 ${result.batch.total} 篇；本地预检跳过 ${skipped.length} 篇，服务器另跳过 ${serverRejected} 篇。${operation === "titles" ? "生成完成后可在本页集中审核标题。" : operation === "outline" ? "生成完成后可在本页集中审核并确认大纲。" : ""}`
          : result.rejected.map((item) => item.message).join("；") || "没有任务进入批次。",
      );
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setCreatePending("");
    }
  }

  async function cancelBatch(batchId: string) {
    setCancelPending(batchId);
    setError("");
    try {
      const updated = await apiPost<BatchRecord>(`/api/batches/${batchId}/cancel`);
      setBatches((current) =>
        current.map((batch) => (batch.id === updated.id ? updated : batch)),
      );
      setMessage("已请求取消批次；正在执行的条目会在当前请求返回后停止保存。");
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setCancelPending("");
    }
  }

  async function retryJob(jobId: string) {
    setRetryPending((current) => new Set(current).add(jobId));
    setError("");
    try {
      const updated = await apiPost<BatchJobRecord>(`/api/batch-jobs/${jobId}/retry`);
      setBatches((current) =>
        current.map((batch) =>
          batch.id === updated.batch_id
            ? {
                ...batch,
                status: "running",
                jobs: batch.jobs.map((job) => (job.id === updated.id ? updated : job)),
              }
            : batch,
        ),
      );
      setMessage(`topic_${String(updated.topic_index).padStart(3, "0")} 已重新加入队列。`);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setRetryPending((current) => {
        const next = new Set(current);
        next.delete(jobId);
        return next;
      });
    }
  }

  function handleTitleSaved(updated: TaskRecord) {
    setTasks((current) =>
      current.map((task) => (task.id === updated.id ? updated : task)),
    );
    setError("");
    setMessage(
      `topic_${String(updated.topic_index).padStart(3, "0")} 标题已保存。`,
    );
  }

  function handleOutlineSaved(updated: TaskRecord) {
    setTasks((current) =>
      current.map((task) => (task.id === updated.id ? updated : task)),
    );
    setError("");
    setMessage(
      `topic_${String(updated.topic_index).padStart(3, "0")} 大纲已保存并确认。`,
    );
  }

  async function openTaskFolder(task: TaskRecord) {
    setOpenFolderPending(task.id);
    setError("");
    try {
      const result = await apiPost<ApiMessage>(
        `/api/tasks/${encodeURIComponent(task.id)}/open-folder`,
      );
      setMessage(result.message || `已打开 topic_${String(task.topic_index).padStart(3, "0")} 文件夹。`);
    } catch (nextError) {
      setError(errorMessage(nextError));
    } finally {
      setOpenFolderPending("");
    }
  }

  return (
    <main className="min-h-screen bg-background text-foreground">
      <div className="border-b bg-[color-mix(in_oklch,var(--background),var(--accent)_22%)]">
        <div className="mx-auto grid max-w-[1500px] gap-4 px-5 py-5">
          <ProjectNavigation customer={projectName} />
          <div className="flex flex-col gap-3 px-1 sm:flex-row sm:items-center sm:justify-between">
            <div className="min-w-0">
              <h1 className="text-xl font-semibold">批量生成中心</h1>
              <p className="text-sm text-muted-foreground">
                选择任务、执行预检并跟踪批量生成结果。
              </p>
            </div>
            <div className="flex flex-wrap gap-2">
              {pendingTitleTasks.length > 0 && (
                <Button onClick={() => setTitleReviewOpen(true)}>
                  <CheckCircle2 />
                  集中选标题（{pendingTitleTasks.length}）
                </Button>
              )}
              {pendingOutlineTasks.length > 0 && (
                <Button variant="outline" onClick={() => setOutlineReviewOpen(true)}>
                  <CheckCircle2 />
                  集中确认大纲（{pendingOutlineTasks.length}）
                </Button>
              )}
              <Button
                variant="outline"
                onClick={() => void refreshTasks()}
                disabled={tasksPending}
              >
                {tasksPending ? <Loader2 className="animate-spin" /> : <RefreshCw />}
                刷新任务
              </Button>
            </div>
          </div>
        </div>
      </div>

      <div className="mx-auto grid max-w-[1500px] gap-4 px-5 py-5">
        {(error || message) && (
          <Alert variant={error ? "destructive" : "default"}>
            <AlertTitle>{error ? "操作失败" : "状态"}</AlertTitle>
            <AlertDescription>{error || message}</AlertDescription>
          </Alert>
        )}

        <section className="grid gap-3 md:grid-cols-4">
          <SummaryCard title="项目任务" value={tasks.length} pending={tasksPending} />
          <SummaryCard title="已勾选" value={selectedIds.size} />
          <SummaryCard title="可执行" value={executable.length} />
          <SummaryCard title="运行批次" value={activeBatches.length} pending={batchesPending} />
        </section>

        {pendingTitleTasks.length > 0 && (
          <Card className="rounded-lg border-emerald-300 bg-emerald-50/45">
            <CardContent className="flex flex-col gap-3 py-4 sm:flex-row sm:items-center sm:justify-between">
              <div>
                <div className="font-medium">有 {pendingTitleTasks.length} 篇文章等待人工选标题</div>
                <p className="mt-1 text-sm text-muted-foreground">
                  可以在同一个窗口连续审核候选标题，保存后自动进入下一篇，不必逐个打开任务。
                </p>
              </div>
              <Button className="shrink-0" onClick={() => setTitleReviewOpen(true)}>
                <CheckCircle2 />
                开始集中审核
              </Button>
            </CardContent>
          </Card>
        )}

        {pendingOutlineTasks.length > 0 && (
          <Card className="rounded-lg border-sky-300 bg-sky-50/45">
            <CardContent className="flex flex-col gap-3 py-4 sm:flex-row sm:items-center sm:justify-between">
              <div>
                <div className="font-medium">有 {pendingOutlineTasks.length} 篇大纲等待人工确认</div>
                <p className="mt-1 text-sm text-muted-foreground">
                  在同一个窗口检查或修改大纲，保存并确认后自动进入下一篇。
                </p>
              </div>
              <Button
                className="shrink-0"
                variant="outline"
                onClick={() => setOutlineReviewOpen(true)}
              >
                <CheckCircle2 />
                开始集中审核大纲
              </Button>
            </CardContent>
          </Card>
        )}

        <Card className="rounded-lg">
          <CardHeader className="border-b">
            <CardTitle>1. 选择任务与操作</CardTitle>
            <CardDescription>先选择操作，再勾选文章；页面会在提交前列出可执行和跳过项。</CardDescription>
            <CardAction>
              <div className="relative">
                <Search className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
                <Input
                  className="h-8 w-[280px] pl-8"
                  placeholder="搜索话题、标题、竞品或编号"
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                />
              </div>
            </CardAction>
          </CardHeader>
          <CardContent className="grid gap-4">
            <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-5">
              {OPERATIONS.map((item) => (
                <button
                  key={item.value}
                  type="button"
                  onClick={() => setOperation(item.value)}
                  className={cn(
                    "grid min-h-24 gap-1 rounded-lg border p-3 text-left transition-colors hover:bg-accent/40",
                    operation === item.value && "border-primary bg-accent/50 ring-1 ring-primary/20",
                  )}
                >
                  <span className="flex items-center gap-2 font-medium">
                    <OperationIcon operation={item.value} />
                    {item.label}
                  </span>
                  <span className="text-xs leading-5 text-muted-foreground">{item.description}</span>
                </button>
              ))}
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

            <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border bg-muted/20 p-3">
              <div className="flex flex-wrap items-center gap-2 text-sm">
                <Badge>可执行 {executable.length}</Badge>
                <Badge variant={skipped.length ? "destructive" : "outline"}>跳过 {skipped.length}</Badge>
                <span className="text-muted-foreground">已勾选 {selectedIds.size} 篇</span>
              </div>
              <div className="flex flex-wrap gap-2">
                <Button size="sm" variant="outline" onClick={selectFilteredExecutable} disabled={!filteredExecutable.length}>
                  <CheckCircle2 />
                  勾选当前筛选可执行项
                </Button>
                {selectedIds.size > 0 && (
                  <Button size="sm" variant="ghost" onClick={() => setSelectedIds(new Set())}>
                    <X />
                    清空勾选
                  </Button>
                )}
                <Button onClick={() => void startBatch()} disabled={Boolean(createPending) || !executable.length}>
                  {createPending ? <Loader2 className="animate-spin" /> : <OperationIcon operation={operation} />}
                  创建批次（{executable.length}）
                </Button>
              </div>
            </div>

            {skipped.length > 0 && (
              <details className="rounded-lg border border-amber-300 bg-amber-50/60 p-3 text-sm">
                <summary className="cursor-pointer font-medium text-amber-900">
                  查看 {skipped.length} 篇跳过任务及原因
                </summary>
                <div className="mt-3 grid max-h-48 gap-2 overflow-y-auto">
                  {skipped.map(({ task, reason }) => (
                    <div key={task.id} className="flex flex-wrap justify-between gap-2 rounded-md bg-background px-3 py-2">
                      <span>topic_{String(task.topic_index).padStart(3, "0")} · {task.topic}</span>
                      <span className="text-amber-800">{reason}</span>
                    </div>
                  ))}
                </div>
              </details>
            )}

            <ScrollArea className="h-[480px] rounded-lg border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="w-[44px]">
                      <input
                        type="checkbox"
                        className="size-4 accent-emerald-700"
                        aria-label="选择当前筛选结果"
                        checked={allFilteredSelected}
                        onChange={(event) => toggleFiltered(event.target.checked)}
                      />
                    </TableHead>
                    <TableHead className="w-[100px]">编号</TableHead>
                    <TableHead>话题 / 标题</TableHead>
                    <TableHead className="w-[140px]">状态</TableHead>
                    <TableHead className="w-[180px]">当前操作</TableHead>
                    <TableHead className="w-[220px] text-right">快捷入口</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {filteredTasks.map((task) => {
                    const reason = preflightReason(task, operation, activeTaskIds);
                    return (
                      <TableRow key={task.id} className={selectedIds.has(task.id) ? "bg-accent/50" : undefined}>
                        <TableCell>
                          <input
                            type="checkbox"
                            className="size-4 accent-emerald-700"
                            aria-label={`选择 topic_${String(task.topic_index).padStart(3, "0")}`}
                            checked={selectedIds.has(task.id)}
                            onChange={(event) => toggleTask(task.id, event.target.checked)}
                          />
                        </TableCell>
                        <TableCell className="text-xs text-muted-foreground">
                          <Link
                            href={`/projects/${encodeURIComponent(projectName)}/articles/${encodeURIComponent(task.id)}?step=${taskWorkbenchStep(task.status)}`}
                            target="_blank"
                            className="font-mono hover:text-primary hover:underline"
                          >
                            topic_{String(task.topic_index).padStart(3, "0")}
                          </Link>
                        </TableCell>
                        <TableCell>
                          <Link
                            href={`/projects/${encodeURIComponent(projectName)}/articles/${encodeURIComponent(task.id)}?step=${taskWorkbenchStep(task.status)}`}
                            target="_blank"
                            className="line-clamp-2 max-w-[680px] hover:text-primary hover:underline"
                          >
                            {task.topic}
                          </Link>
                          {task.selected_title && <div className="mt-1 line-clamp-1 text-xs text-muted-foreground">{task.selected_title}</div>}
                        </TableCell>
                        <TableCell>
                          <Badge variant={statusVariant(task.status)}>{statusLabel(task.status)}</Badge>
                        </TableCell>
                        <TableCell>
                          {reason ? (
                            <span className="text-xs text-muted-foreground">跳过：{reason}</span>
                          ) : (
                            <Badge variant="outline" className="border-emerald-400 text-emerald-800">可执行</Badge>
                          )}
                        </TableCell>
                        <TableCell>
                          <div className="flex justify-end gap-1">
                            <Button
                              size="xs"
                              variant="outline"
                              onClick={() => void openTaskFolder(task)}
                              disabled={openFolderPending === task.id}
                            >
                              {openFolderPending === task.id ? (
                                <Loader2 className="animate-spin" />
                              ) : (
                                <FolderOpen />
                              )}
                              文件夹
                            </Button>
                            <Button
                              size="xs"
                              variant="ghost"
                              nativeButton={false}
                              render={
                                <Link
                                  href={`/projects/${encodeURIComponent(projectName)}/articles/${encodeURIComponent(task.id)}?step=${taskWorkbenchStep(task.status)}`}
                                  target="_blank"
                                />
                              }
                            >
                              <ExternalLink />
                              工作台
                            </Button>
                          </div>
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </ScrollArea>
          </CardContent>
        </Card>

        <section className="grid items-start gap-4 xl:grid-cols-2">
          <BatchSection
            title="2. 运行中的批次"
            description="活动批次自动刷新；同一轮询完成后才会安排下一轮，不会重叠请求。"
            batches={activeBatches}
            pending={batchesPending}
            empty="当前没有运行中的批次。"
            customer={projectName}
            cancelPending={cancelPending}
            retryPending={retryPending}
            onCancel={(batchId) => void cancelBatch(batchId)}
            onRetry={(jobId) => void retryJob(jobId)}
          />
          <BatchSection
            title="3. 历史批次"
            description="查看完成结果、失败原因，并将失败、取消或冲突条目重新加入队列。"
            batches={historyBatches}
            pending={batchesPending}
            empty="暂无历史批次。"
            customer={projectName}
            cancelPending={cancelPending}
            retryPending={retryPending}
            onCancel={(batchId) => void cancelBatch(batchId)}
            onRetry={(jobId) => void retryJob(jobId)}
          />
        </section>
      </div>
      <BatchTitleReview
        customer={projectName}
        tasks={tasks}
        open={titleReviewOpen}
        onOpenChange={setTitleReviewOpen}
        onTaskSaved={handleTitleSaved}
      />
      <BatchOutlineReview
        customer={projectName}
        tasks={tasks}
        open={outlineReviewOpen}
        onOpenChange={setOutlineReviewOpen}
        onTaskSaved={handleOutlineSaved}
      />
    </main>
  );
}

function SummaryCard({ title, value, pending = false }: { title: string; value: number; pending?: boolean }) {
  return (
    <Card className="rounded-lg">
      <CardHeader>
        <CardTitle>{title}</CardTitle>
        <CardDescription className="text-2xl font-semibold text-foreground">
          {pending ? <Loader2 className="animate-spin" /> : value}
        </CardDescription>
      </CardHeader>
    </Card>
  );
}

function BatchSection({
  title,
  description,
  batches,
  pending,
  empty,
  customer,
  cancelPending,
  retryPending,
  onCancel,
  onRetry,
}: {
  title: string;
  description: string;
  batches: BatchRecord[];
  pending: boolean;
  empty: string;
  customer: string;
  cancelPending: string;
  retryPending: Set<string>;
  onCancel: (batchId: string) => void;
  onRetry: (jobId: string) => void;
}) {
  return (
    <Card className="min-w-0 rounded-lg">
      <CardHeader className="border-b">
        <CardTitle>{title}</CardTitle>
        <CardDescription>{description}</CardDescription>
      </CardHeader>
      <CardContent className="grid gap-3">
        {pending && !batches.length ? (
          <div className="flex h-40 items-center justify-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="animate-spin" />
            正在读取批次
          </div>
        ) : batches.length ? (
          batches.map((batch) => (
            <BatchCard
              key={batch.id}
              batch={batch}
              customer={customer}
              cancelPending={cancelPending}
              retryPending={retryPending}
              onCancel={onCancel}
              onRetry={onRetry}
            />
          ))
        ) : (
          <div className="flex h-40 items-center justify-center rounded-lg border border-dashed text-sm text-muted-foreground">{empty}</div>
        )}
      </CardContent>
    </Card>
  );
}

function BatchCard({
  batch,
  customer,
  cancelPending,
  retryPending,
  onCancel,
  onRetry,
}: {
  batch: BatchRecord;
  customer: string;
  cancelPending: string;
  retryPending: Set<string>;
  onCancel: (batchId: string) => void;
  onRetry: (jobId: string) => void;
}) {
  const active = batch.status === "queued" || batch.status === "running";
  const [expanded, setExpanded] = useState(active);
  const progress = batch.total ? Math.round((batch.completed / batch.total) * 100) : 0;
  return (
    <div className="grid gap-3 rounded-lg border p-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-medium">{operationLabel(batch.operation)}</span>
            <Badge variant={batch.status === "completed_with_errors" ? "destructive" : active ? "secondary" : "outline"}>
              {batchStatusLabel(batch.status)}
            </Badge>
            <Badge variant="outline">{batch.completed}/{batch.total}</Badge>
          </div>
          <div className="mt-1 text-xs text-muted-foreground">更新于 {formatTime(batch.updated_at)}</div>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button
            size="sm"
            variant="outline"
            nativeButton={false}
            render={
              <Link
                href={`/projects/${encodeURIComponent(customer)}/batches/${encodeURIComponent(batch.id)}`}
              />
            }
          >
            <ExternalLink />
            批次详情
          </Button>
          {active && (
            <Button size="sm" variant="destructive" disabled={Boolean(cancelPending)} onClick={() => onCancel(batch.id)}>
              {cancelPending === batch.id ? <Loader2 className="animate-spin" /> : <X />}
              取消批次
            </Button>
          )}
        </div>
      </div>
      <Progress value={progress} className="h-2" />
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
        {Object.entries(batch.status_counts).map(([status, count]) => (
          <span key={status}>{jobStatusLabel(status as BatchJobRecord["status"])} {count}</span>
        ))}
      </div>
      <details
        open={expanded}
        onToggle={(event) => setExpanded(event.currentTarget.open)}
      >
        <summary className="cursor-pointer text-sm font-medium text-primary hover:underline">查看 {batch.jobs.length} 篇任务</summary>
        <div className="mt-2 grid max-h-[360px] gap-2 overflow-y-auto pr-1">
          {batch.jobs.map((job) => {
            const canRetry = ["failed", "cancelled", "conflict"].includes(job.status);
            return (
              <div key={job.id} className="grid gap-2 rounded-md border bg-muted/20 p-2 text-sm">
                <div className="flex min-w-0 items-start justify-between gap-2">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-medium">topic_{String(job.topic_index).padStart(3, "0")}</span>
                      <Badge variant={job.status === "failed" || job.status === "conflict" ? "destructive" : job.status === "succeeded" ? "default" : "outline"}>
                        {jobStatusLabel(job.status)}
                      </Badge>
                      {job.attempts > 0 && <span className="text-xs text-muted-foreground">尝试 {job.attempts}/{job.max_attempts}</span>}
                    </div>
                    <div className="mt-1 line-clamp-2 text-xs text-muted-foreground">{job.topic}</div>
                  </div>
                  <div className="flex shrink-0 flex-wrap gap-1">
                    <Button
                      size="xs"
                      variant="ghost"
                      nativeButton={false}
                      render={
                        <Link
                          href={`/projects/${encodeURIComponent(customer)}/articles/${encodeURIComponent(job.task_id)}?step=${operationStep(batch.operation)}`}
                        />
                      }
                    >
                      <ExternalLink />
                      打开文章
                    </Button>
                    {canRetry && (
                      <Button size="xs" variant="outline" disabled={retryPending.has(job.id)} onClick={() => onRetry(job.id)}>
                        {retryPending.has(job.id) ? <Loader2 className="animate-spin" /> : <RefreshCw />}
                        重试
                      </Button>
                    )}
                  </div>
                </div>
                {job.error && <div className="rounded bg-destructive/5 px-2 py-1.5 text-xs text-destructive">{job.error}</div>}
              </div>
            );
          })}
        </div>
      </details>
    </div>
  );
}
