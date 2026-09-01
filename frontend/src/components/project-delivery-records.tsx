"use client";

/* eslint-disable react-hooks/set-state-in-effect */

import {
  AlertCircle,
  Download,
  FileText,
  ImageIcon,
  Loader2,
  Package,
  RefreshCw,
  Search,
  ShieldCheck,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { apiGet, apiPost } from "@/lib/api";
import { triggerBrowserDownload } from "@/lib/browser-download";
import { sameProjectId } from "@/lib/project-id";
import { formatProjectDate } from "@/lib/project-date";
import { cn } from "@/lib/utils";
import type { AccessibleProject, ProjectAssetDownload, TaskRecord } from "@/types";

type DeliveryFilter = "all" | "incomplete" | "ready" | "packaged";

const DELIVERY_LABELS: Record<string, string> = {
  article: "正文",
  links: "链接",
  screenshot: "AI 截图",
  images: "图片",
  word: "Word",
  tdk: "D 文档",
};

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "未知错误";
}

function deliveryParts(task: TaskRecord) {
  const images = task.images ?? [];
  return {
    article: Boolean(task.final_article || task.linked_article || task.humanized_article),
    links: Boolean(task.link_validation?.passed),
    screenshot: Boolean(task.final_ai_check?.screenshot_path || task.final_ai_check?.screenshot_asset_id),
    images: Boolean(images.length && images.every((image) => image.prepared_path || image.prepared_asset_id)),
    word: Boolean(task.docx_path || task.docx_asset_id),
    tdk: Boolean(task.tdk_path || task.tdk_asset_id),
    package: Boolean(task.delivery_package_path || task.delivery_package_asset_id),
  };
}

function canDeliver(
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

function formatUpdatedAt(value: string) {
  return formatProjectDate(value, {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }) || "-";
}

export function ProjectDeliveryRecords({ customer }: { customer: string }) {
  const [tasks, setTasks] = useState<TaskRecord[]>([]);
  const [effectiveRole, setEffectiveRole] = useState<AccessibleProject["effective_role"] | null>(null);
  const [isProjectOwner, setIsProjectOwner] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<DeliveryFilter>("all");
  const [pending, setPending] = useState<Record<string, string>>({});

  const projectApi = `/api/projects/${encodeURIComponent(customer)}`;

  const loadTasks = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [nextTasks, projects] = await Promise.all([
        apiGet<TaskRecord[]>(`${projectApi}/tasks`),
        apiGet<AccessibleProject[]>("/api/projects"),
      ]);
      const project = projects.find((item) => sameProjectId(item.project_id, customer));
      setEffectiveRole(project?.effective_role ?? null);
      setIsProjectOwner(project?.is_project_owner === true);
      setTasks(nextTasks);
    } catch (loadError) {
      setError(errorMessage(loadError));
    } finally {
      setLoading(false);
    }
  }, [customer, projectApi]);

  useEffect(() => {
    void loadTasks();
  }, [loadTasks]);

  const counts = useMemo(() => {
    const result = { all: tasks.length, incomplete: 0, ready: 0, packaged: 0 };
    for (const task of tasks) {
      const parts = deliveryParts(task);
      if (parts.package) result.packaged += 1;
      else if (parts.article && parts.links && parts.screenshot && parts.images && parts.word && parts.tdk) result.ready += 1;
      else result.incomplete += 1;
    }
    return result;
  }, [tasks]);

  const filteredTasks = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    return tasks.filter((task) => {
      const parts = deliveryParts(task);
      const ready = parts.article && parts.links && parts.screenshot && parts.images && parts.word && parts.tdk;
      if (filter === "packaged" && !parts.package) return false;
      if (filter === "ready" && (!ready || parts.package)) return false;
      if (filter === "incomplete" && (ready || parts.package)) return false;
      return !normalized || [task.id, task.topic, task.selected_title].join(" ").toLowerCase().includes(normalized);
    });
  }, [filter, query, tasks]);

  async function runTaskAction(task: TaskRecord, label: string, action: () => Promise<unknown>) {
    setPending((current) => ({ ...current, [task.id]: label }));
    setError("");
    setMessage("");
    try {
      await action();
      setMessage(`${task.id}：${label}成功`);
      await loadTasks();
    } catch (actionError) {
      setError(errorMessage(actionError));
    } finally {
      setPending((current) => {
        const next = { ...current };
        delete next[task.id];
        return next;
      });
    }
  }

  async function downloadArtifact(task: TaskRecord, label: string, path: string) {
    setPending((current) => ({ ...current, [task.id]: label }));
    setError("");
    setMessage("");
    try {
      const download = await apiGet<ProjectAssetDownload>(path, 30_000);
      if (!download.url) throw new Error("服务器没有返回可用的短期下载地址。");
      const fallback = path.endsWith("/tdk/download")
        ? task.tdk_filename || "D.docx"
        : path.endsWith("/delivery-package/download")
          ? task.delivery_package_filename || "delivery.zip"
          : task.docx_filename || "article.docx";
      triggerBrowserDownload(download.url, download.filename || fallback);
      setMessage(`${task.id}：${label}已开始`);
    } catch (downloadError) {
      setError(errorMessage(downloadError));
    } finally {
      setPending((current) => {
        const next = { ...current };
        delete next[task.id];
        return next;
      });
    }
  }

  const deliveryAllowed = canDeliver(effectiveRole, isProjectOwner);
  const reviewAllowed = canReview(effectiveRole, isProjectOwner);
  const filters: Array<{ value: DeliveryFilter; label: string }> = [
    { value: "all", label: "全部" },
    { value: "incomplete", label: "缺少交付项" },
    { value: "ready", label: "可打包" },
    { value: "packaged", label: "已打包" },
  ];

  return (
    <main className="min-h-screen bg-background text-foreground">
      <div className="border-b bg-card">
        <div className="mx-auto grid max-w-[1480px] gap-4 px-5 py-5">
          <div className="px-1">
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="text-xl font-semibold">交付记录</h1>
              <Badge variant="outline"><ShieldCheck />Server 私有交付</Badge>
            </div>
            <p className="mt-1 text-sm text-muted-foreground">
              集中检查 Word、D 文档、最终 AI 截图、图片和交付包；下载时由服务器重新授权并签发短期地址。
            </p>
          </div>
        </div>
      </div>

      <div className="mx-auto grid max-w-[1480px] gap-4 px-5 py-5">
        {error && <Alert variant="destructive"><AlertCircle /><AlertTitle>操作失败</AlertTitle><AlertDescription>{error}</AlertDescription></Alert>}
        {message && <Alert><AlertTitle>操作完成</AlertTitle><AlertDescription>{message}</AlertDescription></Alert>}
        {!deliveryAllowed && !loading && (
          <Alert>
            <ShieldCheck />
            <AlertTitle>当前角色为只读交付视图</AlertTitle>
            <AlertDescription>
              你可以查看交付状态{reviewAllowed ? "和终审截图" : ""}，但不能生成或下载 Word、TDK 与交付 ZIP。
            </AlertDescription>
          </Alert>
        )}

        <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
          {filters.map((item) => (
            <button key={item.value} type="button" onClick={() => setFilter(item.value)} className={cn("min-h-11 rounded-lg border bg-card px-3 py-2.5 text-left transition-colors hover:bg-accent/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring", filter === item.value && "border-primary bg-accent/50 ring-1 ring-primary/20")} aria-pressed={filter === item.value}>
              <span className="block text-xs text-muted-foreground">{item.label}</span>
              <span className="mt-0.5 block text-xl font-semibold">{counts[item.value]}</span>
            </button>
          ))}
        </div>

        <Card className="rounded-lg">
          <CardHeader className="border-b">
            <CardTitle>文章交付状态</CardTitle>
            <CardDescription>仅显示服务器中的项目任务；缺失项需要在对应工作流中补齐。</CardDescription>
          </CardHeader>
          <CardContent className="grid gap-3">
            <div className="flex flex-col gap-2 sm:flex-row">
              <div className="relative flex-1"><Search className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" /><Input value={query} onChange={(event) => setQuery(event.target.value)} className="pl-8" placeholder="搜索编号、话题或标题" aria-label="搜索交付记录" /></div>
              <Button variant="outline" onClick={() => void loadTasks()} disabled={loading}>{loading ? <Loader2 className="animate-spin" /> : <RefreshCw />}刷新</Button>
            </div>
            <div className="overflow-x-auto rounded-lg border">
              <Table>
                <TableHeader><TableRow><TableHead className="w-28">编号</TableHead><TableHead>文章</TableHead><TableHead>交付项</TableHead><TableHead className="w-32">更新时间</TableHead><TableHead className="min-w-72 text-right">操作</TableHead></TableRow></TableHeader>
                <TableBody>
                  {filteredTasks.map((task) => {
                    const parts = deliveryParts(task);
                    const missing = Object.entries(parts).filter(([key, done]) => key !== "package" && !done).map(([key]) => DELIVERY_LABELS[key]);
                    const taskPending = pending[task.id];
                    const taskApi = `${projectApi}/tasks/${encodeURIComponent(task.id)}`;
                    const topicNumber = String(task.topic_index).padStart(3, "0");
                    return (
                      <TableRow key={task.id}>
                        <TableCell className="font-mono text-xs">topic_{topicNumber}</TableCell>
                        <TableCell className="max-w-0 whitespace-normal"><div className="truncate font-medium">{task.selected_title || task.topic}</div><div className="mt-1 truncate text-xs text-muted-foreground">{task.topic}</div></TableCell>
                        <TableCell><div className="flex flex-wrap gap-1">{parts.package ? <Badge>已打包</Badge> : missing.length ? missing.map((item) => <Badge key={item} variant="outline">缺 {item}</Badge>) : <Badge>可打包</Badge>}</div></TableCell>
                        <TableCell className="text-xs text-muted-foreground">{formatUpdatedAt(task.updated_at)}</TableCell>
                        <TableCell>
                          <div className="flex flex-wrap justify-end gap-1">
                            {parts.word && deliveryAllowed && <Button size="sm" variant="outline" disabled={Boolean(taskPending)} aria-label={`下载 topic_${topicNumber} 的 Word`} onClick={() => void downloadArtifact(task, "下载 Word", `${taskApi}/docx/download`)}>{taskPending === "下载 Word" ? <Loader2 className="animate-spin" /> : <FileText />}Word</Button>}
                            {parts.tdk && deliveryAllowed && <Button size="sm" variant="outline" disabled={Boolean(taskPending)} aria-label={`下载 topic_${topicNumber} 的 D 文档`} onClick={() => void downloadArtifact(task, "下载 D 文档", `${taskApi}/tdk/download`)}>{taskPending === "下载 D 文档" ? <Loader2 className="animate-spin" /> : <FileText />}D.docx</Button>}
                            {parts.screenshot && reviewAllowed && <Button size="sm" variant="outline" disabled={Boolean(taskPending)} aria-label={`查看 topic_${topicNumber} 的终审截图`} onClick={() => void downloadArtifact(task, "打开终审截图", `${taskApi}/checks/final-ai/screenshot/download`)}>{taskPending === "打开终审截图" ? <Loader2 className="animate-spin" /> : <ImageIcon />}终审</Button>}
                            {!parts.package && !missing.length && deliveryAllowed && <Button size="sm" disabled={Boolean(taskPending)} onClick={() => void runTaskAction(task, "生成交付包", () => apiPost<TaskRecord>(`${taskApi}/package-delivery`, { revision: task.revision ?? 0 }))}>{taskPending === "生成交付包" ? <Loader2 className="animate-spin" /> : <Package />}打包</Button>}
                            {parts.package && deliveryAllowed && <Button size="sm" disabled={Boolean(taskPending)} aria-label={`下载 topic_${topicNumber} 的交付 ZIP`} onClick={() => void downloadArtifact(task, "下载交付 ZIP", `${taskApi}/delivery-package/download`)}>{taskPending === "下载交付 ZIP" ? <Loader2 className="animate-spin" /> : <Download />}ZIP</Button>}
                          </div>
                        </TableCell>
                      </TableRow>
                    );
                  })}
                  {loading && !filteredTasks.length && <TableRow><TableCell colSpan={5} className="h-36 text-center text-muted-foreground"><span className="inline-flex items-center gap-2" role="status" aria-live="polite"><Loader2 className="size-4 animate-spin" />正在读取交付记录…</span></TableCell></TableRow>}
                  {!loading && !filteredTasks.length && <TableRow><TableCell colSpan={5} className="h-36 text-center text-muted-foreground">没有符合当前条件的交付记录</TableCell></TableRow>}
                </TableBody>
              </Table>
            </div>
          </CardContent>
        </Card>
      </div>
    </main>
  );
}
