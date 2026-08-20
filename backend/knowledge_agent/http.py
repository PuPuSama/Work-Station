from __future__ import annotations

import asyncio
from collections.abc import Mapping
from hashlib import sha256
import logging
from pathlib import PurePath
from datetime import datetime, timezone
from time import monotonic
from typing import Annotated, Literal
from urllib.parse import quote
import uuid

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from services.access_control import ActorIdentity, ProjectAccessDenied
from services.job_queue import ActiveJobError, JobConflict
from services.server_knowledge_commands import (
    PostgresServerKnowledgeCommands,
    ServerKnowledgeCommandUnavailable,
)
from services.server_knowledge_research import (
    ServerKnowledgeResearchRegistry,
    ServerKnowledgeResearchUnavailable,
    is_server_generated_retrieval_plan,
)
from services.server_private_document_ingestion import (
    PostgresServerPrivateDocumentIngestion,
    ServerPrivateDocumentUploadConflict,
    ServerPrivateDocumentUploadUnavailable,
    default_private_source_id,
)
from services.server_product_rediscovery import (
    OfficialSiteScanCommand,
    ProductRediscoveryCommand,
    ProductRediscoveryUnavailable,
    ServerProductRediscoveryRegistry,
)
from services.server_snapshot_evidence import (
    PostgresServerSnapshotEvidenceService,
    SnapshotEvidenceNotFound,
    SnapshotEvidenceUnavailable,
)

from .artifact_store import ArtifactStoreError
from .assets import KnowledgeAssetRepositoryError
from .catalog import (
    MANUAL_SPECIFICATION_TABLES_KEY,
    KnowledgeProduct,
    ProductCatalogRepositoryError,
    effective_product_specification_tables,
)
from .contracts import (
    EvidenceLink,
    EvidencePack,
    HardFactSentenceTarget,
    KnowledgeProject,
    KnowledgeSource,
    ParagraphEvidenceTarget,
    RetrievalPlan,
    RetrievalScope,
)
from .evidence import calculate_knowledge_coverage
from .evidence_repository import EvidenceRepositoryError
from .hybrid_retriever import HybridRetrievalConfigurationError
from .ingestion import DocumentInput, DocumentParserError
from .embedding import EmbeddingProviderError
from .library import KnowledgeSourceSummary
from .repository import KnowledgeRecordNotFound, KnowledgeRepositoryError
from .publication import KnowledgePublicationError
from .snapshot_reviews import (
    SnapshotReviewConflict,
    SnapshotReviewRepositoryError,
)
from .runtime import KnowledgeAgentRuntime
from .research_graph import ResearchGraphRequest, new_research_thread_id
from .research_runs import (
    GapFillAttempt,
    ResearchGraphEvent,
    ResearchGraphRun,
    ResearchRunConflictError,
    ResearchRunNotFound,
    ResearchRunRepositoryError,
    TERMINAL_RESEARCH_STATUSES,
)
from .research_stream import encode_sse, resolve_after_sequence
from .research_chat import (
    ResearchAnswerProviderError,
    ResearchChatError,
)
from .research_chat_repository import (
    ResearchChatRepositoryError,
    ResearchConversation,
)
from .scope_evidence import ScopeEvidenceNotFound, ScopeEvidenceService
from .security import require_knowledge_project_access
from .wordpress import (
    OfficialSiteFetchError,
    UnsafeOfficialSiteUrl,
    WordPressIngestionError,
    same_official_site,
)


MAX_KNOWLEDGE_UPLOAD_BYTES = 100 * 1024 * 1024
TrustTierValue = Literal[
    "hard_fact",
    "reference_material",
    "writing_instruction",
]
ReviewModeValue = Literal["manual", "automatic"]


class KnowledgeApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class KnowledgeSourceResponse(KnowledgeApiModel):
    project_id: str
    source_id: str
    display_name: str
    source_kind: str
    trust_tier: str
    status: str
    canonical_url: str | None
    current_snapshot_id: str | None
    latest_snapshot_id: str | None
    pending_snapshot_id: str | None
    pending_fetched_at: str | None
    pending_chunk_count: int
    pending_asset_count: int
    pending_review_decision: str | None
    pending_review_reason: str | None
    pending_review_version: int | None
    pending_reviewed_at: str | None
    snapshot_count: int
    chunk_count: int
    asset_count: int
    latest_fetched_at: str | None
    classification_reason: str
    review_decision: str | None
    raw_evidence_url: str | None


class KnowledgeProductResponse(KnowledgeApiModel):
    project_id: str
    product_id: str
    name: str
    status: str
    canonical_url: str | None
    category_path: list[str]
    description: str
    main_content_facts: list[str]
    specification_tables: list[dict[str, object]]
    specification_tables_overridden: bool = False
    faq: list[dict[str, str]]


class ProductSpecificationTableUpdate(KnowledgeApiModel):
    caption: str = Field(default="", max_length=500)
    headers: list[str] = Field(default_factory=list, max_length=40)
    rows: list[list[str]] = Field(default_factory=list, max_length=500)


class ProductSpecificationsUpdateRequest(KnowledgeApiModel):
    specification_tables: list[ProductSpecificationTableUpdate] = Field(
        default_factory=list,
        max_length=20,
    )


class KnowledgeLibraryResponse(KnowledgeApiModel):
    project_id: str
    source_count: int
    inbox_count: int
    pending_count: int
    published_count: int
    product_count: int
    confirmed_product_count: int
    asset_count: int
    sources: list[KnowledgeSourceResponse]
    products: list[KnowledgeProductResponse]


class KnowledgeUploadResponse(KnowledgeApiModel):
    project_id: str
    source_id: str
    snapshot_id: str
    status: str
    parser_name: str
    parser_version: str
    chunk_count: int
    asset_count: int
    created: bool
    message: str
    review_mode: ReviewModeValue = "manual"
    review_decision: str | None = None
    published: bool = False


class ProductConfirmResponse(KnowledgeApiModel):
    project_id: str
    product_id: str
    status: str


class KnowledgeSourceReviewRequest(KnowledgeApiModel):
    source_kind: Literal[
        "private_file",
        "product_detail",
        "product_category",
        "official_blog",
        "knowledge_page",
    ]
    trust_tier: TrustTierValue
    decision: Literal["approve", "needs_review", "reject"]
    reason: str = Field(min_length=1, max_length=500)


class KnowledgeSourceReviewResponse(KnowledgeApiModel):
    project_id: str
    source_id: str
    status: str
    decision: str


class KnowledgeSnapshotReviewRequest(KnowledgeSourceReviewRequest):
    receipt_id: str = Field(min_length=1, max_length=200)


class KnowledgeSnapshotReviewResponse(KnowledgeSourceReviewResponse):
    snapshot_id: str
    receipt_id: str
    review_version: int


class KnowledgePublicationResponse(KnowledgeApiModel):
    project_id: str
    source_id: str
    snapshot_id: str
    status: str
    embedding_model: str
    chunk_count: int


class SnapshotEvidenceManifestResponse(KnowledgeApiModel):
    project_id: str
    source_id: str
    snapshot_id: str
    slot: Literal["current", "pending"]
    raw_available: bool
    normalized_available: bool
    raw_content_type: str | None
    raw_byte_size: int | None
    normalized_content_type: str | None
    normalized_byte_size: int | None
    preview_supported: bool


class SnapshotEvidencePreviewResponse(KnowledgeApiModel):
    project_id: str
    source_id: str
    snapshot_id: str
    slot: Literal["current", "pending"]
    text: str
    truncated: bool
    block_count: int


class SnapshotEvidenceDownloadResponse(KnowledgeApiModel):
    project_id: str
    source_id: str
    snapshot_id: str
    slot: Literal["current", "pending"]
    download_url: str
    expires_seconds: int


class WordPressProbeRequest(KnowledgeApiModel):
    site_url: str | None = Field(default=None, max_length=2048)


class WordPressProbeResponse(KnowledgeApiModel):
    project_id: str
    site_url: str
    detected: bool
    rest_api_url: str | None
    namespaces: list[str]
    route_count: int
    reason: str
    probe_version: str


class WordPressSyncRequest(KnowledgeApiModel):
    category_url: str = Field(min_length=1, max_length=4096)
    site_url: str | None = Field(default=None, max_length=2048)
    max_products: int = Field(default=12, ge=1, le=50)


class WordPressSyncedPageResponse(KnowledgeApiModel):
    source_id: str
    snapshot_id: str
    page_type: str
    canonical_url: str
    status: str
    product_id: str | None
    asset_count: int
    warnings: list[str]


class WordPressSyncResponse(KnowledgeApiModel):
    project_id: str
    wordpress_detected: bool
    category: WordPressSyncedPageResponse
    products: list[WordPressSyncedPageResponse]
    skipped_urls: list[str]
    warnings: list[str]


class OfficialSiteScanRequest(KnowledgeApiModel):
    start_url: str = Field(default="", max_length=4096)
    max_pages: int = Field(
        default=100,
        ge=1,
        le=500,
        description="Total HTML page budget for this official-site scan.",
    )


class OfficialSiteScanResponse(KnowledgeApiModel):
    accepted: bool
    message: str
    scan_id: str
    status: str


class OfficialSiteScanStatusResponse(KnowledgeApiModel):
    scan_id: str | None
    status: str
    started_at: str | None
    finished_at: str | None
    processed_pages: int
    skipped_pages: int
    processed_products: int
    skipped_products: int
    source_count: int
    product_count: int
    error: str


class RetrievalScopeInput(KnowledgeApiModel):
    scope_id: str = Field(min_length=1, max_length=200)
    ordinal: int = Field(ge=0)
    scope_type: Literal["introduction", "h2_section", "product_fact", "faq"]
    scope_key: str = Field(min_length=1, max_length=500)
    title: str = Field(min_length=1, max_length=500)
    query_variants: list[str] = Field(min_length=1, max_length=20)
    filters: dict[str, object] = Field(default_factory=dict)
    minimum_hits: int = Field(default=2, ge=1, le=50)
    minimum_distinct_sources: int = Field(default=1, ge=1, le=20)
    require_hard_fact: bool = False
    metadata: dict[str, object] = Field(default_factory=dict)


class RetrievalPlanCreateRequest(KnowledgeApiModel):
    retrieval_plan_id: str = Field(min_length=1, max_length=200)
    article_id: str = Field(min_length=1, max_length=200)
    outline_version: int = Field(ge=1)
    max_gap_fill_rounds: int = Field(default=2, ge=0, le=2)
    scopes: list[RetrievalScopeInput] = Field(min_length=1, max_length=100)
    metadata: dict[str, object] = Field(default_factory=dict)


class RetrievalScopeResponse(RetrievalScopeInput):
    project_id: str
    retrieval_plan_id: str


class RetrievalPlanResponse(KnowledgeApiModel):
    project_id: str
    retrieval_plan_id: str
    article_id: str
    outline_version: int
    max_gap_fill_rounds: int
    scopes: list[RetrievalScopeResponse]
    metadata: dict[str, object]
    created_at: str


class EvidencePackBuildRequest(KnowledgeApiModel):
    limit: int = Field(default=8, ge=1, le=50)


class EvidenceProvenanceResponse(KnowledgeApiModel):
    source_id: str
    snapshot_id: str
    display_name: str
    source_kind: str
    trust_tier: str
    public_source: bool
    canonical_url: str | None
    fetched_at: str | None


class EvidenceHitResponse(KnowledgeApiModel):
    chunk_id: str
    text: str
    heading_path: list[str]
    locator: dict[str, object]
    score: float
    provenance: EvidenceProvenanceResponse | None
    explanation: dict[str, object]


class EvidencePackResponse(KnowledgeApiModel):
    project_id: str
    evidence_pack_id: str
    retrieval_plan_id: str
    scope_id: str
    article_id: str
    outline_version: int
    scope_type: str
    scope_key: str
    sufficiency: str
    gap_reasons: list[str]
    hard_fact_chunk_ids: list[str]
    public_citation_urls: list[str]
    hits: list[EvidenceHitResponse]
    created_at: str


class EvidenceLinkWriteRequest(KnowledgeApiModel):
    evidence_link_id: str = Field(min_length=1, max_length=200)
    article_id: str = Field(min_length=1, max_length=200)
    paragraph_id: str = Field(min_length=1, max_length=200)
    paragraph_hash: str = Field(min_length=64, max_length=64)
    chunk_id: str = Field(min_length=1, max_length=500)
    support_scope: Literal["paragraph", "sentence"] = "paragraph"
    claim_type: Literal["reference", "hard_fact"] = "reference"
    support_type: Literal["direct", "paraphrase", "contextual"] = "paraphrase"
    sentence_id: str | None = Field(default=None, max_length=200)
    visible_words: int = Field(default=0, ge=0)
    public_citation_url: str | None = Field(default=None, max_length=4096)
    validation_status: Literal["valid", "needs_review", "invalid"] = "valid"
    metadata: dict[str, object] = Field(default_factory=dict)


class EvidenceLinkResponse(EvidenceLinkWriteRequest):
    project_id: str


class ParagraphCoverageInput(KnowledgeApiModel):
    paragraph_id: str = Field(min_length=1, max_length=200)
    paragraph_hash: str = Field(min_length=64, max_length=64)
    visible_words: int = Field(ge=0)
    eligible: bool = True


class HardFactCoverageInput(KnowledgeApiModel):
    paragraph_id: str = Field(min_length=1, max_length=200)
    sentence_id: str = Field(min_length=1, max_length=200)
    paragraph_hash: str = Field(min_length=64, max_length=64)


class KnowledgeCoverageRequest(KnowledgeApiModel):
    paragraphs: list[ParagraphCoverageInput]
    hard_fact_sentences: list[HardFactCoverageInput] = Field(default_factory=list)


class KnowledgeCoverageResponse(KnowledgeApiModel):
    article_id: str
    eligible_paragraphs: int
    supported_paragraphs: int
    paragraph_coverage: float
    hard_fact_sentences: int
    supported_hard_fact_sentences: int
    hard_fact_coverage: float


class ParagraphHashReviewRequest(KnowledgeApiModel):
    paragraph_id: str = Field(min_length=1, max_length=200)
    current_paragraph_hash: str = Field(min_length=64, max_length=64)


class ParagraphHashReviewResponse(KnowledgeApiModel):
    article_id: str
    paragraph_id: str
    marked_needs_review: int


class ResearchRunCreateRequest(KnowledgeApiModel):
    organization_id: str | None = Field(default=None, max_length=200)
    request_id: str | None = Field(default=None, max_length=200)
    retrieval_plan_id: str = Field(min_length=1, max_length=200)
    max_discovery_queries: int = Field(default=2, ge=0, le=20)


class ResearchRunResumeRequest(KnowledgeApiModel):
    request_id: str | None = Field(default=None, max_length=200)
    approved_candidate_ids: list[
        Annotated[str, Field(min_length=1, max_length=200)]
    ] = Field(default_factory=list, max_length=20)
    approved_urls: list[
        Annotated[str, Field(min_length=1, max_length=4096)]
    ] = Field(default_factory=list, max_length=20)


class ResearchRunResponse(KnowledgeApiModel):
    project_id: str
    thread_id: str
    organization_id: str
    retrieval_plan_id: str
    article_id: str
    outline_version: int
    status: str
    current_node: str
    current_scope_id: str | None
    gap_fill_round: int
    max_gap_fill_rounds: int
    discovery_queries_used: int
    max_discovery_queries: int
    evidence_pack_ids: list[str]
    warnings: list[str]
    error_code: str | None
    error_message: str | None
    created_at: str | None
    updated_at: str | None
    finished_at: str | None


class ResearchEventResponse(KnowledgeApiModel):
    sequence: int
    event_type: str
    node_name: str
    scope_id: str | None
    attempt: int
    details: dict[str, object]
    created_at: str | None


class GapFillAttemptResponse(KnowledgeApiModel):
    scope_id: str
    round_number: int
    attempt_id: str
    reason: str
    channel: str
    query: str
    discovered_urls: list[str]
    published_source_ids: list[str]
    result: str
    cost_usage: dict[str, object]
    created_at: str | None
    updated_at: str | None


class ResearchCandidateEvidenceResponse(KnowledgeApiModel):
    reason: str | None = None
    channel: str | None = None
    same_site: bool | None = None
    score: float | None = None
    reused_attempt: bool | None = None


class ResearchReviewCandidateResponse(KnowledgeApiModel):
    candidate_id: str
    url: str
    page_type: str
    needs_review: bool
    evidence: ResearchCandidateEvidenceResponse


class ResearchRunDetailResponse(ResearchRunResponse):
    events: list[ResearchEventResponse]
    gap_fill_attempts: list[GapFillAttemptResponse]
    review_candidates: list[ResearchReviewCandidateResponse]


class ResearchRunQueuedResponse(KnowledgeApiModel):
    run: ResearchRunResponse
    queue_batch_id: str
    queue_job_id: str


class ResearchChatAskRequest(KnowledgeApiModel):
    request_id: str = Field(min_length=1, max_length=120)
    question: str = Field(min_length=1, max_length=4000)
    conversation_id: str | None = Field(default=None, max_length=200)
    article_id: str | None = Field(default=None, max_length=200)
    limit: int = Field(default=8, ge=1, le=20)


class ResearchCitationResponse(KnowledgeApiModel):
    chunk_id: str
    source_id: str
    snapshot_id: str
    display_name: str
    canonical_url: str | None
    text: str
    ordinal: int
    locator: dict[str, object]


class ResearchMessageResponse(KnowledgeApiModel):
    message_id: str
    request_id: str
    sequence: int
    role: str
    content: str
    citations: list[ResearchCitationResponse]
    created_at: str | None


class ResearchConversationResponse(KnowledgeApiModel):
    project_id: str
    conversation_id: str
    article_id: str | None
    messages: list[ResearchMessageResponse]
    created_at: str | None
    updated_at: str | None
    expires_at: str | None


def _runtime(request: Request) -> KnowledgeAgentRuntime:
    runtime = getattr(request.app.state, "knowledge_agent_runtime", None)
    if runtime is None:
        raise HTTPException(status_code=404, detail="Knowledge Agent is disabled.")
    return runtime


def _server_knowledge_context(
    request: Request,
    project: str,
) -> tuple[ActorIdentity, str]:
    """Return the trusted authorization context for every knowledge request."""
    actor = getattr(request.state, "actor_identity", None)
    project_id = str(getattr(request.state, "project_id", "")).strip()
    if not isinstance(actor, ActorIdentity) or not project_id:
        raise HTTPException(
            status_code=503,
            detail="Server authorization context is not available.",
        )
    if project_id != _project_id(project):
        raise HTTPException(status_code=403, detail="project access denied")
    return actor, project_id


def _server_knowledge_commands(
    request: Request,
    runtime: KnowledgeAgentRuntime,
) -> PostgresServerKnowledgeCommands:
    configured = getattr(
        request.app.state,
        "server_knowledge_commands",
        None,
    )
    if isinstance(configured, PostgresServerKnowledgeCommands):
        return configured
    return PostgresServerKnowledgeCommands(
        runtime.engine,
        repository=runtime.repository,
        catalog=runtime.catalog_repository,
        publication=runtime.publication,
    )


def _server_private_document_ingestion(
    request: Request,
) -> PostgresServerPrivateDocumentIngestion:
    configured = getattr(
        request.app.state,
        "server_private_document_ingestion",
        None,
    )
    if not isinstance(
        configured,
        PostgresServerPrivateDocumentIngestion,
    ):
        raise HTTPException(
            status_code=503,
            detail="Server private document ingestion is not available.",
        )
    return configured


def _server_snapshot_evidence(
    request: Request,
) -> PostgresServerSnapshotEvidenceService:
    configured = getattr(
        request.app.state,
        "server_snapshot_evidence",
        None,
    )
    if not isinstance(
        configured,
        PostgresServerSnapshotEvidenceService,
    ):
        raise HTTPException(
            status_code=503,
            detail="Server Snapshot evidence is not available.",
        )
    return configured


def _research_enqueue(request: Request):
    enqueue = getattr(request.app.state, "knowledge_research_enqueue", None)
    if not callable(enqueue):
        raise HTTPException(
            status_code=503,
            detail="Knowledge research queue is not available.",
        )
    return enqueue


def _server_research(
    request: Request,
) -> ServerKnowledgeResearchRegistry:
    configured = getattr(
        request.app.state,
        "server_knowledge_research",
        None,
    )
    if not isinstance(configured, ServerKnowledgeResearchRegistry):
        raise HTTPException(
            status_code=503,
            detail="Server knowledge research is not available.",
        )
    return configured


def _server_product_rediscovery(
    request: Request,
) -> ServerProductRediscoveryRegistry:
    configured = getattr(
        request.app.state,
        "server_product_rediscovery",
        None,
    )
    if not isinstance(configured, ServerProductRediscoveryRegistry):
        raise HTTPException(
            status_code=503,
            detail="Official-site scanning is not available.",
        )
    return configured


def _project_id(value: str) -> str:
    normalized = value.strip().lower().rstrip(".")
    if normalized.startswith("www."):
        normalized = normalized[4:]
    return normalized


def _automatic_review_receipt_id(
    *,
    organization_id: str,
    project_id: str,
    source_id: str,
    snapshot_id: str,
) -> str:
    return "auto_review_" + uuid.uuid5(
        uuid.NAMESPACE_URL,
        "\x1f".join(
            (organization_id, project_id, source_id, snapshot_id, "v2")
        ),
    ).hex


def _source_response(
    item: KnowledgeSourceSummary,
    *,
    include_raw_evidence_url: bool = True,
) -> KnowledgeSourceResponse:
    snapshot_id = item.current_snapshot_id or item.latest_snapshot_id
    raw_url = (
        None
        if snapshot_id is None or not include_raw_evidence_url
        else (
            f"/api/knowledge/{quote(item.project_id, safe='')}/sources/"
            f"{quote(item.source_id, safe='')}/snapshots/"
            f"{quote(snapshot_id, safe='')}/raw"
        )
    )
    return KnowledgeSourceResponse(
        project_id=item.project_id,
        source_id=item.source_id,
        display_name=item.display_name,
        source_kind=item.source_kind,
        trust_tier=item.trust_tier,
        status=item.status,
        canonical_url=item.canonical_url,
        current_snapshot_id=item.current_snapshot_id,
        latest_snapshot_id=item.latest_snapshot_id,
        pending_snapshot_id=item.pending_snapshot_id,
        pending_fetched_at=(
            None
            if item.pending_fetched_at is None
            else item.pending_fetched_at.isoformat()
        ),
        pending_chunk_count=item.pending_chunk_count,
        pending_asset_count=item.pending_asset_count,
        pending_review_decision=item.pending_review_decision,
        pending_review_reason=item.pending_review_reason,
        pending_review_version=item.pending_review_version,
        pending_reviewed_at=(
            None
            if item.pending_reviewed_at is None
            else item.pending_reviewed_at.isoformat()
        ),
        snapshot_count=item.snapshot_count,
        chunk_count=item.chunk_count,
        asset_count=item.asset_count,
        latest_fetched_at=(
            None
            if item.latest_fetched_at is None
            else item.latest_fetched_at.isoformat()
        ),
        classification_reason=item.classification_reason,
        review_decision=item.review_decision,
        raw_evidence_url=raw_url,
    )


def _product_response(item: KnowledgeProduct) -> KnowledgeProductResponse:
    description = item.metadata.get("description", "")
    facts = item.metadata.get("main_content_facts", [])
    tables = effective_product_specification_tables(item.metadata)
    faq = item.metadata.get("faq", [])
    return KnowledgeProductResponse(
        project_id=item.project_id,
        product_id=item.product_id,
        name=item.name,
        status=item.status,
        canonical_url=item.canonical_url,
        category_path=list(item.category_path),
        description=description if isinstance(description, str) else "",
        main_content_facts=(
            [str(value) for value in facts] if isinstance(facts, list) else []
        ),
        specification_tables=(
            [dict(value) for value in tables if isinstance(value, Mapping)]
            if isinstance(tables, list)
            else []
        ),
        specification_tables_overridden=(
            MANUAL_SPECIFICATION_TABLES_KEY in item.metadata
        ),
        faq=(
            [
                {str(key): str(value) for key, value in entry.items()}
                for entry in faq
                if isinstance(entry, Mapping)
            ]
            if isinstance(faq, list)
            else []
        ),
    )


def _official_site_for_project(project_id: str, requested: str | None) -> str:
    site_url = (requested or "").strip() or f"https://{project_id}"
    if not same_official_site(f"https://{project_id}", site_url):
        raise HTTPException(
            status_code=422,
            detail="site_url must belong to the requested project domain.",
        )
    return site_url


def _synced_page_response(item) -> WordPressSyncedPageResponse:
    return WordPressSyncedPageResponse(
        source_id=item.source.source_id,
        snapshot_id=item.snapshot.snapshot_id,
        page_type=item.classification.page_type,
        canonical_url=item.classification.canonical_url,
        status=item.source.status,
        product_id=(None if item.product is None else item.product.product_id),
        asset_count=len(item.assets),
        warnings=list(item.warnings),
    )


def _plan_response(plan: RetrievalPlan) -> RetrievalPlanResponse:
    return RetrievalPlanResponse(
        project_id=plan.project_id,
        retrieval_plan_id=plan.retrieval_plan_id,
        article_id=plan.article_id,
        outline_version=plan.outline_version,
        max_gap_fill_rounds=plan.max_gap_fill_rounds,
        metadata=dict(plan.metadata),
        created_at=plan.created_at.isoformat(),
        scopes=[
            RetrievalScopeResponse(
                project_id=scope.project_id,
                retrieval_plan_id=scope.retrieval_plan_id,
                scope_id=scope.scope_id,
                ordinal=scope.ordinal,
                scope_type=scope.scope_type,
                scope_key=scope.scope_key,
                title=scope.title,
                query_variants=list(scope.query_variants),
                filters=dict(scope.filters),
                minimum_hits=scope.minimum_hits,
                minimum_distinct_sources=scope.minimum_distinct_sources,
                require_hard_fact=scope.require_hard_fact,
                metadata=dict(scope.metadata),
            )
            for scope in plan.scopes
        ],
    )


def _pack_response(pack: EvidencePack) -> EvidencePackResponse:
    request = pack.request
    if request.retrieval_plan_id is None or request.scope_id is None:
        raise ValueError("persisted evidence pack is missing plan scope identity")
    return EvidencePackResponse(
        project_id=pack.project_id,
        evidence_pack_id=pack.evidence_pack_id,
        retrieval_plan_id=request.retrieval_plan_id,
        scope_id=request.scope_id,
        article_id=request.article_id,
        outline_version=request.outline_version,
        scope_type=request.scope_type,
        scope_key=request.scope_key,
        sufficiency=pack.sufficiency,
        gap_reasons=list(pack.gap_reasons),
        hard_fact_chunk_ids=list(pack.hard_fact_chunk_ids),
        public_citation_urls=list(pack.public_citation_urls),
        created_at=pack.created_at.isoformat(),
        hits=[
            EvidenceHitResponse(
                chunk_id=hit.chunk.chunk_id,
                text=hit.chunk.text,
                heading_path=list(hit.chunk.heading_path),
                locator=dict(hit.chunk.locator),
                score=hit.score,
                provenance=(
                    None
                    if hit.provenance is None
                    else EvidenceProvenanceResponse(
                        source_id=hit.provenance.source_id,
                        snapshot_id=hit.provenance.snapshot_id,
                        display_name=hit.provenance.display_name,
                        source_kind=hit.provenance.source_kind,
                        trust_tier=hit.provenance.trust_tier,
                        public_source=hit.provenance.public_source,
                        canonical_url=hit.provenance.canonical_url,
                        fetched_at=(
                            None
                            if hit.provenance.fetched_at is None
                            else hit.provenance.fetched_at.isoformat()
                        ),
                    )
                ),
                explanation=dict(hit.explanation),
            )
            for hit in pack.hits
        ],
    )


def _link_response(link: EvidenceLink) -> EvidenceLinkResponse:
    return EvidenceLinkResponse(
        project_id=link.project_id,
        evidence_link_id=link.evidence_link_id,
        article_id=link.article_id,
        paragraph_id=link.paragraph_id,
        sentence_id=link.sentence_id,
        paragraph_hash=link.paragraph_hash,
        chunk_id=link.chunk_id,
        support_scope=link.support_scope,
        claim_type=link.claim_type,
        support_type=link.support_type,
        visible_words=link.visible_words,
        public_citation_url=link.public_citation_url,
        validation_status=link.validation_status,
        metadata=dict(link.metadata),
    )


def _research_run_response(run: ResearchGraphRun) -> ResearchRunResponse:
    return ResearchRunResponse(
        project_id=run.project_id,
        thread_id=run.thread_id,
        organization_id=run.organization_id,
        retrieval_plan_id=run.retrieval_plan_id,
        article_id=run.article_id,
        outline_version=run.outline_version,
        status=run.status,
        current_node=run.current_node,
        current_scope_id=run.current_scope_id,
        gap_fill_round=run.gap_fill_round,
        max_gap_fill_rounds=run.max_gap_fill_rounds,
        discovery_queries_used=run.discovery_queries_used,
        max_discovery_queries=run.max_discovery_queries,
        evidence_pack_ids=list(run.evidence_pack_ids),
        warnings=list(run.warnings),
        error_code=run.error_code,
        error_message=run.error_message,
        created_at=(run.created_at.isoformat() if run.created_at else None),
        updated_at=(run.updated_at.isoformat() if run.updated_at else None),
        finished_at=(run.finished_at.isoformat() if run.finished_at else None),
    )


def _research_event_response(
    event: ResearchGraphEvent,
    *,
    server_mode: bool = False,
) -> ResearchEventResponse:
    details = dict(event.details)
    if server_mode:
        allowed = {
            "approved_url_count",
            "discovery_queries_used",
            "error_code",
            "gap_fill_round",
            "outline_version",
            "status",
            "warning_count",
        }
        details = {
            key: value
            for key, value in details.items()
            if key in allowed
            and isinstance(value, (bool, int, float, str))
        }
    return ResearchEventResponse(
        sequence=event.sequence,
        event_type=event.event_type,
        node_name=event.node_name,
        scope_id=event.scope_id,
        attempt=event.attempt,
        details=details,
        created_at=(event.created_at.isoformat() if event.created_at else None),
    )


def _gap_attempt_response(
    attempt: GapFillAttempt,
    *,
    server_mode: bool = False,
) -> GapFillAttemptResponse:
    cost_usage = dict(attempt.cost_usage)
    if server_mode:
        allowed_cost_fields = {
            "queries",
            "result_count",
            "request_id_present",
        }
        cost_usage = {
            key: value
            for key, value in cost_usage.items()
            if key in allowed_cost_fields
            and isinstance(value, (bool, int, float))
        }
    return GapFillAttemptResponse(
        scope_id=attempt.scope_id,
        round_number=attempt.round_number,
        attempt_id=attempt.attempt_id,
        reason=attempt.reason,
        channel=attempt.channel,
        query=("" if server_mode else attempt.query),
        discovered_urls=([] if server_mode else list(attempt.discovered_urls)),
        published_source_ids=list(attempt.published_source_ids),
        result=attempt.result,
        cost_usage=cost_usage,
        created_at=(attempt.created_at.isoformat() if attempt.created_at else None),
        updated_at=(attempt.updated_at.isoformat() if attempt.updated_at else None),
    )


def _review_candidate_response(
    candidate: dict[str, object],
) -> ResearchReviewCandidateResponse:
    raw_evidence = candidate.get("evidence")
    evidence = (
        dict(raw_evidence)
        if isinstance(raw_evidence, dict)
        else {}
    )
    return ResearchReviewCandidateResponse(
        candidate_id=str(candidate.get("candidate_id") or ""),
        url=str(candidate.get("url") or ""),
        page_type=str(candidate.get("page_type") or "unknown"),
        needs_review=True,
        evidence=ResearchCandidateEvidenceResponse(
            reason=(
                str(evidence["reason"])
                if isinstance(evidence.get("reason"), str)
                else None
            ),
            channel=(
                str(evidence["channel"])
                if isinstance(evidence.get("channel"), str)
                else None
            ),
            same_site=(
                bool(evidence["same_site"])
                if isinstance(evidence.get("same_site"), bool)
                else None
            ),
            score=(
                float(evidence["score"])
                if isinstance(evidence.get("score"), (int, float))
                and not isinstance(evidence.get("score"), bool)
                else None
            ),
            reused_attempt=(
                bool(evidence["reused_attempt"])
                if isinstance(evidence.get("reused_attempt"), bool)
                else None
            ),
        ),
    )


def _conversation_response(
    conversation: ResearchConversation,
) -> ResearchConversationResponse:
    return ResearchConversationResponse(
        project_id=conversation.project_id,
        conversation_id=conversation.conversation_id,
        article_id=conversation.article_id,
        created_at=(
            conversation.created_at.isoformat()
            if conversation.created_at is not None
            else None
        ),
        updated_at=(
            conversation.updated_at.isoformat()
            if conversation.updated_at is not None
            else None
        ),
        expires_at=(
            conversation.expires_at.isoformat()
            if conversation.expires_at is not None
            else None
        ),
        messages=[
            ResearchMessageResponse(
                message_id=message.message_id,
                request_id=message.request_id,
                sequence=message.sequence,
                role=message.role,
                content=message.content,
                created_at=(
                    message.created_at.isoformat()
                    if message.created_at is not None
                    else None
                ),
                citations=[
                    ResearchCitationResponse(
                        chunk_id=citation.chunk_id,
                        source_id=citation.source_id,
                        snapshot_id=citation.snapshot_id,
                        display_name=citation.display_name,
                        canonical_url=citation.canonical_url,
                        text=citation.text,
                        ordinal=citation.ordinal,
                        locator=dict(citation.locator),
                    )
                    for citation in message.citations
                ],
            )
            for message in conversation.messages
        ],
    )


logger = logging.getLogger(__name__)


router = APIRouter(
    prefix="/api/knowledge",
    tags=["knowledge-agent"],
    dependencies=[Depends(require_knowledge_project_access)],
)


def _run_official_site_scan(
    registry: ServerProductRediscoveryRegistry,
    *,
    actor: ActorIdentity,
    project_id: str,
    command: OfficialSiteScanCommand,
    scan_id: str,
) -> None:
    try:
        registry.run_manual_scan(
            actor=actor,
            project_id=project_id,
            command=command,
            scan_id=scan_id,
        )
    except Exception:
        logger.exception(
            "official-site scan failed; previous published snapshots remain active",
            extra={"project_id": project_id},
        )


@router.post(
    "/{project}/official-site/scan",
    response_model=OfficialSiteScanResponse,
    status_code=202,
)
def scan_official_site(
    project: str,
    payload: OfficialSiteScanRequest,
    request: Request,
    background_tasks: BackgroundTasks,
) -> OfficialSiteScanResponse:
    server_context = _server_knowledge_context(request, project)
    if server_context is None:
        raise HTTPException(
            status_code=503,
            detail="Official-site scanning is only available in server mode.",
        )
    actor, project_id = server_context
    registry = _server_product_rediscovery(request)
    try:
        scan = registry.begin_manual_scan(actor=actor, project_id=project_id)
    except ProjectAccessDenied as exc:
        raise HTTPException(
            status_code=403,
            detail="project access denied",
        ) from exc
    except ProductRediscoveryUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="Official-site scanning is not configured.",
        ) from exc
    except ActiveJobError as exc:
        raise HTTPException(
            status_code=409,
            detail="An official-site scan is already running for this project.",
        ) from exc
    # ponytail: keep project-level manual scans in-process; move them to the
    # durable project queue only if restart-resume becomes a real requirement.
    background_tasks.add_task(
        _run_official_site_scan,
        registry,
        actor=actor,
        project_id=project_id,
        command=OfficialSiteScanCommand(
            start_url=payload.start_url.strip(),
            max_pages=payload.max_pages,
        ),
        scan_id=scan.scan_id,
    )
    return OfficialSiteScanResponse(
        accepted=True,
        message="Official-site knowledge scan started; accepted pages publish automatically.",
        scan_id=scan.scan_id,
        status=scan.status,
    )


@router.get(
    "/{project}/official-site/scan/status",
    response_model=OfficialSiteScanStatusResponse,
)
def read_official_site_scan_status(
    project: str,
    request: Request,
) -> OfficialSiteScanStatusResponse:
    server_context = _server_knowledge_context(request, project)
    if server_context is None:
        raise HTTPException(
            status_code=503,
            detail="Official-site scanning is only available in server mode.",
        )
    actor, project_id = server_context
    try:
        scan = _server_product_rediscovery(request).manual_scan_status(
            actor=actor,
            project_id=project_id,
        )
    except ProjectAccessDenied as exc:
        raise HTTPException(status_code=403, detail="project access denied") from exc
    if scan is None:
        return OfficialSiteScanStatusResponse(
            scan_id=None,
            status="idle",
            started_at=None,
            finished_at=None,
            processed_pages=0,
            skipped_pages=0,
            processed_products=0,
            skipped_products=0,
            source_count=0,
            product_count=0,
            error="",
        )
    return OfficialSiteScanStatusResponse(
        scan_id=scan.scan_id,
        status=scan.status,
        started_at=scan.started_at,
        finished_at=scan.finished_at,
        processed_pages=scan.processed_pages,
        skipped_pages=scan.skipped_pages,
        processed_products=scan.processed_products,
        skipped_products=scan.skipped_products,
        source_count=scan.source_count,
        product_count=scan.product_count,
        error=scan.error,
    )


@router.post(
    "/{project}/wordpress/probe",
    response_model=WordPressProbeResponse,
)
def probe_wordpress_site(
    project: str,
    payload: WordPressProbeRequest,
    request: Request,
) -> WordPressProbeResponse:
    runtime = _runtime(request)
    project_id = _project_id(project)
    site_url = _official_site_for_project(project_id, payload.site_url)
    try:
        result = runtime.wordpress_sync.probe(site_url)
    except (UnsafeOfficialSiteUrl, WordPressIngestionError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return WordPressProbeResponse(
        project_id=project_id,
        site_url=result.site_url,
        detected=result.detected,
        rest_api_url=result.rest_api_url,
        namespaces=list(result.namespaces),
        route_count=result.route_count,
        reason=result.reason,
        probe_version=result.probe_version,
    )


@router.post(
    "/{project}/wordpress/sync",
    response_model=WordPressSyncResponse,
    status_code=201,
)
def sync_wordpress_category(
    project: str,
    payload: WordPressSyncRequest,
    request: Request,
) -> WordPressSyncResponse:
    runtime = _runtime(request)
    project_id = _project_id(project)
    site_url = _official_site_for_project(project_id, payload.site_url)
    try:
        runtime.repository.upsert_project(
            KnowledgeProject(
                project_id=project_id,
                customer_name=project,
                official_domain=project_id,
            )
        )
        result = runtime.wordpress_sync.sync_category(
            project_id=project_id,
            site_url=site_url,
            category_url=payload.category_url,
            max_products=payload.max_products,
        )
    except UnsafeOfficialSiteUrl as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except OfficialSiteFetchError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except (WordPressIngestionError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (
        KnowledgeRepositoryError,
        KnowledgeAssetRepositoryError,
        ProductCatalogRepositoryError,
        ArtifactStoreError,
    ) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return WordPressSyncResponse(
        project_id=project_id,
        wordpress_detected=result.probe.detected,
        category=_synced_page_response(result.category),
        products=[_synced_page_response(item) for item in result.products],
        skipped_urls=list(result.skipped_urls),
        warnings=list(result.warnings),
    )


@router.get("/{project}", response_model=KnowledgeLibraryResponse)
def read_knowledge_library(project: str, request: Request) -> KnowledgeLibraryResponse:
    runtime = _runtime(request)
    project_id = _project_id(project)
    server_context = _server_knowledge_context(request, project)
    summary = runtime.library.summary(project_id)
    sources = runtime.library.list_sources(project_id)
    products = runtime.catalog_repository.list_products(project_id)
    return KnowledgeLibraryResponse(
        project_id=summary.project_id,
        source_count=summary.source_count,
        inbox_count=summary.inbox_count,
        pending_count=summary.pending_count,
        published_count=summary.published_count,
        product_count=summary.product_count,
        confirmed_product_count=summary.confirmed_product_count,
        asset_count=summary.asset_count,
        sources=[
            _source_response(
                item,
                include_raw_evidence_url=server_context is None,
            )
            for item in sources
        ],
        products=[_product_response(item) for item in products],
    )


@router.post(
    "/{project}/tasks/{task_id}/retrieval-plan",
    response_model=RetrievalPlanResponse,
    status_code=201,
)
def create_task_retrieval_plan(
    project: str,
    task_id: str,
    request: Request,
) -> RetrievalPlanResponse:
    actor, project_id = _server_knowledge_context(request, project)
    try:
        plan = _server_research(request).create_plan_from_task(
            actor=actor,
            project_id=project_id,
            task_id=task_id,
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail="Task was not found in the requested project.",
        ) from exc
    except ProjectAccessDenied as exc:
        raise HTTPException(
            status_code=403,
            detail="project access denied",
        ) from exc
    except (EvidenceRepositoryError, JobConflict, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ServerKnowledgeResearchUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return _plan_response(plan)


@router.post(
    "/{project}/retrieval-plans",
    response_model=RetrievalPlanResponse,
    status_code=201,
)
def create_retrieval_plan(
    project: str,
    payload: RetrievalPlanCreateRequest,
    request: Request,
) -> RetrievalPlanResponse:
    if _server_knowledge_context(request, project) is not None:
        raise HTTPException(
            status_code=503,
            detail=(
                "Server retrieval plans must be generated from a confirmed "
                "project task outline."
            ),
        )
    runtime = _runtime(request)
    project_id = _project_id(project)
    plan = RetrievalPlan(
        project_id=project_id,
        retrieval_plan_id=payload.retrieval_plan_id,
        article_id=payload.article_id,
        outline_version=payload.outline_version,
        scopes=tuple(
            RetrievalScope(
                project_id=project_id,
                retrieval_plan_id=payload.retrieval_plan_id,
                scope_id=scope.scope_id,
                ordinal=scope.ordinal,
                scope_type=scope.scope_type,
                scope_key=scope.scope_key,
                title=scope.title,
                query_variants=tuple(scope.query_variants),
                filters=scope.filters,
                minimum_hits=scope.minimum_hits,
                minimum_distinct_sources=scope.minimum_distinct_sources,
                require_hard_fact=scope.require_hard_fact,
                metadata=scope.metadata,
            )
            for scope in payload.scopes
        ),
        max_gap_fill_rounds=payload.max_gap_fill_rounds,
        metadata=payload.metadata,
    )
    try:
        runtime.retrieval_plan_repository.save_retrieval_plan(plan)
    except (ValueError, EvidenceRepositoryError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    persisted = runtime.retrieval_plan_repository.get_retrieval_plan(
        project_id,
        plan.retrieval_plan_id,
    )
    if persisted is None:
        raise HTTPException(status_code=500, detail="Retrieval plan was not persisted.")
    return _plan_response(persisted)


@router.get(
    "/{project}/retrieval-plans",
    response_model=list[RetrievalPlanResponse],
)
def list_retrieval_plans(
    project: str,
    request: Request,
    article_id: str | None = None,
    limit: int = 100,
) -> list[RetrievalPlanResponse]:
    runtime = _runtime(request)
    server_context = _server_knowledge_context(request, project)
    try:
        plans = runtime.retrieval_plan_repository.list_retrieval_plans(
            _project_id(project),
            article_id=article_id,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if server_context is not None:
        plans = [
            plan
            for plan in plans
            if is_server_generated_retrieval_plan(plan)
        ]
    return [_plan_response(plan) for plan in plans]


@router.get(
    "/{project}/retrieval-plans/{retrieval_plan_id}",
    response_model=RetrievalPlanResponse,
)
def read_retrieval_plan(
    project: str,
    retrieval_plan_id: str,
    request: Request,
) -> RetrievalPlanResponse:
    runtime = _runtime(request)
    server_context = _server_knowledge_context(request, project)
    plan = runtime.retrieval_plan_repository.get_retrieval_plan(
        _project_id(project),
        retrieval_plan_id,
    )
    if plan is None:
        raise HTTPException(status_code=404, detail="Retrieval plan was not found.")
    if (
        server_context is not None
        and not is_server_generated_retrieval_plan(plan)
    ):
        raise HTTPException(status_code=404, detail="Retrieval plan was not found.")
    return _plan_response(plan)


@router.post(
    "/{project}/research-runs",
    response_model=ResearchRunQueuedResponse,
    status_code=202,
)
def create_research_run(
    project: str,
    payload: ResearchRunCreateRequest,
    request: Request,
) -> ResearchRunQueuedResponse:
    runtime = _runtime(request)
    server_context = _server_knowledge_context(request, project)
    if server_context is not None:
        actor, project_id = server_context
        if payload.organization_id is not None:
            raise HTTPException(
                status_code=422,
                detail="organization_id is derived from the server session.",
            )
        if payload.request_id is None or not payload.request_id.strip():
            raise HTTPException(
                status_code=422,
                detail="request_id is required in server mode.",
            )
        try:
            queued = _server_research(request).enqueue_start(
                actor=actor,
                project_id=project_id,
                retrieval_plan_id=payload.retrieval_plan_id,
                request_id=payload.request_id,
                max_discovery_queries=payload.max_discovery_queries,
            )
        except KeyError as exc:
            raise HTTPException(
                status_code=404,
                detail="Retrieval plan was not found.",
            ) from exc
        except ProjectAccessDenied as exc:
            raise HTTPException(
                status_code=403,
                detail="project access denied",
            ) from exc
        except (
            ActiveJobError,
            EvidenceRepositoryError,
            JobConflict,
            ResearchRunConflictError,
            ValueError,
        ) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ServerKnowledgeResearchUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return ResearchRunQueuedResponse(
            run=_research_run_response(queued["run"]),
            queue_batch_id=str(queued["batch_id"]),
            queue_job_id=str(queued["job_id"]),
        )
    if runtime.research_execution is None:
        raise HTTPException(
            status_code=503,
            detail="Knowledge research execution is not configured.",
        )
    project_id = _project_id(project)
    plan = runtime.retrieval_plan_repository.get_retrieval_plan(
        project_id,
        payload.retrieval_plan_id,
    )
    if plan is None:
        raise HTTPException(status_code=404, detail="Retrieval plan was not found.")
    if payload.organization_id is None or not payload.organization_id.strip():
        raise HTTPException(
            status_code=422,
            detail="organization_id is required for this compatibility request.",
        )
    graph_request = ResearchGraphRequest(
        organization_id=payload.organization_id,
        project_id=project_id,
        article_id=plan.article_id,
        outline_version=plan.outline_version,
        retrieval_plan_id=plan.retrieval_plan_id,
        thread_id=new_research_thread_id(
            project_id,
            plan.article_id,
            plan.outline_version,
        ),
        max_gap_fill_rounds=plan.max_gap_fill_rounds,
        max_discovery_queries=payload.max_discovery_queries,
    )
    try:
        queued = _research_enqueue(request)(
            action="start",
            graph_request=graph_request,
            approved_urls=(),
        )
    except (ValueError, ResearchRunRepositoryError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ResearchRunQueuedResponse(
        run=_research_run_response(queued["run"]),
        queue_batch_id=str(queued["batch_id"]),
        queue_job_id=str(queued["job_id"]),
    )


@router.get(
    "/{project}/research-runs",
    response_model=list[ResearchRunResponse],
)
def list_research_runs(
    project: str,
    request: Request,
    article_id: str | None = None,
    limit: int = 50,
) -> list[ResearchRunResponse]:
    runtime = _runtime(request)
    server_context = _server_knowledge_context(request, project)
    try:
        runs = runtime.research_run_repository.list_runs(
            _project_id(project),
            article_id=article_id,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if server_context is not None:
        actor, _project_id_value = server_context
        runs = tuple(
            run
            for run in runs
            if run.organization_id == actor.organization_id
        )
    return [_research_run_response(run) for run in runs]


@router.get(
    "/{project}/research-runs/{thread_id}",
    response_model=ResearchRunDetailResponse,
)
def read_research_run(
    project: str,
    thread_id: str,
    request: Request,
) -> ResearchRunDetailResponse:
    runtime = _runtime(request)
    project_id = _project_id(project)
    server_context = _server_knowledge_context(request, project)
    run = runtime.research_run_repository.get_run(project_id, thread_id)
    if (
        run is None
        or (
            server_context is not None
            and run.organization_id
            != server_context[0].organization_id
        )
    ):
        raise HTTPException(status_code=404, detail="Research run was not found.")
    review_candidates: list[ResearchReviewCandidateResponse] = []
    if run.status == "waiting_for_review" and runtime.research_execution is not None:
        try:
            state = runtime.research_execution.checkpoint_state(
                project_id=project_id,
                thread_id=thread_id,
            )
        except ResearchRunRepositoryError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        raw_candidates = state.get("discovered_candidates", [])
        if isinstance(raw_candidates, list):
            review_candidates = [
                _review_candidate_response(candidate)
                for candidate in raw_candidates
                if isinstance(candidate, dict) and candidate.get("needs_review")
            ]
    response = _research_run_response(run)
    return ResearchRunDetailResponse(
        **response.model_dump(),
        events=[
            _research_event_response(event, server_mode=server_context is not None)
            for event in runtime.research_run_repository.list_events(
                project_id,
                thread_id,
            )
        ],
        gap_fill_attempts=[
            _gap_attempt_response(
                attempt,
                server_mode=server_context is not None,
            )
            for attempt in runtime.research_run_repository.list_gap_attempts(
                project_id,
                thread_id,
            )
        ],
        review_candidates=review_candidates,
    )


@router.get(
    "/{project}/research-runs/{thread_id}/events/stream",
    response_class=StreamingResponse,
)
async def stream_research_run_events(
    project: str,
    thread_id: str,
    request: Request,
    after_sequence: int | None = None,
) -> StreamingResponse:
    runtime = _runtime(request)
    project_id = _project_id(project)
    server_context = _server_knowledge_context(request, project)
    try:
        cursor = resolve_after_sequence(
            after_sequence,
            request.headers.get("last-event-id"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    run = await asyncio.to_thread(
        runtime.research_run_repository.get_run,
        project_id,
        thread_id,
    )
    if (
        run is None
        or (
            server_context is not None
            and run.organization_id
            != server_context[0].organization_id
        )
    ):
        raise HTTPException(status_code=404, detail="Research run was not found.")

    async def events():
        nonlocal cursor
        first_iteration = True
        last_heartbeat = monotonic()
        while True:
            new_events = await asyncio.to_thread(
                runtime.research_run_repository.list_events,
                project_id,
                thread_id,
                after_sequence=cursor,
            )
            for event in new_events:
                cursor = event.sequence
                yield encode_sse(
                    event="research_event",
                    event_id=event.sequence,
                    data=_research_event_response(
                        event,
                        server_mode=server_context is not None,
                    ).model_dump(),
                )
            current = await asyncio.to_thread(
                runtime.research_run_repository.get_run,
                project_id,
                thread_id,
            )
            if current is None:
                return
            if first_iteration or new_events:
                yield encode_sse(
                    event="run_state",
                    event_id=cursor,
                    data=_research_run_response(current).model_dump(),
                )
                first_iteration = False
            if current.status in TERMINAL_RESEARCH_STATUSES:
                yield encode_sse(
                    event="done",
                    event_id=cursor,
                    data={"status": current.status},
                )
                return
            if await request.is_disconnected():
                return
            now = monotonic()
            if now - last_heartbeat >= 15:
                yield encode_sse(
                    event="heartbeat",
                    data={"server_time": datetime.now(timezone.utc).isoformat()},
                )
                last_heartbeat = now
            await asyncio.sleep(1)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@router.post(
    "/{project}/research-runs/{thread_id}/resume",
    response_model=ResearchRunQueuedResponse,
    status_code=202,
)
def resume_research_run(
    project: str,
    thread_id: str,
    payload: ResearchRunResumeRequest,
    request: Request,
) -> ResearchRunQueuedResponse:
    runtime = _runtime(request)
    server_context = _server_knowledge_context(request, project)
    if server_context is not None:
        actor, project_id = server_context
        if payload.approved_urls:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Server mode accepts approved_candidate_ids, not URLs."
                ),
            )
        if payload.request_id is None or not payload.request_id.strip():
            raise HTTPException(
                status_code=422,
                detail="request_id is required in server mode.",
            )
        try:
            queued = _server_research(request).enqueue_resume(
                actor=actor,
                project_id=project_id,
                thread_id=thread_id,
                request_id=payload.request_id,
                approved_candidate_ids=payload.approved_candidate_ids,
            )
        except (KeyError, ResearchRunNotFound) as exc:
            raise HTTPException(
                status_code=404,
                detail="Research run was not found.",
            ) from exc
        except ProjectAccessDenied as exc:
            raise HTTPException(
                status_code=403,
                detail="project access denied",
            ) from exc
        except (
            ActiveJobError,
            JobConflict,
            ResearchRunConflictError,
            ValueError,
        ) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ServerKnowledgeResearchUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return ResearchRunQueuedResponse(
            run=_research_run_response(queued["run"]),
            queue_batch_id=str(queued["batch_id"]),
            queue_job_id=str(queued["job_id"]),
        )
    if runtime.research_execution is None:
        raise HTTPException(
            status_code=503,
            detail="Knowledge research execution is not configured.",
        )
    project_id = _project_id(project)
    run = runtime.research_run_repository.get_run(project_id, thread_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Research run was not found.")
    graph_request = ResearchGraphRequest(
        organization_id=run.organization_id,
        project_id=run.project_id,
        article_id=run.article_id,
        outline_version=run.outline_version,
        retrieval_plan_id=run.retrieval_plan_id,
        thread_id=run.thread_id,
        max_gap_fill_rounds=run.max_gap_fill_rounds,
        max_discovery_queries=run.max_discovery_queries,
    )
    try:
        queued = _research_enqueue(request)(
            action="resume",
            graph_request=graph_request,
            approved_urls=tuple(payload.approved_urls),
        )
    except ResearchRunNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, ResearchRunConflictError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ResearchRunQueuedResponse(
        run=_research_run_response(queued["run"]),
        queue_batch_id=str(queued["batch_id"]),
        queue_job_id=str(queued["job_id"]),
    )


@router.post(
    "/{project}/research-assistant/messages",
    response_model=ResearchConversationResponse,
    status_code=201,
)
def ask_research_assistant(
    project: str,
    payload: ResearchChatAskRequest,
    request: Request,
) -> ResearchConversationResponse:
    runtime = _runtime(request)
    if runtime.research_chat is None:
        raise HTTPException(
            status_code=503,
            detail="Research assistant generation is not configured.",
        )
    try:
        conversation = runtime.research_chat.ask(
            project_id=_project_id(project),
            question=payload.question,
            request_id=payload.request_id,
            conversation_id=payload.conversation_id,
            article_id=payload.article_id,
            limit=payload.limit,
        )
    except ResearchAnswerProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except (ValueError, ResearchChatError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ResearchChatRepositoryError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _conversation_response(conversation)


@router.get(
    "/{project}/research-conversations/{conversation_id}",
    response_model=ResearchConversationResponse,
)
def read_research_conversation(
    project: str,
    conversation_id: str,
    request: Request,
) -> ResearchConversationResponse:
    runtime = _runtime(request)
    conversation = runtime.research_chat_repository.get_conversation(
        _project_id(project),
        conversation_id,
    )
    if conversation is None:
        raise HTTPException(
            status_code=404,
            detail="Research conversation was not found.",
        )
    return _conversation_response(conversation)


@router.post(
    "/{project}/retrieval-plans/{retrieval_plan_id}/scopes/"
    "{scope_id}/evidence-packs",
    response_model=EvidencePackResponse,
    status_code=201,
)
def build_scope_evidence_pack(
    project: str,
    retrieval_plan_id: str,
    scope_id: str,
    payload: EvidencePackBuildRequest,
    request: Request,
) -> EvidencePackResponse:
    runtime = _runtime(request)
    if runtime.hybrid_retriever is None:
        raise HTTPException(
            status_code=503,
            detail="Embedding Provider is not configured.",
        )
    project_id = _project_id(project)
    try:
        pack = ScopeEvidenceService(
            plans=runtime.retrieval_plan_repository,
            retriever=runtime.hybrid_retriever,
            packs=runtime.evidence_pack_repository,
        ).build(
            project_id=project_id,
            retrieval_plan_id=retrieval_plan_id,
            scope_id=scope_id,
            limit=payload.limit,
        )
    except ScopeEvidenceNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except EmbeddingProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except (
        ValueError,
        HybridRetrievalConfigurationError,
    ) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except EvidenceRepositoryError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return _pack_response(pack)


@router.get(
    "/{project}/evidence-packs/{evidence_pack_id}",
    response_model=EvidencePackResponse,
)
def read_evidence_pack(
    project: str,
    evidence_pack_id: str,
    request: Request,
) -> EvidencePackResponse:
    runtime = _runtime(request)
    pack = runtime.evidence_pack_repository.get_evidence_pack(
        _project_id(project),
        evidence_pack_id,
    )
    if pack is None:
        raise HTTPException(status_code=404, detail="Evidence pack was not found.")
    return _pack_response(pack)


@router.post(
    "/{project}/evidence-links",
    response_model=EvidenceLinkResponse,
    status_code=201,
)
def create_evidence_link(
    project: str,
    payload: EvidenceLinkWriteRequest,
    request: Request,
) -> EvidenceLinkResponse:
    runtime = _runtime(request)
    try:
        link = EvidenceLink(
            project_id=_project_id(project),
            evidence_link_id=payload.evidence_link_id,
            article_id=payload.article_id,
            paragraph_id=payload.paragraph_id,
            sentence_id=payload.sentence_id,
            paragraph_hash=payload.paragraph_hash,
            chunk_id=payload.chunk_id,
            support_scope=payload.support_scope,
            claim_type=payload.claim_type,
            support_type=payload.support_type,
            visible_words=payload.visible_words,
            public_citation_url=payload.public_citation_url,
            validation_status=payload.validation_status,
            metadata=payload.metadata,
        )
        runtime.evidence_link_repository.save_evidence_link(link)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except EvidenceRepositoryError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _link_response(link)


@router.get(
    "/{project}/articles/{article_id}/evidence-links",
    response_model=list[EvidenceLinkResponse],
)
def list_article_evidence_links(
    project: str,
    article_id: str,
    request: Request,
) -> list[EvidenceLinkResponse]:
    runtime = _runtime(request)
    links = runtime.evidence_link_repository.list_evidence_links(
        _project_id(project),
        article_id,
    )
    return [_link_response(link) for link in links]


@router.post(
    "/{project}/articles/{article_id}/knowledge-coverage",
    response_model=KnowledgeCoverageResponse,
)
def calculate_article_knowledge_coverage(
    project: str,
    article_id: str,
    payload: KnowledgeCoverageRequest,
    request: Request,
) -> KnowledgeCoverageResponse:
    runtime = _runtime(request)
    project_id = _project_id(project)
    links = runtime.evidence_link_repository.list_evidence_links(
        project_id,
        article_id,
    )
    try:
        report = calculate_knowledge_coverage(
            project_id=project_id,
            article_id=article_id,
            paragraphs=tuple(
                ParagraphEvidenceTarget(
                    paragraph_id=item.paragraph_id,
                    paragraph_hash=item.paragraph_hash,
                    visible_words=item.visible_words,
                    eligible=item.eligible,
                )
                for item in payload.paragraphs
            ),
            hard_fact_sentences=tuple(
                HardFactSentenceTarget(
                    paragraph_id=item.paragraph_id,
                    sentence_id=item.sentence_id,
                    paragraph_hash=item.paragraph_hash,
                )
                for item in payload.hard_fact_sentences
            ),
            links=links,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return KnowledgeCoverageResponse(
        article_id=article_id,
        eligible_paragraphs=report.eligible_paragraphs,
        supported_paragraphs=report.supported_paragraphs,
        paragraph_coverage=report.paragraph_coverage,
        hard_fact_sentences=report.hard_fact_sentences,
        supported_hard_fact_sentences=report.supported_hard_fact_sentences,
        hard_fact_coverage=report.hard_fact_coverage,
    )


@router.post(
    "/{project}/articles/{article_id}/evidence-links/review-stale",
    response_model=ParagraphHashReviewResponse,
)
def mark_stale_paragraph_evidence(
    project: str,
    article_id: str,
    payload: ParagraphHashReviewRequest,
    request: Request,
) -> ParagraphHashReviewResponse:
    runtime = _runtime(request)
    try:
        changed = runtime.evidence_link_repository.mark_paragraph_links_for_review(
            _project_id(project),
            article_id,
            payload.paragraph_id,
            payload.current_paragraph_hash.lower(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ParagraphHashReviewResponse(
        article_id=article_id,
        paragraph_id=payload.paragraph_id,
        marked_needs_review=changed,
    )


@router.post(
    "/{project}/sources/upload",
    response_model=KnowledgeUploadResponse,
    status_code=201,
)
def upload_private_knowledge(
    project: str,
    request: Request,
    file: UploadFile = File(...),
    source_id: str | None = Form(default=None),
    display_name: str | None = Form(default=None),
    trust_tier: TrustTierValue = Form(default="reference_material"),
    review_mode: ReviewModeValue = Form(default="manual"),
) -> KnowledgeUploadResponse:
    runtime = _runtime(request)
    project_id = _project_id(project)
    server_context = _server_knowledge_context(request, project)
    filename = PurePath(file.filename or "").name
    content = file.file.read(MAX_KNOWLEDGE_UPLOAD_BYTES + 1)
    if len(content) > MAX_KNOWLEDGE_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Knowledge file exceeds 100 MB.")
    if not content:
        raise HTTPException(status_code=422, detail="Knowledge file is empty.")
    resolved_source_id = (source_id or "").strip() or (
        default_private_source_id(filename, content)
        if server_context is not None
        else (
            "upload_"
            + sha256(
                (filename + ":").encode("utf-8") + content
            ).hexdigest()[:20]
        )
    )
    resolved_display_name = (display_name or "").strip() or filename
    if server_context is not None:
        actor, authorized_project_id = server_context
        try:
            upload = _server_private_document_ingestion(
                request
            ).upload(
                actor=actor,
                project_id=authorized_project_id,
                source_id=resolved_source_id,
                display_name=resolved_display_name,
                document_input=DocumentInput(
                    filename=filename,
                    content=content,
                    content_type=file.content_type,
                ),
                trust_tier=trust_tier,
            )
        except ProjectAccessDenied as exc:
            raise HTTPException(
                status_code=403,
                detail="project access denied",
            ) from exc
        except DocumentParserError as exc:
            logger.warning(
                "private knowledge document parsing failed",
                extra={
                    "project_id": authorized_project_id,
                    "upload_filename": filename,
                    "parser_error": str(exc),
                },
            )
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ServerPrivateDocumentUploadConflict as exc:
            raise HTTPException(
                status_code=409,
                detail="Private document conflicts with existing evidence.",
            ) from exc
        except ServerPrivateDocumentUploadUnavailable as exc:
            raise HTTPException(
                status_code=503,
                detail="Private document ingestion is temporarily unavailable.",
            ) from exc
        result = upload.result
        review_decision: str | None = None
        published = False
        status = result.source.status
        message = (
            "File parsed and stored in the Research Inbox."
            if upload.created
            else "The same immutable upload is already in the Research Inbox."
        )
        if review_mode == "automatic":
            review_decision = "approve"
            review_reason = "自动发布：资料解析完成；异常内容可由运营人员手动撤下。"
            commands = _server_knowledge_commands(request, runtime)
            current = runtime.library.get_source(
                authorized_project_id,
                result.source.source_id,
            )
            if (
                current is not None
                and current.status == "published"
                and current.current_snapshot_id == result.snapshot.snapshot_id
                and current.pending_snapshot_id is None
            ):
                review_decision = "approve"
                status = "published"
                published = True
                message = "The same immutable upload is already published."
                return KnowledgeUploadResponse(
                    project_id=authorized_project_id,
                    source_id=result.source.source_id,
                    snapshot_id=result.snapshot.snapshot_id,
                    status=status,
                    parser_name=result.snapshot.parser_name,
                    parser_version=result.snapshot.parser_version,
                    chunk_count=len(result.chunks),
                    asset_count=len(result.assets),
                    created=upload.created,
                    message=message,
                    review_mode=review_mode,
                    review_decision=review_decision,
                    published=published,
                )
            receipt_id = _automatic_review_receipt_id(
                organization_id=actor.organization_id,
                project_id=authorized_project_id,
                source_id=result.source.source_id,
                snapshot_id=result.snapshot.snapshot_id,
            )
            try:
                receipt = commands.review_snapshot(
                    actor=actor,
                    project_id=authorized_project_id,
                    source_id=result.source.source_id,
                    snapshot_id=result.snapshot.snapshot_id,
                    receipt_id=receipt_id,
                    source_kind="private_file",
                    trust_tier=trust_tier,
                    decision=review_decision,
                    reason=review_reason,
                    reviewer_kind="automation",
                    reviewer_id="knowledge_auto_review_v2",
                )
                review_decision = receipt.decision
                current = runtime.library.get_source(
                    authorized_project_id,
                    result.source.source_id,
                )
                status = result.source.status if current is None else current.status
                if review_decision == "approve":
                    try:
                        commands.publish_source(
                            actor=actor,
                            project_id=authorized_project_id,
                            source_id=result.source.source_id,
                            snapshot_id=result.snapshot.snapshot_id,
                        )
                        status = "published"
                        published = True
                        message = (
                            "File parsed and published automatically."
                        )
                    except (
                        EmbeddingProviderError,
                        KnowledgePublicationError,
                        KnowledgeRepositoryError,
                        ServerKnowledgeCommandUnavailable,
                    ):
                        message = (
                            "Automatic publication did not finish; the previous published version remains active."
                        )
            except ProjectAccessDenied as exc:
                raise HTTPException(
                    status_code=403,
                    detail="project access denied",
                ) from exc
            except (
                EmbeddingProviderError,
                KnowledgePublicationError,
                KnowledgeRepositoryError,
                ServerKnowledgeCommandUnavailable,
                SnapshotReviewConflict,
                SnapshotReviewRepositoryError,
                ValueError,
            ) as exc:
                raise HTTPException(
                    status_code=409,
                    detail="Automatic knowledge publication could not be completed.",
                ) from exc
        return KnowledgeUploadResponse(
            project_id=authorized_project_id,
            source_id=result.source.source_id,
            snapshot_id=result.snapshot.snapshot_id,
            status=status,
            parser_name=result.snapshot.parser_name,
            parser_version=result.snapshot.parser_version,
            chunk_count=len(result.chunks),
            asset_count=len(result.assets),
            created=upload.created,
            message=message,
            review_mode=review_mode,
            review_decision=review_decision,
            published=published,
        )
    try:
        runtime.repository.upsert_project(
            KnowledgeProject(
                project_id=project_id,
                customer_name=project,
                official_domain=project_id,
            )
        )
        result = runtime.private_document_ingestion.ingest(
            project_id=project_id,
            source_id=resolved_source_id,
            display_name=resolved_display_name,
            document_input=DocumentInput(
                filename=filename,
                content=content,
                content_type=file.content_type,
            ),
            trust_tier=trust_tier,
        )
    except DocumentParserError as exc:
        logger.warning(
            "knowledge document parsing failed",
            extra={
                "project_id": project_id,
                "upload_filename": filename,
                "parser_error": str(exc),
            },
        )
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (KnowledgeRepositoryError, ArtifactStoreError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    review_decision = None
    published = False
    status = result.source.status
    message = "File parsed and stored in the Research Inbox."
    if review_mode == "automatic":
        review_decision = "approve"
        review_reason = "自动发布：资料解析完成；异常内容可由运营人员手动撤下。"
        source_metadata = dict(result.source.metadata)
        source_metadata["review"] = {
            "decision": review_decision,
            "reason": review_reason,
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
            "actor": "knowledge_auto_review_v2",
        }
        reviewed = KnowledgeSource(
            project_id=result.source.project_id,
            source_id=result.source.source_id,
            display_name=result.source.display_name,
            source_kind="private_file",
            trust_tier=trust_tier,
            status=(
                "inbox"
                if review_decision == "approve"
                else "needs_review"
                if review_decision == "needs_review"
                else "rejected"
            ),
            canonical_url=result.source.canonical_url,
            public_source=result.source.public_source,
            metadata=source_metadata,
        )
        try:
            runtime.repository.upsert_source(reviewed)
            status = reviewed.status
            if review_decision == "approve" and runtime.publication is not None:
                try:
                    runtime.publication.publish(
                        project_id=project_id,
                        source_id=result.source.source_id,
                        snapshot_id=result.snapshot.snapshot_id,
                    )
                    status = "published"
                    published = True
                    message = "File parsed and published automatically."
                except (EmbeddingProviderError, KnowledgePublicationError):
                    message = (
                        "Automatic publication did not finish; the previous published version remains active."
                    )
            elif review_decision == "approve":
                message = (
                    "File parsed; embedding is not configured, so it remains outside retrieval."
                )
        except (KnowledgeRepositoryError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    return KnowledgeUploadResponse(
        project_id=project_id,
        source_id=result.source.source_id,
        snapshot_id=result.snapshot.snapshot_id,
        status=status,
        parser_name=result.snapshot.parser_name,
        parser_version=result.snapshot.parser_version,
        chunk_count=len(result.chunks),
        asset_count=len(result.assets),
        created=True,
        message=message,
        review_mode=review_mode,
        review_decision=review_decision,
        published=published,
    )


@router.delete(
    "/{project}/sources/{source_id}",
    response_model=KnowledgeSourceReviewResponse,
)
def withdraw_knowledge_source(
    project: str,
    source_id: str,
    request: Request,
) -> KnowledgeSourceReviewResponse:
    runtime = _runtime(request)
    server_context = _server_knowledge_context(request, project)
    if server_context is None:
        raise HTTPException(
            status_code=409,
            detail="Source withdrawal is available only in Server mode.",
        )
    actor, project_id = server_context
    try:
        status = _server_knowledge_commands(request, runtime).withdraw_source(
            actor=actor,
            project_id=project_id,
            source_id=source_id,
        )
    except ProjectAccessDenied as exc:
        raise HTTPException(status_code=403, detail="project access denied") from exc
    except KnowledgeRecordNotFound as exc:
        raise HTTPException(status_code=404, detail="Knowledge source was not found.") from exc
    except ServerKnowledgeCommandUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return KnowledgeSourceReviewResponse(
        project_id=project_id,
        source_id=source_id,
        status=status,
        decision="reject",
    )


@router.put(
    "/{project}/sources/{source_id}/review",
    response_model=KnowledgeSourceReviewResponse,
)
def review_knowledge_source(
    project: str,
    source_id: str,
    payload: KnowledgeSourceReviewRequest,
    request: Request,
) -> KnowledgeSourceReviewResponse:
    runtime = _runtime(request)
    server_context = _server_knowledge_context(request, project)
    project_id = (
        _project_id(project)
        if server_context is None
        else server_context[1]
    )
    if server_context is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                "Server review requires an exact Snapshot review route."
            ),
        )

    source = runtime.library.get_source(project_id, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Knowledge source was not found.")
    if source.status == "published":
        raise HTTPException(
            status_code=409,
            detail="Published source review requires a new snapshot.",
        )
    status_by_decision = {
        "approve": "inbox",
        "needs_review": "needs_review",
        "reject": "rejected",
    }
    metadata = dict(source.metadata)
    metadata["review"] = {
        "decision": payload.decision,
        "reason": payload.reason.strip(),
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
    }
    reviewed = KnowledgeSource(
        project_id=source.project_id,
        source_id=source.source_id,
        display_name=source.display_name,
        source_kind=payload.source_kind,
        trust_tier=payload.trust_tier,
        status=status_by_decision[payload.decision],  # type: ignore[arg-type]
        canonical_url=source.canonical_url,
        public_source=source.public_source,
        metadata=metadata,
    )
    try:
        runtime.repository.upsert_source(reviewed)
    except (ValueError, KnowledgeRepositoryError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return KnowledgeSourceReviewResponse(
        project_id=project_id,
        source_id=source_id,
        status=reviewed.status,
        decision=payload.decision,
    )


@router.get(
    "/{project}/sources/{source_id}/snapshots/{snapshot_id}/evidence",
    response_model=SnapshotEvidenceManifestResponse,
)
def get_snapshot_evidence_manifest(
    project: str,
    source_id: str,
    snapshot_id: str,
    request: Request,
    response: Response,
) -> SnapshotEvidenceManifestResponse:
    _runtime(request)
    server_context = _server_knowledge_context(request, project)
    if server_context is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "Snapshot evidence manifest is available only in Server mode."
            ),
        )
    actor, project_id = server_context
    try:
        manifest = _server_snapshot_evidence(request).get_manifest(
            actor=actor,
            project_id=project_id,
            source_id=source_id,
            snapshot_id=snapshot_id,
        )
    except ProjectAccessDenied as exc:
        raise HTTPException(
            status_code=403,
            detail="project access denied",
        ) from exc
    except SnapshotEvidenceNotFound as exc:
        raise HTTPException(
            status_code=404,
            detail="Snapshot evidence was not found.",
        ) from exc
    except SnapshotEvidenceUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    response.headers["Cache-Control"] = "no-store"
    return SnapshotEvidenceManifestResponse(
        project_id=manifest.project_id,
        source_id=manifest.source_id,
        snapshot_id=manifest.snapshot_id,
        slot=manifest.slot,
        raw_available=manifest.raw_available,
        normalized_available=manifest.normalized_available,
        raw_content_type=manifest.raw_content_type,
        raw_byte_size=manifest.raw_byte_size,
        normalized_content_type=manifest.normalized_content_type,
        normalized_byte_size=manifest.normalized_byte_size,
        preview_supported=manifest.preview_supported,
    )


@router.get(
    "/{project}/sources/{source_id}/snapshots/{snapshot_id}/evidence/preview",
    response_model=SnapshotEvidencePreviewResponse,
)
def preview_snapshot_evidence(
    project: str,
    source_id: str,
    snapshot_id: str,
    request: Request,
    response: Response,
) -> SnapshotEvidencePreviewResponse:
    _runtime(request)
    server_context = _server_knowledge_context(request, project)
    if server_context is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "Snapshot evidence preview is available only in Server mode."
            ),
        )
    actor, project_id = server_context
    try:
        preview = _server_snapshot_evidence(request).get_preview(
            actor=actor,
            project_id=project_id,
            source_id=source_id,
            snapshot_id=snapshot_id,
        )
    except ProjectAccessDenied as exc:
        raise HTTPException(
            status_code=403,
            detail="project access denied",
        ) from exc
    except SnapshotEvidenceNotFound as exc:
        raise HTTPException(
            status_code=404,
            detail="Snapshot evidence was not found.",
        ) from exc
    except SnapshotEvidenceUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    response.headers["Cache-Control"] = "no-store"
    return SnapshotEvidencePreviewResponse(
        project_id=preview.project_id,
        source_id=preview.source_id,
        snapshot_id=preview.snapshot_id,
        slot=preview.slot,
        text=preview.text,
        truncated=preview.truncated,
        block_count=preview.block_count,
    )


@router.post(
    "/{project}/sources/{source_id}/snapshots/{snapshot_id}/evidence/raw-download",
    response_model=SnapshotEvidenceDownloadResponse,
)
def create_snapshot_raw_download(
    project: str,
    source_id: str,
    snapshot_id: str,
    request: Request,
    response: Response,
) -> SnapshotEvidenceDownloadResponse:
    _runtime(request)
    server_context = _server_knowledge_context(request, project)
    if server_context is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "Snapshot evidence download is available only in Server mode."
            ),
        )
    actor, project_id = server_context
    try:
        download = _server_snapshot_evidence(request).create_raw_download(
            actor=actor,
            project_id=project_id,
            source_id=source_id,
            snapshot_id=snapshot_id,
        )
    except ProjectAccessDenied as exc:
        raise HTTPException(
            status_code=403,
            detail="project access denied",
        ) from exc
    except SnapshotEvidenceNotFound as exc:
        raise HTTPException(
            status_code=404,
            detail="Snapshot evidence was not found.",
        ) from exc
    except SnapshotEvidenceUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    response.headers["Cache-Control"] = "no-store"
    return SnapshotEvidenceDownloadResponse(
        project_id=download.project_id,
        source_id=download.source_id,
        snapshot_id=download.snapshot_id,
        slot=download.slot,
        download_url=download.download_url,
        expires_seconds=download.expires_seconds,
    )


@router.put(
    "/{project}/sources/{source_id}/snapshots/{snapshot_id}/review",
    response_model=KnowledgeSnapshotReviewResponse,
)
def review_knowledge_snapshot(
    project: str,
    source_id: str,
    snapshot_id: str,
    payload: KnowledgeSnapshotReviewRequest,
    request: Request,
) -> KnowledgeSnapshotReviewResponse:
    runtime = _runtime(request)
    server_context = _server_knowledge_context(request, project)
    if server_context is None:
        raise HTTPException(
            status_code=409,
            detail="Snapshot review is available only in Server mode.",
        )
    actor, project_id = server_context
    try:
        receipt = _server_knowledge_commands(
            request,
            runtime,
        ).review_snapshot(
            actor=actor,
            project_id=project_id,
            source_id=source_id,
            snapshot_id=snapshot_id,
            receipt_id=payload.receipt_id,
            source_kind=payload.source_kind,
            trust_tier=payload.trust_tier,
            decision=payload.decision,
            reason=payload.reason,
        )
        source = runtime.library.get_source(project_id, source_id)
        if source is None:
            raise KnowledgeRecordNotFound(
                "knowledge source was not found in the requested project"
            )
    except ProjectAccessDenied as exc:
        raise HTTPException(
            status_code=403,
            detail="project access denied",
        ) from exc
    except KnowledgeRecordNotFound as exc:
        raise HTTPException(
            status_code=404,
            detail="Knowledge source or Snapshot was not found.",
        ) from exc
    except (
        KnowledgePublicationError,
        KnowledgeRepositoryError,
        SnapshotReviewConflict,
        SnapshotReviewRepositoryError,
        ValueError,
    ) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ServerKnowledgeCommandUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return KnowledgeSnapshotReviewResponse(
        project_id=project_id,
        source_id=source_id,
        snapshot_id=snapshot_id,
        status=source.status,
        decision=receipt.decision,
        receipt_id=receipt.receipt_id,
        review_version=receipt.review_version,
    )


@router.post(
    "/{project}/sources/{source_id}/publish",
    response_model=KnowledgePublicationResponse,
)
def publish_knowledge_source(
    project: str,
    source_id: str,
    request: Request,
) -> KnowledgePublicationResponse:
    runtime = _runtime(request)
    if runtime.publication is None:
        raise HTTPException(
            status_code=503,
            detail="Embedding Provider is not configured.",
        )
    server_context = _server_knowledge_context(request, project)
    project_id = (
        _project_id(project)
        if server_context is None
        else server_context[1]
    )
    if server_context is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                "Server publication requires an exact Snapshot publish route."
            ),
        )
    try:
        result = runtime.publication.publish(
            project_id=project_id,
            source_id=source_id,
        )
    except ProjectAccessDenied as exc:
        raise HTTPException(
            status_code=403,
            detail="project access denied",
        ) from exc
    except EmbeddingProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except (KnowledgePublicationError, KnowledgeRepositoryError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ServerKnowledgeCommandUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return KnowledgePublicationResponse(
        project_id=result.project_id,
        source_id=result.source_id,
        snapshot_id=result.snapshot_id,
        status="published",
        embedding_model=result.embedding_model,
        chunk_count=result.chunk_count,
    )


@router.post(
    "/{project}/sources/{source_id}/snapshots/{snapshot_id}/publish",
    response_model=KnowledgePublicationResponse,
)
def publish_knowledge_snapshot(
    project: str,
    source_id: str,
    snapshot_id: str,
    request: Request,
) -> KnowledgePublicationResponse:
    runtime = _runtime(request)
    if runtime.publication is None:
        raise HTTPException(
            status_code=503,
            detail="Embedding Provider is not configured.",
        )
    server_context = _server_knowledge_context(request, project)
    if server_context is None:
        raise HTTPException(
            status_code=409,
            detail="Snapshot publication is available only in Server mode.",
        )
    actor, project_id = server_context
    try:
        result = _server_knowledge_commands(
            request,
            runtime,
        ).publish_source(
            actor=actor,
            project_id=project_id,
            source_id=source_id,
            snapshot_id=snapshot_id,
        )
    except ProjectAccessDenied as exc:
        raise HTTPException(
            status_code=403,
            detail="project access denied",
        ) from exc
    except EmbeddingProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except (
        KnowledgePublicationError,
        KnowledgeRepositoryError,
        SnapshotReviewRepositoryError,
    ) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ServerKnowledgeCommandUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return KnowledgePublicationResponse(
        project_id=result.project_id,
        source_id=result.source_id,
        snapshot_id=result.snapshot_id,
        status="published",
        embedding_model=result.embedding_model,
        chunk_count=result.chunk_count,
    )


@router.post(
    "/{project}/products/{product_id}/confirm",
    response_model=ProductConfirmResponse,
)
def confirm_knowledge_product(
    project: str,
    product_id: str,
    request: Request,
) -> ProductConfirmResponse:
    runtime = _runtime(request)
    server_context = _server_knowledge_context(request, project)
    project_id = (
        _project_id(project)
        if server_context is None
        else server_context[1]
    )
    try:
        if server_context is None:
            runtime.catalog_repository.confirm_product(project_id, product_id)
        else:
            _server_knowledge_commands(
                request,
                runtime,
            ).confirm_product(
                actor=server_context[0],
                project_id=project_id,
                product_id=product_id,
            )
    except ProjectAccessDenied as exc:
        raise HTTPException(
            status_code=403,
            detail="project access denied",
        ) from exc
    except ProductCatalogRepositoryError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ServerKnowledgeCommandUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return ProductConfirmResponse(
        project_id=project_id,
        product_id=product_id,
        status="confirmed",
    )


@router.put(
    "/{project}/products/{product_id}/specifications",
    response_model=KnowledgeProductResponse,
)
def update_knowledge_product_specifications(
    project: str,
    product_id: str,
    payload: ProductSpecificationsUpdateRequest,
    request: Request,
) -> KnowledgeProductResponse:
    runtime = _runtime(request)
    server_context = _server_knowledge_context(request, project)
    project_id = (
        _project_id(project)
        if server_context is None
        else server_context[1]
    )
    specification_tables = [
        table.model_dump() for table in payload.specification_tables
    ]
    try:
        if server_context is None:
            product = runtime.catalog_repository.update_product_specifications(
                project_id,
                product_id,
                specification_tables,
            )
        else:
            product = _server_knowledge_commands(
                request,
                runtime,
            ).update_product_specifications(
                actor=server_context[0],
                project_id=project_id,
                product_id=product_id,
                specification_tables=specification_tables,
            )
    except ProjectAccessDenied as exc:
        raise HTTPException(
            status_code=403,
            detail="project access denied",
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ProductCatalogRepositoryError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ServerKnowledgeCommandUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return _product_response(product)


@router.get(
    "/{project}/sources/{source_id}/snapshots/{snapshot_id}/raw",
    response_class=FileResponse,
)
def open_raw_knowledge_evidence(
    project: str,
    source_id: str,
    snapshot_id: str,
    request: Request,
) -> FileResponse:
    runtime = _runtime(request)
    project_id = _project_id(project)
    snapshot = runtime.library.get_snapshot(project_id, source_id, snapshot_id)
    if snapshot is None or snapshot.raw_artifact_uri is None:
        raise HTTPException(status_code=404, detail="Raw evidence was not found.")
    try:
        path = runtime.artifact_store.resolve_local_uri(snapshot.raw_artifact_uri)
    except ArtifactStoreError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(path, filename=path.name)
