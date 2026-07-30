from __future__ import annotations

from collections import Counter
from contextlib import asynccontextmanager, contextmanager
from dataclasses import replace
from io import BytesIO
import os
from pathlib import Path
import re
from threading import Lock
from typing import Callable
from urllib import parse
from uuid import uuid4

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from dotenv import load_dotenv
from PIL import Image, UnidentifiedImageError

from config import ROOT_DIR, load_config, public_config
from models import (
    AICheck,
    AICheckUpdateRequest,
    ApiMessage,
    AuthLoginRequest,
    ArticleImage,
    ArticleUpdateRequest,
    ArticleVersion,
    AutoProductsRequest,
    BatchCreateRequest,
    BatchCreateResponse,
    BatchJobRecord,
    BatchPreflightIssue,
    BatchRecord,
    DashboardSummary,
    GenerateArticleRequest,
    GenerateOutlineRequest,
    ImagesUpdateRequest,
    LinkValidation,
    LlmSettingsUpdateRequest,
    ManualTitleGenerationRequest,
    OutlineUpdateRequest,
    ProductsUpdateRequest,
    ProjectPromptLibrary,
    PromptActiveUpdateRequest,
    PromptCreateRequest,
    PromptDefaults,
    PromptDefaultsUpdateRequest,
    PromptLibraryItem,
    PromptPreview,
    PromptPreviewRequest,
    PromptSnapshot,
    PromptUpdateRequest,
    ProjectBrandUpdateRequest,
    ProjectContextUpdateRequest,
    ProjectDomainUpdateRequest,
    RevisionedRequest,
    SelectTitleRequest,
    SeoReviewRequest,
    SeoReviewChangeUpdateRequest,
    SeoReviewFinalizeRequest,
    SeoReviewPreview,
    SeoReviewPreviewRequest,
    SeoReviewRun,
    SeoReviewSettingsUpdateRequest,
    SourceLink,
    STATUS_DOCX_EXPORTED,
    STATUS_DRAFT_READY,
    STATUS_FINAL_AI_CHECKED,
    STATUS_HUMANIZED_READY,
    STATUS_IMAGES_READY,
    STATUS_INITIAL_AI_CHECKED,
    STATUS_LINKS_VERIFIED,
    STATUS_OUTLINE_CONFIRMED,
    STATUS_OUTLINE_READY,
    STATUS_TITLE_SELECTED,
    STATUS_TITLES_READY,
    TaskRecord,
    VersionRestoreRequest,
    WorkflowError,
    WritingSettingsUpdateRequest,
    ZeroGptReportRequest,
)
from services.article_images import (
    ArticleImageError,
    ImageAnchorRequiredError,
    build_image_audit_markdown,
    prepare_task_images,
    resolve_image_placements,
    sanitize_image_stem,
)
from services.article_validation import (
    ArticleStructureError,
    LinkRestorationError,
    extract_link_inventory,
    has_intro_transition,
    validate_article_layout,
    validate_humanized_article,
    visible_word_count,
)
from services.ai_screenshots import AIScreenshotError, save_ai_rate_screenshot
from services.delivery_package import DeliveryPackageError, build_delivery_zip, package_delivery
from services.docx_export import export_task_docx
from services.generator import (
    ArticleGenerationError,
    PromptTemplateError,
    build_article_prompt,
    build_outline_prompt,
    build_humanize_prompt,
    ensure_transition_before_first_h2,
    ensure_article_hyperlinks,
    generate_outline,
    generate_raw_article,
    generate_titles,
    humanize_article,
    restore_article_links,
    site_homepage,
    validate_minimum_h3_per_h2,
)
from services.llm import LLMClient
from services.project_prompts import (
    ProjectPromptRepository,
    PromptInUseError,
    PromptLibraryError,
)
from services.auth import (
    AUTH_COOKIE_NAME,
    DEFAULT_SESSION_SECONDS,
    authentication_enabled,
    create_session_token,
    password_matches,
    valid_session_token,
)
from services.llm_settings import LlmSettingsRepository
from services.seo_review import (
    SeoReviewError,
    build_review_candidate,
    build_seo_review_prompt,
    effective_review_prompt_snapshot,
    generate_seo_review,
    normalized_keywords,
    update_review_change,
)
from services.job_queue import (
    ActiveJobError,
    BatchJobRunner,
    JobCancelled,
    JobConflict,
    JobQueue,
)
from services.product_asset_pipeline import enrich_product_assets
from services.product_crawler import recommend_products
from services.tavily import TavilyClient
from services.tdk import TdkGenerationError, export_tdk_docx, generate_tdk_metadata
from services.topics import TopicWorkbookError, scan_topic_library, store_topic_workbook
from services.task_identity import article_source_key, normalized_customer
from storage import (
    RevisionConflictError,
    TaskStore,
    content_hash,
    now_iso,
    write_json_artifact,
    write_text_artifact,
)
from workflow.state_machine import (
    ACTION_CONFIRM_FINAL_AI,
    ACTION_CONFIRM_INITIAL_AI,
    ACTION_EXPORT_DOCX,
    ACTION_GENERATE_ARTICLE,
    ACTION_GENERATE_OUTLINE,
    ACTION_GENERATE_TITLES,
    ACTION_GENERATE_TDK,
    ACTION_HUMANIZE_ARTICLE,
    ACTION_PREPARE_IMAGES,
    ACTION_PACKAGE_DELIVERY,
    ACTION_RESTORE_LINKS,
    ACTION_REWRITE_FROM_SCRATCH,
    ACTION_SELECT_TITLE,
    ACTION_UPDATE_ARTICLE,
    ACTION_UPDATE_IMAGES,
    ACTION_UPDATE_HUMANIZED,
    ACTION_UPDATE_OUTLINE,
    ACTION_UPDATE_PRODUCTS,
    InvalidWorkflowTransition,
    WorkflowActionNotAllowed,
    allowed_actions,
    ensure_action_allowed,
    invalidate_downstream,
    reset_for_full_rewrite,
    transition_task,
)
from knowledge_agent.http import router as knowledge_agent_router
from knowledge_agent.embedding import OpenAICompatibleEmbeddingProvider
from knowledge_agent.runtime import create_knowledge_runtime
from knowledge_agent.settings import load_knowledge_agent_settings


load_dotenv(ROOT_DIR / ".env")
load_dotenv(ROOT_DIR / "backend" / ".env")


@asynccontextmanager
async def app_lifespan(application: FastAPI):
    cfg = config()
    knowledge_runtime = None
    application.state.knowledge_agent_runtime = None
    if cfg.knowledge_agent_enabled:
        knowledge_settings = load_knowledge_agent_settings(enabled=True)
        knowledge_runtime = create_knowledge_runtime(
            database_url=knowledge_settings.database_url or "",
            artifact_root=Path(
                os.environ.get(
                    "ARTICLE_AGENT_KNOWLEDGE_ROOT",
                    str(cfg.data_file.parent / "knowledge-agent"),
                )
            ),
            embedding_provider=OpenAICompatibleEmbeddingProvider.from_settings(
                knowledge_settings
            ),
        )
        application.state.knowledge_agent_runtime = knowledge_runtime
    queue = JobQueue(cfg.data_file.with_name("job_queue.sqlite3"))
    writing_runner = BatchJobRunner(
        queue,
        _execute_batch_job,
        concurrency=3,
        operations=(
            "titles",
            "outline",
            "article",
            "rewrite_article",
            "seo_review",
            "humanize",
            "restore_links",
            "generate_tdk",
        ),
    )
    product_runner = BatchJobRunner(
        queue,
        _execute_batch_job,
        concurrency=2,
        operations=(
            "products",
            "prepare_images",
            "export_docx",
            "package_delivery",
        ),
    )
    application.state.job_queue = queue
    application.state.batch_runner = writing_runner
    application.state.batch_runners = (writing_runner, product_runner)
    writing_runner.start()
    product_runner.start()
    try:
        yield
    finally:
        product_runner.stop()
        writing_runner.stop()
        if knowledge_runtime is not None:
            knowledge_runtime.close()
        application.state.knowledge_agent_runtime = None


app = FastAPI(
    title="Article Workflow Agent",
    version="0.3.0",
    lifespan=app_lifespan,
)
app.include_router(knowledge_agent_router)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_AUTH_PUBLIC_PATHS = {
    "/api/auth/login",
    "/api/auth/logout",
    "/api/auth/status",
    "/api/health",
}


@app.middleware("http")
async def require_application_password(request: Request, call_next):
    if (
        not authentication_enabled()
        or request.method == "OPTIONS"
        or request.url.path in _AUTH_PUBLIC_PATHS
    ):
        return await call_next(request)
    token = request.cookies.get(AUTH_COOKIE_NAME, "")
    if valid_session_token(token):
        return await call_next(request)

    response = JSONResponse(
        status_code=401,
        content={"detail": "Authentication required."},
    )
    origin = request.headers.get("origin", "")
    if origin in {"http://localhost:3000", "http://127.0.0.1:3000"}:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
    return response


def auth_cookie_secure(request: Request) -> bool:
    configured = os.environ.get("APP_COOKIE_SECURE", "").strip().lower()
    if configured in {"1", "true", "yes", "on"}:
        return True
    forwarded_proto = request.headers.get("x-forwarded-proto", "")
    return request.url.scheme == "https" or forwarded_proto.split(",", 1)[0].strip() == "https"


@app.get("/api/auth/status", response_model=ApiMessage)
def auth_status(request: Request) -> ApiMessage:
    enabled = authentication_enabled()
    authenticated = not enabled or valid_session_token(
        request.cookies.get(AUTH_COOKIE_NAME, "")
    )
    return ApiMessage(
        message="Authentication status.",
        data={"enabled": enabled, "authenticated": authenticated},
    )


@app.post("/api/auth/login", response_model=ApiMessage)
def auth_login(request: Request, payload: AuthLoginRequest):
    if not authentication_enabled():
        return ApiMessage(
            message="Password protection is not configured.",
            data={"enabled": False, "authenticated": True},
        )
    if not password_matches(payload.password):
        raise HTTPException(status_code=401, detail="密码错误。")
    response = JSONResponse(
        content={
            "message": "登录成功。",
            "data": {"enabled": True, "authenticated": True},
        }
    )
    response.set_cookie(
        key=AUTH_COOKIE_NAME,
        value=create_session_token(),
        max_age=DEFAULT_SESSION_SECONDS,
        httponly=True,
        secure=auth_cookie_secure(request),
        samesite="lax",
        path="/",
    )
    return response


@app.post("/api/auth/logout", response_model=ApiMessage)
def auth_logout() -> JSONResponse:
    response = JSONResponse(
        content={
            "message": "已退出登录。",
            "data": {
                "enabled": authentication_enabled(),
                "authenticated": False,
            },
        }
    )
    response.delete_cookie(AUTH_COOKIE_NAME, path="/")
    return response


MAX_UPLOAD_BYTES = 25 * 1024 * 1024
PRODUCT_IMAGE_SUFFIXES = {
    "JPEG": ".jpg",
    "PNG": ".png",
    "WEBP": ".webp",
}
_PRODUCT_PROCESSING_GUARD = Lock()
_PRODUCT_PROCESSING_TASKS: set[str] = set()


def available_with_current(
    current: str,
    configured: tuple[str, ...],
) -> tuple[str, ...]:
    if not current or current in configured:
        return configured
    return (current, *configured)


def config():
    base = load_config()
    environment_model = os.environ.get("LLM_MODEL", "").strip()
    environment_reasoning_effort = os.environ.get(
        "LLM_REASONING_EFFORT",
        "",
    ).strip()
    environment_base_url = os.environ.get("LLM_BASE_URL", "").strip()
    if environment_model or environment_reasoning_effort or environment_base_url:
        base = replace(
            base,
            llm_model=environment_model or base.llm_model,
            llm_reasoning_effort=(
                environment_reasoning_effort or base.llm_reasoning_effort
            ),
            llm_base_url=(environment_base_url or base.llm_base_url).rstrip("/"),
            llm_available_models=available_with_current(
                environment_model or base.llm_model,
                base.llm_available_models,
            ),
            llm_available_reasoning_efforts=available_with_current(
                environment_reasoning_effort or base.llm_reasoning_effort,
                base.llm_available_reasoning_efforts,
            ),
        )
    saved = LlmSettingsRepository(base.data_file).get()
    if saved is None:
        return base
    return replace(
        base,
        llm_model=saved.model,
        llm_reasoning_effort=saved.reasoning_effort,
        llm_available_models=available_with_current(
            saved.model,
            base.llm_available_models,
        ),
        llm_available_reasoning_efforts=available_with_current(
            saved.reasoning_effort,
            base.llm_available_reasoning_efforts,
        ),
        llm_runtime_override=True,
    )


def store() -> TaskStore:
    return TaskStore(config())


def prompt_store() -> ProjectPromptRepository:
    return ProjectPromptRepository(config().data_file)


def batch_queue() -> JobQueue:
    queue = getattr(app.state, "job_queue", None)
    expected_path = config().data_file.with_name("job_queue.sqlite3")
    if queue is None or queue.path != expected_path:
        queue = JobQueue(expected_path)
        app.state.job_queue = queue
    return queue


def wake_batch_runner() -> None:
    runners = getattr(app.state, "batch_runners", None)
    if runners is None:
        runner = getattr(app.state, "batch_runner", None)
        runners = (runner,) if runner is not None else ()
    for runner in runners:
        runner.wake()


def expose_task(task: TaskRecord) -> TaskRecord:
    # TaskRecord allows extension fields, so v2 clients receive derived actions
    # without persisting them in tasks.json.
    if task.workflow_error and task.workflow_error.code == "compression_failed":
        task.workflow_error = None
    task.allowed_actions = allowed_actions(task)
    issues = initial_readiness_issues(task)
    task.initial_article_ready = not issues
    task.initial_article_issues = issues
    return task


def get_task_or_404(task_id: str) -> TaskRecord:
    try:
        return store().get(task_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Task not found: {task_id}") from None


def _require_workflow_action(task: TaskRecord, action: str) -> None:
    try:
        ensure_action_allowed(task, action)
    except WorkflowActionNotAllowed as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def product_processing_active(task_id: str) -> bool:
    with _PRODUCT_PROCESSING_GUARD:
        return task_id in _PRODUCT_PROCESSING_TASKS


@contextmanager
def product_processing(task_id: str):
    with _PRODUCT_PROCESSING_GUARD:
        if task_id in _PRODUCT_PROCESSING_TASKS:
            raise HTTPException(
                status_code=409,
                detail="Product discovery or asset processing is already running for this task.",
            )
        _PRODUCT_PROCESSING_TASKS.add(task_id)
    try:
        yield
    finally:
        with _PRODUCT_PROCESSING_GUARD:
            _PRODUCT_PROCESSING_TASKS.discard(task_id)


def require_action(task: TaskRecord, action: str) -> None:
    if product_processing_active(task.id):
        raise HTTPException(
            status_code=409,
            detail="Product discovery or asset processing is running for this task.",
        )
    _require_workflow_action(task, action)


def save_task(task: TaskRecord, expected_revision: int | None = None) -> TaskRecord:
    try:
        saved = store().put(task, expected_revision=expected_revision)
    except RevisionConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return expose_task(saved)


def reset_product_enrichment(product):
    """Drop website-derived fields when the operator changes the visible URL."""

    return product.model_copy(
        update={
            "product_id": "",
            "canonical_url": "",
            "image_path": "",
            "reference_summary": "",
            "reference_facts": [],
            "specifications": {},
            "reference_path": "",
            "asset_manifest_path": "",
            "asset_count": 0,
            "selected_asset_id": "",
            "selection_confidence": None,
            "selection_reason": "",
            "discovery_source": "",
            "detail_page_verified": False,
            "asset_status": "",
            "asset_error": "",
        }
    )


def product_url_changed(previous, current) -> bool:
    return str(previous.url or "").strip() != str(current.url or "").strip()


def advance(task: TaskRecord, status: str) -> None:
    try:
        transition_task(task, status)
    except InvalidWorkflowTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def validate_score(score: float | None) -> None:
    if score is not None and not 0 <= score <= 100:
        raise HTTPException(status_code=422, detail="AI rate must be between 0 and 100.")


def version_record(kind: str, content: str, source_kind: str = "") -> ArticleVersion:
    return ArticleVersion(
        kind=kind,
        content=content,
        word_count=visible_word_count(content),
        content_hash=content_hash(content),
        created_at=now_iso(),
        source_kind=source_kind,
    )


def source_links(article: str) -> list[SourceLink]:
    return [SourceLink.model_validate(item) for item in extract_link_inventory(article)]


def initial_readiness_issues(task: TaskRecord) -> list[str]:
    article = task.initial_article or task.article
    if not article.strip():
        return ["第一版正文为空。"]
    issues: list[str] = []
    try:
        if not has_intro_transition(article):
            issues.append("H1 与第一个 H2 之间缺少过渡段，请先保存第一版自动补齐。")
        validate_article_layout(article)
    except ArticleStructureError as exc:
        issues.append(str(exc))
    homepage = site_homepage(task.customer).rstrip("/")
    homepage_links = [
        link
        for link in extract_link_inventory(article)
        if str(link.get("url") or "").rstrip("/") == homepage
    ]
    if homepage and not homepage_links:
        issues.append(
            "第一版未包含客户官网的 Markdown 超链接，后续无法按第一版恢复链接。"
        )
    brand_name = " ".join(task.brand_name.split())
    if homepage_links and brand_name and not any(
        str(link.get("anchor") or "").strip() == brand_name
        for link in homepage_links
    ):
        issues.append(
            f"客户官网超链接必须附在准确品牌名“{brand_name}”上，请重新保存第一版。"
        )
    return issues


def ensure_initial_metadata(task: TaskRecord) -> None:
    """Fill v2 metadata lazily for legacy draft_ready tasks."""

    if not task.initial_article:
        task.initial_article = task.article
    if task.initial_article:
        task.initial_article_word_count = visible_word_count(task.initial_article)
        task.initial_article_hash = content_hash(task.initial_article)
        if not task.source_links:
            task.source_links = source_links(task.initial_article)


def set_stage_error(
    task: TaskRecord,
    *,
    code: str,
    message: str,
    stage: str,
    recoverable: bool = True,
) -> None:
    task.workflow_error = WorkflowError(
        code=code,
        message=message,
        stage=stage,
        occurred_at=now_iso(),
        recoverable=recoverable,
        blocking=True,
    )


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/config")
def read_config() -> dict:
    return public_config(config())


@app.put("/api/settings/llm")
def update_llm_settings(request: LlmSettingsUpdateRequest) -> dict:
    base = load_config()
    model = request.model.strip()
    reasoning_effort = request.reasoning_effort.strip()
    if model not in base.llm_available_models:
        raise HTTPException(
            status_code=422,
            detail="The selected model is not available.",
        )
    if reasoning_effort not in base.llm_available_reasoning_efforts:
        raise HTTPException(
            status_code=422,
            detail="The selected reasoning effort is not available.",
        )
    LlmSettingsRepository(base.data_file).save(
        model=model,
        reasoning_effort=reasoning_effort,
    )
    return public_config(config())


@app.get("/api/dashboard", response_model=DashboardSummary)
def dashboard() -> DashboardSummary:
    cfg = config()
    tasks = store().canonical_tasks(cfg.current_week_folder)
    status_counts = dict(Counter(task.status for task in tasks))
    return DashboardSummary(
        week_folder=cfg.current_week_folder,
        week_path=str(cfg.current_week_path),
        customer_count=len({task.customer for task in tasks}),
        task_count=len(tasks),
        completed_count=sum(1 for task in tasks if task.status == STATUS_DOCX_EXPORTED),
        status_counts=status_counts,
        llm_ready=LLMClient(cfg).ready,
    )


@app.post("/api/sync-tasks", response_model=ApiMessage)
@app.post("/api/init-week", response_model=ApiMessage, deprecated=True)
def sync_tasks() -> ApiMessage:
    cfg = config()
    scanned = scan_topic_library(cfg)
    store().upsert_many(scanned)
    task_count = len(store().canonical_tasks(cfg.current_week_folder))
    return ApiMessage(
        message=f"已同步 {task_count} 个长期任务。",
        data={"week_folder": cfg.current_week_folder, "task_count": task_count},
    )


@app.post("/api/topic-files/upload", response_model=ApiMessage)
def upload_topic_files(files: list[UploadFile] = File(...)) -> ApiMessage:
    if not files:
        raise HTTPException(status_code=422, detail="请选择至少一个 XLSX 文件。")

    uploads: list[tuple[str, bytes]] = []
    for file in files:
        content = file.file.read(MAX_UPLOAD_BYTES + 1)
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"{file.filename or 'XLSX 文件'} 超过 25 MB。",
            )
        uploads.append((file.filename or "", content))

    cfg = config()
    saved_names: list[str] = []
    try:
        for filename, content in uploads:
            saved_names.append(store_topic_workbook(cfg, filename, content).name)
    except TopicWorkbookError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    scanned = scan_topic_library(cfg)
    store().upsert_many(scanned)
    task_count = len(store().canonical_tasks(cfg.current_week_folder))
    return ApiMessage(
        message=f"已上传 {len(saved_names)} 个 XLSX 文件，并同步 {task_count} 个长期任务。",
        data={"files": saved_names, "task_count": task_count},
    )


@app.get("/api/tasks", response_model=list[TaskRecord])
def list_tasks(customer: str | None = None, status: str | None = None) -> list[TaskRecord]:
    cfg = config()
    tasks = store().canonical_tasks(cfg.current_week_folder)
    if customer:
        tasks = [task for task in tasks if task.customer == customer]
    if status:
        tasks = [task for task in tasks if task.status == status]
    return [expose_task(task) for task in sorted(tasks, key=lambda task: (task.customer, task.topic_index))]


@app.get("/api/tasks/{task_id}", response_model=TaskRecord)
def read_task(task_id: str) -> TaskRecord:
    return expose_task(get_task_or_404(task_id))


def append_version(task: TaskRecord, kind: str, content: str, source_kind: str = "") -> None:
    """Append a changed snapshot without duplicating the latest entry."""

    record = version_record(kind, content, source_kind)
    if task.article_versions:
        latest = task.article_versions[-1]
        if (
            latest.kind == record.kind
            and latest.content_hash == record.content_hash
            and latest.source_kind == record.source_kind
        ):
            return
    task.article_versions.append(record)


@app.put("/api/projects/{customer}/brand", response_model=ApiMessage)
def update_project_brand(
    customer: str,
    request: ProjectBrandUpdateRequest,
) -> ApiMessage:
    brand_name = " ".join(request.brand_name.split())
    if any(character in brand_name for character in "[]"):
        raise HTTPException(
            status_code=422,
            detail="Brand name cannot contain Markdown square brackets.",
        )
    try:
        updated_tasks = store().update_customer_brand(customer, brand_name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Project not found: {customer}") from None
    return ApiMessage(
        message=(
            f"已保存品牌名：{brand_name}。" if brand_name else "已清空品牌名，将使用官网域名作为默认名称。"
        ),
        data={
            "customer": customer,
            "brand_name": brand_name,
            "updated_tasks": updated_tasks,
        },
    )


@app.put("/api/projects/{customer}/context", response_model=ApiMessage)
def update_project_context(
    customer: str,
    request: ProjectContextUpdateRequest,
) -> ApiMessage:
    try:
        updated_tasks = store().update_customer_context(
            customer,
            request.project_introduction,
            request.project_notes,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Project not found: {customer}") from None
    return ApiMessage(
        message="项目介绍和项目注意事项已保存。",
        data={
            "customer": customer,
            "project_introduction": request.project_introduction.strip(),
            "project_notes": request.project_notes.strip(),
            "updated_tasks": updated_tasks,
        },
    )


def require_project(customer: str) -> None:
    normalized = customer.strip().lower().rstrip("/")
    if not any(
        task.customer.strip().lower().rstrip("/") == normalized
        for task in store().load()
    ):
        raise HTTPException(status_code=404, detail=f"Project not found: {customer}")


def normalized_project_domain(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("Project domain is required.")
    parsed = parse.urlparse(raw if "://" in raw else f"https://{raw}")
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Project domain must use HTTP or HTTPS.")
    if parsed.username or parsed.password:
        raise ValueError("Project domain cannot contain credentials.")
    try:
        if parsed.port is not None:
            raise ValueError("Project domain cannot contain a port.")
    except ValueError as exc:
        raise ValueError("Project domain contains an invalid port.") from exc
    if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        raise ValueError("Enter only the domain, without a path, query, or fragment.")
    hostname = (parsed.hostname or "").strip(".")
    if not hostname:
        raise ValueError("Project domain is invalid.")
    try:
        domain = hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("Project domain contains invalid characters.") from exc
    if len(domain) > 253 or "." not in domain:
        raise ValueError("Project domain must be a complete website domain.")
    labels = domain.split(".")
    if any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        or re.fullmatch(r"[a-z0-9-]+", label) is None
        for label in labels
    ):
        raise ValueError("Project domain contains an invalid domain label.")
    return domain


def project_rename_moves(
    cfg,
    customer: str,
    new_customer: str,
    tasks: list[TaskRecord],
) -> list[tuple[Path, Path]]:
    """Resolve project-owned directories/files before mutating any state."""

    candidates: list[tuple[Path, Path]] = []
    output_root = cfg.output_root.resolve()
    project_roots: set[Path] = set()
    for task in tasks:
        task_path = Path(task.task_dir).resolve()
        try:
            relative = task_path.relative_to(output_root)
        except ValueError:
            continue
        if len(relative.parts) >= 2:
            project_roots.add(output_root / relative.parts[0])
    if len(project_roots) > 1:
        raise ValueError("Project tasks span multiple output directories; migration was stopped.")
    for project_root in project_roots:
        candidates.append((project_root, output_root / new_customer))

    workbooks = [
        workbook.resolve()
        for workbook in cfg.topic_library.glob("*.xlsx")
        if normalized_customer(workbook.stem) == normalized_customer(customer)
    ]
    if len(workbooks) > 1:
        raise ValueError("Multiple topic workbooks match this project; migration was stopped.")
    if workbooks:
        candidates.append(
            (workbooks[0], cfg.topic_library.resolve() / f"{new_customer}.xlsx")
        )

    if cfg.knowledge_base.exists():
        knowledge_directories = [
            directory.resolve()
            for directory in cfg.knowledge_base.iterdir()
            if directory.is_dir()
            and normalized_customer(directory.name) == normalized_customer(customer)
        ]
        if len(knowledge_directories) > 1:
            raise ValueError("Multiple knowledge folders match this project; migration was stopped.")
        if knowledge_directories:
            candidates.append(
                (knowledge_directories[0], cfg.knowledge_base.resolve() / new_customer)
            )

    moves: list[tuple[Path, Path]] = []
    for source, destination in candidates:
        if not source.exists():
            continue
        if os.path.normcase(str(source)) == os.path.normcase(str(destination)):
            continue
        if destination.exists():
            raise FileExistsError(f"Target path already exists: {destination}")
        moves.append((source, destination))
    return moves


@app.put("/api/projects/{customer}/domain", response_model=ApiMessage)
def update_project_domain(
    customer: str,
    request: ProjectDomainUpdateRequest,
) -> ApiMessage:
    try:
        new_customer = normalized_project_domain(request.new_domain)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    task_store = store()
    tasks = [
        task
        for task in task_store.load()
        if normalized_customer(task.customer) == normalized_customer(customer)
    ]
    if not tasks:
        raise HTTPException(status_code=404, detail=f"Project not found: {customer}")
    if normalized_customer(customer) != normalized_customer(new_customer) and any(
        normalized_customer(task.customer) == normalized_customer(new_customer)
        for task in task_store.load()
        if task.id not in {project_task.id for project_task in tasks}
    ):
        raise HTTPException(status_code=409, detail=f"Project already exists: {new_customer}")
    active = batch_queue().active_task_ids(task.id for task in tasks)
    if active:
        raise HTTPException(
            status_code=409,
            detail="Wait for or cancel the project's active jobs before changing its domain.",
        )

    cfg = config()
    try:
        moves = project_rename_moves(cfg, customer, new_customer, tasks)
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    moved: list[tuple[Path, Path]] = []
    renamed_tasks: list[TaskRecord] = []
    id_mapping: dict[str, str] = {}
    prompts_renamed = False
    queue_renamed = False
    try:
        for source, destination in moves:
            destination.parent.mkdir(parents=True, exist_ok=True)
            source.replace(destination)
            moved.append((destination, source))

        renamed_tasks, id_mapping = task_store.rename_customer(
            customer,
            new_customer,
            path_replacements=moves,
        )
        prompt_store().rename_customer(customer, new_customer)
        prompts_renamed = True
        batch_queue().rename_customer(customer, new_customer, id_mapping)
        queue_renamed = True
    except Exception as exc:
        if queue_renamed:
            batch_queue().rename_customer(
                new_customer,
                customer,
                {new_id: old_id for old_id, new_id in id_mapping.items()},
            )
        if prompts_renamed:
            prompt_store().rename_customer(new_customer, customer)
        if renamed_tasks:
            task_store.rename_customer(
                new_customer,
                customer,
                path_replacements=[
                    (destination, source)
                    for source, destination in moves
                ],
            )
        for source, destination in reversed(moved):
            if source.exists() and not destination.exists():
                source.replace(destination)
        status_code = 409 if isinstance(exc, (FileExistsError, PromptLibraryError)) else 500
        raise HTTPException(
            status_code=status_code,
            detail=f"Project domain could not be changed: {exc}",
        ) from exc

    return ApiMessage(
        message=(
            f"项目域名已从 {customer} 更新为 {new_customer}。"
            "任务、提示词、批次和项目目录已迁移；已生成正文中的旧链接未自动改写。"
        ),
        data={
            "old_domain": customer,
            "new_domain": new_customer,
            "updated_tasks": len(renamed_tasks),
            "task_id_mapping": id_mapping,
            "moved_paths": [str(destination) for _, destination in moves],
        },
    )


def archive_project_sources(cfg, customer: str, tasks: list[TaskRecord]) -> list[str]:
    """Move project-owned source/output paths into a recoverable local trash folder."""

    trash_root = cfg.output_root.parent / "project-trash" / uuid4().hex
    archived: list[str] = []
    candidates: list[tuple[Path, Path]] = []
    output_root = cfg.output_root.resolve()
    for task in tasks:
        task_path = Path(task.task_dir).resolve()
        try:
            relative = task_path.relative_to(output_root)
        except ValueError:
            continue
        if len(relative.parts) >= 2:
            project_root = output_root / relative.parts[0]
            candidates.append((project_root, trash_root / "output" / project_root.name))

    topic_root = cfg.topic_library.resolve()
    for workbook in cfg.topic_library.glob("*.xlsx"):
        if normalized_customer(workbook.stem) == normalized_customer(customer):
            candidates.append((workbook.resolve(), trash_root / "topic-library" / workbook.name))

    seen: set[Path] = set()
    for source, destination in candidates:
        if source in seen or not source.exists():
            continue
        seen.add(source)
        if source != topic_root:
            destination.parent.mkdir(parents=True, exist_ok=True)
            source.replace(destination)
            archived.append(str(destination))
    return archived


@app.delete("/api/projects/{customer}", response_model=ApiMessage)
def delete_project(customer: str, confirmation: str) -> ApiMessage:
    if confirmation.strip() != customer:
        raise HTTPException(status_code=422, detail="Type the exact project domain to confirm deletion.")
    task_store = store()
    tasks = [
        task
        for task in task_store.load()
        if normalized_customer(task.customer) == normalized_customer(customer)
    ]
    if not tasks:
        raise HTTPException(status_code=404, detail=f"Project not found: {customer}")
    active = batch_queue().active_task_ids(task.id for task in tasks)
    if active:
        raise HTTPException(status_code=409, detail="Stop the project's active jobs before deleting it.")

    cfg = config()
    try:
        archived = archive_project_sources(cfg, customer, tasks)
        batch_queue().delete_customer(customer)
        prompt_store().delete_customer(customer)
        deleted = task_store.delete_customer(customer)
    except ActiveJobError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Project files could not be archived: {exc}") from exc
    return ApiMessage(
        message=f"Project deleted. {len(deleted)} article tasks were removed.",
        data={"customer": customer, "deleted_tasks": len(deleted), "archived_paths": archived},
    )


@app.get("/api/projects/{customer}/prompts", response_model=ProjectPromptLibrary)
def list_project_prompts(customer: str) -> ProjectPromptLibrary:
    require_project(customer)
    return prompt_store().list(customer)


@app.post("/api/projects/{customer}/prompts", response_model=PromptLibraryItem)
def create_project_prompt(customer: str, request: PromptCreateRequest) -> PromptLibraryItem:
    require_project(customer)
    try:
        return prompt_store().create(customer, request.name, request.kind, request.content)
    except PromptLibraryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.put("/api/projects/{customer}/prompts/{prompt_id}", response_model=PromptLibraryItem)
def update_project_prompt(
    customer: str,
    prompt_id: str,
    request: PromptUpdateRequest,
) -> PromptLibraryItem:
    try:
        return prompt_store().update(customer, prompt_id, request.name, request.content)
    except KeyError:
        raise HTTPException(status_code=404, detail="提示词不存在。") from None
    except PromptLibraryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.put(
    "/api/projects/{customer}/prompts/{prompt_id}/active",
    response_model=PromptLibraryItem,
)
def update_project_prompt_active(
    customer: str,
    prompt_id: str,
    request: PromptActiveUpdateRequest,
) -> PromptLibraryItem:
    try:
        return prompt_store().set_active(customer, prompt_id, request.active)
    except KeyError:
        raise HTTPException(status_code=404, detail="提示词不存在。") from None


@app.delete("/api/projects/{customer}/prompts/{prompt_id}", response_model=ApiMessage)
def delete_project_prompt(customer: str, prompt_id: str) -> ApiMessage:
    try:
        item = prompt_store().get(customer, prompt_id)
        prompt_store().delete(customer, prompt_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="提示词不存在。") from None
    except PromptInUseError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    reset_count = 0
    task_store = store()
    for task in task_store.load():
        if task.customer.strip().lower().rstrip("/") != customer.strip().lower().rstrip("/"):
            continue
        field = (
            "outline_prompt_selection"
            if item.kind == "outline"
            else (
                "article_prompt_selection"
                if item.kind == "article"
                else "seo_review_prompt_selection"
            )
        )
        if getattr(task, field) != prompt_id:
            continue
        setattr(task, field, "system")
        task_store.put(task, expected_revision=task.revision)
        reset_count += 1
    return ApiMessage(
        message=(
            "提示词已彻底删除。"
            if not reset_count
            else f"提示词已彻底删除，{reset_count} 篇尚未生成的文章已恢复为系统默认。"
        )
    )


@app.put("/api/projects/{customer}/prompt-defaults", response_model=PromptDefaults)
def update_project_prompt_defaults(
    customer: str,
    request: PromptDefaultsUpdateRequest,
) -> PromptDefaults:
    require_project(customer)
    try:
        return prompt_store().set_defaults(
            customer,
            request.default_outline_prompt_id,
            request.default_article_prompt_id,
            request.default_review_prompt_id,
        )
    except KeyError:
        raise HTTPException(status_code=422, detail="默认提示词不存在或已停用。") from None
    except PromptLibraryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def resolved_prompt_snapshot(
    task: TaskRecord,
    kind: str,
    selection: str,
    supplied: PromptSnapshot | None = None,
) -> PromptSnapshot:
    if supplied is not None:
        if supplied.kind != kind:
            raise HTTPException(status_code=422, detail="提示词快照类型不匹配。")
        return supplied
    try:
        return prompt_store().resolve(task.customer, kind, selection)
    except PromptLibraryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/tasks/{task_id}/prompt-preview", response_model=PromptPreview)
def preview_effective_prompt(task_id: str, request: PromptPreviewRequest) -> PromptPreview:
    task = get_task_or_404(task_id)
    snapshot = resolved_prompt_snapshot(task, request.kind, request.selection)
    common = {
        "custom_prompt": request.supplemental_prompt,
        "base_prompt": snapshot.content,
        "include_project_introduction": request.include_project_introduction,
        "include_project_notes": request.include_project_notes,
        "include_topic_notes": request.include_topic_notes,
    }
    if request.kind == "outline":
        effective = build_outline_prompt(config(), task, **common)
    elif request.kind == "article":
        effective = build_article_prompt(config(), task, **common)
    else:
        article = task.initial_article or task.article
        if not article.strip():
            raise HTTPException(status_code=409, detail="请先保存第一版正文，再预览复检提示词。")
        effective, snapshot = build_seo_review_prompt(
            config(),
            task,
            article,
            prompt_snapshot=snapshot,
            primary_keyword=task.seo_primary_keyword,
            long_tail_keywords=task.seo_long_tail_keywords,
        )
    return PromptPreview(snapshot=snapshot, effective_prompt=effective)


@app.put("/api/tasks/{task_id}/writing-settings", response_model=TaskRecord)
def update_writing_settings(
    task_id: str,
    request: WritingSettingsUpdateRequest,
) -> TaskRecord:
    task = get_task_or_404(task_id)
    task.topic_notes = request.topic_notes.strip()
    task.outline_custom_prompt = request.outline_custom_prompt.strip()
    task.article_custom_prompt = request.article_custom_prompt.strip()
    task.use_outline_custom_prompt = request.use_outline_custom_prompt
    task.use_article_custom_prompt = request.use_article_custom_prompt
    task.outline_prompt_selection = request.outline_prompt_selection.strip() or "project_default"
    task.article_prompt_selection = request.article_prompt_selection.strip() or "project_default"
    task.include_project_introduction = request.include_project_introduction
    task.include_project_notes = request.include_project_notes
    task.include_topic_notes = request.include_topic_notes
    write_json_artifact(
        task,
        "writing_settings.json",
        {
            "topic_notes": task.topic_notes,
            "outline_custom_prompt": task.outline_custom_prompt,
            "article_custom_prompt": task.article_custom_prompt,
            "use_outline_custom_prompt": task.use_outline_custom_prompt,
            "use_article_custom_prompt": task.use_article_custom_prompt,
            "outline_prompt_selection": task.outline_prompt_selection,
            "article_prompt_selection": task.article_prompt_selection,
            "include_project_introduction": task.include_project_introduction,
            "include_project_notes": task.include_project_notes,
            "include_topic_notes": task.include_topic_notes,
        },
    )
    return save_task(task, request.revision)


@app.put("/api/tasks/{task_id}/seo-review-settings", response_model=TaskRecord)
def update_seo_review_settings(
    task_id: str,
    request: SeoReviewSettingsUpdateRequest,
) -> TaskRecord:
    task = get_task_or_404(task_id)
    selection = request.prompt_selection.strip() or "project_default"
    resolved_prompt_snapshot(task, "review", selection)
    keywords = normalized_keywords(request.long_tail_keywords)
    if any(len(keyword) > 240 for keyword in keywords):
        raise HTTPException(status_code=422, detail="每个长尾关键词最多 240 个字符。")
    task.seo_primary_keyword = re.sub(
        r"\s+", " ", request.primary_keyword
    ).strip()
    task.seo_long_tail_keywords = keywords
    task.seo_review_prompt_selection = selection
    saved = save_task(task, request.revision)
    write_json_artifact(
        saved,
        "seo_review_settings.json",
        {
            "primary_keyword": saved.seo_primary_keyword,
            "long_tail_keywords": saved.seo_long_tail_keywords,
            "prompt_selection": saved.seo_review_prompt_selection,
        },
    )
    return saved


@app.post("/api/tasks/{task_id}/rewrite-from-scratch", response_model=TaskRecord)
def rewrite_task_from_scratch(
    task_id: str,
    request: RevisionedRequest,
) -> TaskRecord:
    task = get_task_or_404(task_id)
    require_action(task, ACTION_REWRITE_FROM_SCRATCH)
    reset_for_full_rewrite(task)
    return save_task(task, request.revision)


def perform_title_generation(
    task_id: str,
    request: RevisionedRequest,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> TaskRecord:
    task = get_task_or_404(task_id)
    require_action(task, ACTION_GENERATE_TITLES)
    candidates = generate_titles(config(), task)
    raise_if_batch_cancelled(cancelled)
    task.title_candidates = candidates
    invalidate_downstream(task, "titles")
    task.status = STATUS_TITLES_READY
    saved = save_task(task, request.revision)
    write_text_artifact(
        saved,
        "title_candidates.md",
        "\n".join(
            f"{index + 1}. {title}"
            for index, title in enumerate(saved.title_candidates)
        ),
    )
    return saved


@app.post("/api/projects/{customer}/titles/direct", response_model=TaskRecord)
def create_direct_title_task(
    customer: str,
    request: ManualTitleGenerationRequest,
) -> TaskRecord:
    require_project(customer)
    cfg = config()
    project_tasks = [
        task
        for task in store().canonical_tasks(cfg.current_week_folder)
        if normalized_customer(task.customer) == normalized_customer(customer)
    ]
    latest = max(project_tasks, key=lambda task: task.updated_at)
    topic_index = max((task.topic_index for task in project_tasks), default=0) + 1
    task_id = uuid4().hex[:12]
    task_dir = Path(latest.task_dir).parent / f"manual_{task_id}"
    timestamp = now_iso()
    task = TaskRecord(
        id=task_id,
        week_folder=cfg.current_week_folder,
        customer=latest.customer,
        brand_name=latest.brand_name,
        project_introduction=latest.project_introduction,
        project_notes=latest.project_notes,
        source_key=f"manual:{article_source_key(latest.customer, request.topic, topic_index)}",
        source_kind="manual",
        topic_index=topic_index,
        topic=request.topic.strip(),
        title_generation_instruction=request.instruction.strip(),
        task_dir=str(task_dir),
        created_at=timestamp,
        updated_at=timestamp,
    )
    task.title_candidates = generate_titles(
        cfg,
        task,
        instruction=task.title_generation_instruction,
    )[:10]
    task.status = STATUS_TITLES_READY
    task_dir.mkdir(parents=True, exist_ok=False)
    saved = store().put(task, expected_revision=0)
    write_json_artifact(
        saved,
        "manual_title_request.json",
        {"topic": saved.topic, "instruction": saved.title_generation_instruction},
    )
    write_text_artifact(
        saved,
        "title_candidates.md",
        "\n".join(f"{index + 1}. {title}" for index, title in enumerate(saved.title_candidates)),
    )
    return saved


@app.post("/api/tasks/{task_id}/titles", response_model=TaskRecord)
def create_titles(
    task_id: str,
    request: RevisionedRequest | None = None,
) -> TaskRecord:
    return perform_title_generation(task_id, request or RevisionedRequest())


@app.post("/api/tasks/{task_id}/select-title", response_model=TaskRecord)
def select_title(task_id: str, request: SelectTitleRequest) -> TaskRecord:
    task = get_task_or_404(task_id)
    require_action(task, ACTION_SELECT_TITLE)
    task.selected_title = request.title.strip()
    if not task.selected_title:
        raise HTTPException(status_code=422, detail="A selected title is required.")
    invalidate_downstream(task, "selected_title")
    task.status = STATUS_TITLE_SELECTED
    write_text_artifact(task, "selected_title.txt", task.selected_title)
    return save_task(task, request.revision)


@app.put("/api/tasks/{task_id}/products", response_model=TaskRecord)
def update_products(task_id: str, request: ProductsUpdateRequest) -> TaskRecord:
    with product_processing(task_id):
        task = get_task_or_404(task_id)
        _require_workflow_action(task, ACTION_UPDATE_PRODUCTS)
        previous_by_id = {
            product.product_id: product for product in task.products if product.product_id
        }
        next_products = []
        for index, product in enumerate(request.products):
            previous = previous_by_id.get(product.product_id) if product.product_id else None
            if previous is None and index < len(task.products):
                previous = task.products[index]
            if previous is not None and product_url_changed(previous, product):
                product = reset_product_enrichment(product)
            next_products.append(product)
        task.products = next_products
        invalidate_downstream(task, "products")
        saved = save_task(task, request.revision)
        write_json_artifact(
            saved,
            "products.json",
            [product.model_dump() for product in saved.products],
        )
        return saved


def perform_auto_products(
    task_id: str,
    request: AutoProductsRequest,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> TaskRecord:
    with product_processing(task_id):
        task = get_task_or_404(task_id)
        _require_workflow_action(task, ACTION_UPDATE_PRODUCTS)
        cfg = config()
        try:
            discovered = recommend_products(
                cfg,
                task,
                request.limit,
                tavily_client=TavilyClient(timeout=15),
                download_images=False,
                candidate_pool_limit=request.limit + 3,
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Auto product discovery failed: {exc}") from exc
        if not discovered:
            raise HTTPException(
                status_code=422,
                detail="No verified official product detail pages were found; existing products were kept.",
            )
        raise_if_batch_cancelled(cancelled)
        try:
            enriched = enrich_product_assets(
                cfg,
                task,
                discovered,
                llm=LLMClient(cfg, timeout_seconds=60),
                stop_after_selected=request.limit,
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Official product asset crawl failed: {exc}") from exc
        selected = [
            product
            for product in enriched
            if product.asset_status == "selected"
            and product.detail_page_verified
            and product.image_path.strip()
        ][: request.limit]
        if not selected:
            raise HTTPException(
                status_code=422,
                detail=(
                    "No verified official product detail pages with safe images were found; "
                    "existing products were kept."
                ),
            )
        task.products = selected
        raise_if_batch_cancelled(cancelled)
        invalidate_downstream(task, "products")
        saved = save_task(task, request.revision)
        write_json_artifact(
            saved,
            "products.json",
            [product.model_dump() for product in saved.products],
        )
        return saved


@app.post("/api/tasks/{task_id}/products/auto", response_model=TaskRecord)
def auto_products(
    task_id: str,
    limit: int = 3,
    revision: int | None = None,
) -> TaskRecord:
    return perform_auto_products(
        task_id,
        AutoProductsRequest(limit=max(1, min(limit, 3)), revision=revision),
    )


@app.post("/api/tasks/{task_id}/products/assets", response_model=TaskRecord)
def refresh_product_assets(
    task_id: str,
    revision: int | None = None,
) -> TaskRecord:
    with product_processing(task_id):
        task = get_task_or_404(task_id)
        _require_workflow_action(task, ACTION_UPDATE_PRODUCTS)
        eligible = [
            (index, product)
            for index, product in enumerate(task.products)
            if product.url.strip()
        ]
        if not eligible:
            raise HTTPException(status_code=422, detail="At least one official product URL is required.")
        cfg = config()
        try:
            refreshed = enrich_product_assets(
                cfg,
                task,
                [product for _, product in eligible],
                llm=LLMClient(cfg, timeout_seconds=60),
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Product asset refresh failed: {exc}") from exc
        merged = list(task.products)
        for (index, _), product in zip(eligible, refreshed, strict=True):
            merged[index] = product
        task.products = merged
        invalidate_downstream(task, "products")
        saved = save_task(task, revision)
        write_json_artifact(
            saved,
            "products.json",
            [product.model_dump() for product in saved.products],
        )
        return saved


def apply_generation_options(
    task: TaskRecord,
    request: GenerateOutlineRequest | GenerateArticleRequest,
    *,
    prompt_field: str,
    enabled_field: str,
) -> None:
    if request.custom_prompt is not None:
        setattr(task, prompt_field, request.custom_prompt.strip())
    if request.use_custom_prompt is not None:
        setattr(task, enabled_field, request.use_custom_prompt)
    if request.prompt_selection is not None:
        selection_field = (
            "outline_prompt_selection"
            if prompt_field == "outline_custom_prompt"
            else "article_prompt_selection"
        )
        setattr(task, selection_field, request.prompt_selection.strip() or "project_default")
    for field in (
        "include_project_introduction",
        "include_project_notes",
        "include_topic_notes",
    ):
        value = getattr(request, field)
        if value is not None:
            setattr(task, field, value)


def raise_if_batch_cancelled(cancelled: Callable[[], bool] | None) -> None:
    if cancelled is not None and cancelled():
        raise JobCancelled("Batch job was cancelled before its result was saved.")


def perform_outline_generation(
    task_id: str,
    request: GenerateOutlineRequest,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> TaskRecord:
    task = get_task_or_404(task_id)
    require_action(task, ACTION_GENERATE_OUTLINE)
    apply_generation_options(
        task,
        request,
        prompt_field="outline_custom_prompt",
        enabled_field="use_outline_custom_prompt",
    )
    snapshot = resolved_prompt_snapshot(
        task,
        "outline",
        task.outline_prompt_selection,
        request.prompt_snapshot,
    )
    if request.prompt_snapshot is None and snapshot.prompt_id:
        prompt_store().mark_used(task.customer, snapshot.prompt_id)
    task.last_outline_prompt_snapshot = snapshot
    outline = generate_outline(
        config(),
        task,
        custom_prompt=(
            task.outline_custom_prompt if task.use_outline_custom_prompt else ""
        ),
        base_prompt=snapshot.content,
        include_project_introduction=task.include_project_introduction,
        include_project_notes=task.include_project_notes,
        include_topic_notes=task.include_topic_notes,
    )
    raise_if_batch_cancelled(cancelled)
    task.outline = outline
    task.outline_draft = outline
    append_version(task, "outline", outline, "generated")
    invalidate_downstream(task, "outline")
    saved = save_task(task, request.revision)
    write_text_artifact(saved, "outline.md", saved.outline)
    return saved


@app.post("/api/tasks/{task_id}/outline", response_model=TaskRecord)
def create_outline(
    task_id: str,
    request: GenerateOutlineRequest | None = None,
) -> TaskRecord:
    return perform_outline_generation(task_id, request or GenerateOutlineRequest())


@app.put("/api/tasks/{task_id}/outline", response_model=TaskRecord)
def update_outline(task_id: str, request: OutlineUpdateRequest) -> TaskRecord:
    task = get_task_or_404(task_id)
    require_action(task, ACTION_UPDATE_OUTLINE)
    outline = request.outline.strip()
    if not outline:
        raise HTTPException(status_code=422, detail="Outline cannot be empty.")
    task.outline_draft = outline
    append_version(
        task,
        "outline" if request.confirmed else "outline_draft",
        outline,
        "manual_confirmed" if request.confirmed else "manual_draft",
    )
    if request.confirmed:
        task.outline = outline
        invalidate_downstream(task, "outline")
        advance(task, STATUS_OUTLINE_CONFIRMED)
    # Commit the revision before replacing the human-readable artifact.  A
    # stale client must not overwrite outline.md and then fail to save tasks.json.
    saved = save_task(task, request.revision)
    write_text_artifact(saved, "outline-draft.md", saved.outline_draft)
    if request.confirmed:
        write_text_artifact(saved, "outline.md", saved.outline)
    return saved


@app.post("/api/tasks/{task_id}/versions/restore", response_model=TaskRecord)
def restore_content_version(task_id: str, request: VersionRestoreRequest) -> TaskRecord:
    task = get_task_or_404(task_id)
    if request.version_index >= len(task.article_versions):
        raise HTTPException(status_code=404, detail="The selected version no longer exists.")
    version = task.article_versions[request.version_index]
    content = version.content.strip()
    if not content:
        raise HTTPException(status_code=422, detail="The selected version is empty.")

    if version.kind in {"outline", "outline_draft"}:
        require_action(task, ACTION_UPDATE_OUTLINE)
        # Restore into the editable buffer so comparison and confirmation stay
        # separate; downstream work is untouched until explicit confirmation.
        task.outline_draft = content
        append_version(task, "outline_draft", content, "restored")
        saved = save_task(task, request.revision)
        write_text_artifact(saved, "outline-draft.md", saved.outline_draft)
        return saved

    if version.kind != "initial":
        raise HTTPException(
            status_code=422,
            detail="Only outline and first-version article snapshots can be restored here.",
        )

    require_action(task, ACTION_UPDATE_ARTICLE)
    try:
        initial = ensure_article_hyperlinks(content, task)
        validate_article_layout(initial)
        validate_minimum_h3_per_h2(initial)
        if not has_intro_transition(initial):
            raise ArticleStructureError(
                "Article must include a transition paragraph between its H1 and first H2."
            )
    except (ArticleStructureError, ArticleGenerationError, PromptTemplateError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    task.initial_article = initial
    task.initial_article_word_count = visible_word_count(initial)
    task.initial_article_hash = content_hash(initial)
    task.article = initial
    invalidate_downstream(task, "initial_article")
    task.source_links = source_links(initial)
    task.transition_added = True
    append_version(task, "initial", initial, "restored")
    saved = save_task(task, request.revision)
    write_text_artifact(saved, "02_initial_article.md", saved.initial_article)
    write_json_artifact(
        saved,
        "02_initial_links.json",
        [link.model_dump() for link in saved.source_links],
    )
    return saved


def perform_article_generation(
    task_id: str,
    request: GenerateArticleRequest,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> TaskRecord:
    task = get_task_or_404(task_id)
    require_action(task, ACTION_GENERATE_ARTICLE)
    cfg = config()
    client = LLMClient(cfg)
    original_revision = task.revision
    is_regeneration = bool(task.initial_article.strip())

    apply_generation_options(
        task,
        request,
        prompt_field="article_custom_prompt",
        enabled_field="use_article_custom_prompt",
    )

    snapshot = resolved_prompt_snapshot(
        task,
        "article",
        task.article_prompt_selection,
        request.prompt_snapshot,
    )
    if request.prompt_snapshot is None and snapshot.prompt_id:
        prompt_store().mark_used(task.customer, snapshot.prompt_id)
    task.last_article_prompt_snapshot = snapshot

    raw_article = generate_raw_article(
        cfg,
        task,
        request.word_count,
        custom_prompt=(
            task.article_custom_prompt if task.use_article_custom_prompt else ""
        ),
        base_prompt=snapshot.content,
        include_project_introduction=task.include_project_introduction,
        include_project_notes=task.include_project_notes,
        include_topic_notes=task.include_topic_notes,
        llm=client,
    )
    raise_if_batch_cancelled(cancelled)
    task.raw_draft_article = raw_article
    task.raw_draft_word_count = visible_word_count(raw_article)
    task.raw_draft_hash = content_hash(raw_article)
    write_text_artifact(task, "01_raw_draft.md", raw_article)

    try:
        transition_was_present = has_intro_transition(raw_article)
        prepared = ensure_transition_before_first_h2(cfg, task, raw_article, llm=client)
        raise_if_batch_cancelled(cancelled)
        initial = ensure_article_hyperlinks(prepared, task)
        validate_article_layout(initial)
    except (
        ArticleStructureError,
        ArticleGenerationError,
        PromptTemplateError,
    ) as exc:
        set_stage_error(task, code="article_generation_failed", message=str(exc), stage="article")
        save_task(task, request.revision if request.revision is not None else original_revision)
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    task.initial_article = initial
    task.initial_article_word_count = visible_word_count(initial)
    task.initial_article_hash = content_hash(initial)
    task.article = initial
    invalidate_downstream(task, "initial_article")
    task.transition_added = not transition_was_present
    task.source_links = source_links(initial)
    task.article_versions.extend(
        [
            version_record("raw_draft", raw_article),
            version_record(
                "initial",
                initial,
                "regenerated_raw_draft" if is_regeneration else "raw_draft",
            ),
        ]
    )
    task.compression = {
        "required": False,
        "attempted_at": "",
        "before_words": task.initial_article_word_count,
        "after_words": task.initial_article_word_count,
        "prompt_version": "disabled",
    }
    task.workflow_error = None
    raise_if_batch_cancelled(cancelled)
    write_text_artifact(task, "02_initial_article.md", initial)
    write_json_artifact(task, "02_initial_links.json", [link.model_dump() for link in task.source_links])
    return save_task(task, request.revision if request.revision is not None else original_revision)


@app.post("/api/tasks/{task_id}/article", response_model=TaskRecord)
def create_article(task_id: str, request: GenerateArticleRequest) -> TaskRecord:
    return perform_article_generation(task_id, request)


def batch_request_snapshot(
    task: TaskRecord,
    operation: str,
    *,
    word_count: int | None = None,
) -> dict:
    if operation == "titles":
        return RevisionedRequest(revision=task.revision).model_dump(
            mode="json",
            exclude_none=True,
        )
    if operation == "products":
        return AutoProductsRequest(revision=task.revision, limit=3).model_dump(
            mode="json",
            exclude_none=True,
        )
    if operation == "seo_review":
        snapshot = effective_review_prompt_snapshot(
            resolved_prompt_snapshot(
                task,
                "review",
                task.seo_review_prompt_selection,
            )
        )
        return SeoReviewRequest(
            revision=task.revision,
            primary_keyword=task.seo_primary_keyword,
            long_tail_keywords=task.seo_long_tail_keywords,
            prompt_selection=task.seo_review_prompt_selection,
            prompt_snapshot=snapshot,
        ).model_dump(mode="json", exclude_none=True)
    if operation in {
        "humanize",
        "restore_links",
        "prepare_images",
        "export_docx",
        "generate_tdk",
        "package_delivery",
    }:
        return RevisionedRequest(revision=task.revision).model_dump(
            mode="json",
            exclude_none=True,
        )
    shared = {
        "revision": task.revision,
        "include_project_introduction": task.include_project_introduction,
        "include_project_notes": task.include_project_notes,
        "include_topic_notes": task.include_topic_notes,
    }
    if operation == "outline":
        snapshot = resolved_prompt_snapshot(
            task,
            "outline",
            task.outline_prompt_selection,
        )
        return GenerateOutlineRequest(
            **shared,
            custom_prompt=task.outline_custom_prompt,
            use_custom_prompt=task.use_outline_custom_prompt,
            prompt_selection=task.outline_prompt_selection,
            prompt_snapshot=snapshot,
        ).model_dump(mode="json", exclude_none=True)
    snapshot = resolved_prompt_snapshot(
        task,
        "article",
        task.article_prompt_selection,
    )
    return GenerateArticleRequest(
        **shared,
        word_count=word_count or config().default_word_count,
        custom_prompt=task.article_custom_prompt,
        use_custom_prompt=task.use_article_custom_prompt,
        prompt_selection=task.article_prompt_selection,
        prompt_snapshot=snapshot,
    ).model_dump(mode="json", exclude_none=True)


def batch_preflight_issue(task: TaskRecord, operation: str) -> str:
    if product_processing_active(task.id):
        return "该任务正在抓取产品或处理图片资产。"
    actions = allowed_actions(task)
    if operation == "titles":
        if ACTION_GENERATE_TITLES not in actions:
            return f"当前状态“{task.status}”不能生成候选标题。"
        return ""
    if operation == "products":
        if ACTION_UPDATE_PRODUCTS not in actions:
            return f"当前状态“{task.status}”不能自动查找产品。"
        return ""
    if operation == "outline":
        if ACTION_GENERATE_OUTLINE not in actions:
            return f"当前状态“{task.status}”不能生成大纲。"
        return ""
    if operation == "seo_review":
        if not task.initial_article.strip():
            return "请先保存第一版正文，再执行 SEO 质量复检。"
        return ""
    action_by_operation = {
        "humanize": ACTION_HUMANIZE_ARTICLE,
        "restore_links": ACTION_RESTORE_LINKS,
        "prepare_images": ACTION_PREPARE_IMAGES,
        "export_docx": ACTION_EXPORT_DOCX,
        "generate_tdk": ACTION_GENERATE_TDK,
        "package_delivery": ACTION_PACKAGE_DELIVERY,
    }
    if operation in action_by_operation:
        if action_by_operation[operation] not in actions:
            return f"当前状态“{task.status}”不能执行操作“{operation}”。"
        return ""
    if ACTION_GENERATE_ARTICLE not in actions:
        return f"当前状态“{task.status}”不能生成正文。"
    has_article = bool(task.initial_article.strip() or task.raw_draft_article.strip())
    if operation == "article" and has_article:
        return "已经存在第一版，请使用“批量仅重写正文”。"
    if operation == "rewrite_article" and not has_article:
        return "还没有第一版，请使用“批量生成正文”。"
    return ""


def _execute_batch_job(
    job: dict,
    cancelled: Callable[[], bool],
) -> int:
    task_id = str(job["task_id"])
    try:
        current = store().get(task_id)
    except KeyError as exc:
        raise JobConflict(f"任务 {task_id} 已不存在。") from exc
    expected_revision = int(job["source_revision"])
    if current.revision != expected_revision:
        raise JobConflict(
            f"任务在排队后被修改，未覆盖新内容：排队版本 {expected_revision}，"
            f"当前版本 {current.revision}。"
        )
    issue = batch_preflight_issue(current, str(job["operation"]))
    if issue:
        raise JobConflict(issue)
    if cancelled():
        raise JobCancelled("Batch job cancelled before model request.")
    try:
        if job["operation"] == "products":
            request = AutoProductsRequest.model_validate(job["request"])
            saved = perform_auto_products(
                task_id,
                request,
                cancelled=cancelled,
            )
        elif job["operation"] == "titles":
            request = RevisionedRequest.model_validate(job["request"])
            saved = perform_title_generation(
                task_id,
                request,
                cancelled=cancelled,
            )
        elif job["operation"] == "outline":
            request = GenerateOutlineRequest.model_validate(job["request"])
            saved = perform_outline_generation(
                task_id,
                request,
                cancelled=cancelled,
            )
        elif job["operation"] == "seo_review":
            request = SeoReviewRequest.model_validate(job["request"])
            saved = perform_seo_review(
                task_id,
                request,
                cancelled=cancelled,
            )
        elif job["operation"] == "humanize":
            request = RevisionedRequest.model_validate(job["request"])
            saved = perform_humanize(
                task_id,
                request,
                cancelled=cancelled,
            )
        elif job["operation"] == "restore_links":
            request = RevisionedRequest.model_validate(job["request"])
            saved = perform_restore_links(
                task_id,
                request,
                cancelled=cancelled,
            )
        elif job["operation"] == "prepare_images":
            request = RevisionedRequest.model_validate(job["request"])
            saved = perform_prepare_images(
                task_id,
                request,
                cancelled=cancelled,
            )
        elif job["operation"] == "export_docx":
            request = RevisionedRequest.model_validate(job["request"])
            saved = perform_export_docx(task_id, request)
        elif job["operation"] == "generate_tdk":
            request = RevisionedRequest.model_validate(job["request"])
            saved = perform_generate_tdk(
                task_id,
                request,
                cancelled=cancelled,
            )
        elif job["operation"] == "package_delivery":
            request = RevisionedRequest.model_validate(job["request"])
            saved = perform_package_delivery(task_id, request)
        else:
            request = GenerateArticleRequest.model_validate(job["request"])
            saved = perform_article_generation(
                task_id,
                request,
                cancelled=cancelled,
            )
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        if exc.status_code == 409:
            raise JobConflict(detail) from exc
        raise
    return saved.revision


@app.post("/api/batches", response_model=BatchCreateResponse)
def create_batch(request: BatchCreateRequest) -> BatchCreateResponse:
    queue = batch_queue()
    task_ids = list(dict.fromkeys(task_id.strip() for task_id in request.task_ids if task_id.strip()))
    if not task_ids:
        raise HTTPException(status_code=422, detail="请至少选择一个任务。")
    active_ids = queue.active_task_ids(task_ids)
    rejected: list[BatchPreflightIssue] = []
    accepted: list[dict] = []
    customers: set[str] = set()
    for task_id in task_ids:
        try:
            task = store().get(task_id)
        except KeyError:
            rejected.append(BatchPreflightIssue(task_id=task_id, message="任务不存在。"))
            continue
        if task_id in active_ids:
            rejected.append(
                BatchPreflightIssue(task_id=task_id, message="该任务已有排队或执行中的批量操作。")
            )
            continue
        issue = batch_preflight_issue(task, request.operation)
        if issue:
            rejected.append(BatchPreflightIssue(task_id=task_id, message=issue))
            continue
        customers.add(task.customer)
        accepted.append(
            {
                "task_id": task.id,
                "customer": task.customer,
                "topic_index": task.topic_index,
                "topic": task.topic,
                "source_revision": task.revision,
                "request": batch_request_snapshot(
                    task,
                    request.operation,
                    word_count=request.word_count,
                ),
            }
        )
    if not accepted:
        return BatchCreateResponse(batch=None, rejected=rejected)
    try:
        payload = queue.create_batch(
            request.operation,
            accepted,
            customer=next(iter(customers)) if len(customers) == 1 else "",
        )
    except ActiveJobError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    for item in accepted:
        snapshot = item["request"].get("prompt_snapshot") or {}
        prompt_id = str(snapshot.get("prompt_id") or "")
        if prompt_id:
            prompt_store().mark_used(item["customer"], prompt_id)
    wake_batch_runner()
    return BatchCreateResponse(
        batch=BatchRecord.model_validate(payload),
        rejected=rejected,
    )


@app.get("/api/batches", response_model=list[BatchRecord])
def list_batches(customer: str = "", limit: int = 10) -> list[BatchRecord]:
    return [
        BatchRecord.model_validate(item)
        for item in batch_queue().list_batches(customer=customer, limit=limit)
    ]


@app.get("/api/batches/{batch_id}", response_model=BatchRecord)
def read_batch(batch_id: str) -> BatchRecord:
    try:
        return BatchRecord.model_validate(batch_queue().get_batch(batch_id))
    except KeyError:
        raise HTTPException(status_code=404, detail="批量任务不存在。") from None


@app.post("/api/batches/{batch_id}/cancel", response_model=BatchRecord)
def cancel_batch(batch_id: str) -> BatchRecord:
    try:
        result = batch_queue().cancel_batch(batch_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="批量任务不存在。") from None
    wake_batch_runner()
    return BatchRecord.model_validate(result)


@app.post("/api/batch-jobs/{job_id}/cancel", response_model=BatchJobRecord)
def cancel_batch_job(job_id: str) -> BatchJobRecord:
    try:
        result = batch_queue().request_cancel(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="批量任务条目不存在。") from None
    wake_batch_runner()
    return BatchJobRecord.model_validate(result)


@app.post("/api/batch-jobs/{job_id}/retry", response_model=BatchJobRecord)
def retry_batch_job(job_id: str) -> BatchJobRecord:
    queue = batch_queue()
    try:
        previous = queue.get_job(job_id)
        task = store().get(str(previous["task_id"]))
    except KeyError:
        raise HTTPException(status_code=404, detail="任务或批量任务条目不存在。") from None
    issue = batch_preflight_issue(task, str(previous["operation"]))
    if issue:
        raise HTTPException(status_code=409, detail=issue)
    snapshot = batch_request_snapshot(
        task,
        str(previous["operation"]),
        word_count=previous["request"].get("word_count"),
    )
    try:
        result = queue.retry_job(
            job_id,
            source_revision=task.revision,
            request=snapshot,
        )
    except (ActiveJobError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    wake_batch_runner()
    return BatchJobRecord.model_validate(result)


def validated_initial_article(article: str, task: TaskRecord) -> str:
    try:
        # PUT is a deterministic persistence endpoint.  Generation may repair
        # a missing transition with the LLM, but saving pasted/edited text must
        # never trigger a model call or silently rewrite prose.
        initial = ensure_article_hyperlinks(article, task)
        validate_article_layout(initial)
        validate_minimum_h3_per_h2(initial)
        if not has_intro_transition(initial):
            raise ArticleStructureError(
                "Article must include a transition paragraph between its H1 and first H2."
            )
    except (
        ArticleStructureError,
        ArticleGenerationError,
        PromptTemplateError,
    ) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return initial


def replace_initial_article(
    task: TaskRecord,
    initial: str,
    *,
    source_kind: str,
) -> None:
    task.initial_article = initial
    task.initial_article_word_count = visible_word_count(initial)
    task.initial_article_hash = content_hash(initial)
    task.article = initial
    invalidate_downstream(task, "initial_article")
    task.source_links = source_links(initial)
    task.transition_added = has_intro_transition(initial)
    task.article_versions.append(version_record("initial", initial, source_kind))


def write_initial_article_artifacts(task: TaskRecord) -> None:
    write_text_artifact(task, "02_initial_article.md", task.initial_article)
    write_json_artifact(
        task,
        "02_initial_links.json",
        [link.model_dump() for link in task.source_links],
    )


@app.put("/api/tasks/{task_id}/article", response_model=TaskRecord)
def update_article(task_id: str, request: ArticleUpdateRequest) -> TaskRecord:
    task = get_task_or_404(task_id)
    require_action(task, ACTION_UPDATE_ARTICLE)
    initial = validated_initial_article(request.article, task)
    replace_initial_article(task, initial, source_kind="manual_edit")
    # Revision validation happens in save_task.  Only publish artifacts after
    # it succeeds so a stale editor cannot overwrite the accepted version.
    saved = save_task(task, request.revision)
    write_initial_article_artifacts(saved)
    return saved


def perform_seo_review(
    task_id: str,
    request: SeoReviewRequest,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> TaskRecord:
    task = get_task_or_404(task_id)
    article = task.initial_article.strip()
    if not article:
        raise HTTPException(
            status_code=409,
            detail="请先保存第一版正文，再执行 SEO 质量复检。",
        )
    snapshot = resolved_prompt_snapshot(
        task,
        "review",
        request.prompt_selection,
        request.prompt_snapshot,
    )
    snapshot = effective_review_prompt_snapshot(snapshot)
    if cancelled and cancelled():
        raise JobCancelled("SEO review cancelled before model request.")
    try:
        generated = generate_seo_review(
            config(),
            task,
            article,
            prompt_snapshot=snapshot,
            primary_keyword=request.primary_keyword,
            long_tail_keywords=request.long_tail_keywords,
        )
    except (SeoReviewError, ArticleStructureError, PromptTemplateError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if cancelled and cancelled():
        raise JobCancelled("SEO review cancelled before saving the result.")

    review_id = uuid4().hex[:12]
    keywords = normalized_keywords(request.long_tail_keywords)
    run = SeoReviewRun(
        id=review_id,
        source_article=article,
        source_article_hash=content_hash(article),
        source_revision=task.revision,
        score=generated.score,
        dimensions=generated.dimensions,
        publish_ready=generated.publish_ready,
        publish_recommendation=generated.publish_recommendation,
        report=generated.report,
        changes=generated.changes,
        prompt_snapshot=generated.prompt_snapshot,
        primary_keyword=re.sub(r"\s+", " ", request.primary_keyword).strip(),
        long_tail_keywords=keywords,
        created_at=now_iso(),
    )
    task.seo_primary_keyword = run.primary_keyword
    task.seo_long_tail_keywords = keywords
    task.seo_review_prompt_selection = (
        request.prompt_selection.strip() or "project_default"
    )
    task.seo_reviews.append(run)
    saved = save_task(task, request.revision)
    write_seo_review_artifacts(saved, review_id)
    return saved


def seo_review_entry(
    task: TaskRecord,
    review_id: str,
) -> tuple[int, SeoReviewRun]:
    for index, review in enumerate(task.seo_reviews):
        if review.id == review_id:
            return index, review
    raise HTTPException(status_code=404, detail="找不到指定的 SEO 复检记录。")


def ensure_review_editable(task: TaskRecord, review: SeoReviewRun) -> None:
    if review.status != "open":
        raise HTTPException(status_code=409, detail="该复检记录已经锁定，不能继续修改。")
    if content_hash(task.initial_article.strip()) != review.source_article_hash:
        raise HTTPException(
            status_code=409,
            detail="当前第一版正文已经变化，这份旧 Diff 只能查看，请针对新正文重新复检。",
        )


def seo_review_operator() -> str:
    return str(getattr(config(), "week_owner", "") or "").strip() or "本地操作员"


def write_seo_review_artifacts(task: TaskRecord, review_id: str) -> None:
    index, review = seo_review_entry(task, review_id)
    artifact_root = f"seo-reviews/{index + 1:03d}-{review_id}"
    write_text_artifact(task, f"{artifact_root}-report.md", review.report)
    write_json_artifact(task, f"{artifact_root}.json", review.model_dump(mode="json"))


def build_validated_review_preview(
    task: TaskRecord,
    review: SeoReviewRun,
) -> SeoReviewPreview:
    try:
        candidate, change_ids = build_review_candidate(review)
    except (SeoReviewError, ArticleStructureError, PromptTemplateError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    candidate = validated_initial_article(candidate, task)
    return SeoReviewPreview(
        review_id=review.id,
        article=candidate,
        article_hash=content_hash(candidate),
        accepted_change_ids=change_ids,
        pending_count=sum(change.decision == "pending" for change in review.changes),
        rejected_count=sum(change.decision == "rejected" for change in review.changes),
        invalid_count=sum(not change.applicable for change in review.changes),
        structure_valid=True,
    )


@app.put(
    "/api/tasks/{task_id}/seo-reviews/{review_id}/changes/{change_id}",
    response_model=TaskRecord,
)
def update_seo_review_change(
    task_id: str,
    review_id: str,
    change_id: str,
    request: SeoReviewChangeUpdateRequest,
) -> TaskRecord:
    task = get_task_or_404(task_id)
    review_index, review = seo_review_entry(task, review_id)
    ensure_review_editable(task, review)
    change_index = next(
        (
            index
            for index, change in enumerate(review.changes)
            if change.id == change_id
        ),
        -1,
    )
    if change_index < 0:
        raise HTTPException(status_code=404, detail="找不到指定的复检修改块。")
    timestamp = now_iso()
    try:
        updated = update_review_change(
            review.changes[change_index],
            reviewed_text=request.reviewed_text,
            decision=request.decision,
            brand_name=task.brand_name,
            product_names=[product.name for product in task.products if product.name],
            confirm_risks=request.confirm_risks,
            decided_at=timestamp,
            decided_by=seo_review_operator(),
        )
    except SeoReviewError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    review.changes[change_index] = updated
    task.seo_reviews[review_index] = review
    saved = save_task(task, request.revision)
    write_seo_review_artifacts(saved, review_id)
    return saved


@app.post(
    "/api/tasks/{task_id}/seo-reviews/{review_id}/preview",
    response_model=SeoReviewPreview,
)
def preview_seo_review_changes(
    task_id: str,
    review_id: str,
    request: SeoReviewPreviewRequest,
) -> SeoReviewPreview:
    task = get_task_or_404(task_id)
    _index, review = seo_review_entry(task, review_id)
    ensure_review_editable(task, review)
    if request.revision is not None and request.revision != task.revision:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Task {task.id} revision conflict: expected "
                f"{request.revision}, current {task.revision}."
            ),
        )
    return build_validated_review_preview(task, review)


@app.post(
    "/api/tasks/{task_id}/seo-reviews/{review_id}/apply",
    response_model=TaskRecord,
)
def apply_seo_review_changes(
    task_id: str,
    review_id: str,
    request: SeoReviewFinalizeRequest,
) -> TaskRecord:
    task = get_task_or_404(task_id)
    require_action(task, ACTION_UPDATE_ARTICLE)
    review_index, review = seo_review_entry(task, review_id)
    ensure_review_editable(task, review)
    accepted_count = sum(
        change.decision == "accepted" for change in review.changes
    )
    if not accepted_count:
        raise HTTPException(
            status_code=422,
            detail="当前没有已接受的修改，请使用“完成审核，不修改正文”。",
        )
    pending_count = sum(change.decision == "pending" for change in review.changes)
    if pending_count and not request.confirm_pending:
        raise HTTPException(
            status_code=409,
            detail="仍有未处理修改块，必须确认将其按“未处理”状态锁定。",
        )
    preview = build_validated_review_preview(task, review)
    if not request.preview_hash or request.preview_hash != preview.article_hash:
        raise HTTPException(
            status_code=409,
            detail="完整正文预览已经失效，请重新预览后再应用。",
        )

    timestamp = now_iso()
    review.status = "applied"
    review.finalized_at = timestamp
    review.finalized_by = seo_review_operator()
    review.applied_article_hash = preview.article_hash
    review.applied_revision = task.revision + 1
    task.seo_reviews[review_index] = review
    replace_initial_article(
        task,
        preview.article,
        source_kind=f"seo_review:{review_id}",
    )
    saved = save_task(task, request.revision)
    write_initial_article_artifacts(saved)
    write_seo_review_artifacts(saved, review_id)
    write_text_artifact(
        saved,
        f"seo-reviews/{review_index + 1:03d}-{review_id}-applied.md",
        saved.initial_article,
    )
    return saved


@app.post(
    "/api/tasks/{task_id}/seo-reviews/{review_id}/complete",
    response_model=TaskRecord,
)
def complete_seo_review_without_changes(
    task_id: str,
    review_id: str,
    request: SeoReviewFinalizeRequest,
) -> TaskRecord:
    task = get_task_or_404(task_id)
    review_index, review = seo_review_entry(task, review_id)
    ensure_review_editable(task, review)
    if any(change.decision == "accepted" for change in review.changes):
        raise HTTPException(
            status_code=422,
            detail="仍有已接受的修改；请先改为拒绝或暂不处理，再选择不修改正文完成审核。",
        )
    pending_count = sum(change.decision == "pending" for change in review.changes)
    if pending_count and not request.confirm_pending:
        raise HTTPException(
            status_code=409,
            detail="仍有未处理修改块，必须确认将其按“未处理”状态锁定。",
        )
    timestamp = now_iso()
    review.status = "completed"
    review.finalized_at = timestamp
    review.finalized_by = seo_review_operator()
    task.seo_reviews[review_index] = review
    saved = save_task(task, request.revision)
    write_seo_review_artifacts(saved, review_id)
    return saved


@app.put("/api/tasks/{task_id}/checks/initial-ai", response_model=TaskRecord)
def confirm_initial_ai(task_id: str, request: AICheckUpdateRequest) -> TaskRecord:
    task = get_task_or_404(task_id)
    require_action(task, ACTION_CONFIRM_INITIAL_AI)
    ensure_initial_metadata(task)
    issues = initial_readiness_issues(task)
    if issues:
        raise HTTPException(status_code=409, detail=" ".join(issues))
    validate_score(request.score)
    task.initial_ai_check = AICheck(
        confirmed=request.confirmed,
        score=request.score,
        report=request.report,
        screenshot_path=task.initial_ai_check.screenshot_path,
        confirmed_at=now_iso() if request.confirmed else "",
        article_hash=content_hash(task.initial_article),
    )
    task.zero_gpt_report = request.report
    if request.confirmed:
        advance(task, STATUS_INITIAL_AI_CHECKED)
        threshold = float(config().ai_pass_threshold)
        if request.score is not None and request.score < threshold:
            task.humanized_article = task.initial_article
            task.humanized_article_word_count = task.initial_article_word_count
            task.humanized_article_hash = task.initial_article_hash
            task.humanization_skipped = True
            task.article = task.initial_article
            task.final_ai_check = AICheck(
                confirmed=True,
                score=request.score,
                report=(
                    f"Initial AI rate {request.score:g}% was below the {threshold:g}% threshold; "
                    "humanization and the second AI check were skipped."
                ),
                screenshot_path=task.initial_ai_check.screenshot_path,
                confirmed_at=task.initial_ai_check.confirmed_at,
                article_hash=task.initial_article_hash,
            )
            advance(task, STATUS_HUMANIZED_READY)
            advance(task, STATUS_FINAL_AI_CHECKED)
            write_text_artifact(task, "04_humanized_candidate.md", task.initial_article)
            write_json_artifact(task, "05_zerogpt_after.json", task.final_ai_check.model_dump())
    write_json_artifact(task, "03_zerogpt_before.json", task.initial_ai_check.model_dump())
    return save_task(task, request.revision)


@app.post("/api/tasks/{task_id}/checks/{stage}-ai/screenshot", response_model=TaskRecord)
def upload_ai_rate_screenshot(
    task_id: str,
    stage: str,
    revision: int | None = None,
    file: UploadFile = File(...),
) -> TaskRecord:
    task = get_task_or_404(task_id)
    normalized_stage = stage.strip().lower()
    if normalized_stage == "initial":
        if not (task.initial_article or task.article).strip():
            raise HTTPException(status_code=409, detail="Save the first article before adding its AI-rate screenshot.")
        check = task.initial_ai_check
        artifact_name = "03_zerogpt_before.json"
    elif normalized_stage == "final":
        if not task.humanized_article.strip():
            raise HTTPException(status_code=409, detail="Save the humanized article before adding its AI-rate screenshot.")
        check = task.final_ai_check
        artifact_name = "05_zerogpt_after.json"
    else:
        raise HTTPException(status_code=422, detail="AI check stage must be 'initial' or 'final'.")

    content = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="AI-rate screenshot exceeds 25 MB.")
    try:
        output = save_ai_rate_screenshot(task, normalized_stage, content)
    except AIScreenshotError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    check.screenshot_path = str(output)
    task.delivery_package_path = ""
    write_json_artifact(task, artifact_name, check.model_dump(mode="json"))
    return save_task(task, revision)


@app.put("/api/tasks/{task_id}/zerogpt", response_model=TaskRecord)
def update_legacy_zero_gpt_report(task_id: str, request: ZeroGptReportRequest) -> TaskRecord:
    task = get_task_or_404(task_id)
    task.zero_gpt_report = request.report
    task.initial_ai_check.report = request.report
    write_text_artifact(task, "zerogpt_report.txt", request.report)
    return save_task(task, request.revision)


def perform_humanize(
    task_id: str,
    request: RevisionedRequest,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> TaskRecord:
    task = get_task_or_404(task_id)
    require_action(task, ACTION_HUMANIZE_ARTICLE)
    rehumanizing = (
        task.status in {STATUS_HUMANIZED_READY, STATUS_FINAL_AI_CHECKED}
        and bool(task.humanized_article)
    )
    source_article = (
        task.humanized_article
        if rehumanizing
        else task.initial_article
    )
    try:
        humanized = humanize_article(config(), task, source_article)
    except (
        ArticleStructureError,
        ArticleGenerationError,
        PromptTemplateError,
    ) as exc:
        set_stage_error(task, code="humanize_failed", message=str(exc), stage="humanize")
        if cancelled is None:
            save_task(task, request.revision)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    raise_if_batch_cancelled(cancelled)
    task.humanized_article = humanized
    task.humanization_skipped = False
    task.humanized_article_word_count = visible_word_count(humanized)
    task.humanized_article_hash = content_hash(humanized)
    task.article = humanized
    task.article_versions.append(
        version_record(
            "humanized",
            humanized,
            "humanized" if rehumanizing else "initial",
        )
    )
    task.workflow_error = None
    if task.status == STATUS_INITIAL_AI_CHECKED:
        advance(task, STATUS_HUMANIZED_READY)
    else:
        invalidate_downstream(task, "humanized_article")
    write_text_artifact(task, "04_humanized_candidate.md", humanized)
    write_text_artifact(
        task,
        "04_humanize_prompt.txt",
        build_humanize_prompt(config(), source_article),
    )
    return save_task(task, request.revision)


@app.post("/api/tasks/{task_id}/humanize", response_model=TaskRecord)
def humanize(
    task_id: str,
    request: RevisionedRequest | None = None,
) -> TaskRecord:
    return perform_humanize(task_id, request or RevisionedRequest())


@app.put("/api/tasks/{task_id}/humanized-article", response_model=TaskRecord)
def update_humanized_article(
    task_id: str, request: ArticleUpdateRequest
) -> TaskRecord:
    task = get_task_or_404(task_id)
    require_action(task, ACTION_UPDATE_HUMANIZED)
    source_article = task.initial_article or task.article
    if not source_article.strip():
        raise HTTPException(status_code=409, detail="The first article version is empty.")

    candidate = request.article.strip()
    if not candidate:
        raise HTTPException(status_code=422, detail="The humanized article cannot be empty.")

    required_phrases = [task.competitor_keyword or task.topic]
    required_phrases.extend(product.name for product in task.products if product.name)
    try:
        validate_humanized_article(
            source_article,
            candidate,
            required_phrases=required_phrases,
        )
    except ArticleStructureError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    task.humanized_article = candidate
    task.humanization_skipped = False
    task.humanized_article_word_count = visible_word_count(candidate)
    task.humanized_article_hash = content_hash(candidate)
    task.article = candidate
    invalidate_downstream(task, "humanized_article")
    task.article_versions.append(version_record("humanized", candidate, "external_manual"))
    write_text_artifact(task, "04_humanized_candidate.md", candidate)
    return save_task(task, request.revision)


@app.put("/api/tasks/{task_id}/checks/final-ai", response_model=TaskRecord)
def confirm_final_ai(task_id: str, request: AICheckUpdateRequest) -> TaskRecord:
    task = get_task_or_404(task_id)
    require_action(task, ACTION_CONFIRM_FINAL_AI)
    validate_score(request.score)
    task.final_ai_check = AICheck(
        confirmed=request.confirmed,
        score=request.score,
        report=request.report,
        screenshot_path=task.final_ai_check.screenshot_path,
        confirmed_at=now_iso() if request.confirmed else "",
        article_hash=content_hash(task.humanized_article),
    )
    if request.confirmed:
        advance(task, STATUS_FINAL_AI_CHECKED)
    write_json_artifact(task, "05_zerogpt_after.json", task.final_ai_check.model_dump())
    return save_task(task, request.revision)


def perform_restore_links(
    task_id: str,
    request: RevisionedRequest,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> TaskRecord:
    task = get_task_or_404(task_id)
    require_action(task, ACTION_RESTORE_LINKS)
    try:
        restored = restore_article_links(
            config(), task, task.initial_article, task.humanized_article
        )
    except LinkRestorationError as exc:
        set_stage_error(task, code="link_restore_failed", message=str(exc), stage="links")
        if cancelled is None:
            save_task(task, request.revision)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    raise_if_batch_cancelled(cancelled)

    task.linked_article = restored
    task.linked_article_word_count = visible_word_count(restored)
    task.linked_article_hash = content_hash(restored)
    task.article = restored
    expected_count = sum(link.count for link in task.source_links)
    task.link_validation = LinkValidation(
        passed=True,
        source_count=expected_count,
        preserved_count=expected_count,
        missing_links=[],
        unexpected_links=[],
        visible_text_unchanged=True,
        article_hash=content_hash(restored),
        verified_at=now_iso(),
    )
    task.article_versions.append(version_record("linked", restored, "humanized"))
    task.workflow_error = None
    advance(task, STATUS_LINKS_VERIFIED)
    write_text_artifact(task, "06_links_restored.md", restored)
    write_json_artifact(task, "06_link_validation.json", task.link_validation.model_dump())
    return save_task(task, request.revision)


@app.post("/api/tasks/{task_id}/restore-links", response_model=TaskRecord)
def restore_links(
    task_id: str,
    request: RevisionedRequest | None = None,
) -> TaskRecord:
    return perform_restore_links(task_id, request or RevisionedRequest())


@app.put("/api/tasks/{task_id}/images", response_model=TaskRecord)
def update_images(task_id: str, request: ImagesUpdateRequest) -> TaskRecord:
    task = get_task_or_404(task_id)
    require_action(task, ACTION_UPDATE_IMAGES)
    task.hero_image = request.hero_image.strip()
    if request.images is not None:
        task.images = request.images
    invalidate_downstream(task, "hero_image")
    # As with outline/article saves, reject a stale revision before touching
    # the artifact visible in the task directory.
    saved = save_task(task, request.revision)
    write_json_artifact(
        saved,
        "images.json",
        {
            "hero_image": saved.hero_image,
            "images": [image.model_dump() for image in saved.images],
        },
    )
    return saved


@app.post("/api/tasks/{task_id}/images/upload", response_model=TaskRecord)
def upload_image(
    task_id: str,
    role: str = "hero",
    revision: int | None = None,
    file: UploadFile = File(...),
) -> TaskRecord:
    task = get_task_or_404(task_id)
    require_action(task, ACTION_UPDATE_IMAGES)
    if role != "hero":
        raise HTTPException(status_code=422, detail="Only hero uploads are supported by this endpoint.")
    original_name = Path(file.filename or "hero-image").name
    stem = sanitize_image_stem(Path(original_name).stem, fallback="hero-image")
    suffix = Path(original_name).suffix.lower() or ".img"
    upload_dir = Path(task.task_dir) / "images" / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    destination = upload_dir / f"{stem}-{uuid4().hex[:8]}{suffix}"
    total = 0
    with destination.open("wb") as output:
        while chunk := file.file.read(1024 * 1024):
            total += len(chunk)
            if total > MAX_UPLOAD_BYTES:
                output.close()
                destination.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail="Image upload exceeds 25 MB.")
            output.write(chunk)
    task.hero_image = str(destination)
    invalidate_downstream(task, "hero_image")
    return save_task(task, revision)


@app.post("/api/tasks/{task_id}/products/image-upload", response_model=ApiMessage)
def upload_product_image(
    task_id: str,
    file: UploadFile = File(...),
) -> ApiMessage:
    task = get_task_or_404(task_id)
    require_action(task, ACTION_UPDATE_PRODUCTS)
    content = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Product image upload exceeds 25 MB.")
    if not content:
        raise HTTPException(status_code=422, detail="Product image is empty.")

    try:
        with Image.open(BytesIO(content)) as image:
            image_format = (image.format or "").upper()
            image.verify()
    except (UnidentifiedImageError, OSError, ValueError):
        raise HTTPException(status_code=422, detail="The uploaded file is not a valid image.") from None
    suffix = PRODUCT_IMAGE_SUFFIXES.get(image_format)
    if suffix is None:
        raise HTTPException(
            status_code=422,
            detail="Only JPEG, PNG, and WebP product images are supported.",
        )

    original_name = Path(file.filename or "product-image").name
    stem = sanitize_image_stem(Path(original_name).stem, fallback="product-image")
    task_dir = Path(task.task_dir)
    upload_dir = task_dir / "images" / "uploads" / "products"
    upload_dir.mkdir(parents=True, exist_ok=True)
    destination = upload_dir / f"{stem}-{uuid4().hex[:8]}{suffix}"
    destination.write_bytes(content)
    relative_path = destination.relative_to(task_dir).as_posix()
    return ApiMessage(
        message="Product image uploaded.",
        data={
            "image_path": relative_path,
            "filename": destination.name,
        },
    )


@app.get("/api/tasks/{task_id}/images/preview", response_class=FileResponse)
def preview_task_image(task_id: str, path: str) -> FileResponse:
    task = get_task_or_404(task_id)
    raw_path = path.strip()
    if not raw_path:
        raise HTTPException(status_code=422, detail="Image path is empty.")

    source = Path(raw_path).expanduser()
    if not source.is_absolute():
        source = Path(task.task_dir) / source
    try:
        resolved = source.resolve(strict=True)
    except (OSError, RuntimeError):
        raise HTTPException(status_code=404, detail=f"Image not found: {source}") from None

    task_root = Path(task.task_dir).resolve()
    try:
        resolved.relative_to(task_root)
    except ValueError:
        raise HTTPException(
            status_code=403,
            detail="Image preview is limited to the current task directory.",
        ) from None
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail=f"Image not found: {resolved}")
    if resolved.stat().st_size > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Image preview exceeds 25 MB.")

    try:
        with Image.open(resolved) as image:
            image_format = image.format or ""
            image.verify()
    except (UnidentifiedImageError, OSError, ValueError):
        raise HTTPException(status_code=422, detail="The selected file is not a valid image.") from None

    media_type = Image.MIME.get(image_format, "application/octet-stream")
    return FileResponse(
        resolved,
        media_type=media_type,
        headers={"Cache-Control": "no-store"},
    )


def perform_prepare_images(
    task_id: str,
    request: RevisionedRequest,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> TaskRecord:
    task = get_task_or_404(task_id)
    require_action(task, ACTION_PREPARE_IMAGES)
    article = task.linked_article or task.humanized_article
    try:
        prepared = prepare_task_images(task, article, require_hero=True)
        task.images = [ArticleImage.model_validate(item) for item in prepared]
        resolve_image_placements(article, task.images)
    except ImageAnchorRequiredError as exc:
        set_stage_error(
            task,
            code="image_prepare_failed",
            message=str(exc),
            stage="images",
        )
        if cancelled is None:
            save_task(task, request.revision)
        raise HTTPException(
            status_code=409,
            detail={"message": str(exc), "unresolved": exc.unresolved},
        ) from exc
    except ArticleImageError as exc:
        set_stage_error(
            task,
            code="image_prepare_failed",
            message=str(exc),
            stage="images",
        )
        if cancelled is None:
            save_task(task, request.revision)
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    raise_if_batch_cancelled(cancelled)

    task.final_article = article
    task.final_article_word_count = visible_word_count(article)
    task.final_article_hash = content_hash(article)
    task.article = article
    task.article_versions.append(version_record("final", article, "linked"))
    task.workflow_error = None
    advance(task, STATUS_IMAGES_READY)
    write_text_artifact(
        task,
        "07_final_with_images.md",
        build_image_audit_markdown(article, task.images),
    )
    write_json_artifact(task, "07_images.json", [image.model_dump() for image in task.images])
    return save_task(task, request.revision)


@app.post("/api/tasks/{task_id}/prepare-images", response_model=TaskRecord)
def prepare_images(
    task_id: str,
    request: RevisionedRequest | None = None,
) -> TaskRecord:
    return perform_prepare_images(task_id, request or RevisionedRequest())


def perform_export_docx(task_id: str, request: RevisionedRequest) -> TaskRecord:
    task = get_task_or_404(task_id)
    require_action(task, ACTION_EXPORT_DOCX)
    try:
        output = export_task_docx(config(), task)
    except (ArticleImageError, ArticleStructureError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    task.docx_path = str(output)
    task.delivery_package_path = ""
    task.workflow_error = None
    advance(task, STATUS_DOCX_EXPORTED)
    return save_task(task, request.revision)


@app.post("/api/tasks/{task_id}/export-docx", response_model=TaskRecord)
def export_docx(
    task_id: str,
    request: RevisionedRequest | None = None,
) -> TaskRecord:
    return perform_export_docx(task_id, request or RevisionedRequest())


def perform_generate_tdk(
    task_id: str,
    request: RevisionedRequest,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> TaskRecord:
    task = get_task_or_404(task_id)
    require_action(task, ACTION_GENERATE_TDK)
    try:
        metadata = generate_tdk_metadata(config(), task)
        raise_if_batch_cancelled(cancelled)
        output = export_tdk_docx(task, metadata)
    except TdkGenerationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    task.tdk = metadata
    task.tdk_path = str(output)
    task.delivery_package_path = ""
    task.workflow_error = None
    write_json_artifact(task, "08_tdk.json", metadata.model_dump(mode="json"))
    return save_task(task, request.revision)


@app.post("/api/tasks/{task_id}/generate-tdk", response_model=TaskRecord)
def generate_tdk(
    task_id: str,
    request: RevisionedRequest | None = None,
) -> TaskRecord:
    return perform_generate_tdk(task_id, request or RevisionedRequest())


def perform_package_delivery(task_id: str, request: RevisionedRequest) -> TaskRecord:
    task = get_task_or_404(task_id)
    require_action(task, ACTION_PACKAGE_DELIVERY)
    try:
        output = package_delivery(task)
    except DeliveryPackageError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    task.delivery_package_path = str(output)
    task.workflow_error = None
    return save_task(task, request.revision)


@app.post("/api/tasks/{task_id}/package-delivery", response_model=TaskRecord)
def build_delivery_package(
    task_id: str,
    request: RevisionedRequest | None = None,
) -> TaskRecord:
    return perform_package_delivery(task_id, request or RevisionedRequest())


@app.get("/api/tasks/{task_id}/delivery-package/download", response_class=FileResponse)
def download_delivery_package(task_id: str) -> FileResponse:
    task = get_task_or_404(task_id)
    try:
        archive = build_delivery_zip(task)
    except DeliveryPackageError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return FileResponse(
        archive,
        media_type="application/zip",
        filename=archive.name,
    )
