"use client";

/* eslint-disable react-hooks/set-state-in-effect */

import {
  AlertCircle,
  Check,
  ChevronDown,
  CircleDot,
  ExternalLink,
  Loader2,
  MessageSquareText,
  Pause,
  Play,
  Plus,
  RotateCcw,
  Send,
  Settings2,
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
import {
  gapFillWorkflowAssistantPlan,
  getResearchRun,
} from "@/lib/research-api";
import { WORKFLOW_STEP_LABELS } from "@/lib/workflow-steps";
import type {
  AccessibleProject,
  AuthStatus,
  ResearchRunDetail,
  WorkflowAssistantConversation,
  WorkflowAssistantConversationList,
  WorkflowAssistantDispatch,
  WorkflowAssistantAttentionList,
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

const workflowStepStatusLabels: Record<string, string> = {
  pending: "待执行",
  running: "执行中",
  waiting_job: "排队中",
  waiting_review: "待人工处理",
  succeeded: "已完成",
  failed: "失败",
  skipped: "已跳过",
  cancelled: "已取消",
};

const ARTICLE_WORKFLOW_ACTIONS = new Set([
  "create_task",
  "generate_titles",
  "select_title",
  "generate_products",
  "confirm_products",
  "generate_outline",
  "start_research",
  "generate_article",
  "review",
  "humanize",
  "restore_links",
  "prepare_images",
  "export_docx",
  "generate_tdk",
  "package_delivery",
]);

function isArticleWorkflowPlan(plan: WorkflowAssistantPlan | null): boolean {
  return Boolean(
    plan?.steps.some((step) => ARTICLE_WORKFLOW_ACTIONS.has(step.action_kind)),
  );
}

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
      if (dispatchUrl) {
        // A queued lease is the only changing state while the planner works.
        // Polling its compact projection avoids three full reads every two
        // seconds; fetch the conversation and plan once the lease is done.
        const dispatch = await apiGet<WorkflowAssistantDispatch>(dispatchUrl).catch(
          () => null,
        );
        if (dispatch?.dispatch_status === "failed") {
          const conversation = await apiGet<WorkflowAssistantConversation>(
            `/api/workflow-assistant/conversations/${encodeURIComponent(conversationId)}`,
          );
          return {
            conversation,
            plan: dispatch.plan,
            error: dispatch.dispatch_error_code
              ? `计划生成失败（${dispatch.dispatch_error_code}），可以重新发送请求重试。`
              : "计划生成失败，可以重新发送请求重试。",
          };
        }
        if (dispatch?.dispatch_status === "succeeded" || dispatch?.message) {
          const [conversation, fallbackPlan] = await Promise.all([
            apiGet<WorkflowAssistantConversation>(
              `/api/workflow-assistant/conversations/${encodeURIComponent(conversationId)}`,
            ),
            apiGet<WorkflowAssistantPlan | null>(
              `/api/workflow-assistant/conversations/${encodeURIComponent(conversationId)}/latest-plan`,
            ).catch(() => null),
          ]);
          return { conversation, plan: dispatch.plan || fallbackPlan || null };
        }
      } else {
        // Compatibility fallback for a lost response before a dispatch ID
        // exists. This path is rarely used and remains bounded by the same
        // durable recovery deadline.
        const [conversation, latestPlan] = await Promise.all([
          apiGet<WorkflowAssistantConversation>(
            `/api/workflow-assistant/conversations/${encodeURIComponent(conversationId)}`,
          ),
          apiGet<WorkflowAssistantPlan | null>(
            `/api/workflow-assistant/conversations/${encodeURIComponent(conversationId)}/latest-plan`,
          ).catch(() => null),
        ]);
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
        if (planMatchesRequest || hasAssistantReply) {
          return { conversation, plan: planMatchesRequest ? latestPlan : null };
        }
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

type AssistantRequestPhase = "idle" | "sending" | "waiting_reply" | "refreshing";
type PendingUserMessage = { conversationId: string; content: string };

export function WorkflowAssistantWorkspace() {
  const [projects, setProjects] = useState<AccessibleProject[]>([]);
  const [conversations, setConversations] = useState<WorkflowAssistantConversation[]>([]);
  const [selectedConversation, setSelectedConversation] = useState<WorkflowAssistantConversation | null>(null);
  const [selectedProjectIds, setSelectedProjectIds] = useState<string[]>([]);
  const [plan, setPlan] = useState<WorkflowAssistantPlan | null>(null);
  const [draft, setDraft] = useState("");
  const [revisionDraft, setRevisionDraft] = useState("");
  const [loading, setLoading] = useState(true);
  const [pending, setPending] = useState(false);
  const [requestPhase, setRequestPhase] = useState<AssistantRequestPhase>("idle");
  const [pendingUserMessage, setPendingUserMessage] = useState<PendingUserMessage | null>(null);
  const [scopeDialogOpen, setScopeDialogOpen] = useState(false);
  const [planPreviewOpen, setPlanPreviewOpen] = useState(false);
  const [revisionPending, setRevisionPending] = useState(false);
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
    const response = await apiGet<WorkflowAssistantAttentionList>(
      "/api/workflow-assistant/attention",
    );
    setAttentionCount(response.count ?? response.plans.length);
    setAttentionPlans(response.plans);
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
      setProjects(nextProjects);
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
        // Keep the last durable cards visible during a transient inbox
        // failure; clearing them makes a refresh look like data loss.
      });
      setSelectedConversation((current) => current || nextConversations.conversations[0] || null);
      setSelectedProjectIds((current) => current.length ? current : nextProjects.map((project) => project.project_id));
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
      setScopeDialogOpen(false);
      setTimeline([]);
      setTimelineExpanded(false);
      lastEventSequenceRef.current = 0;
      reconnectAttemptRef.current = 0;
    }
  }, [projects, selectedConversation?.conversation_id, selectedConversationProjectIds]);

  useEffect(() => {
    const conversationId = selectedConversation?.conversation_id;
    if (!conversationId || loadedConversationRef.current === conversationId) return;
    loadedConversationRef.current = conversationId;
    let disposed = false;
    void Promise.all([
      apiGet<WorkflowAssistantConversation>(
        `/api/workflow-assistant/conversations/${encodeURIComponent(conversationId)}`,
      ),
      apiGet<WorkflowAssistantPlan | null>(
        `/api/workflow-assistant/conversations/${encodeURIComponent(conversationId)}/latest-plan`,
      ),
      apiGet<WorkflowAssistantDispatch | null>(
        `/api/workflow-assistant/conversations/${encodeURIComponent(conversationId)}/active-dispatch`,
      ).catch(() => null),
    ]).then(([conversation, latestPlan, activeDispatch]) => {
      if (disposed) return;
      setSelectedConversation(conversation);
      setConversations((current) => current.map((item) => (
        item.conversation_id === conversation.conversation_id ? conversation : item
      )));
      setPlan((current) => (
        current?.conversation_id === conversationId ? current : latestPlan
      ));
      if (
        activeDispatch?.dispatch_id
        && (activeDispatch.dispatch_status === "queued" || activeDispatch.dispatch_status === "running")
      ) {
        const lastUserMessage = [...conversation.messages]
          .reverse()
          .find((message) => message.role === "user");
        const content = lastUserMessage?.content || "";
        setPending(true);
        setRequestPhase("waiting_reply");
        if (content) {
          setPendingUserMessage({ conversationId, content });
        }
        void recoverDispatchResult(
          conversationId,
          content,
          latestPlan?.plan_id || null,
          activeDispatch.dispatch_id,
        ).then((recovered) => {
          if (disposed || activeConversationIdRef.current !== conversationId) return;
          if (!recovered) {
            setError("计划仍在后台生成，稍后会自动同步结果。");
            return;
          }
          if (recovered.error) {
            setError(recovered.error);
            return;
          }
          if (recovered.plan) setPlan(recovered.plan);
          setSelectedConversation(recovered.conversation);
          setConversations((current) => current.map((item) => (
            item.conversation_id === recovered.conversation.conversation_id
              ? recovered.conversation
              : item
          )));
          setPendingUserMessage(null);
          void refreshAttention().catch(() => undefined);
        }).catch((nextError) => {
          if (!disposed) setError(messageText(nextError));
        }).finally(() => {
          if (!disposed && activeConversationIdRef.current === conversationId) {
            setPending(false);
            setRequestPhase("idle");
          }
        });
      } else if (activeDispatch?.dispatch_status === "failed") {
        if (activeDispatch.plan) setPlan(activeDispatch.plan);
        setError(activeDispatch.dispatch_error_code
          ? `计划生成失败（${activeDispatch.dispatch_error_code}），可以重新发送请求重试。`
          : "计划生成失败，可以重新发送请求重试。");
      }
    }).catch((nextError) => {
      if (!disposed) setError(messageText(nextError));
    });
    return () => {
      disposed = true;
    };
  }, [refreshAttention, selectedConversation?.conversation_id]);

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
  const hasArticleWorkflow = isArticleWorkflowPlan(plan);
  const waitingResearchSteps = useMemo(
    () => (plan?.steps || []).filter(
      (step) => step.action_kind === "start_research" && step.status === "waiting_review",
    ),
    [plan],
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
        title: "新的提示词与资料会话",
        project_ids: selectedProjectIds,
      });
      setConversations((current) => [created, ...current]);
      setSelectedConversation(created);
      setPlan(null);
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
      setRevisionDraft("");
      void refreshAttention().catch(() => undefined);
    } catch (nextError) {
      setError(messageText(nextError, plan));
    } finally {
      setRevisionPending(false);
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

  const planQuickActions = plan && !hasArticleWorkflow && (
    <div className="flex flex-wrap items-center justify-end gap-2" aria-label="计划快速操作">
      {(plan.status === "awaiting_confirmation" || plan.status === "waiting_review") && (
        <Button type="button" size="sm" variant="destructive" onClick={() => void changePlan("cancel")} disabled={pending}>
          <Square />取消计划
        </Button>
      )}
      {(plan.status === "queued" || plan.status === "running") && (
        <>
          <Button type="button" size="sm" variant="outline" onClick={() => void changePlan("pause")} disabled={pending}>
            <Pause />暂停
          </Button>
          <Button type="button" size="sm" variant="destructive" onClick={() => void changePlan("cancel")} disabled={pending}>
            <Square />取消
          </Button>
        </>
      )}
      {plan.status === "paused" && (
        <>
          <Button type="button" size="sm" onClick={() => void changePlan("resume")} disabled={pending}>
            <Play />恢复
          </Button>
          <Button type="button" size="sm" variant="destructive" onClick={() => void changePlan("cancel")} disabled={pending}>
            <Square />取消
          </Button>
        </>
      )}
    </div>
  );

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
              <h1 className="text-xl font-semibold">提示词与资料助手</h1>
              <p className="text-sm text-muted-foreground">配置项目提示词、查询项目资料；文章生成请前往批量写文章或文章工作台。</p>
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
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <CardTitle>提示词与资料助手</CardTitle>
                <CardDescription>用于配置项目提示词和查询已发布资料，不负责生成文章或执行文章批次。</CardDescription>
              </div>
            </div>
          </CardHeader>
          <CardContent className="flex min-h-[570px] flex-col gap-4 px-5 py-5">
            <div className="rounded-xl border bg-muted/30 p-4">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div className="min-w-0">
                  <Label>查询与配置范围</Label>
                  <p className="mt-1 text-sm font-medium">
                    {selectedProjects.length ? `${selectedProjects.length} 个项目` : "未选择项目"}
                  </p>
                  <p className="mt-1 truncate text-xs text-muted-foreground" title={selectedProjects.map((project) => project.customer_name).join("、")}>
                    {selectedProjects.length ? selectedProjects.map((project) => project.customer_name).join("、") : "请选择至少一个项目"}
                  </p>
                </div>
                <Button type="button" variant="outline" size="sm" className="min-h-9 shrink-0" aria-haspopup="dialog" onClick={() => setScopeDialogOpen(true)}>
                  <Settings2 />设置范围
                </Button>
              </div>
              <p className="mt-3 text-xs leading-5 text-muted-foreground">
                项目范围用于限定资料查询与提示词配置；文章生成、复检和导出请在批量写文章或文章工作台完成。
              </p>
            </div>
            <Dialog open={scopeDialogOpen} onOpenChange={setScopeDialogOpen}>
              <DialogContent className="h-[min(760px,calc(100vh-2rem))] w-[calc(100vw-2rem)] max-w-4xl sm:max-w-4xl grid-rows-[auto_minmax(0,1fr)] overflow-hidden p-0">
                <DialogHeader className="border-b px-5 py-4 pr-12">
                  <DialogTitle>查询与配置范围</DialogTitle>
                  <DialogDescription>
                    选择本次资料查询或提示词配置要覆盖的项目。
                  </DialogDescription>
                </DialogHeader>
                <div className="min-h-0 overflow-y-auto px-5 pb-5">
                  <div className="grid gap-5 pt-5">
                    <section className="grid gap-3" aria-labelledby="workflow-scope-projects">
                      <div className="flex items-center justify-between gap-3">
                        <Label id="workflow-scope-projects">项目范围</Label>
                        <span className="text-xs text-muted-foreground">已选 {selectedProjects.length} 个项目</span>
                      </div>
                      <div className="grid gap-2 sm:grid-cols-2">
                        {projects.map((project) => {
                          const checked = selectedProjectIds.includes(project.project_id);
                          return (
                            <label key={project.project_id} className="flex cursor-pointer items-start gap-2 rounded-lg border bg-background px-3 py-3 text-sm transition-colors hover:bg-muted/50">
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
                    </section>
                  </div>
                </div>
              </DialogContent>
            </Dialog>
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
                  </> : <div className="flex min-h-52 flex-col items-center justify-center text-center text-sm text-muted-foreground"><MessageSquareText className="mb-3 size-8" /><p className="font-medium text-foreground">配置提示词，或查询项目资料</p><p className="mt-1 max-w-sm">例如：把这段要求整理成正文提示词，或查询项目知识库里的产品参数。</p></div>}
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
                <span className="text-muted-foreground">{requestPhase === "sending" ? "正在提交当前项目范围。" : requestPhase === "waiting_reply" ? "助手正在处理请求，页面不会重复提交。" : "正在刷新会话和配置预览。"}</span>
              </div>}
              <Textarea value={draft} onChange={(event) => setDraft(event.target.value)} placeholder="例如：把这段要求整理成正文提示词，或查询项目资料…" rows={4} disabled={pending} onKeyDown={(event) => { if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); void sendMessage(); } }} />
              <p className="text-xs leading-5 text-muted-foreground">可以让我整理、优化和配置项目提示词，也可以查询当前项目的已发布资料；文章生成、复检和导出请使用批量写文章或文章工作台。</p>
              <div className="flex items-center justify-between gap-3"><span className="text-xs text-muted-foreground">Ctrl/Cmd + Enter 发送 · 不会显示原始提示词或模型思维链</span><Button type="button" onClick={() => void sendMessage()} disabled={pending || !draft.trim() || !selectedProjectIds.length}>{pending ? <Loader2 className="workflow-assistant-spinner" /> : <Send />}{requestPhase === "sending" ? "正在发送" : requestPhase === "waiting_reply" ? "等待回复" : requestPhase === "refreshing" ? "更新结果" : pending ? "处理中" : "发送"}</Button></div>
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
                  <CardTitle className="text-base">配置变更预览</CardTitle>
                  <CardDescription className="mt-1">提示词或项目配置变更会先生成提案，确认后才会写入。</CardDescription>
                </div>
                <div className="flex flex-wrap items-center justify-end gap-2">
                  {plan && <Badge variant={statusVariant(plan.status)}>{statusLabels[plan.status]}</Badge>}
                  {planQuickActions}
                  {plan && !hasArticleWorkflow && <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    className="min-h-9"
                    aria-haspopup="dialog"
                    onClick={() => setPlanPreviewOpen(true)}
                  >
                    <Workflow />查看变更
                  </Button>}
                  {plan && hasArticleWorkflow && <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    className="min-h-9"
                    nativeButton={false}
                    render={
                      <Link
                        href={`/batch-writing?plan_id=${encodeURIComponent(plan.plan_id)}&conversation_id=${encodeURIComponent(plan.conversation_id)}`}
                      />
                    }
                  >
                    <ExternalLink />去批量写文章
                  </Button>}
                </div>
              </div>
            </CardHeader>
            <CardContent className="px-4 py-4">
              {plan ? <div>
                <p className="font-medium">{plan.title}</p>
                {hasArticleWorkflow ? (
                  <div className="mt-3 rounded-lg border border-dashed border-amber-500/40 bg-amber-50/60 px-3 py-3 text-sm text-amber-900 dark:bg-amber-950/20 dark:text-amber-100" role="status">
                    这是历史文章计划，文章生成与批次控制已迁移到批量写文章页面；助手不再执行或导出文章。
                  </div>
                ) : (
                  <>
                    <p className="mt-1 text-xs leading-5 text-muted-foreground">
                      Revision {plan.revision} · {plan.steps.length} 个配置步骤{plan.budget_warning ? " · 接近软预算" : ""}
                    </p>
                    <p className="mt-3 rounded-lg border border-dashed bg-muted/20 px-3 py-3 text-sm text-muted-foreground" role="status">
                      点击“查看变更”查看配置提案和确认操作；知识库查询会直接回复，不生成执行计划。
                    </p>
                  </>
                )}
              </div> : <div className="py-6 text-center text-sm text-muted-foreground"><CircleDot className="mx-auto mb-3 size-7" /><p>发送请求后，这里会显示结构化计划。</p></div>}
            </CardContent>
          </Card>
          {plan && !hasArticleWorkflow && <Dialog open={planPreviewOpen} onOpenChange={setPlanPreviewOpen}>
            <DialogContent className="h-[min(900px,calc(100vh-2rem))] w-[calc(100vw-2rem)] max-w-7xl sm:max-w-7xl grid-rows-[auto_minmax(0,1fr)] overflow-hidden p-0">
              <DialogHeader className="border-b px-5 py-4 pr-12">
                <DialogTitle>配置变更详情</DialogTitle>
                <DialogDescription>
                  {plan.title} · Revision {plan.revision} · {plan.steps.length} 个配置步骤
                </DialogDescription>
              </DialogHeader>
              <div className="min-h-0 overflow-y-auto px-5 pb-5">
                <Card className="gap-0 border-0 py-0 shadow-none">
                  <CardHeader className="border-b px-0 py-4">
                    <div className="flex flex-wrap items-center justify-between gap-3">
                      <div>
                        <CardTitle className="text-base">配置变更详情</CardTitle>
                        <CardDescription className="mt-1">查看配置步骤和确认操作。</CardDescription>
                      </div>
                      <div className="flex items-center gap-2">
                        <Badge variant={statusVariant(plan.status)}>{statusLabels[plan.status]}</Badge>
                      </div>
                    </div>
                  </CardHeader>
                  <CardContent id="workflow-plan-details" className="px-0 py-4">
              {plan ? <div className="grid gap-4">
                <div>
                  <p className="font-medium">{plan.title}</p>
                  <p className="mt-1 text-xs leading-5 text-muted-foreground">
                    配置 Revision {plan.revision} · {plan.steps.length} 个步骤{plan.budget_warning ? " · 接近软预算" : ""}
                  </p>
                </div>
                <div className="grid gap-2 rounded-lg border bg-background p-3">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div>
                      <p className="text-sm font-medium">配置步骤</p>
                      <p className="mt-1 text-xs text-muted-foreground">确认后才会写入项目配置；资料查询类请求会直接回复。</p>
                    </div>
                    <Badge variant="outline">当前 {plan.steps.length} 步</Badge>
                  </div>
                  <ol className="grid gap-2">
                    {plan.steps.map((step) => (
                      <li key={step.step_id} className="flex items-center gap-2 rounded-lg border px-3 py-2 text-sm">
                        <span className="flex size-7 shrink-0 items-center justify-center rounded-full bg-muted text-xs font-semibold">{step.sequence}</span>
                        <span className="min-w-0 flex-1">
                          <span className="block truncate font-medium">{WORKFLOW_STEP_LABELS[step.action_kind] || step.action_kind}</span>
                          <span className="mt-0.5 block truncate text-xs text-muted-foreground">{step.project_id}</span>
                        </span>
                        <Badge variant={step.status === "failed" ? "destructive" : step.status === "succeeded" ? "secondary" : "outline"}>
                          {workflowStepStatusLabels[step.status] || step.status}
                        </Badge>
                      </li>
                    ))}
                  </ol>
                </div>
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
                {plan.status === "awaiting_confirmation" && <div className="flex flex-wrap gap-2"><Button type="button" onClick={() => void changePlan("confirm")} disabled={pending}><Check />确认计划并排队</Button></div>}
                {plan.status === "waiting_review" && <div className="flex flex-wrap gap-2"><Button type="button" onClick={() => void changePlan("confirm")} disabled={pending}><Check />确认并继续</Button></div>}
                {(plan.status === "queued" || plan.status === "running" || plan.status === "waiting_review") && plan.project_ids.length > 1 && <div className="grid gap-2 rounded-lg border border-dashed p-3"><span className="text-xs font-medium text-muted-foreground">项目执行通道</span><div className="flex flex-wrap gap-2">{plan.project_ids.map((projectId) => { const paused = plan.paused_project_ids.includes(projectId); return <Button key={projectId} type="button" size="sm" variant="outline" onClick={() => void changePlan(paused ? "resume" : "pause", [projectId])} disabled={pending}>{paused ? <Play /> : <Pause />}{paused ? `恢复 ${projectId}` : `暂停 ${projectId}`}</Button>; })}</div></div>}
                {plan.status === "failed" && <div className="grid gap-2 rounded-lg border border-dashed border-destructive/40 bg-destructive/5 p-3"><div className="text-sm text-muted-foreground">已完成的配置步骤不会重复执行，只会重新排队失败步骤。</div><Button type="button" variant="outline" className="justify-self-start" onClick={() => void changePlan("retry")} disabled={pending}><RotateCcw />重试失败步骤</Button></div>}
                {["draft", "awaiting_confirmation", "paused", "waiting_review", "failed"].includes(plan.status) && <div className="grid gap-2 rounded-lg border border-dashed p-3"><Label htmlFor="workflow-plan-revision">调整未完成的配置变更</Label><Textarea id="workflow-plan-revision" value={revisionDraft} onChange={(event) => setRevisionDraft(event.target.value)} placeholder="例如：保留已完成内容，只调整正文提示词中的语气。" rows={3} disabled={revisionPending || pending} /><div className="flex items-center justify-between gap-2"><span className="text-xs text-muted-foreground">会生成新的 Revision，并要求重新确认。</span><Button type="button" variant="outline" onClick={() => void revisePlan()} disabled={revisionPending || pending || !revisionDraft.trim()}>{revisionPending ? <Loader2 className="animate-spin" /> : <Workflow />}生成修订预览</Button></div></div>}
              </div> : <div className="py-10 text-center text-sm text-muted-foreground"><CircleDot className="mx-auto mb-3 size-7" /><p>发送请求后，这里会显示结构化计划。</p></div>}
                  </CardContent>
                </Card>
              </div>
            </DialogContent>
          </Dialog>}
          <Card className="gap-0 py-0"><CardHeader className="border-b px-4 py-4"><div className="flex flex-wrap items-center justify-between gap-3"><div><CardTitle className="text-base">执行时间线</CardTitle><CardDescription>{timeline.length ? `已记录 ${timeline.length} 条公开状态事件。` : "SSE 事件只展示公开状态，不展示思维链。"}</CardDescription></div><Button type="button" variant="outline" size="sm" className="min-h-9" aria-expanded={timelineExpanded} aria-controls="workflow-timeline-details" onClick={() => setTimelineExpanded((current) => !current)}><ChevronDown className={`size-4 transition-transform duration-200 ${timelineExpanded ? "rotate-180" : ""}`} />{timelineExpanded ? "收起事件" : "展开事件"}</Button></div></CardHeader><CardContent id="workflow-timeline-details" className="px-4 py-4">{timelineExpanded ? (timeline.length ? <ol className="max-h-72 overflow-auto rounded-lg border bg-muted/10 p-3 text-sm">{timeline.map((item) => <li key={item} className="flex items-center gap-2 py-1"><CircleDot className="size-3 shrink-0 text-primary" />{item}</li>)}</ol> : <p className="text-sm text-muted-foreground">计划确认后会在这里显示状态事件。</p>) : <div className="rounded-lg border border-dashed bg-muted/20 px-3 py-3 text-sm text-muted-foreground" role="status">时间线已收起{timeline.length ? `，共 ${timeline.length} 条事件` : "，暂无事件"}。点击“展开事件”查看公开执行记录。</div>}</CardContent></Card>
        </div>
      </div>
    </main>
  );
}
