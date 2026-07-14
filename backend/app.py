from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from uuid import uuid4

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

from config import ROOT_DIR, load_config, public_config
from models import (
    AICheck,
    AICheckUpdateRequest,
    ApiMessage,
    ArticleImage,
    ArticleUpdateRequest,
    ArticleVersion,
    DashboardSummary,
    GenerateArticleRequest,
    ImagesUpdateRequest,
    LinkValidation,
    OutlineUpdateRequest,
    ProductsUpdateRequest,
    SelectTitleRequest,
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
    WorkflowError,
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
from services.delivery_package import DeliveryPackageError, package_delivery
from services.docx_export import export_task_docx
from services.generator import (
    ArticleGenerationError,
    PromptTemplateError,
    build_humanize_prompt,
    ensure_transition_before_first_h2,
    ensure_article_hyperlinks,
    generate_outline,
    generate_raw_article,
    generate_titles,
    humanize_article,
    restore_article_links,
    site_homepage,
)
from services.llm import LLMClient
from services.product_asset_pipeline import enrich_product_assets
from services.product_crawler import recommend_products
from services.tavily import TavilyClient
from services.tdk import TdkGenerationError, export_tdk_docx, generate_tdk_metadata
from services.task_files import TaskDirectoryError, open_task_directory
from services.topics import scan_topic_library
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
    transition_task,
)


load_dotenv(ROOT_DIR / ".env")
load_dotenv(ROOT_DIR / "backend" / ".env")


app = FastAPI(title="Article Workflow Agent", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
_PRODUCT_PROCESSING_GUARD = Lock()
_PRODUCT_PROCESSING_TASKS: set[str] = set()


def config():
    return load_config()


def store() -> TaskStore:
    return TaskStore(config())


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
    known_urls = {site_homepage(task.customer).rstrip("/")}
    known_urls.update(
        str(product.canonical_url or product.url).rstrip("/")
        for product in task.products
        if (not product.asset_status or product.detail_page_verified)
        and (product.canonical_url or product.url)
    )
    article_urls = {
        str(link.get("url") or "").rstrip("/")
        for link in extract_link_inventory(article)
    }
    known_urls.discard("")
    if known_urls and not (known_urls & article_urls):
        issues.append(
            "第一版未包含客户官网或已确认产品的 Markdown 超链接，后续无法按第一版恢复链接。"
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


@app.get("/api/dashboard", response_model=DashboardSummary)
def dashboard() -> DashboardSummary:
    cfg = config()
    tasks = store().load()
    current_week_tasks = [task for task in tasks if task.week_folder == cfg.current_week_folder]
    status_counts = dict(Counter(task.status for task in current_week_tasks))
    return DashboardSummary(
        week_folder=cfg.current_week_folder,
        week_path=str(cfg.current_week_path),
        customer_count=len({task.customer for task in current_week_tasks}),
        task_count=len(current_week_tasks),
        completed_count=sum(1 for task in current_week_tasks if task.status == STATUS_DOCX_EXPORTED),
        status_counts=status_counts,
        llm_ready=LLMClient(cfg).ready,
    )


@app.post("/api/init-week", response_model=ApiMessage)
def init_week() -> ApiMessage:
    cfg = config()
    scanned = scan_topic_library(cfg)
    tasks = store().upsert_many(scanned)
    current_week_count = sum(1 for task in tasks if task.week_folder == cfg.current_week_folder)
    return ApiMessage(
        message=f"Initialized {current_week_count} tasks for {cfg.current_week_folder}.",
        data={"week_folder": cfg.current_week_folder, "task_count": current_week_count},
    )


@app.get("/api/tasks", response_model=list[TaskRecord])
def list_tasks(customer: str | None = None, status: str | None = None) -> list[TaskRecord]:
    cfg = config()
    tasks = [task for task in store().load() if task.week_folder == cfg.current_week_folder]
    if customer:
        tasks = [task for task in tasks if task.customer == customer]
    if status:
        tasks = [task for task in tasks if task.status == status]
    return [expose_task(task) for task in sorted(tasks, key=lambda task: (task.customer, task.topic_index))]


@app.get("/api/tasks/{task_id}", response_model=TaskRecord)
def read_task(task_id: str) -> TaskRecord:
    return expose_task(get_task_or_404(task_id))


@app.post("/api/tasks/{task_id}/open-folder", response_model=ApiMessage)
def open_task_folder(task_id: str) -> ApiMessage:
    task = get_task_or_404(task_id)
    try:
        directory = open_task_directory(config(), task)
    except TaskDirectoryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ApiMessage(
        message="Task directory opened.",
        data={"path": str(directory)},
    )


@app.post("/api/tasks/{task_id}/titles", response_model=TaskRecord)
def create_titles(task_id: str) -> TaskRecord:
    task = get_task_or_404(task_id)
    require_action(task, ACTION_GENERATE_TITLES)
    task.title_candidates = generate_titles(config(), task)
    invalidate_downstream(task, "titles")
    task.status = STATUS_TITLES_READY
    write_text_artifact(
        task,
        "title_candidates.md",
        "\n".join(f"{index + 1}. {title}" for index, title in enumerate(task.title_candidates)),
    )
    return save_task(task)


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


@app.post("/api/tasks/{task_id}/products/auto", response_model=TaskRecord)
def auto_products(task_id: str, limit: int = 3) -> TaskRecord:
    with product_processing(task_id):
        task = get_task_or_404(task_id)
        _require_workflow_action(task, ACTION_UPDATE_PRODUCTS)
        cfg = config()
        try:
            discovered = recommend_products(
                cfg,
                task,
                max(1, min(limit, 3)),
                tavily_client=TavilyClient(timeout=15),
                download_images=False,
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Auto product discovery failed: {exc}") from exc
        if not discovered:
            raise HTTPException(
                status_code=422,
                detail="No verified official product detail pages were found; existing products were kept.",
            )
        try:
            task.products = enrich_product_assets(
                cfg,
                task,
                discovered,
                llm=LLMClient(cfg, timeout_seconds=60),
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Official product asset crawl failed: {exc}") from exc
        invalidate_downstream(task, "products")
        saved = save_task(task)
        write_json_artifact(
            saved,
            "products.json",
            [product.model_dump() for product in saved.products],
        )
        return saved


@app.post("/api/tasks/{task_id}/products/assets", response_model=TaskRecord)
def refresh_product_assets(task_id: str) -> TaskRecord:
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
        saved = save_task(task)
        write_json_artifact(
            saved,
            "products.json",
            [product.model_dump() for product in saved.products],
        )
        return saved


@app.post("/api/tasks/{task_id}/outline", response_model=TaskRecord)
def create_outline(task_id: str) -> TaskRecord:
    task = get_task_or_404(task_id)
    require_action(task, ACTION_GENERATE_OUTLINE)
    task.outline = generate_outline(config(), task)
    task.status = STATUS_OUTLINE_READY
    task.workflow_error = None
    write_text_artifact(task, "outline.md", task.outline)
    return save_task(task)


@app.put("/api/tasks/{task_id}/outline", response_model=TaskRecord)
def update_outline(task_id: str, request: OutlineUpdateRequest) -> TaskRecord:
    task = get_task_or_404(task_id)
    require_action(task, ACTION_UPDATE_OUTLINE)
    task.outline = request.outline.strip()
    if not task.outline:
        raise HTTPException(status_code=422, detail="Outline cannot be empty.")
    invalidate_downstream(task, "outline")
    advance(task, STATUS_OUTLINE_CONFIRMED)
    write_text_artifact(task, "outline.md", task.outline)
    return save_task(task, request.revision)


@app.post("/api/tasks/{task_id}/article", response_model=TaskRecord)
def create_article(task_id: str, request: GenerateArticleRequest) -> TaskRecord:
    task = get_task_or_404(task_id)
    require_action(task, ACTION_GENERATE_ARTICLE)
    cfg = config()
    client = LLMClient(cfg)
    original_revision = task.revision

    raw_article = generate_raw_article(cfg, task, request.word_count, llm=client)
    task.raw_draft_article = raw_article
    task.raw_draft_word_count = visible_word_count(raw_article)
    task.raw_draft_hash = content_hash(raw_article)
    write_text_artifact(task, "01_raw_draft.md", raw_article)

    try:
        transition_was_present = has_intro_transition(raw_article)
        prepared = ensure_transition_before_first_h2(cfg, task, raw_article, llm=client)
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
    task.transition_added = not transition_was_present
    task.source_links = source_links(initial)
    task.article_versions.extend(
        [version_record("raw_draft", raw_article), version_record("initial", initial, "raw_draft")]
    )
    task.compression = {
        "required": False,
        "attempted_at": "",
        "before_words": task.initial_article_word_count,
        "after_words": task.initial_article_word_count,
        "prompt_version": "disabled",
    }
    task.workflow_error = None
    advance(task, STATUS_DRAFT_READY)
    write_text_artifact(task, "02_initial_article.md", initial)
    write_json_artifact(task, "02_initial_links.json", [link.model_dump() for link in task.source_links])
    return save_task(task, request.revision if request.revision is not None else original_revision)


@app.put("/api/tasks/{task_id}/article", response_model=TaskRecord)
def update_article(task_id: str, request: ArticleUpdateRequest) -> TaskRecord:
    task = get_task_or_404(task_id)
    require_action(task, ACTION_UPDATE_ARTICLE)
    cfg = config()
    client = LLMClient(cfg)
    try:
        prepared = ensure_transition_before_first_h2(cfg, task, request.article, llm=client)
        initial = ensure_article_hyperlinks(prepared, task)
        validate_article_layout(initial)
    except (
        ArticleStructureError,
        ArticleGenerationError,
        PromptTemplateError,
    ) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    task.initial_article = initial
    task.initial_article_word_count = visible_word_count(initial)
    task.initial_article_hash = content_hash(initial)
    task.article = initial
    invalidate_downstream(task, "initial_article")
    task.source_links = source_links(initial)
    task.transition_added = has_intro_transition(initial)
    task.article_versions.append(version_record("initial", initial, "manual_edit"))
    write_text_artifact(task, "02_initial_article.md", initial)
    write_json_artifact(task, "02_initial_links.json", [link.model_dump() for link in task.source_links])
    return save_task(task, request.revision)


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
    write_json_artifact(task, "03_zerogpt_before.json", task.initial_ai_check.model_dump())
    return save_task(task, request.revision)


@app.post("/api/tasks/{task_id}/checks/{stage}-ai/screenshot", response_model=TaskRecord)
def upload_ai_rate_screenshot(
    task_id: str,
    stage: str,
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
    return save_task(task)


@app.put("/api/tasks/{task_id}/zerogpt", response_model=TaskRecord)
def update_legacy_zero_gpt_report(task_id: str, request: ZeroGptReportRequest) -> TaskRecord:
    task = get_task_or_404(task_id)
    task.zero_gpt_report = request.report
    task.initial_ai_check.report = request.report
    write_text_artifact(task, "zerogpt_report.txt", request.report)
    return save_task(task, request.revision)


@app.post("/api/tasks/{task_id}/humanize", response_model=TaskRecord)
def humanize(task_id: str) -> TaskRecord:
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
        save_task(task)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    task.humanized_article = humanized
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
    return save_task(task)


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


@app.post("/api/tasks/{task_id}/restore-links", response_model=TaskRecord)
def restore_links(task_id: str) -> TaskRecord:
    task = get_task_or_404(task_id)
    require_action(task, ACTION_RESTORE_LINKS)
    try:
        restored = restore_article_links(
            config(), task, task.initial_article, task.humanized_article
        )
    except LinkRestorationError as exc:
        set_stage_error(task, code="link_restore_failed", message=str(exc), stage="links")
        save_task(task)
        raise HTTPException(status_code=422, detail=str(exc)) from exc

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
    return save_task(task)


@app.put("/api/tasks/{task_id}/images", response_model=TaskRecord)
def update_images(task_id: str, request: ImagesUpdateRequest) -> TaskRecord:
    task = get_task_or_404(task_id)
    require_action(task, ACTION_UPDATE_IMAGES)
    task.hero_image = request.hero_image.strip()
    if request.images:
        task.images = request.images
    invalidate_downstream(task, "hero_image")
    write_json_artifact(
        task,
        "images.json",
        {"hero_image": task.hero_image, "images": [image.model_dump() for image in task.images]},
    )
    return save_task(task, request.revision)


@app.post("/api/tasks/{task_id}/images/upload", response_model=TaskRecord)
def upload_image(task_id: str, role: str = "hero", file: UploadFile = File(...)) -> TaskRecord:
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
    return save_task(task)


@app.post("/api/tasks/{task_id}/prepare-images", response_model=TaskRecord)
def prepare_images(task_id: str) -> TaskRecord:
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
        save_task(task)
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
        save_task(task)
        raise HTTPException(status_code=422, detail=str(exc)) from exc

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
    return save_task(task)


@app.post("/api/tasks/{task_id}/export-docx", response_model=TaskRecord)
def export_docx(task_id: str) -> TaskRecord:
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
    return save_task(task)


@app.post("/api/tasks/{task_id}/generate-tdk", response_model=TaskRecord)
def generate_tdk(task_id: str) -> TaskRecord:
    task = get_task_or_404(task_id)
    require_action(task, ACTION_GENERATE_TDK)
    try:
        metadata = generate_tdk_metadata(config(), task)
        output = export_tdk_docx(task, metadata)
    except TdkGenerationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    task.tdk = metadata
    task.tdk_path = str(output)
    task.delivery_package_path = ""
    task.workflow_error = None
    write_json_artifact(task, "08_tdk.json", metadata.model_dump(mode="json"))
    return save_task(task)


@app.post("/api/tasks/{task_id}/package-delivery", response_model=TaskRecord)
def build_delivery_package(task_id: str) -> TaskRecord:
    task = get_task_or_404(task_id)
    require_action(task, ACTION_PACKAGE_DELIVERY)
    try:
        output = package_delivery(task)
    except DeliveryPackageError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    task.delivery_package_path = str(output)
    task.workflow_error = None
    return save_task(task)
