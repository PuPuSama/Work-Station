import { apiFileUrl, apiGet, apiPost } from "@/lib/api";
import type {
  KnowledgeEvidencePack,
  KnowledgeRetrievalPlan,
  ResearchRun,
  ResearchRunDetail,
  ResearchRunQueued,
  TargetedGapRepairQueued,
  WorkflowAssistantGapFillRequest,
  WorkflowAssistantGapFillResponse,
} from "@/types";

export type ServerResearchRunStart = {
  request_id: string;
  retrieval_plan_id: string;
  max_discovery_queries: number;
};

export type ServerResearchRunResume = {
  request_id: string;
  approved_candidate_ids: string[];
};

function projectPath(projectId: string) {
  return `/api/knowledge/${encodeURIComponent(projectId)}`;
}

export function listResearchPlans(projectId: string) {
  return apiGet<KnowledgeRetrievalPlan[]>(
    `${projectPath(projectId)}/retrieval-plans?limit=100`,
  );
}

export function createTaskResearchPlan(projectId: string, taskId: string) {
  return apiPost<KnowledgeRetrievalPlan>(
    `${projectPath(projectId)}/tasks/${encodeURIComponent(taskId)}/retrieval-plan`,
  );
}

export type TargetedGapRepairRequest = {
  revision: number;
  request_id: string;
  retrieval_plan_id: string;
  sentence_ids: string[];
  max_discovery_queries: number;
};

export function startTargetedGapRepair(
  projectId: string,
  taskId: string,
  request: TargetedGapRepairRequest,
) {
  return apiPost<TargetedGapRepairQueued>(
    `${projectPath(projectId)}/tasks/${encodeURIComponent(taskId)}/knowledge-gap-repair`,
    request,
  );
}

export function listResearchRuns(projectId: string) {
  return apiGet<ResearchRun[]>(
    `${projectPath(projectId)}/research-runs?limit=50`,
  );
}

export function getResearchRun(projectId: string, threadId: string) {
  return apiGet<ResearchRunDetail>(
    `${projectPath(projectId)}/research-runs/${encodeURIComponent(threadId)}`,
  );
}

export function startResearchRun(
  projectId: string,
  request: ServerResearchRunStart,
) {
  return apiPost<ResearchRunQueued>(
    `${projectPath(projectId)}/research-runs`,
    request,
  );
}

export function resumeResearchRun(
  projectId: string,
  threadId: string,
  request: ServerResearchRunResume,
) {
  return apiPost<ResearchRunQueued>(
    `${projectPath(projectId)}/research-runs/${encodeURIComponent(threadId)}/resume`,
    request,
  );
}

export function gapFillWorkflowAssistantPlan(
  planId: string,
  request: WorkflowAssistantGapFillRequest,
) {
  return apiPost<WorkflowAssistantGapFillResponse>(
    `/api/workflow-assistant/plans/${encodeURIComponent(planId)}/gap-fill`,
    request,
  );
}

export function researchRunEventsUrl(
  projectId: string,
  threadId: string,
  afterSequence = 0,
) {
  const query = afterSequence > 0 ? `?after_sequence=${afterSequence}` : "";
  return apiFileUrl(
    `${projectPath(projectId)}/research-runs/${encodeURIComponent(threadId)}/events/stream${query}`,
  );
}

export function getEvidencePack(projectId: string, evidencePackId: string) {
  return apiGet<KnowledgeEvidencePack>(
    `${projectPath(projectId)}/evidence-packs/${encodeURIComponent(evidencePackId)}`,
  );
}
