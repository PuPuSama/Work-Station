"use client";

/* eslint-disable react-hooks/set-state-in-effect */

import { AlertCircle, Download, ExternalLink, Package, RefreshCw, Search } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { apiFileUrl, apiGet, apiPost } from "@/lib/api";
import { cn } from "@/lib/utils";
import type { TaskRecord } from "@/types";

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
  return {
    article: Boolean(task.final_article || task.linked_article || task.humanized_article),
    links: Boolean(task.link_validation?.passed),
    screenshot: Boolean(task.final_ai_check?.screenshot_path),
    images: Boolean(task.images?.length),
    word: Boolean(task.docx_path),
    tdk: Boolean(task.tdk_path),
    package: Boolean(task.delivery_package_path),
  };
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

export function ProjectDeliveryRecords({ customer }: { customer: string }) {
  const [tasks, setTasks] = useState<TaskRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<DeliveryFilter>("all");
  const [pending, setPending] = useState<Record<string, string>>({});

  const loadTasks = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setTasks(await apiGet<TaskRecord[]>(`/api/tasks?customer=${encodeURIComponent(customer)}`));
    } catch (loadError) {
      setError(errorMessage(loadError));
    } finally {
      setLoading(false);
    }
  }, [customer]);

  useEffect(() => {
    void loadTasks();
  }, [loadTasks]);

  const counts = useMemo(() => {
    const summarized = { all: tasks.length, incomplete: 0, ready: 0, packaged: 0 };
    for (const task of tasks) {
      const parts = deliveryParts(task);
      if (parts.package) summarized.packaged += 1;
      else if (parts.article && parts.links && parts.screenshot && parts.images && parts.word && parts.tdk) summarized.ready += 1;
      else summarized.incomplete += 1;
    }
    return summarized;
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

  const projectPath = `/projects/${encodeURIComponent(customer)}`;
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
            <h1 className="text-xl font-semibold">交付记录</h1>
            <p className="mt-1 text-sm text-muted-foreground">集中检查 Word、D 文档、最终 AI 截图、图片和交付包。</p>
          </div>
        </div>
      </div>

      <div className="mx-auto grid max-w-[1480px] gap-4 px-5 py-5">
        {error && <Alert variant="destructive"><AlertCircle /><AlertTitle>操作失败</AlertTitle><AlertDescription>{error}</AlertDescription></Alert>}
        {message && <Alert><AlertTitle>操作完成</AlertTitle><AlertDescription>{message}</AlertDescription></Alert>}

        <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
          {filters.map((item) => (
            <button key={item.value} type="button" onClick={() => setFilter(item.value)} className={cn("rounded-lg border bg-card px-3 py-2.5 text-left hover:bg-accent/40", filter === item.value && "border-primary bg-accent/50 ring-1 ring-primary/20")}>
              <span className="block text-xs text-muted-foreground">{item.label}</span>
              <span className="mt-0.5 block text-xl font-semibold">{counts[item.value]}</span>
            </button>
          ))}
        </div>

        <Card className="rounded-lg">
          <CardHeader className="border-b">
            <CardTitle>文章交付状态</CardTitle>
            <CardDescription>缺失项可直接进入文章的交付阶段补齐。</CardDescription>
          </CardHeader>
          <CardContent className="grid gap-3">
            <div className="flex flex-col gap-2 sm:flex-row">
              <div className="relative flex-1"><Search className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" /><Input value={query} onChange={(event) => setQuery(event.target.value)} className="pl-8" placeholder="搜索编号、话题或标题" /></div>
              <Button variant="outline" onClick={() => void loadTasks()} disabled={loading}><RefreshCw className={cn(loading && "animate-spin")} />刷新</Button>
            </div>
            <div className="overflow-hidden rounded-lg border">
              <Table>
                <TableHeader><TableRow><TableHead className="w-28">编号</TableHead><TableHead>文章</TableHead><TableHead>交付项</TableHead><TableHead className="w-32">更新时间</TableHead><TableHead className="w-64 text-right">操作</TableHead></TableRow></TableHeader>
                <TableBody>
                  {filteredTasks.map((task) => {
                    const parts = deliveryParts(task);
                    const missing = Object.entries(parts)
                      .filter(([key, done]) => key !== "package" && !done)
                      .map(([key]) => DELIVERY_LABELS[key]);
                    return <TableRow key={task.id}>
                      <TableCell className="font-mono text-xs">topic_{String(task.topic_index).padStart(3, "0")}</TableCell>
                      <TableCell className="max-w-0 whitespace-normal"><div className="truncate font-medium">{task.selected_title || task.topic}</div><div className="mt-1 truncate text-xs text-muted-foreground">{task.topic}</div></TableCell>
                      <TableCell><div className="flex flex-wrap gap-1">{parts.package ? <Badge>已打包</Badge> : missing.length ? missing.map((item) => <Badge key={item} variant="outline">缺 {item}</Badge>) : <Badge>可打包</Badge>}</div></TableCell>
                      <TableCell className="text-xs text-muted-foreground">{formatUpdatedAt(task.updated_at)}</TableCell>
                      <TableCell><div className="flex justify-end gap-1">
                        {!parts.package && !missing.length && <Button size="sm" disabled={Boolean(pending[task.id])} onClick={() => void runTaskAction(task, "生成交付包", () => apiPost<TaskRecord>(`/api/tasks/${task.id}/package-delivery`))}><Package />打包</Button>}
                        {parts.package && <Button size="sm" variant="outline" nativeButton={false} render={<a href={apiFileUrl(`/api/tasks/${task.id}/delivery-package/download`)} />}><Download />下载</Button>}
                        <Link href={`${projectPath}/articles/${encodeURIComponent(task.id)}?step=files`} className="inline-flex h-8 items-center gap-1 rounded-md px-2.5 text-sm hover:bg-muted"><ExternalLink className="size-4" />处理</Link>
                      </div></TableCell>
                    </TableRow>;
                  })}
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
