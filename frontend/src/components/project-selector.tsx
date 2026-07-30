"use client";

/* eslint-disable react-hooks/set-state-in-effect */

import {
  AlertTriangle,
  ArrowRight,
  CheckCircle2,
  Clock3,
  FolderKanban,
  Loader2,
  PenLine,
  RefreshCw,
  Search,
  Settings2,
  Sparkles,
  Upload,
} from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { LogoutButton } from "@/components/logout-button";
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
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Progress } from "@/components/ui/progress";
import { apiGet, apiPost, apiPut, apiUpload } from "@/lib/api";
import { cn } from "@/lib/utils";
import type {
  ApiMessage,
  DashboardSummary,
  PublicConfig,
  TaskRecord,
  WorkflowStatus,
} from "@/types";

type ProjectSummary = {
  customer: string;
  brandName: string;
  projectIntroduction: string;
  projectNotes: string;
  taskCount: number;
  completedCount: number;
  updatedAt: string;
};

const WORKFLOW_STATUS_LABELS: Record<WorkflowStatus, string> = {
  new: "待生成标题",
  titles_ready: "待选择标题",
  title_selected: "待确认产品",
  outline_ready: "大纲待确认",
  outline_confirmed: "待生成正文",
  draft_ready: "待 ZeroGPT 初检",
  initial_ai_checked: "初检已完成",
  humanized_ready: "待 ZeroGPT 复检",
  final_ai_checked: "待恢复链接",
  links_verified: "待准备图片",
  images_ready: "可导出 Word",
  docx_exported: "已完成交付",
};

const ACTIONABLE_STATUSES = new Set<WorkflowStatus>([
  "titles_ready",
  "outline_ready",
  "draft_ready",
  "humanized_ready",
  "final_ai_checked",
  "links_verified",
  "images_ready",
]);

function taskStep(status: WorkflowStatus) {
  if (status === "new" || status === "titles_ready") return "titles";
  if (status === "title_selected") return "products";
  if (status === "outline_ready") return "outline";
  if (status === "outline_confirmed") return "article";
  if (["draft_ready", "initial_ai_checked", "humanized_ready", "final_ai_checked"].includes(status)) {
    return "review";
  }
  if (status === "links_verified" || status === "images_ready") return "media";
  return "files";
}

function shortUpdatedAt(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "Unknown error";
}

export function ProjectSelector() {
  const [dashboard, setDashboard] = useState<DashboardSummary | null>(null);
  const [config, setConfig] = useState<PublicConfig | null>(null);
  const [tasks, setTasks] = useState<TaskRecord[]>([]);
  const [query, setQuery] = useState("");
  const [pendingActions, setPendingActions] = useState<Record<string, boolean>>({});
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [llmSettingsOpen, setLlmSettingsOpen] = useState(false);
  const [selectedModel, setSelectedModel] = useState("");
  const [selectedReasoningEffort, setSelectedReasoningEffort] = useState("");
  const uploadInputRef = useRef<HTMLInputElement>(null);

  function setPending(key: string, pending: boolean) {
    setPendingActions((current) => {
      if (pending) return { ...current, [key]: true };
      const next = { ...current };
      delete next[key];
      return next;
    });
  }

  const isPending = (key: string) => Boolean(pendingActions[key]);

  const loadData = useCallback(async () => {
    const [nextDashboard, nextConfig, nextTasks] = await Promise.all([
      apiGet<DashboardSummary>("/api/dashboard"),
      apiGet<PublicConfig>("/api/config"),
      apiGet<TaskRecord[]>("/api/tasks"),
    ]);
    setDashboard(nextDashboard);
    setConfig(nextConfig);
    setTasks(nextTasks);
  }, []);

  useEffect(() => {
    loadData().catch((err) => setError(errorMessage(err)));
  }, [loadData]);

  const projects = useMemo(() => {
    const grouped = new Map<string, ProjectSummary>();
    for (const task of tasks) {
      const existing = grouped.get(task.customer);
      if (!existing) {
        grouped.set(task.customer, {
          customer: task.customer,
          brandName: task.brand_name ?? "",
          projectIntroduction: task.project_introduction ?? "",
          projectNotes: task.project_notes ?? "",
          taskCount: 1,
          completedCount: task.status === "docx_exported" ? 1 : 0,
          updatedAt: task.updated_at,
        });
        continue;
      }
      existing.taskCount += 1;
      if (task.brand_name) {
        existing.brandName = task.brand_name;
      }
      if (task.project_introduction) {
        existing.projectIntroduction = task.project_introduction;
      }
      if (task.project_notes) {
        existing.projectNotes = task.project_notes;
      }
      if (task.status === "docx_exported") {
        existing.completedCount += 1;
      }
      if (task.updated_at > existing.updatedAt) {
        existing.updatedAt = task.updated_at;
      }
    }
    return Array.from(grouped.values()).sort((a, b) =>
      a.customer.localeCompare(b.customer),
    );
  }, [tasks]);

  const filteredProjects = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    if (!normalized) {
      return projects;
    }
    return projects.filter(
      (project) =>
        project.customer.toLowerCase().includes(normalized) ||
        project.brandName.toLowerCase().includes(normalized),
    );
  }, [projects, query]);

  const actionTasks = useMemo(
    () =>
      tasks
        .filter((task) => task.workflow_error || ACTIONABLE_STATUSES.has(task.status))
        .sort((a, b) => b.updated_at.localeCompare(a.updated_at))
        .slice(0, 7),
    [tasks],
  );
  const failedTaskCount = useMemo(
    () => tasks.filter((task) => Boolean(task.workflow_error)).length,
    [tasks],
  );
  const actionableTaskCount = useMemo(
    () => tasks.filter((task) => ACTIONABLE_STATUSES.has(task.status)).length,
    [tasks],
  );

  async function syncTasks() {
    setPending("sync", true);
    setError("");
    setMessage("");
    try {
      const result = await apiPost<ApiMessage>("/api/sync-tasks");
      await loadData();
      setMessage(result.message);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setPending("sync", false);
    }
  }

  async function refreshProjects() {
    setPending("refresh", true);
    setError("");
    setMessage("");
    try {
      await loadData();
      setMessage("数据已刷新");
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setPending("refresh", false);
    }
  }

  function openLlmSettings() {
    setSelectedModel(config?.llm.model || "");
    setSelectedReasoningEffort(config?.llm.reasoning_effort || "");
    setLlmSettingsOpen(true);
  }

  async function saveLlmSettings() {
    if (!selectedModel || !selectedReasoningEffort) return;
    setPending("llm-settings", true);
    setError("");
    setMessage("");
    try {
      const nextConfig = await apiPut<PublicConfig>("/api/settings/llm", {
        model: selectedModel,
        reasoning_effort: selectedReasoningEffort,
      });
      setConfig(nextConfig);
      setLlmSettingsOpen(false);
      setMessage(
        `模型设置已保存：${nextConfig.llm.model} / ${nextConfig.llm.reasoning_effort}`,
      );
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setPending("llm-settings", false);
    }
  }

  async function uploadTopicFiles(event: React.ChangeEvent<HTMLInputElement>) {
    const files = Array.from(event.target.files ?? []);
    if (!files.length) return;
    setPending("upload", true);
    setError("");
    setMessage("");
    try {
      const formData = new FormData();
      for (const file of files) {
        formData.append("files", file);
      }
      const result = await apiUpload<ApiMessage>("/api/topic-files/upload", formData);
      await loadData();
      setMessage(result.message);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setPending("upload", false);
      event.target.value = "";
    }
  }

  const completion = dashboard?.task_count
    ? Math.round((dashboard.completed_count / dashboard.task_count) * 100)
    : 0;

  return (
    <main className="min-h-screen bg-background text-foreground">
      <div className="border-b bg-card">
        <div className="mx-auto flex max-w-[1440px] flex-col gap-5 px-5 py-6 lg:flex-row lg:items-end lg:justify-between lg:px-8">
          <div className="min-w-0">
            <div className="mb-3 flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.14em] text-primary">
              <span className="flex size-7 items-center justify-center rounded-lg bg-primary text-primary-foreground">
                <PenLine className="size-3.5" />
              </span>
              Article Agent · Content Operations
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="text-xl font-semibold tracking-normal">
                项目选择
              </h1>
              <Badge variant={dashboard?.llm_ready ? "default" : "outline"}>
                {dashboard?.llm_ready ? "LLM Ready" : "Mock LLM"}
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
              onClick={openLlmSettings}
              disabled={!config || isPending("llm-settings")}
            >
              <Settings2 />
              模型设置
              {config && (
                <span className="hidden text-xs text-muted-foreground sm:inline">
                  {config.llm.model} · {config.llm.reasoning_effort}
                </span>
              )}
            </Button>
            <input
              ref={uploadInputRef}
              type="file"
              accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
              multiple
              className="hidden"
              onChange={(event) => void uploadTopicFiles(event)}
            />
            <Button
              variant="outline"
              onClick={() => uploadInputRef.current?.click()}
              disabled={isPending("upload") || isPending("sync") || isPending("refresh")}
            >
              {isPending("upload") ? <Loader2 className="animate-spin" /> : <Upload />}
              上传 XLSX
            </Button>
            <Button
              variant="outline"
              onClick={() => void refreshProjects()}
              disabled={isPending("upload") || isPending("sync") || isPending("refresh")}
            >
              {isPending("refresh") ? (
                <Loader2 className="animate-spin" />
              ) : (
                <RefreshCw />
              )}
              刷新
            </Button>
            <Button
              onClick={syncTasks}
              disabled={isPending("upload") || isPending("sync") || isPending("refresh")}
            >
              {isPending("sync") ? <Loader2 className="animate-spin" /> : <Sparkles />}
              同步话题库
            </Button>
            <LogoutButton />
          </div>
        </div>
      </div>

      <div className="mx-auto grid max-w-[1440px] gap-5 px-5 py-6 lg:px-8">
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

        <section className="grid gap-3 md:grid-cols-2 lg:grid-cols-4">
          <SummaryCard title="项目" value={projects.length} />
          <SummaryCard title="任务" value={dashboard?.task_count ?? 0} />
          <SummaryCard title="已导出" value={dashboard?.completed_count ?? 0} />
          <Card className="rounded-xl">
            <CardHeader>
              <CardTitle>完成率</CardTitle>
              <CardDescription>{completion}%</CardDescription>
            </CardHeader>
            <CardContent>
              <Progress value={completion} />
            </CardContent>
          </Card>
        </section>

        <section className="grid items-start gap-4 lg:grid-cols-[minmax(0,1fr)_340px]">
          <Card className="min-w-0 gap-0 py-0">
            <CardHeader className="border-b px-4 py-4 max-sm:grid-cols-1! max-sm:gap-3">
              <CardTitle className="flex items-center gap-2">
                <FolderKanban className="size-4 text-primary" />
                项目组合
              </CardTitle>
              <CardDescription>
                {filteredProjects.length} / {projects.length} 个项目
              </CardDescription>
              <CardAction className="max-sm:col-start-1 max-sm:row-start-3 max-sm:w-full">
                <div className="relative">
                  <Search className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
                  <Input
                    className="h-8 w-[260px] pl-8 max-sm:w-full"
                    placeholder="搜索项目或品牌"
                    value={query}
                    onChange={(event) => setQuery(event.target.value)}
                  />
                </div>
              </CardAction>
            </CardHeader>
            <CardContent className="p-0">
              {filteredProjects.length ? (
                <div className="divide-y">
                  {filteredProjects.map((project) => {
                    const percent = project.taskCount
                      ? Math.round((project.completedCount / project.taskCount) * 100)
                      : 0;
                    const projectHref = `/projects/${encodeURIComponent(project.customer)}/articles`;
                    return (
                      <div
                        key={project.customer}
                        className="group grid gap-4 p-4 transition-colors hover:bg-accent/25 lg:grid-cols-[minmax(0,1fr)_190px_auto] lg:items-center"
                      >
                        <Link href={projectHref} className="min-w-0">
                          <div className="flex items-center gap-2">
                            <span className="truncate font-semibold">
                              {project.brandName || project.customer}
                            </span>
                            {!project.projectIntroduction || !project.projectNotes ? (
                              <Badge variant="outline">资料待完善</Badge>
                            ) : null}
                          </div>
                          <div className="mt-1 truncate text-xs text-muted-foreground">
                            {project.customer}
                          </div>
                          <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
                            <span>{project.taskCount} 篇任务</span>
                            <span>{project.completedCount} 篇已交付</span>
                            <span>更新于 {shortUpdatedAt(project.updatedAt)}</span>
                          </div>
                        </Link>

                        <div className="grid gap-1.5">
                          <div className="flex items-center justify-between text-xs text-muted-foreground">
                            <span>交付进度</span>
                            <span className="font-medium tabular-nums text-foreground">
                              {percent}%
                            </span>
                          </div>
                          <Progress value={percent} />
                        </div>

                        <div className="flex items-center gap-1.5 lg:justify-end">
                          <Button
                            size="sm"
                            variant="ghost"
                            nativeButton={false}
                            render={
                              <Link
                                href={`/projects/${encodeURIComponent(project.customer)}/settings`}
                                aria-label={`打开 ${project.customer} 项目设置`}
                              />
                            }
                          >
                            <Settings2 />
                            设置
                          </Button>
                          <Button
                            size="icon-sm"
                            variant="outline"
                            nativeButton={false}
                            render={
                              <Link
                                href={projectHref}
                                aria-label={`进入 ${project.customer}`}
                              />
                            }
                          >
                            <ArrowRight />
                          </Button>
                        </div>
                      </div>
                    );
                  })}
                </div>
              ) : (
                <div className="flex h-[260px] items-center justify-center text-sm text-muted-foreground">
                  暂无项目
                </div>
              )}
            </CardContent>
          </Card>

          <div className="grid gap-4 lg:sticky lg:top-4">
            <Card className="gap-0 py-0">
              <CardHeader className="border-b px-4 py-4">
                <CardTitle className="flex items-center gap-2">
                  <Clock3 className="size-4 text-primary" />
                  运营待办
                </CardTitle>
                <CardDescription>最近需要人工推进的文章</CardDescription>
              </CardHeader>
              <CardContent className="p-0">
                {actionTasks.length ? (
                  <div className="divide-y">
                    {actionTasks.map((task) => (
                      <Link
                        key={task.id}
                        href={`/projects/${encodeURIComponent(task.customer)}/articles/${encodeURIComponent(task.id)}?step=${taskStep(task.status)}`}
                        className="flex items-start gap-3 p-3 transition-colors hover:bg-accent/35"
                      >
                        <span
                          className={cn(
                            "mt-1 flex size-7 shrink-0 items-center justify-center rounded-lg",
                            task.workflow_error
                              ? "bg-destructive/10 text-destructive"
                              : "bg-accent text-accent-foreground",
                          )}
                        >
                          {task.workflow_error ? (
                            <AlertTriangle className="size-3.5" />
                          ) : (
                            <Clock3 className="size-3.5" />
                          )}
                        </span>
                        <span className="min-w-0 flex-1">
                          <span className="block truncate text-sm font-medium">
                            {task.selected_title || task.topic}
                          </span>
                          <span className="mt-0.5 flex items-center justify-between gap-2 text-xs text-muted-foreground">
                            <span className="truncate">
                              {task.customer} · topic_
                              {String(task.topic_index).padStart(3, "0")}
                            </span>
                            <span className="shrink-0">
                              {task.workflow_error
                                ? "处理失败"
                                : WORKFLOW_STATUS_LABELS[task.status]}
                            </span>
                          </span>
                        </span>
                      </Link>
                    ))}
                  </div>
                ) : (
                  <div className="p-6 text-center text-sm text-muted-foreground">
                    当前没有待处理文章
                  </div>
                )}
              </CardContent>
            </Card>

            <Card className="gap-0 py-0">
              <CardHeader className="border-b px-4 py-4">
                <CardTitle>运行概况</CardTitle>
              </CardHeader>
              <CardContent className="grid grid-cols-2 gap-px bg-border p-0">
                <div className="bg-card p-4">
                  <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
                    <Clock3 className="size-3.5" />
                    待人工推进
                  </div>
                  <div className="mt-1 text-2xl font-semibold tabular-nums">
                    {actionableTaskCount}
                  </div>
                </div>
                <div className="bg-card p-4">
                  <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
                    <AlertTriangle className="size-3.5" />
                    处理失败
                  </div>
                  <div className="mt-1 text-2xl font-semibold tabular-nums">
                    {failedTaskCount}
                  </div>
                </div>
                <div className="col-span-2 flex items-center gap-2 bg-card p-4 text-sm">
                  <CheckCircle2
                    className={cn(
                      "size-4",
                      dashboard?.llm_ready ? "text-emerald-600" : "text-amber-600",
                    )}
                  />
                  <span className="font-medium">
                    {dashboard?.llm_ready ? "模型服务已连接" : "当前使用 Mock LLM"}
                  </span>
                </div>
              </CardContent>
            </Card>
          </div>
        </section>
      </div>

      <Dialog open={llmSettingsOpen} onOpenChange={setLlmSettingsOpen}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>全局模型设置</DialogTitle>
            <DialogDescription>
              用于标题、产品分析、大纲、正文、复检和 TDK 等后续模型请求。
            </DialogDescription>
          </DialogHeader>

          <div className="grid gap-5 py-2">
            <div className="grid gap-2">
              <Label htmlFor="global-llm-model">模型</Label>
              <select
                id="global-llm-model"
                className="h-9 w-full rounded-md border bg-background px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
                value={selectedModel}
                onChange={(event) => setSelectedModel(event.target.value)}
              >
                {(config?.llm.available_models || []).map((model) => (
                  <option key={model} value={model}>
                    {model}
                  </option>
                ))}
              </select>
            </div>

            <div className="grid gap-2">
              <Label htmlFor="global-reasoning-effort">推理强度</Label>
              <select
                id="global-reasoning-effort"
                className="h-9 w-full rounded-md border bg-background px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
                value={selectedReasoningEffort}
                onChange={(event) =>
                  setSelectedReasoningEffort(event.target.value)
                }
              >
                {(config?.llm.available_reasoning_efforts || []).map((effort) => (
                  <option key={effort} value={effort}>
                    {effort}
                  </option>
                ))}
              </select>
              <p className="text-xs leading-5 text-muted-foreground">
                推理越高通常越适合复杂的大纲、正文和复检，但响应时间和用量也可能增加。
              </p>
            </div>

            <Alert>
              <AlertTitle>生效范围</AlertTitle>
              <AlertDescription>
                保存后，新的请求及队列中尚未开始的任务使用新设置；正在执行的请求不会被中断。
              </AlertDescription>
            </Alert>
          </div>

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => setLlmSettingsOpen(false)}
              disabled={isPending("llm-settings")}
            >
              取消
            </Button>
            <Button
              type="button"
              onClick={() => void saveLlmSettings()}
              disabled={
                !selectedModel ||
                !selectedReasoningEffort ||
                isPending("llm-settings")
              }
            >
              {isPending("llm-settings") ? (
                <Loader2 className="animate-spin" />
              ) : (
                <Settings2 />
              )}
              保存模型设置
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </main>
  );
}

function SummaryCard({ title, value }: { title: string; value: number }) {
  return (
    <Card className="rounded-xl">
      <CardHeader>
        <CardTitle className="text-xs font-semibold uppercase tracking-[0.12em] text-muted-foreground">
          {title}
        </CardTitle>
        <CardDescription className="text-3xl font-semibold tracking-tight text-foreground">
          {value}
        </CardDescription>
      </CardHeader>
    </Card>
  );
}
