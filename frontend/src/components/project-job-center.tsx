"use client";

import {
  AlertCircle,
  CheckCircle2,
  ExternalLink,
  Loader2,
  RotateCcw,
  Square,
  TimerReset,
  Trash2,
} from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { ScrollArea } from "@/components/ui/scroll-area";
import { apiDelete, apiGet, apiPost } from "@/lib/api";
import type {
  ApiMessage,
  BatchJobRecord,
  BatchOperation,
  BatchRecord,
} from "@/types";

const ACTIVE_STATUSES = new Set(["queued", "running", "retry_wait"]);
const RETRYABLE_STATUSES = new Set(["failed", "cancelled", "conflict"]);

function operationLabel(operation: BatchOperation) {
  const labels: Record<BatchOperation, string> = {
    titles: "生成标题",
    products: "查找产品",
    outline: "生成大纲",
    article: "生成正文",
    rewrite_article: "重写正文",
    seo_review: "SEO 质量复检",
    humanize: "降 AI 改写",
    restore_links: "恢复链接",
    prepare_images: "准备图片",
    export_docx: "导出 Word",
    generate_tdk: "生成 TDK",
    package_delivery: "交付打包",
  };
  return labels[operation];
}

function statusLabel(status: BatchJobRecord["status"]) {
  const labels: Record<BatchJobRecord["status"], string> = {
    queued: "排队中",
    running: "执行中",
    retry_wait: "等待重试",
    succeeded: "已完成",
    failed: "失败",
    cancelled: "已取消",
    conflict: "内容已变化",
  };
  return labels[status];
}

function operationStep(operation: BatchOperation) {
  if (operation === "titles") return "titles";
  if (operation === "products") return "products";
  if (operation === "outline") return "outline";
  if (
    operation === "article" ||
    operation === "rewrite_article" ||
    operation === "seo_review"
  ) return "article";
  if (operation === "humanize" || operation === "restore_links") return "review";
  if (operation === "prepare_images") return "media";
  return "files";
}

function message(error: unknown) {
  return error instanceof Error ? error.message : "操作失败";
}

export function ProjectJobCenter({ customer }: { customer: string }) {
  const [batches, setBatches] = useState<BatchRecord[]>([]);
  const [open, setOpen] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const [pending, setPending] = useState<Record<string, string>>({});

  const loadBatches = useCallback(async () => {
    const next = await apiGet<BatchRecord[]>(
      `/api/batches?customer=${encodeURIComponent(customer)}&limit=20`,
    );
    setBatches(next);
    return next;
  }, [customer]);

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    const poll = async () => {
      let delay = 12_000;
      try {
        const next = await loadBatches();
        const active = next.some((batch) =>
          batch.jobs.some((job) => ACTIVE_STATUSES.has(job.status)),
        );
        delay = active ? 2500 : 12_000;
        if (!cancelled) setError("");
      } catch (loadError) {
        if (!cancelled) setError(message(loadError));
      } finally {
        if (!cancelled) timer = window.setTimeout(poll, delay);
      }
    };
    void poll();
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [loadBatches]);

  const jobs = useMemo(
    () => batches.flatMap((batch) => batch.jobs.map((job) => ({ batch, job }))),
    [batches],
  );
  const activeJobs = jobs.filter(({ job }) => ACTIVE_STATUSES.has(job.status));
  const visibleJobs = [...jobs]
    .sort((left, right) => {
      const activeDifference =
        Number(ACTIVE_STATUSES.has(right.job.status)) -
        Number(ACTIVE_STATUSES.has(left.job.status));
      return (
        activeDifference ||
        right.job.updated_at.localeCompare(left.job.updated_at)
      );
    })
    .slice(0, 16);

  async function mutateJob(job: BatchJobRecord, action: "cancel" | "retry") {
    setPending((current) => ({ ...current, [job.id]: action }));
    setError("");
    setSuccess("");
    try {
      await apiPost<BatchJobRecord>(`/api/batch-jobs/${job.id}/${action}`);
      await loadBatches();
    } catch (actionError) {
      setError(message(actionError));
    } finally {
      setPending((current) => {
        const next = { ...current };
        delete next[job.id];
        return next;
      });
    }
  }

  async function deleteJob(job: BatchJobRecord) {
    if (
      !window.confirm(
        `删除 topic_${String(job.topic_index).padStart(3, "0")} 的“${operationLabel(job.operation)}”队列记录吗？\n\n这只删除运行记录，不会删除文章内容。`,
      )
    ) {
      return;
    }
    setPending((current) => ({ ...current, [job.id]: "delete" }));
    setError("");
    setSuccess("");
    try {
      const result = await apiDelete<ApiMessage>(`/api/batch-jobs/${job.id}`);
      await loadBatches();
      setSuccess(result.message);
    } catch (actionError) {
      setError(message(actionError));
    } finally {
      setPending((current) => {
        const next = { ...current };
        delete next[job.id];
        return next;
      });
    }
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger render={<Button variant={activeJobs.length ? "secondary" : "ghost"} size="sm" />}>
        {activeJobs.length ? <Loader2 className="animate-spin" /> : <TimerReset />}
        运行队列
        <Badge variant={activeJobs.length ? "default" : "outline"}>{activeJobs.length}</Badge>
      </DialogTrigger>
      <DialogContent className="sm:max-w-3xl">
        <DialogHeader>
          <DialogTitle>项目运行队列</DialogTitle>
          <DialogDescription>
            任务在后台持续执行。历史记录可手动删除，但不会影响已经生成的文章内容。
          </DialogDescription>
        </DialogHeader>

        {error && (
          <div className="flex items-start gap-2 rounded-lg border border-destructive/40 bg-destructive/5 p-3 text-sm text-destructive">
            <AlertCircle className="mt-0.5 size-4 shrink-0" />
            <span>{error}</span>
          </div>
        )}
        {success && (
          <div className="flex items-start gap-2 rounded-lg border border-primary/20 bg-primary/5 p-3 text-sm text-foreground">
            <CheckCircle2 className="mt-0.5 size-4 shrink-0" />
            <span>{success}</span>
          </div>
        )}

        <ScrollArea className="max-h-[60vh] pr-3">
          <div className="grid gap-2">
            {visibleJobs.map(({ job }) => {
              const active = ACTIVE_STATUSES.has(job.status);
              const retryable = RETRYABLE_STATUSES.has(job.status);
              const projectPath = `/projects/${encodeURIComponent(customer)}`;
              const href = `${projectPath}/articles/${encodeURIComponent(job.task_id)}?step=${operationStep(job.operation)}`;
              return (
                <div key={job.id} className="grid gap-2 rounded-lg border p-3 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-medium">topic_{String(job.topic_index).padStart(3, "0")}</span>
                      <Badge variant={job.status === "failed" || job.status === "conflict" ? "destructive" : active ? "secondary" : "outline"}>
                        {statusLabel(job.status)}
                      </Badge>
                      <span className="text-xs text-muted-foreground">{operationLabel(job.operation)}</span>
                    </div>
                    <div className="mt-1 truncate text-xs text-muted-foreground">{job.topic}</div>
                    {job.error && <div className="mt-1 line-clamp-2 text-xs text-destructive">{job.error}</div>}
                  </div>
                  <div className="flex flex-wrap justify-end gap-1">
                    <Button size="xs" variant="ghost" nativeButton={false} render={<Link href={href} onClick={() => setOpen(false)} />}>
                      <ExternalLink />打开文章
                    </Button>
                    {active && (
                      <Button size="xs" variant="outline" disabled={Boolean(pending[job.id])} onClick={() => void mutateJob(job, "cancel")}>
                        {pending[job.id] ? <Loader2 className="animate-spin" /> : <Square />}
                        取消
                      </Button>
                    )}
                    {retryable && (
                      <Button size="xs" variant="outline" disabled={Boolean(pending[job.id])} onClick={() => void mutateJob(job, "retry")}>
                        {pending[job.id] ? <Loader2 className="animate-spin" /> : <RotateCcw />}
                        重试
                      </Button>
                    )}
                    {!active && (
                      <Button
                        size="xs"
                        variant="ghost"
                        className="text-destructive hover:text-destructive"
                        disabled={Boolean(pending[job.id])}
                        onClick={() => void deleteJob(job)}
                      >
                        {pending[job.id] === "delete" ? (
                          <Loader2 className="animate-spin" />
                        ) : (
                          <Trash2 />
                        )}
                        删除记录
                      </Button>
                    )}
                  </div>
                </div>
              );
            })}
            {!visibleJobs.length && (
              <div className="rounded-lg border border-dashed p-8 text-center text-sm text-muted-foreground">
                当前没有队列记录。
              </div>
            )}
          </div>
        </ScrollArea>

        <div className="flex justify-end border-t pt-3">
          <Button variant="outline" nativeButton={false} render={<Link href={`/projects/${encodeURIComponent(customer)}/batches`} onClick={() => setOpen(false)} />}>
            查看全部批次
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
