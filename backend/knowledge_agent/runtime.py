from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.engine import Engine
from services.tavily import TavilyClient

from .artifact_store import LocalKnowledgeArtifactStore
from .assets import PostgresKnowledgeAssetRepository
from .catalog import PostgresProductCatalogRepository
from .database import create_knowledge_engine
from .ingestion import PrivateDocumentIngestionService
from .ingestion.mineru import document_parser_router_from_environment
from .interfaces import EmbeddingProvider
from .evidence_repository import (
    PostgresEvidenceLinkRepository,
    PostgresEvidencePackRepository,
    PostgresRetrievalPlanRepository,
)
from .hybrid_retriever import BasicHybridRetriever
from .library import PostgresKnowledgeLibrary
from .publication import KnowledgePublicationService
from .research_adapters import (
    M3ScopeEvidenceAdapter,
    OfficialCandidateIngestionAdapter,
    PostgresProjectDirectory,
    PostgresRetrievalPlanAdapter,
    TavilyOfficialDiscoveryAdapter,
)
from .research_execution import (
    ResearchGraphExecutionService,
    ResearchGraphSessionFactory,
)
from .research_runs import PostgresResearchRunRepository
from .research_chat import ResearchAnswerProvider, ResearchChatService
from .research_chat_repository import PostgresResearchChatRepository
from .research_telemetry import PostgresResearchTelemetry
from .scope_evidence import ScopeEvidenceService
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
    research_chat_repository: PostgresResearchChatRepository
    research_chat: ResearchChatService | None
    research_execution: ResearchGraphExecutionService | None

    def close(self) -> None:
        self.private_document_ingestion.close()
        close_provider = getattr(self.embedding_provider, "close", None)
        if callable(close_provider):
            close_provider()
        self.engine.dispose()


def create_knowledge_runtime(
    *,
    database_url: str,
    artifact_root: Path,
    embedding_provider: EmbeddingProvider | None = None,
    answer_provider: ResearchAnswerProvider | None = None,
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
    research_chat_repository = PostgresResearchChatRepository(engine)
    official_site_fetcher = SafeOfficialSiteFetcher()
    web_page_ingestion = OfficialWebPageIngestionService(
        repository=repository,
        asset_repository=asset_repository,
        catalog_repository=catalog_repository,
        artifact_store=artifact_store,
        fetcher=official_site_fetcher,
        snapshot_lookup=library,
    )
    publication = (
        None
        if embedding_provider is None
        else KnowledgePublicationService(
            repository=repository,
            library=library,
            embedding_provider=embedding_provider,
        )
    )
    hybrid_retriever = (
        None
        if embedding_provider is None
        else BasicHybridRetriever(engine, embedding_provider)
    )
    research_execution = None
    research_chat = None
    if hybrid_retriever is not None and answer_provider is not None:
        research_chat = ResearchChatService(
            retriever=hybrid_retriever,
            provider=answer_provider,
            conversations=research_chat_repository,
        )
    if publication is not None and hybrid_retriever is not None:
        projects = PostgresProjectDirectory(engine)
        scope_evidence = ScopeEvidenceService(
            plans=retrieval_plan_repository,
            retriever=hybrid_retriever,
            packs=evidence_pack_repository,
        )
        research_execution = ResearchGraphExecutionService(
            sessions=ResearchGraphSessionFactory(
                database_url=database_url,
                plans=PostgresRetrievalPlanAdapter(
                    retrieval_plan_repository
                ),
                evidence=M3ScopeEvidenceAdapter(scope_evidence),
                discovery=TavilyOfficialDiscoveryAdapter(
                    projects=projects,
                    plans=retrieval_plan_repository,
                    search=TavilyClient(),
                    attempts=research_run_repository,
                ),
                ingestion=OfficialCandidateIngestionAdapter(
                    projects=projects,
                    web_ingestion=web_page_ingestion,
                    repository=repository,
                    library=library,
                    publication=publication,
                    attempts=research_run_repository,
                ),
                telemetry=PostgresResearchTelemetry(
                    research_run_repository
                ),
            ),
            runs=research_run_repository,
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
            parser_router=document_parser_router_from_environment(),
        ),
        wordpress_sync=WordPressProductSyncService(
            fetcher=official_site_fetcher,
            page_ingestion=web_page_ingestion,
        ),
        publication=publication,
        embedding_provider=embedding_provider,
        hybrid_retriever=hybrid_retriever,
        retrieval_plan_repository=retrieval_plan_repository,
        evidence_pack_repository=evidence_pack_repository,
        evidence_link_repository=evidence_link_repository,
        research_run_repository=research_run_repository,
        research_chat_repository=research_chat_repository,
        research_chat=research_chat,
        research_execution=research_execution,
    )
