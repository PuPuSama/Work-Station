from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


SCHEMA_VERSION = 2

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
    score: float | None = None
    report: str = ""
    screenshot_path: str = ""
    confirmed_at: str = ""
    article_hash: str = ""


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
    source_path: str = ""
    prepared_path: str = ""
    filename: str = ""
    marker: str = ""
    product_name: str = ""
    product_url: str = ""
    anchor_heading: str = ""
    anchor_text: str = ""
    anchor_after: str = ""
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


class TaskRecord(WorkflowModel):
    # Storage/workflow metadata.
    schema_version: int = SCHEMA_VERSION
    revision: int = 0
    workflow_error: WorkflowError | None = None

    # Source task fields retained from schema v1.
    id: str
    week_folder: str
    customer: str
    topic_index: int
    topic: str
    competitor_keyword: str = ""
    competitor_blog: str = ""
    status: str = STATUS_NEW
    task_dir: str
    title_candidates: list[str] = Field(default_factory=list)
    selected_title: str = ""
    outline: str = ""

    # `article` remains the compatibility mirror used by the v1 API/exporter.
    article: str = ""
    raw_draft_article: str = ""
    initial_article: str = ""
    humanized_article: str = ""
    linked_article: str = ""
    final_article: str = ""
    article_versions: list[ArticleVersion] = Field(default_factory=list)

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

    products: list[Product] = Field(default_factory=list)
    hero_image: str = ""
    images: list[ArticleImage] = Field(default_factory=list)
    transition_added: bool = False

    initial_ai_check: AICheck = Field(default_factory=AICheck)
    final_ai_check: AICheck = Field(default_factory=AICheck)
    source_links: list[SourceLink] = Field(default_factory=list)
    link_validation: LinkValidation = Field(default_factory=LinkValidation)

    docx_path: str = ""
    tdk: TdkMetadata = Field(default_factory=TdkMetadata)
    tdk_path: str = ""
    delivery_package_path: str = ""
    legacy_export: bool = False
    zero_gpt_report: str = ""
    created_at: str
    updated_at: str


class RevisionedRequest(WorkflowModel):
    revision: int | None = None


class SelectTitleRequest(RevisionedRequest):
    title: str


class OutlineUpdateRequest(RevisionedRequest):
    outline: str


class ArticleUpdateRequest(RevisionedRequest):
    article: str


class ProductsUpdateRequest(RevisionedRequest):
    products: list[Product]


class ZeroGptReportRequest(RevisionedRequest):
    report: str


class GenerateArticleRequest(RevisionedRequest):
    word_count: int | None = None


class AICheckUpdateRequest(RevisionedRequest):
    score: float | None = None
    report: str = ""
    confirmed: bool = True


class ImagesUpdateRequest(RevisionedRequest):
    hero_image: str = ""
    images: list[ArticleImage] = Field(default_factory=list)


class DashboardSummary(WorkflowModel):
    week_folder: str
    week_path: str
    customer_count: int
    task_count: int
    completed_count: int
    status_counts: dict[str, int]
    llm_ready: bool


class ApiMessage(WorkflowModel):
    message: str
    data: Any | None = None
