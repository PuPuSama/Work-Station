from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from .contracts import (
    EMBEDDING_DIMENSIONS,
    RetrievalHit,
    RetrievalQuery,
)
from .interfaces import EmbeddingProvider
from .repository import _chunk_from_row
from .schema import knowledge_chunks, knowledge_sources


class PgVectorKnowledgeRetriever:
    """M1 vector-only retriever over the currently published project snapshot."""

    def __init__(self, engine: Engine, embedding_provider: EmbeddingProvider) -> None:
        if embedding_provider.dimensions != EMBEDDING_DIMENSIONS:
            raise ValueError(
                f"embedding provider dimensions must be {EMBEDDING_DIMENSIONS}"
            )
        self._engine = engine
        self._embedding_provider = embedding_provider

    def retrieve(self, query: RetrievalQuery) -> tuple[RetrievalHit, ...]:
        if query.filters:
            raise ValueError("metadata filters are not supported by the M1 retriever")

        batch = self._embedding_provider.embed((query.text,))
        if batch.count != 1:
            raise ValueError("embedding provider must return one query vector")

        distance = knowledge_chunks.c.embedding.cosine_distance(
            list(batch.vectors[0])
        )
        current_chunks = knowledge_chunks.join(
            knowledge_sources,
            sa.and_(
                knowledge_sources.c.project_id == knowledge_chunks.c.project_id,
                knowledge_sources.c.source_id == knowledge_chunks.c.source_id,
                knowledge_sources.c.current_snapshot_id
                == knowledge_chunks.c.snapshot_id,
            ),
        )
        statement = (
            sa.select(
                knowledge_chunks.c.project_id,
                knowledge_chunks.c.chunk_id,
                knowledge_chunks.c.source_id,
                knowledge_chunks.c.snapshot_id,
                knowledge_chunks.c.ordinal,
                knowledge_chunks.c.heading_path,
                knowledge_chunks.c.text,
                knowledge_chunks.c.locator,
                knowledge_chunks.c.metadata,
                distance.label("cosine_distance"),
            )
            .select_from(current_chunks)
            .where(
                knowledge_chunks.c.project_id == query.project_id,
                knowledge_sources.c.status == "published",
                knowledge_chunks.c.embedding.is_not(None),
                knowledge_chunks.c.embedding_model == batch.model_id,
            )
            .order_by(distance.asc(), knowledge_chunks.c.chunk_id.asc())
            .limit(query.limit)
        )

        with self._engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()

        return tuple(
            RetrievalHit(
                chunk=_chunk_from_row(row),
                score=1.0 - float(row["cosine_distance"]),
            )
            for row in rows
        )
