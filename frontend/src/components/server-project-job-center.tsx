"use client";

import {
  AlertCircle,
  ExternalLink,
  Loader2,
  RotateCcw,
  Square,
  TimerReset,
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
import { apiGet, apiPost } from "@/lib/api";
import {
  canControlServerJob,
  serverJobHref,
  SERVER_ACTIVE_JOB_STATUSES,
  SERVER_RETRYABLE_JOB_STATUSES,
  serverJobStatusLabel,
  serverOperationLabel,
} from "@/lib/server-jobs";
import type {
  AccessibleProject,
  ServerBatchPage,
  ServerJobSummary,
} from "@/types";

function message(error: unknown) {
  return error instanceof Error ? error.message : "Server Job 操作失败。";
}

export function ServerProjectJobCenter({
  customer,
  role,
  isProjectOwner = false,
}: {
  customer: string;
  role: AccessibleProject["effective_role"] | null;
  isProjectOwner?: boolean;
}) {
  const [page, setPage] = useState<ServerBatchPage>({
    items: [],
    next_after_batch_id: null,
  });
  const [open, setOpen] = useState(false);
  const [error, setError] = useState("");
  const [pending, setPending] = useState<Record<string, string>>({});
  const projectApi = `/api/projects/${encodeURIComponent(customer)}`;

  const load = useCallback(async () => {
    const next = await apiGet<ServerBatchPage>(
      `${projectApi}/batches?limit=20`,
    );
    setPage(next);
    return next;
  }, [projectApi]);

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    const poll = async () => {
      let delay = 12_000;
      try {
        const next = await load();
        const active = next.items.some((batch) =>
          batch.jobs.some((job) =>
            SERVER_ACTIVE_JOB_STATUSES.has(job.status),
          ),
        );
        delay = active ? 2500 : 12_000;
        if (!cancelled) setError("");
      } catch (reason) {
        if (!cancelled) setError(message(reason));
      } finally {
        if (!cancelled) timer = window.setTimeout(poll, delay);
      }
    };
    void poll();
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [load]);

  const jobs = useMemo(
    () => page.items.flatMap((batch) => batch.jobs),
    [page.items],
  );
  const activeJobs = jobs.filter((job) =>
    SERVER_ACTIVE_JOB_STATUSES.has(job.status),
  );
  const problemJobs = jobs.filter((job) =>
    ["failed", "conflict"].includes(job.status),
  );
  const visibleJobs = [
    ...activeJobs,
    ...problemJobs.filter(
      (problem) =>
        !activeJobs.some((active) => active.job_id === problem.job_id),
    ),
  ].slice(0, 16);

  async function mutate(job: ServerJobSummary, action: "cancel" | "retry") {
    setPending((current) => ({ ...current, [job.job_id]: action }));
    setError("");
    try {
      await apiPost<ServerJobSummary>(
        `${projectApi}/jobs/${encodeURIComponent(job.job_id)}/${action}`,
        {},
      );
      await load();
    } catch (reason) {
      setError(message(reason));
    } finally {
      setPending((current) => {
        const next = { ...current };
        delete next[job.job_id];
        return next;
      });
    }
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger
        render={
          <Button
            variant={activeJobs.length ? "secondary" : "ghost"}
            size="sm"
          />
        }
      >
        {activeJobs.length ? (
          <Loader2 className="animate-spin" />
        ) : (
          <TimerReset />
        )}
        Server 队列
        <Badge variant={activeJobs.length ? "default" : "outline"}>
          {activeJobs.length}
        </Badge>
      </DialogTrigger>
      <DialogContent className="sm:max-w-3xl">
        <DialogHeader>
          <DialogTitle>Project-scoped Server 队列</DialogTitle>
          <DialogDescription>
            这里只显示已迁移 PostgreSQL Operation。公开状态不含 Request、Requester、Prompt、
            URL 或原始错误。
          </DialogDescription>
        </DialogHeader>

        {error && (
          <div
            className="flex items-start gap-2 rounded-lg border border-destructive/40 bg-destructive/5 p-3 text-sm text-destructive"
            role="alert"
          >
            <AlertCircle className="mt-0.5 size-4 shrink-0" />
            <span>{error}</span>
          </div>
        )}

        <ScrollArea className="max-h-[60dvh] pr-3">
          <div className="grid gap-2">
            {visibleJobs.map((job) => {
              const active = SERVER_ACTIVE_JOB_STATUSES.has(job.status);
              const retryable = SERVER_RETRYABLE_JOB_STATUSES.has(job.status);
              const controllable = canControlServerJob(
                role,
                job.operation,
                isProjectOwner,
              );
              const href = serverJobHref(
                customer,
                job.task_id,
                job.operation,
              );
              return (
                <div
                  key={job.job_id}
                  className="grid gap-2 rounded-lg border p-3 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center"
                >
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-mono text-xs">
                        {job.task_id}
                      </span>
                      <Badge
                        variant={
                          ["failed", "conflict"].includes(job.status)
                            ? "destructive"
                            : active
                              ? "secondary"
                              : "outline"
                        }
                      >
                        {serverJobStatusLabel(job.status)}
                      </Badge>
                      <span className="text-xs text-muted-foreground">
                        {serverOperationLabel(job.operation)}
                      </span>
                    </div>
                    <p className="mt-1 text-xs text-muted-foreground">
                      Attempt {job.attempts}/{job.max_attempts}
                      {job.has_error ? " · 服务端记录了脱敏失败状态" : ""}
                    </p>
                  </div>
                  <div className="flex flex-wrap justify-end gap-1">
                    <Button
                      size="xs"
                      variant="ghost"
                      nativeButton={false}
                      render={<Link href={href} onClick={() => setOpen(false)} />}
                    >
                      <ExternalLink />
                      打开文章
                    </Button>
                    {active && controllable && (
                      <Button
                        size="xs"
                        variant="outline"
                        disabled={Boolean(pending[job.job_id])}
                        onClick={() => void mutate(job, "cancel")}
                      >
                        {pending[job.job_id] ? (
                          <Loader2 className="animate-spin" />
                        ) : (
                          <Square />
                        )}
                        取消
                      </Button>
                    )}
                    {retryable && controllable && (
                      <Button
                        size="xs"
                        variant="outline"
                        disabled={Boolean(pending[job.job_id])}
                        onClick={() => void mutate(job, "retry")}
                      >
                        {pending[job.job_id] ? (
                          <Loader2 className="animate-spin" />
                        ) : (
                          <RotateCcw />
                        )}
                        重试
                      </Button>
                    )}
                  </div>
                </div>
              );
            })}
            {!visibleJobs.length && (
              <div className="rounded-lg border border-dashed p-8 text-center text-sm text-muted-foreground">
                当前没有运行中或需要处理的 Server Job。
              </div>
            )}
          </div>
        </ScrollArea>

        <div className="flex justify-end border-t pt-3">
          <Button
            variant="outline"
            nativeButton={false}
            render={
              <Link
                href={`/projects/${encodeURIComponent(customer)}/batches`}
                onClick={() => setOpen(false)}
              />
            }
          >
            查看全部 Server 批次
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
