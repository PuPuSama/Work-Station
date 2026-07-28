from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from math import isfinite
from types import MappingProxyType
from typing import Literal, Mapping, Sequence
from urllib.parse import urlsplit


Metadata = Mapping[str, object]
Sufficiency = Literal["sufficient", "weak", "missing"]
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
TRUST_TIERS = frozenset(
    {"hard_fact", "reference_material", "writing_instruction"}
)
SOURCE_STATUSES = frozenset(
    {"inbox", "published", "needs_review", "rejected", "stale"}
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
        if self.status == "published" and current_snapshot_id is None:
            raise ValueError("published source requires current_snapshot_id")

        object.__setattr__(self, "canonical_url", canonical_url)
        object.__setattr__(self, "current_snapshot_id", current_snapshot_id)
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
        if self.limit <= 0:
            raise ValueError("limit must be positive")


@dataclass(frozen=True, slots=True)
class RetrievalHit:
    chunk: KnowledgeChunk
    score: float

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

    def __post_init__(self) -> None:
        for name in ("project_id", "article_id", "scope_type", "scope_key"):
            object.__setattr__(self, name, _require(getattr(self, name), name))
        if self.outline_version <= 0:
            raise ValueError("outline_version must be positive")
        queries = tuple(_require(query, "query_variants") for query in self.query_variants)
        if not queries:
            raise ValueError("query_variants must not be empty")
        object.__setattr__(self, "query_variants", queries)


@dataclass(frozen=True, slots=True)
class EvidencePack:
    evidence_pack_id: str
    request: EvidencePackRequest
    hits: tuple[RetrievalHit, ...]
    sufficiency: Sufficiency
    gap_reasons: tuple[str, ...] = ()

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

    @property
    def project_id(self) -> str:
        return self.request.project_id


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
