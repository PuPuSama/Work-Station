from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.engine import Engine

from .artifact_store import LocalKnowledgeArtifactStore
from .assets import PostgresKnowledgeAssetRepository
from .catalog import PostgresProductCatalogRepository
from .database import create_knowledge_engine
from .ingestion import PrivateDocumentIngestionService
from .interfaces import EmbeddingProvider
from .evidence_repository import (
    PostgresEvidenceLinkRepository,
    PostgresEvidencePackRepository,
    PostgresRetrievalPlanRepository,
)
from .hybrid_retriever import BasicHybridRetriever
from .library import PostgresKnowledgeLibrary
from .publication import KnowledgePublicationService
from .research_runs import PostgresResearchRunRepository
from .repository import PostgresKnowledgeRepository
from .web_ingestion import (
    OfficialWebPageIngestionService,
    WordPressProductSyncService,
)
from .wordpress import SafeOfficialSiteFetcher


@dataclass(slots=True)
class KnowledgeAgentRuntime:
    """Long-lived adapters shared by the optional M2 HTTP routes."""

    engine: Engine
    repository: PostgresKnowledgeRepository
    asset_repository: PostgresKnowledgeAssetRepository
    catalog_repository: PostgresProductCatalogRepository
    library: PostgresKnowledgeLibrary
    artifact_store: LocalKnowledgeArtifactStore
    private_document_ingestion: PrivateDocumentIngestionService
    wordpress_sync: WordPressProductSyncService
    publication: KnowledgePublicationService | None
    embedding_provider: EmbeddingProvider | None
    hybrid_retriever: BasicHybridRetriever | None
    retrieval_plan_repository: PostgresRetrievalPlanRepository
    evidence_pack_repository: PostgresEvidencePackRepository
    evidence_link_repository: PostgresEvidenceLinkRepository
    research_run_repository: PostgresResearchRunRepository

    def close(self) -> None:
        close_provider = getattr(self.embedding_provider, "close", None)
        if callable(close_provider):
            close_provider()
        self.engine.dispose()


def create_knowledge_runtime(
    *,
    database_url: str,
    artifact_root: Path,
    embedding_provider: EmbeddingProvider | None = None,
) -> KnowledgeAgentRuntime:
    engine = create_knowledge_engine(database_url)
    repository = PostgresKnowledgeRepository(engine)
    asset_repository = PostgresKnowledgeAssetRepository(engine)
    artifact_store = LocalKnowledgeArtifactStore(artifact_root)
    library = PostgresKnowledgeLibrary(engine)
    catalog_repository = PostgresProductCatalogRepository(engine)
    retrieval_plan_repository = PostgresRetrievalPlanRepository(engine)
    evidence_pack_repository = PostgresEvidencePackRepository(engine)
    evidence_link_repository = PostgresEvidenceLinkRepository(engine)
    research_run_repository = PostgresResearchRunRepository(engine)
    official_site_fetcher = SafeOfficialSiteFetcher()
    web_page_ingestion = OfficialWebPageIngestionService(
        repository=repository,
        asset_repository=asset_repository,
        catalog_repository=catalog_repository,
        artifact_store=artifact_store,
        fetcher=official_site_fetcher,
        snapshot_lookup=library,
    )
    return KnowledgeAgentRuntime(
        engine=engine,
        repository=repository,
        asset_repository=asset_repository,
        catalog_repository=catalog_repository,
        library=library,
        artifact_store=artifact_store,
        private_document_ingestion=PrivateDocumentIngestionService(
            repository=repository,
            asset_repository=asset_repository,
            artifact_store=artifact_store,
            snapshot_lookup=library,
        ),
        wordpress_sync=WordPressProductSyncService(
            fetcher=official_site_fetcher,
            page_ingestion=web_page_ingestion,
        ),
        publication=(
            None
            if embedding_provider is None
            else KnowledgePublicationService(
                repository=repository,
                library=library,
                embedding_provider=embedding_provider,
            )
        ),
        embedding_provider=embedding_provider,
        hybrid_retriever=(
            None
            if embedding_provider is None
            else BasicHybridRetriever(engine, embedding_provider)
        ),
        retrieval_plan_repository=retrieval_plan_repository,
        evidence_pack_repository=evidence_pack_repository,
        evidence_link_repository=evidence_link_repository,
        research_run_repository=research_run_repository,
    )
