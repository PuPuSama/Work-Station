from __future__ import annotations

import uuid
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from server_project_http import require_server_actor
from services.access_control import ActorIdentity
from services.local_password_login import (
    LocalPasswordLoginFailed,
    LocalPasswordLoginService,
    LocalPasswordLoginUnavailable,
)
from services.server_auth import SERVER_AUTH_COOKIE_NAME
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


class AccountPasswordChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=8, max_length=256)


class AccountPasswordChangeResponse(BaseModel):
    message: str


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


def _auth_cookie_secure(request: Request) -> bool:
    configured = os.environ.get("APP_COOKIE_SECURE", "").strip().lower()
    if configured in {"1", "true", "yes", "on"}:
        return True
    forwarded_proto = request.headers.get("x-forwarded-proto", "")
    return request.url.scheme == "https" or (
        forwarded_proto.split(",", 1)[0].strip() == "https"
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


@router.post(
    "/password",
    response_model=AccountPasswordChangeResponse,
)
def change_account_password(
    payload: AccountPasswordChangeRequest,
    request: Request,
    actor: ActorIdentity = Depends(require_server_actor),
) -> JSONResponse:
    service = getattr(request.app.state, "server_password_login", None)
    if not isinstance(service, LocalPasswordLoginService):
        raise HTTPException(
            status_code=503,
            detail="Password login is not configured.",
        )
    try:
        result = service.change_password(
            actor=actor,
            current_password=payload.current_password,
            new_password=payload.new_password,
            event_id=f"workspace_user_password_{uuid.uuid4().hex}",
        )
    except LocalPasswordLoginFailed as exc:
        raise HTTPException(
            status_code=401,
            detail="当前密码错误。",
        ) from exc
    except LocalPasswordLoginUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="密码服务当前不可用。",
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    response = JSONResponse(
        content={"message": "密码已更新。"},
    )
    response.set_cookie(
        key=SERVER_AUTH_COOKIE_NAME,
        value=result.actor_session,
        max_age=service.settings.session_seconds,
        httponly=True,
        secure=_auth_cookie_secure(request),
        samesite="lax",
        path="/",
    )
    return response


__all__ = [
    "AccountProfileResponse",
    "AccountProfileUpdateRequest",
    "AccountPasswordChangeRequest",
    "AccountPasswordChangeResponse",
    "router",
]
