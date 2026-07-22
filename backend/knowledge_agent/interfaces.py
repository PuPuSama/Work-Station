from __future__ import annotations

from typing import Protocol, Sequence, runtime_checkable

from .contracts import (
    DiscoveryRequest,
    EvidencePack,
    EvidencePackRequest,
    KnowledgeChunk,
    ResearchRequest,
    ResearchResult,
    RetrievalHit,
    RetrievalQuery,
    SourceCandidate,
)


@runtime_checkable
class KnowledgeRepository(Protocol):
    """Persistence boundary for project-scoped knowledge and evidence."""

    def upsert_chunks(self, project_id: str, chunks: Sequence[KnowledgeChunk]) -> None: ...

    def get_chunks(
        self, project_id: str, chunk_ids: Sequence[str]
    ) -> Sequence[KnowledgeChunk]: ...

    def save_evidence_pack(self, evidence_pack: EvidencePack) -> None: ...

    def get_evidence_pack(
        self, project_id: str, evidence_pack_id: str
    ) -> EvidencePack | None: ...


@runtime_checkable
class KnowledgeRetriever(Protocol):
    """Retrieval boundary independent of pgvector or another index backend."""

    def retrieve(self, query: RetrievalQuery) -> Sequence[RetrievalHit]: ...


@runtime_checkable
class SourceDiscovery(Protocol):
    """Discovery boundary for official-site and approved search providers."""

    def discover(self, request: DiscoveryRequest) -> Sequence[SourceCandidate]: ...


@runtime_checkable
class EvidencePackBuilder(Protocol):
    """Build one bounded evidence pack from already retrieved project data."""

    def build(
        self, request: EvidencePackRequest, hits: Sequence[RetrievalHit]
    ) -> EvidencePack: ...


@runtime_checkable
class ResearchOrchestrator(Protocol):
    """Coordinate bounded research without owning workflow business rules."""

    def run(self, request: ResearchRequest) -> ResearchResult: ...
