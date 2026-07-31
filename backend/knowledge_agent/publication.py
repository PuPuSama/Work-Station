from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .contracts import ChunkEmbedding
from .interfaces import EmbeddingProvider, KnowledgeRepository
from .library import PostgresKnowledgeLibrary


class KnowledgePublicationError(RuntimeError):
    """Raised when an Inbox snapshot is not ready for atomic publication."""


@dataclass(frozen=True, slots=True)
class PublicationCandidate:
    """Fully embedded snapshot that has not necessarily been activated."""

    project_id: str
    source_id: str
    snapshot_id: str
    embedding_model: str
    chunk_count: int


@dataclass(frozen=True, slots=True)
class PublicationResult(PublicationCandidate):
    """Snapshot activated as the source's current published version."""


class KnowledgePublicationService:
    """Prepare embeddings, then atomically switch the current source."""

    def __init__(
        self,
        *,
        repository: KnowledgeRepository,
        library: PostgresKnowledgeLibrary,
        embedding_provider: EmbeddingProvider,
        batch_size: int = 64,
    ) -> None:
        if isinstance(batch_size, bool) or not isinstance(batch_size, int):
            raise ValueError("batch_size must be a positive integer")
        if batch_size <= 0:
            raise ValueError("batch_size must be a positive integer")
        self._repository = repository
        self._library = library
        self._embedding_provider = embedding_provider
        self._batch_size = batch_size

    def publish(
        self,
        *,
        project_id: str,
        source_id: str,
        snapshot_id: str | None = None,
    ) -> PublicationResult:
        candidate = self.prepare(
            project_id=project_id,
            source_id=source_id,
            snapshot_id=snapshot_id,
        )
        self._repository.activate_snapshot(
            candidate.project_id,
            candidate.source_id,
            candidate.snapshot_id,
            candidate.embedding_model,
        )
        return PublicationResult(
            project_id=candidate.project_id,
            source_id=candidate.source_id,
            snapshot_id=candidate.snapshot_id,
            embedding_model=candidate.embedding_model,
            chunk_count=candidate.chunk_count,
        )

    def prepare(
        self,
        *,
        project_id: str,
        source_id: str,
        snapshot_id: str | None = None,
    ) -> PublicationCandidate:
        """Embed a reviewed snapshot without changing the serving pointer."""

        source = self._library.get_source(project_id, source_id)
        if source is None:
            raise KnowledgePublicationError(
                "knowledge source was not found in the requested project"
            )
        review = source.metadata.get("review")
        if (
            not isinstance(review, Mapping)
            or review.get("decision") != "approve"
        ):
            raise KnowledgePublicationError(
                "source classification must be approved before publication"
            )
        selected_snapshot = (
            self._library.get_snapshot(project_id, source_id, snapshot_id)
            if snapshot_id is not None
            else self._library.latest_snapshot(project_id, source_id)
        )
        if selected_snapshot is None:
            raise KnowledgePublicationError(
                "source snapshot was not found in the requested project"
            )
        chunks = self._library.get_snapshot_chunks(
            project_id,
            source_id,
            selected_snapshot.snapshot_id,
        )
        if not chunks:
            raise KnowledgePublicationError(
                "source snapshot has no text chunks to publish"
            )

        for offset in range(0, len(chunks), self._batch_size):
            group = chunks[offset : offset + self._batch_size]
            batch = self._embedding_provider.embed(
                tuple(chunk.text for chunk in group)
            )
            if batch.count != len(group):
                raise KnowledgePublicationError(
                    "embedding provider returned an unexpected vector count"
                )
            if batch.model_id != self._embedding_provider.model_id:
                raise KnowledgePublicationError(
                    "embedding provider returned an unexpected model"
                )
            self._repository.store_embeddings(
                project_id,
                tuple(
                    ChunkEmbedding(
                        project_id=project_id,
                        chunk_id=chunk.chunk_id,
                        snapshot_id=chunk.snapshot_id,
                        embedding_model=batch.model_id,
                        vector=vector,
                    )
                    for chunk, vector in zip(group, batch.vectors, strict=True)
                ),
            )

        return PublicationCandidate(
            project_id=project_id,
            source_id=source_id,
            snapshot_id=selected_snapshot.snapshot_id,
            embedding_model=self._embedding_provider.model_id,
            chunk_count=len(chunks),
        )
