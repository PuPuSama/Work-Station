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

from server_project_http import require_server_actor
from services.access_control import ActorIdentity
from services.team_administration import (
    PostgresTeamAdministrationService,
    TeamAdministrationConflict,
    TeamAdministrationDenied,
    TeamAdministrationUnavailable,
    TeamMemberRecord,
    TeamNotFound,
    TeamRecord,
    TeamUserNotFound,
)


class TeamCreateRequest(BaseModel):
    """Create an active Team; manager metadata does not grant access."""

    model_config = ConfigDict(extra="forbid")

    team_id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    manager_user_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
    )

    @field_validator("team_id", "name", "manager_user_id")
    @classmethod
    def validate_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized


class TeamUpdateRequest(BaseModel):
    """Update Team metadata/lifecycle without changing TeamMembership."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    status: Literal["active", "archived"] | None = None
    manager_user_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
    )

    @field_validator("name", "manager_user_id")
    @classmethod
    def validate_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized

    @model_validator(mode="after")
    def require_change(self) -> "TeamUpdateRequest":
        if not self.model_fields_set:
            raise ValueError("at least one team field is required")
        return self


class TeamResponse(BaseModel):
    team_id: str
    name: str
    manager_user_id: str | None
    status: Literal["active", "archived"]
    member_count: int
    team_lead_count: int
    project_count: int


class TeamListResponse(BaseModel):
    items: list[TeamResponse]
    next_after_team_id: str | None = None


class TeamMembershipUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["team_lead", "member"]


class TeamMemberResponse(BaseModel):
    user_id: str
    display_name: str
    user_status: Literal["active", "disabled"]
    role: Literal["team_lead", "member"]


class TeamMemberListResponse(BaseModel):
    items: list[TeamMemberResponse]
    next_after_user_id: str | None = None


class TeamMemberRevokeResponse(BaseModel):
    user_id: str
    revoked: bool


def _service(request: Request) -> PostgresTeamAdministrationService:
    service = getattr(request.app.state, "server_team_administration", None)
    if not isinstance(service, PostgresTeamAdministrationService):
        raise HTTPException(
            status_code=503,
            detail="Team administration is not available.",
        )
    return service


def _team_response(record: TeamRecord) -> TeamResponse:
    return TeamResponse(
        team_id=record.team_id,
        name=record.name,
        manager_user_id=record.manager_user_id,
        status=record.status,
        member_count=record.member_count,
        team_lead_count=record.team_lead_count,
        project_count=record.project_count,
    )


def _member_response(record: TeamMemberRecord) -> TeamMemberResponse:
    return TeamMemberResponse(
        user_id=record.user_id,
        display_name=record.display_name,
        user_status=record.user_status,
        role=record.role,
    )


def _raise_http_error(exc: Exception) -> None:
    if isinstance(exc, TeamAdministrationDenied):
        raise HTTPException(
            status_code=403,
            detail="team administration denied",
        ) from exc
    if isinstance(exc, TeamNotFound):
        raise HTTPException(
            status_code=404,
            detail="team is unavailable",
        ) from exc
    if isinstance(exc, TeamUserNotFound):
        raise HTTPException(
            status_code=404,
            detail="team user is unavailable",
        ) from exc
    if isinstance(exc, TeamAdministrationConflict):
        raise HTTPException(
            status_code=409,
            detail=str(exc),
        ) from exc
    if isinstance(exc, TeamAdministrationUnavailable):
        raise HTTPException(
            status_code=503,
            detail="Team administration is unavailable.",
        ) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    raise exc


_TEAM_ERRORS = (
    TeamAdministrationConflict,
    TeamAdministrationDenied,
    TeamAdministrationUnavailable,
    TeamNotFound,
    TeamUserNotFound,
    ValueError,
)


router = APIRouter(
    prefix="/api/organizations",
    tags=["server-team-administration"],
)


@router.get(
    "/{organization_id}/teams",
    response_model=TeamListResponse,
)
def list_teams(
    organization_id: str,
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    after_team_id: str | None = Query(default=None, min_length=1),
    actor: ActorIdentity = Depends(require_server_actor),
) -> TeamListResponse:
    try:
        page = _service(request).list_teams(
            actor=actor,
            organization_id=organization_id,
            limit=limit,
            after_team_id=after_team_id,
        )
    except _TEAM_ERRORS as exc:
        _raise_http_error(exc)
        raise AssertionError("team error mapping returned")
    return TeamListResponse(
        items=[_team_response(item) for item in page.items],
        next_after_team_id=page.next_after_team_id,
    )


@router.post(
    "/{organization_id}/teams",
    response_model=TeamResponse,
    status_code=201,
)
def create_team(
    organization_id: str,
    payload: TeamCreateRequest,
    request: Request,
    actor: ActorIdentity = Depends(require_server_actor),
) -> TeamResponse:
    try:
        record = _service(request).create_team(
            actor=actor,
            organization_id=organization_id,
            team_id=payload.team_id,
            name=payload.name,
            manager_user_id=payload.manager_user_id,
            event_id=f"team_create_{uuid.uuid4().hex}",
        )
    except _TEAM_ERRORS as exc:
        _raise_http_error(exc)
        raise AssertionError("team error mapping returned")
    return _team_response(record)


@router.patch(
    "/{organization_id}/teams/{team_id}",
    response_model=TeamResponse,
)
def update_team(
    organization_id: str,
    team_id: str,
    payload: TeamUpdateRequest,
    request: Request,
    actor: ActorIdentity = Depends(require_server_actor),
) -> TeamResponse:
    try:
        record = _service(request).update_team(
            actor=actor,
            organization_id=organization_id,
            team_id=team_id,
            event_id=f"team_update_{uuid.uuid4().hex}",
            name=payload.name,
            status=payload.status,
            manager_user_id=payload.manager_user_id,
            manager_user_id_set=(
                "manager_user_id" in payload.model_fields_set
            ),
        )
    except _TEAM_ERRORS as exc:
        _raise_http_error(exc)
        raise AssertionError("team error mapping returned")
    return _team_response(record)


@router.get(
    "/{organization_id}/teams/{team_id}/members",
    response_model=TeamMemberListResponse,
)
def list_team_members(
    organization_id: str,
    team_id: str,
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    after_user_id: str | None = Query(default=None, min_length=1),
    actor: ActorIdentity = Depends(require_server_actor),
) -> TeamMemberListResponse:
    try:
        page = _service(request).list_members(
            actor=actor,
            organization_id=organization_id,
            team_id=team_id,
            limit=limit,
            after_user_id=after_user_id,
        )
    except _TEAM_ERRORS as exc:
        _raise_http_error(exc)
        raise AssertionError("team error mapping returned")
    return TeamMemberListResponse(
        items=[_member_response(item) for item in page.items],
        next_after_user_id=page.next_after_user_id,
    )


@router.put(
    "/{organization_id}/teams/{team_id}/members/{user_id}",
    response_model=TeamMemberResponse,
)
def upsert_team_member(
    organization_id: str,
    team_id: str,
    user_id: str,
    payload: TeamMembershipUpdateRequest,
    request: Request,
    actor: ActorIdentity = Depends(require_server_actor),
) -> TeamMemberResponse:
    try:
        record = _service(request).upsert_member(
            actor=actor,
            organization_id=organization_id,
            team_id=team_id,
            user_id=user_id,
            role=payload.role,
            event_id=f"team_membership_upsert_{uuid.uuid4().hex}",
        )
    except _TEAM_ERRORS as exc:
        _raise_http_error(exc)
        raise AssertionError("team error mapping returned")
    return _member_response(record)


@router.delete(
    "/{organization_id}/teams/{team_id}/members/{user_id}",
    response_model=TeamMemberRevokeResponse,
)
def revoke_team_member(
    organization_id: str,
    team_id: str,
    user_id: str,
    request: Request,
    actor: ActorIdentity = Depends(require_server_actor),
) -> TeamMemberRevokeResponse:
    try:
        revoked = _service(request).revoke_member(
            actor=actor,
            organization_id=organization_id,
            team_id=team_id,
            user_id=user_id,
            event_id=f"team_membership_revoke_{uuid.uuid4().hex}",
        )
    except _TEAM_ERRORS as exc:
        _raise_http_error(exc)
        raise AssertionError("team error mapping returned")
    return TeamMemberRevokeResponse(
        user_id=user_id.strip(),
        revoked=revoked,
    )


__all__ = [
    "TeamCreateRequest",
    "TeamListResponse",
    "TeamMemberListResponse",
    "TeamMemberResponse",
    "TeamMemberRevokeResponse",
    "TeamMembershipUpdateRequest",
    "TeamResponse",
    "TeamUpdateRequest",
    "router",
]
