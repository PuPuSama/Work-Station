from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict

from knowledge_agent.object_storage import KnowledgeObjectNotFound
from services.access_control import ActorIdentity, ProjectAccessDenied
from services.delivery_package import official_website_folder_name
from services.object_store import ObjectStoreError
from services.server_delivery_package import (
    ServerBatchDeliveryPackage,
    ServerDeliveryPackageError,
)
from services.server_request_security import AuthorizedProjectRequest
from server_project_http import require_server_actor

from .context import (
    AssistantContextError,
    AssistantWorkspaceContext,
    WorkflowAssistantContextResolver,
)
from .contracts import (
    AssistantConversationCreateRequest,
    AssistantConversationResponse,
    AssistantMessageRequest,
    AssistantMessageResponse,
    AttentionCountResponse,
    BatchWritingPlanRequest,
    PlanCommandRequest,
    WorkflowPlanSummary,
    PlanDraft,
    PlanRevisionRequest,
    PlanStep,
    WorkflowPlanResponse,
)
from .gap_fill import (
    GapFillConflict,
    GapFillError,
    GapFillNotFound,
    GapFillRequest,
    GapFillResponse,
    GapFillUnavailable,
    WorkflowAssistantGapFillService,
)
from .fixed_workflow import build_fixed_article_plan
from .message_router import (
    AssistantMessageRouter,
    AssistantMessageRouterUnavailable,
    is_article_generation_request,
    render_knowledge_answer,
)
from .planner import (
    PlannerModelIdentity,
    PlannerOutputError,
    PlannerUnavailable,
    StructuredWorkflowPlanner,
    estimate_planner_usage,
)
from .policy import (
    ASSISTANT_ALLOWED_ACTION_KINDS,
    AssistantPolicyError,
    TASK_BOUND_ACTION_KINDS,
    WRITE_ACTION_KINDS,
    bind_plan_context,
    requires_confirmation,
    sanitize_message,
)
from .repository import (
    AssistantConversation,
    AssistantMessage,
    PostgresWorkflowAssistantRepository,
    WorkflowAssistantConflict,
    WorkflowAssistantDispatch,
    WorkflowAssistantNotFound,
    WorkflowPlan,
    WorkflowPlanSummaryRecord,
)
from .tools import (
    WorkflowToolInvocation,
    WorkflowToolRegistry,
    build_read_only_tool_handlers,
)


LOGGER = logging.getLogger(__name__)


router = APIRouter(
    prefix="/api/workflow-assistant",
    tags=["workflow-assistant"],
)


class AssistantMessageDispatchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Workflow planning is acknowledged before the model call starts.  Chat
    # and knowledge answers still return a message immediately; a queued
    # workflow returns the durable dispatch identity instead.
    message: AssistantMessageResponse | None = None
    plan: WorkflowPlanResponse | None = None
    dispatch_id: str | None = None
    dispatch_status: str | None = None
    dispatch_error_code: str | None = None


class AssistantConversationListResponse(BaseModel):
    conversations: list[AssistantConversationResponse]


class WorkflowAssistantBatchDownloadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: str
    url: str
    expires_seconds: int
    filename: str
    file_count: int


class AssistantEventResponse(BaseModel):
    sequence: int
    event_kind: str
    public_payload: dict[str, Any]
    created_at: str | None


class AssistantAttentionListResponse(BaseModel):
    plans: list[WorkflowPlanSummary]
    count: int = 0


class BatchPlanHistoryResponse(BaseModel):
    plans: list[WorkflowPlanSummary]
    count: int = 0


def _feature_enabled(request: Request) -> None:
    config = getattr(request.app.state, "article_agent_config", None)
    if config is None or not bool(getattr(config, "workflow_assistant_enabled", False)):
        raise HTTPException(status_code=404, detail="Workflow Assistant is disabled.")


def _gap_fill_feature_enabled(request: Request) -> None:
    _feature_enabled(request)
    config = getattr(request.app.state, "article_agent_config", None)
    if config is None or not bool(
        getattr(config, "workflow_assistant_gap_fill_enabled", False)
    ):
        raise HTTPException(status_code=404, detail="Workflow Assistant gap-fill is disabled.")


def _gap_fill_service(request: Request) -> WorkflowAssistantGapFillService:
    registry = getattr(request.app.state, "server_knowledge_research", None)
    if registry is None:
        raise HTTPException(
            status_code=503,
            detail="Workflow Assistant knowledge research is not available.",
        )
    return WorkflowAssistantGapFillService(registry)


def _apply_execution_limits(request: Request, plan: Any) -> Any:
    """Apply server concurrency ceilings to every persisted plan variant."""

    config = getattr(request.app.state, "article_agent_config", None)
    try:
        configured = int(getattr(config, "workflow_assistant_max_concurrency", 5))
    except (TypeError, ValueError):
        configured = 5
    maximum = max(1, min(32, configured))
    if getattr(plan, "concurrency_limit", maximum) <= maximum:
        return plan
    return plan.model_copy(update={"concurrency_limit": maximum})


def _repository(request: Request) -> PostgresWorkflowAssistantRepository:
    repository = getattr(request.app.state, "workflow_assistant_repository", None)
    if not isinstance(repository, PostgresWorkflowAssistantRepository):
        raise HTTPException(status_code=503, detail="Workflow Assistant storage is not available.")
    return repository


def _context(request: Request) -> WorkflowAssistantContextResolver:
    resolver = getattr(request.app.state, "workflow_assistant_context", None)
    if not isinstance(resolver, WorkflowAssistantContextResolver):
        raise HTTPException(status_code=503, detail="Workflow Assistant context is not available.")
    return resolver


def _planner(request: Request) -> StructuredWorkflowPlanner:
    planner = getattr(request.app.state, "workflow_assistant_planner", None)
    if not isinstance(planner, StructuredWorkflowPlanner):
        raise HTTPException(status_code=503, detail="Workflow Assistant planner is not available.")
    return planner


def _message_router(request: Request) -> AssistantMessageRouter:
    return AssistantMessageRouter(
        getattr(request.app.state, "server_llm_client_factory", None)
    )


def _knowledge_chat(request: Request) -> Any:
    runtime = getattr(request.app.state, "knowledge_agent_runtime", None)
    service = getattr(runtime, "research_chat", None)
    if service is None:
        raise AssistantMessageRouterUnavailable(
            "project knowledge Q&A is not configured"
        )
    return service


def _knowledge_reply(
    request: Request,
    *,
    project_id: str,
    question: str,
    conversation_id: str,
    request_id: str,
    article_id: str | None,
) -> str:
    research_conversation_id = _assistant_identity(
        "\x00".join((conversation_id, project_id, article_id or "project")),
        prefix="assistant_chat",
    )
    try:
        conversation = _knowledge_chat(request).ask(
            project_id=project_id,
            question=question,
            request_id=request_id,
            conversation_id=research_conversation_id,
            article_id=article_id,
        )
    except AssistantMessageRouterUnavailable:
        raise
    except Exception as exc:
        raise AssistantMessageRouterUnavailable(
            "project knowledge Q&A is temporarily unavailable"
        ) from exc
    answer = next(
        (
            message
            for message in reversed(conversation.messages)
            if message.role == "assistant" and message.request_id == request_id
        ),
        None,
    )
    if answer is None:
        raise AssistantMessageRouterUnavailable(
            "project knowledge Q&A returned no answer"
        )
    return render_knowledge_answer(answer)


def _iso(value: datetime | None) -> str:
    return value.isoformat() if value is not None else ""


def _assistant_identity(value: str, *, prefix: str) -> str:
    """Derive a bounded internal identity from a client request identity."""

    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"{prefix}_{digest}"


def _gap_fill_request_identity(
    *,
    plan_id: str,
    step_id: str,
    thread_id: str,
    request_id: str,
    approved_candidate_ids: list[str],
) -> str:
    """Derive a server-owned idempotency identity for the domain Resume Job."""

    candidate_key = "\x1f".join(sorted(dict.fromkeys(approved_candidate_ids)))
    raw = "\x1f".join(
        (plan_id, step_id, thread_id, request_id, candidate_key)
    )
    return "assistant-gap-fill-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _message_response(message: AssistantMessage) -> AssistantMessageResponse:
    return AssistantMessageResponse(
        message_id=message.message_id,
        sequence=message.sequence,
        role=message.role,
        content=message.content,
        request_id=message.request_id,
        created_at=_iso(message.created_at),
    )


def _conversation_response(
    conversation: AssistantConversation,
) -> AssistantConversationResponse:
    return AssistantConversationResponse(
        conversation_id=conversation.conversation_id,
        title=conversation.title,
        project_ids=list(conversation.project_ids),
        created_at=_iso(conversation.created_at),
        updated_at=_iso(conversation.updated_at),
        expires_at=_iso(conversation.expires_at),
        messages=[_message_response(message) for message in conversation.messages],
    )


def _plan_summary(
    plan: WorkflowPlan | WorkflowPlanSummaryRecord,
) -> WorkflowPlanSummary:
    """Lightweight plan summary without full steps and knowledge snapshots.

    Used for attention lists to avoid transferring megabytes of repeated
    knowledge snapshots and step details when only displaying plan cards.
    """
    pending_count = (
        plan.pending_step_count
        if isinstance(plan, WorkflowPlanSummaryRecord)
        else sum(1 for step in plan.steps if step.status == "pending")
    )
    step_count = (
        plan.step_count
        if isinstance(plan, WorkflowPlanSummaryRecord)
        else len(plan.steps)
    )
    return WorkflowPlanSummary(
        plan_id=plan.plan_id,
        conversation_id=plan.conversation_id,
        title=plan.title,
        natural_language_request=plan.natural_language_request,
        plan_hash=plan.plan_hash,
        revision=plan.revision,
        status=plan.status,
        project_ids=list(plan.project_ids),
        paused_project_ids=list(plan.paused_project_ids),
        step_count=step_count,
        pending_step_count=pending_count,
        concurrency_limit=plan.concurrency_limit,
        budget_warning=plan.budget_warning,
        attention_state=plan.attention_state,
        approved_by=plan.approved_by,
        approved_at=_iso(plan.approved_at) if plan.approved_at else None,
        created_at=_iso(plan.created_at),
        updated_at=_iso(plan.updated_at),
    )


def _plan_response(plan: WorkflowPlan) -> WorkflowPlanResponse:
    steps = [
        PlanStep(
            step_id=step.step_id,
            sequence=step.sequence,
            action_kind=step.action_kind,  # type: ignore[arg-type]
            project_id=step.project_id,
            article_task_id=step.article_task_id,
            expected_task_revision=step.expected_task_revision,
            pinned_prompt_version=step.pinned_prompt_version,
            pinned_knowledge_snapshot=step.pinned_knowledge_snapshot,
            input_summary=step.input_summary,
            hard_gate=step.hard_gate,
            status=step.status,
            background_job_id=step.background_job_id,
            retry_count=step.retry_count,
            output_summary=step.output_summary,
            standardized_error_code=step.standardized_error_code,
            human_gate_confirmed=step.human_gate_confirmed,
            updated_at=_iso(step.updated_at) if step.updated_at else None,
        )
        for step in plan.steps
    ]
    return WorkflowPlanResponse(
        plan_id=plan.plan_id,
        conversation_id=plan.conversation_id,
        title=plan.title,
        natural_language_request=plan.natural_language_request,
        plan_hash=plan.plan_hash,
        revision=plan.revision,
        status=plan.status,
        project_ids=list(plan.project_ids),
        paused_project_ids=list(plan.paused_project_ids),
        steps=steps,
        concurrency_limit=plan.concurrency_limit,
        budget_warning=plan.budget_warning,
        attention_state=plan.attention_state,
        approved_by=plan.approved_by,
        approved_at=_iso(plan.approved_at) if plan.approved_at else None,
    )


def _step_draft_from_persisted(step: Any) -> PlanStep:
    """Project a persisted step into the planner contract for revision."""

    return PlanStep(
        step_id=step.step_id,
        sequence=step.sequence,
        action_kind=step.action_kind,  # type: ignore[arg-type]
        project_id=step.project_id,
        article_task_id=step.article_task_id,
        expected_task_revision=step.expected_task_revision,
        pinned_prompt_version=step.pinned_prompt_version,
        pinned_knowledge_snapshot=step.pinned_knowledge_snapshot,
        input_summary=step.input_summary,
        hard_gate=step.hard_gate,
        status=step.status,
        background_job_id=step.background_job_id,
        retry_count=step.retry_count,
        output_summary=step.output_summary,
        standardized_error_code=step.standardized_error_code,
        human_gate_confirmed=step.human_gate_confirmed,
    )


def _normalize_explicit_revision_execution(
    current: WorkflowPlan,
    proposed: PlanDraft,
) -> PlanDraft:
    """Keep execution history server-owned for a structured revision.

    A client may edit the shape of unfinished work, but it cannot claim that
    a new or previously unfinished step already succeeded. Persisted terminal
    steps are projected from PostgreSQL verbatim; every other supplied step
    starts as unexecuted work and must pass through the normal confirmation
    and runner boundaries.
    """

    terminal_steps = {
        step.step_id: step
        for step in current.steps
        if step.status in {"succeeded", "skipped"}
    }
    normalized_steps: list[PlanStep] = []
    for step in proposed.steps:
        terminal = terminal_steps.get(step.step_id)
        if terminal is not None:
            normalized_steps.append(_step_draft_from_persisted(terminal))
            continue
        normalized_steps.append(
            step.model_copy(
                update={
                    "status": "pending",
                    "background_job_id": None,
                    "retry_count": 0,
                    "output_summary": {},
                    "standardized_error_code": None,
                    "human_gate_confirmed": False,
                    "updated_at": None,
                }
            )
        )
    return proposed.model_copy(update={"steps": normalized_steps})


def _merge_natural_language_revision(
    current: WorkflowPlan,
    generated: Any,
) -> Any:
    """Keep terminal steps fixed while fitting a newly planned suffix.

    The repository performs the final immutable-step check.  This projection
    gives a natural-language revision a complete draft even when the planner
    only describes the requested unfinished work: completed/skipped slots are
    preserved at their original sequence, unfinished slots are replaced in
    order, and additional generated steps are appended.
    """

    terminal_steps = {
        step.step_id: step
        for step in current.steps
        if step.status in {"succeeded", "skipped"}
    }
    # Planner step ids are model-generated and commonly restart at ``step-1``
    # on every revision.  Keep colliding candidates as new work; the id
    # allocator below renames them instead of accidentally dropping the
    # requested change alongside an immutable completed step.
    generated_steps = list(generated.steps)
    current_step_ids = {step.step_id for step in current.steps}
    used_ids = set(terminal_steps)
    generated_id_map: dict[str, str] = {}
    generated_step_ids: set[str] = set()

    def unique_step_id(
        candidate: PlanStep,
        *,
        existing_step_id: str | None = None,
    ) -> str:
        base = candidate.step_id
        if (
            base not in used_ids
            and (base not in current_step_ids or base == existing_step_id)
        ):
            used_ids.add(base)
            return base
        index = 1
        while True:
            suffix = f"-rev-{current.revision + 1}-{index}"
            value = f"{base[: max(1, 128 - len(suffix))]}{suffix}"
            if value not in used_ids:
                used_ids.add(value)
                return value
            index += 1

    generated_index = 0
    merged: list[PlanStep] = []
    for old_step in current.steps:
        if old_step.status in {"succeeded", "skipped"}:
            merged.append(_step_draft_from_persisted(old_step))
            continue
        if generated_index < len(generated_steps):
            candidate = generated_steps[generated_index]
            generated_index += 1
            new_step_id = unique_step_id(
                candidate,
                existing_step_id=old_step.step_id,
            )
            generated_id_map[candidate.step_id] = new_step_id
            generated_step_ids.add(new_step_id)
            merged.append(
                candidate.model_copy(
                    update={
                        "step_id": new_step_id,
                        "sequence": old_step.sequence,
                    }
                )
            )
            continue
        # If the model returned fewer steps than the current unfinished
        # portion, retain the old step rather than silently deleting work.
        retained = _step_draft_from_persisted(old_step).model_copy(
            update={
                "status": "pending",
                "background_job_id": None,
                "retry_count": 0,
                "output_summary": {},
                "standardized_error_code": None,
                "human_gate_confirmed": False,
            }
        )
        used_ids.add(retained.step_id)
        merged.append(retained)
    while generated_index < len(generated_steps):
        candidate = generated_steps[generated_index]
        generated_index += 1
        new_step_id = unique_step_id(candidate)
        generated_id_map[candidate.step_id] = new_step_id
        generated_step_ids.add(new_step_id)
        merged.append(
            candidate.model_copy(
                update={
                    "step_id": new_step_id,
                    "sequence": len(merged) + 1,
                }
            )
        )
    if generated_id_map:
        remapped: list[PlanStep] = []
        for step in merged:
            if step.step_id not in generated_step_ids:
                remapped.append(step)
                continue
            input_summary = dict(step.input_summary)
            source_id = input_summary.get("create_task_step_id")
            if isinstance(source_id, str) and source_id in generated_id_map:
                input_summary["create_task_step_id"] = generated_id_map[source_id]
            bind_step_ids = input_summary.get("bind_step_ids")
            if isinstance(bind_step_ids, list):
                input_summary["bind_step_ids"] = [
                    generated_id_map.get(str(value), value)
                    for value in bind_step_ids
                ]
            remapped.append(step.model_copy(update={"input_summary": input_summary}))
        merged = remapped
    merged = [
        step.model_copy(update={"sequence": index, "updated_at": None})
        for index, step in enumerate(merged, start=1)
    ]
    if len(merged) > 1000:
        raise AssistantPolicyError("revised plan contains too many steps")
    project_ids = list(dict.fromkeys([*current.project_ids, *generated.project_ids]))
    return generated.model_copy(update={"project_ids": project_ids, "steps": merged})


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, WorkflowAssistantNotFound):
        return HTTPException(status_code=404, detail="Workflow Assistant resource not found.")
    if isinstance(exc, WorkflowAssistantConflict):
        detail: dict[str, Any] = {
            "error_code": exc.code,
            "message": str(exc),
        }
        if exc.current_revision is not None:
            detail["current_revision"] = exc.current_revision
        if exc.current_plan_hash:
            detail["current_plan_hash"] = exc.current_plan_hash
        if exc.current_steps:
            detail["current_steps"] = list(exc.current_steps)
            detail["current_step_count"] = len(exc.current_steps)
            detail["current_steps_truncated"] = len(exc.current_steps) >= 100
        return HTTPException(status_code=409, detail=detail)
    if isinstance(exc, AssistantContextError):
        if str(exc) in {
            "project access denied",
            "project scope contains an inaccessible project",
            "selected article task is outside or ambiguous in the project context",
        }:
            return HTTPException(status_code=422, detail=str(exc))
        return HTTPException(
            status_code=503,
            detail="Workflow Assistant project context is temporarily unavailable.",
        )
    if isinstance(exc, (PlannerOutputError, AssistantPolicyError)):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, PlannerUnavailable):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, AssistantMessageRouterUnavailable):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, GapFillNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, GapFillConflict):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, GapFillUnavailable):
        return HTTPException(status_code=503, detail=str(exc))
    return HTTPException(status_code=503, detail="Workflow Assistant is temporarily unavailable.")


def _authorized_plan_scope(
    request: Request,
    *,
    actor: ActorIdentity,
    project_ids: list[str] | tuple[str, ...],
    step_project_ids: list[str] | tuple[str, ...] = (),
) -> dict[str, dict[str, Any]]:
    """Re-authorize every project before a plan is persisted or executed.

    The resolver uses the same SQL-backed directory and access matrix as the
    existing Server APIs.  Checking the step bindings separately prevents a
    malformed revision from smuggling a project outside the plan scope.
    """

    normalized_projects = tuple(dict.fromkeys(item.strip() for item in project_ids if item.strip()))
    if not normalized_projects:
        raise AssistantPolicyError("plan must contain at least one project")
    normalized_steps = {item.strip() for item in step_project_ids if item.strip()}
    if not normalized_steps.issubset(set(normalized_projects)):
        raise AssistantPolicyError("every step must belong to a plan project")
    context = _context(request).resolve(actor=actor, project_ids=list(normalized_projects))
    return {
        project.project_id: {
            "effective_role": project.effective_role,
            "project_revision": project.revision,
        }
        for project in context.projects
    }


def _reauthorize_plan_read(
    request: Request,
    *,
    actor: ActorIdentity,
    plan: WorkflowPlan,
) -> None:
    """Keep plan projections behind the same live project scope as writes."""

    _authorized_plan_scope(
        request,
        actor=actor,
        project_ids=plan.project_ids,
        step_project_ids=tuple(step.project_id for step in plan.steps),
    )


def _reauthorize_plan_summary_read(
    request: Request,
    *,
    actor: ActorIdentity,
    plan: WorkflowPlan | WorkflowPlanSummaryRecord,
) -> None:
    """Recheck only project permissions for the lightweight attention inbox."""

    _context(request).authorize_project_scope(
        actor=actor,
        project_ids=plan.project_ids,
        step_project_ids=(
            plan.step_project_ids
            if isinstance(plan, WorkflowPlanSummaryRecord)
            else tuple(step.project_id for step in plan.steps)
        ),
    )


def _request_plan_job_cancellation(
    request: Request,
    *,
    actor: ActorIdentity,
    plan: WorkflowPlan,
) -> None:
    """Ask the existing Server Job control plane to stop queued work.

    The assistant plan is already cancelled before this best-effort sweep
    runs.  That ordering makes cancellation durable even when a particular
    underlying Job is already terminal or belongs to a domain with its own
    cancellation protocol (for example knowledge research).  No provider
    error or internal Job payload is exposed in the public timeline.
    """

    service = getattr(request.app.state, "server_job_control", None)
    cancel_job = getattr(service, "cancel_job", None)
    if not callable(cancel_job):
        return
    repository = _repository(request)
    for step in plan.steps:
        job_id = str(step.background_job_id or "").strip()
        if not job_id or step.status not in {"running", "waiting_job"}:
            continue
        try:
            cancel_job(
                actor=actor,
                project_id=step.project_id,
                job_id=job_id,
            )
        except Exception:
            # The plan cancellation itself is authoritative.  Keep a safe
            # public audit marker so the UI can tell that an underlying Job
            # still needs service-specific follow-up.
            try:
                repository.append_event(
                    actor=actor,
                    plan_id=plan.plan_id,
                    event_kind="background_job_cancel_unavailable",
                    public_payload={
                        "step_id": step.step_id,
                        "background_job_id": job_id,
                        "error_code": "background_job_cancel_unavailable",
                    },
                )
            except Exception:
                continue


def _execute_read_only_plan(
    request: Request,
    *,
    actor: ActorIdentity,
    context: AssistantWorkspaceContext,
    plan: WorkflowPlan,
    repository: PostgresWorkflowAssistantRepository,
) -> dict[str, dict[str, Any]]:
    """Run only the closed read projection for a non-writing plan."""

    configured_registry = getattr(request.app.state, "workflow_assistant_tools", None)
    if isinstance(configured_registry, WorkflowToolRegistry):
        # The application registry wires evidence_query to the existing
        # project-scoped Research Chat service. It is still closed and
        # permission-checked by WorkflowToolRegistry; the fallback below is
        # only for isolated tests or an intentionally partial startup.
        registry = configured_registry
    else:
        registry = WorkflowToolRegistry(
            access=_context(request).access,
            handlers=build_read_only_tool_handlers(
                context=context,
                plan_status=lambda requested_plan_id: {
                    "plan_id": requested_plan_id,
                    "status": repository.get_plan(
                        actor=actor,
                        plan_id=requested_plan_id,
                    ).status,
                },
            ),
        )
    outputs: dict[str, dict[str, Any]] = {}
    for step in plan.steps:
        claimed = False
        deadline = time.monotonic() + 10.0
        while not claimed:
            current = repository.get_plan(actor=actor, plan_id=plan.plan_id)
            current_step = next(
                (item for item in current.steps if item.step_id == step.step_id),
                None,
            )
            if current_step is None:
                raise WorkflowAssistantConflict("read-only step disappeared")
            if current_step.status in {"succeeded", "skipped"}:
                outputs[step.step_id] = current_step.output_summary
                break
            if current_step.status in {"failed", "cancelled"}:
                raise WorkflowAssistantConflict("read-only plan cannot be replayed")
            if current.status == "completed":
                outputs.update(
                    {
                        item.step_id: item.output_summary
                        for item in current.steps
                        if item.status in {"succeeded", "skipped"}
                    }
                )
                break
            if current_step.status == "pending":
                claimed = repository.claim_step(
                    actor=actor,
                    plan_id=plan.plan_id,
                    step_id=step.step_id,
                    allow_unconfirmed_read_only=True,
                )
                if claimed:
                    break
            if time.monotonic() >= deadline:
                raise WorkflowAssistantConflict("read-only step remained in progress")
            # A concurrent idempotent request owns the short read operation;
            # wait for its durable step result instead of executing it twice.
            time.sleep(0.05)
        try:
            if not claimed:
                continue
            output = registry.invoke(
                WorkflowToolInvocation(
                    actor=actor,
                    plan_id=plan.plan_id,
                    step_id=step.step_id,
                    action_kind=step.action_kind,  # type: ignore[arg-type]
                    project_id=step.project_id,
                    article_task_id=step.article_task_id,
                    expected_task_revision=step.expected_task_revision,
                    input_summary=step.input_summary,
                    pinned_prompt_version=step.pinned_prompt_version,
                    pinned_knowledge_snapshot=step.pinned_knowledge_snapshot,
                    hard_gate=False,
                    confirmed=False,
                )
            )
        except Exception as exc:
            repository.finish_step(
                actor=actor,
                plan_id=plan.plan_id,
                step_id=step.step_id,
                status="failed",
                standardized_error_code=type(exc).__name__,
            )
            raise
        committed = repository.finish_step(
            actor=actor,
            plan_id=plan.plan_id,
            step_id=step.step_id,
            status="succeeded",
            output_summary=output,
        )
        if not committed:
            current = repository.get_plan(actor=actor, plan_id=plan.plan_id)
            current_step = next(
                item for item in current.steps if item.step_id == step.step_id
            )
            if current_step.status in {"succeeded", "skipped"}:
                outputs[step.step_id] = current_step.output_summary
                continue
            raise WorkflowAssistantConflict("read-only step commit was lost")
        outputs[step.step_id] = output
    return outputs


def _read_only_message(outputs: dict[str, dict[str, Any]]) -> str:
    """Render safe public projections without provider responses or traces."""

    serialized = json.dumps(outputs, ensure_ascii=False, sort_keys=True)
    return f"只读查询已完成。\n{serialized[:12000]}"


def _run_and_complete_read_only_plan(
    request: Request,
    *,
    actor: ActorIdentity,
    context: AssistantWorkspaceContext,
    plan: WorkflowPlan,
    repository: PostgresWorkflowAssistantRepository,
) -> tuple[WorkflowPlan, dict[str, dict[str, Any]]]:
    outputs = _execute_read_only_plan(
        request,
        actor=actor,
        context=context,
        plan=plan,
        repository=repository,
    )
    current = repository.get_plan(actor=actor, plan_id=plan.plan_id)
    if current.status in {"draft", "awaiting_confirmation"}:
        current = repository.complete_read_only_plan(
            actor=actor,
            plan_id=plan.plan_id,
            outputs=outputs,
        )
    elif current.status != "completed":
        raise WorkflowAssistantConflict("read-only plan is not complete")
    return current, outputs


def _assistant_content_for_plan(plan: WorkflowPlan) -> str:
    """Rebuild the safe assistant acknowledgement for an idempotent retry."""

    if any(step.action_kind in WRITE_ACTION_KINDS for step in plan.steps):
        return (
            f"\u5df2\u751f\u6210\u8ba1\u5212\u201c{plan.title}\u201d\uff0c\u5171 {len(plan.steps)} \u4e2a\u6b65\u9aa4\uff0c"
            "\u7b49\u5f85\u786e\u8ba4\u540e\u6267\u884c\u3002"
        )
    outputs = {
        step.step_id: step.output_summary
        for step in plan.steps
        if step.status in {"succeeded", "skipped"}
    }
    return _read_only_message(outputs)


def _record_planner_usage(
    request: Request,
    *,
    actor: ActorIdentity,
    plan: WorkflowPlan,
    request_id: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    model_identity: PlannerModelIdentity | None = None,
) -> None:
    """Record one bounded planner call without making telemetry a dependency.

    The current LLM client returns text only, so the token fields use the
    provider-neutral estimate produced by the planner.  A provider's exact
    usage metadata can replace the estimate later without changing the
    durable scope or idempotency contract.
    """

    config = getattr(request.app.state, "article_agent_config", None)
    provider = str(
        (
            model_identity.provider
            if model_identity is not None
            else getattr(config, "llm_provider", "unknown")
        )
        or "unknown"
    ).strip()
    model = str(
        (
            model_identity.model
            if model_identity is not None
            else getattr(config, "llm_model", "unknown")
        )
        or "unknown"
    ).strip()
    if not provider or not model:
        return
    project_count = max(1, len(plan.project_ids))
    per_project_input = (max(0, input_tokens) + project_count - 1) // project_count
    per_project_output = (max(0, output_tokens) + project_count - 1) // project_count
    try:
        repository = _repository(request)
    except Exception:
        return
    for project_id in plan.project_ids:
        try:
            repository.record_usage(
                actor=actor,
                provider=provider,
                model=model,
                operation_kind="workflow_planner",
                input_tokens=per_project_input,
                output_tokens=per_project_output,
                project_id=project_id,
                plan_id=plan.plan_id,
                usage_event_id=_assistant_identity(
                    f"{request_id}:{plan.plan_id}:{project_id}",
                    prefix="usage",
                ),
            )
        except Exception:
            # Usage accounting must never turn a valid plan into a failed
            # workflow.  The plan/event rows remain the source of truth.
            continue


def _planning_dispatch_response(
    request: Request,
    *,
    actor: ActorIdentity,
    dispatch: WorkflowAssistantDispatch,
) -> AssistantMessageDispatchResponse:
    """Project a planning lease without exposing its request internals."""

    repository = _repository(request)
    plan = repository.get_plan_by_idempotency(
        actor=actor,
        conversation_id=dispatch.conversation_id,
        source_idempotency_key=dispatch.idempotency_key,
    )
    if plan is not None:
        _reauthorize_plan_read(request, actor=actor, plan=plan)
    message = None
    try:
        conversation = repository.get_conversation(
            actor=actor,
            conversation_id=dispatch.conversation_id,
        )
        assistant_key = _assistant_identity(
            dispatch.idempotency_key,
            prefix="asst",
        )
        assistant = next(
            (
                item
                for item in conversation.messages
                if item.idempotency_key == assistant_key
            ),
            None,
        )
        if assistant is not None:
            message = _message_response(assistant)
    except WorkflowAssistantNotFound:
        # The dispatch is still private to the actor; a short-lived message
        # retention cleanup must not make its status endpoint leak anything.
        pass
    return AssistantMessageDispatchResponse(
        message=message,
        plan=_plan_response(plan) if plan is not None else None,
        dispatch_id=dispatch.dispatch_id,
        dispatch_status=dispatch.status,
        dispatch_error_code=dispatch.error_code,
    )


def _planner_recent_messages(
    conversation: AssistantConversation,
) -> tuple[dict[str, str], ...]:
    """Expose only bounded, visible conversation text to the planner."""

    return tuple(
        {
            "role": message.role,
            "content": message.content[:2_000],
        }
        for message in conversation.messages[-8:]
        if message.role in {"user", "assistant"} and message.content.strip()
    )


def _planner_plan_summary(plan: WorkflowPlan | None) -> dict[str, Any]:
    """Build a compact, non-authoritative view of the latest plan.

    The planner uses this only to understand short follow-ups such as
    "继续". Step state, task identity, and authorization remain owned by the
    PostgreSQL plan and are checked again by the normal execution path.
    """

    if plan is None:
        return {}
    return {
        "plan_id": plan.plan_id,
        "title": plan.title,
        "status": plan.status,
        "revision": plan.revision,
        "project_ids": list(plan.project_ids),
        "step_count": len(plan.steps),
        "steps": [
            {
                "sequence": step.sequence,
                "action_kind": step.action_kind,
                "project_id": step.project_id,
                "article_task_id": step.article_task_id,
                "status": step.status,
            }
            for step in plan.steps[:120]
        ],
    }


def process_planning_dispatch(
    request: Request,
    *,
    dispatch: WorkflowAssistantDispatch,
    worker_id: str,
) -> None:
    """Run one durable planning dispatch from a server worker.

    This is intentionally separate from the HTTP handler.  It can be called
    after a proxy timeout, after a browser restart, or by a different web
    process that claimed the same PostgreSQL lease.  All plan creation and
    assistant-message writes remain idempotent on the original key.
    """

    repository = _repository(request)
    actor = ActorIdentity(
        dispatch.organization_id,
        dispatch.creator_user_id,
    )
    try:
        existing_plan = repository.get_plan_by_idempotency(
            actor=actor,
            conversation_id=dispatch.conversation_id,
            source_idempotency_key=dispatch.idempotency_key,
        )
        if existing_plan is not None:
            _reauthorize_plan_read(request, actor=actor, plan=existing_plan)
            if (
                not requires_confirmation(existing_plan)
                and existing_plan.status != "completed"
            ):
                # A provider/database retry can arrive after a read-only plan
                # was persisted but before its tool outputs were committed.
                # Re-run that durable read projection instead of acknowledging
                # an unfinished plan as if it had completed.
                existing_context = _context(request).resolve(
                    actor=actor,
                    project_ids=list(existing_plan.project_ids),
                )
                existing_plan, outputs = _run_and_complete_read_only_plan(
                    request,
                    actor=actor,
                    context=existing_context,
                    plan=existing_plan,
                    repository=repository,
                )
                assistant_content = _read_only_message(outputs)
            else:
                assistant_content = _assistant_content_for_plan(existing_plan)
            assistant_message = repository.append_message(
                actor=actor,
                conversation_id=dispatch.conversation_id,
                role="assistant",
                content=assistant_content,
                request_id=_assistant_identity(
                    dispatch.request_id,
                    prefix="asst_req",
                ),
                idempotency_key=_assistant_identity(
                    dispatch.idempotency_key,
                    prefix="asst",
                ),
            )
            del assistant_message
            repository.complete_planning_dispatch(
                dispatch=dispatch,
                worker_id=worker_id,
                plan_id=existing_plan.plan_id,
            )
            return

        context = _context(request).resolve(
            actor=actor,
            project_ids=list(dispatch.project_ids),
        )
        selected_task_ids = tuple(dispatch.article_task_ids)
        task_projects: dict[str, set[str]] = {}
        for project in context.projects:
            for task in project.tasks:
                task_projects.setdefault(task.task_id, set()).add(project.project_id)
        if any(
            task_id not in task_projects or len(task_projects[task_id]) != 1
            for task_id in selected_task_ids
        ):
            raise AssistantContextError(
                "selected article task is outside or ambiguous in the project context"
            )
        conversation = repository.get_conversation(
            actor=actor,
            conversation_id=dispatch.conversation_id,
        )
        latest_plan = repository.get_latest_plan_for_conversation(
            actor=actor,
            conversation_id=dispatch.conversation_id,
        )
        recent_messages = _planner_recent_messages(conversation)
        active_plan_summary = _planner_plan_summary(latest_plan)
        planner = _planner(request)
        planner_started_at = time.monotonic()
        LOGGER.info(
            "workflow assistant durable planner started: dispatch_id=%s "
            "project_count=%s selected_task_count=%s",
            dispatch.dispatch_id,
            len(context.project_ids),
            len(selected_task_ids),
        )
        plan = planner.plan(
            actor=actor,
            request=dispatch.content,
            context=context,
            selected_project_ids=context.project_ids,
            selected_task_ids=selected_task_ids,
            allowed_action_kinds=ASSISTANT_ALLOWED_ACTION_KINDS,
            recent_messages=recent_messages,
            active_plan_summary=active_plan_summary,
        )
        LOGGER.info(
            "workflow assistant durable planner completed: dispatch_id=%s "
            "duration_seconds=%.3f step_count=%s",
            dispatch.dispatch_id,
            time.monotonic() - planner_started_at,
            len(plan.steps),
        )
        planner_model_identity = planner.consume_model_identity()
        usage = estimate_planner_usage(
            dispatch.content,
            context,
            selected_project_ids=context.project_ids,
            selected_task_ids=selected_task_ids,
            plan=plan,
            allowed_action_kinds=ASSISTANT_ALLOWED_ACTION_KINDS,
            recent_messages=recent_messages,
            active_plan_summary=active_plan_summary,
        )
        plan = bind_plan_context(
            plan,
            context=context,
            selected_task_ids=(
                selected_task_ids
                if dispatch.article_task_selection_locked
                else None
            ),
        )
        plan = _apply_execution_limits(request, plan)
        authorization_snapshot = _authorized_plan_scope(
            request,
            actor=actor,
            project_ids=plan.project_ids,
            step_project_ids=tuple(step.project_id for step in plan.steps),
        )
        persisted_plan = repository.create_plan(
            actor=actor,
            conversation_id=dispatch.conversation_id,
            plan=plan,
            authorization_snapshot=authorization_snapshot,
            source_idempotency_key=dispatch.idempotency_key,
        )
        _record_planner_usage(
            request,
            actor=actor,
            plan=persisted_plan,
            request_id=dispatch.request_id,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            model_identity=planner_model_identity,
        )
        if requires_confirmation(plan):
            final_plan = persisted_plan
            assistant_content = _assistant_content_for_plan(persisted_plan)
        else:
            final_plan, outputs = _run_and_complete_read_only_plan(
                request,
                actor=actor,
                context=context,
                plan=persisted_plan,
                repository=repository,
            )
            assistant_content = _read_only_message(outputs)
        repository.append_message(
            actor=actor,
            conversation_id=dispatch.conversation_id,
            role="assistant",
            content=assistant_content,
            request_id=_assistant_identity(
                dispatch.request_id,
                prefix="asst_req",
            ),
            idempotency_key=_assistant_identity(
                dispatch.idempotency_key,
                prefix="asst",
            ),
        )
        if not repository.complete_planning_dispatch(
            dispatch=dispatch,
            worker_id=worker_id,
            plan_id=final_plan.plan_id,
        ):
            LOGGER.info(
                "planning dispatch lease changed before completion: dispatch_id=%s",
                dispatch.dispatch_id,
            )
    except Exception:
        LOGGER.exception(
            "workflow assistant durable planner failed: dispatch_id=%s",
            dispatch.dispatch_id,
        )
        raise


@router.post(
    "/conversations",
    response_model=AssistantConversationResponse,
    status_code=201,
)
def create_conversation(
    payload: AssistantConversationCreateRequest,
    request: Request,
    actor: ActorIdentity = Depends(require_server_actor),
) -> AssistantConversationResponse:
    _feature_enabled(request)
    try:
        # Resolve the scope before persisting it, so a conversation cannot
        # advertise a project the actor cannot currently access.
        context = _context(request).resolve(
            actor=actor,
            project_ids=payload.project_ids or None,
        )
        project_ids = payload.project_ids or list(context.project_ids)
        conversation = _repository(request).create_conversation(
            actor=actor,
            title=payload.title,
            project_ids=project_ids,
        )
        return _conversation_response(conversation)
    except Exception as exc:
        raise _error(exc) from exc


def _batch_writing_request_summary(payload: BatchWritingPlanRequest) -> str:
    projects = "、".join(
        f"{item.project_id} {item.article_count}篇"
        for item in payload.projects
    )
    review = "跳过复检" if payload.skip_review else "包含复检"
    instruction = payload.writing_instruction.strip()
    suffix = f"；补充要求：{instruction}" if instruction else ""
    return sanitize_message(
        f"批量写作（结构化配置）：{projects}；{review}；并发上限 {payload.concurrency_limit}{suffix}",
        max_length=20_000,
    )


@router.post(
    "/conversations/{conversation_id}/batch-plans",
    response_model=AssistantMessageDispatchResponse,
)
def create_batch_writing_plan(
    conversation_id: str,
    payload: BatchWritingPlanRequest,
    request: Request,
    actor: ActorIdentity = Depends(require_server_actor),
) -> AssistantMessageDispatchResponse:
    """Create a durable fixed-writing plan from explicit form fields.

    This route deliberately does not invoke the natural-language planner. The
    browser owns only the batch configuration; project context, topic
    availability, prompt/knowledge pins, authorization and Task revisions are
    resolved and checked by the Server before the plan is persisted.
    """

    _feature_enabled(request)
    repository = _repository(request)
    try:
        conversation = repository.get_conversation(
            actor=actor,
            conversation_id=conversation_id,
            include_messages=False,
        )
        project_ids = [item.project_id for item in payload.projects]
        if set(conversation.project_ids) != set(project_ids):
            raise AssistantPolicyError(
                "批量写作项目范围与会话范围不一致，请重新开始"
            )

        request_summary = _batch_writing_request_summary(payload)
        existing_user_message = repository.get_message_by_idempotency(
            actor=actor,
            conversation_id=conversation_id,
            idempotency_key=payload.idempotency_key,
        )
        if existing_user_message is not None and (
            existing_user_message.content != request_summary
            or existing_user_message.request_id != payload.request_id
        ):
            raise WorkflowAssistantConflict(
                "batch writing idempotency key already has different configuration"
            )

        existing_plan = repository.get_plan_by_idempotency(
            actor=actor,
            conversation_id=conversation_id,
            source_idempotency_key=payload.idempotency_key,
        )
        if existing_plan is not None:
            if existing_plan.natural_language_request != request_summary:
                raise WorkflowAssistantConflict(
                    "batch writing idempotency key already has different configuration"
                )
            _reauthorize_plan_read(request, actor=actor, plan=existing_plan)
            assistant_message = repository.get_message_by_idempotency(
                actor=actor,
                conversation_id=conversation_id,
                idempotency_key=_assistant_identity(
                    payload.idempotency_key,
                    prefix="asst",
                ),
            )
            return AssistantMessageDispatchResponse(
                message=(
                    _message_response(assistant_message)
                    if assistant_message is not None
                    else None
                ),
                plan=_plan_response(existing_plan),
            )

        latest_plan = repository.get_latest_plan_for_conversation(
            actor=actor,
            conversation_id=conversation_id,
        )
        if latest_plan is not None and latest_plan.status not in {
            "completed",
            "cancelled",
            "failed",
        }:
            raise AssistantPolicyError(
                "当前会话已有未完成批量计划，请先处理或取消它"
            )

        context = _context(request).resolve(
            actor=actor,
            project_ids=project_ids,
        )
        repository.append_message(
            actor=actor,
            conversation_id=conversation_id,
            role="user",
            content=request_summary,
            request_id=payload.request_id,
            idempotency_key=payload.idempotency_key,
        )
        fixed_plan = build_fixed_article_plan(
            request_summary,
            context,
            article_counts={
                item.project_id: item.article_count
                for item in payload.projects
            },
            writing_instruction=payload.writing_instruction,
            skip_review=payload.skip_review,
            concurrency_limit=payload.concurrency_limit,
        )
        fixed_plan = fixed_plan.model_copy(
            update={
                "title": (
                    f"批量写作 · {len(payload.projects)} 个项目 · "
                    f"{sum(item.article_count for item in payload.projects)} 篇"
                ),
            }
        )
        fixed_plan = bind_plan_context(
            fixed_plan,
            context=context,
        )
        fixed_plan = _apply_execution_limits(request, fixed_plan)
        authorization_snapshot = _authorized_plan_scope(
            request,
            actor=actor,
            project_ids=fixed_plan.project_ids,
            step_project_ids=tuple(
                step.project_id for step in fixed_plan.steps
            ),
        )
        persisted_plan = repository.create_plan(
            actor=actor,
            conversation_id=conversation_id,
            plan=fixed_plan,
            authorization_snapshot=authorization_snapshot,
            source_idempotency_key=payload.idempotency_key,
        )
        assistant_message = repository.append_message(
            actor=actor,
            conversation_id=conversation_id,
            role="assistant",
            content=_assistant_content_for_plan(persisted_plan),
            request_id=_assistant_identity(
                payload.request_id,
                prefix="asst_req",
            ),
            idempotency_key=_assistant_identity(
                payload.idempotency_key,
                prefix="asst",
            ),
        )
        return AssistantMessageDispatchResponse(
            message=_message_response(assistant_message),
            plan=_plan_response(persisted_plan),
        )
    except Exception as exc:
        raise _error(exc) from exc


@router.get(
    "/conversations",
    response_model=AssistantConversationListResponse,
)
def list_conversations(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    actor: ActorIdentity = Depends(require_server_actor),
) -> AssistantConversationListResponse:
    _feature_enabled(request)
    try:
        conversations = _repository(request).list_conversations(actor=actor, limit=limit)
        # The list route is only a private session index. Resolving the full
        # project context once per conversation made the first screen scale
        # with every conversation's knowledge/tasks. Directory membership is
        # the same access boundary used by the resolver; the selected
        # conversation is still fully re-authorized by its detail route.
        accessible_project_ids = {
            project.project_id
            for project in _context(request).accessible_projects(actor)
        }
        visible_conversations = [
            conversation
            for conversation in conversations
            if not conversation.project_ids
            or set(conversation.project_ids).issubset(accessible_project_ids)
        ]
        return AssistantConversationListResponse(
            conversations=[_conversation_response(item) for item in visible_conversations]
        )
    except Exception as exc:
        raise _error(exc) from exc


@router.get(
    "/conversations/{conversation_id}",
    response_model=AssistantConversationResponse,
)
def get_conversation(
    conversation_id: str,
    request: Request,
    actor: ActorIdentity = Depends(require_server_actor),
) -> AssistantConversationResponse:
    _feature_enabled(request)
    try:
        conversation = _repository(request).get_conversation(
            actor=actor,
            conversation_id=conversation_id,
        )
        try:
            _context(request).resolve(
                actor=actor,
                project_ids=list(conversation.project_ids) or None,
            )
        except AssistantContextError as exc:
            if str(exc) in {
                "project access denied",
                "project scope contains an inaccessible project",
            }:
                raise WorkflowAssistantNotFound("conversation not found") from exc
            raise
        return _conversation_response(conversation)
    except Exception as exc:
        raise _error(exc) from exc


@router.get(
    "/conversations/{conversation_id}/latest-plan",
    response_model=WorkflowPlanResponse | None,
)
def get_latest_conversation_plan(
    conversation_id: str,
    request: Request,
    actor: ActorIdentity = Depends(require_server_actor),
) -> WorkflowPlanResponse | None:
    """Restore the creator's latest durable plan after a browser restart."""

    _feature_enabled(request)
    try:
        repository = _repository(request)
        # Fail closed through the private conversation lookup first. A plan
        # remains durable longer than messages, but this route is explicitly
        # conversation-scoped and must never become a plan enumeration API.
        repository.get_conversation(
            actor=actor,
            conversation_id=conversation_id,
            include_messages=False,
        )
        plan = repository.get_latest_plan_for_conversation(
            actor=actor,
            conversation_id=conversation_id,
        )
        if plan is None:
            return None
        _reauthorize_plan_read(request, actor=actor, plan=plan)
        if plan.attention_state == "unread":
            plan = repository.mark_plan_seen(
                actor=actor,
                plan_id=plan.plan_id,
            )
        return _plan_response(plan)
    except Exception as exc:
        raise _error(exc) from exc


@router.get(
    "/conversations/{conversation_id}/dispatches/{dispatch_id}",
    response_model=AssistantMessageDispatchResponse,
)
def get_planning_dispatch(
    conversation_id: str,
    dispatch_id: str,
    request: Request,
    actor: ActorIdentity = Depends(require_server_actor),
) -> AssistantMessageDispatchResponse:
    """Poll the durable planning hand-off after the POST has returned."""

    _feature_enabled(request)
    try:
        repository = _repository(request)
        dispatch = repository.get_planning_dispatch(
            actor=actor,
            dispatch_id=dispatch_id,
        )
        if dispatch.conversation_id != conversation_id:
            raise WorkflowAssistantNotFound("planning dispatch not found")
        return _planning_dispatch_response(
            request,
            actor=actor,
            dispatch=dispatch,
        )
    except Exception as exc:
        raise _error(exc) from exc


@router.get(
    "/conversations/{conversation_id}/dispatches",
    response_model=AssistantMessageDispatchResponse,
)
def get_planning_dispatch_by_idempotency(
    conversation_id: str,
    request: Request,
    idempotency_key: str = Query(..., min_length=1, max_length=128),
    actor: ActorIdentity = Depends(require_server_actor),
) -> AssistantMessageDispatchResponse:
    """Recover a planning hand-off when the original POST response was lost."""

    _feature_enabled(request)
    try:
        repository = _repository(request)
        dispatch = repository.get_planning_dispatch_by_idempotency(
            actor=actor,
            conversation_id=conversation_id,
            idempotency_key=idempotency_key,
        )
        return _planning_dispatch_response(
            request,
            actor=actor,
            dispatch=dispatch,
        )
    except Exception as exc:
        raise _error(exc) from exc


@router.get(
    "/conversations/{conversation_id}/active-dispatch",
    response_model=AssistantMessageDispatchResponse | None,
)
def get_active_planning_dispatch(
    conversation_id: str,
    request: Request,
    actor: ActorIdentity = Depends(require_server_actor),
) -> AssistantMessageDispatchResponse | None:
    """Restore an in-flight planner lease after a page refresh."""

    _feature_enabled(request)
    try:
        repository = _repository(request)
        # Authorize the conversation before returning whether a lease exists.
        repository.get_conversation(
            actor=actor,
            conversation_id=conversation_id,
            include_messages=False,
        )
        dispatch = repository.get_active_planning_dispatch(
            actor=actor,
            conversation_id=conversation_id,
        )
        if dispatch is None:
            return None
        return _planning_dispatch_response(
            request,
            actor=actor,
            dispatch=dispatch,
        )
    except Exception as exc:
        raise _error(exc) from exc


@router.post(
    "/conversations/{conversation_id}/messages",
    response_model=AssistantMessageDispatchResponse,
)
def append_message(
    conversation_id: str,
    payload: AssistantMessageRequest,
    request: Request,
    actor: ActorIdentity = Depends(require_server_actor),
) -> AssistantMessageDispatchResponse:
    _feature_enabled(request)
    repository = _repository(request)
    try:
        if payload.workflow_mode == "article":
            raise AssistantPolicyError(
                "文章生成已移至批量写文章页面；助手仅负责提示词配置和资料查询"
            )
        conversation = repository.get_conversation(
            actor=actor,
            conversation_id=conversation_id,
            include_messages=False,
        )
        existing_user_message = repository.get_message_by_idempotency(
            actor=actor,
            conversation_id=conversation_id,
            idempotency_key=payload.idempotency_key,
        )
        should_append_user_message = existing_user_message is None
        if existing_user_message is not None:
            # A retried message must not call the model or create a second
            # plan when the first request completed.  If planning failed
            # before the assistant reply was persisted, keep the user row and
            # allow the same idempotency key to retry safely.
            if (
                existing_user_message.content != sanitize_message(payload.content)
                or existing_user_message.request_id != payload.request_id
            ):
                raise WorkflowAssistantConflict(
                    "message idempotency key already has different content"
                )
            persisted_conversation = repository.get_conversation(
                actor=actor,
                conversation_id=conversation_id,
            )
            assistant_message = next(
                (
                    message
                    for message in persisted_conversation.messages
                    if message.idempotency_key
                    == _assistant_identity(payload.idempotency_key, prefix="asst")
                ),
                None,
            )
            if assistant_message is None:
                should_append_user_message = False
            else:
                existing_plan = repository.get_plan_by_idempotency(
                    actor=actor,
                    conversation_id=conversation_id,
                    source_idempotency_key=payload.idempotency_key,
                )
                if existing_plan is not None:
                    _reauthorize_plan_read(
                        request,
                        actor=actor,
                        plan=existing_plan,
                    )
                return AssistantMessageDispatchResponse(
                    message=_message_response(assistant_message),
                    plan=_plan_response(existing_plan) if existing_plan else None,
                )
            existing_plan = repository.get_plan_by_idempotency(
                actor=actor,
                conversation_id=conversation_id,
                source_idempotency_key=payload.idempotency_key,
            )
            if existing_plan is not None:
                _reauthorize_plan_read(
                    request,
                    actor=actor,
                    plan=existing_plan,
                )
                if not any(
                    step.action_kind in WRITE_ACTION_KINDS
                    for step in existing_plan.steps
                ):
                    existing_context = _context(request).resolve(
                        actor=actor,
                        project_ids=list(existing_plan.project_ids),
                    )
                    existing_plan, outputs = _run_and_complete_read_only_plan(
                        request,
                        actor=actor,
                        context=existing_context,
                        plan=existing_plan,
                        repository=repository,
                    )
                    assistant_content = _read_only_message(outputs)
                else:
                    assistant_content = _assistant_content_for_plan(existing_plan)
                assistant_message = repository.append_message(
                    actor=actor,
                    conversation_id=conversation_id,
                    role="assistant",
                    content=assistant_content,
                    request_id=_assistant_identity(
                        payload.request_id,
                        prefix="asst_req",
                    ),
                    idempotency_key=_assistant_identity(
                        payload.idempotency_key,
                        prefix="asst",
                    ),
                )
                return AssistantMessageDispatchResponse(
                    message=_message_response(assistant_message),
                    plan=_plan_response(existing_plan),
                )
        if should_append_user_message:
            repository.append_message(
                actor=actor,
                conversation_id=conversation_id,
                role="user",
                content=payload.content,
                request_id=payload.request_id,
                idempotency_key=payload.idempotency_key,
            )
        # A concurrent retry may have won the idempotency race between the
        # preflight read above and append_message().  If its assistant reply
        # is already present, return it without planning a second workflow.
        persisted_conversation = repository.get_conversation(
            actor=actor,
            conversation_id=conversation_id,
        )
        existing_assistant = next(
            (
                message
                for message in persisted_conversation.messages
                if message.idempotency_key
                == _assistant_identity(payload.idempotency_key, prefix="asst")
            ),
            None,
        )
        if existing_assistant is not None:
            existing_plan = repository.get_plan_by_idempotency(
                actor=actor,
                conversation_id=conversation_id,
                source_idempotency_key=payload.idempotency_key,
            )
            if existing_plan is not None:
                _reauthorize_plan_read(
                    request,
                    actor=actor,
                    plan=existing_plan,
                )
            return AssistantMessageDispatchResponse(
                message=_message_response(existing_assistant),
                plan=_plan_response(existing_plan) if existing_plan else None,
            )
        selected_project_ids = payload.project_ids or list(conversation.project_ids)
        context = _context(request).resolve(
            actor=actor,
            project_ids=selected_project_ids or None,
        )
        selected_task_ids = tuple(payload.article_task_ids or ())
        task_projects: dict[str, set[str]] = {}
        for project in context.projects:
            for task in project.tasks:
                task_projects.setdefault(task.task_id, set()).add(project.project_id)
        if any(
            task_id not in task_projects or len(task_projects[task_id]) != 1
            for task_id in selected_task_ids
        ):
            raise AssistantContextError(
                "selected article task is outside or ambiguous in the project context"
            )
        if payload.project_ids is not None:
            conversation = repository.update_conversation_scope(
                actor=actor,
                conversation_id=conversation_id,
                project_ids=context.project_ids,
            )
        get_latest_plan = getattr(
            repository,
            "get_latest_plan_for_conversation",
            None,
        )
        latest_plan = (
            get_latest_plan(
                actor=actor,
                conversation_id=conversation_id,
            )
            if callable(get_latest_plan)
            else None
        )
        message_router = _message_router(request)
        intent = message_router.route(
            actor=actor,
            request=payload.content,
            context=context,
            has_active_plan=(
                latest_plan is not None
                and latest_plan.status not in {"completed", "cancelled"}
            ),
        )
        legacy_article_plan = bool(
            latest_plan
            and any(
                step.action_kind in TASK_BOUND_ACTION_KINDS
                or step.action_kind == "create_task"
                for step in latest_plan.steps
            )
        )
        legacy_article_follow_up = legacy_article_plan and payload.content.strip().casefold() in {
            "继续",
            "继续执行",
            "恢复",
            "重试",
            "resume",
            "continue",
            "retry",
            "暂停",
            "取消",
        }
        if intent.kind == "workflow" and (
            is_article_generation_request(payload.content)
            or legacy_article_follow_up
        ):
            assistant_content = (
                "文章生成和批次控制已移至“批量写文章”页面；当前助手只负责提示词配置和资料查询。"
                "请打开批量写文章选择项目、文章数量后提交，已有批次也可以在那里暂停、取消或导出。"
            )
            assistant_message = repository.append_message(
                actor=actor,
                conversation_id=conversation_id,
                role="assistant",
                content=assistant_content,
                request_id=_assistant_identity(payload.request_id, prefix="asst_req"),
                idempotency_key=_assistant_identity(
                    payload.idempotency_key,
                    prefix="asst",
                ),
            )
            return AssistantMessageDispatchResponse(
                message=_message_response(assistant_message),
                plan=None,
            )
        LOGGER.info(
            "workflow assistant message routed: request_id=%s kind=%s project_id=%s",
            payload.request_id,
            intent.kind,
            intent.project_id or "",
        )
        if intent.kind == "chat":
            assistant_content = message_router.chat_reply(
                actor=actor,
                request=payload.content,
                recent_messages=[
                    {"role": message.role, "content": message.content}
                    for message in persisted_conversation.messages
                ],
            )
            assistant_message = repository.append_message(
                actor=actor,
                conversation_id=conversation_id,
                role="assistant",
                content=assistant_content,
                request_id=_assistant_identity(payload.request_id, prefix="asst_req"),
                idempotency_key=_assistant_identity(
                    payload.idempotency_key,
                    prefix="asst",
                ),
            )
            return AssistantMessageDispatchResponse(
                message=_message_response(assistant_message),
                plan=None,
            )
        if intent.kind == "knowledge_qa":
            if intent.project_id is None:
                assistant_content = (
                    "知识库问答一次只查询一个项目。请在左侧只保留一个项目，"
                    "或者在问题中明确写出项目域名后再发送。"
                )
            else:
                if intent.project_id not in context.project_ids:
                    raise AssistantContextError(
                        "project scope contains an inaccessible project"
                    )
                article_id = (
                    selected_task_ids[0]
                    if len(selected_task_ids) == 1
                    and intent.project_id in task_projects[selected_task_ids[0]]
                    else None
                )
                assistant_content = _knowledge_reply(
                    request,
                    project_id=intent.project_id,
                    question=payload.content,
                    conversation_id=conversation_id,
                    request_id=_assistant_identity(
                        payload.idempotency_key,
                        prefix="knowledge_req",
                    ),
                    article_id=article_id,
                )
            assistant_message = repository.append_message(
                actor=actor,
                conversation_id=conversation_id,
                role="assistant",
                content=assistant_content,
                request_id=_assistant_identity(payload.request_id, prefix="asst_req"),
                idempotency_key=_assistant_identity(
                    payload.idempotency_key,
                    prefix="asst",
                ),
            )
            return AssistantMessageDispatchResponse(
                message=_message_response(assistant_message),
                plan=None,
            )
        # Do not hold the message HTTP request open while the planner makes
        # several model calls.  The durable runner will resolve the context,
        # create the immutable plan, and append the assistant acknowledgement.
        dispatch = repository.enqueue_planning_dispatch(
            actor=actor,
            conversation_id=conversation_id,
            content=payload.content,
            request_id=payload.request_id,
            idempotency_key=payload.idempotency_key,
            project_ids=context.project_ids,
            article_task_ids=selected_task_ids,
            article_task_selection_locked=payload.article_task_ids is not None,
        )
        runner = getattr(request.app.state, "workflow_assistant_runner", None)
        wake = getattr(runner, "wake", None)
        if callable(wake):
            wake()
        return AssistantMessageDispatchResponse(
            message=None,
            plan=None,
            dispatch_id=dispatch.dispatch_id,
            dispatch_status=dispatch.status,
            dispatch_error_code=dispatch.error_code,
        )
    except Exception as exc:
        raise _error(exc) from exc


@router.get(
    "/plans/{plan_id}",
    response_model=WorkflowPlanResponse,
)
def get_plan(
    plan_id: str,
    request: Request,
    actor: ActorIdentity = Depends(require_server_actor),
) -> WorkflowPlanResponse:
    _feature_enabled(request)
    try:
        plan = _repository(request).get_plan(actor=actor, plan_id=plan_id)
        _reauthorize_plan_read(request, actor=actor, plan=plan)
        if plan.attention_state == "unread":
            plan = _repository(request).mark_plan_seen(
                actor=actor,
                plan_id=plan.plan_id,
            )
        return _plan_response(plan)
    except Exception as exc:
        raise _error(exc) from exc


@router.get(
    "/plans/{plan_id}/delivery-package/download",
    response_model=WorkflowAssistantBatchDownloadResponse,
)
def create_plan_delivery_download(
    plan_id: str,
    request: Request,
    expires_seconds: int = Query(default=300, ge=30, le=3600),
    project_id: str | None = Query(default=None, min_length=1, max_length=128),
    actor: ActorIdentity = Depends(require_server_actor),
) -> WorkflowAssistantBatchDownloadResponse:
    """Create one download URL for every completed package in the plan.

    Without ``project_id`` the ZIP aggregates successful articles across all
    projects in the plan.  The optional project filter remains available for
    callers that need a single-project package.
    """

    _feature_enabled(request)
    try:
        repository = _repository(request)
        plan = repository.get_plan(actor=actor, plan_id=plan_id)
        _reauthorize_plan_read(request, actor=actor, plan=plan)
        package_steps = [
            step
            for step in plan.steps
            if step.action_kind == "package_delivery"
        ]
        if not package_steps:
            raise HTTPException(
                status_code=409,
                detail="当前计划没有可批量下载的交付包。",
            )

        # A partial plan is still allowed to produce a useful delivery ZIP.
        # A package step may still be waiting for its final human gate even
        # though the article Word and TDK artifacts are already complete. Keep
        # both forms of success so failed or unfinished lanes do not hide
        # downloadable articles from the operator.
        ready_package_steps = [
            step
            for step in package_steps
            if (
                step.status == "succeeded"
                and bool(step.article_task_id)
                and str(step.output_summary.get("artifact_kind") or "")
                == "delivery_package"
                and bool(str(step.output_summary.get("asset_id") or "").strip())
            )
        ]
        ready_package_by_task = {
            (step.project_id.strip(), str(step.article_task_id).strip()): str(
                step.output_summary.get("asset_id") or ""
            ).strip()
            for step in ready_package_steps
            if step.project_id.strip() and str(step.article_task_id or "").strip()
        }
        article_artifacts: dict[tuple[str, str], dict[str, str]] = {}
        for step in plan.steps:
            if (
                step.status != "succeeded"
                or not step.article_task_id
                or step.action_kind not in {"export_docx", "generate_tdk"}
            ):
                continue
            artifact_kind = str(step.output_summary.get("artifact_kind") or "").strip()
            asset_id = str(step.output_summary.get("asset_id") or "").strip()
            expected_kind = "docx" if step.action_kind == "export_docx" else "tdk"
            key = (step.project_id.strip(), str(step.article_task_id).strip())
            if (
                key[0]
                and key[1]
                and artifact_kind == expected_kind
                and asset_id
            ):
                article_artifacts.setdefault(key, {})[expected_kind] = asset_id
        partial_article_refs = {
            key
            for key, artifacts in article_artifacts.items()
            if artifacts.get("docx") and artifacts.get("tdk")
        }
        ready_article_refs: list[tuple[str, str]] = []
        for key in (
            list(ready_package_by_task)
            + [key for key in article_artifacts if key in partial_article_refs]
        ):
            if key not in ready_article_refs:
                ready_article_refs.append(key)

        if not ready_article_refs:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "当前计划没有已成功并可下载的文章。",
                    "ready_count": 0,
                    "total_count": len(package_steps),
                },
            )

        requested_project_id = project_id.strip() if project_id is not None else None
        if project_id is not None and not requested_project_id:
            raise HTTPException(
                status_code=422,
                detail="project_id 不能为空。",
            )
        if requested_project_id is not None:
            ready_article_refs = [
                key
                for key in ready_article_refs
                if key[0] == requested_project_id
            ]
            partial_article_refs = {
                key for key in partial_article_refs if key in ready_article_refs
            }
            if not ready_article_refs:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "指定项目没有已成功并可下载的文章。",
                        "ready_count": 0,
                        "total_count": len(package_steps),
                        "project_id": requested_project_id,
                    },
                )
        package_project_ids = {
            source_project_id
            for source_project_id, _task_id in ready_article_refs
            if source_project_id
        }
        if not package_project_ids:
            raise HTTPException(
                status_code=409,
                detail="成功文章没有有效的项目范围。",
            )
        for source_project_id in sorted(package_project_ids):
            _context(request).access.require(
                actor,
                source_project_id,
                "article.deliver",
            )

        factory = getattr(
            request.app.state,
            "server_project_task_store_factory",
            None,
        )
        create_runtime = getattr(factory, "create", None)
        if not callable(create_runtime):
            raise HTTPException(
                status_code=503,
                detail="Server project task storage is not available.",
            )
        runtimes: dict[str, Any] = {}
        for source_project_id in sorted(package_project_ids):
            try:
                runtimes[source_project_id] = create_runtime(
                    AuthorizedProjectRequest(
                        actor=actor,
                        project_id=source_project_id,
                        permission="article.deliver",
                    )
                )
            except Exception as exc:
                raise HTTPException(
                    status_code=503,
                    detail="Server project task storage is not available.",
                ) from exc

        tasks = []
        task_project_ids: dict[str, str] = {}
        seen_task_ids: set[str] = set()
        for source_project_id, task_id in ready_article_refs:
            if not task_id or task_id in seen_task_ids:
                raise HTTPException(
                    status_code=409,
                    detail="批量交付包中的文章任务标识无效或重复。",
                )
            seen_task_ids.add(task_id)
            runtime = runtimes.get(source_project_id)
            if runtime is None:
                raise HTTPException(
                    status_code=409,
                    detail="批量交付包中的文章项目范围无效。",
                )
            try:
                task = runtime.store.get(task_id)
            except KeyError as exc:
                raise HTTPException(
                    status_code=409,
                    detail="批量交付包引用的文章任务已不存在。",
                ) from exc
            key = (source_project_id, task_id)
            if key in partial_article_refs:
                artifacts = article_artifacts[key]
                if (
                    str(getattr(task, "docx_asset_id", "") or "").strip()
                    != artifacts["docx"]
                    or str(getattr(task, "tdk_asset_id", "") or "").strip()
                    != artifacts["tdk"]
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="文章 Word 或 TDK 已发生变化，请刷新计划后重试。",
                    )
            else:
                output_asset_id = ready_package_by_task[key]
                if task.delivery_package_asset_id.strip() != output_asset_id:
                    raise HTTPException(
                        status_code=409,
                        detail="文章交付包已发生变化，请刷新计划后重试。",
                    )
            tasks.append(task)
            task_project_ids[task_id] = source_project_id

        object_service = getattr(
            request.app.state,
            "server_project_object_service",
            None,
        )
        if not callable(getattr(object_service, "read_for_article_delivery", None)):
            raise HTTPException(
                status_code=503,
                detail="Server project object storage is not available.",
            )
        anchor_project_id = sorted(package_project_ids)[0]
        batch_packer = ServerBatchDeliveryPackage(objects=object_service)
        if partial_article_refs:
            asset = batch_packer.package_completed_articles(
                actor=actor,
                project_id=anchor_project_id,
                tasks=tasks,
                task_project_ids=task_project_ids,
            )
        else:
            asset = batch_packer.package(
                actor=actor,
                project_id=anchor_project_id,
                tasks=tasks,
                task_project_ids=task_project_ids,
            )
        filename = (
            f"{official_website_folder_name(anchor_project_id)}-batch-delivery.zip"
            if requested_project_id is not None
            else "workflow-batch-delivery.zip"
        )
        url = object_service.create_delivery_zip_download_url(
            actor=actor,
            project_id=anchor_project_id,
            asset_id=asset.asset_id,
            content_hash=asset.content_hash,
            filename=filename,
            expires_seconds=expires_seconds,
        )
        return WorkflowAssistantBatchDownloadResponse(
            asset_id=asset.asset_id,
            url=url,
            expires_seconds=expires_seconds,
            filename=filename,
            file_count=len(tasks),
        )
    except HTTPException:
        raise
    except ProjectAccessDenied as exc:
        raise HTTPException(status_code=403, detail="project access denied") from exc
    except ServerDeliveryPackageError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except KnowledgeObjectNotFound as exc:
        raise HTTPException(
            status_code=409,
            detail="批量交付包中的文件已失效，请重新生成对应文章的交付包。",
        ) from exc
    except ObjectStoreError as exc:
        raise HTTPException(
            status_code=503,
            detail="批量交付下载暂时不可用。",
        ) from exc
    except Exception as exc:
        raise _error(exc) from exc


@router.post(
    "/plans/{plan_id}/gap-fill",
    response_model=GapFillResponse,
)
def gap_fill_plan(
    plan_id: str,
    payload: GapFillRequest,
    request: Request,
    actor: ActorIdentity = Depends(require_server_actor),
) -> GapFillResponse:
    """Approve bounded research candidates and release the waiting step.

    Candidate URLs are never accepted from this request.  The current graph
    checkpoint is the only authority for candidate IDs; the existing Server
    research registry resolves those IDs to private URLs after authorization.
    """

    _gap_fill_feature_enabled(request)
    try:
        repository = _repository(request)
        plan = repository.get_plan(actor=actor, plan_id=plan_id)
        _reauthorize_plan_read(request, actor=actor, plan=plan)
        step = next(
            (item for item in plan.steps if item.step_id == payload.step_id),
            None,
        )
        if step is None:
            raise GapFillNotFound("plan step not found")
        if step.action_kind != "start_research":
            raise GapFillConflict("gap-fill target step is not a research step")
        persisted_thread_id = str(
            step.input_summary.get("research_thread_id")
            or step.output_summary.get("research_thread_id")
            or ""
        ).strip()
        if not persisted_thread_id or persisted_thread_id != payload.research_thread_id:
            raise GapFillConflict("research thread does not match the waiting step")
        request_identity = _gap_fill_request_identity(
            plan_id=plan.plan_id,
            step_id=step.step_id,
            thread_id=persisted_thread_id,
            request_id=payload.request_id,
            approved_candidate_ids=payload.approved_candidate_ids,
        )
        service = _gap_fill_service(request)
        snapshot = service.snapshot(
            actor=actor,
            project_id=step.project_id,
            thread_id=persisted_thread_id,
        )
        if snapshot.research_thread_id != persisted_thread_id:
            raise GapFillConflict("research checkpoint identity is invalid")
        same_request = (
            step.input_summary.get("gap_fill_request_id") == request_identity
        )
        if same_request:
            # Exact retries are answered from the durable assistant binding.
            # In particular, do not revalidate candidate IDs after the graph
            # has advanced and cleared its review-candidate checkpoint.
            return GapFillResponse(
                plan=_plan_response(plan),
                step_id=step.step_id,
                research_thread_id=persisted_thread_id,
                queue_job_id=str(step.background_job_id or ""),
                queue_job_status=(
                    "queued"
                    if step.status == "waiting_job"
                    else str(step.output_summary.get("status") or step.status)
                ),
                snapshot=snapshot,
            )
        if snapshot.status != "waiting_for_review":
            raise GapFillConflict("research run is not waiting for candidate review")
        visible_candidate_ids = {
            candidate.candidate_id for candidate in snapshot.review_candidates
        }
        unknown = set(payload.approved_candidate_ids) - visible_candidate_ids
        if unknown:
            raise GapFillConflict(
                "approved_candidate_ids contain unknown or non-official candidates"
            )
        queued = service.enqueue_resume(
            actor=actor,
            project_id=step.project_id,
            thread_id=persisted_thread_id,
            request_id=request_identity,
            approved_candidate_ids=payload.approved_candidate_ids,
        )
        current = repository.release_research_gap_fill(
            actor=actor,
            plan_id=plan.plan_id,
            expected_revision=payload.revision,
            step_id=step.step_id,
            research_thread_id=persisted_thread_id,
            approved_candidate_ids=payload.approved_candidate_ids,
            request_id=request_identity,
            background_job_id=str(queued["job_id"]),
        )
        runner = getattr(request.app.state, "workflow_assistant_runner", None)
        wake = getattr(runner, "wake", None)
        if callable(wake):
            wake()
        return GapFillResponse(
            plan=_plan_response(current),
            step_id=step.step_id,
            research_thread_id=persisted_thread_id,
            queue_job_id=str(queued["job_id"]),
            queue_job_status=str(queued.get("status") or "queued"),
            snapshot=snapshot,
        )
    except HTTPException:
        raise
    except GapFillError as exc:
        raise _error(exc) from exc
    except Exception as exc:
        raise _error(exc) from exc


def _change_plan_status(
    plan_id: str,
    payload: PlanCommandRequest,
    request: Request,
    actor: ActorIdentity,
    status: str,
) -> WorkflowPlanResponse:
    _feature_enabled(request)
    try:
        plan = _repository(request).get_plan(actor=actor, plan_id=plan_id)
        jobs_to_cancel = plan if status == "cancelled" else None
        # Confirmation, resume and cancellation are all state-changing
        # commands. Re-check the current project assignment at the command
        # boundary instead of trusting the snapshot captured by the planner.
        _authorized_plan_scope(
            request,
            actor=actor,
            project_ids=plan.project_ids,
            step_project_ids=tuple(step.project_id for step in plan.steps),
        )
        if status == "queued":
            if not payload.plan_hash:
                raise HTTPException(status_code=422, detail="plan_hash is required for confirmation")
            plan = _repository(request).confirm_plan(
                actor=actor,
                plan_id=plan_id,
                expected_revision=payload.revision,
                expected_plan_hash=payload.plan_hash,
            )
        elif payload.project_ids is not None and status in {"paused", "running"}:
            plan = _repository(request).set_projects_paused(
                actor=actor,
                plan_id=plan_id,
                expected_revision=payload.revision,
                project_ids=payload.project_ids,
                paused=status == "paused",
            )
        else:
            plan = _repository(request).set_plan_status(
                actor=actor,
                plan_id=plan_id,
                expected_revision=payload.revision,
                new_status=status,  # type: ignore[arg-type]
            )
            if jobs_to_cancel is not None:
                _request_plan_job_cancellation(
                    request,
                    actor=actor,
                    plan=jobs_to_cancel,
                )
        if status in {"queued", "running"}:
            runner = getattr(
                request.app.state,
                "workflow_assistant_runner",
                None,
            )
            wake = getattr(runner, "wake", None)
            if callable(wake):
                wake()
        return _plan_response(plan)
    except HTTPException:
        raise
    except Exception as exc:
        raise _error(exc) from exc


@router.post("/plans/{plan_id}/confirm", response_model=WorkflowPlanResponse)
def confirm_plan(plan_id: str, payload: PlanCommandRequest, request: Request, actor: ActorIdentity = Depends(require_server_actor)) -> WorkflowPlanResponse:
    return _change_plan_status(plan_id, payload, request, actor, "queued")


@router.post("/plans/{plan_id}/pause", response_model=WorkflowPlanResponse)
def pause_plan(plan_id: str, payload: PlanCommandRequest, request: Request, actor: ActorIdentity = Depends(require_server_actor)) -> WorkflowPlanResponse:
    return _change_plan_status(plan_id, payload, request, actor, "paused")


@router.post("/plans/{plan_id}/resume", response_model=WorkflowPlanResponse)
def resume_plan(plan_id: str, payload: PlanCommandRequest, request: Request, actor: ActorIdentity = Depends(require_server_actor)) -> WorkflowPlanResponse:
    return _change_plan_status(plan_id, payload, request, actor, "running")


@router.post("/plans/{plan_id}/retry", response_model=WorkflowPlanResponse)
def retry_plan(plan_id: str, payload: PlanCommandRequest, request: Request, actor: ActorIdentity = Depends(require_server_actor)) -> WorkflowPlanResponse:
    _feature_enabled(request)
    if not payload.plan_hash:
        raise HTTPException(status_code=422, detail="plan_hash is required for retry")
    try:
        repository = _repository(request)
        plan = repository.get_plan(actor=actor, plan_id=plan_id)
        _authorized_plan_scope(
            request,
            actor=actor,
            project_ids=plan.project_ids,
            step_project_ids=tuple(step.project_id for step in plan.steps),
        )
        plan = repository.retry_failed_steps(
            actor=actor,
            plan_id=plan_id,
            expected_revision=payload.revision,
            expected_plan_hash=payload.plan_hash,
        )
        runner = getattr(request.app.state, "workflow_assistant_runner", None)
        wake = getattr(runner, "wake", None)
        if callable(wake):
            wake()
        return _plan_response(plan)
    except HTTPException:
        raise
    except Exception as exc:
        raise _error(exc) from exc


@router.post("/plans/{plan_id}/cancel", response_model=WorkflowPlanResponse)
def cancel_plan(plan_id: str, payload: PlanCommandRequest, request: Request, actor: ActorIdentity = Depends(require_server_actor)) -> WorkflowPlanResponse:
    return _change_plan_status(plan_id, payload, request, actor, "cancelled")


@router.post("/plans/{plan_id}/revise", response_model=WorkflowPlanResponse)
def revise_plan(plan_id: str, payload: PlanRevisionRequest, request: Request, actor: ActorIdentity = Depends(require_server_actor)) -> WorkflowPlanResponse:
    _feature_enabled(request)
    try:
        repository = _repository(request)
        current_plan = repository.get_plan(actor=actor, plan_id=plan_id)
        usage = None
        planner_model_identity = None
        if payload.plan is None:
            revision_context = _context(request).resolve(
                actor=actor,
                project_ids=list(current_plan.project_ids),
            )
            planner = _planner(request)
            generated_plan = planner.plan(
                actor=actor,
                request=payload.natural_language_request,
                context=revision_context,
                selected_project_ids=current_plan.project_ids,
                allowed_action_kinds=ASSISTANT_ALLOWED_ACTION_KINDS,
            )
            planner_model_identity = planner.consume_model_identity()
            usage = estimate_planner_usage(
                payload.natural_language_request,
                revision_context,
                selected_project_ids=current_plan.project_ids,
                plan=generated_plan,
                allowed_action_kinds=ASSISTANT_ALLOWED_ACTION_KINDS,
            )
            bound_plan = bind_plan_context(
                generated_plan,
                context=revision_context,
            )
            bound_plan = _merge_natural_language_revision(
                current_plan,
                bound_plan,
            )
        else:
            revision_context = _context(request).resolve(
                actor=actor,
                project_ids=payload.plan.project_ids,
            )
            revision_draft = _normalize_explicit_revision_execution(
                current_plan,
                payload.plan,
            )
            bound_plan = bind_plan_context(
                revision_draft,
                context=revision_context,
            )
        if any(
            step.action_kind not in ASSISTANT_ALLOWED_ACTION_KINDS
            for step in bound_plan.steps
        ):
            raise AssistantPolicyError(
                "文章计划已移至批量写文章页面，助手不能修改文章执行计划"
            )
        bound_plan = _apply_execution_limits(request, bound_plan)
        bound_plan = bound_plan.model_copy(
            update={"natural_language_request": sanitize_message(payload.natural_language_request)}
        )
        authorization_snapshot = _authorized_plan_scope(
            request,
            actor=actor,
            project_ids=bound_plan.project_ids,
            step_project_ids=tuple(step.project_id for step in bound_plan.steps),
        )
        plan = repository.revise_plan(
            actor=actor,
            plan_id=plan_id,
            expected_revision=payload.revision,
            expected_plan_hash=payload.plan_hash,
            plan=bound_plan,
            authorization_snapshot=authorization_snapshot,
        )
        if usage is not None:
            _record_planner_usage(
                request,
                actor=actor,
                plan=plan,
                request_id=f"revision:{plan.plan_id}:{plan.revision}",
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                model_identity=planner_model_identity,
            )
        return _plan_response(plan)
    except Exception as exc:
        raise _error(exc) from exc


@router.get("/plans/{plan_id}/events/stream")
async def stream_plan_events(
    plan_id: str,
    request: Request,
    after_sequence: int = Query(default=0, ge=0),
    actor: ActorIdentity = Depends(require_server_actor),
) -> StreamingResponse:
    _feature_enabled(request)

    repository = _repository(request)

    def load_plan() -> WorkflowPlan:
        plan = repository.get_plan(actor=actor, plan_id=plan_id)
        _reauthorize_plan_read(request, actor=actor, plan=plan)
        return plan

    # Validate the plan before opening a long-lived response. This keeps a
    # missing or unauthorized plan as a normal HTTP error instead of making
    # the browser retry an SSE stream that can never succeed.
    try:
        await asyncio.to_thread(load_plan)
    except Exception as exc:
        raise _error(exc) from exc

    async def iterator() -> AsyncIterator[str]:
        cursor = after_sequence
        last_heartbeat = time.monotonic()
        # Emit an initial comment so reverse proxies flush the stream even
        # when the plan has not produced a new event yet.
        yield ": connected\n\n"
        while True:
            if await request.is_disconnected():
                return
            try:
                events = await asyncio.to_thread(
                    repository.list_events,
                    actor=actor,
                    plan_id=plan_id,
                    after_sequence=cursor,
                )
                plan = await asyncio.to_thread(load_plan)
            except Exception as exc:
                error = _error(exc)
                payload = json.dumps({"detail": error.detail}, ensure_ascii=False)
                yield f"event: error\ndata: {payload}\n\n"
                return
            for event in events:
                cursor = max(cursor, event.sequence)
                payload = json.dumps(
                    {
                        "sequence": event.sequence,
                        "event_kind": event.event_kind,
                        "public_payload": event.public_payload,
                        "created_at": _iso(event.created_at),
                    },
                    ensure_ascii=False,
                )
                # Use one stable SSE event name so browser clients can
                # subscribe without exposing internal event kinds as routing
                # metadata.
                yield f"id: {event.sequence}\nevent: workflow-assistant\ndata: {payload}\n\n"
            if plan.status in {"completed", "failed", "cancelled"}:
                yield (
                    "event: done\ndata: "
                    + json.dumps({"status": plan.status}, ensure_ascii=False)
                    + "\n\n"
                )
                return
            if time.monotonic() - last_heartbeat >= 15:
                yield ": keep-alive\n\n"
                last_heartbeat = time.monotonic()
            await asyncio.sleep(1)

    return StreamingResponse(
        iterator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/attention-count", response_model=AttentionCountResponse)
def attention_count(
    request: Request,
    actor: ActorIdentity = Depends(require_server_actor),
) -> AttentionCountResponse:
    _feature_enabled(request)
    try:
        accessible_projects = _context(request).accessible_projects(actor)
        return AttentionCountResponse(
            count=_repository(request).attention_count(
                actor=actor,
                accessible_project_ids=tuple(
                    project.project_id for project in accessible_projects
                ),
            )
        )
    except Exception as exc:
        raise _error(exc) from exc


@router.get("/attention", response_model=AssistantAttentionListResponse)
def attention(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    actor: ActorIdentity = Depends(require_server_actor),
) -> AssistantAttentionListResponse:
    """Return the creator's visible attention inbox, not just its badge."""

    _feature_enabled(request)
    try:
        accessible_projects = _context(request).accessible_projects(actor)
        plans = _repository(request).list_attention_plans(
            actor=actor,
            accessible_project_ids=tuple(
                project.project_id for project in accessible_projects
            ),
            limit=limit,
        )
        visible: list[WorkflowPlanSummary] = []
        for plan in plans:
            try:
                _reauthorize_plan_summary_read(request, actor=actor, plan=plan)
            except AssistantContextError as exc:
                if str(exc) in {
                    "project access denied",
                    "project scope contains an inaccessible project",
                }:
                    continue
                raise
            visible.append(_plan_summary(plan))
        count = _repository(request).attention_count(
            actor=actor,
            accessible_project_ids=tuple(
                project.project_id for project in accessible_projects
            ),
        )
        return AssistantAttentionListResponse(plans=visible, count=count)
    except Exception as exc:
        raise _error(exc) from exc


@router.get("/batch-plans", response_model=BatchPlanHistoryResponse)
def batch_plan_history(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    actor: ActorIdentity = Depends(require_server_actor),
) -> BatchPlanHistoryResponse:
    """Return the creator's durable structured batch-writing history."""

    _feature_enabled(request)
    try:
        accessible_projects = _context(request).accessible_projects(actor)
        accessible_project_ids = tuple(
            project.project_id for project in accessible_projects
        )
        plans = _repository(request).list_batch_plan_summaries(
            actor=actor,
            accessible_project_ids=accessible_project_ids,
            limit=limit,
        )
        visible: list[WorkflowPlanSummary] = []
        for plan in plans:
            try:
                _reauthorize_plan_summary_read(request, actor=actor, plan=plan)
            except AssistantContextError as exc:
                if str(exc) in {
                    "project access denied",
                    "project scope contains an inaccessible project",
                }:
                    continue
                raise
            visible.append(_plan_summary(plan))
        return BatchPlanHistoryResponse(plans=visible, count=len(visible))
    except Exception as exc:
        raise _error(exc) from exc


__all__ = ["router"]
