"use client";

/* eslint-disable react-hooks/set-state-in-effect */

import { AlertCircle, ArrowRight, RefreshCw, Search } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";

import { ProjectNavigation } from "@/components/project-navigation";
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
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { apiGet } from "@/lib/api";
import { cn } from "@/lib/utils";
import type { TaskRecord, WorkflowStatus } from "@/types";

type ProjectArticleListProps = {
  customer: string;
};

type QuickView = "all" | "manual" | "attention" | "deliverable" | "completed";

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

const STATUS_ORDER = Object.keys(STATUS_LABELS) as WorkflowStatus[];
const MANUAL_STATUSES = new Set<WorkflowStatus>([
  "titles_ready",
  "outline_ready",
  "draft_ready",
  "humanized_ready",
  "final_ai_checked",
  "links_verified",
  "images_ready",
]);

const QUICK_VIEWS: Array<{ value: QuickView; label: string }> = [
  { value: "all", label: "全部" },
  { value: "manual", label: "待我处理" },
  { value: "attention", label: "生成中 / 失败" },
  { value: "deliverable", label: "可交付" },
  { value: "completed", label: "已完成" },
];

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "未知错误";
}

function statusVariant(status: WorkflowStatus) {
  if (status === "docx_exported" || status === "images_ready") return "default";
  if (MANUAL_STATUSES.has(status)) return "secondary";
  if (status === "titles_ready" || status === "title_selected") return "outline";
  return "ghost";
}

function matchesQuickView(task: TaskRecord, view: QuickView) {
  if (view === "all") return true;
  if (view === "manual") return MANUAL_STATUSES.has(task.status);
  if (view === "attention") return Boolean(task.workflow_error);
  if (view === "deliverable") return task.status === "images_ready";
  return task.status === "docx_exported";
}

function formatUpdatedAt(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value || "-";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function stepForStatus(status: WorkflowStatus) {
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

export function ProjectArticleList({ customer }: ProjectArticleListProps) {
  const [tasks, setTasks] = useState<TaskRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [quickView, setQuickView] = useState<QuickView>("all");
  const [status, setStatus] = useState<"all" | WorkflowStatus>("all");

  const loadTasks = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setTasks(
        await apiGet<TaskRecord[]>(`/api/tasks?customer=${encodeURIComponent(customer)}`),
      );
    } catch (loadError) {
      setError(errorMessage(loadError));
    } finally {
      setLoading(false);
    }
  }, [customer]);

  useEffect(() => {
    void loadTasks();
  }, [loadTasks]);

  const counts = useMemo(
    () => ({
      all: tasks.length,
      manual: tasks.filter((task) => matchesQuickView(task, "manual")).length,
      attention: tasks.filter((task) => matchesQuickView(task, "attention")).length,
      deliverable: tasks.filter((task) => matchesQuickView(task, "deliverable")).length,
      completed: tasks.filter((task) => matchesQuickView(task, "completed")).length,
    }),
    [tasks],
  );

  const filteredTasks = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase();
    return tasks.filter((task) => {
      if (!matchesQuickView(task, quickView)) return false;
      if (status !== "all" && task.status !== status) return false;
      if (!normalized) return true;
      const products = task.products.map((product) => product.name).join(" ");
      return [task.id, `topic_${String(task.topic_index).padStart(3, "0")}`, task.topic, task.selected_title, products]
        .join(" ")
        .toLocaleLowerCase()
        .includes(normalized);
    });
  }, [query, quickView, status, tasks]);

  const projectPath = `/projects/${encodeURIComponent(customer)}`;

  return (
    <main className="min-h-screen bg-background text-foreground">
      <div className="border-b bg-[color-mix(in_oklch,var(--background),var(--accent)_22%)]">
        <div className="mx-auto grid max-w-[1480px] gap-4 px-5 py-5">
          <ProjectNavigation customer={customer} />
          <div className="flex flex-col gap-1 px-1 sm:flex-row sm:items-end sm:justify-between">
            <div>
              <h1 className="text-xl font-semibold">文章任务</h1>
              <p className="mt-1 text-sm text-muted-foreground">
                先定位需要处理的文章，再进入单篇工作台完成具体操作。
              </p>
            </div>
            <span className="text-sm text-muted-foreground">
              显示 {filteredTasks.length} / {tasks.length} 篇
            </span>
          </div>
        </div>
      </div>

      <div className="mx-auto grid max-w-[1480px] gap-4 px-5 py-5">
        {error && (
          <Alert variant="destructive">
            <AlertCircle />
            <AlertTitle>文章任务加载失败</AlertTitle>
            <AlertDescription>{error}</AlertDescription>
            <div className="col-start-2 mt-2">
              <Button variant="outline" size="sm" onClick={() => void loadTasks()}>
                <RefreshCw />
                重试
              </Button>
            </div>
          </Alert>
        )}

        <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-5">
          {QUICK_VIEWS.map((view) => (
            <button
              key={view.value}
              type="button"
              onClick={() => setQuickView(view.value)}
              className={cn(
                "rounded-lg border bg-card px-3 py-2.5 text-left transition-colors hover:bg-accent/40",
                quickView === view.value && "border-primary bg-accent/50 ring-1 ring-primary/20",
              )}
            >
              <span className="block text-xs text-muted-foreground">{view.label}</span>
              <span className="mt-0.5 block text-xl font-semibold">{counts[view.value]}</span>
            </button>
          ))}
        </div>

        <Card className="rounded-lg">
          <CardHeader className="border-b">
            <CardTitle>任务列表</CardTitle>
            <CardDescription>
              “生成中 / 失败”当前依据任务错误记录筛选；后端未提供运行中字段时不会误报。
            </CardDescription>
          </CardHeader>
          <CardContent className="grid gap-3">
            <div className="flex flex-col gap-2 md:flex-row md:items-center">
              <div className="relative min-w-0 flex-1">
                <Search className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
                <Input
                  className="pl-8"
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder="搜索编号、话题、标题、产品"
                  aria-label="搜索文章任务"
                />
              </div>
              <select
                value={status}
                onChange={(event) => setStatus(event.target.value as "all" | WorkflowStatus)}
                aria-label="按具体状态筛选"
                className="h-8 rounded-lg border border-input bg-background px-2.5 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
              >
                <option value="all">全部具体状态</option>
                {STATUS_ORDER.map((value) => (
                  <option key={value} value={value}>
                    {STATUS_LABELS[value]}
                  </option>
                ))}
              </select>
              <Button variant="outline" onClick={() => void loadTasks()} disabled={loading}>
                <RefreshCw className={cn(loading && "animate-spin")} />
                刷新
              </Button>
            </div>

            <div className="overflow-hidden rounded-lg border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="w-28">编号</TableHead>
                    <TableHead>话题 / 标题</TableHead>
                    <TableHead className="w-40">状态</TableHead>
                    <TableHead className="w-36">更新时间</TableHead>
                    <TableHead className="w-16 text-right">打开</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {filteredTasks.map((task) => {
                    const href = `${projectPath}/articles/${encodeURIComponent(task.id)}?step=${stepForStatus(task.status)}`;
                    return (
                      <TableRow key={task.id}>
                        <TableCell className="font-mono text-xs">
                          topic_{String(task.topic_index).padStart(3, "0")}
                        </TableCell>
                        <TableCell className="max-w-0 whitespace-normal">
                          <Link href={href} className="block min-w-0 hover:text-primary">
                            <span className="block truncate font-medium">{task.topic}</span>
                            <span className="mt-0.5 block truncate text-xs text-muted-foreground">
                              {task.selected_title || "尚未选择文章标题"}
                            </span>
                            {task.workflow_error && (
                              <span className="mt-1 block truncate text-xs text-destructive">
                                {task.workflow_error.message}
                              </span>
                            )}
                          </Link>
                        </TableCell>
                        <TableCell>
                          <Badge
                            variant={task.workflow_error ? "destructive" : statusVariant(task.status)}
                          >
                            {task.workflow_error ? "处理失败" : STATUS_LABELS[task.status]}
                          </Badge>
                        </TableCell>
                        <TableCell className="text-xs text-muted-foreground">
                          {formatUpdatedAt(task.updated_at)}
                        </TableCell>
                        <TableCell className="text-right">
                          <Link
                            href={href}
                            aria-label={`打开 topic_${String(task.topic_index).padStart(3, "0")}`}
                            className="inline-flex size-7 items-center justify-center rounded-md hover:bg-muted"
                          >
                            <ArrowRight className="size-4" />
                          </Link>
                        </TableCell>
                      </TableRow>
                    );
                  })}
                  {!loading && !filteredTasks.length && (
                    <TableRow>
                      <TableCell colSpan={5} className="h-36 text-center text-muted-foreground">
                        没有符合当前筛选条件的文章
                      </TableCell>
                    </TableRow>
                  )}
                  {loading && (
                    <TableRow>
                      <TableCell colSpan={5} className="h-36 text-center text-muted-foreground">
                        正在加载文章任务…
                      </TableCell>
                    </TableRow>
                  )}
                </TableBody>
              </Table>
            </div>
          </CardContent>
        </Card>
      </div>
    </main>
  );
}
