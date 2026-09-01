"use client";

/* eslint-disable react-hooks/set-state-in-effect */

import {
  AlertCircle,
  ArrowLeft,
  ExternalLink,
  Loader2,
  RefreshCw,
  RotateCcw,
  Square,
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
import { Progress } from "@/components/ui/progress";
import { apiGet, apiPost } from "@/lib/api";
import { sameProjectId } from "@/lib/project-id";
import { formatProjectDate } from "@/lib/project-date";
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
  ServerBatchSummary,
  ServerJobSummary,
} from "@/types";

function message(error: unknown) {
  return error instanceof Error ? error.message : "无法读取 Server 批次详情。";
}

function formatTime(value: string | null) {
  if (!value) return "—";
  return formatProjectDate(value, {
    dateStyle: "short",
    timeStyle: "medium",
  }) || "—";
}

export function ServerProjectBatchDetail({
  customer,
  batchId,
}: {
  customer: string;
  batchId: string;
}) {
  const [batch, setBatch] = useState<ServerBatchSummary | null>(null);
  const [role, setRole] = useState<
    AccessibleProject["effective_role"] | null
  >(null);
  const [isProjectOwner, setIsProjectOwner] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [pending, setPending] = useState<Record<string, string>>({});
  const projectApi = `/api/projects/${encodeURIComponent(customer)}`;

  const load = useCallback(async (quiet = false) => {
    if (!quiet) setLoading(true);
    try {
      const [nextBatch, projects] = await Promise.all([
        apiGet<ServerBatchSummary>(
          `${projectApi}/batches/${encodeURIComponent(batchId)}`,
        ),
        apiGet<AccessibleProject[]>("/api/projects"),
      ]);
      setBatch(nextBatch);
      const project = projects.find((item) => sameProjectId(item.project_id, customer));
      setRole(project?.effective_role ?? null);
      setIsProjectOwner(project?.is_project_owner === true);
      setError("");
    } catch (reason) {
      setError(message(reason));
    } finally {
      if (!quiet) setLoading(false);
    }
  }, [batchId, customer, projectApi]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (
      !batch?.jobs.some((job) =>
        SERVER_ACTIVE_JOB_STATUSES.has(job.status),
      )
    ) {
      return;
    }
    const timer = window.setTimeout(() => void load(true), 2500);
    return () => window.clearTimeout(timer);
  }, [batch, load]);

  const progress = useMemo(
    () =>
      batch?.total
        ? Math.round((batch.completed / batch.total) * 100)
        : 0,
    [batch],
  );

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
      await load(true);
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

  async function cancelBatch() {
    if (!batch) return;
    const key = `batch:${batch.batch_id}`;
    setPending((current) => ({ ...current, [key]: "cancel" }));
    try {
      const next = await apiPost<ServerBatchSummary>(
        `${projectApi}/batches/${encodeURIComponent(batch.batch_id)}/cancel`,
        {},
      );
      setBatch(next);
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

  const batchActive = Boolean(
    batch?.jobs.some((job) => SERVER_ACTIVE_JOB_STATUSES.has(job.status)),
  );
  const batchControllable = Boolean(
    batch && canControlServerJob(role, batch.operation, isProjectOwner),
  );
  const batchPendingKey = batch ? `batch:${batch.batch_id}` : "";

  return (
    <main className="mx-auto grid min-h-screen w-full max-w-[1480px] gap-4 p-4 xl:p-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <Button
            variant="ghost"
            size="sm"
            nativeButton={false}
            render={
              <Link
                href={`/projects/${encodeURIComponent(customer)}/batches`}
              />
            }
          >
            <ArrowLeft />
            返回 Server 批次
          </Button>
          <h1 className="mt-2 text-xl font-semibold">Server 批次详情</h1>
          <p className="mt-1 break-all font-mono text-xs text-muted-foreground">
            {batchId}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button
            variant="outline"
            className="min-h-11"
            disabled={loading}
            onClick={() => void load()}
          >
            {loading ? <Loader2 className="animate-spin" /> : <RefreshCw />}
            刷新
          </Button>
          {batchActive && batchControllable && batch && (
            <Button
              variant="destructive"
              className="min-h-11"
              disabled={Boolean(pending[batchPendingKey])}
              onClick={() => void cancelBatch()}
            >
              {pending[batchPendingKey] ? (
                <Loader2 className="animate-spin" />
              ) : (
                <Square />
              )}
              取消批次
            </Button>
          )}
        </div>
      </div>

      {error && (
        <Alert variant="destructive" role="alert">
          <AlertCircle />
          <AlertTitle>Server 批次读取失败</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      {loading && !batch ? (
        <Card>
          <CardContent className="flex min-h-56 items-center justify-center gap-2 text-muted-foreground">
            <Loader2 className="animate-spin" />
            正在读取 Project-scoped 批次…
          </CardContent>
        </Card>
      ) : batch ? (
        <>
          <Card>
            <CardHeader>
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <CardTitle>
                    {serverOperationLabel(batch.operation)}
                  </CardTitle>
                  <CardDescription>
                    创建 {formatTime(batch.created_at)} · 更新{" "}
                    {formatTime(batch.updated_at)}
                  </CardDescription>
                </div>
                <div className="flex gap-2">
                  <Badge variant={batchActive ? "secondary" : "outline"}>
                    {serverJobStatusLabel(batch.status)}
                  </Badge>
                  <Badge variant="outline">
                    {batch.completed}/{batch.total}
                  </Badge>
                </div>
              </div>
            </CardHeader>
            <CardContent className="grid gap-3">
              <Progress value={progress} className="h-2" />
              <div className="flex flex-wrap gap-x-5 gap-y-1 text-sm text-muted-foreground">
                {Object.entries(batch.status_counts).map(([status, count]) => (
                  <span key={status}>
                    {serverJobStatusLabel(status)} {count}
                  </span>
                ))}
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Job 明细</CardTitle>
              <CardDescription>
                Cancel/Retry 使用空 Body；权限在后端事务内按 Operation 重新判断。
              </CardDescription>
            </CardHeader>
            <CardContent className="grid gap-2">
              {batch.jobs.map((job) => {
                const active = SERVER_ACTIVE_JOB_STATUSES.has(job.status);
                const retryable = SERVER_RETRYABLE_JOB_STATUSES.has(job.status);
                const controllable = canControlServerJob(
                  role,
                  job.operation,
                  isProjectOwner,
                );
                return (
                  <div
                    key={job.job_id}
                    className="grid gap-3 rounded-lg border p-3 md:grid-cols-[minmax(0,1fr)_auto] md:items-center"
                  >
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="font-mono text-xs">{job.task_id}</span>
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
                      <p className="mt-1 text-xs text-muted-foreground">
                        Source Revision {job.source_revision} · Result Revision{" "}
                        {job.result_revision ?? "—"}
                      </p>
                      {job.has_error && (
                        <p className="mt-2 rounded bg-destructive/5 px-2 py-1.5 text-xs text-destructive">
                          Job 失败；原始错误和私有请求未进入公共响应。
                        </p>
                      )}
                    </div>
                    <div className="flex flex-wrap gap-2">
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
                      {active && controllable && (
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={Boolean(pending[job.job_id])}
                          onClick={() => void mutateJob(job, "cancel")}
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
            </CardContent>
          </Card>
        </>
      ) : null}
    </main>
  );
}
