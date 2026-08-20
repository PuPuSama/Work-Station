from __future__ import annotations

from typing import Mapping, Protocol

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from models import PromptKind
from services.access_control import ActorIdentity
from server_project_http import require_server_actor

from .attachment_http import (
    AttachmentResponse,
    _attachment_response,
    _attachments_enabled,
    _authorize_proposed_project,
    _error as _attachment_error,
    _require_conversation,
    _service as _attachment_service,
)
from .attachment_jobs import AttachmentJob, AttachmentJobConflict, AttachmentJobError
from .attachments import AssistantAttachment, AttachmentConflict
from .import_proposals import (
    ImportProposal,
    ImportProposalConflict,
    ImportProposalError,
    ImportProposalNotFound,
    ImportProposalValidationError,
    ImportTargetKind,
)


router = APIRouter(prefix="/api/workflow-assistant", tags=["workflow-assistant"])


class AttachmentReviewWorkflow(Protocol):
    """Fail-closed application seam for durable attachment review operations.

    Preview jobs do not point at a proposal: the worker builds the normalized
    diff first and creates the review proposal only after that succeeds.
    Confirmation is only a proposal CAS in M2.2; execution is intentionally
    unavailable until the M2.3 import adapters are wired.
    """

    def enqueue_classification(
        self,
        *,
        actor: ActorIdentity,
        attachment: AssistantAttachment,
        idempotency_key: str,
        expected_attachment_revision: int,
        prompt_kind: PromptKind | None = None,
    ) -> AttachmentJob: ...

    def enqueue_proposal_preview(
        self,
        *,
        actor: ActorIdentity,
        attachment: AssistantAttachment,
        target_kind: ImportTargetKind,
        target_project_id: str | None,
        plan_id: str | None,
        idempotency_key: str,
        expected_attachment_revision: int,
    ) -> AttachmentJob: ...

    def get_proposal(
        self, *, actor: ActorIdentity, proposal_id: str
    ) -> ImportProposal: ...

    def get_job(self, *, actor: ActorIdentity, job_id: str) -> AttachmentJob: ...

    def revise_proposal(
        self,
        *,
        actor: ActorIdentity,
        proposal_id: str,
        expected_revision: int,
        expected_attachment_revision: int,
        target_kind: ImportTargetKind,
        target_project_id: str | None,
        normalized_diff: Mapping[str, object],
    ) -> ImportProposal: ...

    def confirm_proposal(
        self,
        *,
        actor: ActorIdentity,
        proposal_id: str,
        target_project_id: str,
        expected_revision: int,
        expected_attachment_revision: int,
    ) -> ImportProposal: ...

    def cancel_proposal(
        self,
        *,
        actor: ActorIdentity,
        proposal_id: str,
        expected_revision: int,
    ) -> ImportProposal: ...


class ClassifyAttachmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: str = Field(min_length=1, max_length=128)
    expected_attachment_revision: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=512)


class CreateImportProposalRequest(ClassifyAttachmentRequest):
    target_kind: ImportTargetKind
    target_project_id: str | None = Field(default=None, min_length=1, max_length=255)
    prompt_kind: PromptKind | None = None
    plan_id: str | None = Field(default=None, min_length=1, max_length=255)


class ReviseImportProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: str = Field(min_length=1, max_length=128)
    expected_revision: int = Field(ge=0)
    expected_attachment_revision: int = Field(ge=0)
    target_kind: ImportTargetKind
    target_project_id: str | None = Field(default=None, min_length=1, max_length=255)
    normalized_diff: dict[str, object]


class ConfirmImportProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: str = Field(min_length=1, max_length=128)
    target_project_id: str = Field(min_length=1, max_length=255)
    expected_revision: int = Field(ge=0)
    expected_attachment_revision: int = Field(ge=0)


class CancelImportProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: str = Field(min_length=1, max_length=128)
    expected_revision: int = Field(ge=0)


class AttachmentJobResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    operation: str
    status: str
    attachment_id: str
    proposal_id: str | None
    expected_attachment_revision: int
    expected_proposal_revision: int | None
    result_payload: dict[str, object]
    result_attachment_revision: int | None
    result_proposal_revision: int | None
    standardized_error_code: str | None


class ImportProposalResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: str
    attachment_id: str
    target_project_id: str | None
    plan_id: str | None
    target_kind: str
    normalized_diff: dict[str, object]
    revision: int
    status: str
    resulting_entity_refs: list[dict[str, object]]
    standardized_error_code: str | None
    created_at: str
    updated_at: str


class AttachmentReviewResponse(BaseModel):
    """Explicitly separates each M2 lifecycle gate in the public contract."""

    model_config = ConfigDict(extra="forbid")

    attachment: AttachmentResponse
    proposal: ImportProposalResponse | None = None
    job: AttachmentJobResponse | None = None
    attachment_stage: str
    proposal_stage: str
    import_stage: str
    publication_stage: str = "not_published"


def _workflow(request: Request) -> AttachmentReviewWorkflow:
    service = getattr(
        request.app.state, "workflow_assistant_attachment_review_workflow", None
    )
    required = (
        "enqueue_classification",
        "enqueue_proposal_preview",
        "get_job",
        "get_proposal",
        "revise_proposal",
        "confirm_proposal",
        "cancel_proposal",
    )
    if not all(callable(getattr(service, name, None)) for name in required):
        raise HTTPException(
            status_code=503,
            detail="Workflow Assistant attachment review is not available.",
        )
    return service


def _job_response(job: AttachmentJob) -> AttachmentJobResponse:
    return AttachmentJobResponse(
        job_id=job.job_id,
        operation=job.operation,
        status=job.status,
        attachment_id=job.attachment_id,
        proposal_id=job.proposal_id,
        expected_attachment_revision=job.expected_attachment_revision,
        expected_proposal_revision=job.expected_proposal_revision,
        result_payload=dict(job.result_payload),
        result_attachment_revision=job.result_attachment_revision,
        result_proposal_revision=job.result_proposal_revision,
        standardized_error_code=job.standardized_error_code,
    )


def _proposal_response(proposal: ImportProposal) -> ImportProposalResponse:
    return ImportProposalResponse(
        proposal_id=proposal.proposal_id,
        attachment_id=proposal.attachment_id,
        target_project_id=proposal.target_project_id,
        plan_id=proposal.plan_id,
        target_kind=proposal.target_kind,
        normalized_diff=dict(proposal.normalized_diff),
        revision=proposal.revision,
        status=proposal.status,
        resulting_entity_refs=[dict(item) for item in proposal.resulting_entity_refs],
        standardized_error_code=proposal.standardized_error_code,
        created_at=proposal.created_at.isoformat(),
        updated_at=proposal.updated_at.isoformat(),
    )


def _attachment_stage(attachment: AssistantAttachment) -> str:
    if attachment.status == "imported":
        return "imported"
    if attachment.classification is not None or attachment.status in {
        "proposal_ready",
        "needs_user_choice",
        "unsupported",
    }:
        return "classified"
    return "uploaded"


def _response(
    attachment: AssistantAttachment,
    *,
    proposal: ImportProposal | None = None,
    job: AttachmentJob | None = None,
) -> AttachmentReviewResponse:
    proposal_stage = "none"
    import_stage = "not_imported"
    if proposal is not None:
        if proposal.status in {"confirmed", "running"}:
            proposal_stage = "confirmed"
        elif proposal.status in {"completed", "waiting_publication"}:
            proposal_stage = "imported"
            import_stage = "imported"
        else:
            proposal_stage = "proposal"
    elif job is not None and job.operation == "preview_import_proposal":
        proposal_stage = "preview_queued"
    if attachment.status == "imported":
        import_stage = "imported"
    return AttachmentReviewResponse(
        attachment=_attachment_response(attachment),
        proposal=_proposal_response(proposal) if proposal else None,
        job=_job_response(job) if job else None,
        attachment_stage=_attachment_stage(attachment),
        proposal_stage=proposal_stage,
        import_stage=import_stage,
        # Publication is a separate human-gated action and is never implied by
        # attachment import or by the presence of resulting entity references.
        publication_stage="not_published",
    )


def _load_attachment(
    request: Request,
    *,
    actor: ActorIdentity,
    conversation_id: str,
    attachment_id: str,
) -> AssistantAttachment:
    _require_conversation(request, actor=actor, conversation_id=conversation_id)
    return _attachment_service(request).get(
        actor=actor,
        conversation_id=conversation_id,
        attachment_id=attachment_id,
    )


def _require_revision(attachment: AssistantAttachment, expected_revision: int) -> None:
    if attachment.revision != expected_revision:
        raise AttachmentConflict(
            f"attachment revision changed (current revision {attachment.revision})"
        )


def _authorize_target(
    request: Request,
    *,
    actor: ActorIdentity,
    project_id: str | None,
) -> str | None:
    return _authorize_proposed_project(
        request,
        actor=actor,
        proposed_project_id=project_id,
    )


def _review_error(exc: Exception) -> HTTPException:
    if isinstance(exc, (AttachmentConflict, HTTPException)):
        return _attachment_error(exc)
    if isinstance(exc, ImportProposalNotFound):
        return HTTPException(status_code=404, detail="Import proposal not found.")
    if isinstance(exc, ImportProposalConflict):
        detail: dict[str, object] = {
            "error_code": exc.code,
            "message": str(exc),
        }
        if exc.current_revision is not None:
            detail["current_revision"] = exc.current_revision
        return HTTPException(status_code=409, detail=detail)
    if isinstance(exc, ImportProposalValidationError):
        return HTTPException(
            status_code=422,
            detail={"error_code": exc.code, "message": str(exc)},
        )
    if isinstance(exc, AttachmentJobConflict):
        return HTTPException(
            status_code=409,
            detail={"error_code": exc.code, "message": str(exc)},
        )
    if isinstance(exc, (ImportProposalError, AttachmentJobError, ValueError)):
        return HTTPException(status_code=503, detail="Attachment review is unavailable.")
    return _attachment_error(exc)


@router.post(
    "/attachments/{attachment_id}/classify",
    response_model=AttachmentReviewResponse,
    status_code=202,
)
def classify_attachment(
    attachment_id: str,
    payload: ClassifyAttachmentRequest,
    request: Request,
    actor: ActorIdentity = Depends(require_server_actor),
) -> AttachmentReviewResponse:
    _attachments_enabled(request)
    try:
        attachment = _load_attachment(
            request,
            actor=actor,
            conversation_id=payload.conversation_id,
            attachment_id=attachment_id,
        )
        _require_revision(attachment, payload.expected_attachment_revision)
        _authorize_target(request, actor=actor, project_id=attachment.proposed_project_id)
        job = _workflow(request).enqueue_classification(
            actor=actor,
            attachment=attachment,
            idempotency_key=payload.idempotency_key,
            expected_attachment_revision=payload.expected_attachment_revision,
        )
        return _response(attachment, job=job)
    except Exception as exc:
        raise _review_error(exc) from exc


@router.post(
    "/attachments/{attachment_id}/proposals",
    response_model=AttachmentReviewResponse,
    status_code=202,
)
def create_import_proposal(
    attachment_id: str,
    payload: CreateImportProposalRequest,
    request: Request,
    actor: ActorIdentity = Depends(require_server_actor),
) -> AttachmentReviewResponse:
    _attachments_enabled(request)
    try:
        attachment = _load_attachment(
            request,
            actor=actor,
            conversation_id=payload.conversation_id,
            attachment_id=attachment_id,
        )
        preselected_project_id = _authorize_target(
            request, actor=actor, project_id=attachment.proposed_project_id
        )
        project_id = (
            preselected_project_id
            if payload.target_project_id is None
            or payload.target_project_id == attachment.proposed_project_id
            else _authorize_target(
                request, actor=actor, project_id=payload.target_project_id
            )
        )
        if project_id is None:
            raise ImportProposalValidationError(
                "target_project_id is required for proposal preview",
                code="target_project_required",
            )
        job = _workflow(request).enqueue_proposal_preview(
            actor=actor,
            attachment=attachment,
            target_kind=payload.target_kind,
            target_project_id=project_id,
            plan_id=payload.plan_id,
            idempotency_key=payload.idempotency_key,
            expected_attachment_revision=payload.expected_attachment_revision,
            prompt_kind=payload.prompt_kind,
        )
        return _response(attachment, job=job)
    except Exception as exc:
        raise _review_error(exc) from exc


@router.get(
    "/attachment-jobs/{job_id}",
    response_model=AttachmentReviewResponse,
)
def get_attachment_job(
    job_id: str,
    request: Request,
    conversation_id: str = Query(..., min_length=1, max_length=128),
    actor: ActorIdentity = Depends(require_server_actor),
) -> AttachmentReviewResponse:
    _attachments_enabled(request)
    try:
        job = _workflow(request).get_job(actor=actor, job_id=job_id)
        attachment = _load_attachment(
            request,
            actor=actor,
            conversation_id=conversation_id,
            attachment_id=job.attachment_id,
        )
        _authorize_target(request, actor=actor, project_id=job.project_id)
        proposal = None
        result_proposal_id = job.result_payload.get("proposal_id")
        if job.operation == "preview_import_proposal" and result_proposal_id:
            proposal = _workflow(request).get_proposal(
                actor=actor,
                proposal_id=str(result_proposal_id),
            )
        return _response(attachment, proposal=proposal, job=job)
    except Exception as exc:
        raise _review_error(exc) from exc


@router.get(
    "/import-proposals/{proposal_id}", response_model=AttachmentReviewResponse
)
def get_import_proposal(
    proposal_id: str,
    request: Request,
    conversation_id: str = Query(..., min_length=1, max_length=128),
    actor: ActorIdentity = Depends(require_server_actor),
) -> AttachmentReviewResponse:
    _attachments_enabled(request)
    try:
        proposal = _workflow(request).get_proposal(actor=actor, proposal_id=proposal_id)
        attachment = _load_attachment(
            request,
            actor=actor,
            conversation_id=conversation_id,
            attachment_id=proposal.attachment_id,
        )
        _authorize_target(request, actor=actor, project_id=proposal.target_project_id)
        return _response(attachment, proposal=proposal)
    except Exception as exc:
        raise _review_error(exc) from exc


@router.post(
    "/import-proposals/{proposal_id}/revise",
    response_model=AttachmentReviewResponse,
)
def revise_import_proposal(
    proposal_id: str,
    payload: ReviseImportProposalRequest,
    request: Request,
    actor: ActorIdentity = Depends(require_server_actor),
) -> AttachmentReviewResponse:
    _attachments_enabled(request)
    try:
        current = _workflow(request).get_proposal(actor=actor, proposal_id=proposal_id)
        attachment = _load_attachment(
            request,
            actor=actor,
            conversation_id=payload.conversation_id,
            attachment_id=current.attachment_id,
        )
        _require_revision(attachment, payload.expected_attachment_revision)
        current_project_id = _authorize_target(
            request, actor=actor, project_id=current.target_project_id
        )
        project_id = (
            current_project_id
            if payload.target_project_id == current.target_project_id
            else _authorize_target(
                request, actor=actor, project_id=payload.target_project_id
            )
        )
        proposal = _workflow(request).revise_proposal(
            actor=actor,
            proposal_id=proposal_id,
            expected_revision=payload.expected_revision,
            expected_attachment_revision=payload.expected_attachment_revision,
            target_kind=payload.target_kind,
            target_project_id=project_id,
            normalized_diff=payload.normalized_diff,
        )
        return _response(attachment, proposal=proposal)
    except Exception as exc:
        raise _review_error(exc) from exc


@router.post(
    "/import-proposals/{proposal_id}/confirm",
    response_model=AttachmentReviewResponse,
)
def confirm_import_proposal(
    proposal_id: str,
    payload: ConfirmImportProposalRequest,
    request: Request,
    actor: ActorIdentity = Depends(require_server_actor),
) -> AttachmentReviewResponse:
    _attachments_enabled(request)
    try:
        current = _workflow(request).get_proposal(actor=actor, proposal_id=proposal_id)
        attachment = _load_attachment(
            request,
            actor=actor,
            conversation_id=payload.conversation_id,
            attachment_id=current.attachment_id,
        )
        _require_revision(attachment, payload.expected_attachment_revision)
        current_project_id = _authorize_target(
            request, actor=actor, project_id=current.target_project_id
        )
        project_id = (
            current_project_id
            if payload.target_project_id == current.target_project_id
            else _authorize_target(
                request, actor=actor, project_id=payload.target_project_id
            )
        )
        assert project_id is not None
        proposal = _workflow(request).confirm_proposal(
            actor=actor,
            proposal_id=proposal_id,
            target_project_id=project_id,
            expected_revision=payload.expected_revision,
            expected_attachment_revision=payload.expected_attachment_revision,
        )
        return _response(attachment, proposal=proposal)
    except Exception as exc:
        raise _review_error(exc) from exc


@router.post(
    "/import-proposals/{proposal_id}/cancel",
    response_model=AttachmentReviewResponse,
)
def cancel_import_proposal(
    proposal_id: str,
    payload: CancelImportProposalRequest,
    request: Request,
    actor: ActorIdentity = Depends(require_server_actor),
) -> AttachmentReviewResponse:
    _attachments_enabled(request)
    try:
        current = _workflow(request).get_proposal(actor=actor, proposal_id=proposal_id)
        attachment = _load_attachment(
            request,
            actor=actor,
            conversation_id=payload.conversation_id,
            attachment_id=current.attachment_id,
        )
        _authorize_target(request, actor=actor, project_id=current.target_project_id)
        proposal = _workflow(request).cancel_proposal(
            actor=actor,
            proposal_id=proposal_id,
            expected_revision=payload.expected_revision,
        )
        return _response(attachment, proposal=proposal)
    except Exception as exc:
        raise _review_error(exc) from exc


__all__ = ["AttachmentReviewWorkflow", "router"]
