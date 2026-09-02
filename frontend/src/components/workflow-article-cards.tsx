import { ExternalLink } from "lucide-react";
import Link from "next/link";

import { Badge } from "@/components/ui/badge";
import { formatProjectDate, parseProjectDate } from "@/lib/project-date";
import type { WorkflowAssistantPlan, WorkflowAssistantStep } from "@/types";

export const WORKFLOW_STEP_LABELS: Record<string, string> = {
  create_task: "创建文章任务",
  generate_titles: "生成标题候选",
  select_title: "确认标题",
  generate_products: "生成产品候选",
  confirm_products: "确认产品",
  generate_outline: "生成大纲",
  start_research: "知识库研究",
  generate_article: "生成正文",
  review: "正文复检",
  humanize: "降 AI / 人化",
  restore_links: "恢复并校验链接",
  prepare_images: "准备图片",
  export_docx: "导出 Word",
  generate_tdk: "生成 TDK",
  package_delivery: "生成交付包",
};

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
};

export function stepSummaryText(
  summary: Record<string, unknown>,
  ...keys: string[]
): string {
  for (const key of keys) {
    const value = summary[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return "";
}

export function isReadyDeliveryStep(step: WorkflowAssistantStep): boolean {
  return step.status === "succeeded"
    && Boolean(step.article_task_id)
    && step.output_summary.artifact_kind === "delivery_package"
    && Boolean(String(step.output_summary.asset_id || "").trim());
}

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

export function WorkflowArticleCards({
  plan,
  taskTopics = new Map(),
  projectNames = new Map(),
}: {
  plan: WorkflowAssistantPlan;
  taskTopics?: ReadonlyMap<string, string>;
  projectNames?: ReadonlyMap<string, string>;
}) {
  const cards = buildWorkflowArticleCards(plan, taskTopics);
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
        return (
          <article key={card.key} className="rounded-xl border bg-background p-3 shadow-xs">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <p className="truncate text-sm font-semibold" title={card.title}>{card.title}</p>
                <p className="mt-1 truncate text-xs text-muted-foreground" title={card.projectId}>
                  {projectLabel}{card.taskId ? ` · ${card.taskId}` : ""}
                </p>
              </div>
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
            </div>
            <div className="mt-3 flex flex-wrap items-center gap-2">
              <Badge variant={workflowArticleCardStatusVariants[card.status]}>
                {workflowArticleCardStatusLabels[card.status]}
              </Badge>
              <span className="text-xs text-muted-foreground">{card.progress}/{card.total} 步已结束</span>
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
            </dl>
            {card.errorCode && (
              <p className="mt-3 truncate text-xs text-destructive" title={card.errorMessage || card.errorCode}>
                错误：{card.errorMessage || card.errorCode}
              </p>
            )}
          </article>
        );
      })}
    </div>
  );
}
