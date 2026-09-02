"use client";

import {
  ArrowLeft,
  Check,
  CircleDot,
  Download,
  ExternalLink,
  Layers3,
  Loader2,
  Pause,
  Play,
  RefreshCw,
  Settings2,
  Sparkles,
  Square,
  Workflow,
} from "lucide-react";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";

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
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import {
  WORKFLOW_STEP_LABELS,
  WorkflowArticleCards,
} from "@/components/workflow-article-cards";
import { ApiError, apiGet, apiPost } from "@/lib/api";
import { triggerBrowserDownload } from "@/lib/browser-download";
import type {
  AccessibleProject,
  WorkflowAssistantBatchDownload,
  WorkflowAssistantConversation,
  WorkflowAssistantDispatch,
  WorkflowAssistantPlan,
} from "@/types";

const MAX_ARTICLES_PER_PROJECT = 50;
const MAX_TOTAL_ARTICLES = 60;

const statusLabels: Record<WorkflowAssistantPlan["status"], string> = {
  draft: "草稿",
  awaiting_confirmation: "等待确认",
  queued: "已确认，等待执行",
  running: "执行中",
  waiting_review: "等待人工处理",
  paused: "已暂停",
  completed: "已完成",
  failed: "执行失败",
  cancelled: "已取消",
};

type BatchProjectRow = {
  project_id: string;
  article_count: number;
};

type PlanAction = "confirm" | "pause" | "resume" | "retry" | "cancel";

function localId(prefix: string) {
  const random = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`;
  return `${prefix}-${random}`.replace(/[^A-Za-z0-9._:-]/g, "-");
}

function messageText(error: unknown) {
  if (error instanceof ApiError && error.detail && typeof error.detail === "object") {
    const detail = error.detail as { message?: unknown };
    if (typeof detail.message === "string") return detail.message;
  }
  return error instanceof Error ? error.message : "批量写作请求失败。";
}

function statusVariant(
  status: WorkflowAssistantPlan["status"],
): "default" | "outline" | "secondary" | "destructive" {
  if (status === "failed") return "destructive";
  if (status === "awaiting_confirmation" || status === "waiting_review") return "outline";
  if (status === "completed") return "secondary";
  return "default";
}

function summaryText(value: Record<string, unknown>, key: string): string {
  const candidate = value[key];
  return typeof candidate === "string" ? candidate.trim() : "";
}

function planArticleCounts(plan: WorkflowAssistantPlan): Record<string, number> {
  const chains = new Set<string>();
  for (const step of plan.steps) {
    if (step.action_kind === "create_task") {
      chains.add(`${step.project_id}:create:${step.step_id}`);
      continue;
    }
    if (step.action_kind !== "package_delivery") continue;
    const source = summaryText(step.input_summary, "create_task_step_id");
    const key = step.article_task_id
      ? `${step.project_id}:task:${step.article_task_id}`
      : source
        ? `${step.project_id}:create:${source}`
        : `${step.project_id}:package:${step.step_id}`;
    chains.add(key);
  }
  const counts: Record<string, number> = {};
  for (const chain of chains) {
    const projectId = chain.split(":", 1)[0];
    counts[projectId] = (counts[projectId] || 0) + 1;
  }
  return counts;
}

function readyDeliveryCount(plan: WorkflowAssistantPlan): number {
  return plan.steps.filter(
    (step) => step.action_kind === "package_delivery"
      && step.status === "succeeded"
      && step.output_summary.artifact_kind === "delivery_package"
      && Boolean(String(step.output_summary.asset_id || "").trim()),
  ).length;
}

export function BatchWritingWorkspace() {
  const [projects, setProjects] = useState<AccessibleProject[]>([]);
  const [rows, setRows] = useState<BatchProjectRow[]>([]);
  const [skipReview, setSkipReview] = useState(false);
  const [concurrencyLimit, setConcurrencyLimit] = useState(5);
  const [writingInstruction, setWritingInstruction] = useState("");
  const [plan, setPlan] = useState<WorkflowAssistantPlan | null>(null);
  const [loading, setLoading] = useState(true);
  const [pending, setPending] = useState("");
  const [error, setError] = useState("");

  const projectById = useMemo(
    () => new Map(projects.map((project) => [project.project_id, project])),
    [projects],
  );
  const projectNames = useMemo(
    () => new Map(
      projects.map((project) => [
        project.project_id,
        project.customer_name || project.project_id,
      ]),
    ),
    [projects],
  );
  const totalArticles = rows.reduce((total, row) => total + row.article_count, 0);
  const selectedPlanCounts = plan ? planArticleCounts(plan) : {};
  const deliveryReady = plan ? readyDeliveryCount(plan) : 0;

  useEffect(() => {
    let disposed = false;
    async function load() {
      setLoading(true);
      setError("");
      try {
        const params = new URLSearchParams(window.location.search);
        const planId = params.get("plan_id");
        const projectHint = params.get("project");
        const [nextProjects, nextPlan] = await Promise.all([
          apiGet<AccessibleProject[]>("/api/projects"),
          planId
            ? apiGet<WorkflowAssistantPlan>(
              `/api/workflow-assistant/plans/${encodeURIComponent(planId)}`,
            )
            : Promise.resolve(null),
        ]);
        if (disposed) return;
        setProjects(nextProjects);
        if (nextPlan) {
          setPlan(nextPlan);
          const counts = planArticleCounts(nextPlan);
          setRows(
            nextPlan.project_ids.map((projectId) => ({
              project_id: projectId,
              article_count: counts[projectId] || 1,
            })),
          );
        } else if (projectHint && nextProjects.some((item) => item.project_id === projectHint)) {
          setRows([{ project_id: projectHint, article_count: 1 }]);
        }
      } catch (nextError) {
        if (!disposed) setError(messageText(nextError));
      } finally {
        if (!disposed) setLoading(false);
      }
    }
    void load();
    return () => {
      disposed = true;
    };
  }, []);

  useEffect(() => {
    if (!plan || !["queued", "running", "waiting_review"].includes(plan.status)) {
      return;
    }
    let disposed = false;
    const timer = window.setTimeout(() => {
      void apiGet<WorkflowAssistantPlan>(
        `/api/workflow-assistant/plans/${encodeURIComponent(plan.plan_id)}`,
      )
        .then((nextPlan) => {
          if (!disposed) setPlan(nextPlan);
        })
        .catch(() => {
          // Keep the current durable plan visible during a temporary poll failure.
        });
    }, 2500);
    return () => {
      disposed = true;
      window.clearTimeout(timer);
    };
  }, [plan]);

  function toggleProject(projectId: string, checked: boolean) {
    setRows((current) => {
      if (checked) {
        return current.some((row) => row.project_id === projectId)
          ? current
          : [...current, { project_id: projectId, article_count: 1 }];
      }
      return current.filter((row) => row.project_id !== projectId);
    });
  }

  function updateCount(projectId: string, value: string) {
    const parsed = Number(value);
    const articleCount = Number.isFinite(parsed)
      ? Math.max(1, Math.min(MAX_ARTICLES_PER_PROJECT, Math.trunc(parsed)))
      : 1;
    setRows((current) => current.map((row) => (
      row.project_id === projectId ? { ...row, article_count: articleCount } : row
    )));
  }

  async function createPlan() {
    if (!rows.length) {
      setError("请至少选择一个项目。");
      return;
    }
    if (totalArticles > MAX_TOTAL_ARTICLES) {
      setError(`本批次最多 ${MAX_TOTAL_ARTICLES} 篇文章，请减少项目数量或篇数。`);
      return;
    }
    setPending("create");
    setError("");
    try {
      const projectIds = rows.map((row) => row.project_id);
      const conversation = await apiPost<WorkflowAssistantConversation>(
        "/api/workflow-assistant/conversations",
        {
          title: `批量写作 · ${projectIds.length} 个项目 · ${totalArticles} 篇`,
          project_ids: projectIds,
        },
      );
      const response = await apiPost<WorkflowAssistantDispatch>(
        `/api/workflow-assistant/conversations/${encodeURIComponent(conversation.conversation_id)}/batch-plans`,
        {
          projects: rows,
          writing_instruction: writingInstruction.trim(),
          skip_review: skipReview,
          concurrency_limit: concurrencyLimit,
          request_id: localId("batch-request"),
          idempotency_key: localId("batch-idempotency"),
        },
      );
      if (!response.plan) throw new Error("服务器没有返回批量写作计划。");
      setPlan(response.plan);
      const params = new URLSearchParams({
        plan_id: response.plan.plan_id,
        conversation_id: response.plan.conversation_id,
      });
      window.history.replaceState(null, "", `/batch-writing?${params.toString()}`);
    } catch (nextError) {
      setError(messageText(nextError));
    } finally {
      setPending("");
    }
  }

  async function changePlan(action: PlanAction) {
    if (!plan || pending) return;
    setPending(action);
    setError("");
    try {
      const body = action === "confirm" || action === "retry"
        ? { revision: plan.revision, plan_hash: plan.plan_hash }
        : { revision: plan.revision };
      const nextPlan = await apiPost<WorkflowAssistantPlan>(
        `/api/workflow-assistant/plans/${encodeURIComponent(plan.plan_id)}/${action}`,
        body,
      );
      setPlan(nextPlan);
    } catch (nextError) {
      setError(messageText(nextError));
    } finally {
      setPending("");
    }
  }

  async function downloadDelivery() {
    if (!plan || !deliveryReady || pending) return;
    setPending("download");
    setError("");
    try {
      const download = await apiGet<WorkflowAssistantBatchDownload>(
        `/api/workflow-assistant/plans/${encodeURIComponent(plan.plan_id)}/delivery-package/download`,
        30_000,
      );
      if (!download.url) throw new Error("服务器没有返回可用的批量下载地址。");
      triggerBrowserDownload(download.url, download.filename || "batch-delivery.zip");
    } catch (nextError) {
      setError(messageText(nextError));
    } finally {
      setPending("");
    }
  }

  function resetPlan() {
    setPlan(null);
    setRows([]);
    setError("");
    window.history.replaceState(null, "", "/batch-writing");
  }

  return (
    <main className="min-h-screen bg-background text-foreground">
      <header className="border-b bg-card">
        <div className="mx-auto flex max-w-[1380px] items-center justify-between gap-4 px-5 py-5">
          <div className="flex min-w-0 items-center gap-3">
            <Link
              href="/"
              className="flex size-10 shrink-0 items-center justify-center rounded-xl bg-primary text-primary-foreground"
              aria-label="返回项目目录"
            >
              <ArrowLeft className="size-5" />
            </Link>
            <div className="min-w-0">
              <p className="text-xs font-semibold uppercase tracking-[0.14em] text-primary">
                Article Agent · Batch Writing
              </p>
              <h1 className="truncate text-xl font-semibold">批量写文章</h1>
              <p className="text-sm text-muted-foreground">
                用明确的项目和数量配置批次，服务端直接生成固定写作计划。
              </p>
            </div>
          </div>
          <div className="flex flex-wrap items-center justify-end gap-2">
            <Button
              type="button"
              variant="outline"
              nativeButton={false}
              render={<Link href="/assistant" />}
              className="shrink-0"
            >
              <Sparkles />
              对话助手
            </Button>
            <Button
              type="button"
              variant="outline"
              nativeButton={false}
              render={<Link href="/settings" />}
              className="shrink-0"
            >
              <Settings2 />
              全局设置
            </Button>
          </div>
        </div>
      </header>

      <div className="mx-auto grid max-w-[1380px] gap-4 px-5 py-5 lg:grid-cols-[minmax(0,1fr)_380px]">
        <section className="grid content-start gap-4">
          {error && (
            <Alert variant="destructive" role="alert">
              <CircleDot />
              <AlertTitle>批量写作不可用</AlertTitle>
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}

          <Card className="gap-0 py-0">
            <CardHeader className="border-b px-5 py-5">
              <CardTitle className="flex items-center gap-2">
                <Layers3 className="size-5 text-primary" />
                1. 选择项目与文章数量
              </CardTitle>
              <CardDescription>
                每个项目的数量独立配置；服务端会优先继续未完成任务，不足时再使用已发布话题创建新任务。
              </CardDescription>
            </CardHeader>
            <CardContent className="grid gap-3 px-5 py-5">
              {loading ? (
                <div className="flex items-center gap-2 py-8 text-sm text-muted-foreground">
                  <Loader2 className="size-4 animate-spin" />正在读取可访问项目…
                </div>
              ) : projects.length ? (
                projects.map((project) => {
                  const row = rows.find((item) => item.project_id === project.project_id);
                  const checked = Boolean(row);
                  return (
                    <div
                      key={project.project_id}
                      className={`rounded-xl border p-4 transition-colors ${checked ? "border-primary/50 bg-primary/5" : "bg-card"}`}
                    >
                      <div className="flex flex-wrap items-start justify-between gap-3">
                        <label className="flex min-w-0 cursor-pointer items-start gap-3">
                          <input
                            type="checkbox"
                            className="mt-1 size-4 accent-primary"
                            checked={checked}
                            onChange={(event) => toggleProject(project.project_id, event.target.checked)}
                          />
                          <span className="min-w-0">
                            <span className="block truncate font-medium">
                              {project.customer_name || project.project_id}
                            </span>
                            <span className="mt-1 block truncate text-xs text-muted-foreground">
                              {project.official_domain || project.project_id}
                            </span>
                          </span>
                        </label>
                        <Badge variant="outline">{project.effective_role}</Badge>
                      </div>
                      {checked && row && (
                        <div className="mt-4 flex items-center justify-between gap-3 border-t pt-3">
                          <div>
                            <Label htmlFor={`batch-count-${project.project_id}`}>本项目文章数</Label>
                            <p className="mt-1 text-xs text-muted-foreground">1–{MAX_ARTICLES_PER_PROJECT} 篇</p>
                          </div>
                          <Input
                            id={`batch-count-${project.project_id}`}
                            type="number"
                            min={1}
                            max={MAX_ARTICLES_PER_PROJECT}
                            value={row.article_count}
                            className="w-28 text-right"
                            onChange={(event) => updateCount(project.project_id, event.target.value)}
                          />
                        </div>
                      )}
                    </div>
                  );
                })
              ) : (
                <p className="py-8 text-center text-sm text-muted-foreground">当前账号没有可访问项目。</p>
              )}
            </CardContent>
          </Card>

          <Card className="gap-0 py-0">
            <CardHeader className="border-b px-5 py-5">
              <CardTitle>2. 固定执行选项</CardTitle>
              <CardDescription>结构化配置会原样进入服务端固定工作流，不经过自然语言规划。</CardDescription>
            </CardHeader>
            <CardContent className="grid gap-5 px-5 py-5">
              <label className="flex cursor-pointer items-start gap-3 rounded-lg border p-3">
                <input
                  type="checkbox"
                  className="mt-1 size-4 accent-primary"
                  checked={skipReview}
                  onChange={(event) => setSkipReview(event.target.checked)}
                />
                <span>
                  <span className="block text-sm font-medium">跳过正文复检</span>
                  <span className="mt-1 block text-xs leading-5 text-muted-foreground">
                    只跳过固定流程中的 SEO 正文复检；交付包的最终人工确认仍然保留。
                  </span>
                </span>
              </label>
              <div className="grid gap-2">
                <Label htmlFor="batch-concurrency">并发上限</Label>
                <select
                  id="batch-concurrency"
                  className="h-11 w-full rounded-lg border border-input bg-background px-3 text-sm"
                  value={concurrencyLimit}
                  onChange={(event) => setConcurrencyLimit(Number(event.target.value))}
                >
                  {[1, 2, 3, 4, 5, 6, 8, 10].map((value) => (
                    <option key={value} value={value}>{value} 篇同时处理</option>
                  ))}
                </select>
                <p className="text-xs leading-5 text-muted-foreground">最终仍受服务端并发上限限制。</p>
              </div>
              <div className="grid gap-2">
                <Label htmlFor="batch-writing-instruction">补充写作要求（可选）</Label>
                <Textarea
                  id="batch-writing-instruction"
                  value={writingInstruction}
                  maxLength={7_000}
                  rows={6}
                  placeholder="例如：统一面向采购经理，保持技术型 B2B 语气；如果需要配置提示词，可以直接写在这里。"
                  onChange={(event) => setWritingInstruction(event.target.value)}
                />
                <p className="text-xs leading-5 text-muted-foreground">
                  这里只承载文章要求，不负责识别项目和文章数量。
                </p>
              </div>
            </CardContent>
          </Card>

          {plan && (
            <Card className="gap-0 py-0">
              <CardHeader className="border-b px-5 py-5">
                <CardTitle>文章执行概览</CardTitle>
                <CardDescription>
                  每张卡片对应一篇文章；失败或未完成时会标出实际步骤，打开工作台会定位到对应阶段。
                </CardDescription>
              </CardHeader>
              <CardContent className="px-5 py-5">
                <WorkflowArticleCards
                  plan={plan}
                  projectNames={projectNames}
                />
              </CardContent>
            </Card>
          )}
        </section>

        <aside className="grid content-start gap-4">
          <Card className="gap-0 py-0">
            <CardHeader className="border-b px-5 py-5">
              <CardTitle>提交前预览</CardTitle>
              <CardDescription>项目和数量会在提交前明确锁定。</CardDescription>
            </CardHeader>
            <CardContent className="grid gap-4 px-5 py-5">
              <div className="grid grid-cols-2 gap-3">
                <div className="rounded-lg border bg-muted/20 p-3">
                  <p className="text-2xl font-semibold">{rows.length}</p>
                  <p className="text-xs text-muted-foreground">个项目</p>
                </div>
                <div className="rounded-lg border bg-muted/20 p-3">
                  <p className="text-2xl font-semibold">{totalArticles}</p>
                  <p className="text-xs text-muted-foreground">篇文章</p>
                </div>
              </div>
              <div className="grid gap-2">
                {rows.length ? rows.map((row) => {
                  const project = projectById.get(row.project_id);
                  return (
                    <div key={row.project_id} className="flex items-center justify-between gap-3 rounded-lg border px-3 py-2 text-sm">
                      <span className="min-w-0 truncate">{project?.customer_name || row.project_id}</span>
                      <Badge variant="secondary" className="shrink-0">{row.article_count} 篇</Badge>
                    </div>
                  );
                }) : (
                  <div className="rounded-lg border border-dashed px-3 py-5 text-center text-sm text-muted-foreground">
                    还没有选择项目
                  </div>
                )}
              </div>
              <div className="rounded-lg border border-dashed bg-muted/20 px-3 py-3 text-xs leading-5 text-muted-foreground">
                选题策略：未完成任务优先 → 已发布话题补足。提交后由服务端生成可确认的固定步骤，不调用规划模型。
              </div>
              {totalArticles > MAX_TOTAL_ARTICLES && (
                <p className="text-sm text-destructive">当前总数超过 {MAX_TOTAL_ARTICLES} 篇上限。</p>
              )}
              <Button
                type="button"
                className="min-h-11"
                disabled={loading || pending !== "" || !rows.length || totalArticles > MAX_TOTAL_ARTICLES}
                onClick={() => void createPlan()}
              >
                {pending === "create" ? <Loader2 className="animate-spin" /> : <Workflow />}
                {pending === "create" ? "正在生成固定计划…" : "生成批量写作计划"}
              </Button>
            </CardContent>
          </Card>

          {plan && (
            <Card className="gap-0 py-0">
              <CardHeader className="border-b px-5 py-5">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <CardTitle className="truncate">{plan.title}</CardTitle>
                    <CardDescription className="mt-1">Revision {plan.revision} · {plan.steps.length} 个步骤</CardDescription>
                  </div>
                  <Badge variant={statusVariant(plan.status)}>{statusLabels[plan.status]}</Badge>
                </div>
              </CardHeader>
              <CardContent className="grid gap-4 px-5 py-5">
                <div className="grid gap-2">
                  {plan.project_ids.map((projectId) => (
                    <div key={projectId} className="flex items-center justify-between gap-3 rounded-lg border px-3 py-2 text-sm">
                      <span className="min-w-0 truncate">{projectById.get(projectId)?.customer_name || projectId}</span>
                      <Badge variant="outline">{selectedPlanCounts[projectId] || 0} 篇</Badge>
                    </div>
                  ))}
                </div>
                <p className="rounded-lg border border-dashed bg-muted/20 px-3 py-3 text-xs leading-5 text-muted-foreground">
                  刷新页面不会丢失该计划；计划状态和步骤均从 PostgreSQL 恢复。
                </p>
                <div className="flex flex-wrap gap-2">
                  {(plan.status === "awaiting_confirmation" || plan.status === "waiting_review") && (
                    <Button type="button" onClick={() => void changePlan("confirm")} disabled={Boolean(pending)}>
                      {pending === "confirm" ? <Loader2 className="animate-spin" /> : <Check />}
                      {plan.status === "waiting_review" ? "确认并继续" : "确认计划并排队"}
                    </Button>
                  )}
                  {(plan.status === "queued" || plan.status === "running") && (
                    <Button type="button" variant="outline" onClick={() => void changePlan("pause")} disabled={Boolean(pending)}>
                      {pending === "pause" ? <Loader2 className="animate-spin" /> : <Pause />}
                      暂停
                    </Button>
                  )}
                  {plan.status === "paused" && (
                    <Button type="button" onClick={() => void changePlan("resume")} disabled={Boolean(pending)}>
                      {pending === "resume" ? <Loader2 className="animate-spin" /> : <Play />}
                      恢复
                    </Button>
                  )}
                  {!["completed", "cancelled"].includes(plan.status) && (
                    <Button type="button" variant="destructive" onClick={() => void changePlan("cancel")} disabled={Boolean(pending)}>
                      {pending === "cancel" ? <Loader2 className="animate-spin" /> : <Square />}
                      取消
                    </Button>
                  )}
                  {plan.status === "failed" && (
                    <Button type="button" variant="outline" onClick={() => void changePlan("retry")} disabled={Boolean(pending)}>
                      {pending === "retry" ? <Loader2 className="animate-spin" /> : <RefreshCw />}
                      重试失败步骤
                    </Button>
                  )}
                </div>
                {deliveryReady > 0 && (
                  <Button type="button" variant="outline" onClick={() => void downloadDelivery()} disabled={Boolean(pending)}>
                    {pending === "download" ? <Loader2 className="animate-spin" /> : <Download />}
                    下载成功的 {deliveryReady} 篇 ZIP
                  </Button>
                )}
                <details className="rounded-lg border bg-muted/10">
                  <summary className="cursor-pointer px-3 py-3 text-sm font-medium">查看固定步骤</summary>
                  <ol className="grid gap-2 border-t px-3 py-3">
                    {plan.steps.slice(0, 120).map((step) => (
                      <li key={step.step_id} className="flex items-center gap-2 text-xs">
                        <span className="flex size-6 shrink-0 items-center justify-center rounded-full bg-muted font-semibold">{step.sequence}</span>
                        <span className="min-w-0 flex-1 truncate">
                          {WORKFLOW_STEP_LABELS[step.action_kind] || step.action_kind}
                          <span className="ml-1 text-muted-foreground">· {step.project_id}{step.article_task_id ? ` · ${step.article_task_id}` : ""}</span>
                        </span>
                        <Badge variant={step.status === "failed" ? "destructive" : step.status === "succeeded" ? "secondary" : "outline"}>
                          {step.status}
                        </Badge>
                      </li>
                    ))}
                    {plan.steps.length > 120 && <li className="text-xs text-muted-foreground">其余步骤已省略，请打开对话助手查看完整计划。</li>}
                  </ol>
                </details>
                <div className="flex flex-wrap items-center justify-between gap-2 border-t pt-3">
                  <Button type="button" variant="ghost" size="sm" onClick={resetPlan} disabled={Boolean(pending)}>
                    <RefreshCw />新建批量任务
                  </Button>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    nativeButton={false}
                    render={<Link href="/assistant" />}
                  >
                    <ExternalLink />打开对话助手
                  </Button>
                </div>
              </CardContent>
            </Card>
          )}
        </aside>
      </div>
    </main>
  );
}
