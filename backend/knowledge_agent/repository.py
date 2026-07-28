from __future__ import annotations

from datetime import datetime, timezone
from struct import pack, unpack
from typing import Mapping, Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection, Engine, RowMapping
from sqlalchemy.exc import IntegrityError

from .contracts import (
    ChunkEmbedding,
    KnowledgeChunk,
    KnowledgeProject,
    KnowledgeSource,
    SourceSnapshot,
    require_project_scope,
)
from .schema import knowledge_chunks, knowledge_sources, projects, source_snapshots


class KnowledgeRepositoryError(RuntimeError):
    """Base error for formal knowledge persistence failures."""


class KnowledgeRecordNotFound(KnowledgeRepositoryError):
    """Raised when a project-scoped persistence target does not exist."""


class KnowledgeConflictError(KnowledgeRepositoryError):
    """Raised when an immutable record is retried with different content."""


class SnapshotActivationError(KnowledgeRepositoryError):
    """Raised when a snapshot is not ready to become the published version."""


def _required_text(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized


def _float32(value: float) -> float:
    return unpack("!f", pack("!f", float(value)))[0]


def _stored_vector(vector: Sequence[float]) -> tuple[float, ...]:
    return tuple(_float32(value) for value in vector)


def _chunk_from_row(row: Mapping[str, object] | RowMapping) -> KnowledgeChunk:
    return KnowledgeChunk(
        project_id=str(row["project_id"]),
        chunk_id=str(row["chunk_id"]),
        source_id=str(row["source_id"]),
        snapshot_id=str(row["snapshot_id"]),
        text=str(row["text"]),
        ordinal=int(row["ordinal"]),
        heading_path=tuple(row["heading_path"] or ()),  # type: ignore[arg-type]
        locator=dict(row["locator"] or {}),  # type: ignore[arg-type]
        metadata=dict(row["metadata"] or {}),  # type: ignore[arg-type]
    )


class PostgresKnowledgeRepository:
    """SQLAlchemy Core implementation of the project-scoped knowledge store."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    @property
    def engine(self) -> Engine:
        return self._engine

    def upsert_project(self, project: KnowledgeProject) -> None:
        statement = insert(projects).values(
            project_id=project.project_id,
            customer_name=project.customer_name,
            official_domain=project.official_domain,
            status=project.status,
        )
        statement = statement.on_conflict_do_update(
            index_elements=[projects.c.project_id],
            set_={
                "customer_name": statement.excluded.customer_name,
                "official_domain": statement.excluded.official_domain,
                "status": statement.excluded.status,
                "updated_at": sa.func.now(),
            },
        )
        with self._engine.begin() as connection:
            connection.execute(statement)

    def upsert_source(self, source: KnowledgeSource) -> None:
        if source.status == "published" or source.current_snapshot_id is not None:
            raise ValueError(
                "activate_snapshot must be used to publish a knowledge source"
            )

        statement = insert(knowledge_sources).values(
            project_id=source.project_id,
            source_id=source.source_id,
            display_name=source.display_name,
            source_kind=source.source_kind,
            trust_tier=source.trust_tier,
            status=source.status,
            public_source=source.public_source,
            canonical_url=source.canonical_url,
            metadata=dict(source.metadata),
        )
        statement = statement.on_conflict_do_update(
            index_elements=[
                knowledge_sources.c.project_id,
                knowledge_sources.c.source_id,
            ],
            set_={
                "display_name": statement.excluded.display_name,
                "source_kind": statement.excluded.source_kind,
                "trust_tier": statement.excluded.trust_tier,
                # A refresh may upsert source metadata before its new snapshot
                # has embeddings. Keep the active snapshot serving until
                # activate_snapshot performs the atomic switch. Explicit
                # moderation states still withdraw the source from retrieval.
                "status": sa.case(
                    (
                        sa.and_(
                            knowledge_sources.c.status == "published",
                            statement.excluded.status == "inbox",
                        ),
                        knowledge_sources.c.status,
                    ),
                    else_=statement.excluded.status,
                ),
                "public_source": statement.excluded.public_source,
                "canonical_url": statement.excluded.canonical_url,
                "metadata": statement.excluded.metadata,
                "updated_at": sa.func.now(),
            },
        )
        with self._engine.begin() as connection:
            connection.execute(statement)

    def store_snapshot(
        self,
        project_id: str,
        snapshot: SourceSnapshot,
        chunks: Sequence[KnowledgeChunk],
    ) -> None:
        normalized_project_id = require_project_scope(
            project_id,
            (snapshot, *chunks),
        )
        if not chunks:
            raise ValueError("chunks must not be empty")
        if any(
            chunk.source_id != snapshot.source_id
            or chunk.snapshot_id != snapshot.snapshot_id
            for chunk in chunks
        ):
            raise ValueError("chunks must belong to the supplied source snapshot")
        chunk_id_prefix = f"{snapshot.snapshot_id}:"
        if any(not chunk.chunk_id.startswith(chunk_id_prefix) for chunk in chunks):
            raise ValueError(
                "chunk_id must start with the snapshot_id followed by ':'"
            )
        if len({chunk.chunk_id for chunk in chunks}) != len(chunks):
            raise ValueError("chunk_id values must be unique within a snapshot")
        if len({chunk.ordinal for chunk in chunks}) != len(chunks):
            raise ValueError("chunk ordinals must be unique within a snapshot")

        try:
            with self._engine.begin() as connection:
                snapshot_insert = (
                    insert(source_snapshots)
                    .values(
                        project_id=snapshot.project_id,
                        snapshot_id=snapshot.snapshot_id,
                        source_id=snapshot.source_id,
                        content_hash=snapshot.content_hash,
                        parser_name=snapshot.parser_name,
                        parser_version=snapshot.parser_version,
                        raw_artifact_uri=snapshot.raw_artifact_uri,
                        normalized_artifact_uri=snapshot.normalized_artifact_uri,
                        fetched_at=snapshot.fetched_at,
                        metadata=dict(snapshot.metadata),
                    )
                    .on_conflict_do_nothing()
                    .returning(source_snapshots.c.snapshot_id)
                )
                inserted_snapshot_id = connection.execute(
                    snapshot_insert
                ).scalar_one_or_none()
                if inserted_snapshot_id is None:
                    existing = connection.execute(
                        sa.select(source_snapshots).where(
                            source_snapshots.c.project_id
                            == normalized_project_id,
                            source_snapshots.c.snapshot_id
                            == snapshot.snapshot_id,
                        )
                    ).mappings().one_or_none()
                    if existing is None:
                        raise KnowledgeConflictError(
                            "snapshot conflicts with an existing immutable record"
                        )
                    self._verify_snapshot_retry(
                        connection,
                        snapshot=snapshot,
                        chunks=chunks,
                        stored_snapshot=existing,
                    )
                    return

                connection.execute(
                    knowledge_chunks.insert(),
                    [
                        {
                            "project_id": chunk.project_id,
                            "chunk_id": chunk.chunk_id,
                            "source_id": chunk.source_id,
                            "snapshot_id": chunk.snapshot_id,
                            "ordinal": chunk.ordinal,
                            "heading_path": list(chunk.heading_path),
                            "text": chunk.text,
                            "locator": dict(chunk.locator),
                            "metadata": dict(chunk.metadata),
                        }
                        for chunk in chunks
                    ],
                )
        except IntegrityError as exc:
            raise KnowledgeConflictError(
                "snapshot conflicts with an existing project-scoped record"
            ) from exc

    def _verify_snapshot_retry(
        self,
        connection: Connection,
        *,
        snapshot: SourceSnapshot,
        chunks: Sequence[KnowledgeChunk],
        stored_snapshot: RowMapping,
    ) -> None:
        expected_snapshot = {
            "project_id": snapshot.project_id,
            "snapshot_id": snapshot.snapshot_id,
            "source_id": snapshot.source_id,
            "content_hash": snapshot.content_hash,
            "parser_name": snapshot.parser_name,
            "parser_version": snapshot.parser_version,
            "raw_artifact_uri": snapshot.raw_artifact_uri,
            "normalized_artifact_uri": snapshot.normalized_artifact_uri,
            "fetched_at": snapshot.fetched_at,
            "metadata": dict(snapshot.metadata),
        }
        if any(
            stored_snapshot[name] != value
            for name, value in expected_snapshot.items()
        ):
            raise KnowledgeConflictError(
                "snapshot_id already exists with different immutable content"
            )

        stored_chunks = connection.execute(
            sa.select(knowledge_chunks)
            .where(
                knowledge_chunks.c.project_id == snapshot.project_id,
                knowledge_chunks.c.snapshot_id == snapshot.snapshot_id,
            )
            .order_by(knowledge_chunks.c.ordinal)
        ).mappings().all()
        requested = sorted(chunks, key=lambda item: item.ordinal)
        if len(stored_chunks) != len(requested):
            raise KnowledgeConflictError(
                "snapshot retry contains a different number of chunks"
            )
        for row, chunk in zip(stored_chunks, requested, strict=True):
            if _chunk_from_row(row) != chunk:
                raise KnowledgeConflictError(
                    "snapshot retry contains different immutable chunks"
                )

    def store_embeddings(
        self,
        project_id: str,
        embeddings: Sequence[ChunkEmbedding],
    ) -> None:
        normalized_project_id = require_project_scope(project_id, embeddings)
        if not embeddings:
            raise ValueError("embeddings must not be empty")
        if len({item.chunk_id for item in embeddings}) != len(embeddings):
            raise ValueError("chunk embeddings must not contain duplicate chunk IDs")

        with self._engine.begin() as connection:
            chunk_ids = tuple(sorted(item.chunk_id for item in embeddings))
            identities = connection.execute(
                sa.select(
                    knowledge_chunks.c.chunk_id,
                    knowledge_chunks.c.source_id,
                    knowledge_chunks.c.snapshot_id,
                )
                .where(
                    knowledge_chunks.c.project_id == normalized_project_id,
                    knowledge_chunks.c.chunk_id.in_(chunk_ids),
                )
                .order_by(knowledge_chunks.c.chunk_id)
            ).mappings().all()
            identities_by_id = {
                str(row["chunk_id"]): row for row in identities
            }
            for item in embeddings:
                identity = identities_by_id.get(item.chunk_id)
                if (
                    identity is None
                    or identity["snapshot_id"] != item.snapshot_id
                ):
                    raise KnowledgeRecordNotFound(
                        "chunk embedding target was not found in the requested snapshot"
                    )

            source_ids = tuple(
                sorted({str(row["source_id"]) for row in identities})
            )
            locked_sources = connection.execute(
                sa.select(
                    knowledge_sources.c.source_id,
                    knowledge_sources.c.current_snapshot_id,
                )
                .where(
                    knowledge_sources.c.project_id == normalized_project_id,
                    knowledge_sources.c.source_id.in_(source_ids),
                )
                .order_by(knowledge_sources.c.source_id)
                .with_for_update()
            ).mappings().all()
            current_snapshots = {
                str(row["source_id"]): row["current_snapshot_id"]
                for row in locked_sources
            }
            if len(current_snapshots) != len(source_ids):
                raise KnowledgeRecordNotFound(
                    "knowledge source for an embedding target was not found"
                )

            stored_rows = connection.execute(
                sa.select(
                    knowledge_chunks.c.chunk_id,
                    knowledge_chunks.c.source_id,
                    knowledge_chunks.c.snapshot_id,
                    knowledge_chunks.c.embedding_model,
                    knowledge_chunks.c.embedding,
                )
                .where(
                    knowledge_chunks.c.project_id == normalized_project_id,
                    knowledge_chunks.c.chunk_id.in_(chunk_ids),
                )
                .order_by(knowledge_chunks.c.chunk_id)
                .with_for_update()
            ).mappings().all()
            stored_by_id = {
                str(row["chunk_id"]): row for row in stored_rows
            }
            pending_updates: list[tuple[ChunkEmbedding, tuple[float, ...]]] = []
            for item in embeddings:
                stored = stored_by_id.get(item.chunk_id)
                if (
                    stored is None
                    or stored["snapshot_id"] != item.snapshot_id
                ):
                    raise KnowledgeRecordNotFound(
                        "chunk embedding target was not found in the requested snapshot"
                    )

                vector = _stored_vector(item.vector)
                existing_model = stored["embedding_model"]
                existing_vector = stored["embedding"]
                if existing_model is not None:
                    normalized_existing = tuple(
                        float(value) for value in existing_vector
                    )
                    if (
                        existing_model == item.embedding_model
                        and normalized_existing == vector
                    ):
                        continue
                    if (
                        current_snapshots[str(stored["source_id"])]
                        == item.snapshot_id
                    ):
                        raise KnowledgeConflictError(
                            "an active snapshot embedding cannot be overwritten"
                        )

                pending_updates.append((item, vector))

            embedded_at = datetime.now(timezone.utc)
            for item, vector in pending_updates:
                result = connection.execute(
                    knowledge_chunks.update()
                    .where(
                        knowledge_chunks.c.project_id == normalized_project_id,
                        knowledge_chunks.c.chunk_id == item.chunk_id,
                        knowledge_chunks.c.snapshot_id == item.snapshot_id,
                    )
                    .values(
                        embedding_model=item.embedding_model,
                        embedding=list(vector),
                        embedded_at=embedded_at,
                    )
                )
                if result.rowcount != 1:
                    raise KnowledgeConflictError(
                        "chunk embedding changed during the transaction"
                    )

    def activate_snapshot(
        self,
        project_id: str,
        source_id: str,
        snapshot_id: str,
        embedding_model: str,
    ) -> None:
        normalized_project_id = _required_text(project_id, "project_id")
        normalized_source_id = _required_text(source_id, "source_id")
        normalized_snapshot_id = _required_text(snapshot_id, "snapshot_id")
        normalized_model = _required_text(embedding_model, "embedding_model")

        with self._engine.begin() as connection:
            source_exists = connection.execute(
                sa.select(knowledge_sources.c.source_id)
                .where(
                    knowledge_sources.c.project_id == normalized_project_id,
                    knowledge_sources.c.source_id == normalized_source_id,
                )
                .with_for_update()
            ).scalar_one_or_none()
            if source_exists is None:
                raise KnowledgeRecordNotFound(
                    "knowledge source was not found in the requested project"
                )

            snapshot_exists = connection.execute(
                sa.select(source_snapshots.c.snapshot_id).where(
                    source_snapshots.c.project_id == normalized_project_id,
                    source_snapshots.c.source_id == normalized_source_id,
                    source_snapshots.c.snapshot_id == normalized_snapshot_id,
                )
            ).scalar_one_or_none()
            if snapshot_exists is None:
                raise KnowledgeRecordNotFound(
                    "source snapshot was not found in the requested project"
                )

            total_chunks = connection.execute(
                sa.select(sa.func.count())
                .select_from(knowledge_chunks)
                .where(
                    knowledge_chunks.c.project_id == normalized_project_id,
                    knowledge_chunks.c.source_id == normalized_source_id,
                    knowledge_chunks.c.snapshot_id == normalized_snapshot_id,
                )
            ).scalar_one()
            incomplete_chunks = connection.execute(
                sa.select(sa.func.count())
                .select_from(knowledge_chunks)
                .where(
                    knowledge_chunks.c.project_id == normalized_project_id,
                    knowledge_chunks.c.source_id == normalized_source_id,
                    knowledge_chunks.c.snapshot_id == normalized_snapshot_id,
                    sa.or_(
                        knowledge_chunks.c.embedding.is_(None),
                        knowledge_chunks.c.embedding_model != normalized_model,
                    ),
                )
            ).scalar_one()
            if total_chunks == 0 or incomplete_chunks:
                raise SnapshotActivationError(
                    "snapshot cannot be activated until every chunk has a "
                    "matching embedding"
                )

            result = connection.execute(
                knowledge_sources.update()
                .where(
                    knowledge_sources.c.project_id == normalized_project_id,
                    knowledge_sources.c.source_id == normalized_source_id,
                )
                .values(
                    status="published",
                    current_snapshot_id=normalized_snapshot_id,
                    updated_at=sa.func.now(),
                )
            )
            if result.rowcount != 1:
                raise KnowledgeRecordNotFound(
                    "knowledge source disappeared during snapshot activation"
                )

    def get_chunks(
        self,
        project_id: str,
        chunk_ids: Sequence[str],
    ) -> Sequence[KnowledgeChunk]:
        normalized_project_id = _required_text(project_id, "project_id")
        normalized_ids = tuple(
            _required_text(chunk_id, "chunk_id") for chunk_id in chunk_ids
        )
        if not normalized_ids:
            return ()

        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(knowledge_chunks).where(
                    knowledge_chunks.c.project_id == normalized_project_id,
                    knowledge_chunks.c.chunk_id.in_(set(normalized_ids)),
                )
            ).mappings().all()
        chunks_by_id = {
            str(row["chunk_id"]): _chunk_from_row(row)
            for row in rows
        }
        return tuple(
            chunks_by_id[chunk_id]
            for chunk_id in normalized_ids
            if chunk_id in chunks_by_id
        )
