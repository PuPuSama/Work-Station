from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from services.access_control import ActorIdentity
from services.actor_sessions import (
    ActorSessionRevocationDenied,
    ActorSessionRevocationError,
    PostgresActorSessionRevocationService,
)
from services.workspace_users import (
    PostgresWorkspaceUserService,
    WorkspaceUserConflict,
    WorkspaceUserDenied,
    WorkspaceUserLastAdmin,
    WorkspaceUserNotFound,
    WorkspaceUserRecord,
    WorkspaceUserUnavailable,
)
from server_project_http import require_server_actor


class RevokeActorSessionsRequest(BaseModel):
    """An intentionally empty body; versions and roles are server facts."""

    model_config = ConfigDict(extra="forbid")


class RevokeActorSessionsResponse(BaseModel):
    user_id: str
    revoked: bool


class WorkspaceUserCreateRequest(BaseModel):
    """Create an active local user record; login linkage is separate."""

    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1, max_length=200)
    display_name: str = Field(min_length=1, max_length=200)
    organization_role: Literal["org_admin", "member"]

    @field_validator("user_id", "display_name")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized


class WorkspaceUserUpdateRequest(BaseModel):
    """Change public profile/lifecycle facts, never a session version."""

    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
    )
    status: Literal["active", "disabled"] | None = None
    organization_role: Literal["org_admin", "member"] | None = None

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("display name must not be blank")
        return normalized

    @model_validator(mode="after")
    def require_change(self) -> "WorkspaceUserUpdateRequest":
        if (
            self.display_name is None
            and self.status is None
            and self.organization_role is None
        ):
            raise ValueError("at least one workspace user field is required")
        return self


class WorkspaceUserResponse(BaseModel):
    user_id: str
    display_name: str
    status: Literal["active", "disabled"]
    organization_role: Literal["org_admin", "member"]
    team_membership_count: int
    project_membership_count: int
    login_linked: bool


class WorkspaceUserListResponse(BaseModel):
    items: list[WorkspaceUserResponse]
    next_after_user_id: str | None = None


def _revocation_service(
    request: Request,
) -> PostgresActorSessionRevocationService:
    service = getattr(
        request.app.state,
        "server_actor_session_revocation",
        None,
    )
    if not isinstance(service, PostgresActorSessionRevocationService):
        raise HTTPException(
            status_code=503,
            detail="Actor session management is not available.",
        )
    return service


def _workspace_user_service(
    request: Request,
) -> PostgresWorkspaceUserService:
    service = getattr(request.app.state, "server_workspace_users", None)
    if not isinstance(service, PostgresWorkspaceUserService):
        raise HTTPException(
            status_code=503,
            detail="Workspace user management is not available.",
        )
    return service


def _workspace_user_response(
    record: WorkspaceUserRecord,
) -> WorkspaceUserResponse:
    return WorkspaceUserResponse(
        user_id=record.user_id,
        display_name=record.display_name,
        status=record.status,
        organization_role=record.organization_role,
        team_membership_count=record.team_membership_count,
        project_membership_count=record.project_membership_count,
        login_linked=record.login_linked,
    )


def _raise_workspace_user_http_error(exc: Exception) -> None:
    if isinstance(exc, WorkspaceUserDenied):
        raise HTTPException(
            status_code=403,
            detail="workspace user administration denied",
        ) from exc
    if isinstance(exc, WorkspaceUserNotFound):
        raise HTTPException(
            status_code=404,
            detail="workspace user is unavailable",
        ) from exc
    if isinstance(exc, WorkspaceUserLastAdmin):
        raise HTTPException(
            status_code=409,
            detail="the last active organization administrator is protected",
        ) from exc
    if isinstance(exc, WorkspaceUserConflict):
        raise HTTPException(
            status_code=409,
            detail="workspace user change conflicted",
        ) from exc
    if isinstance(exc, (WorkspaceUserUnavailable, ValueError)):
        status_code = 422 if isinstance(exc, ValueError) else 503
        detail = (
            str(exc)
            if isinstance(exc, ValueError)
            else "Workspace user management is unavailable."
        )
        raise HTTPException(status_code=status_code, detail=detail) from exc
    raise exc


router = APIRouter(
    prefix="/api/organizations",
    tags=["server-administration"],
)


@router.get(
    "/{organization_id}/users",
    response_model=WorkspaceUserListResponse,
)
def list_workspace_users(
    organization_id: str,
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    after_user_id: str | None = Query(default=None, min_length=1),
    actor: ActorIdentity = Depends(require_server_actor),
) -> WorkspaceUserListResponse:
    try:
        page = _workspace_user_service(request).list_users(
            actor=actor,
            organization_id=organization_id,
            limit=limit,
            after_user_id=after_user_id,
        )
    except (
        WorkspaceUserConflict,
        WorkspaceUserDenied,
        WorkspaceUserLastAdmin,
        WorkspaceUserNotFound,
        WorkspaceUserUnavailable,
        ValueError,
    ) as exc:
        _raise_workspace_user_http_error(exc)
        raise AssertionError("workspace user error mapping returned")
    return WorkspaceUserListResponse(
        items=[_workspace_user_response(item) for item in page.items],
        next_after_user_id=page.next_after_user_id,
    )


@router.post(
    "/{organization_id}/users",
    response_model=WorkspaceUserResponse,
    status_code=201,
)
def create_workspace_user(
    organization_id: str,
    payload: WorkspaceUserCreateRequest,
    request: Request,
    actor: ActorIdentity = Depends(require_server_actor),
) -> WorkspaceUserResponse:
    try:
        record = _workspace_user_service(request).create_user(
            actor=actor,
            organization_id=organization_id,
            user_id=payload.user_id,
            display_name=payload.display_name,
            organization_role=payload.organization_role,
            event_id=f"workspace_user_create_{uuid.uuid4().hex}",
        )
    except (
        WorkspaceUserConflict,
        WorkspaceUserDenied,
        WorkspaceUserLastAdmin,
        WorkspaceUserNotFound,
        WorkspaceUserUnavailable,
        ValueError,
    ) as exc:
        _raise_workspace_user_http_error(exc)
        raise AssertionError("workspace user error mapping returned")
    return _workspace_user_response(record)


@router.patch(
    "/{organization_id}/users/{user_id}",
    response_model=WorkspaceUserResponse,
)
def update_workspace_user(
    organization_id: str,
    user_id: str,
    payload: WorkspaceUserUpdateRequest,
    request: Request,
    actor: ActorIdentity = Depends(require_server_actor),
) -> WorkspaceUserResponse:
    try:
        record = _workspace_user_service(request).update_user(
            actor=actor,
            organization_id=organization_id,
            user_id=user_id,
            event_id=f"workspace_user_update_{uuid.uuid4().hex}",
            display_name=payload.display_name,
            status=payload.status,
            organization_role=payload.organization_role,
        )
    except (
        WorkspaceUserConflict,
        WorkspaceUserDenied,
        WorkspaceUserLastAdmin,
        WorkspaceUserNotFound,
        WorkspaceUserUnavailable,
        ValueError,
    ) as exc:
        _raise_workspace_user_http_error(exc)
        raise AssertionError("workspace user error mapping returned")
    return _workspace_user_response(record)


@router.post(
    "/{organization_id}/users/{user_id}/sessions/revoke",
    response_model=RevokeActorSessionsResponse,
)
def revoke_actor_sessions(
    organization_id: str,
    user_id: str,
    payload: RevokeActorSessionsRequest,
    request: Request,
    actor: ActorIdentity = Depends(require_server_actor),
) -> RevokeActorSessionsResponse:
    del payload
    if organization_id.strip() != actor.organization_id:
        raise HTTPException(
            status_code=403,
            detail="actor session revocation denied",
        )
    try:
        _revocation_service(request).revoke_all(
            actor=actor,
            user_id=user_id,
            event_id=f"session_revoke_{uuid.uuid4().hex}",
        )
    except ActorSessionRevocationDenied as exc:
        raise HTTPException(
            status_code=403,
            detail="actor session revocation denied",
        ) from exc
    except ActorSessionRevocationError as exc:
        raise HTTPException(
            status_code=503,
            detail="Actor sessions could not be revoked.",
        ) from exc
    return RevokeActorSessionsResponse(
        user_id=user_id.strip(),
        revoked=True,
    )


__all__ = [
    "RevokeActorSessionsRequest",
    "RevokeActorSessionsResponse",
    "WorkspaceUserCreateRequest",
    "WorkspaceUserListResponse",
    "WorkspaceUserResponse",
    "WorkspaceUserUpdateRequest",
    "router",
]
