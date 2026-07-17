"use client";

/* eslint-disable react-hooks/set-state-in-effect */

import { ArrowLeft, ExternalLink, Loader2, RefreshCw, X } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";

import {
  batchStatusLabel,
  jobStatusLabel,
  operationLabel,
  operationStep,
} from "@/components/project-batch-center";
import { ProjectNavigation } from "@/components/project-navigation";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { apiGet, apiPost } from "@/lib/api";
import type { BatchJobRecord, BatchRecord } from "@/types";

type ProjectBatchDetailProps = {
  customer: string;
  batchId: string;
};

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "读取批次失败";
}

function formatTime(value: string) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN");
}

export function ProjectBatchDetail({ customer, batchId }: ProjectBatchDetailProps) {
  const [batch, setBatch] = useState<BatchRecord | null>(null);
  const [error, setError] = useState("");
  const [pending, setPending] = useState(true);
  const [cancelPending, setCancelPending] = useState(false);
  const [retryPending, setRetryPending] = useState<Set<string>>(new Set());

  const loadBatch = useCallback(async (quiet = false) => {
    if (!quiet) setPending(true);
    try {
      const next = await apiGet<BatchRecord>(`/api/batches/${encodeURIComponent(batchId)}`);
      setBatch(next);
      setError("");
    } catch (nextError) {
      setError(errorMessage(nextError));
    } finally {
      if (!quiet) setPending(false);
    }
  }, [batchId]);

  useEffect(() => {
    void loadBatch();
  }, [loadBatch]);

  useEffect(() => {
    if (!batch || !["queued", "running"].includes(batch.status)) return;
    const timer = window.setInterval(() => void loadBatch(true), 1500);
    return () => window.clearInterval(timer);
  }, [batch, loadBatch]);

  const progress = useMemo(
    () => (batch?.total ? Math.round((batch.completed / batch.total) * 100) : 0),
    [batch],
  );

  async function cancelBatch() {
    if (!batch) return;
    setCancelPending(true);
    try {
      setBatch(await apiPost<BatchRecord>(`/api/batches/${batch.id}/cancel`));
      setError("");
    } catch (nextError) {
      setError(errorMessage(nextError));
    } finally {
      setCancelPending(false);
    }
  }

  async function retryJob(jobId: string) {
    setRetryPending((current) => new Set(current).add(jobId));
    try {
      const updated = await apiPost<BatchJobRecord>(`/api/batch-jobs/${jobId}/retry`);
      setBatch((current) =>
        current
          ? {
              ...current,
              status: "running",
              jobs: current.jobs.map((job) => (job.id === updated.id ? updated : job)),
            }
          : current,
      );
      setError("");
    } catch (nextError) {
      setError(errorMessage(nextError));
    } finally {
      setRetryPending((current) => {
        const next = new Set(current);
        next.delete(jobId);
        return next;
      });
    }
  }

  const active = Boolean(batch && ["queued", "running"].includes(batch.status));

  return (
    <main className="mx-auto grid min-h-screen w-full max-w-[1680px] gap-4 p-4 xl:p-6">
      <ProjectNavigation customer={customer} />

      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <Button
            variant="ghost"
            size="sm"
            nativeButton={false}
            render={<Link href={`/projects/${encodeURIComponent(customer)}/batches`} />}
          >
            <ArrowLeft />
            返回批量生成中心
          </Button>
          <h1 className="mt-2 text-xl font-semibold">批次详情</h1>
          <p className="mt-1 break-all text-sm text-muted-foreground">{batchId}</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" onClick={() => void loadBatch()} disabled={pending}>
            {pending ? <Loader2 className="animate-spin" /> : <RefreshCw />}
            刷新
          </Button>
          {active && (
            <Button variant="destructive" onClick={() => void cancelBatch()} disabled={cancelPending}>
              {cancelPending ? <Loader2 className="animate-spin" /> : <X />}
              取消批次
            </Button>
          )}
        </div>
      </div>

      {error && (
        <Alert variant="destructive">
          <AlertTitle>批次读取失败</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      {pending && !batch ? (
        <Card className="rounded-lg">
          <CardContent className="flex min-h-56 items-center justify-center gap-2 text-muted-foreground">
            <Loader2 className="animate-spin" />
            正在读取批次
          </CardContent>
        </Card>
      ) : batch ? (
        <>
          <Card className="rounded-lg">
            <CardHeader>
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <CardTitle>{operationLabel(batch.operation)}</CardTitle>
                  <CardDescription>创建于 {formatTime(batch.created_at)} · 更新于 {formatTime(batch.updated_at)}</CardDescription>
                </div>
                <div className="flex gap-2">
                  <Badge variant={batch.status === "completed_with_errors" ? "destructive" : active ? "secondary" : "outline"}>
                    {batchStatusLabel(batch.status)}
                  </Badge>
                  <Badge variant="outline">{batch.completed}/{batch.total}</Badge>
                </div>
              </div>
            </CardHeader>
            <CardContent className="grid gap-3">
              <Progress value={progress} className="h-2" />
              <div className="flex flex-wrap gap-x-5 gap-y-1 text-sm text-muted-foreground">
                {Object.entries(batch.status_counts).map(([status, count]) => (
                  <span key={status}>{jobStatusLabel(status as BatchJobRecord["status"])} {count}</span>
                ))}
              </div>
            </CardContent>
          </Card>

          <Card className="rounded-lg">
            <CardHeader>
              <CardTitle>任务明细</CardTitle>
              <CardDescription>每篇文章都可以直接打开到本次批处理对应的步骤。</CardDescription>
            </CardHeader>
            <CardContent className="grid gap-2">
              {batch.jobs.map((job) => {
                const canRetry = ["failed", "cancelled", "conflict"].includes(job.status);
                return (
                  <div key={job.id} className="grid gap-2 rounded-lg border p-3 sm:grid-cols-[1fr_auto] sm:items-center">
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="font-medium">topic_{String(job.topic_index).padStart(3, "0")}</span>
                        <Badge variant={job.status === "failed" || job.status === "conflict" ? "destructive" : job.status === "succeeded" ? "default" : "outline"}>
                          {jobStatusLabel(job.status)}
                        </Badge>
                        {job.attempts > 0 && <span className="text-xs text-muted-foreground">尝试 {job.attempts}/{job.max_attempts}</span>}
                      </div>
                      <p className="mt-1 text-sm text-muted-foreground">{job.topic}</p>
                      {job.error && <p className="mt-2 rounded bg-destructive/5 px-2 py-1.5 text-xs text-destructive">{job.error}</p>}
                    </div>
                    <div className="flex flex-wrap gap-2">
                      <Button
                        size="sm"
                        variant="outline"
                        nativeButton={false}
                        render={
                          <Link
                            href={`/projects/${encodeURIComponent(customer)}/articles/${encodeURIComponent(job.task_id)}?step=${operationStep(batch.operation)}`}
                          />
                        }
                      >
                        <ExternalLink />
                        打开文章
                      </Button>
                      {canRetry && (
                        <Button size="sm" onClick={() => void retryJob(job.id)} disabled={retryPending.has(job.id)}>
                          {retryPending.has(job.id) ? <Loader2 className="animate-spin" /> : <RefreshCw />}
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
