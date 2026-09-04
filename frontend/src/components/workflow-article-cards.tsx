"use client";

import { ChevronDown, ExternalLink } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { formatProjectDate, parseProjectDate } from "@/lib/project-date";
import {
  isReadyDeliveryStep,
  stepSummaryText,
  WORKFLOW_STEP_LABELS,
} from "@/lib/workflow-steps";
import type {
  KnowledgeCoverageCheckRecord,
  WorkflowAssistantPlan,
  WorkflowAssistantStep,
} from "@/types";

type WorkbenchStep = "setup" | "outline" | "draft" | "review" | "delivery";

const WORKBENCH_STEP_LABELS: Record<WorkbenchStep, string> = {
  setup: "内容准备",
  outline: "大纲",
  draft: "初稿",
  review: "审阅",
  delivery: "图片与交付",
};

export type WorkflowArticleCardStatus =
  | "completed"
  | "failed"
  | "cancelled"
  | "waiting_review"
  | "running"
  | "pending"
  | "skipped";

export type WorkflowArticleCardMetrics = {
  finalAiRate: number | null;
  knowledgeCoverageRate: number | null;
  knowledgeCoverageStatus: KnowledgeCoverageCheckRecord["status"];
};

export type WorkflowArticleCard = {
  key: string;
  projectId: string;
  taskId: string | null;
  title: string;
  status: WorkflowArticleCardStatus;
  progress: number;
  total: number;
  updatedAt: string | null;
  packageReady: boolean;
  articleReady: boolean;
  errorCode: string | null;
  errorMessage: string | null;
  focusStepSequence: number | null;
  focusStepLabel: string | null;
  focusStepStatus: WorkflowAssistantStep["status"] | null;
  workbenchStep: WorkbenchStep;
  finalAiRate: number | null;
  knowledgeCoverageRate: number | null;
  knowledgeCoverageStatus: KnowledgeCoverageCheckRecord["status"];
  steps: WorkflowAssistantStep[];
};

function workflowArticleLaneKey(step: WorkflowAssistantStep): string {
  const taskId = step.article_task_id?.trim();
  if (taskId) return `${step.project_id}:task:${taskId}`;
  const createTaskStepId = stepSummaryText(step.input_summary, "create_task_step_id");
  if (createTaskStepId) return `${step.project_id}:create:${createTaskStepId}`;
  if (step.action_kind === "create_task") return `${step.project_id}:create:${step.step_id}`;
  return `${step.project_id}:step:${step.step_id}`;
}

function workflowArticleCardStatus(
  steps: WorkflowAssistantStep[],
  packageStep: WorkflowAssistantStep | undefined,
): WorkflowArticleCardStatus {
  if (packageStep && isReadyDeliveryStep(packageStep)) return "completed";
  const statuses = steps.map((step) => step.status);
  if (statuses.includes("failed")) return "failed";
  if (statuses.includes("cancelled")) return "cancelled";
  if (statuses.includes("waiting_review")) return "waiting_review";
  if (statuses.includes("running") || statuses.includes("waiting_job")) return "running";
  if (statuses.includes("pending")) return "pending";
  if (
    packageStep?.status === "skipped"
    && statuses.length
    && statuses.every((status) => status === "succeeded" || status === "skipped")
  ) return "skipped";
  if (statuses.length && statuses.every((status) => status === "skipped")) return "skipped";
  if (
    statuses.length
    && statuses.every((status) => status === "succeeded" || status === "skipped")
  ) return "completed";
  return "running";
}

const workflowArticleCardStatusLabels: Record<WorkflowArticleCardStatus, string> = {
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
  waiting_review: "待人工处理",
  running: "执行中",
  pending: "待执行",
  skipped: "已跳过",
};

const workflowArticleCardStatusVariants: Record<
  WorkflowArticleCardStatus,
  "default" | "outline" | "secondary" | "destructive"
> = {
  completed: "secondary",
  failed: "destructive",
  cancelled: "outline",
  waiting_review: "outline",
  running: "default",
  pending: "default",
  skipped: "outline",
};

const workflowStepStatusLabels: Record<WorkflowAssistantStep["status"], string> = {
  pending: "待执行",
  running: "执行中",
  waiting_job: "等待任务",
  waiting_review: "待人工处理",
  succeeded: "已完成",
  failed: "失败",
  skipped: "已跳过",
  cancelled: "已取消",
};

const workflowStepStatusVariants: Record<
  WorkflowAssistantStep["status"],
  "default" | "outline" | "secondary" | "destructive"
> = {
  pending: "outline",
  running: "default",
  waiting_job: "default",
  waiting_review: "outline",
  succeeded: "secondary",
  failed: "destructive",
  skipped: "outline",
  cancelled: "outline",
};

function workflowArticleCardTitle(
  steps: WorkflowAssistantStep[],
  taskId: string | null,
  index: number,
  fallbackTitle: string,
): string {
  for (const step of steps) {
    const value = stepSummaryText(
      step.output_summary,
      "selected_title",
      "title",
    ) || stepSummaryText(step.input_summary, "title", "topic", "primary_keyword");
    if (value) return value.slice(0, 180);
  }
  if (fallbackTitle) return fallbackTitle.slice(0, 180);
  return taskId ? `文章 ${taskId}` : `文章 ${index + 1}`;
}

function workflowArticleCardUpdatedAt(
  steps: WorkflowAssistantStep[],
): string | null {
  const timestamps = steps
    .map((step) => step.updated_at)
    .filter((value): value is string => Boolean(value));
  if (!timestamps.length) return null;
  timestamps.sort((left, right) => {
    const leftTime = parseProjectDate(left)?.getTime() ?? 0;
    const rightTime = parseProjectDate(right)?.getTime() ?? 0;
    return leftTime - rightTime;
  });
  return timestamps[timestamps.length - 1];
}

function workbenchStepForAction(actionKind: string): WorkbenchStep {
  if (["create_task", "generate_titles", "select_title", "generate_products", "confirm_products"].includes(actionKind)) {
    return "setup";
  }
  if (["generate_outline", "start_research"].includes(actionKind)) return "outline";
  if (actionKind === "generate_article") return "draft";
  if (["review", "humanize", "restore_links"].includes(actionKind)) return "review";
  return "delivery";
}

function focusStepForCard(
  steps: WorkflowAssistantStep[],
): WorkflowAssistantStep | undefined {
  return steps.find((step) => step.status === "failed" || step.status === "cancelled")
    || steps.find((step) => ["running", "waiting_job", "waiting_review"].includes(step.status))
    || steps.find((step) => step.status === "pending")
    || steps[steps.length - 1];
}

export function buildWorkflowArticleCards(
  plan: WorkflowAssistantPlan,
  taskTopics: ReadonlyMap<string, string> = new Map(),
  taskMetrics: ReadonlyMap<string, WorkflowArticleCardMetrics> = new Map(),
): WorkflowArticleCard[] {
  const packageSteps = plan.steps.filter((step) => step.action_kind === "package_delivery");
  const sourceSteps = packageSteps.length
    ? packageSteps
    : plan.steps.filter((step) => step.action_kind === "create_task" || Boolean(step.article_task_id));
  const seenKeys = new Set<string>();
  const cards: WorkflowArticleCard[] = [];

  sourceSteps.forEach((sourceStep, index) => {
    const key = workflowArticleLaneKey(sourceStep);
    if (seenKeys.has(key)) return;
    seenKeys.add(key);
    const sourceTaskId = sourceStep.article_task_id?.trim() || null;
    const createTaskStepId = stepSummaryText(sourceStep.input_summary, "create_task_step_id");
    const relatedSteps = plan.steps.filter((candidate) => {
      if (candidate.project_id !== sourceStep.project_id) return false;
      if (sourceTaskId && candidate.article_task_id === sourceTaskId) return true;
      if (createTaskStepId) {
        return candidate.step_id === createTaskStepId
          || stepSummaryText(candidate.input_summary, "create_task_step_id") === createTaskStepId;
      }
      return workflowArticleLaneKey(candidate) === key;
    });
    const steps = relatedSteps.length ? relatedSteps : [sourceStep];
    const taskId = sourceTaskId
      || steps.find((step) => step.article_task_id)?.article_task_id?.trim()
      || null;
    const metrics = taskId
      ? taskMetrics.get(`${sourceStep.project_id}:${taskId}`)
      : undefined;
    const taskTopic = taskId
      ? taskTopics.get(`${sourceStep.project_id}:${taskId}`) || ""
      : "";
    const packageStep = sourceStep.action_kind === "package_delivery"
      ? sourceStep
      : packageSteps.find((step) => {
        if (taskId && step.article_task_id === taskId) return true;
        return step.project_id === sourceStep.project_id
          && createTaskStepId
          && stepSummaryText(step.input_summary, "create_task_step_id") === createTaskStepId;
      });
    const articleStep = steps.find((step) => step.action_kind === "generate_article");
    const hasDownstreamArticleResult = steps.some(
      (step) => [
        "humanize",
        "restore_links",
        "prepare_images",
        "export_docx",
        "generate_tdk",
        "package_delivery",
      ].includes(step.action_kind) && step.status === "succeeded",
    );
    const articleReady = articleStep
      ? articleStep.status === "succeeded"
        && articleStep.output_summary.article_ready !== false
      : hasDownstreamArticleResult;
    const articleResultMissing = articleStep?.status === "succeeded"
      && articleStep.output_summary.article_ready === false;
    const status = articleResultMissing
      ? "failed"
      : workflowArticleCardStatus(steps, packageStep);
    const errorStep = steps.find((step) => step.status === "failed" || step.status === "cancelled");
    const focusStep = focusStepForCard(steps);
    cards.push({
      key,
      projectId: sourceStep.project_id,
      taskId,
      title: workflowArticleCardTitle(steps, taskId, index, taskTopic),
      status,
      progress: steps.filter((step) => step.status === "succeeded" || step.status === "skipped").length,
      total: steps.length,
      updatedAt: workflowArticleCardUpdatedAt(steps),
      packageReady: Boolean(packageStep && isReadyDeliveryStep(packageStep)),
      articleReady,
      errorCode: articleResultMissing
        ? "article_result_missing"
        : errorStep?.standardized_error_code || null,
      errorMessage: articleResultMissing
        ? "正文结果不可用"
        : stepSummaryText(errorStep?.output_summary || {}, "error_message", "message") || null,
      focusStepSequence: focusStep?.sequence ?? null,
      focusStepLabel: focusStep ? WORKFLOW_STEP_LABELS[focusStep.action_kind] || focusStep.action_kind : null,
      focusStepStatus: focusStep?.status ?? null,
      workbenchStep: workbenchStepForAction(focusStep?.action_kind || "create_task"),
      finalAiRate: metrics?.finalAiRate ?? null,
      knowledgeCoverageRate: metrics?.knowledgeCoverageRate ?? null,
      knowledgeCoverageStatus: metrics?.knowledgeCoverageStatus ?? "not_checked",
      steps,
    });
  });

  return cards;
}

function formatWorkflowArticleCardTime(value: string | null): string {
  if (!value) return "—";
  return formatProjectDate(value, {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }) || "—";
}

function formatAiRate(value: number | null, taskId: string | null): string {
  if (!taskId) return "待生成";
  if (value === null) return "待检测";
  return `${Number.isInteger(value) ? value : value.toFixed(1)}%`;
}

function formatKnowledgeCoverage(
  value: number | null,
  status: KnowledgeCoverageCheckRecord["status"],
  taskId: string | null,
): string {
  if (!taskId) return "待生成";
  if (value !== null) return `${Math.round(value * 100)}%`;
  if (status === "stale") return "需复检";
  if (status === "unavailable") return "不可用";
  return "待检查";
}

export function WorkflowArticleCards({
  plan,
  taskTopics = new Map(),
  taskMetrics = new Map(),
  projectNames = new Map(),
}: {
  plan: WorkflowAssistantPlan;
  taskTopics?: ReadonlyMap<string, string>;
  taskMetrics?: ReadonlyMap<string, WorkflowArticleCardMetrics>;
  projectNames?: ReadonlyMap<string, string>;
}) {
  const cards = buildWorkflowArticleCards(plan, taskTopics, taskMetrics);
  const [expandedCardKey, setExpandedCardKey] = useState<string | null>(null);
  if (!cards.length) {
    return (
      <div className="rounded-lg border border-dashed px-4 py-10 text-center text-sm text-muted-foreground">
        当前计划尚未形成可展示的文章链。
      </div>
    );
  }

  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
      {cards.map((card) => {
        const workbenchHref = card.taskId
          ? `/projects/${encodeURIComponent(card.projectId)}/articles/${encodeURIComponent(card.taskId)}?step=${card.workbenchStep}`
          : null;
        const timeLabel = card.status === "completed" ? "完成时间" : "最近更新";
        const focusVisible = card.status === "failed"
          || !["succeeded", "skipped"].includes(card.focusStepStatus || "");
        const projectLabel = projectNames.get(card.projectId) || card.projectId;
        const isExpanded = expandedCardKey === card.key;
        const detailsId = `workflow-article-card-details-${card.key.replace(/[^a-zA-Z0-9_-]/g, "-")}`;
        const progressPercent = card.total ? Math.round((card.progress / card.total) * 100) : 0;
        return (
          <article key={card.key} className="rounded-xl border bg-background p-3 shadow-xs">
            <div className="flex items-start gap-2">
              {workbenchHref ? (
                <Link
                  href={workbenchHref}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex size-8 shrink-0 items-center justify-center rounded-md border text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
                  aria-label={`在新标签页打开${card.title}的文章工作台，定位到${card.focusStepLabel || "当前步骤"}`}
                >
                  <ExternalLink className="size-4" />
                  <span className="sr-only">在新标签页打开文章工作台</span>
                </Link>
              ) : (
                <span className="inline-flex size-8 shrink-0 items-center justify-center rounded-md border text-muted-foreground/40" title="文章任务创建后可打开工作台">
                  <ExternalLink className="size-4" />
                  <span className="sr-only">文章任务尚未创建</span>
                </span>
              )}
              <button
                type="button"
                className="group min-w-0 flex-1 text-left"
                aria-expanded={isExpanded}
                aria-controls={detailsId}
                title="点击查看该文章的处理步骤"
                onClick={() => setExpandedCardKey(isExpanded ? null : card.key)}
              >
                <span className="flex items-start justify-between gap-2">
                  <span className="min-w-0">
                    <span className="block truncate text-sm font-semibold" title={card.title}>{card.title}</span>
                    <span className="mt-1 block truncate text-xs text-muted-foreground" title={card.projectId}>
                      {projectLabel}{card.taskId ? ` · ${card.taskId}` : ""}
                    </span>
                  </span>
                  <ChevronDown className={`mt-0.5 size-4 shrink-0 text-muted-foreground transition-transform ${isExpanded ? "rotate-180" : ""}`} />
                </span>
              </button>
            </div>
            <div className="mt-3 flex flex-wrap items-center gap-2">
              <Badge variant={workflowArticleCardStatusVariants[card.status]}>
                {workflowArticleCardStatusLabels[card.status]}
              </Badge>
              <span className="text-xs text-muted-foreground">{card.progress}/{card.total} 步已结束</span>
            </div>
            <div
              className="mt-2 h-1.5 overflow-hidden rounded-full bg-muted"
              role="progressbar"
              aria-label={`${card.title}执行进度`}
              aria-valuemin={0}
              aria-valuemax={card.total}
              aria-valuenow={card.progress}
            >
              <div
                className={`h-full rounded-full transition-[width] ${card.status === "failed" ? "bg-destructive" : "bg-primary"}`}
                style={{ width: `${progressPercent}%` }}
              />
            </div>
            {focusVisible && card.focusStepLabel && (
              <p className={`mt-3 truncate text-xs ${card.status === "failed" ? "text-destructive" : "text-muted-foreground"}`}>
                {card.status === "failed" ? "失败于" : card.focusStepStatus === "waiting_review" ? "待人工处理" : "当前步骤"}
                ：第 {card.focusStepSequence} 步 · {card.focusStepLabel}
              </p>
            )}
            <dl className="mt-3 grid grid-cols-2 gap-x-3 gap-y-2 text-xs">
              <div className="min-w-0">
                <dt className="text-muted-foreground">正文</dt>
                <dd className={`mt-0.5 truncate font-medium ${card.articleReady ? "text-foreground" : "text-muted-foreground"}`}>
                  {card.articleReady ? "已生成" : "未生成"}
                </dd>
              </div>
              <div className="min-w-0">
                <dt className="text-muted-foreground">交付包</dt>
                <dd className="mt-0.5 truncate font-medium">{card.packageReady ? "已生成" : "未生成"}</dd>
              </div>
              <div className="min-w-0">
                <dt className="text-muted-foreground">{timeLabel}</dt>
                <dd className="mt-0.5 truncate font-medium">{formatWorkflowArticleCardTime(card.updatedAt)}</dd>
              </div>
              <div className="min-w-0">
                <dt className="text-muted-foreground">入口</dt>
                <dd className="mt-0.5 truncate font-medium">{WORKBENCH_STEP_LABELS[card.workbenchStep]}</dd>
              </div>
              <div className="min-w-0">
                <dt className="text-muted-foreground">最终 AI 率</dt>
                <dd className="mt-0.5 truncate font-medium">
                  {formatAiRate(card.finalAiRate, card.taskId)}
                </dd>
              </div>
              <div className="min-w-0">
                <dt className="text-muted-foreground">知识库引用率</dt>
                <dd className="mt-0.5 truncate font-medium">
                  {formatKnowledgeCoverage(
                    card.knowledgeCoverageRate,
                    card.knowledgeCoverageStatus,
                    card.taskId,
                  )}
                </dd>
              </div>
            </dl>
            {card.errorCode && (
              <p className="mt-3 truncate text-xs text-destructive" title={card.errorMessage || card.errorCode}>
                错误：{card.errorMessage || card.errorCode}
              </p>
            )}
            {isExpanded && (
              <div id={detailsId} className="mt-4 border-t pt-3">
                <p className="mb-2 text-xs font-medium text-muted-foreground">处理步骤</p>
                <ol className="max-h-72 space-y-2 overflow-y-auto pr-1">
                  {card.steps.map((step) => {
                    const detail = stepSummaryText(
                      step.output_summary,
                      "error_message",
                      "message",
                      "selected_title",
                      "title",
                      "topic",
                    ) || stepSummaryText(step.input_summary, "title", "topic", "primary_keyword");
                    return (
                      <li key={step.step_id} className="rounded-lg border bg-muted/20 px-2.5 py-2">
                        <div className="flex items-center justify-between gap-2">
                          <div className="flex min-w-0 items-center gap-2">
                            <span className="inline-flex size-5 shrink-0 items-center justify-center rounded-full bg-muted text-[10px] font-semibold text-muted-foreground">
                              {step.sequence}
                            </span>
                            <span className="min-w-0 truncate text-xs font-medium" title={step.action_kind}>
                              {WORKFLOW_STEP_LABELS[step.action_kind] || step.action_kind}
                            </span>
                          </div>
                          <Badge variant={workflowStepStatusVariants[step.status]}>
                            {workflowStepStatusLabels[step.status]}
                          </Badge>
                        </div>
                        {(detail || step.standardized_error_code) && (
                          <p className={`mt-1 truncate text-[11px] ${step.status === "failed" || step.standardized_error_code ? "text-destructive" : "text-muted-foreground"}`} title={detail || step.standardized_error_code || ""}>
                            {detail || step.standardized_error_code}
                          </p>
                        )}
                      </li>
                    );
                  })}
                </ol>
              </div>
            )}
          </article>
        );
      })}
    </div>
  );
}
