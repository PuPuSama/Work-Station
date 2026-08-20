import { apiGet, apiPost } from "@/lib/api";
import type {
  PromptKind,
  WorkflowAssistantAttachmentReviewResponse,
  WorkflowAssistantImportTargetKind,
} from "@/types";

function attachmentPath(attachmentId: string) {
  return `/api/workflow-assistant/attachments/${encodeURIComponent(attachmentId)}`;
}

export function classifyWorkflowAssistantAttachment(
  attachmentId: string,
  request: {
    conversation_id: string;
    expected_attachment_revision: number;
    idempotency_key: string;
  },
) {
  return apiPost<WorkflowAssistantAttachmentReviewResponse>(
    `${attachmentPath(attachmentId)}/classify`,
    request,
  );
}

export function createWorkflowAssistantImportProposal(
  attachmentId: string,
  request: {
    conversation_id: string;
    expected_attachment_revision: number;
    idempotency_key: string;
    target_kind: WorkflowAssistantImportTargetKind;
    target_project_id: string;
    prompt_kind?: PromptKind;
    plan_id?: string | null;
  },
) {
  return apiPost<WorkflowAssistantAttachmentReviewResponse>(
    `${attachmentPath(attachmentId)}/proposals`,
    request,
  );
}

export function getWorkflowAssistantAttachmentJob(
  jobId: string,
  conversationId: string,
) {
  return apiGet<WorkflowAssistantAttachmentReviewResponse>(
    `/api/workflow-assistant/attachment-jobs/${encodeURIComponent(jobId)}?conversation_id=${encodeURIComponent(conversationId)}`,
  );
}

export function getWorkflowAssistantAttachmentReview(
  attachmentId: string,
  conversationId: string,
) {
  return apiGet<WorkflowAssistantAttachmentReviewResponse>(
    `${attachmentPath(attachmentId)}/review?conversation_id=${encodeURIComponent(conversationId)}`,
  );
}

export function getWorkflowAssistantImportProposal(
  proposalId: string,
  conversationId: string,
) {
  return apiGet<WorkflowAssistantAttachmentReviewResponse>(
    `/api/workflow-assistant/import-proposals/${encodeURIComponent(proposalId)}?conversation_id=${encodeURIComponent(conversationId)}`,
  );
}

export function reviseWorkflowAssistantImportProposal(
  proposalId: string,
  request: {
    conversation_id: string;
    expected_revision: number;
    expected_attachment_revision: number;
    target_kind: WorkflowAssistantImportTargetKind;
    target_project_id: string;
    normalized_diff: Record<string, unknown>;
  },
) {
  return apiPost<WorkflowAssistantAttachmentReviewResponse>(
    `/api/workflow-assistant/import-proposals/${encodeURIComponent(proposalId)}/revise`,
    request,
  );
}

export function confirmWorkflowAssistantImportProposal(
  proposalId: string,
  request: {
    conversation_id: string;
    target_project_id: string;
    expected_revision: number;
    expected_attachment_revision: number;
  },
) {
  return apiPost<WorkflowAssistantAttachmentReviewResponse>(
    `/api/workflow-assistant/import-proposals/${encodeURIComponent(proposalId)}/confirm`,
    request,
  );
}

export function cancelWorkflowAssistantImportProposal(
  proposalId: string,
  request: {
    conversation_id: string;
    expected_revision: number;
  },
) {
  return apiPost<WorkflowAssistantAttachmentReviewResponse>(
    `/api/workflow-assistant/import-proposals/${encodeURIComponent(proposalId)}/cancel`,
    request,
  );
}
