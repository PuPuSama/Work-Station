from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


SCHEMA_VERSION = 6

STATUS_NEW = "new"
STATUS_TITLES_READY = "titles_ready"
STATUS_TITLE_SELECTED = "title_selected"
STATUS_OUTLINE_READY = "outline_ready"
STATUS_OUTLINE_CONFIRMED = "outline_confirmed"
STATUS_DRAFT_READY = "draft_ready"
STATUS_INITIAL_AI_CHECKED = "initial_ai_checked"
STATUS_HUMANIZED_READY = "humanized_ready"
STATUS_FINAL_AI_CHECKED = "final_ai_checked"
STATUS_LINKS_VERIFIED = "links_verified"
STATUS_IMAGES_READY = "images_ready"
STATUS_DOCX_EXPORTED = "docx_exported"

WORKFLOW_STATUSES = (
    STATUS_NEW,
    STATUS_TITLES_READY,
    STATUS_TITLE_SELECTED,
    STATUS_OUTLINE_READY,
    STATUS_OUTLINE_CONFIRMED,
    STATUS_DRAFT_READY,
    STATUS_INITIAL_AI_CHECKED,
    STATUS_HUMANIZED_READY,
    STATUS_FINAL_AI_CHECKED,
    STATUS_LINKS_VERIFIED,
    STATUS_IMAGES_READY,
    STATUS_DOCX_EXPORTED,
)


class WorkflowModel(BaseModel):
    """Base model which preserves fields written by newer or older clients."""

    model_config = ConfigDict(extra="allow", validate_assignment=True)


class Product(WorkflowModel):
    product_id: str = ""
    name: str = ""
    url: str = ""
    canonical_url: str = ""
    image_path: str = ""
    description: str = ""
    reference_summary: str = ""
    reference_facts: list[str] = Field(default_factory=list)
    specifications: dict[str, str] = Field(default_factory=dict)
    reference_path: str = ""
    asset_manifest_path: str = ""
    asset_count: int = 0
    selected_asset_id: str = ""
    selection_confidence: float | None = None
    selection_reason: str = ""
    discovery_source: str = ""
    detail_page_verified: bool = False
    asset_status: str = ""
    asset_error: str = ""


class OfficialLink(WorkflowModel):
    """One current published official-site link pinned for article writing."""

    source_id: str = ""
    snapshot_id: str = ""
    label: str = ""
    url: str = ""
    role: str = "other"


class ArticleVersion(WorkflowModel):
    kind: str
    content: str = ""
    word_count: int = 0
    content_hash: str = ""
    created_at: str = ""
    source_kind: str = ""
    source_hash: str = ""
    prompt_version: str = ""


class AICheck(WorkflowModel):
    """A manual AI-rate check bound to the exact article that was checked."""

    confirmed: bool = False
    deferred: bool = False
    score: float | None = None
    report: str = ""
    # ``provider``/``checked_at`` distinguish an automatic detector result
    # from the retained screenshot-based human confirmation flow.
    provider: str = ""
    checked_at: str = ""
    screenshot_path: str = ""
    screenshot_asset_id: str = ""
    screenshot_content_hash: str = ""
    screenshot_filename: str = ""
    screenshot_width: int | None = None
    screenshot_height: int | None = None
    confirmed_at: str = ""
    article_hash: str = ""


KnowledgeCoverageStatus = Literal[
    "not_checked",
    "available",
    "stale",
    "unavailable",
]


class KnowledgeCoverageCheck(WorkflowModel):
    """Sentence-level project-knowledge support bound to visible article copy."""

    status: KnowledgeCoverageStatus = "not_checked"
    eligible_sentences: int = Field(default=0, ge=0)
    supported_sentences: int = Field(default=0, ge=0)
    sentence_coverage: float = Field(default=0.0, ge=0.0, le=1.0)
    hard_fact_sentences: int = Field(default=0, ge=0)
    supported_hard_fact_sentences: int = Field(default=0, ge=0)
    hard_fact_coverage: float = Field(default=1.0, ge=0.0, le=1.0)
    evidence_link_count: int = Field(default=0, ge=0)
    unsupported_sentence_examples: list[str] = Field(default_factory=list)
    content_hash: str = ""
    provider: str = ""
    checked_at: str = ""
    message: str = ""


class SourceLink(WorkflowModel):
    anchor: str = ""
    url: str = ""
    count: int = 1
    heading: str = ""
    context: str = ""


class LinkValidation(WorkflowModel):
    passed: bool = False
    source_count: int = 0
    preserved_count: int = 0
    missing_links: list[SourceLink] = Field(default_factory=list)
    unexpected_links: list[SourceLink] = Field(default_factory=list)
    visible_text_unchanged: bool | None = None
    article_hash: str = ""
    verified_at: str = ""
    error: str = ""


class ArticleImage(WorkflowModel):
    """A source image plus its eventual WebP placement in the article."""

    id: str = ""
    role: str = "product"
    # Server records use immutable object identities. Path fields remain only
    # for decoding historical payloads and stay empty in active writes.
    source_path: str = ""
    prepared_path: str = ""
    source_asset_id: str = ""
    prepared_asset_id: str = ""
    prepared_content_hash: str = ""
    width: int | None = None
    height: int | None = None
    filename: str = ""
    marker: str = ""
    product_name: str = ""
    product_id: str = ""
    product_url: str = ""
    anchor_heading: str = ""
    anchor_text: str = ""
    anchor_after: str = ""
    # Placement diagnostics are persisted explicitly so a later rendering
    # refactor can reproduce why a product image was attached to this block.
    anchor_line: int | None = None
    anchor_match: str = ""
    status: str = "pending"
    error: str = ""


class WorkflowError(WorkflowModel):
    code: str
    message: str = ""
    stage: str = ""
    occurred_at: str = ""
    recoverable: bool = True
    blocking: bool = True
    details: dict[str, Any] = Field(default_factory=dict)


class TdkMetadata(WorkflowModel):
    title: str = ""
    description: str = ""
    keywords: list[str] = Field(default_factory=list)
    description_character_count: int = 0
    source_article_hash: str = ""
    generated_at: str = ""
    prompt_version: str = "tdk-v1"


PromptKind = Literal["outline", "article", "review", "humanize"]


class PromptLibraryItem(WorkflowModel):
    id: str
    customer: str
    name: str
    kind: PromptKind
    content: str
    version: int = 1
    use_count: int = 0
    active: bool = True
    created_at: str = ""
    updated_at: str = ""


class PromptDefaults(WorkflowModel):
    customer: str
    default_outline_prompt_id: str = ""
    default_article_prompt_id: str = ""
    default_review_prompt_id: str = ""


class ProjectPromptLibrary(WorkflowModel):
    prompts: list[PromptLibraryItem] = Field(default_factory=list)
    defaults: PromptDefaults


class PromptSnapshot(WorkflowModel):
    prompt_id: str = ""
    name: str = "系统默认"
    kind: PromptKind
    content: str = ""
    version: int = 0
    source: Literal["system", "project_default", "library"] = "system"
    captured_at: str = ""


class PromptCreateRequest(WorkflowModel):
    name: str = Field(min_length=1, max_length=120)
    kind: PromptKind
    content: str = Field(min_length=1, max_length=40000)


class PromptUpdateRequest(WorkflowModel):
    name: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=40000)


class PromptActiveUpdateRequest(WorkflowModel):
    active: bool


class PromptDefaultsUpdateRequest(WorkflowModel):
    default_outline_prompt_id: str = ""
    default_article_prompt_id: str = ""
    default_review_prompt_id: str = ""


class PromptPreviewRequest(WorkflowModel):
    kind: PromptKind
    selection: str = "project_default"
    supplemental_prompt: str = Field(default="", max_length=40000)
    include_project_introduction: bool = True
    include_project_notes: bool = True
    include_topic_notes: bool = True


class PromptPreview(WorkflowModel):
    snapshot: PromptSnapshot
    effective_prompt: str


class SeoReviewDimension(WorkflowModel):
    key: str
    name: str
    score: float = Field(ge=0, le=10)
    target_score: float = Field(ge=0, le=10)
    main_issue: str = ""
    needs_revision: bool = False


SeoReviewChangeOperation = Literal["replace", "insert_after", "delete", "structure"]
SeoReviewChangeDecision = Literal["pending", "accepted", "rejected"]
SeoReviewRunStatus = Literal["open", "applied", "completed"]


class SeoReviewRisk(WorkflowModel):
    kind: Literal["number", "url", "brand", "product"]
    label: str
    before: str = ""
    after: str = ""
    message: str = ""


class SeoReviewChange(WorkflowModel):
    id: str
    operation: SeoReviewChangeOperation
    dimension_key: str = ""
    title: str
    rationale: str = ""
    target_text: str = ""
    model_proposed_text: str = ""
    reviewed_text: str = ""
    source_start: int = -1
    source_end: int = -1
    hard_problem: bool = False
    applicable: bool = True
    validation_errors: list[str] = Field(default_factory=list)
    risks: list[SeoReviewRisk] = Field(default_factory=list)
    decision: SeoReviewChangeDecision = "pending"
    decided_at: str = ""
    decided_by: str = ""
    risk_confirmed: bool = False
    risk_confirmed_at: str = ""
    updated_at: str = ""
    raw_payload: Any = None


class SeoReviewRun(WorkflowModel):
    id: str
    source_article: str
    source_article_hash: str
    source_revision: int
    score: float = Field(ge=0, le=100)
    dimensions: list[SeoReviewDimension] = Field(default_factory=list)
    publish_ready: bool = False
    publish_recommendation: str = ""
    report: str
    changes: list[SeoReviewChange] = Field(default_factory=list)
    status: SeoReviewRunStatus = "open"
    finalized_at: str = ""
    finalized_by: str = ""
    applied_article_hash: str = ""
    applied_revision: int | None = None
    # Compatibility fields for review records created by the earlier
    # whole-article revision prototype.
    revised_article: str = ""
    revised_article_hash: str = ""
    prompt_snapshot: PromptSnapshot
    primary_keyword: str = ""
    long_tail_keywords: list[str] = Field(default_factory=list)
    created_at: str


class TaskRecord(WorkflowModel):
    # Storage/workflow metadata.
    schema_version: int = SCHEMA_VERSION
    revision: int = 0
    workflow_error: WorkflowError | None = None

    # Source task fields retained from schema v1.
    id: str
    week_folder: str
    customer: str
    brand_name: str = ""
    project_introduction: str = ""
    project_notes: str = ""
    topic_notes: str = ""
    title_generation_instruction: str = ""
    outline_custom_prompt: str = ""
    article_custom_prompt: str = ""
    use_outline_custom_prompt: bool = False
    use_article_custom_prompt: bool = False
    outline_prompt_selection: str = "project_default"
    article_prompt_selection: str = "project_default"
    seo_review_prompt_selection: str = "project_default"
    last_outline_prompt_snapshot: PromptSnapshot | None = None
    last_article_prompt_snapshot: PromptSnapshot | None = None
    include_project_introduction: bool = True
    include_project_notes: bool = True
    include_topic_notes: bool = True
    source_key: str = ""
    source_kind: str = "xlsx"
    synced_from_task_id: str = ""
    synced_from_week: str = ""
    topic_index: int
    topic: str
    primary_keyword: str = ""
    competitor_keyword: str = ""
    competitor_blog: str = ""
    status: str = STATUS_NEW
    # Human bookkeeping marker kept separate from the generation workflow.
    manual_completed: bool = False
    manual_completed_at: str = ""
    manual_completed_by: str = ""
    task_dir: str
    title_candidates: list[str] = Field(default_factory=list)
    selected_title: str = ""
    outline: str = ""
    # Editable outline buffer. ``outline`` remains the last confirmed version
    # used for article generation, while this field may contain a newer draft.
    outline_draft: str = ""

    # `article` remains the compatibility mirror used by the v1 API/exporter.
    article: str = ""
    raw_draft_article: str = ""
    initial_article: str = ""
    humanized_article: str = ""
    humanization_skipped: bool = False
    linked_article: str = ""
    final_article: str = ""
    article_versions: list[ArticleVersion] = Field(default_factory=list)
    seo_primary_keyword: str = ""
    seo_long_tail_keywords: list[str] = Field(default_factory=list)
    seo_reviews: list[SeoReviewRun] = Field(default_factory=list)

    raw_draft_word_count: int = 0
    raw_draft_hash: str = ""
    initial_article_word_count: int = 0
    initial_article_hash: str = ""
    humanized_article_word_count: int = 0
    humanized_article_hash: str = ""
    linked_article_word_count: int = 0
    linked_article_hash: str = ""
    final_article_word_count: int = 0
    final_article_hash: str = ""

    product_candidate_ids: list[str] = Field(default_factory=list)
    product_candidate_reasons: dict[str, str] = Field(default_factory=dict)
    products: list[Product] = Field(default_factory=list)
    official_links: list[OfficialLink] = Field(default_factory=list)
    hero_image: str = ""
    images: list[ArticleImage] = Field(default_factory=list)
    transition_added: bool = False

    initial_ai_check: AICheck = Field(default_factory=AICheck)
    final_ai_check: AICheck = Field(default_factory=AICheck)
    knowledge_coverage: KnowledgeCoverageCheck = Field(
        default_factory=KnowledgeCoverageCheck
    )
    source_links: list[SourceLink] = Field(default_factory=list)
    link_validation: LinkValidation = Field(default_factory=LinkValidation)

    # Server records use an immutable private Asset identity. The historical
    # path field remains empty in active writes.
    docx_path: str = ""
    docx_asset_id: str = ""
    docx_content_hash: str = ""
    docx_filename: str = ""
    tdk: TdkMetadata = Field(default_factory=TdkMetadata)
    tdk_path: str = ""
    tdk_asset_id: str = ""
    tdk_content_hash: str = ""
    tdk_filename: str = ""
    delivery_package_path: str = ""
    delivery_package_asset_id: str = ""
    delivery_package_content_hash: str = ""
    delivery_package_filename: str = ""
    legacy_export: bool = False
    zero_gpt_report: str = ""
    created_at: str
    updated_at: str


class RevisionedRequest(WorkflowModel):
    revision: int | None = None


class SelectTitleRequest(RevisionedRequest):
    title: str


class ManualTitleGenerationRequest(WorkflowModel):
    topic: str = Field(min_length=1, max_length=500)
    instruction: str = Field(default="", max_length=4000)


class OutlineUpdateRequest(RevisionedRequest):
    outline: str
    confirmed: bool = True


class ArticleUpdateRequest(RevisionedRequest):
    article: str


class VersionRestoreRequest(RevisionedRequest):
    version_index: int = Field(ge=0)


class ProductsUpdateRequest(RevisionedRequest):
    products: list[Product]


class ZeroGptReportRequest(RevisionedRequest):
    report: str


class GenerateArticleRequest(RevisionedRequest):
    word_count: int | None = None
    custom_prompt: str | None = Field(default=None, max_length=40000)
    use_custom_prompt: bool | None = None
    include_project_introduction: bool | None = None
    include_project_notes: bool | None = None
    include_topic_notes: bool | None = None
    prompt_selection: str | None = None
    prompt_snapshot: PromptSnapshot | None = None


class GenerateOutlineRequest(RevisionedRequest):
    custom_prompt: str | None = Field(default=None, max_length=40000)
    use_custom_prompt: bool | None = None
    include_project_introduction: bool | None = None
    include_project_notes: bool | None = None
    include_topic_notes: bool | None = None
    prompt_selection: str | None = None
    prompt_snapshot: PromptSnapshot | None = None


class AutoProductsRequest(RevisionedRequest):
    limit: int = Field(default=3, ge=1, le=3)


class ProjectBrandUpdateRequest(WorkflowModel):
    brand_name: str = Field(default="", max_length=120)


class ProjectDomainUpdateRequest(WorkflowModel):
    new_domain: str = Field(min_length=1, max_length=253)


class AuthLoginRequest(WorkflowModel):
    password: str = Field(min_length=1, max_length=1024)


class ProjectContextUpdateRequest(WorkflowModel):
    project_introduction: str = Field(default="", max_length=30000)
    project_notes: str = Field(default="", max_length=30000)


class LlmSettingsUpdateRequest(WorkflowModel):
    model: str = Field(min_length=1, max_length=120)
    reasoning_effort: Literal["low", "medium", "high", "xhigh"]
    revision: int = Field(default=0, ge=0)


class WritingSettingsUpdateRequest(RevisionedRequest):
    topic_notes: str = Field(default="", max_length=30000)
    outline_custom_prompt: str = Field(default="", max_length=40000)
    article_custom_prompt: str = Field(default="", max_length=40000)
    use_outline_custom_prompt: bool = False
    use_article_custom_prompt: bool = False
    outline_prompt_selection: str = "project_default"
    article_prompt_selection: str = "project_default"
    include_project_introduction: bool = True
    include_project_notes: bool = True
    include_topic_notes: bool = True


class AICheckUpdateRequest(RevisionedRequest):
    score: float | None = None
    report: str = ""
    confirmed: bool = True
    deferred: bool = False


class SeoReviewSettingsUpdateRequest(RevisionedRequest):
    primary_keyword: str = Field(default="", max_length=240)
    long_tail_keywords: list[str] = Field(default_factory=list, max_length=30)
    prompt_selection: str = Field(default="project_default", max_length=128)


class SeoReviewRequest(SeoReviewSettingsUpdateRequest):
    prompt_snapshot: PromptSnapshot | None = None


class SeoReviewChangeUpdateRequest(RevisionedRequest):
    decision: SeoReviewChangeDecision = "pending"
    reviewed_text: str = Field(default="", max_length=40000)
    confirm_risks: bool = False


class SeoReviewPreviewRequest(RevisionedRequest):
    pass


class SeoReviewFinalizeRequest(RevisionedRequest):
    preview_hash: str = ""
    confirm_pending: bool = False


class SeoReviewPreview(WorkflowModel):
    review_id: str
    article: str
    article_hash: str
    accepted_change_ids: list[str] = Field(default_factory=list)
    pending_count: int = 0
    rejected_count: int = 0
    invalid_count: int = 0
    structure_valid: bool = True


class ImagesUpdateRequest(RevisionedRequest):
    hero_image: str = ""
    # ``None`` means the client only updates the hero path.  An explicit empty
    # list is meaningful and clears previously prepared/anchored images.
    images: list[ArticleImage] | None = None


class DashboardSummary(WorkflowModel):
    week_folder: str
    week_path: str
    customer_count: int
    task_count: int
    completed_count: int
    status_counts: dict[str, int]
    llm_ready: bool


BatchOperation = Literal[
    "titles",
    "products",
    "outline",
    "article",
    "rewrite_article",
    "seo_review",
    "humanize",
    "restore_links",
    "prepare_images",
    "export_docx",
    "generate_tdk",
    "package_delivery",
    "knowledge_research",
]
BatchJobStatus = Literal[
    "queued",
    "running",
    "retry_wait",
    "succeeded",
    "failed",
    "cancelled",
    "conflict",
]


class BatchCreateRequest(WorkflowModel):
    operation: BatchOperation
    task_ids: list[str] = Field(min_length=1, max_length=100)
    word_count: int | None = Field(default=None, ge=300, le=5000)


class BatchPreflightIssue(WorkflowModel):
    task_id: str
    message: str


class BatchJobRecord(WorkflowModel):
    id: str
    batch_id: str
    task_id: str
    requested_by_user_id: str | None = None
    customer: str
    topic_index: int
    topic: str
    operation: BatchOperation
    status: BatchJobStatus
    request: dict[str, Any] = Field(default_factory=dict)
    source_revision: int
    result_revision: int | None = None
    attempts: int = 0
    max_attempts: int = 4
    available_at: float = 0
    cancel_requested: bool = False
    error: str = ""
    created_at: str
    started_at: str = ""
    finished_at: str = ""
    updated_at: str


class BatchRecord(WorkflowModel):
    id: str
    operation: BatchOperation
    customer: str = ""
    status: Literal[
        "queued",
        "running",
        "succeeded",
        "cancelled",
        "completed_with_errors",
    ]
    total: int
    completed: int
    status_counts: dict[str, int] = Field(default_factory=dict)
    jobs: list[BatchJobRecord] = Field(default_factory=list)
    created_at: str
    updated_at: str


class BatchCreateResponse(WorkflowModel):
    batch: BatchRecord | None = None
    rejected: list[BatchPreflightIssue] = Field(default_factory=list)


class ApiMessage(WorkflowModel):
    message: str
    data: Any | None = None
