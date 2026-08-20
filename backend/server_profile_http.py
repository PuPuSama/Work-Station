from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from server_project_http import require_server_actor
from services.access_control import ActorIdentity
from services.workspace_users import (
    PostgresWorkspaceUserService,
    WorkspaceUserConflict,
    WorkspaceUserNotFound,
    WorkspaceUserUnavailable,
)


class AccountProfileUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=200)

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("display name must not be blank")
        return normalized


class AccountProfileResponse(BaseModel):
    organization_id: str
    user_id: str
    display_name: str
    status: str
    organization_role: str


def _service(request: Request) -> PostgresWorkspaceUserService:
    service = getattr(request.app.state, "server_workspace_users", None)
    if not isinstance(service, PostgresWorkspaceUserService):
        raise HTTPException(
            status_code=503,
            detail="Workspace user management is not available.",
        )
    return service


def _response(actor: ActorIdentity, record) -> AccountProfileResponse:
    return AccountProfileResponse(
        organization_id=actor.organization_id,
        user_id=record.user_id,
        display_name=record.display_name,
        status=record.status,
        organization_role=record.organization_role,
    )


def _raise_profile_error(exc: Exception) -> None:
    if isinstance(exc, WorkspaceUserNotFound):
        raise HTTPException(
            status_code=404,
            detail="workspace user profile is unavailable",
        ) from exc
    if isinstance(exc, WorkspaceUserConflict):
        raise HTTPException(
            status_code=409,
            detail="workspace user profile change conflicted",
        ) from exc
    if isinstance(exc, WorkspaceUserUnavailable):
        raise HTTPException(
            status_code=503,
            detail="Workspace user management is unavailable.",
        ) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    raise exc


_PROFILE_ERRORS = (
    WorkspaceUserConflict,
    WorkspaceUserNotFound,
    WorkspaceUserUnavailable,
    ValueError,
)


router = APIRouter(
    prefix="/api/account",
    tags=["server-account"],
)


@router.get("/profile", response_model=AccountProfileResponse)
def get_account_profile(
    request: Request,
    actor: ActorIdentity = Depends(require_server_actor),
) -> AccountProfileResponse:
    try:
        record = _service(request).get_profile(actor=actor)
    except _PROFILE_ERRORS as exc:
        _raise_profile_error(exc)
        raise AssertionError("profile error mapping returned")
    return _response(actor, record)


@router.patch("/profile", response_model=AccountProfileResponse)
def update_account_profile(
    payload: AccountProfileUpdateRequest,
    request: Request,
    actor: ActorIdentity = Depends(require_server_actor),
) -> AccountProfileResponse:
    try:
        record = _service(request).update_profile(
            actor=actor,
            display_name=payload.display_name,
            event_id=f"workspace_user_profile_{uuid.uuid4().hex}",
        )
    except _PROFILE_ERRORS as exc:
        _raise_profile_error(exc)
        raise AssertionError("profile error mapping returned")
    return _response(actor, record)


__all__ = [
    "AccountProfileResponse",
    "AccountProfileUpdateRequest",
    "router",
]
