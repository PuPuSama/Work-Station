from __future__ import annotations

from typing import Protocol, Sequence, runtime_checkable

from .contracts import (
    ChunkEmbedding,
    DiscoveryRequest,
    EmbeddingBatch,
    EvidencePack,
    EvidencePackRequest,
    EvidenceLink,
    KnowledgeChunk,
    KnowledgeProject,
    KnowledgeSource,
    ResearchRequest,
    ResearchResult,
    RetrievalHit,
    RetrievalQuery,
    RetrievalPlan,
    SourceSnapshot,
    SourceCandidate,
)


@runtime_checkable
class KnowledgeRepository(Protocol):
    """Persistence boundary for project-scoped knowledge."""

    def upsert_project(self, project: KnowledgeProject) -> None: ...

    def upsert_source(self, source: KnowledgeSource) -> None: ...

    def store_snapshot(
        self,
        project_id: str,
        snapshot: SourceSnapshot,
        chunks: Sequence[KnowledgeChunk],
    ) -> None: ...

    def store_embeddings(
        self, project_id: str, embeddings: Sequence[ChunkEmbedding]
    ) -> None: ...

    def activate_snapshot(
        self,
        project_id: str,
        source_id: str,
        snapshot_id: str,
        embedding_model: str,
    ) -> None: ...

    def get_chunks(
        self, project_id: str, chunk_ids: Sequence[str]
    ) -> Sequence[KnowledgeChunk]: ...


@runtime_checkable
class EvidencePackRepository(Protocol):
    """Persistence boundary for evidence packs introduced in M3."""

    def save_evidence_pack(self, evidence_pack: EvidencePack) -> None: ...

    def get_evidence_pack(
        self, project_id: str, evidence_pack_id: str
    ) -> EvidencePack | None: ...


@runtime_checkable
class RetrievalPlanRepository(Protocol):
    """Persistence boundary for outline-versioned retrieval intent."""

    def save_retrieval_plan(self, plan: RetrievalPlan) -> None: ...

    def get_retrieval_plan(
        self, project_id: str, retrieval_plan_id: str
    ) -> RetrievalPlan | None: ...

    def list_retrieval_plans(
        self,
        project_id: str,
        *,
        article_id: str | None = None,
        limit: int = 100,
    ) -> Sequence[RetrievalPlan]: ...


@runtime_checkable
class EvidenceLinkRepository(Protocol):
    """Persistence boundary for article claims linked to active knowledge."""

    def save_evidence_link(self, link: EvidenceLink) -> None: ...

    def list_evidence_links(
        self, project_id: str, article_id: str
    ) -> Sequence[EvidenceLink]: ...

    def mark_paragraph_links_for_review(
        self,
        project_id: str,
        article_id: str,
        paragraph_id: str,
        current_paragraph_hash: str,
    ) -> int: ...


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Provider-neutral, synchronous embedding boundary."""

    @property
    def model_id(self) -> str: ...

    @property
    def dimensions(self) -> int: ...

    def embed(self, texts: Sequence[str]) -> EmbeddingBatch: ...


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
