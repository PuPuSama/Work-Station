"use client";

/* eslint-disable react-hooks/set-state-in-effect */

import { ArrowRight, Loader2, RefreshCw, Search, Sparkles } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";

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
import { apiGet, apiPost } from "@/lib/api";
import { cn } from "@/lib/utils";
import type { ApiMessage, DashboardSummary, PublicConfig, TaskRecord } from "@/types";

type ProjectSummary = {
  customer: string;
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
  const [busy, setBusy] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

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
          taskCount: 1,
          completedCount: task.status === "docx_exported" ? 1 : 0,
          updatedAt: task.updated_at,
        });
        continue;
      }
      existing.taskCount += 1;
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
    return projects.filter((project) =>
      project.customer.toLowerCase().includes(normalized),
    );
  }, [projects, query]);

  async function initializeWeek() {
    setBusy("init");
    setError("");
    setMessage("");
    try {
      const result = await apiPost<ApiMessage>("/api/init-week");
      await loadData();
      setMessage(result.message);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy("");
    }
  }

  const isBusy = Boolean(busy);
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
              <span>{dashboard?.week_folder ?? config?.current_week_folder ?? "未初始化"}</span>
              <span>{dashboard?.week_path ?? config?.current_week_path}</span>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Button
              variant="outline"
              onClick={() =>
                loadData()
                  .then(() => setMessage("数据已刷新"))
                  .catch((err) => setError(errorMessage(err)))
              }
              disabled={isBusy}
            >
              <RefreshCw />
              刷新
            </Button>
            <Button onClick={initializeWeek} disabled={isBusy}>
              {isBusy ? <Loader2 className="animate-spin" /> : <Sparkles />}
              初始化本周任务
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
                      <Link
                        key={project.customer}
                        href={`/projects/${encodeURIComponent(project.customer)}`}
                        className="block rounded-lg border bg-card p-4 text-card-foreground transition-colors hover:bg-accent/40"
                      >
                        <div className="flex items-start justify-between gap-3">
                          <div className="min-w-0">
                            <div className="truncate font-medium">
                              {project.customer}
                            </div>
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
