from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from knowledge_agent.object_storage import (
    KnowledgeObjectNotFound,
    ProjectKnowledgeObjectService,
)
from models import RevisionedRequest, TaskRecord
from services.access_control import (
    ActorIdentity,
    ProjectAccessDenied,
    ProjectPermission,
)
from services.object_store import ObjectStoreError
from services.project_directory import (
    AccessibleProject,
    PostgresProjectDirectory,
    ProjectDirectoryDenied,
)
from services.server_auth import SERVER_AUTH_COOKIE_NAME, server_mode_enabled
from services.server_project_tasks import ServerProjectTaskStoreFactory
from services.server_product_selection import (
    ConfirmedProductSelectionError,
    PostgresConfirmedProductSelection,
)
from services.server_request_security import (
    AuthorizedProjectRequest,
    ServerRequestForbidden,
    ServerRequestSecurity,
    ServerRequestUnauthenticated,
)
from storage import RevisionConflictError
from workflow.state_machine import (
    ACTION_REWRITE_FROM_SCRATCH,
    ACTION_UPDATE_PRODUCTS,
    WorkflowActionNotAllowed,
    ensure_action_allowed,
    invalidate_downstream,
    reset_for_full_rewrite,
)


class ProjectAssetDownload(BaseModel):
    asset_id: str
    url: str
    expires_seconds: int


class ConfirmedProductsUpdateRequest(BaseModel):
    """Select catalog identities, never caller-supplied product facts or URLs."""

    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=0)
    product_ids: list[str] = Field(min_length=1, max_length=3)

    @field_validator("product_ids")
    @classmethod
    def validate_product_ids(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("product ids must not be empty")
        if len(set(normalized)) != len(normalized):
            raise ValueError("product ids must be unique")
        return normalized


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


def _confirmed_product_selection(
    request: Request,
) -> PostgresConfirmedProductSelection:
    selection = getattr(
        request.app.state,
        "server_confirmed_product_selection",
        None,
    )
    if not isinstance(selection, PostgresConfirmedProductSelection):
        raise HTTPException(
            status_code=503,
            detail="Server product selection is not available.",
        )
    return selection


def _require_project_permission(
    request: Request,
    authorized: AuthorizedProjectRequest,
    permission: ProjectPermission,
) -> AuthorizedProjectRequest:
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
            project=authorized.project_id,
            permission=permission,
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


@router.post(
    "/{project}/tasks/{task_id}/rewrite-from-scratch",
    response_model=TaskRecord,
)
def rewrite_project_task_from_scratch(
    project: str,
    task_id: str,
    payload: RevisionedRequest,
    request: Request,
    authorized: AuthorizedProjectRequest = Depends(
        require_server_project_access
    ),
) -> TaskRecord:
    del project
    authorized = _require_project_permission(
        request,
        authorized,
        "article.edit",
    )
    store = _task_store(request, authorized)
    try:
        task = store.get(task_id)
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail="Task was not found in the requested project.",
        ) from None
    try:
        ensure_action_allowed(
            task,
            ACTION_REWRITE_FROM_SCRATCH,
        )
    except WorkflowActionNotAllowed as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
        ) from exc
    reset_for_full_rewrite(task)
    try:
        return store.put(
            task,
            expected_revision=payload.revision,
        )
    except RevisionConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
        ) from exc


@router.put(
    "/{project}/tasks/{task_id}/products",
    response_model=TaskRecord,
)
def replace_project_task_products(
    project: str,
    task_id: str,
    payload: ConfirmedProductsUpdateRequest,
    request: Request,
    authorized: AuthorizedProjectRequest = Depends(
        require_server_project_access
    ),
) -> TaskRecord:
    del project
    authorized = _require_project_permission(
        request,
        authorized,
        "article.edit",
    )
    store = _task_store(request, authorized)
    try:
        task = store.get(task_id)
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail="Task was not found in the requested project.",
        ) from None
    try:
        ensure_action_allowed(task, ACTION_UPDATE_PRODUCTS)
    except WorkflowActionNotAllowed as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
        ) from exc
    try:
        products = _confirmed_product_selection(request).select(
            authorized.project_id,
            payload.product_ids,
        )
    except ConfirmedProductSelectionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    task.products = list(products)
    invalidate_downstream(task, "products")
    try:
        return store.put(
            task,
            expected_revision=payload.revision,
        )
    except RevisionConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
        ) from exc


__all__ = [
    "ConfirmedProductsUpdateRequest",
    "ProjectAssetDownload",
    "require_server_actor",
    "require_server_project_access",
    "router",
]
