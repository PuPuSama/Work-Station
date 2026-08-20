from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, ConfigDict

from services.access_control import ActorIdentity
from services.server_auth import SERVER_AUTH_COOKIE_NAME
from services.server_request_security import (
    ServerRequestForbidden,
    ServerRequestUnauthenticated,
)
from server_project_http import require_server_actor

from .attachments import (
    MAX_ATTACHMENT_BYTES,
    AttachmentConflict,
    AttachmentDownload,
    AttachmentNotFound,
    AttachmentService,
    AttachmentStorageError,
    AttachmentValidationError,
    AssistantAttachment,
)
from .repository import WorkflowAssistantNotFound


router = APIRouter(
    prefix="/api/workflow-assistant",
    tags=["workflow-assistant"],
)


class AttachmentResponse(BaseModel):
    """Public metadata for a temporary assistant attachment only.

    This model deliberately does not imply that the attachment was classified,
    imported, or published to the knowledge base.
    """

    model_config = ConfigDict(extra="forbid")

    attachment_id: str
    conversation_id: str
    proposed_project_id: str | None
    original_filename: str
    mime_type: str
    byte_size: int
    sha256: str
    classification: str | None
    classification_payload: dict[str, object]
    revision: int
    status: str
    expires_at: str
    created_at: str
    updated_at: str
    download_url: str | None = None
    download_url_expires_seconds: int | None = None


class AttachmentListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attachments: list[AttachmentResponse]


class AssistantConversationLookup(Protocol):
    def get_conversation(
        self,
        *,
        actor: ActorIdentity,
        conversation_id: str,
        include_messages: bool = False,
    ) -> object: ...


def _attachments_enabled(request: Request) -> None:
    config = getattr(request.app.state, "article_agent_config", None)
    if config is None or not bool(getattr(config, "workflow_assistant_enabled", False)):
        raise HTTPException(status_code=404, detail="Workflow Assistant is disabled.")
    if not bool(getattr(config, "workflow_assistant_attachments_enabled", False)):
        raise HTTPException(
            status_code=404,
            detail="Workflow Assistant attachments are disabled.",
        )


def _service(request: Request) -> AttachmentService:
    service = getattr(request.app.state, "workflow_assistant_attachment_service", None)
    required = ("upload", "get", "list", "create_download", "reject")
    if not all(callable(getattr(service, name, None)) for name in required):
        raise HTTPException(
            status_code=503,
            detail="Workflow Assistant attachment storage is not available.",
        )
    return service


def _conversation_repository(request: Request) -> AssistantConversationLookup:
    repository = getattr(request.app.state, "workflow_assistant_repository", None)
    if not callable(getattr(repository, "get_conversation", None)):
        raise HTTPException(
            status_code=503,
            detail="Workflow Assistant conversation storage is not available.",
        )
    return repository


def _require_conversation(
    request: Request,
    *,
    actor: ActorIdentity,
    conversation_id: str,
) -> None:
    """Fail closed before an attachment can be read or written.

    The PostgreSQL assistant repository scopes the lookup by organization and
    creator.  Its not-found exception is intentionally projected as a generic
    attachment/conversation 404 by this HTTP boundary.
    """

    try:
        _conversation_repository(request).get_conversation(
            actor=actor,
            conversation_id=conversation_id,
            include_messages=False,
        )
    except HTTPException:
        raise
    except WorkflowAssistantNotFound as exc:
        raise AttachmentNotFound("conversation not found") from exc


def _authorize_proposed_project(
    request: Request,
    *,
    actor: ActorIdentity,
    proposed_project_id: str | None,
) -> str | None:
    if proposed_project_id is None:
        return None
    security = getattr(request.app.state, "server_request_security", None)
    if not callable(getattr(security, "authorize_project", None)):
        raise HTTPException(status_code=503, detail="Server security is not available.")
    try:
        authorized = security.authorize_project(
            token=request.cookies.get(SERVER_AUTH_COOKIE_NAME, ""),
            project=proposed_project_id,
            permission="project.view",
        )
    except ServerRequestUnauthenticated as exc:
        raise HTTPException(status_code=401, detail="Authentication required.") from exc
    except ServerRequestForbidden as exc:
        raise HTTPException(status_code=403, detail="project access denied") from exc
    if authorized.actor != actor:
        # Keep direct handler invocation and a malformed middleware state from
        # borrowing another user's cookie-derived authorization.
        raise HTTPException(status_code=401, detail="Authentication required.")
    return str(authorized.project_id)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _attachment_response(
    attachment: AssistantAttachment,
    *,
    download: AttachmentDownload | None = None,
) -> AttachmentResponse:
    return AttachmentResponse(
        attachment_id=attachment.attachment_id,
        conversation_id=attachment.conversation_id,
        proposed_project_id=attachment.proposed_project_id,
        original_filename=attachment.original_filename,
        mime_type=attachment.mime_type,
        byte_size=attachment.byte_size,
        sha256=attachment.sha256,
        classification=attachment.classification,
        classification_payload=dict(attachment.classification_payload),
        revision=attachment.revision,
        status=attachment.status,
        expires_at=_iso(attachment.expires_at),
        created_at=_iso(attachment.created_at),
        updated_at=_iso(attachment.updated_at),
        download_url=download.download_url if download else None,
        download_url_expires_seconds=(download.url_expires_seconds if download else None),
    )


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, AttachmentNotFound):
        return HTTPException(status_code=404, detail="Attachment not found.")
    if isinstance(exc, AttachmentConflict):
        return HTTPException(
            status_code=409,
            detail={"error_code": "attachment_conflict", "message": str(exc)},
        )
    if isinstance(exc, AttachmentValidationError):
        return HTTPException(
            status_code=422,
            detail={"error_code": exc.code, "message": str(exc)},
        )
    if isinstance(exc, (AttachmentStorageError, ValueError)):
        return HTTPException(status_code=503, detail="Attachment service is unavailable.")
    return HTTPException(status_code=503, detail="Attachment service is unavailable.")


@router.post(
    "/conversations/{conversation_id}/attachments",
    response_model=AttachmentResponse,
    status_code=201,
)
async def upload_attachment(
    conversation_id: str,
    request: Request,
    file: UploadFile = File(...),
    idempotency_key: str = Form(...),
    proposed_project_id: str | None = Form(default=None),
    actor: ActorIdentity = Depends(require_server_actor),
) -> AttachmentResponse:
    """Store a validated temporary attachment; never classify or import it."""

    _attachments_enabled(request)
    try:
        _require_conversation(request, actor=actor, conversation_id=conversation_id)
        authorized_project_id = _authorize_proposed_project(
            request,
            actor=actor,
            proposed_project_id=proposed_project_id,
        )
        # Bound memory before the service performs its content and signature
        # validation; a malicious multipart body must not force an unbounded
        # read merely to receive the documented 25 MiB rejection.
        content = await file.read(MAX_ATTACHMENT_BYTES + 1)
        attachment = _service(request).upload(
            actor=actor,
            conversation_id=conversation_id,
            original_filename=file.filename or "",
            mime_type=file.content_type or "",
            content=content,
            idempotency_key=idempotency_key,
            proposed_project_id=authorized_project_id,
        )
        download = _service(request).create_download(
            actor=actor,
            conversation_id=conversation_id,
            attachment_id=attachment.attachment_id,
        )
        return _attachment_response(attachment, download=download)
    except Exception as exc:
        raise _error(exc) from exc
    finally:
        await file.close()


@router.get(
    "/conversations/{conversation_id}/attachments",
    response_model=AttachmentListResponse,
)
def list_attachments(
    conversation_id: str,
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    actor: ActorIdentity = Depends(require_server_actor),
) -> AttachmentListResponse:
    _attachments_enabled(request)
    try:
        _require_conversation(request, actor=actor, conversation_id=conversation_id)
        attachments = _service(request).list(
            actor=actor,
            conversation_id=conversation_id,
            limit=limit,
        )
        return AttachmentListResponse(
            attachments=[_attachment_response(item) for item in attachments]
        )
    except Exception as exc:
        raise _error(exc) from exc


@router.get("/attachments/{attachment_id}", response_model=AttachmentResponse)
def get_attachment(
    attachment_id: str,
    request: Request,
    conversation_id: str = Query(..., min_length=1, max_length=128),
    actor: ActorIdentity = Depends(require_server_actor),
) -> AttachmentResponse:
    _attachments_enabled(request)
    try:
        _require_conversation(request, actor=actor, conversation_id=conversation_id)
        attachment = _service(request).get(
            actor=actor,
            conversation_id=conversation_id,
            attachment_id=attachment_id,
        )
        return _attachment_response(attachment)
    except Exception as exc:
        raise _error(exc) from exc


@router.get("/attachments/{attachment_id}/download", response_model=AttachmentResponse)
def create_attachment_download(
    attachment_id: str,
    request: Request,
    conversation_id: str = Query(..., min_length=1, max_length=128),
    actor: ActorIdentity = Depends(require_server_actor),
) -> AttachmentResponse:
    _attachments_enabled(request)
    try:
        _require_conversation(request, actor=actor, conversation_id=conversation_id)
        download = _service(request).create_download(
            actor=actor,
            conversation_id=conversation_id,
            attachment_id=attachment_id,
        )
        return _attachment_response(download.attachment, download=download)
    except Exception as exc:
        raise _error(exc) from exc


@router.post("/attachments/{attachment_id}/reject", response_model=AttachmentResponse)
def reject_attachment(
    attachment_id: str,
    request: Request,
    conversation_id: str = Query(..., min_length=1, max_length=128),
    actor: ActorIdentity = Depends(require_server_actor),
) -> AttachmentResponse:
    _attachments_enabled(request)
    try:
        _require_conversation(request, actor=actor, conversation_id=conversation_id)
        attachment = _service(request).reject(
            actor=actor,
            conversation_id=conversation_id,
            attachment_id=attachment_id,
        )
        return _attachment_response(attachment)
    except Exception as exc:
        raise _error(exc) from exc


__all__ = ["router"]
