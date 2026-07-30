from __future__ import annotations

from datetime import datetime
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from server_project_http import require_server_actor
from services.access_control import ActorIdentity
from services.workspace_invitations import (
    IssuedWorkspaceInvitation,
    PostgresWorkspaceInvitationService,
    WorkspaceInvitationConflict,
    WorkspaceInvitationDenied,
    WorkspaceInvitationNotFound,
    WorkspaceInvitationRecord,
    WorkspaceInvitationUnavailable,
)


class WorkspaceInvitationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1, max_length=200)
    issuer: str = Field(min_length=1, max_length=2048)
    expires_in_hours: int = Field(default=24, ge=1, le=168)

    @field_validator("user_id", "issuer")
    @classmethod
    def validate_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized


class WorkspaceInvitationResponse(BaseModel):
    organization_id: str
    invitation_id: str
    user_id: str
    user_display_name: str
    issuer: str
    status: Literal["pending", "expired", "accepted", "revoked"]
    expires_at: datetime
    created_by_user_id: str
    created_at: datetime


class IssuedWorkspaceInvitationResponse(WorkspaceInvitationResponse):
    """Creation-only response; the raw token is never listable again."""

    invitation_token: str


class WorkspaceInvitationListResponse(BaseModel):
    items: list[WorkspaceInvitationResponse]
    next_after_invitation_id: str | None = None


def _service(request: Request) -> PostgresWorkspaceInvitationService:
    service = getattr(
        request.app.state,
        "server_workspace_invitations",
        None,
    )
    if not isinstance(service, PostgresWorkspaceInvitationService):
        raise HTTPException(
            status_code=503,
            detail="Workspace invitation management is not available.",
        )
    return service


def _response(
    record: WorkspaceInvitationRecord,
) -> WorkspaceInvitationResponse:
    return WorkspaceInvitationResponse(
        organization_id=record.organization_id,
        invitation_id=record.invitation_id,
        user_id=record.user_id,
        user_display_name=record.user_display_name,
        issuer=record.issuer,
        status=record.status,
        expires_at=record.expires_at,
        created_by_user_id=record.created_by_user_id,
        created_at=record.created_at,
    )


def _issued_response(
    issued: IssuedWorkspaceInvitation,
) -> IssuedWorkspaceInvitationResponse:
    return IssuedWorkspaceInvitationResponse(
        **_response(issued.invitation).model_dump(),
        invitation_token=issued.invitation_token,
    )


def _raise_http_error(exc: Exception) -> None:
    if isinstance(exc, WorkspaceInvitationDenied):
        raise HTTPException(
            status_code=403,
            detail="workspace invitation denied",
        ) from exc
    if isinstance(exc, WorkspaceInvitationConflict):
        raise HTTPException(
            status_code=409,
            detail="a pending invitation already exists",
        ) from exc
    if isinstance(exc, WorkspaceInvitationNotFound):
        raise HTTPException(
            status_code=404,
            detail="workspace invitation is unavailable",
        ) from exc
    if isinstance(exc, WorkspaceInvitationUnavailable):
        raise HTTPException(
            status_code=503,
            detail="Workspace invitation management is unavailable.",
        ) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    raise exc


_INVITATION_ERRORS = (
    WorkspaceInvitationConflict,
    WorkspaceInvitationDenied,
    WorkspaceInvitationNotFound,
    WorkspaceInvitationUnavailable,
    ValueError,
)


router = APIRouter(
    prefix="/api/organizations",
    tags=["server-invitation-administration"],
)


@router.get(
    "/{organization_id}/invitations",
    response_model=WorkspaceInvitationListResponse,
)
def list_workspace_invitations(
    organization_id: str,
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    after_invitation_id: str | None = Query(default=None, min_length=1),
    actor: ActorIdentity = Depends(require_server_actor),
) -> WorkspaceInvitationListResponse:
    try:
        page = _service(request).list_invitations(
            actor=actor,
            organization_id=organization_id,
            limit=limit,
            after_invitation_id=after_invitation_id,
        )
    except _INVITATION_ERRORS as exc:
        _raise_http_error(exc)
        raise AssertionError("invitation error mapping returned")
    return WorkspaceInvitationListResponse(
        items=[_response(item) for item in page.items],
        next_after_invitation_id=page.next_after_invitation_id,
    )


@router.post(
    "/{organization_id}/invitations",
    response_model=IssuedWorkspaceInvitationResponse,
    status_code=201,
)
def issue_workspace_invitation(
    organization_id: str,
    payload: WorkspaceInvitationCreateRequest,
    request: Request,
    actor: ActorIdentity = Depends(require_server_actor),
) -> IssuedWorkspaceInvitationResponse:
    try:
        issued = _service(request).issue(
            actor=actor,
            organization_id=organization_id,
            user_id=payload.user_id,
            issuer=payload.issuer,
            expires_in_hours=payload.expires_in_hours,
            event_id=f"workspace_invitation_issue_{uuid.uuid4().hex}",
        )
    except _INVITATION_ERRORS as exc:
        _raise_http_error(exc)
        raise AssertionError("invitation error mapping returned")
    return _issued_response(issued)


@router.delete(
    "/{organization_id}/invitations/{invitation_id}",
    response_model=WorkspaceInvitationResponse,
)
def revoke_workspace_invitation(
    organization_id: str,
    invitation_id: str,
    request: Request,
    actor: ActorIdentity = Depends(require_server_actor),
) -> WorkspaceInvitationResponse:
    try:
        record = _service(request).revoke(
            actor=actor,
            organization_id=organization_id,
            invitation_id=invitation_id,
            event_id=f"workspace_invitation_revoke_{uuid.uuid4().hex}",
        )
    except _INVITATION_ERRORS as exc:
        _raise_http_error(exc)
        raise AssertionError("invitation error mapping returned")
    return _response(record)


__all__ = [
    "IssuedWorkspaceInvitationResponse",
    "WorkspaceInvitationCreateRequest",
    "WorkspaceInvitationListResponse",
    "WorkspaceInvitationResponse",
    "router",
]
