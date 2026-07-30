from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.engine import Engine

from .artifact_store import LocalKnowledgeArtifactStore
from .assets import PostgresKnowledgeAssetRepository
from .catalog import PostgresProductCatalogRepository
from .database import create_knowledge_engine
from .ingestion import PrivateDocumentIngestionService
from .library import PostgresKnowledgeLibrary
from .repository import PostgresKnowledgeRepository


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

    def close(self) -> None:
        self.engine.dispose()


def create_knowledge_runtime(
    *,
    database_url: str,
    artifact_root: Path,
) -> KnowledgeAgentRuntime:
    engine = create_knowledge_engine(database_url)
    repository = PostgresKnowledgeRepository(engine)
    asset_repository = PostgresKnowledgeAssetRepository(engine)
    artifact_store = LocalKnowledgeArtifactStore(artifact_root)
    library = PostgresKnowledgeLibrary(engine)
    return KnowledgeAgentRuntime(
        engine=engine,
        repository=repository,
        asset_repository=asset_repository,
        catalog_repository=PostgresProductCatalogRepository(engine),
        library=library,
        artifact_store=artifact_store,
        private_document_ingestion=PrivateDocumentIngestionService(
            repository=repository,
            asset_repository=asset_repository,
            artifact_store=artifact_store,
            snapshot_lookup=library,
        ),
    )
