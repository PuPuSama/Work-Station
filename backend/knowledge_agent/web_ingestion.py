from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
from pathlib import PurePosixPath
from typing import Mapping, Protocol
from urllib.parse import urlsplit

from PIL import Image, UnidentifiedImageError

from .artifact_store import ArtifactStore
from .assets import (
    KnowledgeAsset,
    KnowledgeAssetRepository,
    SnapshotAsset,
)
from .catalog import (
    KnowledgeProduct,
    ProductAssetEvidence,
    ProductCatalogRepository,
    ProductSourceEvidence,
)
from .contracts import (
    KnowledgeChunk,
    KnowledgeSource,
    SourceKind,
    SourceSnapshot,
    TrustTier,
)
from .ingestion import ParsedBlock, ParsedDocument, ParsedDocumentChunker
from .interfaces import KnowledgeRepository
from .wordpress import (
    ClassifiedWebPage,
    FetchedResource,
    OfficialSiteFetchError,
    OfficialSiteFetcher,
    UnsafeOfficialSiteUrl,
    WEB_PAGE_PARSER_VERSION,
    WordPressIngestionError,
    WordPressProbeResult,
    WordPressSiteProbe,
    classify_web_page,
    discover_product_links,
    normalize_official_url,
    normalize_site_url,
)


MAX_PRODUCT_IMAGES_PER_PAGE = 12
MAX_PRODUCT_IMAGE_BYTES = 12 * 1024 * 1024


class WebSnapshotLookup(Protocol):
    def find_snapshot_by_content(
        self,
        *,
        project_id: str,
        source_id: str,
        content_hash: str,
        parser_name: str,
        parser_version: str,
    ) -> SourceSnapshot | None: ...


class WebPageIngestion(Protocol):
    """Shared Local/Server page-ingestion surface.

    Local mode persists through the M2 repositories. Server mode supplies a
    wrapper with the same methods but commits the prepared evidence in one
    authorized PostgreSQL transaction.
    """

    def ingest_url(
        self,
        *,
        project_id: str,
        site_url: str,
        url: str,
        metadata: Mapping[str, object] | None = None,
    ) -> WebPageIngestionResult: ...

    def ingest_resource(
        self,
        *,
        project_id: str,
        site_url: str,
        resource: FetchedResource,
        classification: ClassifiedWebPage,
        metadata: Mapping[str, object] | None = None,
    ) -> WebPageIngestionResult: ...


class WebPagePreparation(Protocol):
    """Prepare immutable page evidence without relational database writes."""

    def prepare_url(
        self,
        *,
        project_id: str,
        site_url: str,
        url: str,
        metadata: Mapping[str, object] | None = None,
    ) -> PreparedWebPageIngestion: ...

    def prepare_resource(
        self,
        *,
        project_id: str,
        site_url: str,
        resource: FetchedResource,
        classification: ClassifiedWebPage,
        metadata: Mapping[str, object] | None = None,
    ) -> PreparedWebPageIngestion: ...


@dataclass(frozen=True, slots=True)
class WebPageIngestionResult:
    """One reviewable webpage snapshot and any product evidence derived from it."""

    source: KnowledgeSource
    snapshot: SourceSnapshot
    chunks: tuple[KnowledgeChunk, ...]
    classification: ClassifiedWebPage
    product: KnowledgeProduct | None
    assets: tuple[KnowledgeAsset, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PreparedWebPageIngestion:
    """Frozen official-page evidence with no database side effects.

    ArtifactStore writes are content addressed and may already have occurred.
    Repository writes are deliberately deferred so a Server adapter can lock
    access and commit every relational record with its Audit event.
    """

    source: KnowledgeSource
    snapshot: SourceSnapshot
    normalized_content_hash: str
    chunks: tuple[KnowledgeChunk, ...]
    classification: ClassifiedWebPage
    product: KnowledgeProduct | None
    source_evidence: ProductSourceEvidence | None
    assets: tuple[KnowledgeAsset, ...]
    snapshot_assets: tuple[SnapshotAsset, ...]
    asset_evidence: tuple[ProductAssetEvidence, ...]
    warnings: tuple[str, ...]

    def __post_init__(self) -> None:
        project_id = self.source.project_id
        if not re.fullmatch(r"[0-9a-f]{64}", self.normalized_content_hash):
            raise ValueError(
                "normalized_content_hash must be a lowercase SHA-256 digest"
            )
        snapshot_identity = (
            project_id,
            self.source.source_id,
            self.snapshot.snapshot_id,
        )
        if (
            self.snapshot.project_id != project_id
            or self.snapshot.source_id != self.source.source_id
            or not self.chunks
            or any(
                (
                    chunk.project_id,
                    chunk.source_id,
                    chunk.snapshot_id,
                )
                != snapshot_identity
                for chunk in self.chunks
            )
        ):
            raise ValueError(
                "prepared chunks must belong to the supplied source snapshot"
            )
        if len({chunk.ordinal for chunk in self.chunks}) != len(self.chunks):
            raise ValueError("prepared chunk ordinals must be unique")
        if (self.product is None) != (self.source_evidence is None):
            raise ValueError(
                "prepared product and source evidence must be provided together"
            )
        if self.product is not None and (
            self.product.project_id != project_id
            or self.source_evidence is None
            or self.source_evidence.project_id != project_id
            or self.source_evidence.product_id != self.product.product_id
            or self.source_evidence.source_id != self.source.source_id
            or self.source_evidence.snapshot_id != self.snapshot.snapshot_id
        ):
            raise ValueError("prepared product evidence is out of scope")
        asset_ids = {asset.asset_id for asset in self.assets}
        if len(asset_ids) != len(self.assets):
            raise ValueError("prepared asset ids must be unique")
        if any(
            link.project_id != project_id
            or link.source_id != self.source.source_id
            or link.snapshot_id != self.snapshot.snapshot_id
            or link.asset_id not in asset_ids
            for link in self.snapshot_assets
        ):
            raise ValueError("prepared snapshot assets are out of scope")
        if len({link.ordinal for link in self.snapshot_assets}) != len(
            self.snapshot_assets
        ):
            raise ValueError("prepared snapshot asset ordinals must be unique")
        linked_asset_ids = {link.asset_id for link in self.snapshot_assets}
        if (
            len(linked_asset_ids) != len(self.snapshot_assets)
            or linked_asset_ids != asset_ids
        ):
            raise ValueError(
                "every prepared asset must have one snapshot link"
            )
        if any(
            self.product is None
            or evidence.project_id != project_id
            or evidence.product_id != self.product.product_id
            or evidence.source_id != self.source.source_id
            or evidence.snapshot_id != self.snapshot.snapshot_id
            or evidence.asset_id not in linked_asset_ids
            for evidence in self.asset_evidence
        ):
            raise ValueError("prepared product asset evidence is out of scope")


@dataclass(frozen=True, slots=True)
class WordPressCategorySyncResult:
    """Bounded, synchronous M2 sync result. It never auto-publishes sources."""

    probe: WordPressProbeResult
    category: WebPageIngestionResult
    products: tuple[WebPageIngestionResult, ...]
    skipped_urls: tuple[str, ...]
    warnings: tuple[str, ...]


def _source_id(canonical_url: str) -> str:
    digest = sha256(canonical_url.encode("utf-8")).hexdigest()
    return f"web_{digest[:24]}"


def _snapshot_id(source_id: str, content_hash: str) -> str:
    identity = (
        f"{source_id}\x1f{content_hash}\x1fofficial-web-page"
        f"\x1f{WEB_PAGE_PARSER_VERSION}"
    ).encode("utf-8")
    return f"snapshot_{sha256(identity).hexdigest()[:32]}"


def _product_id(canonical_url: str) -> str:
    path = PurePosixPath(urlsplit(canonical_url).path.rstrip("/"))
    slug = path.name.casefold()
    slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-") or "product"
    digest = sha256(canonical_url.encode("utf-8")).hexdigest()[:8]
    return f"product_{slug[:48]}_{digest}"


def _document_for_page(
    resource: FetchedResource,
    page: ClassifiedWebPage,
) -> ParsedDocument:
    blocks: list[ParsedBlock] = []
    if page.heading:
        blocks.append(
            ParsedBlock(
                kind="heading",
                ordinal=0,
                text=page.heading,
                heading_path=(page.heading,),
                locator={"selector": "h1", "page_url": page.canonical_url},
            )
        )
    for text in page.text_blocks:
        if page.heading and text.casefold() == page.heading.casefold():
            continue
        blocks.append(
            ParsedBlock(
                kind="paragraph",
                ordinal=len(blocks),
                text=text,
                heading_path=((page.heading,) if page.heading else ()),
                locator={
                    "text_block_index": len(blocks),
                    "page_url": page.canonical_url,
                },
            )
        )
    if not blocks:
        raise WordPressIngestionError(
            "classified official page contains no reviewable text"
        )
    content_hash = sha256(resource.content).hexdigest()
    return ParsedDocument(
        filename="page.html",
        content_type=resource.content_type,
        content_hash=content_hash,
        parser_name="official-web-page",
        parser_version=WEB_PAGE_PARSER_VERSION,
        blocks=tuple(blocks),
        title=page.heading or page.title or page.canonical_url,
        metadata={
            "canonical_url": page.canonical_url,
            "page_type": page.page_type,
            "classification_confidence": page.confidence,
            "classification_reasons": list(page.reasons),
            "breadcrumbs": list(page.breadcrumbs),
            **dict(page.metadata),
        },
    )


def _normalized_page_bytes(
    page: ClassifiedWebPage,
    document: ParsedDocument,
) -> bytes:
    product = page.product_page
    image_candidates: list[dict[str, object]] = []
    if product is not None:
        seen: set[str] = set()
        for group_name, values in product.image_sources.items():
            for item in values:
                if item.source_url in seen:
                    continue
                seen.add(item.source_url)
                image_candidates.append(
                    {
                        "source_url": item.source_url,
                        "source_kind": item.source_kind,
                        "source_kinds": list(item.source_kinds),
                        "alt": item.alt,
                        "title": item.title,
                        "caption": item.caption,
                        "width": item.width,
                        "height": item.height,
                        "group": group_name,
                    }
                )
    payload = {
        "schema_version": 1,
        "parser": {
            "name": document.parser_name,
            "version": document.parser_version,
        },
        "source": {
            "requested_url": page.requested_url,
            "canonical_url": page.canonical_url,
            "content_hash": document.content_hash,
        },
        "classification": {
            "page_type": page.page_type,
            "confidence": page.confidence,
            "reasons": list(page.reasons),
        },
        "page": {
            "title": page.title,
            "heading": page.heading,
            "breadcrumbs": list(page.breadcrumbs),
            "blocks": [
                {
                    "kind": block.kind,
                    "ordinal": block.ordinal,
                    "text": block.text,
                    "heading_path": list(block.heading_path),
                    "locator": dict(block.locator),
                }
                for block in document.blocks
            ],
        },
        "product": (
            None
            if product is None
            else {
                "name": product.h1,
                "meta_description": product.meta_description,
                "main_content_facts": list(product.main_content_facts),
                "specification_tables": list(product.specification_tables),
                "faq": list(product.faq),
                "image_candidates": image_candidates,
            }
        ),
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _page_policy(page_type: str) -> tuple[SourceKind, TrustTier]:
    if page_type == "product_detail":
        return "product_detail", "hard_fact"
    if page_type == "product_category":
        return "product_category", "hard_fact"
    if page_type == "official_blog":
        return "official_blog", "reference_material"
    if page_type == "knowledge_page":
        return "knowledge_page", "reference_material"
    raise WordPressIngestionError("unknown pages must be reviewed before ingestion")


def _safe_image_name(url: str, content_type: str, ordinal: int) -> str:
    suffix = PurePosixPath(urlsplit(url).path).suffix.casefold()
    allowed = {".avif", ".gif", ".jpeg", ".jpg", ".png", ".webp"}
    if suffix not in allowed:
        suffix_by_type = {
            "image/avif": ".avif",
            "image/gif": ".gif",
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
        }
        suffix = suffix_by_type.get(content_type, ".bin")
    return f"product-image-{ordinal:02d}{suffix}"


def _image_dimensions(content: bytes) -> tuple[int, int]:
    try:
        with Image.open(BytesIO(content)) as image:
            image.verify()
        with Image.open(BytesIO(content)) as image:
            width, height = image.size
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise WordPressIngestionError(
            "product image bytes are not a supported raster image"
        ) from exc
    if width <= 0 or height <= 0:
        raise WordPressIngestionError("product image dimensions are invalid")
    return width, height


class OfficialWebPageIngestionService:
    """Persist official HTML as immutable Inbox evidence, never as live mutable state."""

    def __init__(
        self,
        *,
        repository: KnowledgeRepository,
        asset_repository: KnowledgeAssetRepository,
        catalog_repository: ProductCatalogRepository,
        artifact_store: ArtifactStore,
        fetcher: OfficialSiteFetcher,
        snapshot_lookup: WebSnapshotLookup | None = None,
        chunker: ParsedDocumentChunker | None = None,
        max_product_images: int = MAX_PRODUCT_IMAGES_PER_PAGE,
    ) -> None:
        if max_product_images < 0:
            raise ValueError("max_product_images must be non-negative")
        self._repository = repository
        self._asset_repository = asset_repository
        self._catalog_repository = catalog_repository
        self._artifact_store = artifact_store
        self._fetcher = fetcher
        self._snapshot_lookup = snapshot_lookup
        self._chunker = chunker or ParsedDocumentChunker()
        self._max_product_images = max_product_images

    def ingest_url(
        self,
        *,
        project_id: str,
        site_url: str,
        url: str,
        metadata: Mapping[str, object] | None = None,
    ) -> WebPageIngestionResult:
        return self._persist_prepared(
            self.prepare_url(
                project_id=project_id,
                site_url=site_url,
                url=url,
                metadata=metadata,
            )
        )

    def prepare_url(
        self,
        *,
        project_id: str,
        site_url: str,
        url: str,
        metadata: Mapping[str, object] | None = None,
    ) -> PreparedWebPageIngestion:
        """Fetch, parse and store immutable objects without database writes."""

        site = normalize_site_url(site_url)
        target = normalize_official_url(site, url)
        resource = self._fetcher.fetch(site_url=site, url=target)
        page = classify_web_page(
            requested_url=resource.final_url,
            html=resource.content,
        )
        return self.prepare_resource(
            project_id=project_id,
            site_url=site,
            resource=resource,
            classification=page,
            metadata=metadata,
        )

    def ingest_resource(
        self,
        *,
        project_id: str,
        site_url: str,
        resource: FetchedResource,
        classification: ClassifiedWebPage,
        metadata: Mapping[str, object] | None = None,
    ) -> WebPageIngestionResult:
        return self._persist_prepared(
            self.prepare_resource(
                project_id=project_id,
                site_url=site_url,
                resource=resource,
                classification=classification,
                metadata=metadata,
            )
        )

    def prepare_resource(
        self,
        *,
        project_id: str,
        site_url: str,
        resource: FetchedResource,
        classification: ClassifiedWebPage,
        metadata: Mapping[str, object] | None = None,
    ) -> PreparedWebPageIngestion:
        """Prepare one page and its product assets without relational writes."""

        normalize_official_url(site_url, classification.canonical_url)
        source_kind, trust_tier = _page_policy(classification.page_type)
        document = _document_for_page(resource, classification)
        source_id = _source_id(classification.canonical_url)
        existing = (
            None
            if self._snapshot_lookup is None
            else self._snapshot_lookup.find_snapshot_by_content(
                project_id=project_id,
                source_id=source_id,
                content_hash=document.content_hash,
                parser_name=document.parser_name,
                parser_version=document.parser_version,
            )
        )
        snapshot_id = (
            existing.snapshot_id
            if existing is not None
            else _snapshot_id(source_id, document.content_hash)
        )
        chunks = self._chunker.chunk(
            project_id=project_id,
            source_id=source_id,
            snapshot_id=snapshot_id,
            document=document,
        )
        raw_uri = self._artifact_store.put(
            project_id=project_id,
            namespace="raw-web",
            content_hash=document.content_hash,
            filename="page.html",
            content=resource.content,
        )
        normalized = _normalized_page_bytes(classification, document)
        normalized_hash = sha256(normalized).hexdigest()
        normalized_uri = self._artifact_store.put(
            project_id=project_id,
            namespace="normalized-web",
            content_hash=normalized_hash,
            filename="page.json",
            content=normalized,
        )
        source = KnowledgeSource(
            project_id=project_id,
            source_id=source_id,
            display_name=(
                classification.heading
                or classification.title
                or classification.canonical_url
            ),
            source_kind=source_kind,
            trust_tier=trust_tier,
            status="inbox",
            canonical_url=classification.canonical_url,
            public_source=True,
            metadata={
                "classification": {
                    "page_type": classification.page_type,
                    "confidence": classification.confidence,
                    "reason": "; ".join(classification.reasons),
                    "reasons": list(classification.reasons),
                    "classifier_version": WEB_PAGE_PARSER_VERSION,
                },
                "site_url": normalize_site_url(site_url),
                **dict(metadata or {}),
            },
        )
        snapshot = existing or SourceSnapshot(
            project_id=project_id,
            source_id=source_id,
            snapshot_id=snapshot_id,
            content_hash=document.content_hash,
            fetched_at=datetime.now(timezone.utc),
            parser_name=document.parser_name,
            parser_version=document.parser_version,
            raw_artifact_uri=raw_uri,
            normalized_artifact_uri=normalized_uri,
            metadata={
                "page_type": classification.page_type,
                "classification_confidence": classification.confidence,
                "classification_reasons": list(classification.reasons),
                "block_count": len(document.blocks),
                "source_projection": {
                    "schema_version": 1,
                    "display_name": source.display_name,
                    "public_source": source.public_source,
                    "canonical_url": source.canonical_url,
                    "metadata": dict(source.metadata),
                },
            },
        )
        product: KnowledgeProduct | None = None
        source_evidence: ProductSourceEvidence | None = None
        assets: tuple[KnowledgeAsset, ...] = ()
        snapshot_assets: tuple[SnapshotAsset, ...] = ()
        asset_evidence: tuple[ProductAssetEvidence, ...] = ()
        warnings: tuple[str, ...] = ()
        if classification.page_type == "product_detail":
            (
                product,
                source_evidence,
                assets,
                snapshot_assets,
                asset_evidence,
                warnings,
            ) = self._prepare_product_evidence(
                project_id=project_id,
                site_url=site_url,
                source=source,
                snapshot=snapshot,
                page=classification,
            )
        return PreparedWebPageIngestion(
            source=source,
            snapshot=snapshot,
            normalized_content_hash=normalized_hash,
            chunks=chunks,
            classification=classification,
            product=product,
            source_evidence=source_evidence,
            assets=assets,
            snapshot_assets=snapshot_assets,
            asset_evidence=asset_evidence,
            warnings=warnings,
        )

    def _persist_prepared(
        self,
        prepared: PreparedWebPageIngestion,
    ) -> WebPageIngestionResult:
        """Keep the M2 Local persistence behavior behind the new prepare seam."""

        self._repository.upsert_source(prepared.source)
        self._repository.store_snapshot(
            prepared.source.project_id,
            prepared.snapshot,
            prepared.chunks,
        )
        if prepared.product is not None:
            self._catalog_repository.upsert_product(prepared.product)
        if prepared.source_evidence is not None:
            self._catalog_repository.store_source_evidence(
                prepared.source_evidence
            )

        stored_assets: list[KnowledgeAsset] = []
        stored_ids: dict[str, str] = {}
        for asset in prepared.assets:
            stored = self._asset_repository.put_asset(asset)
            stored_assets.append(stored)
            stored_ids[asset.asset_id] = stored.asset_id
        for link in prepared.snapshot_assets:
            self._asset_repository.link_snapshot_asset(
                replace(
                    link,
                    asset_id=stored_ids.get(link.asset_id, link.asset_id),
                )
            )
        for evidence in prepared.asset_evidence:
            self._catalog_repository.store_asset_evidence(
                replace(
                    evidence,
                    asset_id=stored_ids.get(
                        evidence.asset_id,
                        evidence.asset_id,
                    ),
                )
            )
        return WebPageIngestionResult(
            source=prepared.source,
            snapshot=prepared.snapshot,
            chunks=prepared.chunks,
            classification=prepared.classification,
            product=prepared.product,
            assets=tuple(stored_assets),
            warnings=prepared.warnings,
        )

    def _prepare_product_evidence(
        self,
        *,
        project_id: str,
        site_url: str,
        source: KnowledgeSource,
        snapshot: SourceSnapshot,
        page: ClassifiedWebPage,
    ) -> tuple[
        KnowledgeProduct,
        ProductSourceEvidence,
        tuple[KnowledgeAsset, ...],
        tuple[SnapshotAsset, ...],
        tuple[ProductAssetEvidence, ...],
        tuple[str, ...],
    ]:
        parsed = page.product_page
        name = (
            parsed.h1
            if parsed is not None and parsed.h1
            else page.heading or page.title
        )
        if not name:
            raise WordPressIngestionError(
                "product detail page requires a product name"
            )
        product = KnowledgeProduct(
            project_id=project_id,
            product_id=_product_id(page.canonical_url),
            name=name,
            canonical_url=page.canonical_url,
            category_path=page.breadcrumbs,
            metadata={
                "description": (
                    parsed.meta_description if parsed is not None else ""
                ),
                "main_content_facts": (
                    list(parsed.main_content_facts) if parsed is not None else []
                ),
                "specification_tables": (
                    list(parsed.specification_tables) if parsed is not None else []
                ),
                "faq": list(parsed.faq) if parsed is not None else [],
            },
        )
        source_evidence = ProductSourceEvidence(
            project_id=project_id,
            product_id=product.product_id,
            source_id=source.source_id,
            snapshot_id=snapshot.snapshot_id,
            relation="primary_detail",
            confidence=page.confidence,
            reason=(
                "deterministic classifier identified an official product "
                "detail page"
            ),
            metadata={
                "classification_reasons": list(page.reasons),
                "selection_projection": {
                    "schema_version": 1,
                    "name": product.name,
                    "canonical_url": product.canonical_url,
                    "description": product.metadata.get(
                        "description",
                        "",
                    ),
                    "reference_facts": product.metadata.get(
                        "main_content_facts",
                        [],
                    ),
                    "specification_tables": product.metadata.get(
                        "specification_tables",
                        [],
                    ),
                },
            },
        )
        if parsed is None or self._max_product_images == 0:
            return product, source_evidence, (), (), (), ()

        candidates = []
        seen_urls: set[str] = set()
        ordered_groups = (
            ("gallery", parsed.main_gallery),
            ("json_ld", parsed.json_ld_product_images),
            ("body", parsed.body_images),
        )
        for evidence_kind, values in ordered_groups:
            for candidate in values:
                if candidate.source_url in seen_urls:
                    continue
                seen_urls.add(candidate.source_url)
                candidates.append((evidence_kind, candidate))
                if len(candidates) >= self._max_product_images:
                    break
            if len(candidates) >= self._max_product_images:
                break

        stored: list[KnowledgeAsset] = []
        snapshot_assets: list[SnapshotAsset] = []
        asset_evidence: list[ProductAssetEvidence] = []
        warnings: list[str] = []
        stored_hashes: set[str] = set()
        for evidence_kind, candidate in candidates:
            try:
                image_url = normalize_official_url(site_url, candidate.source_url)
                resource = self._fetcher.fetch(
                    site_url=site_url,
                    url=image_url,
                    max_bytes=MAX_PRODUCT_IMAGE_BYTES,
                )
                content_type = resource.content_type.partition(";")[0].strip().lower()
                if not content_type.startswith("image/"):
                    raise WordPressIngestionError(
                        "product image response is not an image"
                    )
                width, height = _image_dimensions(resource.content)
                content_hash = sha256(resource.content).hexdigest()
                if content_hash in stored_hashes:
                    continue
                stored_hashes.add(content_hash)
                artifact_uri = self._artifact_store.put(
                    project_id=project_id,
                    namespace="assets",
                    content_hash=content_hash,
                    filename=_safe_image_name(
                        image_url,
                        content_type,
                        len(stored),
                    ),
                    content=resource.content,
                )
                asset = KnowledgeAsset(
                    project_id=project_id,
                    asset_id=f"asset_{content_hash[:32]}",
                    content_hash=content_hash,
                    artifact_uri=artifact_uri,
                    content_type=content_type,
                    byte_size=len(resource.content),
                    width=width,
                    height=height,
                    metadata={"source_url": image_url},
                )
                link = SnapshotAsset(
                    project_id=project_id,
                    source_id=source.source_id,
                    snapshot_id=snapshot.snapshot_id,
                    asset_id=asset.asset_id,
                    evidence_kind=evidence_kind,  # type: ignore[arg-type]
                    ordinal=len(stored),
                    source_url=image_url,
                    alt_text=candidate.alt or None,
                    title=candidate.title or None,
                    caption=candidate.caption or None,
                    locator=dict(candidate.dom_context),
                    metadata={"source_kinds": list(candidate.source_kinds)},
                )
                snapshot_assets.append(link)
                role = (
                    "primary"
                    if not stored and evidence_kind in {"gallery", "json_ld"}
                    else "gallery"
                    if evidence_kind in {"gallery", "json_ld"}
                    else "detail"
                )
                asset_evidence.append(
                    ProductAssetEvidence(
                        project_id=project_id,
                        product_id=product.product_id,
                        source_id=source.source_id,
                        snapshot_id=snapshot.snapshot_id,
                        asset_id=asset.asset_id,
                        role=role,  # type: ignore[arg-type]
                        confidence=0.95 if role == "primary" else 0.8,
                        reason=(
                            "image appears in the official product page "
                            f"{evidence_kind} evidence"
                        ),
                        metadata={"source_url": image_url},
                    )
                )
                stored.append(asset)
            except (
                OfficialSiteFetchError,
                UnsafeOfficialSiteUrl,
                WordPressIngestionError,
                ValueError,
            ) as exc:
                warnings.append(
                    f"image candidate skipped ({candidate.source_url}): {exc}"
                )
        return (
            product,
            source_evidence,
            tuple(stored),
            tuple(snapshot_assets),
            tuple(asset_evidence),
            tuple(warnings),
        )


class WordPressProductSyncService:
    """Probe WordPress, ingest one category, then bounded product-detail candidates."""

    def __init__(
        self,
        *,
        fetcher: OfficialSiteFetcher,
        page_ingestion: WebPageIngestion,
    ) -> None:
        self._fetcher = fetcher
        self._page_ingestion = page_ingestion
        self._probe = WordPressSiteProbe(fetcher)

    def probe(self, site_url: str) -> WordPressProbeResult:
        return self._probe.probe(site_url)

    def sync_category(
        self,
        *,
        project_id: str,
        site_url: str,
        category_url: str,
        max_products: int = 12,
    ) -> WordPressCategorySyncResult:
        if max_products <= 0 or max_products > 50:
            raise ValueError("max_products must be between 1 and 50")
        site = normalize_site_url(site_url)
        category_target = normalize_official_url(site, category_url)
        probe = self._probe.probe(site)
        category_resource = self._fetcher.fetch(
            site_url=site,
            url=category_target,
        )
        category_page = classify_web_page(
            requested_url=category_resource.final_url,
            html=category_resource.content,
        )
        if category_page.page_type != "product_category":
            raise WordPressIngestionError(
                "category_url was not classified as a product category"
            )
        category = self._page_ingestion.ingest_resource(
            project_id=project_id,
            site_url=site,
            resource=category_resource,
            classification=category_page,
            metadata={
                "wordpress_probe": {
                    "detected": probe.detected,
                    "rest_api_url": probe.rest_api_url,
                    "probe_version": probe.probe_version,
                }
            },
        )
        candidates = discover_product_links(
            site_url=site,
            category_url=category_page.canonical_url,
            html=category_resource.content,
            limit=max_products,
        )
        products: list[WebPageIngestionResult] = []
        skipped: list[str] = []
        warnings: list[str] = []
        for candidate_url in candidates:
            try:
                resource = self._fetcher.fetch(
                    site_url=site,
                    url=candidate_url,
                )
                page = classify_web_page(
                    requested_url=resource.final_url,
                    html=resource.content,
                )
                if page.page_type != "product_detail":
                    skipped.append(candidate_url)
                    warnings.append(
                        f"{candidate_url}: classified as "
                        f"{page.page_type}, not product_detail"
                    )
                    continue
                result = self._page_ingestion.ingest_resource(
                    project_id=project_id,
                    site_url=site,
                    resource=resource,
                    classification=page,
                    metadata={
                        "discovered_from": category_page.canonical_url,
                        "wordpress_detected": probe.detected,
                    },
                )
            except (WordPressIngestionError, OfficialSiteFetchError, ValueError) as exc:
                skipped.append(candidate_url)
                warnings.append(f"{candidate_url}: {exc}")
                continue
            products.append(result)
        return WordPressCategorySyncResult(
            probe=probe,
            category=category,
            products=tuple(products),
            skipped_urls=tuple(skipped),
            warnings=tuple(warnings),
        )


__all__ = [
    "MAX_PRODUCT_IMAGES_PER_PAGE",
    "MAX_PRODUCT_IMAGE_BYTES",
    "OfficialWebPageIngestionService",
    "PreparedWebPageIngestion",
    "WebPageIngestion",
    "WebPagePreparation",
    "WebPageIngestionResult",
    "WebSnapshotLookup",
    "WordPressCategorySyncResult",
    "WordPressProductSyncService",
]
