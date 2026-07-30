from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from knowledge_agent.object_storage import (
    KnowledgeObjectNotFound,
    ProjectKnowledgeObjectService,
)
from models import TaskRecord
from services.access_control import ActorIdentity, ProjectAccessDenied
from services.object_store import ObjectStoreError
from services.project_directory import (
    AccessibleProject,
    PostgresProjectDirectory,
    ProjectDirectoryDenied,
)
from services.server_auth import SERVER_AUTH_COOKIE_NAME, server_mode_enabled
from services.server_project_tasks import ServerProjectTaskStoreFactory
from services.server_request_security import (
    AuthorizedProjectRequest,
    ServerRequestForbidden,
    ServerRequestSecurity,
    ServerRequestUnauthenticated,
)


class ProjectAssetDownload(BaseModel):
    asset_id: str
    url: str
    expires_seconds: int


def require_server_project_access(
    request: Request,
) -> AuthorizedProjectRequest:
    """Authenticate and authorize one explicitly project-scoped server API."""

    configured_mode = getattr(
        request.app.state,
        "server_mode_enabled",
        None,
    )
    enabled = (
        server_mode_enabled()
        if configured_mode is None
        else bool(configured_mode)
    )
    if not enabled:
        raise HTTPException(
            status_code=404,
            detail="Server project API is not available in local mode.",
        )
    security = getattr(
        request.app.state,
        "server_request_security",
        None,
    )
    if not isinstance(security, ServerRequestSecurity):
        raise HTTPException(
            status_code=503,
            detail="Server security is not available.",
        )
    try:
        return security.authorize_project(
            token=request.cookies.get(SERVER_AUTH_COOKIE_NAME, ""),
            project=str(request.path_params.get("project") or ""),
            permission="project.view",
        )
    except ServerRequestUnauthenticated as exc:
        raise HTTPException(
            status_code=401,
            detail="Authentication required.",
        ) from exc
    except ServerRequestForbidden as exc:
        raise HTTPException(
            status_code=403,
            detail="project access denied",
        ) from exc


def require_server_actor(request: Request) -> ActorIdentity:
    """Authenticate a server Actor before the SQL-scoped project listing."""

    configured_mode = getattr(
        request.app.state,
        "server_mode_enabled",
        None,
    )
    enabled = (
        server_mode_enabled()
        if configured_mode is None
        else bool(configured_mode)
    )
    if not enabled:
        raise HTTPException(
            status_code=404,
            detail="Server project API is not available in local mode.",
        )
    security = getattr(
        request.app.state,
        "server_request_security",
        None,
    )
    if not isinstance(security, ServerRequestSecurity):
        raise HTTPException(
            status_code=503,
            detail="Server security is not available.",
        )
    try:
        return security.authenticate(
            request.cookies.get(SERVER_AUTH_COOKIE_NAME, "")
        )
    except ServerRequestUnauthenticated as exc:
        raise HTTPException(
            status_code=401,
            detail="Authentication required.",
        ) from exc


def _project_directory(request: Request) -> PostgresProjectDirectory:
    directory = getattr(
        request.app.state,
        "server_project_directory",
        None,
    )
    if not isinstance(directory, PostgresProjectDirectory):
        raise HTTPException(
            status_code=503,
            detail="Server project directory is not available.",
        )
    return directory


def _task_store(
    request: Request,
    authorized: AuthorizedProjectRequest,
):
    factory = getattr(
        request.app.state,
        "server_project_task_store_factory",
        None,
    )
    if not isinstance(factory, ServerProjectTaskStoreFactory):
        raise HTTPException(
            status_code=503,
            detail="Server project task storage is not available.",
        )
    return factory.create(authorized).store


def _knowledge_object_service(
    request: Request,
) -> ProjectKnowledgeObjectService:
    service = getattr(
        request.app.state,
        "server_project_object_service",
        None,
    )
    if not isinstance(service, ProjectKnowledgeObjectService):
        raise HTTPException(
            status_code=503,
            detail="Server project object storage is not available.",
        )
    return service


router = APIRouter(
    prefix="/api/projects",
    tags=["server-projects"],
)


@router.get(
    "",
    response_model=list[AccessibleProject],
)
def list_accessible_projects(
    request: Request,
    actor: ActorIdentity = Depends(require_server_actor),
) -> list[AccessibleProject]:
    try:
        return list(_project_directory(request).list_for_actor(actor))
    except ProjectDirectoryDenied as exc:
        raise HTTPException(
            status_code=403,
            detail="project access denied",
        ) from exc


@router.get(
    "/{project}/tasks",
    response_model=list[TaskRecord],
)
def list_project_tasks(
    project: str,
    request: Request,
    status: str | None = Query(default=None),
    authorized: AuthorizedProjectRequest = Depends(
        require_server_project_access
    ),
) -> list[TaskRecord]:
    del project
    tasks = _task_store(request, authorized).load()
    if status:
        tasks = [task for task in tasks if task.status == status]
    return sorted(
        tasks,
        key=lambda task: (task.topic_index, task.id),
    )


@router.get(
    "/{project}/tasks/{task_id}",
    response_model=TaskRecord,
)
def read_project_task(
    project: str,
    task_id: str,
    request: Request,
    authorized: AuthorizedProjectRequest = Depends(
        require_server_project_access
    ),
) -> TaskRecord:
    del project
    try:
        return _task_store(request, authorized).get(task_id)
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail="Task was not found in the requested project.",
        ) from None


@router.get(
    "/{project}/assets/{asset_id}/download",
    response_model=ProjectAssetDownload,
)
def create_project_asset_download(
    project: str,
    asset_id: str,
    request: Request,
    expires_seconds: int = Query(default=300, ge=30, le=3600),
    authorized: AuthorizedProjectRequest = Depends(
        require_server_project_access
    ),
) -> ProjectAssetDownload:
    del project
    try:
        url = _knowledge_object_service(request).create_download_url(
            actor=authorized.actor,
            project_id=authorized.project_id,
            asset_id=asset_id,
            expires_seconds=expires_seconds,
        )
    except ProjectAccessDenied as exc:
        # The service reauthorizes immediately before signing. Membership may
        # have changed after the route dependency ran.
        raise HTTPException(
            status_code=403,
            detail="project access denied",
        ) from exc
    except KnowledgeObjectNotFound as exc:
        raise HTTPException(
            status_code=404,
            detail="Knowledge object was not found in the requested project.",
        ) from exc
    except ObjectStoreError as exc:
        raise HTTPException(
            status_code=503,
            detail="Object download is temporarily unavailable.",
        ) from exc
    return ProjectAssetDownload(
        asset_id=asset_id,
        url=url,
        expires_seconds=expires_seconds,
    )


__all__ = [
    "ProjectAssetDownload",
    "require_server_actor",
    "require_server_project_access",
    "router",
]
