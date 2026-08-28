from __future__ import annotations

import hashlib
import json
import sys
import uuid
from datetime import datetime, timezone

from config import initialize_environment, load_config

from .contracts import (
    ChunkEmbedding,
    KnowledgeChunk,
    KnowledgeProject,
    KnowledgeSource,
    RetrievalQuery,
    SourceSnapshot,
)
from .database import create_knowledge_engine
from .embedding import OpenAICompatibleEmbeddingProvider
from .repository import PostgresKnowledgeRepository
from .retriever import PgVectorKnowledgeRetriever
from .schema import knowledge_chunks, knowledge_sources, projects, source_snapshots
from .settings import load_knowledge_agent_settings


def main() -> None:
    initialize_environment()
    app_config = load_config()
    settings = load_knowledge_agent_settings(
        enabled=app_config.knowledge_agent_enabled,
        require_ready=True,
    )
    assert settings.database_url is not None

    run_id = uuid.uuid4().hex
    project_id = f"m1-smoke-{run_id}"
    source_id = f"{project_id}:source"
    snapshot_id = f"{project_id}:snapshot:v1"
    texts = (
        "Wood screws use threads designed to grip timber and wood-based panels.",
        "Hex bolts are commonly paired with nuts in through-fastened assemblies.",
    )
    chunks = tuple(
        KnowledgeChunk(
            project_id=project_id,
            chunk_id=f"{snapshot_id}:{index:04d}",
            source_id=source_id,
            snapshot_id=snapshot_id,
            text=text,
            ordinal=index,
            heading_path=("M1 smoke",),
        )
        for index, text in enumerate(texts)
    )
    content_hash = hashlib.sha256("\n\n".join(texts).encode("utf-8")).hexdigest()

    engine = create_knowledge_engine(settings.database_url)
    try:
        repository = PostgresKnowledgeRepository(engine)
        repository.upsert_project(
            KnowledgeProject(
                project_id=project_id,
                customer_name="M1 smoke fixture",
                official_domain="example.com",
            )
        )
        repository.upsert_source(
            KnowledgeSource(
                project_id=project_id,
                source_id=source_id,
                display_name="M1 smoke fixture",
                source_kind="knowledge_page",
                trust_tier="reference_material",
                canonical_url="https://example.com/m1-smoke",
                public_source=True,
            )
        )
        repository.store_snapshot(
            project_id,
            SourceSnapshot(
                project_id=project_id,
                source_id=source_id,
                snapshot_id=snapshot_id,
                content_hash=content_hash,
                fetched_at=datetime(2026, 7, 28, tzinfo=timezone.utc),
                parser_name="m1-smoke",
                parser_version="1",
            ),
            chunks,
        )

        with OpenAICompatibleEmbeddingProvider.from_settings(settings) as provider:
            embedded = provider.embed(tuple(chunk.text for chunk in chunks))
            repository.store_embeddings(
                project_id,
                tuple(
                    ChunkEmbedding(
                        project_id=project_id,
                        chunk_id=chunk.chunk_id,
                        snapshot_id=snapshot_id,
                        embedding_model=embedded.model_id,
                        vector=vector,
                    )
                    for chunk, vector in zip(
                        chunks,
                        embedded.vectors,
                        strict=True,
                    )
                ),
            )
            repository.activate_snapshot(
                project_id,
                source_id,
                snapshot_id,
                embedded.model_id,
            )
            retriever = PgVectorKnowledgeRetriever(engine, provider)
            hits = retriever.retrieve(
                RetrievalQuery(
                    project_id=project_id,
                    text="Which fastener is designed to grip timber?",
                    limit=2,
                )
            )

        print(
            json.dumps(
                {
                    "model": embedded.model_id,
                    "dimensions": embedded.dimensions,
                    "vector_count": embedded.count,
                    "hit_ids": [hit.chunk.chunk_id for hit in hits],
                    "scores": [round(hit.score, 6) for hit in hits],
                },
                ensure_ascii=False,
            )
        )
    finally:
        try:
            with engine.begin() as connection:
                connection.execute(
                    knowledge_sources.update()
                    .where(knowledge_sources.c.project_id == project_id)
                    .values(status="inbox", current_snapshot_id=None)
                )
                connection.execute(
                    knowledge_chunks.delete().where(
                        knowledge_chunks.c.project_id == project_id
                    )
                )
                connection.execute(
                    source_snapshots.delete().where(
                        source_snapshots.c.project_id == project_id
                    )
                )
                connection.execute(
                    knowledge_sources.delete().where(
                        knowledge_sources.c.project_id == project_id
                    )
                )
                connection.execute(
                    projects.delete().where(projects.c.project_id == project_id)
                )
        finally:
            engine.dispose()


def cli() -> None:
    """Run the explicit smoke without exposing lower-level failure details."""

    try:
        main()
    except Exception:
        print(
            json.dumps({"error_code": "M1_SMOKE_FAILED"}),
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    cli()
