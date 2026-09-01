from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Literal

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.exc import SQLAlchemyError

from config import AppConfig
from knowledge_agent.object_storage import (
    KnowledgeObjectNotFound,
    ProjectKnowledgeObjectService,
)
from models import (
    STATUS_FINAL_AI_CHECKED,
    STATUS_HUMANIZED_READY,
    STATUS_INITIAL_AI_CHECKED,
    AICheck,
    SeoReviewChangeDecision,
    SeoReviewPreview,
    TaskRecord,
)
from services.article_validation import visible_word_count
from services.ai_rate_policy import apply_ai_rate_humanization_skip
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
from services.project_memberships import (
    PostgresProjectMembershipService,
    ProjectMembershipConflict,
    ProjectMembershipTargetUnavailable,
    ProjectMembershipUnavailable,
    ProjectOwnerRecord,
)
from services.project_deletion import (
    DeletedProject,
    PostgresProjectDeletionService,
    ProjectDeletionConflict,
    ProjectDeletionDenied,
    ProjectDeletionUnavailable,
)
from services.server_auth import SERVER_AUTH_COOKIE_NAME
from services.server_article_images import (
    ServerArticleImageAnchorRequired,
    ServerArticleImageError,
    ServerArticleImagePreparation,
)
from services.server_ai_screenshots import (
    MAX_SERVER_AI_SCREENSHOT_BYTES,
    ServerAiScreenshotError,
    ServerFinalAiScreenshotPreparation,
    ServerInitialAiScreenshotPreparation,
)
from services.server_docx_export import (
    ServerArticleDocxError,
    ServerArticleDocxExport,
)
from services.server_outline_update import (
    ServerOutlineUpdateError,
    ServerOutlineVersionNotFound,
    apply_reviewed_outline,
    restore_reviewed_outline_version,
)
from services.server_outline_generation import (
    OutlineGenerationUnavailable,
    ServerOutlineGenerationRegistry,
)
from services.server_title_generation import (
    ServerTitleGenerationRegistry,
    TitleGenerationUnavailable,
)
from services.server_article_generation import (
    ARTICLE_GENERATION_OPERATION,
    ARTICLE_REWRITE_OPERATION,
    ArticleGenerationUnavailable,
    ServerArticleGenerationRegistry,
)
from services.server_link_restoration import (
    LinkRestorationUnavailable,
    ServerLinkRestorationRegistry,
)
from services.server_humanized_update import (
    ServerHumanizedArticleError,
    apply_reviewed_humanized_article,
)
from services.server_humanize_generation import (
    HumanizeGenerationUnavailable,
    ServerHumanizeGenerationRegistry,
)
from services.server_knowledge_coverage import ServerKnowledgeCoverageService
from services.server_seo_review_settings import (
    ServerSeoReviewSettingsError,
    apply_server_seo_review_settings,
)
from services.server_seo_review_generation import (
    SeoReviewGenerationUnavailable,
    ServerSeoReviewGenerationRegistry,
)
from services.server_seo_review_commands import (
    ServerSeoReviewConflict,
    ServerSeoReviewNotFound,
    ServerSeoReviewValidationError,
    apply_server_seo_review,
    build_server_seo_review_preview,
    complete_server_seo_review,
    update_server_seo_review_change,
)
from services.server_project_prompts import (
    ServerProjectPromptError,
    ServerProjectPromptServiceFactory,
    ServerProjectPromptUnavailable,
)
from services.server_project_metadata import (
    PostgresServerProjectMetadata,
    ServerProjectCreationConflict,
    ServerProjectMetadata,
    ServerProjectMetadataConflict,
    ServerProjectMetadataUnavailable,
)
from services.server_project_catalog import PostgresServerProjectCatalog
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
from services.server_product_generation import (
    ProductGenerationUnavailable,
    ServerProductGenerationRegistry,
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
from services.server_task_intake import (
    ServerTaskIntakeConflict,
    ServerTaskIntakeResult,
    ServerTaskIntakeRow,
    ServerTaskIntakeUnavailable,
)
from services.server_task_writing_settings import (
    ServerTaskWritingSettings,
    ServerTaskWritingSettingsConflict,
    ServerTaskWritingSettingsError,
    ServerTaskWritingSettingsServiceFactory,
    ServerTaskWritingSettingsUnavailable,
)
from services.server_tdk_export import (
    ServerTdkDocxExport,
    ServerTdkError,
    ServerTdkUnavailable,
)
from services.server_task_workbook import (
    MAX_WORKBOOK_BYTES,
    ServerTaskWorkbookError,
    preview_task_workbook,
)
from services.zerogpt import ZeroGPTClient
from storage import RevisionConflictError, content_hash, now_iso
from workflow.state_machine import (
    ACTION_CONFIRM_FINAL_AI,
    ACTION_CONFIRM_INITIAL_AI,
    ACTION_DOWNLOAD_DOCX,
    ACTION_EXPORT_DOCX,
    ACTION_GENERATE_TDK,
    ACTION_PACKAGE_DELIVERY,
    ACTION_PREPARE_IMAGES,
    ACTION_REWRITE_FROM_SCRATCH,
    ACTION_SELECT_TITLE,
    ACTION_UPDATE_ARTICLE,
    ACTION_UPDATE_HUMANIZED,
    ACTION_UPDATE_OUTLINE,
    ACTION_UPDATE_PRODUCTS,
    WorkflowActionNotAllowed,
    allowed_actions,
    ensure_action_allowed,
    invalidate_downstream,
    reset_for_full_rewrite,
    transition_task,
)


class ProjectAssetDownload(BaseModel):
    asset_id: str
    url: str
    expires_seconds: int
    filename: str | None = None


class ProjectCatalogProductResponse(BaseModel):
    asset_count: int
    name: str
    product_id: str
    selected_asset_id: str


class ProjectCatalogImageAssetResponse(BaseModel):
    asset_id: str
    product_id: str
    byte_size: int
    content_type: str
    evidence_kind: str
    height: int | None
    label: str
    width: int | None


class ProjectCatalogResponse(BaseModel):
    image_assets: list[ProjectCatalogImageAssetResponse]
    products: list[ProjectCatalogProductResponse]


class ProjectCreateRequest(BaseModel):
    """Provision a Project under the signed-in Organization."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    customer_name: str = Field(min_length=1, max_length=120)
    official_domain: str = Field(min_length=1, max_length=253)
    owning_team_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
    )
    owner_user_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
    )


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


class ProjectKnowledgeCoverageApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ProjectKnowledgeCoverageEvidenceResponse(
    ProjectKnowledgeCoverageApiModel
):
    evidence_link_id: str
    chunk_id: str
    source_id: str
    snapshot_id: str
    source_name: str
    heading_path: list[str]
    source_kind: str
    trust_tier: str
    claim_type: str
    support_type: str
    excerpt: str
    canonical_url: str | None


class ProjectKnowledgeCoverageSentenceResponse(
    ProjectKnowledgeCoverageApiModel
):
    paragraph_id: str
    sentence_id: str
    text: str
    eligible: bool
    supported: bool
    hard_fact: bool
    evidence: list[ProjectKnowledgeCoverageEvidenceResponse]


class ProjectKnowledgeCoverageParagraphResponse(
    ProjectKnowledgeCoverageApiModel
):
    paragraph_id: str
    sentences: list[ProjectKnowledgeCoverageSentenceResponse]


class ProjectKnowledgeCoverageDetailResponse(
    ProjectKnowledgeCoverageApiModel
):
    task_id: str
    task_revision: int
    title: str
    status: Literal["not_checked", "available", "stale", "unavailable"]
    message: str
    checked_at: str
    eligible_sentences: int
    supported_sentences: int
    sentence_coverage: float
    hard_fact_sentences: int
    supported_hard_fact_sentences: int
    hard_fact_coverage: float
    evidence_link_count: int
    content_hash: str
    paragraphs: list[ProjectKnowledgeCoverageParagraphResponse]


class ArticleGenerationRequest(ProjectRevisionRequest):
    """Article generation options captured with the queued Job."""

    use_evidence_pack: bool = True


class ProjectTaskIntakeRowRequest(BaseModel):
    """One server-owned Task source row without identity or workflow fields."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )

    topic: str = Field(min_length=1, max_length=500)
    primary_keyword: str = Field(default="", max_length=500)
    competitor_keyword: str = Field(default="", max_length=500)
    competitor_blog: str = Field(default="", max_length=2048)


class ProjectTaskCreateRequest(ProjectTaskIntakeRowRequest):
    """Create one Task with a caller-stable idempotency identity."""

    intake_id: str = Field(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )


class ProjectTaskImportRequest(BaseModel):
    """Import normalized rows; raw workbooks never cross this boundary."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )

    intake_id: str = Field(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    source_name: str = Field(min_length=1, max_length=255)
    rows: list[ProjectTaskIntakeRowRequest] = Field(
        min_length=1,
        max_length=200,
    )


class ProjectTaskIntakeItemResponse(BaseModel):
    """Minimal new-Task projection for create/import confirmation."""

    id: str
    topic_index: int
    topic: str
    primary_keyword: str
    competitor_keyword: str
    competitor_blog: str
    status: str
    revision: int


class ProjectRecentTaskResponse(BaseModel):
    """Small projection for the article workspace's quick-jump list."""

    id: str
    topic_index: int
    topic: str
    selected_title: str
    status: str
    updated_at: str


class ProjectTaskIntakeResponse(BaseModel):
    intake_id: str
    intake_kind: Literal["manual", "row_import"]
    source_name: str
    source_digest: str
    created: bool
    tasks: list[ProjectTaskIntakeItemResponse]


class ProjectTaskWorkbookPreviewResponse(BaseModel):
    filename: str
    sheet_name: str
    headers: list[str]
    rows: list[list[str]]
    mapping: dict[str, int | None]
    truncated: bool


class ProjectMetadataResponse(BaseModel):
    """Project settings; authoritative facts still belong in Knowledge."""

    project_id: str
    customer_name: str
    official_domain: str
    project_notes: str
    revision: int
    owning_team_id: str | None = None
    owner_user_id: str | None = None


class ProjectMetadataUpdateRequest(BaseModel):
    """CAS update without accepting Project identity or arbitrary metadata."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )

    revision: int = Field(ge=0)
    customer_name: str = Field(min_length=1, max_length=120)
    official_domain: str = Field(min_length=1, max_length=253)
    project_notes: str = Field(default="", max_length=30000)


def _project_metadata_response(
    metadata: ServerProjectMetadata,
) -> ProjectMetadataResponse:
    return ProjectMetadataResponse(
        project_id=metadata.project_id,
        customer_name=metadata.customer_name,
        official_domain=metadata.official_domain,
        project_notes=metadata.project_notes,
        revision=metadata.revision,
        owning_team_id=metadata.owning_team_id,
        owner_user_id=metadata.owner_user_id,
    )


class ProjectSeoReviewSettingsRequest(BaseModel):
    """Save SEO Review inputs without accepting Prompt content or identity."""

    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=0)
    primary_keyword: str = Field(default="", max_length=240)
    long_tail_keywords: list[str] = Field(
        default_factory=list,
        max_length=30,
    )
    prompt_selection: str = Field(
        default="project_default",
        max_length=128,
    )


class ProjectSeoReviewChangeRequest(BaseModel):
    """Record one human decision without accepting Review identity in Body."""

    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=0)
    decision: SeoReviewChangeDecision = "pending"
    reviewed_text: str = Field(default="", max_length=40000)
    confirm_risks: bool = False


class ProjectSeoReviewApplyRequest(BaseModel):
    """Apply only the article returned by the latest exact preview."""

    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=0)
    preview_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirm_pending: bool = False


class ProjectSeoReviewCompleteRequest(BaseModel):
    """Finalize without changing the article."""

    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=0)
    confirm_pending: bool = False


class ProjectTitleSelectionRequest(BaseModel):
    """Select a server-owned candidate or save a caller-entered title."""

    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=0)
    candidate_index: int | None = Field(default=None, ge=0, le=99)
    title: str | None = Field(default=None, min_length=1, max_length=300)

    @model_validator(mode="after")
    def require_exactly_one_title_source(self) -> "ProjectTitleSelectionRequest":
        if (self.candidate_index is None) == (self.title is None):
            raise ValueError(
                "Provide exactly one of candidate_index or title."
            )
        return self


class ProjectTaskCompletionRequest(BaseModel):
    """Update the human completion marker without changing workflow status."""

    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=0)
    completed: bool


class ProjectOutlineUpdateRequest(BaseModel):
    """Save one reviewed outline without accepting workflow or audit fields."""

    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=0)
    outline: str = Field(min_length=1, max_length=40000)
    confirmed: bool = True


class ProjectTaskWritingSettingsRequest(BaseModel):
    """Replace the complete Server writing-settings projection under CAS."""

    model_config = ConfigDict(extra="forbid", strict=True)

    revision: int = Field(ge=0)
    topic_notes: str = Field(max_length=30000)
    outline_custom_prompt: str = Field(max_length=40000)
    article_custom_prompt: str = Field(max_length=40000)
    use_outline_custom_prompt: bool
    use_article_custom_prompt: bool
    outline_prompt_selection: str = Field(max_length=255)
    article_prompt_selection: str = Field(max_length=255)
    include_project_introduction: bool
    include_project_notes: bool
    include_topic_notes: bool

    def to_settings(self) -> ServerTaskWritingSettings:
        values = self.model_dump(exclude={"revision", "kind"})
        return ServerTaskWritingSettings(**values)


class ProjectTaskWritingSettingsPreviewRequest(
    ProjectTaskWritingSettingsRequest
):
    """Preview one Server prompt without accepting a caller-owned snapshot."""

    kind: Literal["outline", "article"]


class ProjectTaskPromptSnapshotResponse(BaseModel):
    """Safe Prompt identity without separately returning content or hashes."""

    model_config = ConfigDict(extra="forbid")

    prompt_id: str
    name: str
    kind: Literal["outline", "article"]
    version: int
    source: Literal["system", "project_default", "library"]
    captured_at: str


class ProjectTaskWritingSettingsPreviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    task_id: str
    task_revision: int
    kind: Literal["outline", "article"]
    prompt_snapshot: ProjectTaskPromptSnapshotResponse
    effective_prompt: str
    context_chunk_count: int
    target_words: int
    warnings: list[str]


class ProjectOutlineVersionRestoreRequest(BaseModel):
    """Restore only a server-owned outline Version into the draft field."""

    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=0)
    version_index: int = Field(ge=0, le=9999)


class ProjectMembershipUpdateRequest(BaseModel):
    """Legacy compatibility body; projects now have one owner only."""

    model_config = ConfigDict(extra="forbid")

    role: Literal["editor"]


class ProjectOwnerUpdateRequest(BaseModel):
    """Assign one active member or leave the project pending assignment."""

    model_config = ConfigDict(extra="forbid")

    owner_user_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
    )


class ProjectMembershipResponse(BaseModel):
    user_id: str
    role: Literal["editor", "reviewer", "viewer"]


class ProjectMembershipRevokeResponse(BaseModel):
    user_id: str
    revoked: bool


class ProjectMembershipListItemResponse(BaseModel):
    user_id: str
    display_name: str
    status: Literal["active", "disabled"]
    role: Literal["editor", "reviewer", "viewer"]


class ProjectMembershipListResponse(BaseModel):
    items: list[ProjectMembershipListItemResponse]
    next_after_user_id: str | None = None


class ProjectMembershipCandidateResponse(BaseModel):
    user_id: str
    display_name: str


class ProjectMembershipCandidateListResponse(BaseModel):
    items: list[ProjectMembershipCandidateResponse]
    next_after_user_id: str | None = None


class ProjectOwnerResponse(BaseModel):
    project_id: str
    owning_team_id: str | None
    owner_user_id: str | None
    assignment_status: Literal["assigned", "pending"]


class ProjectDeletionResponse(BaseModel):
    project_id: str
    deleted: bool
    cancelled_job_count: int
    deleted_row_count: int


def _project_owner_response(record: ProjectOwnerRecord) -> ProjectOwnerResponse:
    return ProjectOwnerResponse(
        project_id=record.project_id,
        owning_team_id=record.owning_team_id,
        owner_user_id=record.owner_user_id,
        assignment_status=(
            "assigned" if record.owner_user_id is not None else "pending"
        ),
    )


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
    deferred: bool = False

    @model_validator(mode="after")
    def validate_review_state(self) -> "FinalAiCheckUpdateRequest":
        if self.confirmed and self.deferred:
            raise ValueError("AI review cannot be confirmed and deferred together")
        if self.deferred and self.score is not None:
            raise ValueError("a deferred AI review cannot record a score")
        return self


class InitialAiCheckUpdateRequest(FinalAiCheckUpdateRequest):
    """Bind a manual initial AI review to the current first draft."""

    @model_validator(mode="after")
    def require_score_for_confirmation(self) -> "InitialAiCheckUpdateRequest":
        if self.confirmed and self.score is None:
            raise ValueError("AI-rate score is required when confirming")
        return self


class ReviewedHumanizedArticleRequest(BaseModel):
    """Save externally reviewed humanized Markdown under Task CAS."""

    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=0)
    article: str = Field(min_length=1, max_length=200000)
    recheck_ai_rate: bool = False


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
    product_asset_ids: dict[str, str] = Field(
        default_factory=dict,
        max_length=3,
    )
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

    @field_validator("product_asset_ids")
    @classmethod
    def validate_product_asset_ids(
        cls,
        value: dict[str, str],
    ) -> dict[str, str]:
        normalized = {
            product_id.strip(): asset_id.strip()
            for product_id, asset_id in value.items()
        }
        if (
            len(normalized) != len(value)
            or any(
                not product_id or not asset_id
                for product_id, asset_id in normalized.items()
            )
        ):
            raise ValueError(
                "product image choices require unique product and asset ids"
            )
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


class OutlineGenerationJobResponse(ProductRediscoveryJobResponse):
    """Public outline Job state; private Prompt and Chunk input stay hidden."""


class TitleGenerationJobResponse(ProductRediscoveryJobResponse):
    """Public title Job state; private Template and Chunk input stay hidden."""


class ProductGenerationJobResponse(ProductRediscoveryJobResponse):
    """Public product Job state; private catalog bindings stay hidden."""


class ArticleGenerationJobResponse(ProductRediscoveryJobResponse):
    """Public article Job state; private Prompt and Chunk input stay hidden."""


class LinkRestorationJobResponse(ProductRediscoveryJobResponse):
    """Public link Job state; article and template identities stay hidden."""


class SeoReviewGenerationJobResponse(ProductRediscoveryJobResponse):
    """Public Review Job state; Prompt, Chunk, and Article inputs stay hidden."""


class HumanizeGenerationJobResponse(ProductRediscoveryJobResponse):
    """Public Humanize Job state; Prompt and Article identities stay hidden."""


def require_server_project_access(
    request: Request,
) -> AuthorizedProjectRequest:
    """Authenticate and authorize one explicitly project-scoped server API."""

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


def _project_metadata_service(
    request: Request,
) -> PostgresServerProjectMetadata:
    service = getattr(
        request.app.state,
        "server_project_metadata",
        None,
    )
    if not isinstance(service, PostgresServerProjectMetadata):
        raise HTTPException(
            status_code=503,
            detail="Server project metadata is not available.",
        )
    return service


def _project_membership_service(
    request: Request,
) -> PostgresProjectMembershipService:
    service = getattr(
        request.app.state,
        "server_project_memberships",
        None,
    )
    if not isinstance(service, PostgresProjectMembershipService):
        raise HTTPException(
            status_code=503,
            detail="Project membership management is not available.",
        )
    return service


def _project_deletion_service(
    request: Request,
) -> PostgresProjectDeletionService:
    service = getattr(
        request.app.state,
        "server_project_deletion",
        None,
    )
    if not isinstance(service, PostgresProjectDeletionService):
        raise HTTPException(
            status_code=503,
            detail="Project deletion is not available.",
        )
    return service


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


def _expose_server_task(task: TaskRecord) -> TaskRecord:
    """Expose workflow actions for Server clients just like the local API."""

    task.allowed_actions = allowed_actions(task)
    return task


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


def _task_intake_response(
    result: ServerTaskIntakeResult,
) -> ProjectTaskIntakeResponse:
    return ProjectTaskIntakeResponse(
        intake_id=result.intake_id,
        intake_kind=result.intake_kind,
        source_name=result.source_name,
        source_digest=result.source_digest,
        created=result.created,
        tasks=[
            ProjectTaskIntakeItemResponse(
                id=task.id,
                topic_index=task.topic_index,
                topic=task.topic,
                primary_keyword=task.primary_keyword,
                competitor_keyword=task.competitor_keyword,
                competitor_blog=task.competitor_blog,
                status=task.status,
                revision=task.revision,
            )
            for task in result.tasks
        ],
    )


def _run_task_intake(
    command: Callable[[], ServerTaskIntakeResult],
) -> ProjectTaskIntakeResponse:
    try:
        return _task_intake_response(command())
    except ProjectAccessDenied as exc:
        raise HTTPException(
            status_code=403,
            detail="project access denied",
        ) from exc
    except ServerTaskIntakeConflict as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=str(exc),
        ) from exc
    except ServerTaskIntakeUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="Task intake is temporarily unavailable.",
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


def _project_catalog(
    request: Request,
) -> PostgresServerProjectCatalog:
    catalog = getattr(
        request.app.state,
        "server_project_catalog",
        None,
    )
    if not isinstance(catalog, PostgresServerProjectCatalog):
        raise HTTPException(
            status_code=503,
            detail="Server project catalog is not available.",
        )
    return catalog


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


def _product_generation(
    request: Request,
) -> ServerProductGenerationRegistry:
    registry = getattr(
        request.app.state,
        "server_product_generation",
        None,
    )
    if not isinstance(registry, ServerProductGenerationRegistry):
        raise HTTPException(
            status_code=503,
            detail="Server product generation is not available.",
        )
    return registry




def _outline_generation(
    request: Request,
) -> ServerOutlineGenerationRegistry:
    registry = getattr(
        request.app.state,
        "server_outline_generation",
        None,
    )
    if not isinstance(registry, ServerOutlineGenerationRegistry):
        raise HTTPException(
            status_code=503,
            detail="Server outline generation is not available.",
        )
    return registry


def _title_generation(
    request: Request,
) -> ServerTitleGenerationRegistry:
    registry = getattr(
        request.app.state,
        "server_title_generation",
        None,
    )
    if not isinstance(registry, ServerTitleGenerationRegistry):
        raise HTTPException(
            status_code=503,
            detail="Server title generation is not available.",
        )
    return registry


def _article_generation(
    request: Request,
) -> ServerArticleGenerationRegistry:
    registry = getattr(
        request.app.state,
        "server_article_generation",
        None,
    )
    if not isinstance(registry, ServerArticleGenerationRegistry):
        raise HTTPException(
            status_code=503,
            detail="Server article generation is not available.",
        )
    return registry


def _knowledge_coverage(
    request: Request,
) -> ServerKnowledgeCoverageService:
    service = getattr(
        request.app.state,
        "server_knowledge_coverage",
        None,
    )
    if not isinstance(service, ServerKnowledgeCoverageService):
        raise HTTPException(
            status_code=503,
            detail="Server knowledge coverage is not available.",
        )
    return service


def _link_restoration(
    request: Request,
) -> ServerLinkRestorationRegistry:
    registry = getattr(
        request.app.state,
        "server_link_restoration",
        None,
    )
    if not isinstance(registry, ServerLinkRestorationRegistry):
        raise HTTPException(
            status_code=503,
            detail="Server link restoration is not available.",
        )
    return registry


def _seo_review_generation(
    request: Request,
) -> ServerSeoReviewGenerationRegistry:
    registry = getattr(
        request.app.state,
        "server_seo_review_generation",
        None,
    )
    if not isinstance(registry, ServerSeoReviewGenerationRegistry):
        raise HTTPException(
            status_code=503,
            detail="Server SEO review generation is not available.",
        )
    return registry


def _humanize_generation(
    request: Request,
) -> ServerHumanizeGenerationRegistry:
    registry = getattr(
        request.app.state,
        "server_humanize_generation",
        None,
    )
    if not isinstance(registry, ServerHumanizeGenerationRegistry):
        raise HTTPException(
            status_code=503,
            detail="Server humanize generation is not available.",
        )
    return registry


def _project_prompt_service(
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


def _task_writing_settings_service(
    request: Request,
    authorized: AuthorizedProjectRequest,
):
    factory = getattr(
        request.app.state,
        "server_task_writing_settings_service_factory",
        None,
    )
    if not isinstance(factory, ServerTaskWritingSettingsServiceFactory):
        raise HTTPException(
            status_code=503,
            detail="Server writing settings are not available.",
        )
    return factory.create(authorized)


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


@router.post(
    "",
    response_model=AccessibleProject,
    status_code=201,
)
def create_project(
    payload: ProjectCreateRequest,
    request: Request,
    actor: ActorIdentity = Depends(require_server_actor),
) -> AccessibleProject:
    try:
        metadata = _project_metadata_service(request).create(
            actor=actor,
            customer_name=payload.customer_name,
            official_domain=payload.official_domain,
            owning_team_id=payload.owning_team_id,
            owner_user_id=payload.owner_user_id,
            event_id=f"project_create_{uuid.uuid4().hex}",
        )
    except ProjectAccessDenied as exc:
        raise HTTPException(
            status_code=403,
            detail="Project creation is not allowed for this account.",
        ) from exc
    except ServerProjectCreationConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ServerProjectMetadataUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="Project creation is temporarily unavailable.",
        ) from exc
    try:
        created = next(
            item
            for item in _project_directory(request).list_for_actor(actor)
            if item.project_id == metadata.project_id
        )
        return created
    except (ProjectDirectoryDenied, StopIteration, HTTPException):
        # Keep the creation response useful in focused service tests where
        # only the metadata dependency is installed.
        fallback_role = (
            "editor"
            if metadata.owner_user_id == actor.user_id
            else "org_admin"
        )
        return AccessibleProject(
            project_id=metadata.project_id,
            customer_name=metadata.customer_name,
            official_domain=metadata.official_domain,
            revision=metadata.revision,
            effective_role=fallback_role,
            owning_team_id=metadata.owning_team_id,
            owner_user_id=metadata.owner_user_id,
            is_project_owner=metadata.owner_user_id == actor.user_id,
            assignment_status=(
                "assigned" if metadata.owner_user_id is not None else "pending"
            ),
        )


@router.delete(
    "/{project}",
    response_model=ProjectDeletionResponse,
)
def delete_project(
    project: str,
    request: Request,
    authorized: AuthorizedProjectRequest = Depends(
        require_server_project_access
    ),
) -> ProjectDeletionResponse:
    del project
    try:
        deleted = _project_deletion_service(request).delete(
            actor=authorized.actor,
            project_id=authorized.project_id,
            event_id=f"project_delete_{uuid.uuid4().hex}",
        )
    except ProjectDeletionDenied as exc:
        raise HTTPException(
            status_code=403,
            detail="project deletion denied",
        ) from exc
    except ProjectDeletionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ProjectDeletionUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="Project deletion is temporarily unavailable.",
        ) from exc
    return ProjectDeletionResponse(
        project_id=deleted.project_id,
        deleted=True,
        cancelled_job_count=deleted.cancelled_job_count,
        deleted_row_count=deleted.deleted_row_count,
    )


@router.get(
    "/{project}/metadata",
    response_model=ProjectMetadataResponse,
)
def get_project_metadata(
    project: str,
    request: Request,
    authorized: AuthorizedProjectRequest = Depends(
        require_server_project_access
    ),
) -> ProjectMetadataResponse:
    del project
    try:
        metadata = _project_metadata_service(request).get(
            actor=authorized.actor,
            project_id=authorized.project_id,
        )
    except ProjectAccessDenied as exc:
        raise HTTPException(
            status_code=403,
            detail="project access denied",
        ) from exc
    except ServerProjectMetadataUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="Project metadata is temporarily unavailable.",
        ) from exc
    return _project_metadata_response(metadata)


@router.put(
    "/{project}/metadata",
    response_model=ProjectMetadataResponse,
)
def update_project_metadata(
    project: str,
    payload: ProjectMetadataUpdateRequest,
    request: Request,
    authorized: AuthorizedProjectRequest = Depends(
        require_server_project_access
    ),
) -> ProjectMetadataResponse:
    del project
    _require_project_permission(
        request,
        authorized,
        "article.edit",
    )
    try:
        metadata = _project_metadata_service(request).update(
            actor=authorized.actor,
            project_id=authorized.project_id,
            expected_revision=payload.revision,
            customer_name=payload.customer_name,
            official_domain=payload.official_domain,
            project_notes=payload.project_notes,
        )
    except ProjectAccessDenied as exc:
        raise HTTPException(
            status_code=403,
            detail="project access denied",
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ServerProjectMetadataConflict as exc:
        raise HTTPException(
            status_code=409,
            detail="Project metadata changed. Reload and try again.",
        ) from exc
    except ServerProjectMetadataUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="Project metadata is temporarily unavailable.",
        ) from exc
    return _project_metadata_response(metadata)


@router.get(
    "/{project}/members",
    response_model=ProjectMembershipListResponse,
)
def list_project_memberships(
    project: str,
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    after_user_id: str | None = Query(
        default=None,
        min_length=1,
        max_length=512,
    ),
    authorized: AuthorizedProjectRequest = Depends(
        require_server_project_access
    ),
) -> ProjectMembershipListResponse:
    del project
    try:
        page = _project_membership_service(request).list_members(
            actor=authorized.actor,
            project_id=authorized.project_id,
            limit=limit,
            after_user_id=after_user_id,
        )
    except ProjectAccessDenied as exc:
        raise HTTPException(
            status_code=403,
            detail="project access denied",
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail="Project membership cursor is invalid.",
        ) from exc
    except ProjectMembershipUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="Project membership list is temporarily unavailable.",
        ) from exc
    return ProjectMembershipListResponse(
        items=[
            ProjectMembershipListItemResponse(
                user_id=item.user_id,
                display_name=item.display_name,
                status=item.status,
                role=item.role,
            )
            for item in page.items
        ],
        next_after_user_id=page.next_after_user_id,
    )


@router.get(
    "/{project}/members/candidates",
    response_model=ProjectMembershipCandidateListResponse,
)
def list_project_membership_candidates(
    project: str,
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    after_user_id: str | None = Query(
        default=None,
        min_length=1,
        max_length=512,
    ),
    authorized: AuthorizedProjectRequest = Depends(
        require_server_project_access
    ),
) -> ProjectMembershipCandidateListResponse:
    del project
    try:
        page = _project_membership_service(request).list_candidates(
            actor=authorized.actor,
            project_id=authorized.project_id,
            limit=limit,
            after_user_id=after_user_id,
        )
    except ProjectAccessDenied as exc:
        raise HTTPException(
            status_code=403,
            detail="project access denied",
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail="Project membership cursor is invalid.",
        ) from exc
    except ProjectMembershipUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="Project membership candidate list is temporarily unavailable.",
        ) from exc
    return ProjectMembershipCandidateListResponse(
        items=[
            ProjectMembershipCandidateResponse(
                user_id=item.user_id,
                display_name=item.display_name,
            )
            for item in page.items
        ],
        next_after_user_id=page.next_after_user_id,
    )


@router.put(
    "/{project}/members/{user_id}",
    response_model=ProjectMembershipResponse,
)
def grant_project_membership(
    project: str,
    user_id: str,
    payload: ProjectMembershipUpdateRequest,
    request: Request,
    authorized: AuthorizedProjectRequest = Depends(
        require_server_project_access
    ),
) -> ProjectMembershipResponse:
    del project
    try:
        record = _project_membership_service(request).grant(
            actor=authorized.actor,
            project_id=authorized.project_id,
            target_user_id=user_id,
            role=payload.role,
            event_id=f"project_member_{uuid.uuid4().hex}",
        )
    except ProjectAccessDenied as exc:
        raise HTTPException(
            status_code=403,
            detail="project access denied",
        ) from exc
    except ProjectMembershipTargetUnavailable as exc:
        raise HTTPException(
            status_code=404,
            detail="Project member target is unavailable.",
        ) from exc
    except ProjectMembershipConflict as exc:
        raise HTTPException(
            status_code=409,
            detail="Project membership change conflicted.",
        ) from exc
    except ProjectMembershipUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="Project membership change is temporarily unavailable.",
        ) from exc
    return ProjectMembershipResponse(
        user_id=record.user_id,
        role=record.role,
    )


@router.delete(
    "/{project}/members/{user_id}",
    response_model=ProjectMembershipRevokeResponse,
)
def revoke_project_membership(
    project: str,
    user_id: str,
    request: Request,
    authorized: AuthorizedProjectRequest = Depends(
        require_server_project_access
    ),
) -> ProjectMembershipRevokeResponse:
    del project
    try:
        revoked = _project_membership_service(request).revoke(
            actor=authorized.actor,
            project_id=authorized.project_id,
            target_user_id=user_id,
            event_id=f"project_member_{uuid.uuid4().hex}",
        )
    except ProjectAccessDenied as exc:
        raise HTTPException(
            status_code=403,
            detail="project access denied",
        ) from exc
    except ProjectMembershipConflict as exc:
        raise HTTPException(
            status_code=409,
            detail="Project membership change conflicted.",
        ) from exc
    except ProjectMembershipUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="Project membership change is temporarily unavailable.",
        ) from exc
    return ProjectMembershipRevokeResponse(
        user_id=user_id.strip(),
        revoked=revoked,
    )


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
    return [
        _expose_server_task(task)
        for task in sorted(
            tasks,
            key=lambda task: (task.topic_index, task.id),
        )
    ]


@router.get(
    "/{project}/tasks/recent",
    response_model=list[ProjectRecentTaskResponse],
)
def list_recent_project_tasks(
    project: str,
    request: Request,
    limit: int = Query(default=5, ge=1, le=20),
    authorized: AuthorizedProjectRequest = Depends(
        require_server_project_access
    ),
) -> list[ProjectRecentTaskResponse]:
    del project
    return [
        ProjectRecentTaskResponse(
            id=str(task["id"]),
            topic_index=int(task["topic_index"]),
            topic=str(task["topic"]),
            selected_title=str(task["selected_title"]),
            status=str(task["status"]),
            updated_at=str(task["updated_at"]),
        )
        for task in _task_store(request, authorized).load_recent(limit)
    ]


@router.post(
    "/{project}/tasks",
    response_model=ProjectTaskIntakeResponse,
)
def create_project_task(
    project: str,
    payload: ProjectTaskCreateRequest,
    request: Request,
    authorized: AuthorizedProjectRequest = Depends(
        require_server_project_access
    ),
) -> ProjectTaskIntakeResponse:
    del project
    authorized = _require_project_permission(
        request,
        authorized,
        "article.edit",
    )
    runtime = _task_runtime(request, authorized)
    return _run_task_intake(
        lambda: runtime.intake.create_manual(
            actor=authorized.actor,
            intake_id=payload.intake_id,
            row=ServerTaskIntakeRow(
                topic=payload.topic,
                primary_keyword=payload.primary_keyword,
                competitor_keyword=payload.competitor_keyword,
                competitor_blog=payload.competitor_blog,
            ),
        )
    )


@router.post(
    "/{project}/task-imports/preview",
    response_model=ProjectTaskWorkbookPreviewResponse,
)
def preview_project_task_workbook(
    project: str,
    request: Request,
    file: UploadFile = File(...),
    authorized: AuthorizedProjectRequest = Depends(
        require_server_project_access
    ),
) -> ProjectTaskWorkbookPreviewResponse:
    del project
    _require_project_permission(request, authorized, "article.edit")
    content = file.file.read(MAX_WORKBOOK_BYTES + 1)
    try:
        preview = preview_task_workbook(
            filename=file.filename or "",
            content=content,
        )
    except ServerTaskWorkbookError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ProjectTaskWorkbookPreviewResponse(
        filename=preview.filename,
        sheet_name=preview.sheet_name,
        headers=list(preview.headers),
        rows=[list(row) for row in preview.rows],
        mapping=preview.mapping,
        truncated=preview.truncated,
    )


@router.put(
    "/{project}/owner",
    response_model=ProjectOwnerResponse,
)
def assign_project_owner(
    project: str,
    payload: ProjectOwnerUpdateRequest,
    request: Request,
    authorized: AuthorizedProjectRequest = Depends(
        require_server_project_access
    ),
) -> ProjectOwnerResponse:
    del project
    try:
        record = _project_membership_service(request).assign_owner(
            actor=authorized.actor,
            project_id=authorized.project_id,
            owner_user_id=payload.owner_user_id,
            event_id=f"project_owner_{uuid.uuid4().hex}",
        )
    except ProjectAccessDenied as exc:
        raise HTTPException(
            status_code=403,
            detail="project access denied",
        ) from exc
    except ProjectMembershipTargetUnavailable as exc:
        raise HTTPException(
            status_code=404,
            detail="Project owner target is unavailable.",
        ) from exc
    except ProjectMembershipConflict as exc:
        raise HTTPException(
            status_code=409,
            detail="Project owner change conflicted.",
        ) from exc
    except ProjectMembershipUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="Project owner change is temporarily unavailable.",
        ) from exc
    return _project_owner_response(record)


@router.post(
    "/{project}/task-imports",
    response_model=ProjectTaskIntakeResponse,
)
def import_project_tasks(
    project: str,
    payload: ProjectTaskImportRequest,
    request: Request,
    authorized: AuthorizedProjectRequest = Depends(
        require_server_project_access
    ),
) -> ProjectTaskIntakeResponse:
    del project
    authorized = _require_project_permission(
        request,
        authorized,
        "article.edit",
    )
    runtime = _task_runtime(request, authorized)
    return _run_task_intake(
        lambda: runtime.intake.import_rows(
            actor=authorized.actor,
            intake_id=payload.intake_id,
            source_name=payload.source_name,
            rows=tuple(
                ServerTaskIntakeRow(
                    topic=row.topic,
                    primary_keyword=row.primary_keyword,
                    competitor_keyword=row.competitor_keyword,
                    competitor_blog=row.competitor_blog,
                )
                for row in payload.rows
            ),
        )
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
        return _expose_server_task(_task_store(request, authorized).get(task_id))
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail="Task was not found in the requested project.",
        ) from None


@router.get(
    "/{project}/catalog",
    response_model=ProjectCatalogResponse,
)
def read_project_catalog(
    project: str,
    request: Request,
    product_limit: int = Query(default=100, ge=1, le=200),
    image_limit: int = Query(default=48, ge=1, le=100),
    image_product_ids: str = Query(default="", max_length=2048),
    authorized: AuthorizedProjectRequest = Depends(
        require_server_project_access
    ),
) -> ProjectCatalogResponse:
    del project
    selected_image_products = tuple(
        dict.fromkeys(
            item.strip()
            for item in image_product_ids.split(",")
            if item.strip()
        )
    )
    if len(selected_image_products) > 3:
        raise HTTPException(
            status_code=422,
            detail="At most three image products are allowed.",
        )
    catalog = _project_catalog(request).read(
        authorized.project_id,
        product_limit=product_limit,
        image_limit=image_limit,
        image_product_ids=selected_image_products,
    )
    return ProjectCatalogResponse(
        products=[
            ProjectCatalogProductResponse(
                product_id=item.product_id,
                name=item.name,
                asset_count=item.asset_count,
                selected_asset_id=item.selected_asset_id,
            )
            for item in catalog.products
        ],
        image_assets=[
            ProjectCatalogImageAssetResponse(
                asset_id=item.asset_id,
                product_id=item.product_id,
                content_type=item.content_type,
                byte_size=item.byte_size,
                width=item.width,
                height=item.height,
                label=item.label,
                evidence_kind=item.evidence_kind,
            )
            for item in catalog.image_assets
        ],
    )


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
    "/{project}/tasks/{task_id}/writing-settings",
    response_model=TaskRecord,
)
def update_project_task_writing_settings(
    project: str,
    task_id: str,
    payload: ProjectTaskWritingSettingsRequest,
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
    try:
        return _task_writing_settings_service(
            request,
            authorized,
        ).update(
            actor=authorized.actor,
            task_id=task_id,
            expected_revision=payload.revision,
            settings=payload.to_settings(),
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
    except (RevisionConflictError, ServerTaskWritingSettingsConflict) as exc:
        raise HTTPException(
            status_code=409,
            detail="Task writing settings conflict.",
        ) from exc
    except (ServerProjectPromptError, ServerTaskWritingSettingsError) as exc:
        raise HTTPException(
            status_code=422,
            detail="Writing settings are invalid.",
        ) from exc
    except (
        ServerProjectPromptUnavailable,
        ServerTaskWritingSettingsUnavailable,
    ) as exc:
        raise HTTPException(
            status_code=503,
            detail="Server writing settings are temporarily unavailable.",
        ) from exc


@router.post(
    "/{project}/tasks/{task_id}/writing-settings/preview",
    response_model=ProjectTaskWritingSettingsPreviewResponse,
)
def preview_project_task_writing_settings(
    project: str,
    task_id: str,
    payload: ProjectTaskWritingSettingsPreviewRequest,
    request: Request,
    response: Response,
    authorized: AuthorizedProjectRequest = Depends(
        require_server_project_access
    ),
) -> ProjectTaskWritingSettingsPreviewResponse:
    del project
    try:
        preview = _task_writing_settings_service(
            request,
            authorized,
        ).preview(
            actor=authorized.actor,
            task_id=task_id,
            expected_revision=payload.revision,
            kind=payload.kind,
            settings=payload.to_settings(),
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
    except (RevisionConflictError, ServerTaskWritingSettingsConflict) as exc:
        raise HTTPException(
            status_code=409,
            detail="Task writing settings conflict.",
        ) from exc
    except (ServerProjectPromptError, ServerTaskWritingSettingsError) as exc:
        raise HTTPException(
            status_code=422,
            detail="Writing settings are invalid.",
        ) from exc
    except (
        ServerProjectPromptUnavailable,
        ServerTaskWritingSettingsUnavailable,
    ) as exc:
        raise HTTPException(
            status_code=503,
            detail="Server writing settings are temporarily unavailable.",
        ) from exc

    response.headers["Cache-Control"] = "no-store"
    snapshot = preview.snapshot
    return ProjectTaskWritingSettingsPreviewResponse(
        project_id=authorized.project_id,
        task_id=task_id,
        task_revision=payload.revision,
        kind=payload.kind,
        prompt_snapshot=ProjectTaskPromptSnapshotResponse(
            prompt_id=snapshot.prompt_id,
            name=snapshot.name,
            kind=payload.kind,
            version=snapshot.version,
            source=snapshot.source,
            captured_at=snapshot.captured_at,
        ),
        effective_prompt=preview.effective_prompt,
        context_chunk_count=preview.context_chunk_count,
        target_words=preview.target_words,
        warnings=[
            "Preview resolves the current Project Prompt and Published "
            "Knowledge; generation pins exact inputs when the Job is "
            "enqueued."
        ],
    )


@router.post(
    "/{project}/tasks/{task_id}/products",
    response_model=ProductGenerationJobResponse,
)
def enqueue_project_task_product_generation(
    project: str,
    task_id: str,
    payload: ProjectRevisionRequest,
    request: Request,
    authorized: AuthorizedProjectRequest = Depends(
        require_server_project_access
    ),
) -> ProductGenerationJobResponse:
    del project
    authorized = _require_project_permission(
        request,
        authorized,
        "article.edit",
    )
    try:
        job = _product_generation(request).enqueue(
            actor=authorized.actor,
            project_id=authorized.project_id,
            task_id=task_id,
            source_revision=payload.revision,
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
    except ProductGenerationUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="Server product generation is not available.",
        ) from exc
    return ProductGenerationJobResponse.model_validate(job)


@router.get(
    "/{project}/tasks/{task_id}/products/jobs/{job_id}",
    response_model=ProductGenerationJobResponse,
)
def read_project_task_product_generation_job(
    project: str,
    task_id: str,
    job_id: str,
    request: Request,
    authorized: AuthorizedProjectRequest = Depends(
        require_server_project_access
    ),
) -> ProductGenerationJobResponse:
    del project
    try:
        job = _product_generation(request).get_job(
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
            detail="Product generation job was not found.",
        ) from None
    except ProductGenerationUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="Server product generation is not available.",
        ) from exc
    return ProductGenerationJobResponse.model_validate(job)


@router.post(
    "/{project}/tasks/{task_id}/titles",
    response_model=TitleGenerationJobResponse,
)
def enqueue_project_task_title_generation(
    project: str,
    task_id: str,
    payload: ProjectRevisionRequest,
    request: Request,
    authorized: AuthorizedProjectRequest = Depends(
        require_server_project_access
    ),
) -> TitleGenerationJobResponse:
    del project
    authorized = _require_project_permission(
        request,
        authorized,
        "article.edit",
    )
    try:
        job = _title_generation(request).enqueue(
            actor=authorized.actor,
            project_id=authorized.project_id,
            task_id=task_id,
            source_revision=payload.revision,
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
    except TitleGenerationUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="Server title generation is not available.",
        ) from exc
    return TitleGenerationJobResponse.model_validate(job)


@router.get(
    "/{project}/tasks/{task_id}/titles/jobs/{job_id}",
    response_model=TitleGenerationJobResponse,
)
def read_project_task_title_generation_job(
    project: str,
    task_id: str,
    job_id: str,
    request: Request,
    authorized: AuthorizedProjectRequest = Depends(
        require_server_project_access
    ),
) -> TitleGenerationJobResponse:
    del project
    try:
        job = _title_generation(request).get_job(
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
            detail="Title generation job was not found.",
        ) from None
    except TitleGenerationUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="Server title generation is not available.",
        ) from exc
    return TitleGenerationJobResponse.model_validate(job)


def _enqueue_project_task_article_job(
    task_id: str,
    payload: ArticleGenerationRequest,
    request: Request,
    authorized: AuthorizedProjectRequest,
    *,
    operation: str,
) -> ArticleGenerationJobResponse:
    authorized = _require_project_permission(
        request,
        authorized,
        "article.edit",
    )
    try:
        job = _article_generation(request).enqueue(
            actor=authorized.actor,
            project_id=authorized.project_id,
            task_id=task_id,
            source_revision=payload.revision,
            operation=operation,
            use_evidence_pack=payload.use_evidence_pack,
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
    except ArticleGenerationUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="Server article generation is not available.",
        ) from exc
    return ArticleGenerationJobResponse.model_validate(job)


@router.post(
    "/{project}/tasks/{task_id}/article",
    response_model=ArticleGenerationJobResponse,
)
def enqueue_project_task_article_generation(
    project: str,
    task_id: str,
    payload: ArticleGenerationRequest,
    request: Request,
    authorized: AuthorizedProjectRequest = Depends(
        require_server_project_access
    ),
) -> ArticleGenerationJobResponse:
    del project
    return _enqueue_project_task_article_job(
        task_id,
        payload,
        request,
        authorized,
        operation=ARTICLE_GENERATION_OPERATION,
    )


@router.post(
    "/{project}/tasks/{task_id}/article/rewrite",
    response_model=ArticleGenerationJobResponse,
)
def enqueue_project_task_article_rewrite(
    project: str,
    task_id: str,
    payload: ArticleGenerationRequest,
    request: Request,
    authorized: AuthorizedProjectRequest = Depends(
        require_server_project_access
    ),
) -> ArticleGenerationJobResponse:
    del project
    return _enqueue_project_task_article_job(
        task_id,
        payload,
        request,
        authorized,
        operation=ARTICLE_REWRITE_OPERATION,
    )


def _read_project_task_article_job(
    task_id: str,
    job_id: str,
    request: Request,
    authorized: AuthorizedProjectRequest,
    *,
    operation: str,
) -> ArticleGenerationJobResponse:
    try:
        job = _article_generation(request).get_job(
            actor=authorized.actor,
            project_id=authorized.project_id,
            task_id=task_id,
            job_id=job_id,
            operation=operation,
        )
    except ProjectAccessDenied as exc:
        raise HTTPException(
            status_code=403,
            detail="project access denied",
        ) from exc
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail="Article generation job was not found.",
        ) from None
    except ArticleGenerationUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="Server article generation is not available.",
        ) from exc
    return ArticleGenerationJobResponse.model_validate(job)


@router.get(
    "/{project}/tasks/{task_id}/article/jobs/{job_id}",
    response_model=ArticleGenerationJobResponse,
)
def read_project_task_article_generation_job(
    project: str,
    task_id: str,
    job_id: str,
    request: Request,
    authorized: AuthorizedProjectRequest = Depends(
        require_server_project_access
    ),
) -> ArticleGenerationJobResponse:
    del project
    return _read_project_task_article_job(
        task_id,
        job_id,
        request,
        authorized,
        operation=ARTICLE_GENERATION_OPERATION,
    )


@router.get(
    "/{project}/tasks/{task_id}/article/rewrite/jobs/{job_id}",
    response_model=ArticleGenerationJobResponse,
)
def read_project_task_article_rewrite_job(
    project: str,
    task_id: str,
    job_id: str,
    request: Request,
    authorized: AuthorizedProjectRequest = Depends(
        require_server_project_access
    ),
) -> ArticleGenerationJobResponse:
    del project
    return _read_project_task_article_job(
        task_id,
        job_id,
        request,
        authorized,
        operation=ARTICLE_REWRITE_OPERATION,
    )


@router.post(
    "/{project}/tasks/{task_id}/restore-links",
    response_model=LinkRestorationJobResponse,
)
def enqueue_project_task_link_restoration(
    project: str,
    task_id: str,
    payload: ProjectRevisionRequest,
    request: Request,
    authorized: AuthorizedProjectRequest = Depends(
        require_server_project_access
    ),
) -> LinkRestorationJobResponse:
    del project
    authorized = _require_project_permission(
        request,
        authorized,
        "article.edit",
    )
    try:
        job = _link_restoration(request).enqueue(
            actor=authorized.actor,
            project_id=authorized.project_id,
            task_id=task_id,
            source_revision=payload.revision,
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
    except LinkRestorationUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="Server link restoration is not available.",
        ) from exc
    return LinkRestorationJobResponse.model_validate(job)


@router.get(
    "/{project}/tasks/{task_id}/restore-links/jobs/{job_id}",
    response_model=LinkRestorationJobResponse,
)
def read_project_task_link_restoration_job(
    project: str,
    task_id: str,
    job_id: str,
    request: Request,
    authorized: AuthorizedProjectRequest = Depends(
        require_server_project_access
    ),
) -> LinkRestorationJobResponse:
    del project
    try:
        job = _link_restoration(request).get_job(
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
            detail="Link restoration job was not found.",
        ) from None
    except LinkRestorationUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="Server link restoration is not available.",
        ) from exc
    return LinkRestorationJobResponse.model_validate(job)


@router.put(
    "/{project}/tasks/{task_id}/seo-review-settings",
    response_model=TaskRecord,
)
def update_project_task_seo_review_settings(
    project: str,
    task_id: str,
    payload: ProjectSeoReviewSettingsRequest,
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
    selection = payload.prompt_selection.strip() or "project_default"
    try:
        snapshot = _project_prompt_service(
            request,
            authorized,
        ).resolve(
            authorized.actor,
            kind="review",
            selection=selection,
        )
        keyword_count, prompt_source, prompt_version = (
            apply_server_seo_review_settings(
                task,
                primary_keyword=payload.primary_keyword,
                long_tail_keywords=payload.long_tail_keywords,
                prompt_selection=selection,
                resolved_prompt=snapshot,
            )
        )
    except ProjectAccessDenied as exc:
        raise HTTPException(
            status_code=403,
            detail="project access denied",
        ) from exc
    except (
        ServerProjectPromptError,
        ServerSeoReviewSettingsError,
    ) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ServerProjectPromptUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="Project prompt management is temporarily unavailable.",
        ) from exc
    return _save_audited_task(
        request,
        authorized,
        task,
        expected_revision=payload.revision,
        action="article.seo_review_settings.updated",
        details={
            "long_tail_keyword_count": keyword_count,
            "prompt_source": prompt_source,
            "prompt_version": prompt_version,
        },
    )


@router.post(
    "/{project}/tasks/{task_id}/seo-reviews",
    response_model=SeoReviewGenerationJobResponse,
)
def enqueue_project_task_seo_review(
    project: str,
    task_id: str,
    payload: ProjectRevisionRequest,
    request: Request,
    authorized: AuthorizedProjectRequest = Depends(
        require_server_project_access
    ),
) -> SeoReviewGenerationJobResponse:
    del project
    authorized = _require_project_permission(
        request,
        authorized,
        "article.review",
    )
    try:
        job = _seo_review_generation(request).enqueue(
            actor=authorized.actor,
            project_id=authorized.project_id,
            task_id=task_id,
            source_revision=payload.revision,
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
    except SeoReviewGenerationUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="Server SEO review generation is not available.",
        ) from exc
    return SeoReviewGenerationJobResponse.model_validate(job)


@router.get(
    "/{project}/tasks/{task_id}/seo-reviews/jobs/{job_id}",
    response_model=SeoReviewGenerationJobResponse,
)
def read_project_task_seo_review_job(
    project: str,
    task_id: str,
    job_id: str,
    request: Request,
    authorized: AuthorizedProjectRequest = Depends(
        require_server_project_access
    ),
) -> SeoReviewGenerationJobResponse:
    del project
    try:
        job = _seo_review_generation(request).get_job(
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
            detail="SEO review generation job was not found.",
        ) from None
    except SeoReviewGenerationUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="Server SEO review generation is not available.",
        ) from exc
    return SeoReviewGenerationJobResponse.model_validate(job)


@router.put(
    "/{project}/tasks/{task_id}/seo-reviews/{review_id}/changes/{change_id}",
    response_model=TaskRecord,
)
def update_project_task_seo_review_change(
    project: str,
    task_id: str,
    review_id: str,
    change_id: str,
    payload: ProjectSeoReviewChangeRequest,
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
    try:
        task = _task_store(request, authorized).get(task_id)
        summary = update_server_seo_review_change(
            task,
            review_id=review_id,
            change_id=change_id,
            decision=payload.decision,
            reviewed_text=payload.reviewed_text,
            confirm_risks=payload.confirm_risks,
            actor_user_id=authorized.actor.user_id,
        )
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail="SEO review or change was not found.",
        ) from None
    except ServerSeoReviewConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ServerSeoReviewValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _save_audited_task(
        request,
        authorized,
        task,
        expected_revision=payload.revision,
        action="article.seo_review.change.updated",
        details={
            "decision": summary.decision,
            "risk_confirmed": summary.risk_confirmed,
            "risk_count": summary.risk_count,
        },
    )


@router.post(
    "/{project}/tasks/{task_id}/seo-reviews/{review_id}/preview",
    response_model=SeoReviewPreview,
)
def preview_project_task_seo_review(
    project: str,
    task_id: str,
    review_id: str,
    payload: ProjectRevisionRequest,
    request: Request,
    authorized: AuthorizedProjectRequest = Depends(
        require_server_project_access
    ),
) -> SeoReviewPreview:
    del project
    authorized = _require_project_permission(
        request,
        authorized,
        "article.review",
    )
    try:
        task = _task_store(request, authorized).get(task_id)
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail="Task was not found in the requested project.",
        ) from None
    if task.revision != payload.revision:
        raise HTTPException(
            status_code=409,
            detail="Task revision changed.",
        )
    try:
        return build_server_seo_review_preview(
            task,
            review_id=review_id,
        )
    except ServerSeoReviewNotFound:
        raise HTTPException(
            status_code=404,
            detail="SEO review was not found.",
        ) from None
    except ServerSeoReviewConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ServerSeoReviewValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post(
    "/{project}/tasks/{task_id}/seo-reviews/{review_id}/apply",
    response_model=TaskRecord,
)
def apply_project_task_seo_review(
    project: str,
    task_id: str,
    review_id: str,
    payload: ProjectSeoReviewApplyRequest,
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
    try:
        task = _task_store(request, authorized).get(task_id)
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail="Task was not found in the requested project.",
        ) from None
    try:
        ensure_action_allowed(task, ACTION_UPDATE_ARTICLE)
        summary = apply_server_seo_review(
            task,
            review_id=review_id,
            preview_hash=payload.preview_hash,
            confirm_pending=payload.confirm_pending,
            actor_user_id=authorized.actor.user_id,
        )
    except WorkflowActionNotAllowed as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ServerSeoReviewNotFound:
        raise HTTPException(
            status_code=404,
            detail="SEO review was not found.",
        ) from None
    except ServerSeoReviewConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ServerSeoReviewValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _save_audited_task(
        request,
        authorized,
        task,
        expected_revision=payload.revision,
        action="article.seo_review.applied",
        details={
            "accepted_count": summary.accepted_count,
            "invalid_count": summary.invalid_count,
            "pending_count": summary.pending_count,
            "rejected_count": summary.rejected_count,
        },
    )


@router.post(
    "/{project}/tasks/{task_id}/seo-reviews/{review_id}/complete",
    response_model=TaskRecord,
)
def complete_project_task_seo_review(
    project: str,
    task_id: str,
    review_id: str,
    payload: ProjectSeoReviewCompleteRequest,
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
    try:
        task = _task_store(request, authorized).get(task_id)
        summary = complete_server_seo_review(
            task,
            review_id=review_id,
            confirm_pending=payload.confirm_pending,
            actor_user_id=authorized.actor.user_id,
        )
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail="SEO review was not found.",
        ) from None
    except ServerSeoReviewConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ServerSeoReviewValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _save_audited_task(
        request,
        authorized,
        task,
        expected_revision=payload.revision,
        action="article.seo_review.completed",
        details={
            "accepted_count": summary.accepted_count,
            "invalid_count": summary.invalid_count,
            "pending_count": summary.pending_count,
            "rejected_count": summary.rejected_count,
        },
    )


@router.put(
    "/{project}/tasks/{task_id}/selected-title",
    response_model=TaskRecord,
)
def select_project_task_title(
    project: str,
    task_id: str,
    payload: ProjectTitleSelectionRequest,
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
        ensure_action_allowed(task, ACTION_SELECT_TITLE)
    except WorkflowActionNotAllowed as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
        ) from exc
    if payload.title is not None:
        selected_title = payload.title.strip()
        if not selected_title:
            raise HTTPException(
                status_code=422,
                detail="Custom title cannot be blank.",
            )
    else:
        assert payload.candidate_index is not None
        if payload.candidate_index >= len(task.title_candidates):
            raise HTTPException(
                status_code=422,
                detail="Title candidate index is out of range.",
            )
        selected_title = task.title_candidates[payload.candidate_index].strip()
    if not selected_title:
        raise HTTPException(
            status_code=409,
            detail="The selected title candidate is unavailable.",
        )
    task.selected_title = selected_title
    invalidate_downstream(task, "selected_title")
    return _save_audited_task(
        request,
        authorized,
        task,
        expected_revision=payload.revision,
        action="article.title.selected",
        details={
            "candidate_count": len(task.title_candidates),
            **(
                {"candidate_index": payload.candidate_index}
                if payload.candidate_index is not None
                else {}
            ),
        },
    )


@router.put(
    "/{project}/tasks/{task_id}/manual-completion",
    response_model=TaskRecord,
)
def update_project_task_manual_completion(
    project: str,
    task_id: str,
    payload: ProjectTaskCompletionRequest,
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
    try:
        task = _task_store(request, authorized).get(task_id)
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail="Task was not found in the requested project.",
        ) from None

    task.manual_completed = payload.completed
    task.manual_completed_at = now_iso() if payload.completed else ""
    task.manual_completed_by = (
        authorized.actor.user_id if payload.completed else ""
    )
    return _save_audited_task(
        request,
        authorized,
        task,
        expected_revision=payload.revision,
        action="article.task.completion.updated",
        details={"completed": payload.completed},
    )


@router.post(
    "/{project}/tasks/{task_id}/outline",
    response_model=OutlineGenerationJobResponse,
)
def enqueue_project_task_outline_generation(
    project: str,
    task_id: str,
    payload: ProjectRevisionRequest,
    request: Request,
    authorized: AuthorizedProjectRequest = Depends(
        require_server_project_access
    ),
) -> OutlineGenerationJobResponse:
    del project
    authorized = _require_project_permission(
        request,
        authorized,
        "article.edit",
    )
    try:
        job = _outline_generation(request).enqueue(
            actor=authorized.actor,
            project_id=authorized.project_id,
            task_id=task_id,
            source_revision=payload.revision,
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
    except OutlineGenerationUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="Server outline generation is not available.",
        ) from exc
    return OutlineGenerationJobResponse.model_validate(job)


@router.get(
    "/{project}/tasks/{task_id}/outline/jobs/{job_id}",
    response_model=OutlineGenerationJobResponse,
)
def read_project_task_outline_generation_job(
    project: str,
    task_id: str,
    job_id: str,
    request: Request,
    authorized: AuthorizedProjectRequest = Depends(
        require_server_project_access
    ),
) -> OutlineGenerationJobResponse:
    del project
    try:
        job = _outline_generation(request).get_job(
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
            detail="Outline generation job was not found.",
        ) from None
    except OutlineGenerationUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="Server outline generation is not available.",
        ) from exc
    return OutlineGenerationJobResponse.model_validate(job)


@router.put(
    "/{project}/tasks/{task_id}/outline",
    response_model=TaskRecord,
)
def update_project_task_outline(
    project: str,
    task_id: str,
    payload: ProjectOutlineUpdateRequest,
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
        ensure_action_allowed(task, ACTION_UPDATE_OUTLINE)
    except WorkflowActionNotAllowed as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
        ) from exc
    try:
        outline = apply_reviewed_outline(
            task,
            outline=payload.outline,
            confirmed=payload.confirmed,
        )
    except ServerOutlineUpdateError:
        raise HTTPException(
            status_code=422,
            detail="Outline cannot be empty.",
        ) from None
    return _save_audited_task(
        request,
        authorized,
        task,
        expected_revision=payload.revision,
        action="article.outline.updated",
        details={
            "confirmed": payload.confirmed,
            "outline_characters": len(outline),
        },
    )


@router.post(
    "/{project}/tasks/{task_id}/outline/restore-version",
    response_model=TaskRecord,
)
def restore_project_task_outline_version(
    project: str,
    task_id: str,
    payload: ProjectOutlineVersionRestoreRequest,
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
        ensure_action_allowed(task, ACTION_UPDATE_OUTLINE)
    except WorkflowActionNotAllowed as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
        ) from exc
    try:
        restored_from = restore_reviewed_outline_version(
            task,
            version_index=payload.version_index,
        )
    except ServerOutlineVersionNotFound:
        raise HTTPException(
            status_code=404,
            detail="Outline version was not found.",
        ) from None
    except ServerOutlineUpdateError:
        raise HTTPException(
            status_code=422,
            detail="The selected version cannot be restored as an outline.",
        ) from None
    return _save_audited_task(
        request,
        authorized,
        task,
        expected_revision=payload.revision,
        action="article.outline_version.restored",
        details={
            "restored_from": restored_from,
            "version_index": payload.version_index,
        },
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

    recommendation_reasons = dict(task.product_candidate_reasons)
    task.products = list(products)
    for product in task.products:
        reason = recommendation_reasons.get(product.product_id, "").strip()
        if reason:
            product.selection_reason = reason
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
    "/{project}/tasks/{task_id}/checks/initial-ai/screenshot",
    response_model=TaskRecord,
)
def upload_project_task_initial_ai_screenshot(
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
        ensure_action_allowed(task, ACTION_CONFIRM_INITIAL_AI)
    except WorkflowActionNotAllowed as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    content = file.file.read(MAX_SERVER_AI_SCREENSHOT_BYTES + 1)
    if len(content) > MAX_SERVER_AI_SCREENSHOT_BYTES:
        raise HTTPException(
            status_code=413,
            detail="AI-rate screenshot exceeds 25 MB.",
        )
    try:
        ServerInitialAiScreenshotPreparation(
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
        action="article.initial_ai_screenshot.uploaded",
        details={
            "screenshot_height": (
                task.initial_ai_check.screenshot_height or 0
            ),
            "screenshot_width": (
                task.initial_ai_check.screenshot_width or 0
            ),
        },
    )


@router.put(
    "/{project}/tasks/{task_id}/checks/initial-ai",
    response_model=TaskRecord,
)
def confirm_project_task_initial_ai(
    project: str,
    task_id: str,
    payload: InitialAiCheckUpdateRequest,
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
        ensure_action_allowed(task, ACTION_CONFIRM_INITIAL_AI)
    except WorkflowActionNotAllowed as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    initial = task.initial_article.strip()
    if not initial:
        raise HTTPException(
            status_code=409,
            detail="Save the initial article before confirming.",
        )
    if (
        task.initial_article_hash.strip()
        and task.initial_article_hash != content_hash(initial)
    ):
        raise HTTPException(
            status_code=409,
            detail="The initial article identity changed.",
        )
    previous = task.initial_ai_check
    report = payload.report.strip() or previous.report
    task.initial_ai_check = AICheck(
        confirmed=payload.confirmed,
        deferred=payload.deferred,
        score=payload.score,
        report=report,
        provider=previous.provider,
        checked_at=previous.checked_at,
        screenshot_path="",
        screenshot_asset_id=previous.screenshot_asset_id,
        screenshot_content_hash=previous.screenshot_content_hash,
        screenshot_filename=previous.screenshot_filename,
        screenshot_width=previous.screenshot_width,
        screenshot_height=previous.screenshot_height,
        confirmed_at=now_iso() if payload.confirmed else "",
        article_hash=content_hash(initial),
    )
    task.zero_gpt_report = report
    if payload.confirmed or payload.deferred:
        transition_task(task, STATUS_INITIAL_AI_CHECKED)
    apply_ai_rate_humanization_skip(
        task,
        threshold=float(_server_app_config(request).ai_pass_threshold),
    )
    return _save_audited_task(
        request,
        authorized,
        task,
        expected_revision=payload.revision,
        action="article.initial_ai_check.updated",
        details={
            "confirmed": payload.confirmed,
            "deferred": payload.deferred,
            "score_recorded": payload.score is not None,
            "humanization_skipped": task.humanization_skipped,
        },
    )


@router.post(
    "/{project}/tasks/{task_id}/humanize",
    response_model=HumanizeGenerationJobResponse,
)
def enqueue_project_task_humanize(
    project: str,
    task_id: str,
    payload: ProjectRevisionRequest,
    request: Request,
    authorized: AuthorizedProjectRequest = Depends(
        require_server_project_access
    ),
) -> HumanizeGenerationJobResponse:
    del project
    authorized = _require_project_permission(
        request,
        authorized,
        "article.edit",
    )
    try:
        job = _humanize_generation(request).enqueue(
            actor=authorized.actor,
            project_id=authorized.project_id,
            task_id=task_id,
            source_revision=payload.revision,
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
    except HumanizeGenerationUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="Server humanize generation is not available.",
        ) from exc
    return HumanizeGenerationJobResponse.model_validate(job)


@router.get(
    "/{project}/tasks/{task_id}/humanize/jobs/{job_id}",
    response_model=HumanizeGenerationJobResponse,
)
def read_project_task_humanize_job(
    project: str,
    task_id: str,
    job_id: str,
    request: Request,
    authorized: AuthorizedProjectRequest = Depends(
        require_server_project_access
    ),
) -> HumanizeGenerationJobResponse:
    del project
    try:
        job = _humanize_generation(request).get_job(
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
            detail="Humanize job was not found.",
        ) from None
    except HumanizeGenerationUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="Server humanize generation is not available.",
        ) from exc
    return HumanizeGenerationJobResponse.model_validate(job)


@router.put(
    "/{project}/tasks/{task_id}/humanized-article",
    response_model=TaskRecord,
)
def save_project_task_humanized_article(
    project: str,
    task_id: str,
    payload: ReviewedHumanizedArticleRequest,
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
        ensure_action_allowed(task, ACTION_UPDATE_HUMANIZED)
    except WorkflowActionNotAllowed as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    try:
        candidate = apply_reviewed_humanized_article(
            task,
            article=payload.article,
        )
    except ServerHumanizedArticleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if payload.recheck_ai_rate:
        detector = ZeroGPTClient()
        checked_at = now_iso()
        article_hash = content_hash(candidate)
        if not detector.ready:
            task.final_ai_check = AICheck(
                confirmed=False,
                report="ZeroGPT 自动复检未运行：服务端尚未配置 API Key。",
                provider="zerogpt",
                checked_at=checked_at,
                article_hash=article_hash,
            )
        else:
            try:
                detection = detector.detect(candidate)
            except Exception:
                task.final_ai_check = AICheck(
                    confirmed=False,
                    report="ZeroGPT 自动复检暂时不可用，请稍后重试或保留截图人工确认。",
                    provider="zerogpt",
                    checked_at=checked_at,
                    article_hash=article_hash,
                )
            else:
                task.final_ai_check = AICheck(
                    confirmed=False,
                    score=detection.ai_percentage,
                    report=detection.report,
                    provider="zerogpt",
                    checked_at=checked_at,
                    article_hash=article_hash,
                )
    _knowledge_coverage(request).evaluate_task(
        task,
        organization_id=authorized.actor.organization_id,
        user_id=authorized.actor.user_id,
        project_id=authorized.project_id,
    )
    return _save_audited_task(
        request,
        authorized,
        task,
        expected_revision=payload.revision,
        action="article.humanized.updated",
        details={
            "humanized_word_count": visible_word_count(candidate),
        },
    )


@router.get(
    "/{project}/tasks/{task_id}/checks/knowledge-coverage",
    response_model=ProjectKnowledgeCoverageDetailResponse,
)
def read_project_task_knowledge_coverage(
    project: str,
    task_id: str,
    request: Request,
    response: Response,
    authorized: AuthorizedProjectRequest = Depends(
        require_server_project_access
    ),
) -> ProjectKnowledgeCoverageDetailResponse:
    del project
    store = _task_store(request, authorized)
    try:
        task = store.get(task_id)
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail="Task was not found in the requested project.",
        ) from None
    try:
        detail = _knowledge_coverage(request).read_detail(
            task,
            project_id=authorized.project_id,
        )
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail="Knowledge coverage details are temporarily unavailable.",
        ) from exc
    response.headers["Cache-Control"] = "no-store"
    return ProjectKnowledgeCoverageDetailResponse.model_validate(detail)


@router.put(
    "/{project}/tasks/{task_id}/checks/knowledge-coverage",
    response_model=TaskRecord,
)
def recheck_project_task_knowledge_coverage(
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
    report = _knowledge_coverage(request).evaluate_task(
        task,
        organization_id=authorized.actor.organization_id,
        user_id=authorized.actor.user_id,
        project_id=authorized.project_id,
    )
    return _save_audited_task(
        request,
        authorized,
        task,
        expected_revision=payload.revision,
        action="article.knowledge_coverage.checked",
        details={
            "status": report.status,
            "eligible_sentences": report.eligible_sentences,
            "supported_sentences": report.supported_sentences,
            "hard_fact_sentences": report.hard_fact_sentences,
            "supported_hard_fact_sentences": (
                report.supported_hard_fact_sentences
            ),
        },
    )


@router.get(
    "/{project}/tasks/{task_id}/checks/initial-ai/screenshot/download",
    response_model=ProjectAssetDownload,
)
def create_project_task_initial_ai_screenshot_download(
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
    asset_id = task.initial_ai_check.screenshot_asset_id.strip()
    if not asset_id:
        raise HTTPException(
            status_code=404,
            detail="The initial AI-rate screenshot has not been uploaded.",
        )
    try:
        url = _knowledge_object_service(
            request
        ).create_initial_ai_screenshot_download_url(
            actor=authorized.actor,
            project_id=authorized.project_id,
            asset_id=asset_id,
            content_hash=(
                task.initial_ai_check.screenshot_content_hash
            ),
            width=task.initial_ai_check.screenshot_width or 0,
            height=task.initial_ai_check.screenshot_height or 0,
            filename=(
                task.initial_ai_check.screenshot_filename
                or "initial-ai-rate.png"
            ),
            expires_seconds=expires_seconds,
        )
    except ProjectAccessDenied as exc:
        raise HTTPException(
            status_code=403,
            detail="project access denied",
        ) from exc
    except KnowledgeObjectNotFound:
        raise HTTPException(
            status_code=404,
            detail="AI-rate screenshot was not found in the requested project.",
        ) from None
    except ObjectStoreError as exc:
        raise HTTPException(
            status_code=503,
            detail="AI-rate screenshot download is temporarily unavailable.",
        ) from exc
    return ProjectAssetDownload(
        asset_id=asset_id,
        url=url,
        expires_seconds=expires_seconds,
        filename=(
            task.initial_ai_check.screenshot_filename
            or "initial-ai-rate.png"
        ),
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
            filename=file.filename or "",
            content_type=file.content_type or "",
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
    if (
        payload.confirmed
        and not previous.screenshot_asset_id.strip()
        and not task.humanization_skipped
    ):
        raise HTTPException(
            status_code=409,
            detail="Upload the final AI-rate screenshot before confirming.",
        )
    if (
        payload.confirmed
        and task.humanization_skipped
        and payload.score is None
    ):
        raise HTTPException(
            status_code=422,
            detail="AI-rate score is required when reconfirming a skipped article.",
        )
    task.final_ai_check = AICheck(
        confirmed=payload.confirmed,
        deferred=payload.deferred,
        score=payload.score,
        report=payload.report,
        provider=previous.provider,
        checked_at=previous.checked_at,
        screenshot_path="",
        screenshot_asset_id=previous.screenshot_asset_id,
        screenshot_content_hash=previous.screenshot_content_hash,
        screenshot_filename=previous.screenshot_filename,
        screenshot_width=previous.screenshot_width,
        screenshot_height=previous.screenshot_height,
        confirmed_at=now_iso() if payload.confirmed else "",
        article_hash=content_hash(task.humanized_article),
    )
    if (payload.confirmed or payload.deferred) and task.status == STATUS_HUMANIZED_READY:
        transition_task(task, STATUS_FINAL_AI_CHECKED)
    return _save_audited_task(
        request,
        authorized,
        task,
        expected_revision=payload.revision,
        action="article.final_ai_check.updated",
        details={
            "confirmed": payload.confirmed,
            "deferred": payload.deferred,
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
                width=task.final_ai_check.screenshot_width,
                height=task.final_ai_check.screenshot_height,
                filename=(
                    task.final_ai_check.screenshot_filename
                    or "final-ai-rate.png"
                ),
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
        filename=(
            task.final_ai_check.screenshot_filename
            or "final-ai-rate.png"
        ),
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
        task_product_ids = tuple(
            product.product_id
            for product in task.products
            if product.product_id
        )
        available_assets = _project_catalog(request).image_asset_ids(
            authorized.project_id,
            task_product_ids,
        )
        selected_product_assets = {
            product.product_id: payload.product_asset_ids.get(
                product.product_id,
                product.selected_asset_id,
            )
            for product in task.products
            if product.product_id
            and payload.product_asset_ids.get(
                product.product_id,
                product.selected_asset_id,
            )
        }
        if payload.hero_asset_id not in {
            asset_id
            for values in available_assets.values()
            for asset_id in values
        }:
            raise ServerArticleImageError(
                "hero image must belong to a selected product"
            )
        if (
            not set(payload.product_asset_ids).issubset(task_product_ids)
            or any(
                asset_id not in available_assets.get(product_id, set())
                for product_id, asset_id in selected_product_assets.items()
            )
        ):
            raise ServerArticleImageError(
                "product images must belong to their selected products"
            )
        ServerArticleImagePreparation(
            _knowledge_object_service(request)
        ).prepare(
            actor=authorized.actor,
            project_id=authorized.project_id,
            task=task,
            hero_asset_id=payload.hero_asset_id,
            product_asset_ids=selected_product_assets,
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
                filename=task.docx_filename or "article.docx",
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
        filename=task.docx_filename or "article.docx",
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
            llm_factory=getattr(
                request.app.state,
                "server_llm_client_factory",
                None,
            ),
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
                filename=task.tdk_filename or "D.docx",
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
        filename=task.tdk_filename or "D.docx",
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
            "file_count": (
                len(task.images)
                + 2
                + int(bool(task.final_ai_check.screenshot_asset_id))
            ),
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
                filename=(
                    task.delivery_package_filename or "delivery.zip"
                ),
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
        filename=(
            task.delivery_package_filename or "delivery.zip"
        ),
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
    "ArticleGenerationJobResponse",
    "ArticleSectionRewriteRequest",
    "ConfirmedProductsUpdateRequest",
    "HumanizeGenerationJobResponse",
    "LinkRestorationJobResponse",
    "OutlineGenerationJobResponse",
    "ProductRediscoveryJobResponse",
    "ProductRediscoveryRequest",
    "ProjectAssetDownload",
    "ProjectCatalogImageAssetResponse",
    "ProjectCatalogProductResponse",
    "ProjectCatalogResponse",
    "ProjectSeoReviewApplyRequest",
    "ProjectSeoReviewChangeRequest",
    "ProjectSeoReviewCompleteRequest",
    "ProjectSeoReviewSettingsRequest",
    "SeoReviewGenerationJobResponse",
    "TitleGenerationJobResponse",
    "require_server_actor",
    "require_server_project_access",
    "router",
]
