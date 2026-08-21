"use client";

/* eslint-disable react-hooks/set-state-in-effect */

import {
  AlertCircle,
  Check,
  CircleDot,
  Download,
  Loader2,
  MessageSquareText,
  Pause,
  Play,
  Plus,
  Send,
  Sparkles,
  Square,
  Workflow,
} from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Textarea } from "@/components/ui/textarea";
import { WorkflowAssistantAttachments } from "@/components/workflow-assistant-attachments";
import { ApiError, apiFileUrl, apiGet, apiPost } from "@/lib/api";
import {
  gapFillWorkflowAssistantPlan,
  getResearchRun,
} from "@/lib/research-api";
import type {
  AccessibleProject,
  AuthStatus,
  ProjectAssetDownload,
  ResearchRunDetail,
  TaskRecord,
  WorkflowAssistantConversation,
  WorkflowAssistantConversationList,
  WorkflowAssistantDispatch,
  WorkflowAssistantAttentionCount,
  WorkflowAssistantAttentionList,
  WorkflowAssistantGapFillResponse,
  WorkflowAssistantPlan,
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

const actionLabels: Record<string, string> = {
  list_projects: "读取项目目录",
  list_tasks: "读取文章任务",
  read_project_context: "读取项目上下文",
  evidence_query: "查询项目证据",
  read_plan_status: "读取计划状态",
  create_task: "创建文章任务",
  generate_titles: "生成标题",
  select_title: "选择标题",
  generate_products: "生成产品候选",
  confirm_products: "确认产品",
  generate_outline: "生成大纲",
  start_research: "启动研究",
  generate_article: "生成正文",
  humanize: "自动人化",
  review: "复检文章",
  restore_links: "恢复链接",
  prepare_images: "准备文章图片",
  export_docx: "导出 DOCX",
  generate_tdk: "生成 TDK",
  package_delivery: "生成交付包",
};

const stepStatusLabels: Record<string, string> = {
  pending: "待执行",
  running: "执行中",
  waiting_job: "等待 Job",
  waiting_review: "等待人工确认",
  succeeded: "已完成",
  failed: "失败",
  skipped: "已跳过",
  cancelled: "已取消",
};

// Planning can legitimately exceed the shared four-minute API timeout when
// the selected model performs deep reasoning over several projects. Keep a
// finite browser guard, but do not abort a healthy server-side planning call.
const WORKFLOW_ASSISTANT_PLANNING_TIMEOUT_MS = 30 * 60 * 1000;

function localId(prefix: string) {
  const random = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`;
  return `${prefix}-${random}`.replace(/[^A-Za-z0-9._:-]/g, "-");
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

function statusVariant(status: WorkflowAssistantPlan["status"]): "default" | "outline" | "secondary" | "destructive" {
  if (status === "failed") return "destructive";
  if (status === "awaiting_confirmation" || status === "waiting_review") return "outline";
  if (status === "completed") return "secondary";
  return "default";
}

const artifactDownloadEndpoints = {
  docx: { endpoint: "docx/download", label: "下载 Word" },
  tdk: { endpoint: "tdk/download", label: "下载 D.docx" },
  delivery_package: { endpoint: "delivery-package/download", label: "下载交付 ZIP" },
} as const;

type ScopedTask = TaskRecord & { project_id: string };

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
  const [revisionPending, setRevisionPending] = useState(false);
  const [artifactPending, setArtifactPending] = useState("");
  const [attentionCount, setAttentionCount] = useState(0);
  const [attentionPlans, setAttentionPlans] = useState<WorkflowAssistantPlan[]>([]);
  const [attachmentsEnabled, setAttachmentsEnabled] = useState(false);
  const [projectChangesEnabled, setProjectChangesEnabled] = useState(false);
  const [gapFillEnabled, setGapFillEnabled] = useState(false);
  const [error, setError] = useState("");
  const [timeline, setTimeline] = useState<string[]>([]);
  const [researchDetails, setResearchDetails] = useState<Record<string, ResearchRunDetail | null>>({});
  const [gapFillSelections, setGapFillSelections] = useState<Record<string, string[]>>({});
  const [gapFillPending, setGapFillPending] = useState<string | null>(null);
  const [gapFillQueueStatus, setGapFillQueueStatus] = useState<Record<string, string>>({});
  const gapFillRequestIdsRef = useRef<Record<string, string>>({});
  const eventSourceRef = useRef<EventSource | null>(null);
  const activeConversationIdRef = useRef<string | null>(null);
  const activePlanIdRef = useRef<string | null>(null);
  const loadedConversationRef = useRef<string | null>(null);
  const lastEventSequenceRef = useRef(0);
  const reconnectTimerRef = useRef<number | null>(null);
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
      lastEventSequenceRef.current = 0;
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
    if (!planId) {
      activePlanIdRef.current = null;
      return;
    }
    if (activePlanIdRef.current !== planId) {
      activePlanIdRef.current = planId;
      setTimeline([]);
      lastEventSequenceRef.current = 0;
    }
    eventSourceRef.current?.close();
    if (reconnectTimerRef.current !== null) {
      window.clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
    let disposed = false;
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
          return current.includes(item) ? current : [...current, item];
        });
        void apiGet<WorkflowAssistantPlan>(
          `/api/workflow-assistant/plans/${encodeURIComponent(planId)}`,
        ).then(setPlan).catch(() => {
          // The next SSE event or a manual reload will retry the projection.
        });
        void refreshAttention().catch(() => {
          // Plan progress remains usable if the inbox refresh is transiently
          // unavailable; the next event or page reload will retry it.
        });
      } catch {
        // Keep the timeline usable if a proxy emits a non-JSON keepalive.
      }
    };
    const connect = () => {
      if (disposed) return;
      const source = new EventSource(
        `${apiFileUrl(`/api/workflow-assistant/plans/${encodeURIComponent(planId)}/events/stream?after_sequence=${lastEventSequenceRef.current}`)}`,
        { withCredentials: true },
      );
      source.onmessage = handleEvent;
      source.addEventListener("workflow-assistant", handleEvent as EventListener);
      source.onerror = () => {
        source.close();
        if (!disposed) {
          reconnectTimerRef.current = window.setTimeout(connect, 1000);
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
    };
  }, [plan?.plan_id, refreshAttention]);

  const conversationMessages = selectedConversation?.messages || [];
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
    setError("");
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
      pendingMessageDispatchRef.current = null;
      setPlan(response.plan);
      void refreshAttention().catch(() => undefined);
      setDraft("");
      const refreshed = await apiGet<WorkflowAssistantConversation>(
        `/api/workflow-assistant/conversations/${encodeURIComponent(conversation.conversation_id)}`,
      );
      setSelectedConversation(refreshed);
      setConversations((current) => current.map((item) => item.conversation_id === refreshed.conversation_id ? refreshed : item));
    } catch (nextError) {
      setError(messageText(nextError));
    } finally {
      setPending(false);
    }
  }

  async function changePlan(
    action: "confirm" | "pause" | "resume" | "cancel",
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

  async function downloadArtifact(step: WorkflowAssistantStep) {
    const artifactKind = step.output_summary.artifact_kind;
    if (
      typeof artifactKind !== "string" ||
      !(artifactKind in artifactDownloadEndpoints) ||
      !step.article_task_id
    ) {
      return;
    }
    const artifact = artifactDownloadEndpoints[artifactKind as keyof typeof artifactDownloadEndpoints];
    const key = `${step.step_id}:${artifactKind}`;
    setArtifactPending(key);
    setError("");
    try {
      const download = await apiGet<ProjectAssetDownload>(
        `/api/projects/${encodeURIComponent(step.project_id)}/tasks/${encodeURIComponent(step.article_task_id)}/${artifact.endpoint}`,
      );
      if (!download.url) throw new Error("服务器没有返回可用的短期下载地址。");
      window.location.assign(download.url);
    } catch (nextError) {
      setError(messageText(nextError));
    } finally {
      setArtifactPending("");
    }
  }

  async function openAttentionPlan(nextPlan: WorkflowAssistantPlan) {
    setError("");
    try {
      const freshPlan = await apiGet<WorkflowAssistantPlan>(
        `/api/workflow-assistant/plans/${encodeURIComponent(nextPlan.plan_id)}`,
      );
      setPlan(freshPlan);
      try {
        const conversation = await apiGet<WorkflowAssistantConversation>(
          `/api/workflow-assistant/conversations/${encodeURIComponent(nextPlan.conversation_id)}`,
        );
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
            <ScrollArea className="min-h-0 flex-1 rounded-xl border bg-background p-4">
              <div className="grid gap-3">
                {conversationMessages.length ? conversationMessages.map((message) => (
                  <div key={message.message_id} className={`max-w-[92%] rounded-xl px-3 py-2.5 text-sm leading-6 ${message.role === "user" ? "ml-auto bg-primary text-primary-foreground" : "bg-muted"}`}>
                    {message.content}
                  </div>
                )) : <div className="flex min-h-52 flex-col items-center justify-center text-center text-sm text-muted-foreground"><Workflow className="mb-3 size-8" /><p className="font-medium text-foreground">从一个明确的问题开始</p><p className="mt-1 max-w-sm">例如：读取两个项目未开始的文章任务，并为每个项目规划两篇文章。</p></div>}
              </div>
            </ScrollArea>
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
              <Textarea value={draft} onChange={(event) => setDraft(event.target.value)} placeholder="描述你想查询或计划的文章工作…" rows={4} disabled={pending} onKeyDown={(event) => { if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); void sendMessage(); } }} />
              <div className="flex items-center justify-between gap-3"><span className="text-xs text-muted-foreground">Ctrl/Cmd + Enter 发送 · 不会显示原始提示词或模型思维链</span><Button type="button" onClick={() => void sendMessage()} disabled={pending || !draft.trim() || !selectedProjectIds.length}>{pending ? <Loader2 className="animate-spin" /> : <Send />}发送</Button></div>
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
                  className="rounded-lg border px-3 py-2 text-left transition-colors hover:bg-muted"
                >
                  <span className="flex items-center justify-between gap-2 text-sm font-medium">
                    <span className="truncate">{attentionPlan.title}</span>
                    <Badge variant={statusVariant(attentionPlan.status)}>
                      {statusLabels[attentionPlan.status]}
                    </Badge>
                  </span>
                  <span className="mt-1 block truncate text-xs text-muted-foreground">
                    {attentionPlan.project_ids.join("、")} · {attentionPlan.attention_state}
                  </span>
                </button>
              ))}
            </CardContent>
          </Card>}
          <Card className="gap-0 py-0">
            <CardHeader className="border-b px-4 py-4"><div className="flex items-center justify-between gap-3"><div><CardTitle className="text-base">计划预览</CardTitle><CardDescription className="mt-1">确认后才会进入写操作队列。</CardDescription></div>{plan && <Badge variant={statusVariant(plan.status)}>{statusLabels[plan.status]}</Badge>}</div></CardHeader>
            <CardContent className="px-4 py-4">
              {plan ? <div className="grid gap-4">
                <div><p className="font-medium">{plan.title}</p><p className="mt-1 text-xs leading-5 text-muted-foreground">计划 Revision {plan.revision} · 并发上限 {plan.concurrency_limit}{plan.budget_warning ? " · 接近软预算" : ""}</p></div>
                <ol className="grid gap-2">
                  {plan.steps.map((step) => {
                    const artifactKind = typeof step.output_summary.artifact_kind === "string" ? step.output_summary.artifact_kind : "";
                    const artifact = artifactKind in artifactDownloadEndpoints ? artifactDownloadEndpoints[artifactKind as keyof typeof artifactDownloadEndpoints] : null;
                    const artifactKey = `${step.step_id}:${artifactKind}`;
                    const pendingAiConfirmation = step.output_summary.pending_ai_confirmation === true;
                    const researchReviewRequired = step.action_kind === "start_research" && step.status === "waiting_review";
                    const plannedTopic = typeof step.input_summary.topic === "string" ? step.input_summary.topic : "";
                    return <li key={step.step_id} className="flex items-start gap-2 rounded-lg border px-3 py-2 text-sm"><span className="mt-0.5 flex size-5 shrink-0 items-center justify-center rounded-full bg-muted text-xs font-semibold">{step.sequence}</span><span className="min-w-0"><span className="flex items-center gap-2 font-medium"><span>{actionLabels[step.action_kind] || step.action_kind}</span><Badge variant="outline">{stepStatusLabels[step.status] || step.status}</Badge>{researchReviewRequired && <Badge variant="outline">待审查研究来源</Badge>}{pendingAiConfirmation && <Badge variant="outline">待确认交付包</Badge>}</span><span className="block text-xs text-muted-foreground">{step.project_id}{step.article_task_id ? ` · ${step.article_task_id}` : ""}{step.action_kind === "create_task" && plannedTopic ? ` · 主题：${plannedTopic}` : ""}{step.background_job_id ? ` · Job ${step.background_job_id}` : ""}{step.retry_count ? ` · 重试 ${step.retry_count}` : ""}{step.hard_gate && !step.human_gate_confirmed ? " · 需要人工确认" : ""}{step.standardized_error_code ? ` · ${step.standardized_error_code}` : ""}</span>{Object.keys(step.output_summary).length > 0 && <span className="mt-1 block truncate text-xs text-muted-foreground">结果：{JSON.stringify(step.output_summary)}</span>}<span className="flex flex-wrap gap-2">{artifact && step.article_task_id && <Button type="button" size="sm" variant="outline" className="mt-2" onClick={() => void downloadArtifact(step)} disabled={Boolean(artifactPending)}>{artifactPending === artifactKey ? <Loader2 className="animate-spin" /> : <Download />}{artifact.label}</Button>}{researchReviewRequired && step.article_task_id && <Link href={`/projects/${encodeURIComponent(step.project_id)}/articles/${encodeURIComponent(step.article_task_id)}?step=outline`} className={buttonVariants({ variant: "outline", size: "sm", className: "mt-2" })}>打开研究候选审查</Link>}{pendingAiConfirmation && step.article_task_id && <Link href={`/projects/${encodeURIComponent(step.project_id)}/articles/${encodeURIComponent(step.article_task_id)}?step=review`} className={buttonVariants({ variant: "outline", size: "sm", className: "mt-2" })}>打开文章工作台并提交人工截图</Link>}</span></span></li>;
                  })}
                </ol>
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
                {plan.status === "awaiting_confirmation" && <Button type="button" onClick={() => void changePlan("confirm")} disabled={pending}><Check />确认计划并排队</Button>}
                {plan.status === "waiting_review" && <div className="flex flex-wrap gap-2"><Button type="button" onClick={() => void changePlan("confirm")} disabled={pending}><Check />确认并继续</Button><Button type="button" variant="destructive" onClick={() => void changePlan("cancel")} disabled={pending}><Square />取消</Button></div>}
                {plan.status === "queued" || plan.status === "running" ? <div className="flex flex-wrap gap-2"><Button type="button" variant="outline" onClick={() => void changePlan("pause")} disabled={pending}><Pause />暂停</Button><Button type="button" variant="destructive" onClick={() => void changePlan("cancel")} disabled={pending}><Square />取消</Button></div> : null}
                {(plan.status === "queued" || plan.status === "running" || plan.status === "waiting_review") && plan.project_ids.length > 1 && <div className="grid gap-2 rounded-lg border border-dashed p-3"><span className="text-xs font-medium text-muted-foreground">项目执行通道</span><div className="flex flex-wrap gap-2">{plan.project_ids.map((projectId) => { const paused = plan.paused_project_ids.includes(projectId); return <Button key={projectId} type="button" size="sm" variant="outline" onClick={() => void changePlan(paused ? "resume" : "pause", [projectId])} disabled={pending}>{paused ? <Play /> : <Pause />}{paused ? `恢复 ${projectId}` : `暂停 ${projectId}`}</Button>; })}</div></div>}
                {plan.status === "paused" && <div className="flex flex-wrap gap-2"><Button type="button" onClick={() => void changePlan("resume")} disabled={pending}><Play />恢复</Button><Button type="button" variant="destructive" onClick={() => void changePlan("cancel")} disabled={pending}><Square />取消</Button></div>}
                {["draft", "awaiting_confirmation", "paused", "waiting_review", "failed"].includes(plan.status) && <div className="grid gap-2 rounded-lg border border-dashed p-3"><Label htmlFor="workflow-plan-revision">调整未完成步骤</Label><Textarea id="workflow-plan-revision" value={revisionDraft} onChange={(event) => setRevisionDraft(event.target.value)} placeholder="例如：保留已完成步骤，只把未完成正文改成面向采购团队。" rows={3} disabled={revisionPending || pending} /><div className="flex items-center justify-between gap-2"><span className="text-xs text-muted-foreground">会生成新的 Revision，并要求重新确认。</span><Button type="button" variant="outline" onClick={() => void revisePlan()} disabled={revisionPending || pending || !revisionDraft.trim()}>{revisionPending ? <Loader2 className="animate-spin" /> : <Workflow />}生成修订预览</Button></div></div>}
              </div> : <div className="py-10 text-center text-sm text-muted-foreground"><CircleDot className="mx-auto mb-3 size-7" /><p>发送请求后，这里会显示结构化计划。</p></div>}
            </CardContent>
          </Card>
          <Card className="gap-0 py-0"><CardHeader className="border-b px-4 py-4"><CardTitle className="text-base">执行时间线</CardTitle><CardDescription>SSE 事件只展示公开状态，不展示思维链。</CardDescription></CardHeader><CardContent className="px-4 py-4">{timeline.length ? <ol className="grid gap-2 text-sm">{timeline.map((item) => <li key={item} className="flex items-center gap-2"><CircleDot className="size-3 text-primary" />{item}</li>)}</ol> : <p className="text-sm text-muted-foreground">计划确认后会在这里显示状态事件。</p>}</CardContent></Card>
        </div>
      </div>
    </main>
  );
}
