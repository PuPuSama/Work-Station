"use client";

/* eslint-disable react-hooks/set-state-in-effect */

import {
  AlertCircle,
  ExternalLink,
  Loader2,
  RefreshCw,
  RotateCcw,
  Square,
} from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

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
import { Progress } from "@/components/ui/progress";
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
  ServerBatchSummary,
  ServerJobSummary,
} from "@/types";

function message(error: unknown) {
  return error instanceof Error ? error.message : "无法读取 Server 批次。";
}

function progressFor(batch: ServerBatchSummary) {
  return batch.total ? Math.round((batch.completed / batch.total) * 100) : 0;
}

export function ServerProjectBatchCenter({
  customer,
}: {
  customer: string;
}) {
  const [page, setPage] = useState<ServerBatchPage>({
    items: [],
    next_after_batch_id: null,
  });
  const [role, setRole] = useState<
    AccessibleProject["effective_role"] | null
  >(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState("");
  const [pending, setPending] = useState<Record<string, string>>({});
  const projectApi = `/api/projects/${encodeURIComponent(customer)}`;

  const load = useCallback(async (quiet = false) => {
    if (!quiet) setLoading(true);
    try {
      const [next, projects] = await Promise.all([
        apiGet<ServerBatchPage>(`${projectApi}/batches?limit=20`),
        apiGet<AccessibleProject[]>("/api/projects"),
      ]);
      setPage(next);
      setRole(
        projects.find((project) => project.project_id === customer)
          ?.effective_role ?? null,
      );
      setError("");
    } catch (reason) {
      setError(message(reason));
    } finally {
      if (!quiet) setLoading(false);
    }
  }, [customer, projectApi]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    const active = page.items.some((batch) =>
      batch.jobs.some((job) =>
        SERVER_ACTIVE_JOB_STATUSES.has(job.status),
      ),
    );
    if (!active) return;
    const timer = window.setTimeout(() => void load(true), 2500);
    return () => window.clearTimeout(timer);
  }, [load, page.items]);

  async function loadMore() {
    if (!page.next_after_batch_id) return;
    setLoadingMore(true);
    try {
      const next = await apiGet<ServerBatchPage>(
        `${projectApi}/batches?limit=20&after_batch_id=${encodeURIComponent(page.next_after_batch_id)}`,
      );
      setPage((current) => ({
        items: [...current.items, ...next.items],
        next_after_batch_id: next.next_after_batch_id,
      }));
      setError("");
    } catch (reason) {
      setError(message(reason));
    } finally {
      setLoadingMore(false);
    }
  }

  async function mutateJob(
    job: ServerJobSummary,
    action: "cancel" | "retry",
  ) {
    setPending((current) => ({ ...current, [job.job_id]: action }));
    try {
      await apiPost<ServerJobSummary>(
        `${projectApi}/jobs/${encodeURIComponent(job.job_id)}/${action}`,
        {},
      );
      await load();
      setError("");
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

  async function cancelBatch(batch: ServerBatchSummary) {
    const key = `batch:${batch.batch_id}`;
    setPending((current) => ({ ...current, [key]: "cancel" }));
    try {
      await apiPost<ServerBatchSummary>(
        `${projectApi}/batches/${encodeURIComponent(batch.batch_id)}/cancel`,
        {},
      );
      await load();
      setError("");
    } catch (reason) {
      setError(message(reason));
    } finally {
      setPending((current) => {
        const next = { ...current };
        delete next[key];
        return next;
      });
    }
  }

  return (
    <main className="mx-auto grid min-h-screen w-full max-w-[1480px] gap-4 p-4 xl:p-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Server 批次与 Job</h1>
          <p className="mt-1 text-sm leading-6 text-muted-foreground">
            仅显示当前 Project 已迁移 PostgreSQL Operation；Retry 重放服务端保存的可信请求，
            不接受浏览器覆盖参数。
          </p>
        </div>
        <Button
          type="button"
          variant="outline"
          className="min-h-11"
          disabled={loading}
          onClick={() => void load()}
        >
          {loading ? <Loader2 className="animate-spin" /> : <RefreshCw />}
          刷新
        </Button>
      </div>

      {error && (
        <Alert variant="destructive" role="alert">
          <AlertCircle />
          <AlertTitle>Server Job Control 不可用</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      {loading && page.items.length === 0 ? (
        <Card>
          <CardContent className="flex min-h-56 items-center justify-center gap-2 text-muted-foreground">
            <Loader2 className="animate-spin" />
            正在读取 Project-scoped 批次…
          </CardContent>
        </Card>
      ) : page.items.length === 0 ? (
        <Card>
          <CardContent className="py-12 text-center text-sm text-muted-foreground">
            当前 Project 还没有已迁移的 Server 批次。
          </CardContent>
        </Card>
      ) : (
        <div className="grid gap-4">
          {page.items.map((batch) => {
            const active = batch.jobs.some((job) =>
              SERVER_ACTIVE_JOB_STATUSES.has(job.status),
            );
            const controllable = canControlServerJob(role, batch.operation);
            const batchKey = `batch:${batch.batch_id}`;
            return (
              <Card key={batch.batch_id}>
                <CardHeader className="border-b">
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div>
                      <CardTitle>
                        {serverOperationLabel(batch.operation)}
                      </CardTitle>
                      <CardDescription className="mt-1 break-all font-mono">
                        {batch.batch_id}
                      </CardDescription>
                    </div>
                    <div className="flex flex-wrap gap-2">
                      <Badge variant={active ? "secondary" : "outline"}>
                        {serverJobStatusLabel(batch.status)}
                      </Badge>
                      <Badge variant="outline">
                        {batch.completed}/{batch.total}
                      </Badge>
                    </div>
                  </div>
                </CardHeader>
                <CardContent className="grid gap-4">
                  <Progress value={progressFor(batch)} className="h-2" />
                  <div className="grid gap-2">
                    {batch.jobs.map((job) => {
                      const jobActive = SERVER_ACTIVE_JOB_STATUSES.has(
                        job.status,
                      );
                      const retryable = SERVER_RETRYABLE_JOB_STATUSES.has(
                        job.status,
                      );
                      const jobControllable = canControlServerJob(
                        role,
                        job.operation,
                      );
                      return (
                        <div
                          key={job.job_id}
                          className="grid gap-2 rounded-lg border p-3 md:grid-cols-[minmax(0,1fr)_auto] md:items-center"
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
                                    : job.status === "succeeded"
                                      ? "default"
                                      : "outline"
                                }
                              >
                                {serverJobStatusLabel(job.status)}
                              </Badge>
                              <span className="text-xs text-muted-foreground">
                                Attempt {job.attempts}/{job.max_attempts}
                              </span>
                            </div>
                            {job.has_error && (
                              <p className="mt-1 text-xs text-destructive">
                                服务端记录了脱敏失败状态；公开响应不返回原始错误。
                              </p>
                            )}
                          </div>
                          <div className="flex flex-wrap justify-end gap-2">
                            <Button
                              size="sm"
                              variant="ghost"
                              nativeButton={false}
                              render={
                                <Link
                                  href={serverJobHref(
                                    customer,
                                    job.task_id,
                                    job.operation,
                                  )}
                                />
                              }
                            >
                              <ExternalLink />
                              打开文章
                            </Button>
                            {jobActive && jobControllable && (
                              <Button
                                size="sm"
                                variant="outline"
                                disabled={Boolean(pending[job.job_id])}
                                onClick={() =>
                                  void mutateJob(job, "cancel")
                                }
                              >
                                {pending[job.job_id] ? (
                                  <Loader2 className="animate-spin" />
                                ) : (
                                  <Square />
                                )}
                                取消
                              </Button>
                            )}
                            {retryable && jobControllable && (
                              <Button
                                size="sm"
                                variant="outline"
                                disabled={Boolean(pending[job.job_id])}
                                onClick={() => void mutateJob(job, "retry")}
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
                  </div>
                  <div className="flex flex-wrap justify-end gap-2">
                    <Button
                      variant="outline"
                      nativeButton={false}
                      render={
                        <Link
                          href={`/projects/${encodeURIComponent(customer)}/batches/${encodeURIComponent(batch.batch_id)}`}
                        />
                      }
                    >
                      查看批次详情
                    </Button>
                    {active && controllable && (
                      <Button
                        variant="destructive"
                        disabled={Boolean(pending[batchKey])}
                        onClick={() => void cancelBatch(batch)}
                      >
                        {pending[batchKey] ? (
                          <Loader2 className="animate-spin" />
                        ) : (
                          <Square />
                        )}
                        取消整个批次
                      </Button>
                    )}
                  </div>
                </CardContent>
              </Card>
            );
          })}
        </div>
      )}

      {page.next_after_batch_id && (
        <Button
          type="button"
          variant="outline"
          className="min-h-11"
          disabled={loadingMore}
          onClick={() => void loadMore()}
        >
          {loadingMore && <Loader2 className="animate-spin" />}
          加载更早批次
        </Button>
      )}
    </main>
  );
}
