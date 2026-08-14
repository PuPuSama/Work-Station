from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import isfinite
from types import MappingProxyType
from typing import Literal, Mapping, Sequence
from urllib.parse import urlsplit


Metadata = Mapping[str, object]
Sufficiency = Literal["sufficient", "weak", "missing"]
RetrievalScopeType = Literal[
    "introduction",
    "h2_section",
    "product_fact",
    "faq",
]
EvidenceSupportScope = Literal["paragraph", "sentence"]
EvidenceClaimType = Literal["reference", "hard_fact"]
EvidenceSupportType = Literal["direct", "paraphrase", "contextual"]
EvidenceValidationStatus = Literal["valid", "needs_review", "invalid"]
ProjectStatus = Literal["active", "archived"]
SourceKind = Literal[
    "private_file",
    "product_detail",
    "product_category",
    "official_blog",
    "knowledge_page",
]
TrustTier = Literal["hard_fact", "reference_material", "writing_instruction"]
SourceStatus = Literal["inbox", "published", "needs_review", "rejected", "stale"]
Vector = tuple[float, ...]

EMBEDDING_DIMENSIONS = 1536
PROJECT_STATUSES = frozenset({"active", "archived"})
SOURCE_KINDS = frozenset(
    {
        "private_file",
        "product_detail",
        "product_category",
        "official_blog",
        "knowledge_page",
    }
)
EVIDENCE_SOURCE_KINDS = frozenset(
    source_kind for source_kind in SOURCE_KINDS if source_kind != "official_blog"
)
TRUST_TIERS = frozenset(
    {"hard_fact", "reference_material", "writing_instruction"}
)
SOURCE_STATUSES = frozenset(
    {"inbox", "published", "needs_review", "rejected", "stale"}
)
RETRIEVAL_SCOPE_TYPES = frozenset(
    {"introduction", "h2_section", "product_fact", "faq"}
)
EVIDENCE_SUPPORT_SCOPES = frozenset({"paragraph", "sentence"})
EVIDENCE_CLAIM_TYPES = frozenset({"reference", "hard_fact"})
EVIDENCE_SUPPORT_TYPES = frozenset({"direct", "paraphrase", "contextual"})
EVIDENCE_VALIDATION_STATUSES = frozenset(
    {"valid", "needs_review", "invalid"}
)


def _require(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} is required")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized


def _optional_text(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    return _require(value, field_name)


def _metadata(value: Metadata, field_name: str) -> Metadata:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a mapping")
    return MappingProxyType(dict(value))


def _http_url(value: str, field_name: str) -> str:
    normalized = _require(value, field_name)
    try:
        parsed = urlsplit(normalized)
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an absolute HTTP(S) URL") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port is not None
        and not 0 < port < 65536
    ):
        raise ValueError(f"{field_name} must be an absolute HTTP(S) URL")
    return normalized


def _official_domain(value: str) -> str:
    normalized = _require(value, "official_domain").rstrip(".").lower()
    if (
        "://" in normalized
        or "/" in normalized
        or "\\" in normalized
        or "@" in normalized
        or ":" in normalized
    ):
        raise ValueError("official_domain must be a hostname without a URL scheme or path")
    try:
        ascii_domain = normalized.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("official_domain must be a valid hostname") from exc
    labels = ascii_domain.split(".")
    if any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        or any(not (character.isalnum() or character == "-") for character in label)
        for label in labels
    ):
        raise ValueError("official_domain must be a valid hostname")
    return ascii_domain


def _artifact_uri(value: str | None, field_name: str) -> str | None:
    normalized = _optional_text(value, field_name)
    if normalized is None:
        return None
    if any(character.isspace() for character in normalized) or "\\" in normalized:
        raise ValueError(f"{field_name} must be an absolute URI")
    try:
        parsed = urlsplit(normalized)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an absolute URI") from exc
    if (
        not parsed.scheme
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
        or (
            parsed.scheme.lower() == "file"
            and not parsed.path.startswith("/")
        )
        or (
            parsed.scheme.lower() != "file"
            and not parsed.netloc
            and not parsed.path.startswith("/")
        )
    ):
        raise ValueError(f"{field_name} must be an absolute URI")
    return normalized


def _content_hash(value: str) -> str:
    normalized = _require(value, "content_hash").lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError("content_hash must be a 64-character SHA-256 hex digest")
    return normalized


def _embedding_vector(
    vector: Sequence[float],
    *,
    field_name: str = "vector",
    dimensions: int = EMBEDDING_DIMENSIONS,
) -> Vector:
    if isinstance(vector, (str, bytes)) or not isinstance(vector, Sequence):
        raise ValueError(f"{field_name} must be a numeric sequence")
    if len(vector) != dimensions:
        raise ValueError(f"{field_name} must contain exactly {dimensions} values")

    normalized: list[float] = []
    for value in vector:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{field_name} values must be numbers")
        number = float(value)
        if not isfinite(number):
            raise ValueError(f"{field_name} values must be finite")
        normalized.append(number)
    if not any(value != 0.0 for value in normalized):
        raise ValueError(f"{field_name} must not be a zero vector")
    return tuple(normalized)


def require_project_scope(project_id: str, items: Sequence[object]) -> str:
    """Validate a batch before it crosses a project-scoped persistence boundary."""

    normalized = _require(project_id, "project_id")
    for item in items:
        if getattr(item, "project_id", None) != normalized:
            raise ValueError("items must belong to the requested project")
    return normalized


@dataclass(frozen=True, slots=True)
class KnowledgeProject:
    project_id: str
    customer_name: str
    official_domain: str
    status: ProjectStatus = "active"

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_id", _require(self.project_id, "project_id"))
        object.__setattr__(
            self, "customer_name", _require(self.customer_name, "customer_name")
        )
        object.__setattr__(self, "official_domain", _official_domain(self.official_domain))
        if self.status not in PROJECT_STATUSES:
            raise ValueError("status must be active or archived")


@dataclass(frozen=True, slots=True)
class KnowledgeSource:
    project_id: str
    source_id: str
    display_name: str
    source_kind: SourceKind
    trust_tier: TrustTier
    status: SourceStatus = "inbox"
    canonical_url: str | None = None
    public_source: bool = False
    current_snapshot_id: str | None = None
    pending_snapshot_id: str | None = None
    metadata: Metadata = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("project_id", "source_id", "display_name"):
            object.__setattr__(self, name, _require(getattr(self, name), name))
        if self.source_kind not in SOURCE_KINDS:
            raise ValueError(f"source_kind must be one of: {', '.join(sorted(SOURCE_KINDS))}")
        if self.trust_tier not in TRUST_TIERS:
            raise ValueError(f"trust_tier must be one of: {', '.join(sorted(TRUST_TIERS))}")
        if self.status not in SOURCE_STATUSES:
            raise ValueError(f"status must be one of: {', '.join(sorted(SOURCE_STATUSES))}")
        if not isinstance(self.public_source, bool):
            raise ValueError("public_source must be a boolean")

        canonical_url = self.canonical_url
        if canonical_url is not None:
            canonical_url = _http_url(canonical_url, "canonical_url")
        if self.public_source and canonical_url is None:
            raise ValueError("public_source requires canonical_url")
        current_snapshot_id = _optional_text(
            self.current_snapshot_id, "current_snapshot_id"
        )
        pending_snapshot_id = _optional_text(
            self.pending_snapshot_id, "pending_snapshot_id"
        )
        if self.status == "published" and current_snapshot_id is None:
            raise ValueError("published source requires current_snapshot_id")
        if (
            current_snapshot_id is not None
            and pending_snapshot_id == current_snapshot_id
        ):
            raise ValueError(
                "pending_snapshot_id must differ from current_snapshot_id"
            )

        object.__setattr__(self, "canonical_url", canonical_url)
        object.__setattr__(self, "current_snapshot_id", current_snapshot_id)
        object.__setattr__(self, "pending_snapshot_id", pending_snapshot_id)
        object.__setattr__(self, "metadata", _metadata(self.metadata, "metadata"))


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    project_id: str
    source_id: str
    snapshot_id: str
    content_hash: str
    fetched_at: datetime
    parser_name: str
    parser_version: str
    raw_artifact_uri: str | None = None
    normalized_artifact_uri: str | None = None
    metadata: Metadata = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("project_id", "source_id", "snapshot_id", "parser_name", "parser_version"):
            object.__setattr__(self, name, _require(getattr(self, name), name))
        object.__setattr__(self, "content_hash", _content_hash(self.content_hash))
        if (
            not isinstance(self.fetched_at, datetime)
            or self.fetched_at.tzinfo is None
            or self.fetched_at.utcoffset() is None
        ):
            raise ValueError("fetched_at must be a timezone-aware datetime")
        object.__setattr__(
            self,
            "raw_artifact_uri",
            _artifact_uri(self.raw_artifact_uri, "raw_artifact_uri"),
        )
        object.__setattr__(
            self,
            "normalized_artifact_uri",
            _artifact_uri(self.normalized_artifact_uri, "normalized_artifact_uri"),
        )
        object.__setattr__(self, "metadata", _metadata(self.metadata, "metadata"))


@dataclass(frozen=True, slots=True)
class KnowledgeChunk:
    project_id: str
    chunk_id: str
    source_id: str
    snapshot_id: str
    text: str
    ordinal: int = 0
    heading_path: tuple[str, ...] = ()
    locator: Metadata = field(default_factory=dict)
    metadata: Metadata = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("project_id", "chunk_id", "source_id", "snapshot_id", "text"):
            object.__setattr__(self, name, _require(getattr(self, name), name))
        if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int):
            raise ValueError("ordinal must be a non-negative integer")
        if self.ordinal < 0:
            raise ValueError("ordinal must be a non-negative integer")
        if isinstance(self.heading_path, (str, bytes)):
            raise ValueError("heading_path must be a sequence of headings")
        heading_path = tuple(
            _require(heading, "heading_path") for heading in self.heading_path
        )
        object.__setattr__(self, "heading_path", heading_path)
        object.__setattr__(self, "locator", _metadata(self.locator, "locator"))
        object.__setattr__(self, "metadata", _metadata(self.metadata, "metadata"))


@dataclass(frozen=True, slots=True)
class ChunkEmbedding:
    project_id: str
    chunk_id: str
    snapshot_id: str
    embedding_model: str
    vector: Vector

    def __post_init__(self) -> None:
        for name in ("project_id", "chunk_id", "snapshot_id", "embedding_model"):
            object.__setattr__(self, name, _require(getattr(self, name), name))
        object.__setattr__(self, "vector", _embedding_vector(self.vector))


@dataclass(frozen=True, slots=True)
class EmbeddingBatch:
    vectors: tuple[Vector, ...]
    model: str
    prompt_tokens: int = 0
    total_tokens: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "model", _require(self.model, "model"))
        vectors = tuple(
            _embedding_vector(vector, field_name=f"vectors[{index}]")
            for index, vector in enumerate(self.vectors)
        )
        if not vectors:
            raise ValueError("vectors must not be empty")
        object.__setattr__(self, "vectors", vectors)
        for name in ("prompt_tokens", "total_tokens"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")

    @property
    def model_id(self) -> str:
        return self.model

    @property
    def dimensions(self) -> int:
        return EMBEDDING_DIMENSIONS

    @property
    def count(self) -> int:
        return len(self.vectors)

    @property
    def embeddings(self) -> tuple[Vector, ...]:
        """Compatibility alias for callers using the OpenAI response term."""

        return self.vectors


@dataclass(frozen=True, slots=True)
class RetrievalQuery:
    project_id: str
    text: str
    limit: int = 5
    filters: Metadata = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_id", _require(self.project_id, "project_id"))
        object.__setattr__(self, "text", _require(self.text, "text"))
        if (
            isinstance(self.limit, bool)
            or not isinstance(self.limit, int)
            or self.limit <= 0
        ):
            raise ValueError("limit must be positive")
        object.__setattr__(self, "filters", _metadata(self.filters, "filters"))


@dataclass(frozen=True, slots=True)
class RetrievalProvenance:
    """Published source identity returned with a retrieval hit."""

    project_id: str
    source_id: str
    snapshot_id: str
    display_name: str
    source_kind: SourceKind
    trust_tier: TrustTier
    public_source: bool
    canonical_url: str | None = None
    fetched_at: datetime | None = None

    def __post_init__(self) -> None:
        for name in ("project_id", "source_id", "snapshot_id", "display_name"):
            object.__setattr__(self, name, _require(getattr(self, name), name))
        if self.source_kind not in SOURCE_KINDS:
            raise ValueError(
                f"source_kind must be one of: {', '.join(sorted(SOURCE_KINDS))}"
            )
        if self.trust_tier not in TRUST_TIERS:
            raise ValueError(
                f"trust_tier must be one of: {', '.join(sorted(TRUST_TIERS))}"
            )
        if not isinstance(self.public_source, bool):
            raise ValueError("public_source must be a boolean")
        canonical_url = self.canonical_url
        if canonical_url is not None:
            canonical_url = _http_url(canonical_url, "canonical_url")
        if self.public_source and canonical_url is None:
            raise ValueError("public retrieval provenance requires canonical_url")
        object.__setattr__(self, "canonical_url", canonical_url)
        if (
            self.fetched_at is not None
            and (
                not isinstance(self.fetched_at, datetime)
                or self.fetched_at.tzinfo is None
                or self.fetched_at.utcoffset() is None
            )
        ):
            raise ValueError("fetched_at must be a timezone-aware datetime")


@dataclass(frozen=True, slots=True)
class RetrievalHit:
    chunk: KnowledgeChunk
    score: float
    provenance: RetrievalProvenance | None = None
    explanation: Metadata = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.score, bool) or not isinstance(self.score, (int, float)):
            raise ValueError("retrieval score must be a finite number")
        normalized_score = float(self.score)
        if not isfinite(normalized_score):
            raise ValueError("retrieval score must be a finite number")
        object.__setattr__(self, "score", normalized_score)
        if (
            self.provenance is not None
            and (
                self.provenance.project_id != self.chunk.project_id
                or self.provenance.source_id != self.chunk.source_id
                or self.provenance.snapshot_id != self.chunk.snapshot_id
            )
        ):
            raise ValueError("retrieval provenance must identify the hit chunk")
        object.__setattr__(
            self,
            "explanation",
            _metadata(self.explanation, "explanation"),
        )

    @property
    def project_id(self) -> str:
        return self.chunk.project_id


@dataclass(frozen=True, slots=True)
class DiscoveryRequest:
    project_id: str
    query: str
    official_domain: str
    max_results: int = 20

    def __post_init__(self) -> None:
        for name in ("project_id", "query", "official_domain"):
            object.__setattr__(self, name, _require(getattr(self, name), name))
        if self.max_results <= 0:
            raise ValueError("max_results must be positive")


@dataclass(frozen=True, slots=True)
class SourceCandidate:
    project_id: str
    url: str
    source_kind: str
    evidence: Metadata = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("project_id", "url", "source_kind"):
            object.__setattr__(self, name, _require(getattr(self, name), name))


@dataclass(frozen=True, slots=True)
class EvidencePackRequest:
    project_id: str
    article_id: str
    outline_version: int
    scope_type: str
    scope_key: str
    query_variants: tuple[str, ...]
    retrieval_plan_id: str | None = None
    scope_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("project_id", "article_id", "scope_type", "scope_key"):
            object.__setattr__(self, name, _require(getattr(self, name), name))
        if self.scope_type not in RETRIEVAL_SCOPE_TYPES:
            raise ValueError(
                "scope_type must be one of: "
                + ", ".join(sorted(RETRIEVAL_SCOPE_TYPES))
            )
        if self.outline_version <= 0:
            raise ValueError("outline_version must be positive")
        queries = tuple(_require(query, "query_variants") for query in self.query_variants)
        if not queries:
            raise ValueError("query_variants must not be empty")
        object.__setattr__(self, "query_variants", queries)
        object.__setattr__(
            self,
            "retrieval_plan_id",
            _optional_text(self.retrieval_plan_id, "retrieval_plan_id"),
        )
        object.__setattr__(
            self,
            "scope_id",
            _optional_text(self.scope_id, "scope_id"),
        )
        if (self.retrieval_plan_id is None) != (self.scope_id is None):
            raise ValueError(
                "retrieval_plan_id and scope_id must be provided together"
            )


@dataclass(frozen=True, slots=True)
class RetrievalScope:
    """One independently retrievable outline scope within a frozen plan."""

    project_id: str
    retrieval_plan_id: str
    scope_id: str
    ordinal: int
    scope_type: RetrievalScopeType
    scope_key: str
    title: str
    query_variants: tuple[str, ...]
    filters: Metadata = field(default_factory=dict)
    minimum_hits: int = 2
    minimum_distinct_sources: int = 1
    require_hard_fact: bool = False
    metadata: Metadata = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "project_id",
            "retrieval_plan_id",
            "scope_id",
            "scope_key",
            "title",
        ):
            object.__setattr__(self, name, _require(getattr(self, name), name))
        if self.scope_type not in RETRIEVAL_SCOPE_TYPES:
            raise ValueError(
                "scope_type must be one of: "
                + ", ".join(sorted(RETRIEVAL_SCOPE_TYPES))
            )
        if isinstance(self.ordinal, bool) or self.ordinal < 0:
            raise ValueError("ordinal must be a non-negative integer")
        queries = tuple(_require(query, "query_variants") for query in self.query_variants)
        if not queries:
            raise ValueError("query_variants must not be empty")
        object.__setattr__(self, "query_variants", queries)
        for name in ("minimum_hits", "minimum_distinct_sources"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if not isinstance(self.require_hard_fact, bool):
            raise ValueError("require_hard_fact must be a boolean")
        object.__setattr__(self, "filters", _metadata(self.filters, "filters"))
        object.__setattr__(self, "metadata", _metadata(self.metadata, "metadata"))

    def evidence_request(
        self,
        *,
        article_id: str,
        outline_version: int,
    ) -> EvidencePackRequest:
        return EvidencePackRequest(
            project_id=self.project_id,
            article_id=article_id,
            outline_version=outline_version,
            scope_type=self.scope_type,
            scope_key=self.scope_key,
            query_variants=self.query_variants,
            retrieval_plan_id=self.retrieval_plan_id,
            scope_id=self.scope_id,
        )


@dataclass(frozen=True, slots=True)
class RetrievalPlan:
    """Immutable retrieval intent for one article outline version."""

    project_id: str
    retrieval_plan_id: str
    article_id: str
    outline_version: int
    scopes: tuple[RetrievalScope, ...]
    max_gap_fill_rounds: int = 2
    metadata: Metadata = field(default_factory=dict)
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    def __post_init__(self) -> None:
        for name in ("project_id", "retrieval_plan_id", "article_id"):
            object.__setattr__(self, name, _require(getattr(self, name), name))
        if (
            isinstance(self.outline_version, bool)
            or not isinstance(self.outline_version, int)
            or self.outline_version <= 0
        ):
            raise ValueError("outline_version must be positive")
        if (
            isinstance(self.max_gap_fill_rounds, bool)
            or not isinstance(self.max_gap_fill_rounds, int)
            or not 0 <= self.max_gap_fill_rounds <= 2
        ):
            raise ValueError("max_gap_fill_rounds must be between 0 and 2")
        scopes = tuple(self.scopes)
        if not scopes:
            raise ValueError("scopes must not be empty")
        if any(
            scope.project_id != self.project_id
            or scope.retrieval_plan_id != self.retrieval_plan_id
            for scope in scopes
        ):
            raise ValueError("retrieval scopes must belong to the same plan")
        if len({scope.scope_id for scope in scopes}) != len(scopes):
            raise ValueError("scope_id values must be unique within a plan")
        if len({scope.ordinal for scope in scopes}) != len(scopes):
            raise ValueError("scope ordinals must be unique within a plan")
        object.__setattr__(self, "scopes", scopes)
        object.__setattr__(self, "metadata", _metadata(self.metadata, "metadata"))
        if (
            not isinstance(self.created_at, datetime)
            or self.created_at.tzinfo is None
            or self.created_at.utcoffset() is None
        ):
            raise ValueError("created_at must be a timezone-aware datetime")


@dataclass(frozen=True, slots=True)
class EvidencePack:
    evidence_pack_id: str
    request: EvidencePackRequest
    hits: tuple[RetrievalHit, ...]
    sufficiency: Sufficiency
    gap_reasons: tuple[str, ...] = ()
    hard_fact_chunk_ids: tuple[str, ...] = ()
    public_citation_urls: tuple[str, ...] = ()
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "evidence_pack_id",
            _require(self.evidence_pack_id, "evidence_pack_id"),
        )
        if self.sufficiency not in {"sufficient", "weak", "missing"}:
            raise ValueError("sufficiency must be sufficient, weak, or missing")
        if any(hit.project_id != self.project_id for hit in self.hits):
            raise ValueError("evidence hits must belong to the same project")
        gap_reasons = tuple(_require(reason, "gap_reasons") for reason in self.gap_reasons)
        object.__setattr__(self, "gap_reasons", gap_reasons)
        hit_chunk_ids = {hit.chunk.chunk_id for hit in self.hits}
        hard_fact_chunk_ids = tuple(
            _require(chunk_id, "hard_fact_chunk_ids")
            for chunk_id in self.hard_fact_chunk_ids
        )
        if not set(hard_fact_chunk_ids).issubset(hit_chunk_ids):
            raise ValueError("hard_fact_chunk_ids must identify evidence hits")
        object.__setattr__(self, "hard_fact_chunk_ids", hard_fact_chunk_ids)
        public_citation_urls = tuple(
            _http_url(url, "public_citation_urls")
            for url in self.public_citation_urls
        )
        object.__setattr__(self, "public_citation_urls", public_citation_urls)
        if (
            not isinstance(self.created_at, datetime)
            or self.created_at.tzinfo is None
            or self.created_at.utcoffset() is None
        ):
            raise ValueError("created_at must be a timezone-aware datetime")

    @property
    def project_id(self) -> str:
        return self.request.project_id


SectionEvidencePack = EvidencePack


@dataclass(frozen=True, slots=True)
class EvidenceLink:
    """Auditable claim-to-chunk link used by coverage and fact gates."""

    project_id: str
    evidence_link_id: str
    article_id: str
    paragraph_id: str
    paragraph_hash: str
    chunk_id: str
    support_scope: EvidenceSupportScope = "paragraph"
    claim_type: EvidenceClaimType = "reference"
    support_type: EvidenceSupportType = "paraphrase"
    sentence_id: str | None = None
    visible_words: int = 0
    public_citation_url: str | None = None
    validation_status: EvidenceValidationStatus = "valid"
    metadata: Metadata = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "project_id",
            "evidence_link_id",
            "article_id",
            "paragraph_id",
            "chunk_id",
        ):
            object.__setattr__(self, name, _require(getattr(self, name), name))
        paragraph_hash = _require(self.paragraph_hash, "paragraph_hash").lower()
        if len(paragraph_hash) != 64 or any(
            character not in "0123456789abcdef" for character in paragraph_hash
        ):
            raise ValueError("paragraph_hash must be a SHA-256 hex digest")
        object.__setattr__(self, "paragraph_hash", paragraph_hash)
        if self.support_scope not in EVIDENCE_SUPPORT_SCOPES:
            raise ValueError("support_scope must be paragraph or sentence")
        if self.claim_type not in EVIDENCE_CLAIM_TYPES:
            raise ValueError("claim_type must be reference or hard_fact")
        if self.support_type not in EVIDENCE_SUPPORT_TYPES:
            raise ValueError(
                "support_type must be direct, paraphrase, or contextual"
            )
        if self.validation_status not in EVIDENCE_VALIDATION_STATUSES:
            raise ValueError(
                "validation_status must be valid, needs_review, or invalid"
            )
        object.__setattr__(
            self,
            "sentence_id",
            _optional_text(self.sentence_id, "sentence_id"),
        )
        if (self.support_scope == "sentence") != (self.sentence_id is not None):
            raise ValueError("sentence support requires sentence_id")
        if self.claim_type == "hard_fact" and self.support_scope != "sentence":
            raise ValueError("hard facts require sentence-level evidence")
        if (
            isinstance(self.visible_words, bool)
            or not isinstance(self.visible_words, int)
            or self.visible_words < 0
        ):
            raise ValueError("visible_words must be a non-negative integer")
        if self.public_citation_url is not None:
            object.__setattr__(
                self,
                "public_citation_url",
                _http_url(self.public_citation_url, "public_citation_url"),
            )
        object.__setattr__(self, "metadata", _metadata(self.metadata, "metadata"))


@dataclass(frozen=True, slots=True)
class ParagraphEvidenceTarget:
    paragraph_id: str
    paragraph_hash: str
    visible_words: int
    eligible: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "paragraph_id", _require(self.paragraph_id, "paragraph_id"))
        normalized_hash = _require(self.paragraph_hash, "paragraph_hash").lower()
        if len(normalized_hash) != 64 or any(
            character not in "0123456789abcdef" for character in normalized_hash
        ):
            raise ValueError("paragraph_hash must be a SHA-256 hex digest")
        object.__setattr__(self, "paragraph_hash", normalized_hash)
        if (
            isinstance(self.visible_words, bool)
            or not isinstance(self.visible_words, int)
            or self.visible_words < 0
        ):
            raise ValueError("visible_words must be a non-negative integer")
        if not isinstance(self.eligible, bool):
            raise ValueError("eligible must be a boolean")


@dataclass(frozen=True, slots=True)
class HardFactSentenceTarget:
    paragraph_id: str
    sentence_id: str
    paragraph_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "paragraph_id", _require(self.paragraph_id, "paragraph_id"))
        object.__setattr__(self, "sentence_id", _require(self.sentence_id, "sentence_id"))
        normalized_hash = _require(self.paragraph_hash, "paragraph_hash").lower()
        if len(normalized_hash) != 64 or any(
            character not in "0123456789abcdef" for character in normalized_hash
        ):
            raise ValueError("paragraph_hash must be a SHA-256 hex digest")
        object.__setattr__(self, "paragraph_hash", normalized_hash)


@dataclass(frozen=True, slots=True)
class KnowledgeCoverageReport:
    eligible_paragraphs: int
    supported_paragraphs: int
    hard_fact_sentences: int
    supported_hard_fact_sentences: int

    def __post_init__(self) -> None:
        for name in (
            "eligible_paragraphs",
            "supported_paragraphs",
            "hard_fact_sentences",
            "supported_hard_fact_sentences",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.supported_paragraphs > self.eligible_paragraphs:
            raise ValueError("supported_paragraphs exceeds eligible_paragraphs")
        if self.supported_hard_fact_sentences > self.hard_fact_sentences:
            raise ValueError(
                "supported_hard_fact_sentences exceeds hard_fact_sentences"
            )

    @property
    def paragraph_coverage(self) -> float:
        if self.eligible_paragraphs == 0:
            return 1.0
        return self.supported_paragraphs / self.eligible_paragraphs

    @property
    def hard_fact_coverage(self) -> float:
        if self.hard_fact_sentences == 0:
            return 1.0
        return self.supported_hard_fact_sentences / self.hard_fact_sentences


@dataclass(frozen=True, slots=True)
class ResearchRequest:
    evidence_request: EvidencePackRequest
    max_gap_fill_rounds: int = 2

    def __post_init__(self) -> None:
        if self.max_gap_fill_rounds < 0:
            raise ValueError("max_gap_fill_rounds must not be negative")

    @property
    def project_id(self) -> str:
        return self.evidence_request.project_id


@dataclass(frozen=True, slots=True)
class ResearchResult:
    evidence_pack: EvidencePack
    discovered_sources: tuple[SourceCandidate, ...] = ()
    gap_fill_rounds: int = 0

    def __post_init__(self) -> None:
        if self.gap_fill_rounds < 0:
            raise ValueError("gap_fill_rounds must not be negative")
        if self.gap_fill_rounds > 2:
            raise ValueError("gap_fill_rounds exceeds the bounded research limit")
        if any(source.project_id != self.project_id for source in self.discovered_sources):
            raise ValueError("discovered sources must belong to the same project")

    @property
    def project_id(self) -> str:
        return self.evidence_pack.project_id
