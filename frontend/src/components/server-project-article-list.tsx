"use client";

/* eslint-disable react-hooks/set-state-in-effect */

import {
  AlertCircle,
  ArrowRight,
  FileText,
  Loader2,
  RefreshCw,
  Search,
  ShieldCheck,
} from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";

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
import { ServerTaskIntakePanel } from "@/components/server-task-intake-panel";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { apiGet, apiPut } from "@/lib/api";
import { sameProjectId } from "@/lib/project-id";
import type { AccessibleProject, TaskRecord, WorkflowStatus } from "@/types";

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

const MANUAL_STATUSES = new Set<WorkflowStatus>([
  "titles_ready",
  "title_selected",
  "outline_ready",
  "draft_ready",
  "humanized_ready",
  "final_ai_checked",
  "links_verified",
  "images_ready",
]);

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "文章任务加载失败。";
}

function canEditProject(
  role: AccessibleProject["effective_role"] | null,
  isProjectOwner: boolean,
) {
  return (
    role === "org_admin" ||
    role === "editor" ||
    (role === "team_lead" && isProjectOwner)
  );
}

function stepForStatus(status: WorkflowStatus) {
  if (status === "new" || status === "titles_ready") return "setup";
  if (status === "title_selected" || status === "outline_ready") return "outline";
  if (status === "outline_confirmed" || status === "draft_ready") return "draft";
  if (
    status === "initial_ai_checked" ||
    status === "humanized_ready" ||
    status === "final_ai_checked"
  ) {
    return "review";
  }
  return "delivery";
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

export function ServerProjectArticleList({ customer }: { customer: string }) {
  const [tasks, setTasks] = useState<TaskRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [manualOnly, setManualOnly] = useState(false);
  const [canEdit, setCanEdit] = useState(false);
  const [completionPending, setCompletionPending] = useState<string | null>(
    null,
  );

  const loadTasks = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [nextTasks, projects] = await Promise.all([
        apiGet<TaskRecord[]>(
          `/api/projects/${encodeURIComponent(customer)}/tasks`,
        ),
        apiGet<AccessibleProject[]>("/api/projects"),
      ]);
      const project = projects.find((item) =>
        sameProjectId(item.project_id, customer),
      );
      setTasks(nextTasks);
      setCanEdit(
        canEditProject(
          project?.effective_role ?? null,
          project?.is_project_owner === true,
        ),
      );
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setLoading(false);
    }
  }, [customer]);

  useEffect(() => {
    void loadTasks();
  }, [loadTasks]);

  const filteredTasks = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase();
    return tasks.filter((task) => {
      if (
        manualOnly &&
        (task.manual_completed || !MANUAL_STATUSES.has(task.status))
      ) {
        return false;
      }
      if (!normalized) return true;
      return [
        task.id,
        task.topic,
        task.selected_title,
        ...task.products.map((product) => product.name),
      ]
        .join(" ")
        .toLocaleLowerCase()
        .includes(normalized);
    });
  }, [manualOnly, query, tasks]);

  async function toggleCompletion(task: TaskRecord, completed: boolean) {
    setCompletionPending(task.id);
    setError("");
    try {
      const updated = await apiPut<TaskRecord>(
        `/api/projects/${encodeURIComponent(customer)}/tasks/${encodeURIComponent(task.id)}/manual-completion`,
        {
          revision: task.revision ?? 0,
          completed,
        },
      );
      setTasks((current) =>
        current.map((item) => (item.id === updated.id ? updated : item)),
      );
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setCompletionPending(null);
    }
  }

  const projectPath = `/projects/${encodeURIComponent(customer)}`;

  return (
    <main className="min-h-screen bg-background text-foreground">
      <header className="border-b bg-card">
        <div className="mx-auto flex max-w-[1480px] flex-col gap-4 px-5 py-5 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <div className="mb-2 flex items-center gap-2">
              <h1 className="text-xl font-semibold">文章任务</h1>
              <Badge variant="outline">
                <ShieldCheck />
                Server
              </Badge>
            </div>
            <p className="max-w-2xl text-sm leading-6 text-muted-foreground">
              任务读取和所有写操作均绑定当前 PostgreSQL Project；此页面不会调用
              已停用的旧文章接口。
            </p>
          </div>
          <div className="text-sm text-muted-foreground">
            显示 {filteredTasks.length} / {tasks.length} 篇
          </div>
        </div>
      </header>

      <div className="mx-auto grid max-w-[1480px] gap-4 px-5 py-5">
        {error && (
          <Alert variant="destructive">
            <AlertCircle />
            <AlertTitle>文章任务加载失败</AlertTitle>
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}

        <ServerTaskIntakePanel
          customer={customer}
          onCompleted={loadTasks}
        />

        <Card className="gap-0 py-0">
          <CardHeader className="border-b px-4 py-4">
            <CardTitle className="flex items-center gap-2">
              <FileText className="size-4 text-primary" />
              Project 工作队列
            </CardTitle>
            <CardDescription>
              “待我处理”按工作流状态过滤，并排除已手动标记完成的任务；完成标记仍由
              Server API 以 Revision CAS 保存。
            </CardDescription>
          </CardHeader>
          <CardContent className="grid gap-3 px-4 py-4">
            <div className="flex flex-col gap-2 sm:flex-row">
              <div className="relative min-w-0 flex-1">
                <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
                <Input
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  className="h-11 pl-9"
                  placeholder="搜索编号、话题、标题或产品"
                  aria-label="搜索 Server 文章任务"
                />
              </div>
              <Button
                type="button"
                variant={manualOnly ? "default" : "outline"}
                className="min-h-11"
                aria-pressed={manualOnly}
                onClick={() => setManualOnly((current) => !current)}
              >
                待我处理
              </Button>
              <Button
                type="button"
                variant="outline"
                className="min-h-11"
                disabled={loading}
                onClick={() => void loadTasks()}
              >
                {loading ? (
                  <Loader2 className="animate-spin" />
                ) : (
                  <RefreshCw />
                )}
                刷新
              </Button>
            </div>

            <div className="overflow-x-auto rounded-lg border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="w-28">编号</TableHead>
                    <TableHead>话题 / 标题</TableHead>
                    <TableHead className="w-40">状态</TableHead>
                    <TableHead className="w-36">更新时间</TableHead>
                    <TableHead className="w-28">完成</TableHead>
                    <TableHead className="w-20 text-right">操作</TableHead>
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
                          <Link
                            href={href}
                            className="block min-w-0 rounded-sm outline-none transition-colors hover:text-primary focus-visible:ring-2 focus-visible:ring-ring"
                          >
                            <span className="block truncate font-medium">
                              {task.selected_title || task.topic}
                            </span>
                            <span className="mt-1 block truncate text-xs text-muted-foreground">
                              {task.topic}
                            </span>
                          </Link>
                          {task.workflow_error && (
                            <span className="mt-1 block text-xs text-destructive">
                              {task.workflow_error.message}
                            </span>
                          )}
                        </TableCell>
                        <TableCell>
                          <Badge
                            variant={
                              task.workflow_error
                                ? "destructive"
                                : MANUAL_STATUSES.has(task.status)
                                  ? "secondary"
                                  : "outline"
                            }
                          >
                            {task.manual_completed
                              ? "已完成"
                              : task.workflow_error
                                ? "处理失败"
                              : task.status === "title_selected" &&
                                  task.products.length
                                ? "产品已保存 · 待生成大纲"
                                : STATUS_LABELS[task.status]}
                          </Badge>
                        </TableCell>
                        <TableCell className="text-xs text-muted-foreground">
                          {formatUpdatedAt(task.updated_at)}
                        </TableCell>
                        <TableCell>
                          <label className="inline-flex items-center gap-2 text-xs">
                            <input
                              type="checkbox"
                              className="size-4 accent-primary"
                              checked={task.manual_completed === true}
                              disabled={
                                !canEdit || completionPending === task.id
                              }
                              onChange={(event) =>
                                void toggleCompletion(
                                  task,
                                  event.target.checked,
                                )
                              }
                              aria-label={`标记 topic_${String(task.topic_index).padStart(3, "0")} 已完成`}
                            />
                            <span>
                              {task.manual_completed ? "已完成" : "标记"}
                            </span>
                          </label>
                        </TableCell>
                        <TableCell className="text-right">
                          <Button
                            nativeButton={false}
                            size="sm"
                            variant="ghost"
                            render={
                              <Link
                                href={href}
                                aria-label={`打开 topic_${String(task.topic_index).padStart(3, "0")}`}
                              />
                            }
                          >
                            打开
                            <ArrowRight />
                          </Button>
                        </TableCell>
                      </TableRow>
                    );
                  })}
                  {loading && !tasks.length && (
                    <TableRow>
                      <TableCell
                        colSpan={6}
                        className="h-40 text-center text-muted-foreground"
                      >
                        <span
                          className="inline-flex items-center gap-2"
                          role="status"
                          aria-live="polite"
                        >
                          <Loader2 className="size-4 animate-spin" />
                          正在读取 Project 文章任务…
                        </span>
                      </TableCell>
                    </TableRow>
                  )}
                  {!loading && !filteredTasks.length && (
                    <TableRow>
                      <TableCell
                        colSpan={6}
                        className="h-40 text-center text-muted-foreground"
                      >
                        当前筛选下没有文章任务
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
