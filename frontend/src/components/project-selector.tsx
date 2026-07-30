"use client";

/* eslint-disable react-hooks/set-state-in-effect */

import { ArrowRight, Loader2, RefreshCw, Search, Settings2, Sparkles, Upload } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

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
import type { ApiMessage, DashboardSummary, PublicConfig, TaskRecord } from "@/types";

type ProjectSummary = {
  customer: string;
  brandName: string;
  projectIntroduction: string;
  projectNotes: string;
  taskCount: number;
  completedCount: number;
  updatedAt: string;
};

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
      <div className="border-b bg-[color-mix(in_oklch,var(--background),var(--accent)_22%)]">
        <div className="mx-auto flex max-w-[1320px] flex-col gap-4 px-5 py-5 lg:flex-row lg:items-center lg:justify-between">
          <div className="min-w-0">
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
          </div>
        </div>
      </div>

      <div className="mx-auto grid max-w-[1320px] gap-4 px-5 py-5">
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

        <section className="grid gap-3 md:grid-cols-4">
          <SummaryCard title="项目" value={projects.length} />
          <SummaryCard title="任务" value={dashboard?.task_count ?? 0} />
          <SummaryCard title="已导出" value={dashboard?.completed_count ?? 0} />
          <Card className="rounded-lg">
            <CardHeader>
              <CardTitle>完成率</CardTitle>
              <CardDescription>{completion}%</CardDescription>
            </CardHeader>
            <CardContent>
              <Progress value={completion} />
            </CardContent>
          </Card>
        </section>

        <section className="grid gap-3">
          <Card className="rounded-lg">
            <CardHeader className="border-b">
              <CardTitle>项目列表</CardTitle>
              <CardDescription>
                {filteredProjects.length} / {projects.length}
              </CardDescription>
              <CardAction>
                <div className="relative">
                  <Search className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
                  <Input
                    className="h-8 w-[260px] pl-8"
                    placeholder="搜索项目"
                    value={query}
                    onChange={(event) => setQuery(event.target.value)}
                  />
                </div>
              </CardAction>
            </CardHeader>
            <CardContent>
              {filteredProjects.length ? (
                <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
                  {filteredProjects.map((project) => {
                    const percent = project.taskCount
                      ? Math.round((project.completedCount / project.taskCount) * 100)
                      : 0;
                    return (
                      <div
                        key={project.customer}
                        className="overflow-hidden rounded-lg border bg-card text-card-foreground"
                      >
                        <Link
                          href={`/projects/${encodeURIComponent(project.customer)}/articles`}
                          className="block p-4 transition-colors hover:bg-accent/40"
                        >
                          <div className="flex items-start justify-between gap-3">
                            <div className="min-w-0">
                              <div className="truncate font-medium">
                                {project.brandName || project.customer}
                              </div>
                              {project.brandName && (
                                <div className="mt-0.5 truncate text-xs text-muted-foreground">
                                  {project.customer}
                                </div>
                              )}
                              <div className="mt-1 text-sm text-muted-foreground">
                                {project.taskCount} tasks / {project.completedCount} exported
                              </div>
                            </div>
                            <ArrowRight className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
                          </div>
                          <div className="mt-4 grid gap-2">
                            <div className="flex items-center justify-between text-xs text-muted-foreground">
                              <span>完成率</span>
                              <span>{percent}%</span>
                            </div>
                            <Progress value={percent} />
                          </div>
                        </Link>
                        <div className="flex flex-wrap items-center justify-between gap-3 border-t bg-muted/20 p-3">
                          <div className="flex flex-wrap gap-2">
                            <Badge variant={project.brandName ? "secondary" : "outline"}>
                              {project.brandName ? "品牌名已设置" : "品牌名待设置"}
                            </Badge>
                            <Badge
                              variant={
                                project.projectIntroduction && project.projectNotes
                                  ? "secondary"
                                  : "outline"
                              }
                            >
                              {project.projectIntroduction && project.projectNotes
                                ? "项目资料已完善"
                                : "项目资料待完善"}
                            </Badge>
                          </div>
                          <Button
                            size="sm"
                            variant="outline"
                            nativeButton={false}
                            render={
                              <Link
                                href={`/projects/${encodeURIComponent(project.customer)}/settings`}
                              />
                            }
                          >
                            <Settings2 />
                            项目设置
                          </Button>
                        </div>
                      </div>
                    );
                  })}
                </div>
              ) : (
                <div className="flex h-[260px] items-center justify-center rounded-lg border border-dashed text-sm text-muted-foreground">
                  暂无项目
                </div>
              )}
            </CardContent>
          </Card>
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
