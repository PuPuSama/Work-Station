"use client";

/* eslint-disable react-hooks/set-state-in-effect */

import {
  AlertCircle,
  Check,
  ChevronDown,
  CircleDot,
  Download,
  ExternalLink,
  Loader2,
  MessageSquareText,
  Pause,
  Play,
  Plus,
  RotateCcw,
  Send,
  Sparkles,
  Square,
  Workflow,
} from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

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
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Textarea } from "@/components/ui/textarea";
import { WorkflowAssistantAttachments } from "@/components/workflow-assistant-attachments";
import { ApiError, apiFileUrl, apiGet, apiPost } from "@/lib/api";
import { triggerBrowserDownload } from "@/lib/browser-download";
import {
  gapFillWorkflowAssistantPlan,
  getResearchRun,
} from "@/lib/research-api";
import type {
  AccessibleProject,
  AuthStatus,
  ResearchRunDetail,
  TaskRecord,
  WorkflowAssistantConversation,
  WorkflowAssistantConversationList,
  WorkflowAssistantDispatch,
  WorkflowAssistantAttentionCount,
  WorkflowAssistantAttentionList,
  WorkflowAssistantBatchDownload,
  WorkflowAssistantGapFillResponse,
  WorkflowAssistantPlan,
  WorkflowAssistantPlanSummary,
  WorkflowAssistantStep,
} from "@/types";

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

// Planning can legitimately exceed the shared four-minute API timeout when
// the selected model performs deep reasoning over several projects. Keep a
// finite browser guard, but do not abort a healthy server-side planning call.
const WORKFLOW_ASSISTANT_PLANNING_TIMEOUT_MS = 30 * 60 * 1000;
const WORKFLOW_ASSISTANT_DISPATCH_RECOVERY_INTERVAL_MS = 2000;

function localId(prefix: string) {
  const random = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`;
  return `${prefix}-${random}`.replace(/[^A-Za-z0-9._:-]/g, "-");
}

function isDispatchConnectionFailure(error: unknown): boolean {
  if (error instanceof ApiError) return error.status >= 500;
  const message = error instanceof Error ? error.message : String(error || "");
  return /socket hang up|network|fetch failed|timed out|timeout|connection/i.test(message);
}

async function recoverDispatchResult(
  conversationId: string,
  content: string,
  previousPlanId: string | null,
  dispatchId: string | null = null,
  idempotencyKey: string | null = null,
): Promise<{
  conversation: WorkflowAssistantConversation;
  plan: WorkflowAssistantPlan | null;
  error?: string;
} | null> {
  const deadline = Date.now() + WORKFLOW_ASSISTANT_PLANNING_TIMEOUT_MS;
  while (Date.now() < deadline) {
    try {
      const dispatchUrl = dispatchId
        ? `/api/workflow-assistant/conversations/${encodeURIComponent(conversationId)}/dispatches/${encodeURIComponent(dispatchId)}`
        : idempotencyKey
          ? `/api/workflow-assistant/conversations/${encodeURIComponent(conversationId)}/dispatches?idempotency_key=${encodeURIComponent(idempotencyKey)}`
          : null;
      const [dispatch, conversation, fallbackPlan] = await Promise.all([
        dispatchUrl
          ? apiGet<WorkflowAssistantDispatch>(dispatchUrl).catch(() => null)
          : Promise.resolve<WorkflowAssistantDispatch | null>(null),
        apiGet<WorkflowAssistantConversation>(
          `/api/workflow-assistant/conversations/${encodeURIComponent(conversationId)}`,
        ),
        apiGet<WorkflowAssistantPlan | null>(
          `/api/workflow-assistant/conversations/${encodeURIComponent(conversationId)}/latest-plan`,
        ).catch(() => null),
      ]);
      if (dispatch?.dispatch_status === "failed") {
        return {
          conversation,
          plan: dispatch.plan,
          error: dispatch.dispatch_error_code
            ? `计划生成失败（${dispatch.dispatch_error_code}），可以重新发送请求重试。`
            : "计划生成失败，可以重新发送请求重试。",
        };
      }
      const latestPlan = dispatch?.plan || fallbackPlan || null;
      const planMatchesRequest = Boolean(
        latestPlan
        && latestPlan.natural_language_request.trim() === content
        && (latestPlan.plan_id !== previousPlanId || previousPlanId === null),
      );
      const lastUserIndex = [...conversation.messages]
        .map((message, index) => ({ message, index }))
        .reverse()
        .find(({ message }) => message.role === "user" && message.content.trim() === content)
        ?.index;
      const hasAssistantReply = lastUserIndex !== undefined
        && conversation.messages.slice(lastUserIndex + 1).some(
          (message) => message.role === "assistant",
        );
      if (dispatch?.dispatch_status === "succeeded" || planMatchesRequest || hasAssistantReply) {
        return { conversation, plan: planMatchesRequest ? latestPlan : dispatch?.plan || null };
      }
    } catch {
      // A proxy reconnect can briefly make both status reads fail. Keep the
      // request in the waiting state and try again without showing a false
      // planning error to the user.
    }
    await new Promise<void>((resolve) => {
      window.setTimeout(resolve, WORKFLOW_ASSISTANT_DISPATCH_RECOVERY_INTERVAL_MS);
    });
  }
  return null;
}

function researchThreadId(step: WorkflowAssistantStep): string {
  const inputThread = step.input_summary.research_thread_id;
  const outputThread = step.output_summary.research_thread_id;
  return typeof inputThread === "string" && inputThread.trim()
    ? inputThread.trim()
    : typeof outputThread === "string" && outputThread.trim()
      ? outputThread.trim()
      : "";
}

function researchGapReasons(detail: ResearchRunDetail): string[] {
  const reasons = detail.gap_fill_attempts
    .map((attempt) => attempt.reason.trim())
    .filter(Boolean);
  return [...new Set(reasons)].slice(-6);
}

function gapFillReviewCandidates(detail: ResearchRunDetail) {
  return detail.review_candidates.filter(
    (candidate) => candidate.needs_review
      && candidate.evidence.same_site === true
      && (candidate.evidence.channel === "official_site"
        || candidate.evidence.channel === "tavily_discovery"),
  );
}

function messageText(
  error: unknown,
  previousPlan: WorkflowAssistantPlan | null = null,
) {
  if (error instanceof ApiError && error.status === 409 && error.detail && typeof error.detail === "object") {
    const detail = error.detail as {
      message?: unknown;
      current_revision?: unknown;
      current_steps?: unknown;
      current_steps_truncated?: unknown;
    };
    const message = typeof detail.message === "string" ? detail.message : error.message;
    const revision = typeof detail.current_revision === "number" ? ` 当前 Revision ${detail.current_revision}。` : "";
    const steps = Array.isArray(detail.current_steps) ? detail.current_steps : [];
    const previousById = new Map(
      (previousPlan?.steps || []).map((step) => [step.step_id, step]),
    );
    const currentIds = new Set<string>();
    const differences = steps.flatMap((step) => {
      if (!step || typeof step !== "object") return [];
      const item = step as {
        step_id?: unknown;
        sequence?: unknown;
        action_kind?: unknown;
        status?: unknown;
      };
      const stepId = String(item.step_id ?? "");
      if (stepId) currentIds.add(stepId);
      const previous = previousById.get(stepId);
      if (!previous) {
        return [`新增 ${String(item.sequence ?? "-")}:${String(item.action_kind ?? "step")}`];
      }
      const currentStatus = String(item.status ?? "unknown");
      if (previous.status !== currentStatus) {
        return [`${String(item.sequence ?? previous.sequence)}:${String(item.action_kind ?? previous.action_kind)} ${previous.status}→${currentStatus}`];
      }
      return [];
    });
    for (const previous of previousPlan?.steps || []) {
      if (!currentIds.has(previous.step_id)) {
        differences.push(`移除 ${previous.sequence}:${previous.action_kind}`);
      }
    }
    const differenceSummary = differences.length
      ? ` 差异：${differences.slice(0, 8).join("、")}${differences.length > 8 ? "……" : ""}`
      : "";
    const stepSummary = steps
      .slice(0, 8)
      .map((step) => {
        if (!step || typeof step !== "object") return "";
        const item = step as { sequence?: unknown; action_kind?: unknown; status?: unknown };
        return `${String(item.sequence ?? "-")}:${String(item.action_kind ?? "step")}=${String(item.status ?? "unknown")}`;
      })
      .filter(Boolean)
      .join("、");
    const visibleSteps = stepSummary ? ` 当前步骤：${stepSummary}${steps.length > 8 ? "……" : ""}` : "";
    const truncated = detail.current_steps_truncated === true ? "（仅显示前 100 个步骤）" : "";
    return `${message}${revision}${differenceSummary || visibleSteps}${truncated} 请刷新计划后再决定是否重试。`;
  }
  return error instanceof Error ? error.message : "助手请求失败。";
}

function MessageContent({ content }: { content: string }) {
  return (
    <>
      {content.split(/(https?:\/\/[^\s]+)/g).map((part, index) => (
        /^https?:\/\//.test(part) ? (
          <a
            key={`${part}-${index}`}
            href={part}
            target="_blank"
            rel="noreferrer"
            className="break-all underline underline-offset-2"
          >
            {part}
          </a>
        ) : (
          part
        )
      ))}
    </>
  );
}

function statusVariant(status: WorkflowAssistantPlan["status"]): "default" | "outline" | "secondary" | "destructive" {
  if (status === "failed") return "destructive";
  if (status === "awaiting_confirmation" || status === "waiting_review") return "outline";
  if (status === "completed") return "secondary";
  return "default";
}

function isReadyDeliveryStep(step: WorkflowAssistantStep): boolean {
  return step.status === "succeeded"
    && Boolean(step.article_task_id)
    && step.output_summary.artifact_kind === "delivery_package"
    && Boolean(String(step.output_summary.asset_id || "").trim());
}

type WorkflowArticleCardStatus =
  | "completed"
  | "failed"
  | "cancelled"
  | "waiting_review"
  | "running"
  | "pending"
  | "skipped";

type WorkflowArticleCard = {
  key: string;
  projectId: string;
  taskId: string | null;
  title: string;
  status: WorkflowArticleCardStatus;
  progress: number;
  total: number;
  updatedAt: string | null;
  packageReady: boolean;
  errorCode: string | null;
};

function stepSummaryText(
  summary: Record<string, unknown>,
  ...keys: string[]
): string {
  for (const key of keys) {
    const value = summary[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return "";
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
  timestamps.sort();
  return timestamps[timestamps.length - 1];
}

function buildWorkflowArticleCards(
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
    const status = workflowArticleCardStatus(steps, packageStep);
    const errorStep = steps.find((step) => step.status === "failed" || step.status === "cancelled");
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
      errorCode: errorStep?.standardized_error_code || null,
    });
  });

  return cards;
}

function formatWorkflowArticleCardTime(value: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

type ScopedTask = TaskRecord & { project_id: string };

type AssistantRequestPhase = "idle" | "sending" | "waiting_reply" | "refreshing";
type PendingUserMessage = { conversationId: string; content: string };

export function WorkflowAssistantWorkspace() {
  const [projects, setProjects] = useState<AccessibleProject[]>([]);
  const [tasks, setTasks] = useState<ScopedTask[]>([]);
  const [conversations, setConversations] = useState<WorkflowAssistantConversation[]>([]);
  const [selectedConversation, setSelectedConversation] = useState<WorkflowAssistantConversation | null>(null);
  const [selectedProjectIds, setSelectedProjectIds] = useState<string[]>([]);
  const [selectedTaskIds, setSelectedTaskIds] = useState<string[]>([]);
  const [plan, setPlan] = useState<WorkflowAssistantPlan | null>(null);
  const [draft, setDraft] = useState("");
  const [revisionDraft, setRevisionDraft] = useState("");
  const [loading, setLoading] = useState(true);
  const [pending, setPending] = useState(false);
  const [requestPhase, setRequestPhase] = useState<AssistantRequestPhase>("idle");
  const [pendingUserMessage, setPendingUserMessage] = useState<PendingUserMessage | null>(null);
  const [planPreviewOpen, setPlanPreviewOpen] = useState(false);
  const [planDetailsOpen, setPlanDetailsOpen] = useState(false);
  const [revisionPending, setRevisionPending] = useState(false);
  const [batchArtifactPending, setBatchArtifactPending] = useState("");
  const [attentionCount, setAttentionCount] = useState(0);
  const [attentionPlans, setAttentionPlans] = useState<WorkflowAssistantPlanSummary[]>([]);
  const [attentionPlanPendingId, setAttentionPlanPendingId] = useState<string | null>(null);

  // Debounce refresh to prevent request storms during SSE history replay
  const refreshTimeoutRef = useRef<number | null>(null);
  const planRefreshTimeoutRef = useRef<number | null>(null);
  const [attachmentsEnabled, setAttachmentsEnabled] = useState(false);
  const [projectChangesEnabled, setProjectChangesEnabled] = useState(false);
  const [gapFillEnabled, setGapFillEnabled] = useState(false);
  const [error, setError] = useState("");
  const [timeline, setTimeline] = useState<string[]>([]);
  const [researchDetails, setResearchDetails] = useState<Record<string, ResearchRunDetail | null>>({});
  const [gapFillSelections, setGapFillSelections] = useState<Record<string, string[]>>({});
  const [gapFillPending, setGapFillPending] = useState<string | null>(null);
  const [gapFillQueueStatus, setGapFillQueueStatus] = useState<Record<string, string>>({});
  const [timelineExpanded, setTimelineExpanded] = useState(false);
  const gapFillRequestIdsRef = useRef<Record<string, string>>({});
  const eventSourceRef = useRef<EventSource | null>(null);
  const activeConversationIdRef = useRef<string | null>(null);
  const activePlanIdRef = useRef<string | null>(null);
  const loadedConversationRef = useRef<string | null>(null);
  const messageScrollAreaRef = useRef<HTMLDivElement | null>(null);
  const lastEventSequenceRef = useRef(0);
  const reconnectTimerRef = useRef<number | null>(null);
  const reconnectAttemptRef = useRef(0);
  const pendingMessageDispatchRef = useRef<{
    conversationId: string;
    content: string;
    scopeKey: string;
    requestId: string;
    idempotencyKey: string;
  } | null>(null);

  const refreshAttention = useCallback(async () => {
    const [countResponse, listResponse] = await Promise.all([
      apiGet<WorkflowAssistantAttentionCount>(
        "/api/workflow-assistant/attention-count",
      ),
      apiGet<WorkflowAssistantAttentionList>(
        "/api/workflow-assistant/attention",
      ),
    ]);
    setAttentionCount(countResponse.count);
    setAttentionPlans(listResponse.plans);
  }, []);

  const debouncedRefreshAttention = useCallback(() => {
    if (refreshTimeoutRef.current !== null) {
      window.clearTimeout(refreshTimeoutRef.current);
    }
    refreshTimeoutRef.current = window.setTimeout(() => {
      refreshTimeoutRef.current = null;
      void refreshAttention().catch(() => {});
    }, 500);
  }, [refreshAttention]);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [nextProjects, nextConversations, authStatus] = await Promise.all([
        apiGet<AccessibleProject[]>("/api/projects"),
        apiGet<WorkflowAssistantConversationList>("/api/workflow-assistant/conversations"),
        apiGet<AuthStatus>("/api/auth/status"),
      ]);
      const nextTasks = (
        await Promise.all(
          nextProjects.map(async (project) => {
            const projectTasks = await apiGet<TaskRecord[]>(
              `/api/projects/${encodeURIComponent(project.project_id)}/tasks`,
            );
            return projectTasks.map((task) => ({
              ...task,
              project_id: project.project_id,
            }));
          }),
        )
      ).flat();
      setProjects(nextProjects);
      setTasks(nextTasks);
      setConversations(nextConversations.conversations);
      setAttachmentsEnabled(Boolean(
        authStatus.data?.workflow_assistant_enabled &&
        authStatus.data?.workflow_assistant_attachments_enabled,
      ));
      setProjectChangesEnabled(Boolean(
        authStatus.data?.workflow_assistant_enabled &&
        authStatus.data?.workflow_assistant_project_changes_enabled,
      ));
      setGapFillEnabled(Boolean(
        authStatus.data?.workflow_assistant_enabled &&
        authStatus.data?.workflow_assistant_gap_fill_enabled,
      ));
      void refreshAttention().catch(() => {
        // Keep the workspace usable when an older backend has no inbox route.
        setAttentionCount(0);
        setAttentionPlans([]);
      });
      setSelectedConversation((current) => current || nextConversations.conversations[0] || null);
      setSelectedProjectIds((current) => current.length ? current : nextProjects.map((project) => project.project_id));
      // Keep an empty task selection as "let the planner choose from the
      // project context". Only explicit checkbox selections lock the request
      // to an article range and disable task supplementation.
    } catch (nextError) {
      setError(messageText(nextError));
    } finally {
      setLoading(false);
    }
  }, [refreshAttention]);

  useEffect(() => {
    void load();
    return () => eventSourceRef.current?.close();
  }, [load]);

  const selectedConversationProjectKey = (
    selectedConversation?.project_ids || []
  ).join("\u0000");
  const selectedConversationProjectIds = useMemo(
    () => selectedConversationProjectKey
      ? selectedConversationProjectKey.split("\u0000")
      : [],
    [selectedConversationProjectKey],
  );

  useEffect(() => {
    const conversationId = selectedConversation?.conversation_id;
    if (!conversationId) {
      activeConversationIdRef.current = null;
      return;
    }
    if (selectedConversationProjectIds.length) {
      setSelectedProjectIds(selectedConversationProjectIds);
    } else if (projects.length) {
      setSelectedProjectIds(projects.map((project) => project.project_id));
    }
    if (activeConversationIdRef.current !== conversationId) {
      activeConversationIdRef.current = conversationId;
      setPlan((current) => (
        current?.conversation_id === conversationId ? current : null
      ));
      setTimeline([]);
      setTimelineExpanded(false);
      lastEventSequenceRef.current = 0;
      reconnectAttemptRef.current = 0;
    }
  }, [projects, selectedConversation?.conversation_id, selectedConversationProjectIds]);

  useEffect(() => {
    const scopedTaskIds = new Set(
      tasks
        .filter((task) => selectedProjectIds.includes(task.project_id))
        .map((task) => task.id),
    );
    setSelectedTaskIds((current) => current.filter((taskId) => scopedTaskIds.has(taskId)));
  }, [selectedProjectIds, tasks]);

  useEffect(() => {
    const conversationId = selectedConversation?.conversation_id;
    if (!conversationId || loadedConversationRef.current === conversationId) return;
    loadedConversationRef.current = conversationId;
    void Promise.all([
      apiGet<WorkflowAssistantConversation>(
        `/api/workflow-assistant/conversations/${encodeURIComponent(conversationId)}`,
      ),
      apiGet<WorkflowAssistantPlan | null>(
        `/api/workflow-assistant/conversations/${encodeURIComponent(conversationId)}/latest-plan`,
      ),
    ]).then(([conversation, latestPlan]) => {
      setSelectedConversation(conversation);
      setConversations((current) => current.map((item) => (
        item.conversation_id === conversation.conversation_id ? conversation : item
      )));
      setPlan((current) => (
        current?.conversation_id === conversationId ? current : latestPlan
      ));
    }).catch((nextError) => setError(messageText(nextError)));
  }, [selectedConversation?.conversation_id]);

  useEffect(() => {
    const planId = plan?.plan_id;
    setPlanPreviewOpen(false);
    if (!planId) {
      activePlanIdRef.current = null;
      return;
    }
    if (activePlanIdRef.current !== planId) {
      activePlanIdRef.current = planId;
      setTimeline([]);
      setTimelineExpanded(false);
      lastEventSequenceRef.current = 0;
      reconnectAttemptRef.current = 0;
    }
    eventSourceRef.current?.close();
    if (reconnectTimerRef.current !== null) {
      window.clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
    let disposed = false;
    let streamFinished = false;
    let eventBatchTimeout: number | null = null;
    let batchedEventCount = 0;

    const debouncedRefreshPlan = () => {
      // Clear any existing plan refresh timeout
      if (planRefreshTimeoutRef.current !== null) {
        window.clearTimeout(planRefreshTimeoutRef.current);
      }

      // Schedule plan refresh after 300ms of inactivity
      planRefreshTimeoutRef.current = window.setTimeout(() => {
        planRefreshTimeoutRef.current = null;
        void apiGet<WorkflowAssistantPlan>(
          `/api/workflow-assistant/plans/${encodeURIComponent(planId)}`,
        ).then(setPlan).catch(() => {
          // The next SSE event or a manual reload will retry the projection.
        });
      }, 300);
    };

    const handleEvent = (event: MessageEvent<string>) => {
      try {
        const data = JSON.parse(event.data) as {
          event_kind?: string;
          sequence?: number;
          public_payload?: {
            added_step_ids?: string[];
            removed_step_ids?: string[];
            changed_step_ids?: string[];
          };
        };
        const sequence = data.sequence ?? 0;
        if (sequence > lastEventSequenceRef.current) {
          lastEventSequenceRef.current = sequence;
        }
        setTimeline((current) => {
          const diff = data.public_payload;
          const diffText = data.event_kind === "plan_revised" && diff
            ? ` · 新增 ${diff.added_step_ids?.length ?? 0} · 变更 ${diff.changed_step_ids?.length ?? 0} · 移除 ${diff.removed_step_ids?.length ?? 0}`
            : "";
          const item = `${sequence || ""} · ${data.event_kind ?? "计划更新"}${diffText}`;
          return current.includes(item) ? current : [...current, item].slice(-50);
        });

        // Batch events: increment counter and schedule debounced refresh
        batchedEventCount++;
        debouncedRefreshPlan();

        // Clear existing batch timeout
        if (eventBatchTimeout !== null) {
          window.clearTimeout(eventBatchTimeout);
        }

        // After 1 second of receiving events, refresh attention once
        eventBatchTimeout = window.setTimeout(() => {
          eventBatchTimeout = null;
          if (batchedEventCount > 0) {
            batchedEventCount = 0;
            debouncedRefreshAttention();
          }
        }, 1000);
      } catch {
        // Keep the timeline usable if a proxy emits a non-JSON keepalive.
      }
    };
    const handleDone = () => {
      streamFinished = true;
      eventSourceRef.current?.close();
    };
    const connect = () => {
      if (disposed || streamFinished) return;
      const source = new EventSource(
        `${apiFileUrl(`/api/workflow-assistant/plans/${encodeURIComponent(planId)}/events/stream?after_sequence=${lastEventSequenceRef.current}`)}`,
        { withCredentials: true },
      );
      source.onopen = () => {
        reconnectAttemptRef.current = 0;
      };
      source.onmessage = handleEvent;
      source.addEventListener("workflow-assistant", handleEvent as EventListener);
      source.addEventListener("done", handleDone);
      source.onerror = () => {
        source.close();
        if (!disposed && !streamFinished) {
          const attempt = reconnectAttemptRef.current;
          reconnectAttemptRef.current = Math.min(attempt + 1, 6);
          const delay = Math.min(30_000, 1000 * (2 ** attempt));
          reconnectTimerRef.current = window.setTimeout(connect, delay);
        }
      };
      eventSourceRef.current = source;
    };
    connect();
    return () => {
      disposed = true;
      eventSourceRef.current?.close();
      if (reconnectTimerRef.current !== null) {
        window.clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
      if (eventBatchTimeout !== null) {
        window.clearTimeout(eventBatchTimeout);
        eventBatchTimeout = null;
      }
      if (planRefreshTimeoutRef.current !== null) {
        window.clearTimeout(planRefreshTimeoutRef.current);
        planRefreshTimeoutRef.current = null;
      }
    };
  }, [plan?.plan_id, debouncedRefreshAttention]);

  const conversationMessages = selectedConversation?.messages || [];
  const showPendingUserMessage = pendingUserMessage?.conversationId === selectedConversation?.conversation_id;
  useEffect(() => {
    const viewport = messageScrollAreaRef.current?.querySelector<HTMLElement>(
      '[data-slot="scroll-area-viewport"]',
    );
    if (viewport) {
      viewport.scrollTop = viewport.scrollHeight;
    }
  }, [
    conversationMessages.length,
    pendingUserMessage?.content,
    selectedConversation?.conversation_id,
    showPendingUserMessage,
  ]);
  const selectedProjects = useMemo(
    () => projects.filter((project) => selectedProjectIds.includes(project.project_id)),
    [projects, selectedProjectIds],
  );
  const waitingResearchSteps = useMemo(
    () => (plan?.steps || []).filter(
      (step) => step.action_kind === "start_research" && step.status === "waiting_review",
    ),
    [plan],
  );
  const deliverySteps = useMemo(
    () => (plan?.steps || []).filter((step) => step.action_kind === "package_delivery"),
    [plan],
  );
  const readyDeliverySteps = useMemo(
    () => deliverySteps.filter(isReadyDeliveryStep),
    [deliverySteps],
  );
  const readyDeliveryCount = readyDeliverySteps.length;
  const readyDeliveryProjectCount = new Set(
    readyDeliverySteps.map((step) => step.project_id),
  ).size;
  const taskTopics = useMemo(
    () => new Map(tasks.map((task) => [`${task.project_id}:${task.id}`, task.topic] as const)),
    [tasks],
  );
  const articleCards = useMemo(
    () => (plan ? buildWorkflowArticleCards(plan, taskTopics) : []),
    [plan, taskTopics],
  );
  const waitingResearchSignature = useMemo(
    () => waitingResearchSteps
      .map((step) => `${step.step_id}:${step.project_id}:${researchThreadId(step)}`)
      .join("|"),
    [waitingResearchSteps],
  );

  useEffect(() => {
    let disposed = false;
    if (!gapFillEnabled || !waitingResearchSteps.length) {
      setResearchDetails({});
      setGapFillSelections({});
      return;
    }
    void Promise.all(
      waitingResearchSteps.map(async (step) => {
        const threadId = researchThreadId(step);
        if (!threadId) return [step.step_id, null] as const;
        try {
          return [step.step_id, await getResearchRun(step.project_id, threadId)] as const;
        } catch {
          return [step.step_id, null] as const;
        }
      }),
    ).then((entries) => {
      if (disposed) return;
      setResearchDetails(Object.fromEntries(entries));
      setGapFillSelections((current) => {
        const next: Record<string, string[]> = {};
        for (const [stepId, detail] of entries) {
          const visibleIds = new Set(
            detail ? gapFillReviewCandidates(detail).map((candidate) => candidate.candidate_id) : [],
          );
          next[stepId] = (current[stepId] || []).filter((id) => visibleIds.has(id));
        }
        return next;
      });
    });
    return () => {
      disposed = true;
    };
  }, [gapFillEnabled, waitingResearchSignature, waitingResearchSteps]);

  async function createConversation() {
    setPending(true);
    setError("");
    try {
      const created = await apiPost<WorkflowAssistantConversation>("/api/workflow-assistant/conversations", {
        title: "新的文章工作计划",
        project_ids: selectedProjectIds,
      });
      setConversations((current) => [created, ...current]);
      setSelectedConversation(created);
      setPlan(null);
      setPlanDetailsOpen(false);
    } catch (nextError) {
      setError(messageText(nextError));
    } finally {
      setPending(false);
    }
  }

  async function sendMessage() {
    const content = draft.trim();
    if (!content || pending) return;
    setPending(true);
    setRequestPhase("sending");
    setPlanDetailsOpen(false);
    setError("");
    let dispatchConversation: WorkflowAssistantConversation | null = selectedConversation;
    let previousPlanId: string | null = null;
    try {
      let conversation = selectedConversation;
      if (!conversation) {
        conversation = await apiPost<WorkflowAssistantConversation>("/api/workflow-assistant/conversations", {
          title: content.slice(0, 80),
          project_ids: selectedProjectIds,
        });
        setConversations((current) => [conversation as WorkflowAssistantConversation, ...current]);
        setSelectedConversation(conversation);
      }
      dispatchConversation = conversation;
      previousPlanId = plan?.conversation_id === conversation.conversation_id
        ? plan.plan_id
        : null;
      const scopeKey = JSON.stringify({
        projectIds: selectedProjectIds,
        articleTaskIds: selectedTaskIds,
      });
      let dispatchIdentity = pendingMessageDispatchRef.current;
      if (
        !dispatchIdentity
        || dispatchIdentity.conversationId !== conversation.conversation_id
        || dispatchIdentity.content !== content
        || dispatchIdentity.scopeKey !== scopeKey
      ) {
        dispatchIdentity = {
          conversationId: conversation.conversation_id,
          content,
          scopeKey,
          requestId: localId("request"),
          idempotencyKey: localId("message"),
        };
        pendingMessageDispatchRef.current = dispatchIdentity;
      }
      setPendingUserMessage({ conversationId: conversation.conversation_id, content });
      setRequestPhase("waiting_reply");
      const response = await apiPost<WorkflowAssistantDispatch>(
        `/api/workflow-assistant/conversations/${encodeURIComponent(conversation.conversation_id)}/messages`,
        {
          content,
          request_id: dispatchIdentity.requestId,
          idempotency_key: dispatchIdentity.idempotencyKey,
          project_ids: selectedProjectIds,
          article_task_ids: selectedTaskIds.length ? selectedTaskIds : null,
        },
        WORKFLOW_ASSISTANT_PLANNING_TIMEOUT_MS,
      );
      if (
        response.dispatch_id
        && (response.dispatch_status === "queued" || response.dispatch_status === "running")
      ) {
        const recovered = await recoverDispatchResult(
          conversation.conversation_id,
          content,
          previousPlanId,
          response.dispatch_id,
          dispatchIdentity.idempotencyKey,
        );
        if (!recovered) {
          throw new Error("计划仍在后台生成，请稍后刷新助手页面查看结果。");
        }
        if (recovered.error) {
          throw new Error(recovered.error);
        }
        pendingMessageDispatchRef.current = null;
        if (recovered.plan) {
          setPlan(recovered.plan);
          setPlanDetailsOpen(false);
        }
        setDraft("");
        setPendingUserMessage(null);
        setSelectedConversation(recovered.conversation);
        setConversations((current) => current.map((item) => (
          item.conversation_id === recovered.conversation.conversation_id
            ? recovered.conversation
            : item
        )));
        void refreshAttention().catch(() => undefined);
        return;
      }
      if (response.dispatch_status === "failed") {
        throw new Error(
          response.dispatch_error_code
            ? `计划生成失败（${response.dispatch_error_code}），可以重新发送请求重试。`
            : "计划生成失败，可以重新发送请求重试。",
        );
      }
      pendingMessageDispatchRef.current = null;
      setRequestPhase("refreshing");
      if (response.plan) {
        setPlan(response.plan);
        setPlanDetailsOpen(false);
      }
      void refreshAttention().catch(() => undefined);
      setDraft("");
      const refreshed = await apiGet<WorkflowAssistantConversation>(
        `/api/workflow-assistant/conversations/${encodeURIComponent(conversation.conversation_id)}`,
      );
      setPendingUserMessage(null);
      setSelectedConversation(refreshed);
      setConversations((current) => current.map((item) => item.conversation_id === refreshed.conversation_id ? refreshed : item));
    } catch (nextError) {
      if (dispatchConversation && isDispatchConnectionFailure(nextError)) {
        setRequestPhase("waiting_reply");
        setError("与服务器的连接暂时中断，计划可能仍在后台生成，正在自动同步……");
        const recovered = await recoverDispatchResult(
          dispatchConversation.conversation_id,
          content,
          previousPlanId,
          null,
          pendingMessageDispatchRef.current?.idempotencyKey || null,
        );
        if (recovered) {
          pendingMessageDispatchRef.current = null;
          setError("");
          if (recovered.plan) {
            setPlan(recovered.plan);
            setPlanDetailsOpen(false);
          }
          setDraft("");
          setPendingUserMessage(null);
          setSelectedConversation(recovered.conversation);
          setConversations((current) => current.map((item) => (
            item.conversation_id === recovered.conversation.conversation_id
              ? recovered.conversation
              : item
          )));
          void refreshAttention().catch(() => undefined);
          return;
        }
      }
      setPendingUserMessage(null);
      setError(messageText(nextError));
    } finally {
      setPending(false);
      setRequestPhase("idle");
    }
  }

  async function changePlan(
    action: "confirm" | "pause" | "resume" | "retry" | "cancel",
    projectIds?: string[],
  ) {
    if (!plan || pending) return;
    setPending(true);
    setError("");
    try {
      const next = await apiPost<WorkflowAssistantPlan>(
        `/api/workflow-assistant/plans/${encodeURIComponent(plan.plan_id)}/${action}`,
        {
          revision: plan.revision,
          plan_hash: plan.plan_hash,
          ...(projectIds ? { project_ids: projectIds } : {}),
        },
      );
      setPlan(next);
      void refreshAttention().catch(() => undefined);
    } catch (nextError) {
      setError(messageText(nextError, plan));
    } finally {
      setPending(false);
    }
  }

  async function submitGapFill(step: WorkflowAssistantStep) {
    if (!plan || gapFillPending) return;
    const threadId = researchThreadId(step);
    if (!threadId) {
      setError("研究 Thread 标识缺失，暂时无法提交证据缺口处理。 ");
      return;
    }
    setGapFillPending(step.step_id);
    setError("");
    try {
      const requestId = gapFillRequestIdsRef.current[step.step_id]
        || (gapFillRequestIdsRef.current[step.step_id] = localId("gap-fill"));
      const response: WorkflowAssistantGapFillResponse = await gapFillWorkflowAssistantPlan(
        plan.plan_id,
        {
          revision: plan.revision,
          step_id: step.step_id,
          research_thread_id: threadId,
          request_id: requestId,
          approved_candidate_ids: gapFillSelections[step.step_id] || [],
        },
      );
      setPlan(response.plan);
      setGapFillQueueStatus((current) => ({
        ...current,
        [step.step_id]: response.queue_job_status || "queued",
      }));
      setGapFillSelections((current) => ({ ...current, [step.step_id]: [] }));
      delete gapFillRequestIdsRef.current[step.step_id];
      void refreshAttention().catch(() => undefined);
    } catch (nextError) {
      setError(messageText(nextError, plan));
    } finally {
      setGapFillPending(null);
    }
  }

  async function revisePlan() {
    const request = revisionDraft.trim();
    if (!plan || !request || pending || revisionPending) return;
    setRevisionPending(true);
    setError("");
    try {
      const next = await apiPost<WorkflowAssistantPlan>(
        `/api/workflow-assistant/plans/${encodeURIComponent(plan.plan_id)}/revise`,
        {
          revision: plan.revision,
          plan_hash: plan.plan_hash,
          natural_language_request: request,
        },
        WORKFLOW_ASSISTANT_PLANNING_TIMEOUT_MS,
      );
      setPlan(next);
      setPlanDetailsOpen(false);
      setRevisionDraft("");
      void refreshAttention().catch(() => undefined);
    } catch (nextError) {
      setError(messageText(nextError, plan));
    } finally {
      setRevisionPending(false);
    }
  }

  async function downloadBatchDelivery() {
    if (!plan || !readyDeliveryCount || batchArtifactPending) return;
    setBatchArtifactPending("all");
    setError("");
    try {
      const download = await apiGet<WorkflowAssistantBatchDownload>(
        `/api/workflow-assistant/plans/${encodeURIComponent(plan.plan_id)}/delivery-package/download`,
        30_000,
      );
      if (!download.url) throw new Error("服务器没有返回可用的批量下载地址。");
      triggerBrowserDownload(
        download.url,
        download.filename || "workflow-batch-delivery.zip",
      );
    } catch (nextError) {
      setError(messageText(nextError, plan));
    } finally {
      setBatchArtifactPending("");
    }
  }

  async function openAttentionPlan(nextPlan: WorkflowAssistantPlanSummary) {
    setError("");
    setAttentionPlanPendingId(nextPlan.plan_id);
    try {
      const [freshPlan, conversation] = await Promise.all([
        apiGet<WorkflowAssistantPlan>(
          `/api/workflow-assistant/plans/${encodeURIComponent(nextPlan.plan_id)}`,
        ),
        apiGet<WorkflowAssistantConversation>(
          `/api/workflow-assistant/conversations/${encodeURIComponent(nextPlan.conversation_id)}`,
        ).catch(() => null),
      ]);
      // Set the refs before React effects observe the new conversation. This
      // prevents the conversation loader from clearing the plan or showing a
      // stale session while an inbox item is being opened.
      activeConversationIdRef.current = freshPlan.conversation_id;
      loadedConversationRef.current = freshPlan.conversation_id;
      activePlanIdRef.current = freshPlan.plan_id;
      setPlan(freshPlan);
      setPlanDetailsOpen(false);
      try {
        if (!conversation) throw new Error("conversation expired");
        setSelectedConversation(conversation);
        setConversations((current) => current.map((item) => (
          item.conversation_id === conversation.conversation_id ? conversation : item
        )));
      } catch {
        // Private messages expire independently from durable plans. Keep the
        // plan/result inbox useful after that retention boundary.
        setSelectedConversation(null);
      }
      await refreshAttention();
    } catch (nextError) {
      setError(messageText(nextError));
    } finally {
      setAttentionPlanPendingId(null);
    }
  }

  return (
    <main className="min-h-screen bg-background text-foreground">
      <header className="border-b bg-card">
        <div className="mx-auto flex max-w-[1380px] items-center justify-between gap-4 px-5 py-5">
          <div className="flex items-center gap-3">
            <Link href="/" className="flex size-10 items-center justify-center rounded-xl bg-primary text-primary-foreground" aria-label="返回项目目录">
              <Sparkles className="size-5" />
            </Link>
            <div>
              <p className="text-xs font-semibold uppercase tracking-[0.14em] text-primary">Workflow Assistant</p>
              <h1 className="text-xl font-semibold">文章工作助手</h1>
              <p className="text-sm text-muted-foreground">查询项目资料，生成跨项目文章计划，并在确认后执行。</p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            {attentionCount > 0 && <Badge variant="outline">待处理 {attentionCount}</Badge>}
            <Button type="button" variant="outline" onClick={() => void createConversation()} disabled={pending || loading}>
              <Plus />
              新会话
            </Button>
          </div>
        </div>
      </header>

      <div className="mx-auto grid max-w-[1380px] gap-4 px-5 py-5 lg:grid-cols-[260px_minmax(0,1fr)_380px]">
        <Card className="min-h-[640px] gap-0 py-0">
          <CardHeader className="border-b px-4 py-4">
            <CardTitle className="flex items-center gap-2 text-base"><MessageSquareText className="size-4" />会话</CardTitle>
            <CardDescription>私人会话保留 30 天。</CardDescription>
          </CardHeader>
          <CardContent className="p-2">
            {loading ? <div className="flex items-center gap-2 p-3 text-sm text-muted-foreground"><Loader2 className="size-4 animate-spin" />读取中…</div> : conversations.length ? conversations.map((conversation) => (
              <button
                key={conversation.conversation_id}
                type="button"
                onClick={() => setSelectedConversation(conversation)}
                className={`mb-1 w-full rounded-lg px-3 py-3 text-left transition-colors ${selectedConversation?.conversation_id === conversation.conversation_id ? "bg-primary text-primary-foreground" : "hover:bg-muted"}`}
              >
                <span className="block truncate text-sm font-medium">{conversation.title}</span>
                <span className={`mt-1 block text-xs ${selectedConversation?.conversation_id === conversation.conversation_id ? "text-primary-foreground/70" : "text-muted-foreground"}`}>{conversation.messages.length} 条消息</span>
              </button>
            )) : <div className="p-3 text-sm text-muted-foreground">还没有助手会话。</div>}
          </CardContent>
        </Card>

        <Card className="min-h-[640px] gap-0 py-0">
          <CardHeader className="border-b px-5 py-4">
            <CardTitle>自然语言请求</CardTitle>
            <CardDescription>只读问题可直接回答；涉及写入时会先生成完整计划并等待一次确认。</CardDescription>
          </CardHeader>
          <CardContent className="flex min-h-[570px] flex-col gap-4 px-5 py-5">
            <div className="rounded-xl border bg-muted/30 p-4">
              <div className="mb-3 flex items-center justify-between gap-3">
                <Label>计划范围</Label>
                <span className="text-xs text-muted-foreground">{selectedProjects.length} 个项目</span>
              </div>
              <div className="grid gap-2 sm:grid-cols-2">
                {projects.map((project) => {
                  const checked = selectedProjectIds.includes(project.project_id);
                  return (
                    <label key={project.project_id} className="flex cursor-pointer items-start gap-2 rounded-lg border bg-background px-3 py-2 text-sm">
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={(event) => setSelectedProjectIds((current) => event.target.checked ? [...current, project.project_id] : current.filter((item) => item !== project.project_id))}
                        className="mt-1 accent-primary"
                      />
                      <span className="min-w-0"><span className="block truncate font-medium">{project.customer_name}</span><span className="block truncate text-xs text-muted-foreground">{project.project_id}</span></span>
                    </label>
                  );
                })}
              </div>
              {tasks.filter((task) => selectedProjectIds.includes(task.project_id)).length > 0 && <div className="mt-4 grid gap-2"><div className="flex items-center justify-between gap-3"><Label>文章范围</Label><span className="text-xs text-muted-foreground">{selectedTaskIds.length} 个任务</span></div><div className="max-h-52 overflow-auto rounded-lg border bg-background p-2"><div className="grid gap-1">{tasks.filter((task) => selectedProjectIds.includes(task.project_id)).map((task) => { const checked = selectedTaskIds.includes(task.id); return <label key={`${task.project_id}:${task.id}`} className="flex cursor-pointer items-start gap-2 rounded-md px-2 py-1.5 text-sm hover:bg-muted"><input type="checkbox" checked={checked} onChange={(event) => setSelectedTaskIds((current) => event.target.checked ? [...current, task.id] : current.filter((item) => item !== task.id))} className="mt-1 accent-primary" /><span className="min-w-0"><span className="block truncate">{task.topic}</span><span className="block truncate text-xs text-muted-foreground">{task.project_id} · {task.id} · {task.status}</span></span></label>; })}</div></div><span className="text-xs text-muted-foreground">不勾选任务时，助手按当前项目上下文规划。</span></div>}
            </div>
            <div ref={messageScrollAreaRef} className="h-[clamp(18rem,42dvh,32rem)] min-h-0 shrink-0">
              <ScrollArea className="h-full rounded-xl border bg-background p-4">
                <div className="grid gap-3" role="log" aria-label="对话消息" aria-live="polite">
                  {conversationMessages.length || showPendingUserMessage ? <>
                    {conversationMessages.map((message) => (
                      <div key={message.message_id} className={`max-w-[92%] whitespace-pre-wrap break-words rounded-xl px-3 py-2.5 text-sm leading-6 ${message.role === "user" ? "ml-auto bg-primary text-primary-foreground" : "bg-muted"}`}>
                        <MessageContent content={message.content} />
                      </div>
                    ))}
                    {pendingUserMessage && showPendingUserMessage && <div className="ml-auto flex max-w-[92%] items-start gap-2 rounded-xl bg-primary/80 px-3 py-2.5 text-sm leading-6 text-primary-foreground" aria-label="已发送，等待助手回复">
                      <span className="whitespace-pre-wrap break-words"><MessageContent content={pendingUserMessage.content} /></span>
                      <Loader2 className="workflow-assistant-spinner mt-1 size-4 shrink-0" />
                    </div>}
                  </> : <div className="flex min-h-52 flex-col items-center justify-center text-center text-sm text-muted-foreground"><MessageSquareText className="mb-3 size-8" /><p className="font-medium text-foreground">直接聊天、查询资料或安排工作</p><p className="mt-1 max-w-sm">例如：你是谁？这个项目的知识库里有哪些产品参数？或者帮我规划两篇文章。</p></div>}
                </div>
              </ScrollArea>
            </div>
            {error && <Alert variant="destructive"><AlertCircle /><AlertTitle>助手暂时无法继续</AlertTitle><AlertDescription>{error}</AlertDescription></Alert>}
            <div className="grid gap-2">
              {attachmentsEnabled && (
                <WorkflowAssistantAttachments
                  conversationId={selectedConversation?.conversation_id ?? null}
                  selectedProjectIds={selectedProjectIds}
                  projectChangesEnabled={projectChangesEnabled}
                  onActivity={(message) => setTimeline((current) => [...current, message].slice(-50))}
                />
              )}
              {requestPhase !== "idle" && <div className="flex min-h-11 items-center gap-2 rounded-lg border bg-muted/30 px-3 py-2 text-sm" role="status" aria-live="polite">
                <Loader2 className="workflow-assistant-spinner size-4 shrink-0 text-primary" />
                <span className="font-medium">{requestPhase === "sending" ? "正在发送请求" : requestPhase === "waiting_reply" ? "已发送，等待助手回复" : "回复已收到，正在更新计划"}</span>
                <span className="text-muted-foreground">{requestPhase === "sending" ? "正在提交当前项目和文章范围。" : requestPhase === "waiting_reply" ? "助手正在处理请求，页面不会重复提交。" : "正在刷新会话和计划预览。"}</span>
              </div>}
              <Textarea value={draft} onChange={(event) => setDraft(event.target.value)} placeholder="直接聊天、查询所选项目的知识库，或描述要执行的工作…" rows={4} disabled={pending} onKeyDown={(event) => { if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); void sendMessage(); } }} />
              <p className="text-xs leading-5 text-muted-foreground">操作提示：需要跳过复检时可写“这篇不用复检”；若当前任务已有对应正文的初检 AI 率且低于 30%，助手会自动跳过降 AI 和二次检测，ZeroGPT 结果仍由人工确认。</p>
              <div className="flex items-center justify-between gap-3"><span className="text-xs text-muted-foreground">Ctrl/Cmd + Enter 发送 · 不会显示原始提示词或模型思维链</span><Button type="button" onClick={() => void sendMessage()} disabled={pending || !draft.trim() || !selectedProjectIds.length}>{pending ? <Loader2 className="workflow-assistant-spinner" /> : <Send />}{requestPhase === "sending" ? "正在发送" : requestPhase === "waiting_reply" ? "等待回复" : requestPhase === "refreshing" ? "更新计划" : pending ? "处理中" : "发送"}</Button></div>
            </div>
          </CardContent>
        </Card>

        <div className="grid content-start gap-4">
          {attentionPlans.length > 0 && <Card className="gap-0 py-0">
            <CardHeader className="border-b px-4 py-4">
              <CardTitle className="text-base">待处理收件箱</CardTitle>
              <CardDescription>确认、失败和未读完成会保留在这里。</CardDescription>
            </CardHeader>
            <CardContent className="grid gap-2 px-4 py-4">
              {attentionPlans.map((attentionPlan) => (
                <button
                  key={attentionPlan.plan_id}
                  type="button"
                  onClick={() => void openAttentionPlan(attentionPlan)}
                  disabled={attentionPlanPendingId === attentionPlan.plan_id}
                  className="rounded-lg border px-3 py-2 text-left transition-colors hover:bg-muted"
                >
                  <span className="flex items-center justify-between gap-2 text-sm font-medium">
                    <span className="truncate">{attentionPlan.title}</span>
                    {attentionPlanPendingId === attentionPlan.plan_id
                      ? <Loader2 className="size-4 shrink-0 animate-spin" />
                      : <Badge variant={statusVariant(attentionPlan.status)}>
                      {statusLabels[attentionPlan.status]}
                      </Badge>}
                  </span>
                  <span className="mt-1 block truncate text-xs text-muted-foreground">
                    {attentionPlan.project_ids.join("、")} · {attentionPlan.attention_state}
                  </span>
                </button>
              ))}
            </CardContent>
          </Card>}
          <Card className="gap-0 py-0">
            <CardHeader className="border-b px-4 py-4">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div>
                  <CardTitle className="text-base">计划预览</CardTitle>
                  <CardDescription className="mt-1">计划详情和执行控制已收进独立页内弹窗。</CardDescription>
                </div>
                <div className="flex items-center gap-2">
                  {plan && <Badge variant={statusVariant(plan.status)}>{statusLabels[plan.status]}</Badge>}
                  {plan && <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    className="min-h-9"
                    aria-haspopup="dialog"
                    onClick={() => setPlanPreviewOpen(true)}
                  >
                    <Workflow />查看计划
                  </Button>}
                </div>
              </div>
            </CardHeader>
            <CardContent className="px-4 py-4">
              {plan ? <div>
                <p className="font-medium">{plan.title}</p>
                <p className="mt-1 text-xs leading-5 text-muted-foreground">
                  Revision {plan.revision} · {plan.steps.length} 个步骤 · {articleCards.length} 篇文章 · 并发上限 {plan.concurrency_limit}{plan.budget_warning ? " · 接近软预算" : ""}
                </p>
                <p className="mt-3 rounded-lg border border-dashed bg-muted/20 px-3 py-3 text-sm text-muted-foreground" role="status">
                  点击“查看计划”打开完整步骤、批量交付和确认操作。
                </p>
              </div> : <div className="py-6 text-center text-sm text-muted-foreground"><CircleDot className="mx-auto mb-3 size-7" /><p>发送请求后，这里会显示结构化计划。</p></div>}
            </CardContent>
          </Card>
          {plan && <Dialog open={planPreviewOpen} onOpenChange={setPlanPreviewOpen}>
            <DialogContent className="h-[min(900px,calc(100vh-2rem))] w-[calc(100vw-2rem)] max-w-7xl sm:max-w-7xl grid-rows-[auto_minmax(0,1fr)] overflow-hidden p-0">
              <DialogHeader className="border-b px-5 py-4 pr-12">
                <DialogTitle>计划详情</DialogTitle>
                <DialogDescription>
                  {plan.title} · Revision {plan.revision} · {plan.steps.length} 个步骤 · {articleCards.length} 篇文章
                </DialogDescription>
              </DialogHeader>
              <div className="min-h-0 overflow-y-auto px-5 pb-5">
                <Card className="gap-0 border-0 py-0 shadow-none">
                  <CardHeader className="border-b px-0 py-4">
                    <div className="flex flex-wrap items-center justify-between gap-3">
                      <div>
                        <CardTitle className="text-base">计划执行详情</CardTitle>
                        <CardDescription className="mt-1">查看步骤状态、批量交付和计划控制。</CardDescription>
                      </div>
                      <div className="flex items-center gap-2">
                        <Badge variant={statusVariant(plan.status)}>{statusLabels[plan.status]}</Badge>
                        <Button
                          type="button"
                          variant="outline"
                          size="sm"
                          className="min-h-9"
                          aria-haspopup="dialog"
                          onClick={() => setPlanDetailsOpen(true)}
                        >
                          <Workflow />查看文章概览
                        </Button>
                      </div>
                    </div>
                  </CardHeader>
                  <CardContent id="workflow-plan-details" className="px-0 py-4">
              {plan ? <div className="grid gap-4">
                <div>
                  <p className="font-medium">{plan.title}</p>
                  <p className="mt-1 text-xs leading-5 text-muted-foreground">
                    计划 Revision {plan.revision} · {plan.steps.length} 个步骤 · {articleCards.length} 篇文章 · 并发上限 {plan.concurrency_limit}{plan.budget_warning ? " · 接近软预算" : ""}
                  </p>
                </div>
                <div className="rounded-lg border border-dashed bg-muted/20 px-3 py-3 text-sm text-muted-foreground" role="status">
                  文章概览已移到右上方的二级窗口；每张卡片显示项目、状态、处理进度和完成时间。
                </div>
                {deliverySteps.length > 0 && <div className="grid gap-3 rounded-lg border border-dashed bg-muted/20 p-3">
                  <div>
                    <p className="text-sm font-medium">批量交付下载</p>
                    <p className="mt-1 text-xs text-muted-foreground">
                      {readyDeliveryCount > 0
                        ? `已成功 ${readyDeliveryCount}/${deliverySteps.length} 篇，来自 ${readyDeliveryProjectCount} 个项目；一键合并为一个 ZIP，失败或未完成文章会自动跳过。`
                        : "暂无成功文章可打包下载；失败或未完成文章会自动跳过。"}
                    </p>
                  </div>
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    className="justify-self-start"
                    aria-label={`一键下载成功的 ${readyDeliveryCount} 篇文章`}
                    onClick={() => void downloadBatchDelivery()}
                    disabled={!readyDeliveryCount || Boolean(batchArtifactPending)}
                  >
                    {batchArtifactPending === "all" ? <Loader2 className="animate-spin" /> : <Download />}
                    {batchArtifactPending === "all" ? "正在打包…" : `一键下载成功的 ${readyDeliveryCount} 篇`}
                  </Button>
                </div>}
                {gapFillEnabled && waitingResearchSteps.map((step) => {
                  const detail = researchDetails[step.step_id];
                  const reviewCandidates = detail ? gapFillReviewCandidates(detail) : [];
                  const selectedIds = gapFillSelections[step.step_id] || [];
                  const reasons = detail ? researchGapReasons(detail) : [];
                  const canSubmit = detail?.status === "waiting_for_review" && gapFillPending === null;
                  return (
                    <Card key={`gap-fill:${step.step_id}`} className="gap-0 border-dashed py-0">
                      <CardHeader className="border-b px-4 py-3">
                        <CardTitle className="text-base">精准补全证据缺口</CardTitle>
                        <CardDescription>仅审查当前项目官网候选；未选中的资料不会进入证据通道。</CardDescription>
                      </CardHeader>
                      <CardContent className="grid gap-3 px-4 py-4">
                        {detail ? (
                          <>
                            <div className="grid gap-1 rounded-lg border bg-muted/20 p-3 text-sm">
                              <span className="font-medium">当前缺口</span>
                              {reasons.length ? reasons.map((reason) => (
                                <span key={reason} className="text-muted-foreground">· {reason}</span>
                              )) : <span className="text-muted-foreground">当前 Scope 仍缺少可作证资料，请审查官网候选。</span>}
                              <span className="text-xs text-muted-foreground">Gap round {detail.gap_fill_round}/{detail.max_gap_fill_rounds} · Scope {detail.current_scope_id || "-"}</span>
                            </div>
                            <div className="grid gap-2">
                              <span className="text-sm font-medium">官网候选资料</span>
                              {reviewCandidates.length ? reviewCandidates.map((candidate) => (
                                <label key={candidate.candidate_id} className="flex cursor-pointer items-start gap-2 rounded-lg border p-3 text-sm">
                                  <input
                                    type="checkbox"
                                    className="mt-1 size-4 accent-primary"
                                    checked={selectedIds.includes(candidate.candidate_id)}
                                    disabled={!canSubmit}
                                    onChange={(event) => setGapFillSelections((current) => ({
                                      ...current,
                                      [step.step_id]: event.target.checked
                                        ? [...(current[step.step_id] || []), candidate.candidate_id]
                                        : (current[step.step_id] || []).filter((id) => id !== candidate.candidate_id),
                                    }))}
                                  />
                                  <span className="min-w-0">
                                    <span className="block break-all font-medium">{candidate.url}</span>
                                    <span className="mt-1 block text-xs text-muted-foreground">{candidate.page_type} · {candidate.evidence.channel || "official discovery"}</span>
                                  </span>
                                </label>
                              )) : <p className="text-sm text-muted-foreground">当前没有可审查的官网候选；仍可明确拒绝全部并继续。</p>}
                            </div>
                            <Button type="button" className="min-h-11 justify-self-start" disabled={!canSubmit} onClick={() => void submitGapFill(step)}>
                              {gapFillPending === step.step_id ? <Loader2 className="animate-spin" /> : <Check />}
                              {selectedIds.length ? `批准 ${selectedIds.length} 个并继续` : "拒绝全部并继续"}
                            </Button>
                          </>
                        ) : <p className="flex items-center gap-2 text-sm text-muted-foreground"><Loader2 className="size-4 animate-spin" />正在读取研究缺口和官网候选……</p>}
                      </CardContent>
                    </Card>
                  );
                })}
                {Object.entries(gapFillQueueStatus).map(([stepId, status]) => (
                  <Alert key={`gap-fill-status:${stepId}`}>
                    <Check />
                    <AlertTitle>证据补全已排队</AlertTitle>
                    <AlertDescription>队列状态：{status}。研究完成后，原计划会继续执行未完成步骤。</AlertDescription>
                  </Alert>
                ))}
                {plan.status === "awaiting_confirmation" && <div className="flex flex-wrap gap-2"><Button type="button" onClick={() => void changePlan("confirm")} disabled={pending}><Check />确认计划并排队</Button><Button type="button" variant="destructive" onClick={() => void changePlan("cancel")} disabled={pending}><Square />取消计划</Button></div>}
                {plan.status === "waiting_review" && <div className="flex flex-wrap gap-2"><Button type="button" onClick={() => void changePlan("confirm")} disabled={pending}><Check />确认并继续</Button><Button type="button" variant="destructive" onClick={() => void changePlan("cancel")} disabled={pending}><Square />取消</Button></div>}
                {plan.status === "queued" || plan.status === "running" ? <div className="flex flex-wrap gap-2"><Button type="button" variant="outline" onClick={() => void changePlan("pause")} disabled={pending}><Pause />暂停</Button><Button type="button" variant="destructive" onClick={() => void changePlan("cancel")} disabled={pending}><Square />取消</Button></div> : null}
                {(plan.status === "queued" || plan.status === "running" || plan.status === "waiting_review") && plan.project_ids.length > 1 && <div className="grid gap-2 rounded-lg border border-dashed p-3"><span className="text-xs font-medium text-muted-foreground">项目执行通道</span><div className="flex flex-wrap gap-2">{plan.project_ids.map((projectId) => { const paused = plan.paused_project_ids.includes(projectId); return <Button key={projectId} type="button" size="sm" variant="outline" onClick={() => void changePlan(paused ? "resume" : "pause", [projectId])} disabled={pending}>{paused ? <Play /> : <Pause />}{paused ? `恢复 ${projectId}` : `暂停 ${projectId}`}</Button>; })}</div></div>}
                {plan.status === "paused" && <div className="flex flex-wrap gap-2"><Button type="button" onClick={() => void changePlan("resume")} disabled={pending}><Play />恢复</Button><Button type="button" variant="destructive" onClick={() => void changePlan("cancel")} disabled={pending}><Square />取消</Button></div>}
                {plan.status === "failed" && <div className="grid gap-2 rounded-lg border border-dashed border-destructive/40 bg-destructive/5 p-3"><div className="text-sm text-muted-foreground">已完成的步骤不会重复执行，只会重新排队失败步骤及同一篇文章被阻断的后续步骤。</div><Button type="button" variant="outline" className="justify-self-start" onClick={() => void changePlan("retry")} disabled={pending}><RotateCcw />重试失败步骤</Button></div>}
                {["draft", "awaiting_confirmation", "paused", "waiting_review", "failed"].includes(plan.status) && <div className="grid gap-2 rounded-lg border border-dashed p-3"><Label htmlFor="workflow-plan-revision">调整未完成步骤</Label><Textarea id="workflow-plan-revision" value={revisionDraft} onChange={(event) => setRevisionDraft(event.target.value)} placeholder="例如：保留已完成步骤，只把未完成正文改成面向采购团队。" rows={3} disabled={revisionPending || pending} /><div className="flex items-center justify-between gap-2"><span className="text-xs text-muted-foreground">会生成新的 Revision，并要求重新确认。</span><Button type="button" variant="outline" onClick={() => void revisePlan()} disabled={revisionPending || pending || !revisionDraft.trim()}>{revisionPending ? <Loader2 className="animate-spin" /> : <Workflow />}生成修订预览</Button></div></div>}
              </div> : <div className="py-10 text-center text-sm text-muted-foreground"><CircleDot className="mx-auto mb-3 size-7" /><p>发送请求后，这里会显示结构化计划。</p></div>}
                  </CardContent>
                </Card>
              </div>
            </DialogContent>
          </Dialog>}
          {plan && <Dialog open={planDetailsOpen} onOpenChange={setPlanDetailsOpen}>
            <DialogContent className="h-[min(880px,calc(100vh-2rem))] w-[calc(100vw-2rem)] max-w-7xl sm:max-w-7xl grid-rows-[auto_minmax(0,1fr)] overflow-hidden p-0">
              <DialogHeader className="border-b px-5 py-4 pr-12">
                <DialogTitle>文章执行概览</DialogTitle>
                <DialogDescription>
                  {plan.title} · {articleCards.length} 篇文章 · 成功 {readyDeliveryCount} 篇；点击卡片右上角可在新标签页打开文章工作台。
                </DialogDescription>
              </DialogHeader>
              <div className="min-h-0 overflow-y-auto px-5 py-4">
                {articleCards.length ? <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                  {articleCards.map((card) => {
                    const workbenchHref = card.taskId
                      ? `/projects/${encodeURIComponent(card.projectId)}/articles/${encodeURIComponent(card.taskId)}?step=review`
                      : null;
                    const timeLabel = card.status === "completed" ? "完成时间" : "最近更新";
                    return (
                      <article key={card.key} className="rounded-xl border bg-background p-3 shadow-xs">
                        <div className="flex items-start justify-between gap-3">
                          <div className="min-w-0">
                            <p className="truncate text-sm font-semibold" title={card.title}>{card.title}</p>
                            <p className="mt-1 truncate text-xs text-muted-foreground">{card.projectId}{card.taskId ? ` · ${card.taskId}` : ""}</p>
                          </div>
                          {workbenchHref ? <Link
                            href={workbenchHref}
                            target="_blank"
                            rel="noreferrer"
                            className="inline-flex size-8 shrink-0 items-center justify-center rounded-md border text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
                            aria-label={`在新标签页打开${card.title}的文章工作台`}
                          >
                            <ExternalLink className="size-4" />
                            <span className="sr-only">在新标签页打开文章工作台</span>
                          </Link> : <span className="inline-flex size-8 shrink-0 items-center justify-center rounded-md border text-muted-foreground/40" title="文章任务创建后可打开工作台">
                            <ExternalLink className="size-4" />
                            <span className="sr-only">文章任务尚未创建</span>
                          </span>}
                        </div>
                        <div className="mt-3 flex flex-wrap items-center gap-2">
                          <Badge variant={workflowArticleCardStatusVariants[card.status]}>
                            {workflowArticleCardStatusLabels[card.status]}
                          </Badge>
                          <span className="text-xs text-muted-foreground">{card.progress}/{card.total} 步已处理</span>
                        </div>
                        <dl className="mt-3 grid grid-cols-2 gap-x-3 gap-y-2 text-xs">
                          <div className="min-w-0">
                            <dt className="text-muted-foreground">交付包</dt>
                            <dd className="mt-0.5 truncate font-medium">{card.packageReady ? "已生成" : "未生成"}</dd>
                          </div>
                          <div className="min-w-0">
                            <dt className="text-muted-foreground">{timeLabel}</dt>
                            <dd className="mt-0.5 truncate font-medium">{formatWorkflowArticleCardTime(card.updatedAt)}</dd>
                          </div>
                        </dl>
                        {card.errorCode && <p className="mt-3 truncate text-xs text-destructive" title={card.errorCode}>失败步骤：{card.errorCode}</p>}
                      </article>
                    );
                  })}
                </div> : <div className="rounded-lg border border-dashed px-4 py-10 text-center text-sm text-muted-foreground">当前计划尚未形成可展示的文章链。</div>}
              </div>
            </DialogContent>
          </Dialog>}
          <Card className="gap-0 py-0"><CardHeader className="border-b px-4 py-4"><div className="flex flex-wrap items-center justify-between gap-3"><div><CardTitle className="text-base">执行时间线</CardTitle><CardDescription>{timeline.length ? `已记录 ${timeline.length} 条公开状态事件。` : "SSE 事件只展示公开状态，不展示思维链。"}</CardDescription></div><Button type="button" variant="outline" size="sm" className="min-h-9" aria-expanded={timelineExpanded} aria-controls="workflow-timeline-details" onClick={() => setTimelineExpanded((current) => !current)}><ChevronDown className={`size-4 transition-transform duration-200 ${timelineExpanded ? "rotate-180" : ""}`} />{timelineExpanded ? "收起事件" : "展开事件"}</Button></div></CardHeader><CardContent id="workflow-timeline-details" className="px-4 py-4">{timelineExpanded ? (timeline.length ? <ol className="max-h-72 overflow-auto rounded-lg border bg-muted/10 p-3 text-sm">{timeline.map((item) => <li key={item} className="flex items-center gap-2 py-1"><CircleDot className="size-3 shrink-0 text-primary" />{item}</li>)}</ol> : <p className="text-sm text-muted-foreground">计划确认后会在这里显示状态事件。</p>) : <div className="rounded-lg border border-dashed bg-muted/20 px-3 py-3 text-sm text-muted-foreground" role="status">时间线已收起{timeline.length ? `，共 ${timeline.length} 条事件` : "，暂无事件"}。点击“展开事件”查看公开执行记录。</div>}</CardContent></Card>
        </div>
      </div>
    </main>
  );
}
