from __future__ import annotations

import hashlib
import os
import sys
import unittest
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from knowledge_agent import (  # noqa: E402
    EMBEDDING_DIMENSIONS,
    ChunkEmbedding,
    EmbeddingBatch,
    EmbeddingProvider,
    KnowledgeChunk,
    KnowledgeProject,
    KnowledgeSource,
    RetrievalQuery,
    SourceSnapshot,
)
from knowledge_agent.database import create_knowledge_engine  # noqa: E402
from knowledge_agent.repository import (  # noqa: E402
    KnowledgeConflictError,
    KnowledgeRecordNotFound,
    PostgresKnowledgeRepository,
    SnapshotActivationError,
)
from knowledge_agent.retriever import PgVectorKnowledgeRetriever  # noqa: E402
from knowledge_agent.schema import (  # noqa: E402
    knowledge_chunks,
    knowledge_sources,
    projects,
    source_snapshots,
)


DATABASE_URL_ENV = "ARTICLE_AGENT_DATABASE_URL"
MODEL_ID = "m1-test-model"
OTHER_MODEL_ID = "m1-other-model"
FETCHED_AT = datetime(2026, 7, 28, 0, 0, tzinfo=timezone.utc)


def axis_vector(index: int, magnitude: float = 1.0) -> tuple[float, ...]:
    values = [0.0] * EMBEDDING_DIMENSIONS
    values[index] = magnitude
    return tuple(values)


def plane_vector(x: float, y: float) -> tuple[float, ...]:
    values = [0.0] * EMBEDDING_DIMENSIONS
    values[0] = x
    values[1] = y
    return tuple(values)


class FakeEmbeddingProvider:
    def __init__(
        self,
        *,
        model_id: str = MODEL_ID,
        vector: tuple[float, ...] | None = None,
    ) -> None:
        self._model_id = model_id
        self._vector = vector or axis_vector(0)
        self.calls: list[tuple[str, ...]] = []

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dimensions(self) -> int:
        return EMBEDDING_DIMENSIONS

    def embed(self, texts: Sequence[str]) -> EmbeddingBatch:
        requested = tuple(texts)
        self.calls.append(requested)
        return EmbeddingBatch(
            vectors=tuple(self._vector for _ in requested),
            model=self._model_id,
        )


class KnowledgeAgentM1PostgresTests(unittest.TestCase):
    engine: sa.Engine
    repository: PostgresKnowledgeRepository

    @classmethod
    def setUpClass(cls) -> None:
        database_url = os.environ.get(DATABASE_URL_ENV, "").strip()
        if not database_url:
            raise unittest.SkipTest(
                f"{DATABASE_URL_ENV} is not set; PostgreSQL integration tests skipped"
            )

        cls.engine = create_knowledge_engine(database_url)
        with cls.engine.connect() as connection:
            connection.execute(sa.text("SELECT 1")).scalar_one()
        cls.repository = PostgresKnowledgeRepository(cls.engine)

    @classmethod
    def tearDownClass(cls) -> None:
        if hasattr(cls, "engine"):
            cls.engine.dispose()

    def setUp(self) -> None:
        self.prefix = f"m1-{uuid.uuid4().hex}"
        self.project_ids: set[str] = set()

    def tearDown(self) -> None:
        if not self.project_ids:
            return

        scoped_ids = tuple(self.project_ids)
        with self.engine.begin() as connection:
            connection.execute(
                knowledge_chunks.delete().where(
                    knowledge_chunks.c.project_id.in_(scoped_ids)
                )
            )
            connection.execute(
                source_snapshots.delete().where(
                    source_snapshots.c.project_id.in_(scoped_ids)
                )
            )
            connection.execute(
                knowledge_sources.delete().where(
                    knowledge_sources.c.project_id.in_(scoped_ids)
                )
            )
            connection.execute(
                projects.delete().where(projects.c.project_id.in_(scoped_ids))
            )

    def _create_project(self, suffix: str) -> str:
        project_id = f"{self.prefix}-{suffix}"
        self.project_ids.add(project_id)
        self.repository.upsert_project(
            KnowledgeProject(
                project_id=project_id,
                customer_name=f"M1 integration {suffix}",
                official_domain=f"{suffix}.{self.prefix}.example.test",
            )
        )
        return project_id

    def _create_source(
        self,
        project_id: str,
        suffix: str,
        *,
        canonical_url: str | None = None,
    ) -> str:
        source_id = f"{self.prefix}-{suffix}"
        self.repository.upsert_source(
            KnowledgeSource(
                project_id=project_id,
                source_id=source_id,
                display_name=f"M1 source {suffix}",
                source_kind="knowledge_page",
                trust_tier="hard_fact",
                canonical_url=canonical_url,
                public_source=canonical_url is not None,
                metadata={"test_prefix": self.prefix},
            )
        )
        return source_id

    def _snapshot(
        self,
        project_id: str,
        source_id: str,
        suffix: str,
        *,
        content_hash: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> SourceSnapshot:
        snapshot_id = f"{self.prefix}-{suffix}"
        digest = content_hash or hashlib.sha256(snapshot_id.encode("utf-8")).hexdigest()
        return SourceSnapshot(
            project_id=project_id,
            source_id=source_id,
            snapshot_id=snapshot_id,
            content_hash=digest,
            fetched_at=FETCHED_AT,
            parser_name="m1-test-parser",
            parser_version="1.0",
            metadata=metadata or {"test_prefix": self.prefix},
        )

    def _chunk(
        self,
        snapshot: SourceSnapshot,
        suffix: str,
        *,
        text: str | None = None,
        ordinal: int = 0,
    ) -> KnowledgeChunk:
        return KnowledgeChunk(
            project_id=snapshot.project_id,
            chunk_id=f"{snapshot.snapshot_id}:{suffix}",
            source_id=snapshot.source_id,
            snapshot_id=snapshot.snapshot_id,
            text=text or f"M1 integration chunk {suffix}",
            ordinal=ordinal,
            heading_path=("M1", suffix),
            locator={"ordinal": ordinal},
            metadata={"test_prefix": self.prefix},
        )

    def _embedding(
        self,
        chunk: KnowledgeChunk,
        *,
        model_id: str = MODEL_ID,
        vector: tuple[float, ...] | None = None,
    ) -> ChunkEmbedding:
        return ChunkEmbedding(
            project_id=chunk.project_id,
            chunk_id=chunk.chunk_id,
            snapshot_id=chunk.snapshot_id,
            embedding_model=model_id,
            vector=vector or axis_vector(0),
        )

    def _store_and_activate(
        self,
        project_id: str,
        source_id: str,
        snapshot: SourceSnapshot,
        chunks: Sequence[KnowledgeChunk],
        *,
        model_id: str = MODEL_ID,
        vectors: Sequence[tuple[float, ...]] | None = None,
    ) -> None:
        stored_vectors = vectors or tuple(axis_vector(0) for _ in chunks)
        self.repository.store_snapshot(project_id, snapshot, chunks)
        self.repository.store_embeddings(
            project_id,
            tuple(
                self._embedding(chunk, model_id=model_id, vector=vector)
                for chunk, vector in zip(chunks, stored_vectors, strict=True)
            ),
        )
        self.repository.activate_snapshot(
            project_id,
            source_id,
            snapshot.snapshot_id,
            model_id,
        )

    def test_catalog_has_vector_schema_and_project_scoped_constraints(self) -> None:
        inspector = sa.inspect(self.engine)
        self.assertTrue(
            {
                "projects",
                "knowledge_sources",
                "source_snapshots",
                "knowledge_chunks",
            }.issubset(inspector.get_table_names())
        )

        with self.engine.connect() as connection:
            extension = connection.execute(
                sa.text(
                    "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
                )
            ).scalar_one_or_none()
            vector_type = connection.execute(
                sa.text(
                    """
                    SELECT format_type(attribute.atttypid, attribute.atttypmod)
                    FROM pg_attribute AS attribute
                    JOIN pg_class AS relation
                      ON relation.oid = attribute.attrelid
                    JOIN pg_namespace AS namespace
                      ON namespace.oid = relation.relnamespace
                    WHERE namespace.nspname = current_schema()
                      AND relation.relname = 'knowledge_chunks'
                      AND attribute.attname = 'embedding'
                      AND NOT attribute.attisdropped
                    """
                )
            ).scalar_one()
            constraint_names = set(
                connection.execute(
                    sa.text(
                        """
                        SELECT conname
                        FROM pg_constraint
                        WHERE conrelid IN (
                            'projects'::regclass,
                            'knowledge_sources'::regclass,
                            'source_snapshots'::regclass,
                            'knowledge_chunks'::regclass
                        )
                        """
                    )
                ).scalars()
            )
            index_rows = connection.execute(
                sa.text(
                    """
                    SELECT index_relation.relname AS index_name,
                           access_method.amname AS access_method,
                           pg_get_indexdef(index_relation.oid) AS definition
                    FROM pg_index AS index_entry
                    JOIN pg_class AS table_relation
                      ON table_relation.oid = index_entry.indrelid
                    JOIN pg_namespace AS namespace
                      ON namespace.oid = table_relation.relnamespace
                    JOIN pg_class AS index_relation
                      ON index_relation.oid = index_entry.indexrelid
                    JOIN pg_am AS access_method
                      ON access_method.oid = index_relation.relam
                    WHERE namespace.nspname = current_schema()
                      AND table_relation.relname IN (
                          'knowledge_sources',
                          'knowledge_chunks'
                      )
                    """
                )
            ).mappings().all()

        self.assertIsNotNone(extension)
        self.assertEqual(vector_type, "vector(1536)")
        self.assertTrue(
            {
                "pk_projects",
                "pk_knowledge_sources",
                "fk_knowledge_sources_project",
                "ck_knowledge_sources_published_snapshot",
                "fk_knowledge_sources_current_snapshot",
                "pk_source_snapshots",
                "fk_source_snapshots_source",
                "uq_source_snapshots_source_snapshot",
                "uq_source_snapshots_content_parser",
                "pk_knowledge_chunks",
                "fk_knowledge_chunks_snapshot",
                "uq_knowledge_chunks_snapshot_ordinal",
                "ck_knowledge_chunks_snapshot_prefix",
                "ck_knowledge_chunks_embedding_state",
            }.issubset(constraint_names)
        )
        index_methods = {
            row["index_name"]: row["access_method"] for row in index_rows
        }
        self.assertEqual(
            index_methods["ix_knowledge_sources_retrieval_scope"],
            "btree",
        )
        self.assertEqual(
            index_methods["ix_knowledge_chunks_retrieval_scope"],
            "btree",
        )
        self.assertNotIn("hnsw", index_methods.values())
        self.assertFalse(
            any(" hnsw " in row["definition"].lower() for row in index_rows)
        )

        project_id = self._create_project("catalog")
        with self.assertRaises(IntegrityError):
            with self.engine.begin() as connection:
                connection.execute(
                    knowledge_sources.insert().values(
                        project_id=project_id,
                        source_id=f"{self.prefix}-invalid-published",
                        display_name="Invalid published source",
                        source_kind="knowledge_page",
                        trust_tier="hard_fact",
                        status="published",
                        public_source=False,
                        current_snapshot_id=None,
                    )
                )

    def test_database_rejects_cross_scope_and_invalid_chunk_states(self) -> None:
        project_a = self._create_project("constraints-a")
        project_b = self._create_project("constraints-b")
        shared_source_a = self._create_source(project_a, "shared-source")
        shared_source_b = self._create_source(project_b, "shared-source")
        self.assertEqual(shared_source_a, shared_source_b)
        other_source_a = self._create_source(project_a, "other-source")
        project_only_source_a = self._create_source(
            project_a,
            "project-only-source",
        )

        canonical_url = f"https://{self.prefix}.example.test/canonical"
        self._create_source(
            project_a,
            "canonical-source",
            canonical_url=canonical_url,
        )
        self._create_source(
            project_b,
            "canonical-source",
            canonical_url=canonical_url,
        )
        with self.assertRaises(IntegrityError):
            with self.engine.begin() as connection:
                connection.execute(
                    knowledge_sources.insert().values(
                        project_id=project_a,
                        source_id=f"{self.prefix}-duplicate-canonical",
                        display_name="Duplicate canonical source",
                        source_kind="knowledge_page",
                        trust_tier="hard_fact",
                        status="inbox",
                        public_source=True,
                        canonical_url=canonical_url,
                    )
                )

        with self.assertRaises(IntegrityError):
            with self.engine.begin() as connection:
                connection.execute(
                    source_snapshots.insert().values(
                        project_id=project_b,
                        snapshot_id=f"{self.prefix}-cross-project-snapshot",
                        source_id=project_only_source_a,
                        content_hash=hashlib.sha256(
                            b"cross-project-snapshot"
                        ).hexdigest(),
                        parser_name="m1-test-parser",
                        parser_version="1.0",
                        fetched_at=FETCHED_AT,
                    )
                )

        snapshot = self._snapshot(
            project_a,
            shared_source_a,
            "constraint-snapshot",
        )
        valid_chunk = self._chunk(snapshot, "valid", ordinal=0)
        self.repository.store_snapshot(project_a, snapshot, (valid_chunk,))

        with self.assertRaises(IntegrityError):
            with self.engine.begin() as connection:
                connection.execute(
                    knowledge_chunks.insert().values(
                        project_id=project_b,
                        chunk_id=f"{snapshot.snapshot_id}:cross-project",
                        source_id=shared_source_b,
                        snapshot_id=snapshot.snapshot_id,
                        ordinal=0,
                        text="Cross-project snapshot identity",
                    )
                )

        with self.assertRaises(IntegrityError):
            with self.engine.begin() as connection:
                connection.execute(
                    knowledge_sources.update()
                    .where(
                        knowledge_sources.c.project_id == project_a,
                        knowledge_sources.c.source_id == other_source_a,
                    )
                    .values(
                        status="published",
                        current_snapshot_id=snapshot.snapshot_id,
                    )
                )

        with self.assertRaises(IntegrityError):
            with self.engine.begin() as connection:
                connection.execute(
                    knowledge_chunks.insert().values(
                        project_id=project_a,
                        chunk_id=f"{snapshot.snapshot_id}:cross-source",
                        source_id=other_source_a,
                        snapshot_id=snapshot.snapshot_id,
                        ordinal=0,
                        text="Cross-source snapshot identity",
                    )
                )

        with self.assertRaises(IntegrityError):
            with self.engine.begin() as connection:
                connection.execute(
                    knowledge_chunks.insert().values(
                        project_id=project_a,
                        chunk_id=f"{snapshot.snapshot_id}:duplicate-ordinal",
                        source_id=shared_source_a,
                        snapshot_id=snapshot.snapshot_id,
                        ordinal=0,
                        text="Duplicate snapshot ordinal",
                    )
                )

        with self.assertRaises(IntegrityError):
            with self.engine.begin() as connection:
                connection.execute(
                    knowledge_chunks.insert().values(
                        project_id=project_a,
                        chunk_id=f"{self.prefix}-missing-snapshot-prefix",
                        source_id=shared_source_a,
                        snapshot_id=snapshot.snapshot_id,
                        ordinal=1,
                        text="Invalid chunk identity",
                    )
                )

        with self.assertRaises(IntegrityError):
            with self.engine.begin() as connection:
                connection.execute(
                    knowledge_chunks.update()
                    .where(
                        knowledge_chunks.c.project_id == project_a,
                        knowledge_chunks.c.chunk_id == valid_chunk.chunk_id,
                    )
                    .values(embedding_model=MODEL_ID)
                )

    def test_repository_is_project_scoped_and_snapshot_retries_are_exact(
        self,
    ) -> None:
        project_a = self._create_project("project-a")
        project_b = self._create_project("project-b")
        shared_source_suffix = "shared-source"
        source_a = self._create_source(project_a, shared_source_suffix)
        source_b = self._create_source(project_b, shared_source_suffix)
        self.assertEqual(source_a, source_b)

        snapshot_a = self._snapshot(project_a, source_a, "shared-snapshot")
        snapshot_b = replace(snapshot_a, project_id=project_b, source_id=source_b)
        chunk_a = self._chunk(snapshot_a, "shared-chunk", text="project A")
        chunk_b = replace(chunk_a, project_id=project_b, text="project B")

        self.repository.store_snapshot(project_a, snapshot_a, (chunk_a,))
        self.repository.store_snapshot(project_b, snapshot_b, (chunk_b,))
        self.repository.store_snapshot(project_a, snapshot_a, (chunk_a,))

        self.assertEqual(
            [chunk.text for chunk in self.repository.get_chunks(project_a, (chunk_a.chunk_id,))],
            ["project A"],
        )
        self.assertEqual(
            [chunk.text for chunk in self.repository.get_chunks(project_b, (chunk_b.chunk_id,))],
            ["project B"],
        )
        with self.assertRaisesRegex(ValueError, "requested project"):
            self.repository.store_snapshot(project_a, snapshot_b, (chunk_b,))

        with self.assertRaises(KnowledgeConflictError):
            self.repository.store_snapshot(
                project_a,
                snapshot_a,
                (replace(chunk_a, text="changed immutable text"),),
            )

        duplicate_content_snapshot = self._snapshot(
            project_a,
            source_a,
            "different-snapshot-id",
            content_hash=snapshot_a.content_hash,
        )
        duplicate_content_chunk = self._chunk(
            duplicate_content_snapshot,
            "different-chunk-id",
        )
        with self.assertRaises(KnowledgeConflictError):
            self.repository.store_snapshot(
                project_a,
                duplicate_content_snapshot,
                (duplicate_content_chunk,),
            )

        with self.engine.connect() as connection:
            snapshot_count = connection.execute(
                sa.select(sa.func.count())
                .select_from(source_snapshots)
                .where(source_snapshots.c.project_id == project_a)
            ).scalar_one()
            chunk_count = connection.execute(
                sa.select(sa.func.count())
                .select_from(knowledge_chunks)
                .where(knowledge_chunks.c.project_id == project_a)
            ).scalar_one()
        self.assertEqual((snapshot_count, chunk_count), (1, 1))

    def test_embedding_batch_rolls_back_and_activation_requires_completeness(
        self,
    ) -> None:
        project_id = self._create_project("embedding-transaction")
        source_id = self._create_source(project_id, "embedding-source")
        snapshot = self._snapshot(project_id, source_id, "embedding-snapshot")
        first_chunk = self._chunk(snapshot, "embedding-chunk-1", ordinal=0)
        second_chunk = self._chunk(snapshot, "embedding-chunk-2", ordinal=1)
        self.repository.store_snapshot(
            project_id,
            snapshot,
            (first_chunk, second_chunk),
        )

        missing_embedding = ChunkEmbedding(
            project_id=project_id,
            chunk_id=f"{snapshot.snapshot_id}:missing-chunk",
            snapshot_id=snapshot.snapshot_id,
            embedding_model=MODEL_ID,
            vector=axis_vector(2),
        )
        with self.assertRaises(KnowledgeRecordNotFound):
            self.repository.store_embeddings(
                project_id,
                (self._embedding(first_chunk), missing_embedding),
            )

        with self.engine.connect() as connection:
            rows = connection.execute(
                sa.select(
                    knowledge_chunks.c.embedding_model,
                    knowledge_chunks.c.embedding,
                    knowledge_chunks.c.embedded_at,
                )
                .where(
                    knowledge_chunks.c.project_id == project_id,
                    knowledge_chunks.c.snapshot_id == snapshot.snapshot_id,
                )
                .order_by(knowledge_chunks.c.ordinal)
            ).all()
        self.assertEqual(rows, [(None, None, None), (None, None, None)])

        first_embedding = self._embedding(first_chunk)
        self.repository.store_embeddings(project_id, (first_embedding,))
        self.repository.store_embeddings(project_id, (first_embedding,))
        with self.assertRaises(SnapshotActivationError):
            self.repository.activate_snapshot(
                project_id,
                source_id,
                snapshot.snapshot_id,
                MODEL_ID,
            )

        with self.engine.connect() as connection:
            source_state = connection.execute(
                sa.select(
                    knowledge_sources.c.status,
                    knowledge_sources.c.current_snapshot_id,
                ).where(
                    knowledge_sources.c.project_id == project_id,
                    knowledge_sources.c.source_id == source_id,
                )
            ).one()
        self.assertEqual(source_state, ("inbox", None))

        self.repository.store_embeddings(
            project_id,
            (self._embedding(second_chunk),),
        )
        self.repository.activate_snapshot(
            project_id,
            source_id,
            snapshot.snapshot_id,
            MODEL_ID,
        )

        with self.engine.connect() as connection:
            activated_state = connection.execute(
                sa.select(
                    knowledge_sources.c.status,
                    knowledge_sources.c.current_snapshot_id,
                ).where(
                    knowledge_sources.c.project_id == project_id,
                    knowledge_sources.c.source_id == source_id,
                )
            ).one()
        self.assertEqual(activated_state, ("published", snapshot.snapshot_id))

        with self.assertRaises(KnowledgeConflictError):
            self.repository.store_embeddings(
                project_id,
                (self._embedding(first_chunk, vector=axis_vector(1)),),
            )

    def test_unactivated_snapshot_does_not_replace_current_retrieval(self) -> None:
        project_id = self._create_project("snapshot-switch")
        source_id = self._create_source(project_id, "switch-source")

        old_snapshot = self._snapshot(project_id, source_id, "snapshot-old")
        old_chunk = self._chunk(old_snapshot, "chunk-old", text="old current")
        self._store_and_activate(
            project_id,
            source_id,
            old_snapshot,
            (old_chunk,),
            vectors=(axis_vector(1),),
        )

        self.repository.upsert_source(
            KnowledgeSource(
                project_id=project_id,
                source_id=source_id,
                display_name="Refreshed source metadata",
                source_kind="knowledge_page",
                trust_tier="hard_fact",
                metadata={"refresh": True},
            )
        )
        with self.engine.connect() as connection:
            source_state = connection.execute(
                sa.select(
                    knowledge_sources.c.status,
                    knowledge_sources.c.current_snapshot_id,
                ).where(
                    knowledge_sources.c.project_id == project_id,
                    knowledge_sources.c.source_id == source_id,
                )
            ).one()
        self.assertEqual(source_state, ("published", old_snapshot.snapshot_id))

        new_snapshot = self._snapshot(project_id, source_id, "snapshot-new")
        new_chunk = self._chunk(new_snapshot, "chunk-new", text="new candidate")
        self.repository.store_snapshot(project_id, new_snapshot, (new_chunk,))

        provider = FakeEmbeddingProvider(vector=axis_vector(0))
        retriever = PgVectorKnowledgeRetriever(self.engine, provider)
        before_embeddings = retriever.retrieve(
            RetrievalQuery(project_id=project_id, text="query", limit=10)
        )
        self.assertEqual(
            [hit.chunk.chunk_id for hit in before_embeddings],
            [old_chunk.chunk_id],
        )

        self.repository.store_embeddings(
            project_id,
            (self._embedding(new_chunk, vector=axis_vector(0)),),
        )
        before_activation = retriever.retrieve(
            RetrievalQuery(project_id=project_id, text="query", limit=10)
        )
        self.assertEqual(
            [hit.chunk.chunk_id for hit in before_activation],
            [old_chunk.chunk_id],
        )

        self.repository.activate_snapshot(
            project_id,
            source_id,
            new_snapshot.snapshot_id,
            MODEL_ID,
        )
        after_activation = retriever.retrieve(
            RetrievalQuery(project_id=project_id, text="query", limit=10)
        )
        self.assertEqual(
            [hit.chunk.chunk_id for hit in after_activation],
            [new_chunk.chunk_id],
        )

        self.repository.upsert_source(
            KnowledgeSource(
                project_id=project_id,
                source_id=source_id,
                display_name="Withdrawn source",
                source_kind="knowledge_page",
                trust_tier="hard_fact",
                status="stale",
            )
        )
        after_withdrawal = retriever.retrieve(
            RetrievalQuery(project_id=project_id, text="query", limit=10)
        )
        self.assertEqual(after_withdrawal, ())
        with self.engine.connect() as connection:
            withdrawn_state = connection.execute(
                sa.select(
                    knowledge_sources.c.status,
                    knowledge_sources.c.current_snapshot_id,
                ).where(
                    knowledge_sources.c.project_id == project_id,
                    knowledge_sources.c.source_id == source_id,
                )
            ).one()
        self.assertEqual(
            withdrawn_state,
            ("stale", new_snapshot.snapshot_id),
        )

    def test_retriever_isolates_projects_states_models_and_stable_ties(
        self,
    ) -> None:
        project_a = self._create_project("retrieval-a")
        project_b = self._create_project("retrieval-b")

        source_a = self._create_source(project_a, "retrieval-current")
        snapshot_a = self._snapshot(project_a, source_a, "retrieval-current")
        closer_chunk = self._chunk(
            snapshot_a,
            "rank-closer",
            text="project A cosine 0.8",
            ordinal=0,
        )
        farther_chunk = self._chunk(
            snapshot_a,
            "rank-farther",
            text="project A cosine 0.6",
            ordinal=1,
        )
        tied_chunk_z = self._chunk(
            snapshot_a,
            "tied-z",
            text="project A tied z",
            ordinal=2,
        )
        tied_chunk_a = self._chunk(
            snapshot_a,
            "tied-a",
            text="project A tied a",
            ordinal=3,
        )
        self._store_and_activate(
            project_a,
            source_a,
            snapshot_a,
            (closer_chunk, farther_chunk, tied_chunk_z, tied_chunk_a),
            vectors=(
                plane_vector(0.8, 0.6),
                plane_vector(0.6, 0.8),
                axis_vector(1),
                axis_vector(1),
            ),
        )

        unpublished_source = self._create_source(project_a, "unpublished")
        unpublished_snapshot = self._snapshot(
            project_a,
            unpublished_source,
            "unpublished",
        )
        unpublished_chunk = self._chunk(
            unpublished_snapshot,
            "unpublished",
            text="unpublished exact match",
        )
        self.repository.store_snapshot(
            project_a,
            unpublished_snapshot,
            (unpublished_chunk,),
        )
        self.repository.store_embeddings(
            project_a,
            (self._embedding(unpublished_chunk, vector=axis_vector(0)),),
        )

        other_model_source = self._create_source(project_a, "other-model")
        other_model_snapshot = self._snapshot(
            project_a,
            other_model_source,
            "other-model",
        )
        other_model_chunk = self._chunk(
            other_model_snapshot,
            "other-model",
            text="wrong model exact match",
        )
        self._store_and_activate(
            project_a,
            other_model_source,
            other_model_snapshot,
            (other_model_chunk,),
            model_id=OTHER_MODEL_ID,
            vectors=(axis_vector(0),),
        )

        source_b = self._create_source(project_b, "more-similar")
        snapshot_b = self._snapshot(project_b, source_b, "more-similar")
        chunk_b = self._chunk(
            snapshot_b,
            "more-similar",
            text="project B exact match",
        )
        self._store_and_activate(
            project_b,
            source_b,
            snapshot_b,
            (chunk_b,),
            vectors=(axis_vector(0),),
        )

        provider = FakeEmbeddingProvider(vector=axis_vector(0))
        self.assertIsInstance(provider, EmbeddingProvider)
        retriever = PgVectorKnowledgeRetriever(self.engine, provider)
        hits = retriever.retrieve(
            RetrievalQuery(project_id=project_a, text="query", limit=10)
        )

        expected_tied_order = sorted((tied_chunk_z.chunk_id, tied_chunk_a.chunk_id))
        self.assertEqual(
            [hit.chunk.chunk_id for hit in hits],
            [closer_chunk.chunk_id, farther_chunk.chunk_id, *expected_tied_order],
        )
        self.assertTrue(all(hit.project_id == project_a for hit in hits))
        self.assertNotIn(chunk_b.chunk_id, [hit.chunk.chunk_id for hit in hits])
        self.assertNotIn(
            unpublished_chunk.chunk_id,
            [hit.chunk.chunk_id for hit in hits],
        )
        self.assertNotIn(
            other_model_chunk.chunk_id,
            [hit.chunk.chunk_id for hit in hits],
        )
        self.assertGreater(hits[0].score, hits[1].score)
        self.assertGreater(hits[1].score, hits[2].score)
        self.assertAlmostEqual(hits[0].score, 0.8, places=5)
        self.assertAlmostEqual(hits[1].score, 0.6, places=5)
        self.assertAlmostEqual(hits[2].score, 0.0, places=5)
        self.assertAlmostEqual(hits[2].score, hits[3].score)

        project_b_hits = retriever.retrieve(
            RetrievalQuery(project_id=project_b, text="query", limit=10)
        )
        self.assertEqual(
            [hit.chunk.chunk_id for hit in project_b_hits],
            [chunk_b.chunk_id],
        )
        self.assertAlmostEqual(project_b_hits[0].score, 1.0, places=5)
        self.assertGreater(project_b_hits[0].score, hits[0].score)

        calls_before_filter_failure = len(provider.calls)
        with self.assertRaisesRegex(ValueError, "metadata filters"):
            retriever.retrieve(
                RetrievalQuery(
                    project_id=project_a,
                    text="query",
                    filters={"language": "en"},
                )
            )
        self.assertEqual(len(provider.calls), calls_before_filter_failure)


if __name__ == "__main__":
    unittest.main()
