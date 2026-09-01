"use client";

import {
  AlertCircle,
  Check,
  Download,
  FileText,
  Loader2,
  Paperclip,
  RefreshCw,
  Save,
  Trash2,
} from "lucide-react";
import Link from "next/link";
import { ChangeEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { ApiError, apiFileUrl, apiGet, apiPost, apiUploadWithProgress } from "@/lib/api";
import { formatProjectDate } from "@/lib/project-date";
import {
  cancelWorkflowAssistantImportProposal,
  classifyWorkflowAssistantAttachment,
  confirmWorkflowAssistantImportProposal,
  createWorkflowAssistantImportProposal,
  getWorkflowAssistantAttachmentJob,
  getWorkflowAssistantAttachmentReview,
  reviseWorkflowAssistantImportProposal,
} from "@/lib/workflow-assistant-attachment-api";
import type {
  PromptKind,
  WorkflowAssistantAttachment,
  WorkflowAssistantAttachmentClassification,
  WorkflowAssistantAttachmentJob,
  WorkflowAssistantAttachmentList,
  WorkflowAssistantAttachmentReviewResponse,
  WorkflowAssistantAttachmentStatus,
  WorkflowAssistantImportProposal,
  WorkflowAssistantImportTargetKind,
} from "@/types";

const MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024;
const ALLOWED_EXTENSIONS = new Set(["pdf", "docx", "xlsx", "xlsm", "txt", "md"]);
const MIME_BY_EXTENSION: Record<string, string> = {
  pdf: "application/pdf",
  docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  xlsx: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  xlsm: "application/vnd.ms-excel.sheet.macroenabled.12",
  txt: "text/plain",
  md: "text/markdown",
};

const statusLabels: Record<WorkflowAssistantAttachmentStatus, string> = {
  uploading: "写入临时存储中",
  uploaded: "临时上传完成",
  classifying: "分类中",
  needs_user_choice: "需要选择",
  proposal_ready: "导入提案待确认",
  importing: "导入中",
  imported: "已导入",
  rejecting: "正在拒绝并清理",
  rejected: "已拒绝",
  expiring: "到期清理中",
  expired: "已过期",
  failed: "处理失败",
};

const targetKindLabels: Record<WorkflowAssistantImportTargetKind, string> = {
  knowledge_source: "知识文件候选",
  prompt_asset: "Prompt 资产",
  task_workbook: "任务表",
  project_notes: "项目注意事项",
  topic_library: "话题库",
  needs_user_choice: "仍需选择",
};
const projectChangeTargetKinds = new Set<WorkflowAssistantImportTargetKind>([
  "prompt_asset",
  "project_notes",
  "topic_library",
]);

const promptKindLabels: Record<PromptKind, string> = {
  outline: "大纲",
  article: "正文",
  review: "审核",
};

const jobStatusLabels: Record<string, string> = {
  queued: "排队中",
  running: "执行中",
  retry_wait: "等待重试",
  succeeded: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

const proposalStatusLabels: Record<string, string> = {
  draft: "需要修订",
  awaiting_confirmation: "等待确认",
  confirmed: "已确认",
  running: "导入中",
  waiting_publication: "等待发布审查",
  completed: "已导入",
  failed: "导入失败",
  cancelled: "已取消",
};

type UploadItem = {
  clientId: string;
  file: File;
  idempotencyKey: string;
  progress: number;
  status: "uploading" | "failed";
  error: string;
};

type ReviewState = {
  job: WorkflowAssistantAttachmentJob | null;
  proposal: WorkflowAssistantImportProposal | null;
  pending: string;
  error: string;
  targetKind: WorkflowAssistantImportTargetKind | "";
  targetProjectId: string;
  promptKind: PromptKind | "";
  diffText: string;
  classifyRequestId: string;
  proposalRequestId: string;
};

const EMPTY_REVIEW_STATE: ReviewState = {
  job: null,
  proposal: null,
  pending: "",
  error: "",
  targetKind: "",
  targetProjectId: "",
  promptKind: "",
  diffText: "",
  classifyRequestId: "",
  proposalRequestId: "",
};

type WorkflowAssistantAttachmentsProps = {
  conversationId: string | null;
  selectedProjectIds: string[];
  projectChangesEnabled: boolean;
  onActivity?: (message: string) => void;
};

function extension(file: File) {
  return file.name.split(".").pop()?.toLowerCase() || "";
}

function normalizedFile(file: File) {
  const suffix = extension(file);
  const expectedMime = MIME_BY_EXTENSION[suffix];
  if (!expectedMime || file.type.toLowerCase() === expectedMime) return file;
  if (suffix === "md" && file.type.toLowerCase() === "text/plain") return file;
  return new File([file], file.name, {
    type: expectedMime,
    lastModified: file.lastModified,
  });
}

function localId(prefix: string) {
  const random = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`;
  return `${prefix}-${random}`.replace(/[^A-Za-z0-9._:-]/g, "-");
}

function errorMessage(error: unknown) {
  if (error instanceof ApiError && error.detail && typeof error.detail === "object") {
    const detail = error.detail as { message?: unknown };
    if (typeof detail.message === "string") return detail.message;
  }
  return error instanceof Error ? error.message : "附件上传失败，请重试。";
}

function sizeLabel(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.ceil(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function expiryLabel(value: string) {
  return formatProjectDate(value, {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function classificationFor(
  attachment: WorkflowAssistantAttachment,
): WorkflowAssistantAttachmentClassification | null {
  const nested = attachment.classification_payload.classification;
  if (!nested || typeof nested !== "object" || Array.isArray(nested)) return null;
  const value = nested as Record<string, unknown>;
  const classification = typeof value.classification === "string"
    ? value.classification
    : attachment.classification;
  if (!classification || typeof value.reason !== "string") return null;
  return {
    classification,
    reason: value.reason,
    confidence: typeof value.confidence === "number" ? value.confidence : 0,
    target_project_id: typeof value.target_project_id === "string"
      ? value.target_project_id
      : null,
    prompt_kind: value.prompt_kind === "outline"
      || value.prompt_kind === "article"
      || value.prompt_kind === "review"
      ? value.prompt_kind
      : null,
    candidate_classifications: Array.isArray(value.candidate_classifications)
      ? value.candidate_classifications.filter((item): item is string => typeof item === "string")
      : [],
    is_ambiguous: value.is_ambiguous === true,
    structure_compatible: value.structure_compatible !== false,
    affects_multiple_projects: value.affects_multiple_projects === true,
  };
}

function defaultReviewState(
  attachment: WorkflowAssistantAttachment,
  selectedProjectIds: string[],
): ReviewState {
  const classification = classificationFor(attachment);
  const defaultKind = classification?.classification && classification.classification in targetKindLabels
    && classification.classification !== "needs_user_choice"
    ? classification.classification as WorkflowAssistantImportTargetKind
    : "";
  return {
    ...EMPTY_REVIEW_STATE,
    targetKind: defaultKind,
    targetProjectId: classification?.target_project_id
      || attachment.proposed_project_id
      || selectedProjectIds[0]
      || "",
    promptKind: classification?.prompt_kind || "",
  };
}

function isActiveJob(job: WorkflowAssistantAttachmentJob | null) {
  return Boolean(job && ["queued", "running", "retry_wait"].includes(job.status));
}

function responseErrorMessage(error: unknown) {
  if (error instanceof ApiError && error.detail && typeof error.detail === "object") {
    const detail = error.detail as { message?: unknown };
    if (typeof detail.message === "string") return detail.message;
  }
  return errorMessage(error);
}

export function WorkflowAssistantAttachments({
  conversationId,
  selectedProjectIds,
  projectChangesEnabled,
  onActivity,
}: WorkflowAssistantAttachmentsProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [attachments, setAttachments] = useState<WorkflowAssistantAttachment[]>([]);
  const [uploads, setUploads] = useState<UploadItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [actionPending, setActionPending] = useState("");
  const [error, setError] = useState("");
  const [reviewStates, setReviewStates] = useState<Record<string, ReviewState>>({});
  const reviewStatesRef = useRef<Record<string, ReviewState>>({});
  const activityRef = useRef(onActivity);
  reviewStatesRef.current = reviewStates;
  activityRef.current = onActivity;

  const mergeReviewResponse = useCallback((response: WorkflowAssistantAttachmentReviewResponse) => {
    const attachment = response.attachment;
    setAttachments((current) => {
      let found = false;
      const next = current.map((candidate) => {
        if (candidate.attachment_id !== attachment.attachment_id) return candidate;
        found = true;
        return attachment;
      });
      return found ? next : [attachment, ...next];
    });
    setReviewStates((current) => {
      const previous = current[attachment.attachment_id] || defaultReviewState(
        attachment,
        selectedProjectIds,
      );
      const proposal = response.proposal || previous.proposal;
      return {
        ...current,
        [attachment.attachment_id]: {
          ...previous,
          job: response.job || previous.job,
          proposal,
          pending: "",
          error: "",
          targetKind: proposal?.target_kind || previous.targetKind,
          targetProjectId: proposal?.target_project_id || previous.targetProjectId,
          diffText: proposal
            ? JSON.stringify(proposal.normalized_diff, null, 2)
            : previous.diffText,
          classifyRequestId: response.job?.operation === "classify_attachment"
            && ["failed", "cancelled", "conflict"].includes(response.job.status)
            ? ""
            : previous.classifyRequestId,
          proposalRequestId: response.proposal?.status === "cancelled"
            || (response.job?.operation === "preview_import_proposal"
              && ["failed", "cancelled", "conflict"].includes(response.job.status))
            ? ""
            : previous.proposalRequestId,
        },
      };
    });
  }, [selectedProjectIds]);

  useEffect(() => {
    let disposed = false;
    setUploads([]);
    setAttachments([]);
    setReviewStates({});
    setError("");
    if (!conversationId) {
      setLoading(false);
      return () => {
        disposed = true;
      };
    }
    setLoading(true);
    void apiGet<WorkflowAssistantAttachmentList>(
        `/api/workflow-assistant/conversations/${encodeURIComponent(conversationId)}/attachments`,
      )
      .then((response) => {
        if (!disposed) setAttachments(response.attachments);
        const durableReviews = response.attachments.filter((attachment) => [
          "classifying",
          "needs_user_choice",
          "proposal_ready",
          "importing",
          "imported",
          "failed",
        ].includes(attachment.status));
        void Promise.all(durableReviews.map(async (attachment) => {
          try {
            return await getWorkflowAssistantAttachmentReview(
              attachment.attachment_id,
              conversationId,
            );
          } catch {
            return null;
          }
        })).then((reviews) => {
          if (disposed) return;
          reviews.filter((item): item is WorkflowAssistantAttachmentReviewResponse => item !== null)
            .forEach((item) => mergeReviewResponse(item));
        });
      })
      .catch((reason) => {
        if (!disposed) setError(errorMessage(reason));
      })
      .finally(() => {
        if (!disposed) setLoading(false);
      });
    return () => {
      disposed = true;
    };
  }, [conversationId, mergeReviewResponse]);

  useEffect(() => {
    if (!conversationId) return;
    let disposed = false;
    const source = new EventSource(
      apiFileUrl(
        `/api/workflow-assistant/conversations/${encodeURIComponent(conversationId)}/attachments/events/stream`,
      ),
      { withCredentials: true },
    );
    source.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data) as {
          sequence?: number;
          public_payload?: {
            attachments?: Array<{
              attachment_id?: string;
              event_kind?: string;
              status?: string;
              job_status?: string | null;
              proposal_status?: string | null;
            }>;
          };
        };
        const states = data.public_payload?.attachments || [];
        const changed = states.filter((item) => item.attachment_id && item.event_kind);
        if (changed.length) {
          const summary = changed
            .slice(0, 3)
            .map((item) => `${item.event_kind} ${item.status || item.job_status || item.proposal_status || "updated"}`)
            .join(" · ");
          activityRef.current?.(`附件 SSE ${data.sequence ?? ""} · ${summary}`);
          void apiGet<WorkflowAssistantAttachmentList>(
            `/api/workflow-assistant/conversations/${encodeURIComponent(conversationId)}/attachments`,
          ).then((response) => {
            if (!disposed) setAttachments(response.attachments);
          }).catch(() => undefined);
        }
      } catch {
        // Ignore proxy keepalives and malformed non-data frames.
      }
    };
    source.onerror = () => {
      // The server intentionally closes this bounded stream after 30 seconds.
      // EventSource reconnects automatically; treating that normal hand-off as
      // an error would flood the shared execution timeline with false alarms.
    };
    return () => {
      disposed = true;
      source.close();
    };
  }, [conversationId]);

  const activeJobSignature = useMemo(
    () => Object.entries(reviewStates)
      .filter(([, state]) => isActiveJob(state.job))
      .map(([attachmentId, state]) => `${attachmentId}:${state.job?.job_id}:${state.job?.status}`)
      .join("|"),
    [reviewStates],
  );

  useEffect(() => {
    if (!conversationId || !activeJobSignature) return;
    let disposed = false;
    const poll = async () => {
      const activeJobs = Object.values(reviewStatesRef.current)
        .map((state) => state.job)
        .filter((job): job is WorkflowAssistantAttachmentJob => isActiveJob(job));
      await Promise.all(activeJobs.map(async (job) => {
        try {
          const response = await getWorkflowAssistantAttachmentJob(job.job_id, conversationId);
          if (!disposed) mergeReviewResponse(response);
        } catch (reason) {
          if (!disposed) {
            setReviewStates((current) => {
              const state = current[job.attachment_id] || EMPTY_REVIEW_STATE;
              return {
                ...current,
                [job.attachment_id]: {
                  ...state,
                  pending: "",
                  error: responseErrorMessage(reason),
                },
              };
            });
          }
        }
      }));
    };
    void poll();
    const timer = window.setInterval(() => void poll(), 1500);
    return () => {
      disposed = true;
      window.clearInterval(timer);
    };
  }, [activeJobSignature, conversationId, mergeReviewResponse]);

  function stateFor(attachment: WorkflowAssistantAttachment) {
    return reviewStates[attachment.attachment_id]
      || defaultReviewState(attachment, selectedProjectIds);
  }

  function updateReviewState(
    attachment: WorkflowAssistantAttachment,
    patch: Partial<ReviewState>,
  ) {
    setReviewStates((current) => ({
      ...current,
      [attachment.attachment_id]: {
        ...(current[attachment.attachment_id]
          || defaultReviewState(attachment, selectedProjectIds)),
        ...patch,
      },
    }));
  }

  async function classify(attachment: WorkflowAssistantAttachment) {
    if (!conversationId) return;
    const state = stateFor(attachment);
    const idempotencyKey = state.classifyRequestId || localId("attachment-classify");
    updateReviewState(attachment, {
      pending: "classify",
      error: "",
      classifyRequestId: idempotencyKey,
    });
    try {
      const response = await classifyWorkflowAssistantAttachment(
        attachment.attachment_id,
        {
          conversation_id: conversationId,
          expected_attachment_revision: attachment.revision,
          idempotency_key: idempotencyKey,
        },
      );
      mergeReviewResponse(response);
    } catch (reason) {
      updateReviewState(attachment, {
        pending: "",
        error: responseErrorMessage(reason),
      });
    }
  }

  async function createProposal(attachment: WorkflowAssistantAttachment) {
    if (!conversationId) return;
    const state = stateFor(attachment);
    if (!state.targetKind || state.targetKind === "needs_user_choice" || !state.targetProjectId) {
      updateReviewState(attachment, { error: "请先选择导入类型和目标项目。" });
      return;
    }
    if (state.targetKind === "prompt_asset" && !state.promptKind) {
      updateReviewState(attachment, { error: "Prompt 资产必须明确选择大纲、正文或审核类型。" });
      return;
    }
    const idempotencyKey = state.proposalRequestId || localId("attachment-proposal");
    updateReviewState(attachment, {
      pending: "proposal",
      error: "",
      proposalRequestId: idempotencyKey,
    });
    try {
      const response = await createWorkflowAssistantImportProposal(
        attachment.attachment_id,
        {
          conversation_id: conversationId,
          expected_attachment_revision: attachment.revision,
          idempotency_key: idempotencyKey,
          target_kind: state.targetKind,
          target_project_id: state.targetProjectId,
          ...(state.promptKind ? { prompt_kind: state.promptKind } : {}),
        },
      );
      mergeReviewResponse(response);
    } catch (reason) {
      updateReviewState(attachment, {
        pending: "",
        error: responseErrorMessage(reason),
      });
    }
  }

  async function reviseProposal(attachment: WorkflowAssistantAttachment) {
    if (!conversationId) return;
    const state = stateFor(attachment);
    const proposal = state.proposal;
    if (!proposal || !state.targetKind || state.targetKind === "needs_user_choice" || !state.targetProjectId) return;
    let normalizedDiff: Record<string, unknown>;
    try {
      const parsed: unknown = JSON.parse(state.diffText);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new Error("结构化差异必须是 JSON 对象。 ");
      }
      normalizedDiff = parsed as Record<string, unknown>;
    } catch (reason) {
      updateReviewState(attachment, { error: reason instanceof Error ? reason.message : "结构化差异 JSON 无效。" });
      return;
    }
    updateReviewState(attachment, { pending: "revise", error: "" });
    try {
      const response = await reviseWorkflowAssistantImportProposal(
        proposal.proposal_id,
        {
          conversation_id: conversationId,
          expected_revision: proposal.revision,
          expected_attachment_revision: attachment.revision,
          target_kind: state.targetKind,
          target_project_id: state.targetProjectId,
          normalized_diff: normalizedDiff,
        },
      );
      mergeReviewResponse(response);
    } catch (reason) {
      updateReviewState(attachment, {
        pending: "",
        error: responseErrorMessage(reason),
      });
    }
  }

  async function confirmProposal(attachment: WorkflowAssistantAttachment) {
    if (!conversationId) return;
    const state = stateFor(attachment);
    const proposal = state.proposal;
    if (!proposal || !state.targetProjectId) return;
    updateReviewState(attachment, { pending: "confirm", error: "" });
    try {
      const response = await confirmWorkflowAssistantImportProposal(
        proposal.proposal_id,
        {
          conversation_id: conversationId,
          target_project_id: state.targetProjectId,
          expected_revision: proposal.revision,
          expected_attachment_revision: attachment.revision,
        },
      );
      mergeReviewResponse(response);
    } catch (reason) {
      updateReviewState(attachment, {
        pending: "",
        error: responseErrorMessage(reason),
      });
    }
  }

  async function cancelProposal(attachment: WorkflowAssistantAttachment) {
    if (!conversationId) return;
    const state = stateFor(attachment);
    const proposal = state.proposal;
    if (!proposal) return;
    updateReviewState(attachment, { pending: "cancel", error: "" });
    try {
      const response = await cancelWorkflowAssistantImportProposal(
        proposal.proposal_id,
        {
          conversation_id: conversationId,
          expected_revision: proposal.revision,
        },
      );
      mergeReviewResponse(response);
    } catch (reason) {
      updateReviewState(attachment, {
        pending: "",
        error: responseErrorMessage(reason),
      });
    }
  }

  async function upload(item: UploadItem) {
    if (!conversationId) return;
    setUploads((current) => current.map((candidate) => (
      candidate.clientId === item.clientId
        ? { ...candidate, status: "uploading", progress: 0, error: "" }
        : candidate
    )));
    const body = new FormData();
    body.append("file", normalizedFile(item.file));
    body.append("idempotency_key", item.idempotencyKey);
    if (selectedProjectIds.length === 1) {
      body.append("proposed_project_id", selectedProjectIds[0]);
    }
    try {
      const created = await apiUploadWithProgress<WorkflowAssistantAttachment>(
        `/api/workflow-assistant/conversations/${encodeURIComponent(conversationId)}/attachments`,
        body,
        (progress) => setUploads((current) => current.map((candidate) => (
          candidate.clientId === item.clientId ? { ...candidate, progress } : candidate
        ))),
      );
      setAttachments((current) => [
        created,
        ...current.filter((candidate) => candidate.attachment_id !== created.attachment_id),
      ]);
      setUploads((current) => current.filter((candidate) => candidate.clientId !== item.clientId));
    } catch (reason) {
      setUploads((current) => current.map((candidate) => (
        candidate.clientId === item.clientId
          ? { ...candidate, status: "failed", error: errorMessage(reason) }
          : candidate
      )));
    }
  }

  function chooseFiles(event: ChangeEvent<HTMLInputElement>) {
    const files = Array.from(event.target.files ?? []);
    event.target.value = "";
    setError("");
    if (!conversationId) {
      setError("请先新建或选择一个会话，再上传临时附件。");
      return;
    }
    const accepted: UploadItem[] = [];
    const rejected: string[] = [];
    for (const file of files) {
      const suffix = extension(file);
      if (!ALLOWED_EXTENSIONS.has(suffix)) {
        rejected.push(`${file.name}（不支持的类型）`);
        continue;
      }
      if (!file.size || file.size > MAX_ATTACHMENT_BYTES) {
        rejected.push(`${file.name}（必须大于 0 且不超过 25 MB）`);
        continue;
      }
      accepted.push({
        clientId: localId("attachment-selection"),
        file,
        idempotencyKey: localId("attachment-upload"),
        progress: 0,
        status: "uploading",
        error: "",
      });
    }
    if (rejected.length) setError(`未加入上传队列：${rejected.join("、")}`);
    if (!accepted.length) return;
    setUploads((current) => [...current, ...accepted]);
    for (const item of accepted) void upload(item);
  }

  async function download(attachment: WorkflowAssistantAttachment) {
    if (!conversationId) return;
    setActionPending(`download:${attachment.attachment_id}`);
    setError("");
    try {
      const response = await apiGet<WorkflowAssistantAttachment>(
        `/api/workflow-assistant/attachments/${encodeURIComponent(attachment.attachment_id)}/download?conversation_id=${encodeURIComponent(conversationId)}`,
      );
      if (!response.download_url) throw new Error("服务器没有返回可用的短期下载地址。");
      window.location.assign(response.download_url);
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setActionPending("");
    }
  }

  async function reject(attachment: WorkflowAssistantAttachment) {
    if (!conversationId) return;
    setActionPending(`reject:${attachment.attachment_id}`);
    setError("");
    try {
      await apiPost<WorkflowAssistantAttachment>(
        `/api/workflow-assistant/attachments/${encodeURIComponent(attachment.attachment_id)}/reject?conversation_id=${encodeURIComponent(conversationId)}`,
      );
      setAttachments((current) => current.filter(
        (candidate) => candidate.attachment_id !== attachment.attachment_id,
      ));
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setActionPending("");
    }
  }

  return (
    <section className="grid gap-2 rounded-xl border border-dashed bg-muted/20 p-3" aria-label="临时附件">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <p className="text-sm font-medium">临时附件</p>
          <p className="text-xs leading-5 text-muted-foreground">
            临时上传，未导入、未发布；最多保留 7 天。
          </p>
        </div>
        <input
          ref={inputRef}
          type="file"
          multiple
          className="sr-only"
          accept=".pdf,.docx,.xlsx,.xlsm,.txt,.md"
          onChange={chooseFiles}
        />
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={!conversationId}
          onClick={() => inputRef.current?.click()}
        >
          <Paperclip />
          添加附件
        </Button>
      </div>
      <p className="text-xs text-muted-foreground">
        支持 PDF、DOCX、XLSX、XLSM、TXT、MD，单文件最大 25 MB。文件内容不会被执行。
      </p>

      {uploads.map((item) => (
        <div key={item.clientId} className="grid gap-2 rounded-lg border bg-background px-3 py-2">
          <div className="flex items-start justify-between gap-3 text-sm">
            <span className="min-w-0">
              <span className="flex items-center gap-2 font-medium"><FileText className="size-4 shrink-0" /><span className="truncate">{item.file.name}</span></span>
              <span className="text-xs text-muted-foreground">{sizeLabel(item.file.size)}</span>
            </span>
            {item.status === "uploading" ? <Badge variant="outline">上传 {item.progress}%</Badge> : <Badge variant="destructive">上传失败</Badge>}
          </div>
          {item.status === "uploading" ? <Progress value={item.progress} aria-label={`${item.file.name} 上传进度`} /> : (
            <div className="flex items-center justify-between gap-2">
              <span className="flex items-center gap-1 text-xs text-destructive"><AlertCircle className="size-3" />{item.error}</span>
              <Button type="button" size="sm" variant="outline" onClick={() => void upload(item)}><RefreshCw />重试</Button>
            </div>
          )}
        </div>
      ))}

      {loading && <div className="flex items-center gap-2 text-xs text-muted-foreground"><Loader2 className="size-3 animate-spin" />读取附件中…</div>}
      {attachments.map((attachment) => {
        const downloadPending = actionPending === `download:${attachment.attachment_id}`;
        const rejectPending = actionPending === `reject:${attachment.attachment_id}`;
        const review = stateFor(attachment);
        const classification = classificationFor(attachment);
        const candidateKinds = (classification?.candidate_classifications || [])
          .filter((kind): kind is WorkflowAssistantImportTargetKind => (
            kind in targetKindLabels && kind !== "needs_user_choice"
          ));
        const targetKindOptions = candidateKinds.length
          ? candidateKinds
          : classification?.classification
            && classification.classification in targetKindLabels
            && classification.classification !== "needs_user_choice"
            ? [classification.classification as WorkflowAssistantImportTargetKind]
            : (Object.keys(targetKindLabels) as WorkflowAssistantImportTargetKind[])
              .filter((kind) => kind !== "needs_user_choice");
        const availableTargetKindOptions = targetKindOptions.filter(
          (kind) => projectChangesEnabled || !projectChangeTargetKinds.has(kind),
        );
        const targetProjectOptions = [...new Set([
          ...selectedProjectIds,
          attachment.proposed_project_id || "",
          classification?.target_project_id || "",
          review.targetProjectId,
        ])].filter(Boolean);
        const reviewBusy = Boolean(review.pending || actionPending);
        const canClassify = attachment.status === "uploaded" || attachment.status === "failed";
        const canReject = [
          "uploading",
          "uploaded",
          "classifying",
          "needs_user_choice",
          "proposal_ready",
          "failed",
        ].includes(attachment.status);
        const canPreview = ["proposal_ready", "needs_user_choice"].includes(attachment.status)
          && (projectChangesEnabled
            || !projectChangeTargetKinds.has(review.targetKind as WorkflowAssistantImportTargetKind));
        return (
          <article key={attachment.attachment_id} className="rounded-lg border bg-background px-3 py-2">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <p className="flex items-center gap-2 text-sm font-medium"><FileText className="size-4 shrink-0" /><span className="truncate">{attachment.original_filename}</span></p>
                <p className="mt-1 text-xs text-muted-foreground">
                  {sizeLabel(attachment.byte_size)} · 到期 {expiryLabel(attachment.expires_at)}
                </p>
              </div>
              <Badge variant={attachment.status === "failed" ? "destructive" : "outline"}>
                {statusLabels[attachment.status] || attachment.status}
              </Badge>
            </div>
              <p className="mt-2 text-xs font-medium text-amber-700 dark:text-amber-300">
                当前附件不会自动导入或发布；所有分类、差异和目标变更都需要人工确认。
              </p>
              {!projectChangesEnabled && <p className="mt-1 text-xs text-muted-foreground">
                项目提示词、注意事项和话题库导入暂未开放；知识文件和任务表仍可继续生成提案。
              </p>}
            <div className="mt-2 flex flex-wrap gap-2">
              <Button type="button" size="sm" variant="outline" disabled={Boolean(actionPending || review.pending)} onClick={() => void download(attachment)}>
                {downloadPending ? <Loader2 className="animate-spin" /> : <Download />}下载
              </Button>
              {canReject && <Button type="button" size="sm" variant="outline" disabled={Boolean(actionPending || review.pending)} onClick={() => void reject(attachment)}>
                {rejectPending ? <Loader2 className="animate-spin" /> : <Trash2 />}拒绝并删除临时文件
              </Button>}
              {canClassify && <Button type="button" size="sm" variant="outline" disabled={reviewBusy} onClick={() => void classify(attachment)}>
                {review.pending === "classify" ? <Loader2 className="animate-spin" /> : <RefreshCw />} {attachment.status === "failed" ? "重试分类" : "开始分类"}
              </Button>}
            </div>
            {review.job && <div className="mt-3 rounded-lg border border-dashed p-3 text-xs text-muted-foreground">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span>后台任务：{review.job.operation}</span>
                <Badge variant={review.job.status === "failed" ? "destructive" : "outline"}>
                  {jobStatusLabels[review.job.status] || review.job.status}
                </Badge>
              </div>
              {review.job.standardized_error_code && <p className="mt-1 text-destructive">错误：{review.job.standardized_error_code}</p>}
            </div>}
            {classification && attachment.status !== "uploaded" && <div className="mt-3 grid gap-2 rounded-lg border bg-muted/20 p-3 text-sm">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="font-medium">分类结果：{targetKindLabels[classification.classification as WorkflowAssistantImportTargetKind] || classification.classification}</span>
                <Badge variant={classification.classification === "needs_user_choice" ? "outline" : "secondary"}>
                  置信度 {Math.round(classification.confidence * 100)}%
                </Badge>
              </div>
              <p className="text-xs text-muted-foreground">{classification.reason}</p>
              {classification.candidate_classifications.length > 0 && <p className="text-xs text-muted-foreground">候选：{classification.candidate_classifications.map((kind) => targetKindLabels[kind as WorkflowAssistantImportTargetKind] || kind).join("、")}</p>}
              {canPreview && <div className="grid gap-2 border-t pt-3">
                <label className="grid gap-1 text-xs font-medium">
                  导入类型
                  <select
                    className="h-9 rounded-md border bg-background px-2 text-sm font-normal"
                    value={review.targetKind}
                    disabled={reviewBusy}
                    onChange={(event) => updateReviewState(attachment, {
                      targetKind: event.target.value as WorkflowAssistantImportTargetKind,
                      promptKind: event.target.value === "prompt_asset" ? review.promptKind : "",
                    })}
                  >
                    <option value="">请选择</option>
                    {availableTargetKindOptions.map((kind) => <option key={kind} value={kind}>{targetKindLabels[kind]}</option>)}
                  </select>
                </label>
                <label className="grid gap-1 text-xs font-medium">
                  目标项目
                  <select
                    className="h-9 rounded-md border bg-background px-2 text-sm font-normal"
                    value={review.targetProjectId}
                    disabled={reviewBusy}
                    onChange={(event) => updateReviewState(attachment, { targetProjectId: event.target.value })}
                  >
                    <option value="">请选择</option>
                    {targetProjectOptions.map((projectId) => <option key={projectId} value={projectId}>{projectId}</option>)}
                  </select>
                </label>
                {review.targetKind === "prompt_asset" && <label className="grid gap-1 text-xs font-medium">
                  Prompt 类型
                  <select
                    className="h-9 rounded-md border bg-background px-2 text-sm font-normal"
                    value={review.promptKind}
                    disabled={reviewBusy}
                    onChange={(event) => updateReviewState(attachment, { promptKind: event.target.value as PromptKind })}
                  >
                    <option value="">请选择</option>
                    {(Object.keys(promptKindLabels) as PromptKind[]).map((kind) => <option key={kind} value={kind}>{promptKindLabels[kind]}</option>)}
                  </select>
                </label>}
                <Button type="button" size="sm" className="justify-self-start" disabled={reviewBusy} onClick={() => void createProposal(attachment)}>
                  {review.pending === "proposal" ? <Loader2 className="animate-spin" /> : <Check />}生成导入提案
                </Button>
              </div>}
            </div>}
            {review.proposal && <div className="mt-3 grid gap-3 rounded-lg border border-primary/30 bg-primary/5 p-3 text-sm">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="font-medium">导入提案与结构化差异</span>
                <Badge variant={review.proposal.status === "failed" ? "destructive" : "outline"}>
                  {proposalStatusLabels[review.proposal.status] || review.proposal.status}
                </Badge>
              </div>
              <p className="text-xs text-muted-foreground">Proposal Revision {review.proposal.revision} · {targetKindLabels[review.proposal.target_kind]}</p>
              <textarea
                className="min-h-40 rounded-md border bg-background p-2 font-mono text-xs leading-5"
                value={review.diffText || JSON.stringify(review.proposal.normalized_diff, null, 2)}
                disabled={reviewBusy || ["confirmed", "running", "completed", "waiting_publication", "cancelled"].includes(review.proposal.status)}
                onChange={(event) => updateReviewState(attachment, { diffText: event.target.value })}
                aria-label="导入提案结构化差异"
              />
              {review.proposal.standardized_error_code && <p className="text-xs text-destructive">错误：{review.proposal.standardized_error_code}</p>}
              <div className="flex flex-wrap gap-2">
                {["draft", "awaiting_confirmation"].includes(review.proposal.status) && <Button type="button" size="sm" variant="outline" disabled={reviewBusy} onClick={() => void reviseProposal(attachment)}>
                  {review.pending === "revise" ? <Loader2 className="animate-spin" /> : <Save />}保存差异修订
                </Button>}
                {review.proposal.status === "awaiting_confirmation" && <Button type="button" size="sm" disabled={reviewBusy} onClick={() => void confirmProposal(attachment)}>
                  {review.pending === "confirm" ? <Loader2 className="animate-spin" /> : <Check />}确认并导入
                </Button>}
                {["draft", "awaiting_confirmation"].includes(review.proposal.status) && <Button type="button" size="sm" variant="outline" disabled={reviewBusy} onClick={() => void cancelProposal(attachment)}>
                  {review.pending === "cancel" ? <Loader2 className="animate-spin" /> : <Trash2 />}取消提案
                </Button>}
              </div>
              {review.proposal.status === "waiting_publication" && review.proposal.target_project_id && <div className="rounded-md border border-amber-300 bg-amber-50 p-2 text-xs text-amber-900 dark:bg-amber-950/30 dark:text-amber-200">
                知识文件已进入待发布状态，仍需人工发布审查。 <Link className="underline" href={`/projects/${encodeURIComponent(review.proposal.target_project_id)}/knowledge`}>打开知识库审查</Link>
              </div>}
              {review.proposal.resulting_entity_refs.length > 0 && <p className="text-xs text-muted-foreground">已生成 {review.proposal.resulting_entity_refs.length} 个业务实体引用；请按目标项目页面复核。</p>}
            </div>}
            {review.error && <p className="mt-2 flex items-start gap-1 text-xs text-destructive"><AlertCircle className="mt-0.5 size-3 shrink-0" />{review.error}</p>}
          </article>
        );
      })}
      {conversationId && !loading && !uploads.length && !attachments.length && (
        <p className="text-xs text-muted-foreground">这个会话还没有临时附件。</p>
      )}
      {!conversationId && <p className="text-xs text-muted-foreground">先新建或选择一个会话后即可添加附件。</p>}
      {error && <p className="flex items-start gap-1 text-xs text-destructive"><AlertCircle className="mt-0.5 size-3 shrink-0" />{error}</p>}
    </section>
  );
}
