from __future__ import annotations

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator

from config import AppConfig
from knowledge_agent.object_storage import (
    KnowledgeObjectNotFound,
    ProjectKnowledgeObjectService,
)
from models import AICheck, STATUS_FINAL_AI_CHECKED, TaskRecord
from services.access_control import (
    ActorIdentity,
    ProjectAccessDenied,
    ProjectPermission,
)
from services.job_queue import ActiveJobError, JobConflict
from services.object_store import ObjectStoreError, ObjectTooLarge
from services.project_directory import (
    AccessibleProject,
    PostgresProjectDirectory,
    ProjectDirectoryDenied,
)
from services.server_auth import SERVER_AUTH_COOKIE_NAME, server_mode_enabled
from services.server_article_images import (
    ServerArticleImageAnchorRequired,
    ServerArticleImageError,
    ServerArticleImagePreparation,
)
from services.server_ai_screenshots import (
    MAX_SERVER_AI_SCREENSHOT_BYTES,
    ServerAiScreenshotError,
    ServerFinalAiScreenshotPreparation,
)
from services.server_docx_export import (
    ServerArticleDocxError,
    ServerArticleDocxExport,
)
from services.server_delivery_package import (
    ServerDeliveryPackage,
    ServerDeliveryPackageError,
)
from services.server_project_tasks import (
    ServerProjectTaskRuntime,
    ServerProjectTaskStoreFactory,
)
from services.server_product_selection import (
    ConfirmedProductSelectionError,
    PostgresConfirmedProductSelection,
)
from services.server_product_rediscovery import (
    ProductRediscoveryCommand,
    ProductRediscoveryUnavailable,
    ServerProductRediscoveryRegistry,
)
from services.server_section_rewrite import (
    SectionRewriteError,
    rewrite_initial_article_section,
)
from services.server_request_security import (
    AuthorizedProjectRequest,
    ServerRequestForbidden,
    ServerRequestSecurity,
    ServerRequestUnauthenticated,
)
from services.server_task_commands import (
    ServerTaskAuditAction,
    ServerTaskCommandUnavailable,
)
from services.server_tdk_export import (
    ServerTdkDocxExport,
    ServerTdkError,
    ServerTdkUnavailable,
)
from storage import RevisionConflictError, content_hash, now_iso
from workflow.state_machine import (
    ACTION_CONFIRM_FINAL_AI,
    ACTION_DOWNLOAD_DOCX,
    ACTION_EXPORT_DOCX,
    ACTION_GENERATE_TDK,
    ACTION_PACKAGE_DELIVERY,
    ACTION_PREPARE_IMAGES,
    ACTION_REWRITE_FROM_SCRATCH,
    ACTION_UPDATE_ARTICLE,
    ACTION_UPDATE_PRODUCTS,
    WorkflowActionNotAllowed,
    ensure_action_allowed,
    invalidate_downstream,
    reset_for_full_rewrite,
    transition_task,
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


class ProjectRevisionRequest(BaseModel):
    """Require an explicit optimistic Revision for a Server Task command."""

    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=0)


class FinalAiCheckUpdateRequest(BaseModel):
    """Bind a manual final AI review to the current humanized article."""

    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=0)
    score: float | None = Field(
        default=None,
        ge=0,
        le=100,
        allow_inf_nan=False,
    )
    report: str = Field(default="", max_length=30000)
    confirmed: bool = True


class ArticleSectionRewriteRequest(BaseModel):
    """A bounded article mutation; generation happens before this commit step."""

    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=0)
    heading_path: list[str] = Field(min_length=1, max_length=5)
    replacement_body: str = Field(min_length=1, max_length=30000)

    @field_validator("heading_path")
    @classmethod
    def validate_heading_path(cls, values: list[str]) -> list[str]:
        normalized = [" ".join(value.split()) for value in values]
        if any(not value for value in normalized):
            raise ValueError("heading_path values must not be empty")
        return normalized


class PrepareProjectImagesRequest(BaseModel):
    """Prepare one trusted hero plus Task-bound product assets."""

    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=0)
    hero_asset_id: str = Field(min_length=1, max_length=512)
    product_anchors: dict[str, str] = Field(
        default_factory=dict,
        max_length=3,
    )

    @field_validator("hero_asset_id")
    @classmethod
    def validate_hero_asset_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("hero asset id must not be empty")
        return normalized

    @field_validator("product_anchors")
    @classmethod
    def validate_product_anchors(
        cls,
        value: dict[str, str],
    ) -> dict[str, str]:
        normalized = {
            product_id.strip(): " ".join(heading.split())
            for product_id, heading in value.items()
        }
        if (
            len(normalized) != len(value)
            or any(
                not product_id or not heading
                for product_id, heading in normalized.items()
            )
        ):
            raise ValueError(
                "product anchors require unique product ids and headings"
            )
        return normalized


class ProductRediscoveryRequest(BaseModel):
    """Queue bounded official-site discovery without exposing worker input."""

    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=0)
    category_url: str = Field(min_length=1, max_length=4096)
    max_products: int = Field(default=12, ge=1, le=50)


class ProductRediscoveryJobResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    batch_id: str
    task_id: str
    operation: str
    status: str
    source_revision: int
    result_revision: int | None
    attempts: int
    created_at: str
    started_at: str | None
    finished_at: str | None
    updated_at: str
    has_error: bool


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
    return _task_runtime(request, authorized).store


def _task_runtime(
    request: Request,
    authorized: AuthorizedProjectRequest,
) -> ServerProjectTaskRuntime:
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
    return factory.create(authorized)


def _save_audited_task(
    request: Request,
    authorized: AuthorizedProjectRequest,
    task: TaskRecord,
    *,
    expected_revision: int,
    action: ServerTaskAuditAction,
    details: dict[str, object] | None = None,
) -> TaskRecord:
    try:
        return _task_runtime(
            request,
            authorized,
        ).audited_writer.put(
            task,
            expected_revision=expected_revision,
            actor=authorized.actor,
            action=action,
            details=details,
        )
    except ProjectAccessDenied as exc:
        raise HTTPException(
            status_code=403,
            detail="project access denied",
        ) from exc
    except RevisionConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
        ) from exc
    except ServerTaskCommandUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="Task update is temporarily unavailable.",
        ) from exc


def _server_app_config(request: Request) -> AppConfig:
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
    return factory.config


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


def _product_rediscovery(
    request: Request,
) -> ServerProductRediscoveryRegistry:
    registry = getattr(
        request.app.state,
        "server_product_rediscovery",
        None,
    )
    if not isinstance(registry, ServerProductRediscoveryRegistry):
        raise HTTPException(
            status_code=503,
            detail="Server product rediscovery is not available.",
        )
    return registry


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
    payload: ProjectRevisionRequest,
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
    return _save_audited_task(
        request,
        authorized,
        task,
        expected_revision=payload.revision,
        action="article.task.rewritten",
    )


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
    return _save_audited_task(
        request,
        authorized,
        task,
        expected_revision=payload.revision,
        action="article.products.confirmed",
        details={"product_count": len(task.products)},
    )


@router.post(
    "/{project}/tasks/{task_id}/checks/final-ai/screenshot",
    response_model=TaskRecord,
)
def upload_project_task_final_ai_screenshot(
    project: str,
    task_id: str,
    request: Request,
    revision: int = Query(ge=0),
    file: UploadFile = File(...),
    authorized: AuthorizedProjectRequest = Depends(
        require_server_project_access
    ),
) -> TaskRecord:
    del project
    authorized = _require_project_permission(
        request,
        authorized,
        "article.review",
    )
    store = _task_store(request, authorized)
    try:
        task = store.get(task_id)
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail="Task was not found in the requested project.",
        ) from None
    if task.revision != revision:
        raise HTTPException(
            status_code=409,
            detail=str(
                RevisionConflictError(
                    task.id,
                    revision,
                    task.revision,
                )
            ),
        )
    try:
        ensure_action_allowed(task, ACTION_CONFIRM_FINAL_AI)
    except WorkflowActionNotAllowed as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    content = file.file.read(MAX_SERVER_AI_SCREENSHOT_BYTES + 1)
    if len(content) > MAX_SERVER_AI_SCREENSHOT_BYTES:
        raise HTTPException(
            status_code=413,
            detail="AI-rate screenshot exceeds 25 MB.",
        )
    try:
        ServerFinalAiScreenshotPreparation(
            objects=_knowledge_object_service(request),
        ).prepare(
            actor=authorized.actor,
            project_id=authorized.project_id,
            task=task,
            content=content,
        )
    except ProjectAccessDenied as exc:
        raise HTTPException(
            status_code=403,
            detail="project access denied",
        ) from exc
    except ServerAiScreenshotError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ObjectStoreError as exc:
        raise HTTPException(
            status_code=503,
            detail="AI-rate screenshot storage is temporarily unavailable.",
        ) from exc
    return _save_audited_task(
        request,
        authorized,
        task,
        expected_revision=revision,
        action="article.final_ai_screenshot.uploaded",
        details={
            "screenshot_height": (
                task.final_ai_check.screenshot_height or 0
            ),
            "screenshot_width": (
                task.final_ai_check.screenshot_width or 0
            ),
        },
    )


@router.put(
    "/{project}/tasks/{task_id}/checks/final-ai",
    response_model=TaskRecord,
)
def confirm_project_task_final_ai(
    project: str,
    task_id: str,
    payload: FinalAiCheckUpdateRequest,
    request: Request,
    authorized: AuthorizedProjectRequest = Depends(
        require_server_project_access
    ),
) -> TaskRecord:
    del project
    authorized = _require_project_permission(
        request,
        authorized,
        "article.review",
    )
    store = _task_store(request, authorized)
    try:
        task = store.get(task_id)
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail="Task was not found in the requested project.",
        ) from None
    if task.revision != payload.revision:
        raise HTTPException(
            status_code=409,
            detail=str(
                RevisionConflictError(
                    task.id,
                    payload.revision,
                    task.revision,
                )
            ),
        )
    try:
        ensure_action_allowed(task, ACTION_CONFIRM_FINAL_AI)
    except WorkflowActionNotAllowed as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    previous = task.final_ai_check
    if payload.confirmed and not previous.screenshot_asset_id.strip():
        raise HTTPException(
            status_code=409,
            detail="Upload the final AI-rate screenshot before confirming.",
        )
    task.final_ai_check = AICheck(
        confirmed=payload.confirmed,
        score=payload.score,
        report=payload.report,
        screenshot_path="",
        screenshot_asset_id=previous.screenshot_asset_id,
        screenshot_content_hash=previous.screenshot_content_hash,
        screenshot_filename=previous.screenshot_filename,
        screenshot_width=previous.screenshot_width,
        screenshot_height=previous.screenshot_height,
        confirmed_at=now_iso() if payload.confirmed else "",
        article_hash=content_hash(task.humanized_article),
    )
    if payload.confirmed:
        transition_task(task, STATUS_FINAL_AI_CHECKED)
    return _save_audited_task(
        request,
        authorized,
        task,
        expected_revision=payload.revision,
        action="article.final_ai_check.updated",
        details={
            "confirmed": payload.confirmed,
            "score_recorded": payload.score is not None,
        },
    )


@router.get(
    "/{project}/tasks/{task_id}/checks/final-ai/screenshot/download",
    response_model=ProjectAssetDownload,
)
def create_project_task_final_ai_screenshot_download(
    project: str,
    task_id: str,
    request: Request,
    expires_seconds: int = Query(default=300, ge=30, le=3600),
    authorized: AuthorizedProjectRequest = Depends(
        require_server_project_access
    ),
) -> ProjectAssetDownload:
    del project
    authorized = _require_project_permission(
        request,
        authorized,
        "article.review",
    )
    store = _task_store(request, authorized)
    try:
        task = store.get(task_id)
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail="Task was not found in the requested project.",
        ) from None
    asset_id = task.final_ai_check.screenshot_asset_id.strip()
    if not asset_id:
        raise HTTPException(
            status_code=409,
            detail="The final AI-rate screenshot has not been uploaded.",
        )
    try:
        url = (
            _knowledge_object_service(
                request
            ).create_final_ai_screenshot_download_url(
                actor=authorized.actor,
                project_id=authorized.project_id,
                asset_id=asset_id,
                content_hash=(
                    task.final_ai_check.screenshot_content_hash
                ),
                width=task.final_ai_check.screenshot_width or 0,
                height=task.final_ai_check.screenshot_height or 0,
                expires_seconds=expires_seconds,
            )
        )
    except ProjectAccessDenied as exc:
        raise HTTPException(
            status_code=403,
            detail="project access denied",
        ) from exc
    except KnowledgeObjectNotFound as exc:
        raise HTTPException(
            status_code=404,
            detail="AI-rate screenshot was not found in the requested project.",
        ) from exc
    except ObjectStoreError as exc:
        raise HTTPException(
            status_code=503,
            detail="AI-rate screenshot download is temporarily unavailable.",
        ) from exc
    return ProjectAssetDownload(
        asset_id=asset_id,
        url=url,
        expires_seconds=expires_seconds,
    )


@router.post(
    "/{project}/tasks/{task_id}/prepare-images",
    response_model=TaskRecord,
)
def prepare_project_task_images(
    project: str,
    task_id: str,
    payload: PrepareProjectImagesRequest,
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
    if task.revision != payload.revision:
        raise HTTPException(
            status_code=409,
            detail=str(
                RevisionConflictError(
                    task.id,
                    payload.revision,
                    task.revision,
                )
            ),
        )
    try:
        ensure_action_allowed(task, ACTION_PREPARE_IMAGES)
    except WorkflowActionNotAllowed as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
        ) from exc
    try:
        ServerArticleImagePreparation(
            _knowledge_object_service(request)
        ).prepare(
            actor=authorized.actor,
            project_id=authorized.project_id,
            task=task,
            hero_asset_id=payload.hero_asset_id,
            product_anchors=payload.product_anchors,
        )
    except ServerArticleImageAnchorRequired as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": str(exc),
                "unresolved": list(exc.unresolved),
            },
        ) from exc
    except ProjectAccessDenied as exc:
        raise HTTPException(
            status_code=403,
            detail="project access denied",
        ) from exc
    except KnowledgeObjectNotFound as exc:
        raise HTTPException(
            status_code=409,
            detail="A selected image is no longer available.",
        ) from exc
    except ObjectTooLarge as exc:
        raise HTTPException(
            status_code=422,
            detail="A selected image exceeds the processing limit.",
        ) from exc
    except ServerArticleImageError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ObjectStoreError as exc:
        raise HTTPException(
            status_code=503,
            detail="Private image processing is temporarily unavailable.",
        ) from exc
    return _save_audited_task(
        request,
        authorized,
        task,
        expected_revision=payload.revision,
        action="article.images.prepared",
        details={"image_count": len(task.images)},
    )


@router.post(
    "/{project}/tasks/{task_id}/export-docx",
    response_model=TaskRecord,
)
def export_project_task_docx(
    project: str,
    task_id: str,
    payload: ProjectRevisionRequest,
    request: Request,
    authorized: AuthorizedProjectRequest = Depends(
        require_server_project_access
    ),
) -> TaskRecord:
    del project
    authorized = _require_project_permission(
        request,
        authorized,
        "article.deliver",
    )
    store = _task_store(request, authorized)
    try:
        task = store.get(task_id)
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail="Task was not found in the requested project.",
        ) from None
    if task.revision != payload.revision:
        raise HTTPException(
            status_code=409,
            detail=str(
                RevisionConflictError(
                    task.id,
                    payload.revision,
                    task.revision,
                )
            ),
        )
    try:
        ensure_action_allowed(task, ACTION_EXPORT_DOCX)
    except WorkflowActionNotAllowed as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    try:
        ServerArticleDocxExport(
            config=_server_app_config(request),
            objects=_knowledge_object_service(request),
        ).export(
            actor=authorized.actor,
            project_id=authorized.project_id,
            task=task,
        )
    except ProjectAccessDenied as exc:
        raise HTTPException(
            status_code=403,
            detail="project access denied",
        ) from exc
    except KnowledgeObjectNotFound as exc:
        raise HTTPException(
            status_code=409,
            detail="A prepared article image is no longer available.",
        ) from exc
    except ObjectTooLarge as exc:
        raise HTTPException(
            status_code=422,
            detail="A prepared article image exceeds the delivery limit.",
        ) from exc
    except ServerArticleDocxError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ObjectStoreError as exc:
        raise HTTPException(
            status_code=503,
            detail="Word export is temporarily unavailable.",
        ) from exc
    return _save_audited_task(
        request,
        authorized,
        task,
        expected_revision=payload.revision,
        action="article.docx.exported",
        details={"image_count": len(task.images)},
    )


@router.get(
    "/{project}/tasks/{task_id}/docx/download",
    response_model=ProjectAssetDownload,
)
def create_project_task_docx_download(
    project: str,
    task_id: str,
    request: Request,
    expires_seconds: int = Query(default=300, ge=30, le=3600),
    authorized: AuthorizedProjectRequest = Depends(
        require_server_project_access
    ),
) -> ProjectAssetDownload:
    del project
    authorized = _require_project_permission(
        request,
        authorized,
        "article.deliver",
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
        ensure_action_allowed(task, ACTION_DOWNLOAD_DOCX)
    except WorkflowActionNotAllowed as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    asset_id = task.docx_asset_id.strip()
    if not asset_id:
        raise HTTPException(
            status_code=409,
            detail="The Server Word document has not been exported.",
        )
    try:
        url = (
            _knowledge_object_service(
                request
            ).create_article_docx_download_url(
                actor=authorized.actor,
                project_id=authorized.project_id,
                asset_id=asset_id,
                expires_seconds=expires_seconds,
            )
        )
    except ProjectAccessDenied as exc:
        raise HTTPException(
            status_code=403,
            detail="project access denied",
        ) from exc
    except KnowledgeObjectNotFound as exc:
        raise HTTPException(
            status_code=404,
            detail="Word document was not found in the requested project.",
        ) from exc
    except ObjectStoreError as exc:
        raise HTTPException(
            status_code=503,
            detail="Word download is temporarily unavailable.",
        ) from exc
    return ProjectAssetDownload(
        asset_id=asset_id,
        url=url,
        expires_seconds=expires_seconds,
    )


@router.post(
    "/{project}/tasks/{task_id}/generate-tdk",
    response_model=TaskRecord,
)
def generate_project_task_tdk(
    project: str,
    task_id: str,
    payload: ProjectRevisionRequest,
    request: Request,
    authorized: AuthorizedProjectRequest = Depends(
        require_server_project_access
    ),
) -> TaskRecord:
    del project
    authorized = _require_project_permission(
        request,
        authorized,
        "article.deliver",
    )
    store = _task_store(request, authorized)
    try:
        task = store.get(task_id)
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail="Task was not found in the requested project.",
        ) from None
    if task.revision != payload.revision:
        raise HTTPException(
            status_code=409,
            detail=str(
                RevisionConflictError(
                    task.id,
                    payload.revision,
                    task.revision,
                )
            ),
        )
    try:
        ensure_action_allowed(task, ACTION_GENERATE_TDK)
    except WorkflowActionNotAllowed as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    try:
        ServerTdkDocxExport(
            config=_server_app_config(request),
            objects=_knowledge_object_service(request),
        ).generate(
            actor=authorized.actor,
            project_id=authorized.project_id,
            task=task,
        )
    except ProjectAccessDenied as exc:
        raise HTTPException(
            status_code=403,
            detail="project access denied",
        ) from exc
    except ServerTdkError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (ServerTdkUnavailable, ObjectStoreError) as exc:
        raise HTTPException(
            status_code=503,
            detail="TDK generation is temporarily unavailable.",
        ) from exc
    return _save_audited_task(
        request,
        authorized,
        task,
        expected_revision=payload.revision,
        action="article.tdk.generated",
        details={
            "description_characters": (
                task.tdk.description_character_count
            ),
            "keyword_count": len(task.tdk.keywords),
        },
    )


@router.get(
    "/{project}/tasks/{task_id}/tdk/download",
    response_model=ProjectAssetDownload,
)
def create_project_task_tdk_download(
    project: str,
    task_id: str,
    request: Request,
    expires_seconds: int = Query(default=300, ge=30, le=3600),
    authorized: AuthorizedProjectRequest = Depends(
        require_server_project_access
    ),
) -> ProjectAssetDownload:
    del project
    authorized = _require_project_permission(
        request,
        authorized,
        "article.deliver",
    )
    store = _task_store(request, authorized)
    try:
        task = store.get(task_id)
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail="Task was not found in the requested project.",
        ) from None
    asset_id = task.tdk_asset_id.strip()
    if not asset_id:
        raise HTTPException(
            status_code=409,
            detail="The Server TDK document has not been generated.",
        )
    try:
        url = (
            _knowledge_object_service(
                request
            ).create_tdk_docx_download_url(
                actor=authorized.actor,
                project_id=authorized.project_id,
                asset_id=asset_id,
                expires_seconds=expires_seconds,
            )
        )
    except ProjectAccessDenied as exc:
        raise HTTPException(
            status_code=403,
            detail="project access denied",
        ) from exc
    except KnowledgeObjectNotFound as exc:
        raise HTTPException(
            status_code=404,
            detail="TDK document was not found in the requested project.",
        ) from exc
    except ObjectStoreError as exc:
        raise HTTPException(
            status_code=503,
            detail="TDK download is temporarily unavailable.",
        ) from exc
    return ProjectAssetDownload(
        asset_id=asset_id,
        url=url,
        expires_seconds=expires_seconds,
    )


@router.post(
    "/{project}/tasks/{task_id}/package-delivery",
    response_model=TaskRecord,
)
def package_project_task_delivery(
    project: str,
    task_id: str,
    payload: ProjectRevisionRequest,
    request: Request,
    authorized: AuthorizedProjectRequest = Depends(
        require_server_project_access
    ),
) -> TaskRecord:
    del project
    authorized = _require_project_permission(
        request,
        authorized,
        "article.deliver",
    )
    store = _task_store(request, authorized)
    try:
        task = store.get(task_id)
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail="Task was not found in the requested project.",
        ) from None
    if task.revision != payload.revision:
        raise HTTPException(
            status_code=409,
            detail=str(
                RevisionConflictError(
                    task.id,
                    payload.revision,
                    task.revision,
                )
            ),
        )
    try:
        ensure_action_allowed(task, ACTION_PACKAGE_DELIVERY)
    except WorkflowActionNotAllowed as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    try:
        ServerDeliveryPackage(
            objects=_knowledge_object_service(request),
        ).package(
            actor=authorized.actor,
            project_id=authorized.project_id,
            task=task,
        )
    except ProjectAccessDenied as exc:
        raise HTTPException(
            status_code=403,
            detail="project access denied",
        ) from exc
    except KnowledgeObjectNotFound as exc:
        raise HTTPException(
            status_code=409,
            detail="A required delivery asset is no longer available.",
        ) from exc
    except ObjectTooLarge as exc:
        raise HTTPException(
            status_code=422,
            detail="A required delivery asset exceeds its size limit.",
        ) from exc
    except ServerDeliveryPackageError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ObjectStoreError as exc:
        raise HTTPException(
            status_code=503,
            detail="Delivery packaging is temporarily unavailable.",
        ) from exc
    return _save_audited_task(
        request,
        authorized,
        task,
        expected_revision=payload.revision,
        action="article.delivery.packaged",
        details={
            "file_count": len(task.images) + 3,
            "image_count": len(task.images),
        },
    )


@router.get(
    "/{project}/tasks/{task_id}/delivery-package/download",
    response_model=ProjectAssetDownload,
)
def create_project_task_delivery_download(
    project: str,
    task_id: str,
    request: Request,
    expires_seconds: int = Query(default=300, ge=30, le=3600),
    authorized: AuthorizedProjectRequest = Depends(
        require_server_project_access
    ),
) -> ProjectAssetDownload:
    del project
    authorized = _require_project_permission(
        request,
        authorized,
        "article.deliver",
    )
    store = _task_store(request, authorized)
    try:
        task = store.get(task_id)
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail="Task was not found in the requested project.",
        ) from None
    asset_id = task.delivery_package_asset_id.strip()
    if not asset_id:
        raise HTTPException(
            status_code=409,
            detail="The Server delivery package has not been generated.",
        )
    try:
        url = (
            _knowledge_object_service(
                request
            ).create_delivery_zip_download_url(
                actor=authorized.actor,
                project_id=authorized.project_id,
                asset_id=asset_id,
                content_hash=task.delivery_package_content_hash,
                expires_seconds=expires_seconds,
            )
        )
    except ProjectAccessDenied as exc:
        raise HTTPException(
            status_code=403,
            detail="project access denied",
        ) from exc
    except KnowledgeObjectNotFound as exc:
        raise HTTPException(
            status_code=404,
            detail="Delivery package was not found in the requested project.",
        ) from exc
    except ObjectStoreError as exc:
        raise HTTPException(
            status_code=503,
            detail="Delivery download is temporarily unavailable.",
        ) from exc
    return ProjectAssetDownload(
        asset_id=asset_id,
        url=url,
        expires_seconds=expires_seconds,
    )


@router.put(
    "/{project}/tasks/{task_id}/article/sections",
    response_model=TaskRecord,
)
def rewrite_project_task_article_section(
    project: str,
    task_id: str,
    payload: ArticleSectionRewriteRequest,
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
        ensure_action_allowed(task, ACTION_UPDATE_ARTICLE)
    except WorkflowActionNotAllowed as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
        ) from exc
    try:
        rewrite_initial_article_section(
            task,
            heading_path=payload.heading_path,
            replacement_body=payload.replacement_body,
        )
    except SectionRewriteError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _save_audited_task(
        request,
        authorized,
        task,
        expected_revision=payload.revision,
        action="article.section.replaced",
        details={"heading_depth": len(payload.heading_path)},
    )


@router.post(
    "/{project}/tasks/{task_id}/product-rediscovery",
    response_model=ProductRediscoveryJobResponse,
)
def enqueue_project_product_rediscovery(
    project: str,
    task_id: str,
    payload: ProductRediscoveryRequest,
    request: Request,
    authorized: AuthorizedProjectRequest = Depends(
        require_server_project_access
    ),
) -> ProductRediscoveryJobResponse:
    del project
    authorized = _require_project_permission(
        request,
        authorized,
        "knowledge.edit",
    )
    try:
        job = _product_rediscovery(request).enqueue(
            actor=authorized.actor,
            project_id=authorized.project_id,
            task_id=task_id,
            source_revision=payload.revision,
            command=ProductRediscoveryCommand(
                category_url=payload.category_url.strip(),
                max_products=payload.max_products,
            ),
        )
    except ProjectAccessDenied as exc:
        raise HTTPException(
            status_code=403,
            detail="project access denied",
        ) from exc
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail="Task was not found in the requested project.",
        ) from None
    except (ActiveJobError, JobConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ProductRediscoveryUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="Server product rediscovery is not available.",
        ) from exc
    return ProductRediscoveryJobResponse.model_validate(job)


@router.get(
    "/{project}/tasks/{task_id}/product-rediscovery/jobs/{job_id}",
    response_model=ProductRediscoveryJobResponse,
)
def read_project_product_rediscovery_job(
    project: str,
    task_id: str,
    job_id: str,
    request: Request,
    authorized: AuthorizedProjectRequest = Depends(
        require_server_project_access
    ),
) -> ProductRediscoveryJobResponse:
    del project
    try:
        job = _product_rediscovery(request).get_job(
            actor=authorized.actor,
            project_id=authorized.project_id,
            task_id=task_id,
            job_id=job_id,
        )
    except ProjectAccessDenied as exc:
        raise HTTPException(
            status_code=403,
            detail="project access denied",
        ) from exc
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail="Product rediscovery job was not found.",
        ) from None
    except ProductRediscoveryUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="Server product rediscovery is not available.",
        ) from exc
    return ProductRediscoveryJobResponse.model_validate(job)


__all__ = [
    "ArticleSectionRewriteRequest",
    "ConfirmedProductsUpdateRequest",
    "ProductRediscoveryJobResponse",
    "ProductRediscoveryRequest",
    "ProjectAssetDownload",
    "require_server_actor",
    "require_server_project_access",
    "router",
]
