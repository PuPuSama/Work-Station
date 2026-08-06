from __future__ import annotations

from typing import Literal, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from models import PromptSnapshot
from server_project_http import require_server_project_access
from services.access_control import ProjectAccessDenied
from services.server_project_prompts import (
    ServerProjectPromptConflict,
    ServerProjectPromptError,
    ServerProjectPromptServiceFactory,
    ServerProjectPromptUnavailable,
)
from services.server_request_security import AuthorizedProjectRequest


PromptKind = Literal["outline", "article", "review", "humanize"]


class ServerPromptItemResponse(BaseModel):
    prompt_id: str
    name: str
    kind: PromptKind
    content: str
    version: int
    status: Literal["active", "archived"]
    captured_at: str


class ServerPromptDirectoryResponse(BaseModel):
    prompts: list[ServerPromptItemResponse]
    defaults: dict[PromptKind, PromptSnapshot]


class ServerPromptCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    kind: PromptKind
    content: str = Field(min_length=1, max_length=40000)


class ServerPromptUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=40000)


class ServerPromptActiveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    active: bool


class ServerPromptDefaultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt_id: str | None = Field(default=None, max_length=255)


def _service(
    request: Request,
    authorized: AuthorizedProjectRequest,
):
    factory = getattr(
        request.app.state,
        "server_project_prompt_service_factory",
        None,
    )
    if not isinstance(factory, ServerProjectPromptServiceFactory):
        raise HTTPException(
            status_code=503,
            detail="Project prompt management is not available.",
        )
    return factory.create(authorized)


def _raise_prompt_error(exc: Exception) -> NoReturn:
    if isinstance(exc, ProjectAccessDenied):
        raise HTTPException(
            status_code=403,
            detail="project access denied",
        ) from exc
    if isinstance(exc, KeyError):
        raise HTTPException(
            status_code=404,
            detail="Project prompt was not found.",
        ) from None
    if isinstance(exc, ServerProjectPromptConflict):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, ServerProjectPromptError):
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    raise HTTPException(
        status_code=503,
        detail="Project prompt management is temporarily unavailable.",
    ) from exc


def _snapshot_response(
    snapshot: PromptSnapshot,
    *,
    status: Literal["active", "archived"] = "active",
) -> ServerPromptItemResponse:
    return ServerPromptItemResponse(
        prompt_id=snapshot.prompt_id,
        name=snapshot.name,
        kind=snapshot.kind,
        content=snapshot.content,
        version=snapshot.version,
        status=status,
        captured_at=snapshot.captured_at,
    )


router = APIRouter(
    prefix="/api/projects",
    tags=["server-project-prompts"],
)


@router.get(
    "/{project}/prompt-snapshots",
    response_model=ServerPromptDirectoryResponse,
)
def list_server_project_prompts(
    project: str,
    request: Request,
    authorized: AuthorizedProjectRequest = Depends(
        require_server_project_access
    ),
) -> ServerPromptDirectoryResponse:
    del project
    try:
        directory = _service(request, authorized).list(
            authorized.actor
        )
    except (
        ProjectAccessDenied,
        ServerProjectPromptUnavailable,
    ) as exc:
        _raise_prompt_error(exc)
    return ServerPromptDirectoryResponse(
        prompts=[
            _snapshot_response(
                item.snapshot,
                status=item.status,
            )
            for item in directory.prompts
        ],
        defaults=directory.defaults,
    )


@router.post(
    "/{project}/prompt-snapshots",
    response_model=ServerPromptItemResponse,
    status_code=201,
)
def create_server_project_prompt(
    project: str,
    payload: ServerPromptCreateRequest,
    request: Request,
    authorized: AuthorizedProjectRequest = Depends(
        require_server_project_access
    ),
) -> ServerPromptItemResponse:
    del project
    try:
        snapshot = _service(request, authorized).create(
            authorized.actor,
            name=payload.name,
            kind=payload.kind,
            content=payload.content,
        )
    except (
        ProjectAccessDenied,
        ServerProjectPromptError,
        ServerProjectPromptUnavailable,
    ) as exc:
        _raise_prompt_error(exc)
    return _snapshot_response(snapshot)


@router.put(
    "/{project}/prompt-snapshots/{prompt_id}",
    response_model=ServerPromptItemResponse,
)
def update_server_project_prompt(
    project: str,
    prompt_id: str,
    payload: ServerPromptUpdateRequest,
    request: Request,
    authorized: AuthorizedProjectRequest = Depends(
        require_server_project_access
    ),
) -> ServerPromptItemResponse:
    del project
    try:
        snapshot = _service(request, authorized).update(
            authorized.actor,
            prompt_id=prompt_id,
            expected_version=payload.expected_version,
            name=payload.name,
            content=payload.content,
        )
    except (
        KeyError,
        ProjectAccessDenied,
        ServerProjectPromptConflict,
        ServerProjectPromptError,
        ServerProjectPromptUnavailable,
    ) as exc:
        _raise_prompt_error(exc)
    return _snapshot_response(snapshot)


@router.put(
    "/{project}/prompt-snapshots/{prompt_id}/active",
    response_model=ServerPromptItemResponse,
)
def set_server_project_prompt_active(
    project: str,
    prompt_id: str,
    payload: ServerPromptActiveRequest,
    request: Request,
    authorized: AuthorizedProjectRequest = Depends(
        require_server_project_access
    ),
) -> ServerPromptItemResponse:
    del project
    try:
        item = _service(request, authorized).set_active(
            authorized.actor,
            prompt_id=prompt_id,
            expected_version=payload.expected_version,
            active=payload.active,
        )
    except (
        KeyError,
        ProjectAccessDenied,
        ServerProjectPromptConflict,
        ServerProjectPromptError,
        ServerProjectPromptUnavailable,
    ) as exc:
        _raise_prompt_error(exc)
    return _snapshot_response(item.snapshot, status=item.status)


@router.put(
    "/{project}/prompt-defaults/{kind}",
    response_model=PromptSnapshot,
)
def set_server_project_prompt_default(
    project: str,
    kind: PromptKind,
    payload: ServerPromptDefaultRequest,
    request: Request,
    authorized: AuthorizedProjectRequest = Depends(
        require_server_project_access
    ),
) -> PromptSnapshot:
    del project
    try:
        return _service(request, authorized).set_default(
            authorized.actor,
            kind=kind,
            prompt_id=payload.prompt_id,
        )
    except (
        ProjectAccessDenied,
        ServerProjectPromptError,
        ServerProjectPromptUnavailable,
    ) as exc:
        _raise_prompt_error(exc)
    raise AssertionError("unreachable")
