from __future__ import annotations

from hashlib import sha256
from pathlib import PurePath
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import quote

from fastapi import (
    APIRouter,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from .artifact_store import ArtifactStoreError
from .catalog import (
    KnowledgeProduct,
    ProductCatalogRepositoryError,
)
from .contracts import KnowledgeProject
from .contracts import KnowledgeSource
from .ingestion import DocumentInput, DocumentParserError
from .embedding import EmbeddingProviderError
from .library import KnowledgeSourceSummary
from .repository import KnowledgeRepositoryError
from .publication import KnowledgePublicationError
from .runtime import KnowledgeAgentRuntime


MAX_KNOWLEDGE_UPLOAD_BYTES = 25 * 1024 * 1024
TrustTierValue = Literal[
    "hard_fact",
    "reference_material",
    "writing_instruction",
]


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
    snapshot_count: int
    chunk_count: int
    asset_count: int
    latest_fetched_at: str | None
    classification_reason: str
    raw_evidence_url: str | None


class KnowledgeProductResponse(KnowledgeApiModel):
    project_id: str
    product_id: str
    name: str
    status: str
    canonical_url: str | None
    category_path: list[str]


class KnowledgeLibraryResponse(KnowledgeApiModel):
    project_id: str
    source_count: int
    inbox_count: int
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
    message: str


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


class KnowledgePublicationResponse(KnowledgeApiModel):
    project_id: str
    source_id: str
    snapshot_id: str
    status: str
    embedding_model: str
    chunk_count: int


def _runtime(request: Request) -> KnowledgeAgentRuntime:
    runtime = getattr(request.app.state, "knowledge_agent_runtime", None)
    if runtime is None:
        raise HTTPException(status_code=404, detail="Knowledge Agent is disabled.")
    return runtime


def _project_id(value: str) -> str:
    normalized = value.strip().lower().rstrip(".")
    if normalized.startswith("www."):
        normalized = normalized[4:]
    return normalized


def _source_response(
    item: KnowledgeSourceSummary,
) -> KnowledgeSourceResponse:
    snapshot_id = item.current_snapshot_id or item.latest_snapshot_id
    raw_url = (
        None
        if snapshot_id is None
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
        snapshot_count=item.snapshot_count,
        chunk_count=item.chunk_count,
        asset_count=item.asset_count,
        latest_fetched_at=(
            None
            if item.latest_fetched_at is None
            else item.latest_fetched_at.isoformat()
        ),
        classification_reason=item.classification_reason,
        raw_evidence_url=raw_url,
    )


def _product_response(item: KnowledgeProduct) -> KnowledgeProductResponse:
    return KnowledgeProductResponse(
        project_id=item.project_id,
        product_id=item.product_id,
        name=item.name,
        status=item.status,
        canonical_url=item.canonical_url,
        category_path=list(item.category_path),
    )


router = APIRouter(prefix="/api/knowledge", tags=["knowledge-agent"])


@router.get("/{project}", response_model=KnowledgeLibraryResponse)
def read_knowledge_library(project: str, request: Request) -> KnowledgeLibraryResponse:
    runtime = _runtime(request)
    project_id = _project_id(project)
    summary = runtime.library.summary(project_id)
    sources = runtime.library.list_sources(project_id)
    products = runtime.catalog_repository.list_products(project_id)
    return KnowledgeLibraryResponse(
        project_id=summary.project_id,
        source_count=summary.source_count,
        inbox_count=summary.inbox_count,
        published_count=summary.published_count,
        product_count=summary.product_count,
        confirmed_product_count=summary.confirmed_product_count,
        asset_count=summary.asset_count,
        sources=[_source_response(item) for item in sources],
        products=[_product_response(item) for item in products],
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
) -> KnowledgeUploadResponse:
    runtime = _runtime(request)
    project_id = _project_id(project)
    filename = PurePath(file.filename or "").name
    content = file.file.read(MAX_KNOWLEDGE_UPLOAD_BYTES + 1)
    if len(content) > MAX_KNOWLEDGE_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Knowledge file exceeds 25 MB.")
    if not content:
        raise HTTPException(status_code=422, detail="Knowledge file is empty.")
    resolved_source_id = (source_id or "").strip() or (
        f"upload_{sha256((filename + ':').encode('utf-8') + content).hexdigest()[:20]}"
    )
    resolved_display_name = (display_name or "").strip() or filename
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
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (KnowledgeRepositoryError, ArtifactStoreError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return KnowledgeUploadResponse(
        project_id=project_id,
        source_id=result.source.source_id,
        snapshot_id=result.snapshot.snapshot_id,
        status=result.source.status,
        parser_name=result.snapshot.parser_name,
        parser_version=result.snapshot.parser_version,
        chunk_count=len(result.chunks),
        asset_count=len(result.assets),
        message="File parsed and stored in the Research Inbox.",
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
    project_id = _project_id(project)
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
    project_id = _project_id(project)
    try:
        result = runtime.publication.publish(
            project_id=project_id,
            source_id=source_id,
        )
    except EmbeddingProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except (KnowledgePublicationError, KnowledgeRepositoryError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
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
    project_id = _project_id(project)
    try:
        runtime.catalog_repository.confirm_product(project_id, product_id)
    except ProductCatalogRepositoryError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ProductConfirmResponse(
        project_id=project_id,
        product_id=product_id,
        status="confirmed",
    )


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
