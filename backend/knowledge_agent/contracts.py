from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Mapping


Metadata = Mapping[str, object]
Sufficiency = Literal["sufficient", "weak", "missing"]


def _require(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized


@dataclass(frozen=True, slots=True)
class KnowledgeChunk:
    project_id: str
    chunk_id: str
    source_id: str
    snapshot_id: str
    text: str
    metadata: Metadata = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("project_id", "chunk_id", "source_id", "snapshot_id", "text"):
            object.__setattr__(self, name, _require(getattr(self, name), name))


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
