from __future__ import annotations

import hashlib
import os
import sys
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import sqlalchemy as sa


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from knowledge_agent import (  # noqa: E402
    EMBEDDING_DIMENSIONS,
    ChunkEmbedding,
    KnowledgeChunk,
    KnowledgeProject,
    KnowledgeSource,
    SourceSnapshot,
)
from knowledge_agent.database import create_knowledge_engine  # noqa: E402
from knowledge_agent.repository import (  # noqa: E402
    KnowledgeConflictError,
    KnowledgeRecordNotFound,
    PostgresKnowledgeRepository,
)
from knowledge_agent.schema import (  # noqa: E402
    knowledge_chunks,
    knowledge_sources,
    projects,
    source_snapshots,
)


DATABASE_URL_ENV = "ARTICLE_AGENT_DATABASE_URL"
FETCHED_AT = datetime(2026, 7, 28, tzinfo=timezone.utc)


def axis_vector(index: int) -> tuple[float, ...]:
    values = [0.0] * EMBEDDING_DIMENSIONS
    values[index] = 1.0
    return tuple(values)


class KnowledgeAgentM1ResilienceTests(unittest.TestCase):
    engine: sa.Engine
    repository: PostgresKnowledgeRepository

    @classmethod
    def setUpClass(cls) -> None:
        database_url = os.environ.get(DATABASE_URL_ENV, "").strip()
        if not database_url:
            raise unittest.SkipTest(
                f"{DATABASE_URL_ENV} is not set; PostgreSQL resilience tests skipped"
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
        self.prefix = f"m1-resilience-{uuid.uuid4().hex}"
        self.project_ids: set[str] = set()

    def tearDown(self) -> None:
        if not self.project_ids:
            return
        project_ids = tuple(self.project_ids)
        with self.engine.begin() as connection:
            connection.execute(
                knowledge_chunks.delete().where(
                    knowledge_chunks.c.project_id.in_(project_ids)
                )
            )
            connection.execute(
                source_snapshots.delete().where(
                    source_snapshots.c.project_id.in_(project_ids)
                )
            )
            connection.execute(
                knowledge_sources.delete().where(
                    knowledge_sources.c.project_id.in_(project_ids)
                )
            )
            connection.execute(
                projects.delete().where(projects.c.project_id.in_(project_ids))
            )

    def _project_and_source(self, suffix: str) -> tuple[str, str]:
        project_id = f"{self.prefix}-{suffix}"
        source_id = f"{self.prefix}-{suffix}-source"
        self.project_ids.add(project_id)
        self.repository.upsert_project(
            KnowledgeProject(
                project_id=project_id,
                customer_name=f"Resilience {suffix}",
                official_domain=f"{suffix}.{self.prefix}.example.test",
            )
        )
        self.repository.upsert_source(
            KnowledgeSource(
                project_id=project_id,
                source_id=source_id,
                display_name=f"Resilience source {suffix}",
                source_kind="knowledge_page",
                trust_tier="hard_fact",
                metadata={"test_prefix": self.prefix},
            )
        )
        return project_id, source_id

    def _snapshot(
        self,
        project_id: str,
        source_id: str,
        suffix: str,
    ) -> SourceSnapshot:
        snapshot_id = f"{self.prefix}-{suffix}-snapshot"
        return SourceSnapshot(
            project_id=project_id,
            source_id=source_id,
            snapshot_id=snapshot_id,
            content_hash=hashlib.sha256(snapshot_id.encode("utf-8")).hexdigest(),
            fetched_at=FETCHED_AT,
            parser_name="resilience-test-parser",
            parser_version="1.0",
            metadata={"test_prefix": self.prefix},
        )

    def _chunk(
        self,
        snapshot: SourceSnapshot,
        ordinal: int,
        *,
        text: str | None = None,
    ) -> KnowledgeChunk:
        return KnowledgeChunk(
            project_id=snapshot.project_id,
            chunk_id=f"{snapshot.snapshot_id}:{ordinal:04d}",
            source_id=snapshot.source_id,
            snapshot_id=snapshot.snapshot_id,
            text=text or f"resilience chunk {ordinal}",
            ordinal=ordinal,
            heading_path=("Resilience",),
            locator={"ordinal": ordinal},
            metadata={"test_prefix": self.prefix},
        )

    def _embedding(
        self,
        chunk: KnowledgeChunk,
        *,
        model: str,
        axis: int,
    ) -> ChunkEmbedding:
        return ChunkEmbedding(
            project_id=chunk.project_id,
            chunk_id=chunk.chunk_id,
            snapshot_id=chunk.snapshot_id,
            embedding_model=model,
            vector=axis_vector(axis),
        )

    def test_concurrent_identical_snapshot_retries_both_succeed(self) -> None:
        project_id, source_id = self._project_and_source("same-snapshot")
        snapshot = self._snapshot(project_id, source_id, "same")
        chunks = (self._chunk(snapshot, 0), self._chunk(snapshot, 1))
        barrier = threading.Barrier(2)

        def store() -> None:
            barrier.wait(timeout=10)
            self.repository.store_snapshot(project_id, snapshot, chunks)

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(store) for _ in range(2)]
            for future in futures:
                future.result(timeout=20)

        with self.engine.connect() as connection:
            snapshot_count = connection.execute(
                sa.select(sa.func.count())
                .select_from(source_snapshots)
                .where(
                    source_snapshots.c.project_id == project_id,
                    source_snapshots.c.snapshot_id == snapshot.snapshot_id,
                )
            ).scalar_one()
            chunk_count = connection.execute(
                sa.select(sa.func.count())
                .select_from(knowledge_chunks)
                .where(
                    knowledge_chunks.c.project_id == project_id,
                    knowledge_chunks.c.snapshot_id == snapshot.snapshot_id,
                )
            ).scalar_one()
        self.assertEqual((snapshot_count, chunk_count), (1, 2))

    def test_concurrent_snapshot_retry_with_different_content_conflicts(
        self,
    ) -> None:
        project_id, source_id = self._project_and_source("snapshot-conflict")
        snapshot = self._snapshot(project_id, source_id, "conflict")
        first_chunks = (self._chunk(snapshot, 0, text="first immutable text"),)
        second_chunks = (
            replace(first_chunks[0], text="different immutable text"),
        )
        barrier = threading.Barrier(2)

        def store(chunks: tuple[KnowledgeChunk, ...]) -> str:
            barrier.wait(timeout=10)
            try:
                self.repository.store_snapshot(project_id, snapshot, chunks)
            except KnowledgeConflictError:
                return "conflict"
            return "stored"

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(store, first_chunks),
                executor.submit(store, second_chunks),
            ]
            outcomes = [future.result(timeout=20) for future in futures]

        self.assertCountEqual(outcomes, ("stored", "conflict"))

    def test_snapshot_rejects_chunk_id_without_snapshot_identity(self) -> None:
        project_id, source_id = self._project_and_source("chunk-identity")
        snapshot = self._snapshot(project_id, source_id, "identity")
        invalid_chunk = replace(
            self._chunk(snapshot, 0),
            chunk_id=f"{self.prefix}-missing-snapshot-prefix",
        )

        with self.assertRaisesRegex(ValueError, "snapshot_id followed by ':'"):
            self.repository.store_snapshot(
                project_id,
                snapshot,
                (invalid_chunk,),
            )

        with self.engine.connect() as connection:
            snapshot_count = connection.execute(
                sa.select(sa.func.count())
                .select_from(source_snapshots)
                .where(
                    source_snapshots.c.project_id == project_id,
                    source_snapshots.c.snapshot_id == snapshot.snapshot_id,
                )
            ).scalar_one()
        self.assertEqual(snapshot_count, 0)

    def test_concurrent_identical_embedding_retries_are_idempotent(self) -> None:
        project_id, source_id = self._project_and_source("same-embedding")
        snapshot = self._snapshot(project_id, source_id, "same-embedding")
        chunk = self._chunk(snapshot, 0)
        self.repository.store_snapshot(project_id, snapshot, (chunk,))
        embedding = self._embedding(chunk, model="model-a", axis=0)
        barrier = threading.Barrier(2)

        def store() -> None:
            barrier.wait(timeout=10)
            self.repository.store_embeddings(project_id, (embedding,))

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(store) for _ in range(2)]
            for future in futures:
                future.result(timeout=20)

        with self.engine.connect() as connection:
            stored = connection.execute(
                sa.select(
                    knowledge_chunks.c.embedding_model,
                    knowledge_chunks.c.embedding,
                ).where(
                    knowledge_chunks.c.project_id == project_id,
                    knowledge_chunks.c.chunk_id == chunk.chunk_id,
                )
            ).one()
        self.assertEqual(stored.embedding_model, "model-a")
        self.assertEqual(tuple(stored.embedding), axis_vector(0))

    def test_candidate_reembedding_recovers_but_active_snapshot_is_immutable(
        self,
    ) -> None:
        project_id, source_id = self._project_and_source("model-recovery")
        snapshot = self._snapshot(project_id, source_id, "model-recovery")
        chunks = (self._chunk(snapshot, 0), self._chunk(snapshot, 1))
        self.repository.store_snapshot(project_id, snapshot, chunks)
        model_a = tuple(
            self._embedding(chunk, model="model-a", axis=0) for chunk in chunks
        )
        self.repository.store_embeddings(project_id, model_a)

        missing = replace(
            self._embedding(chunks[1], model="model-b", axis=1),
            chunk_id=f"{snapshot.snapshot_id}:missing",
        )
        with self.assertRaises(KnowledgeRecordNotFound):
            self.repository.store_embeddings(
                project_id,
                (
                    self._embedding(chunks[0], model="model-b", axis=1),
                    missing,
                ),
            )
        with self.engine.connect() as connection:
            models_after_failure = tuple(
                connection.execute(
                    sa.select(knowledge_chunks.c.embedding_model)
                    .where(
                        knowledge_chunks.c.project_id == project_id,
                        knowledge_chunks.c.snapshot_id == snapshot.snapshot_id,
                    )
                    .order_by(knowledge_chunks.c.ordinal)
                ).scalars()
            )
        self.assertEqual(models_after_failure, ("model-a", "model-a"))

        model_b = tuple(
            self._embedding(chunk, model="model-b", axis=1) for chunk in chunks
        )
        self.repository.store_embeddings(project_id, model_b)
        self.repository.activate_snapshot(
            project_id,
            source_id,
            snapshot.snapshot_id,
            "model-b",
        )
        self.repository.store_embeddings(project_id, model_b)

        with self.assertRaisesRegex(KnowledgeConflictError, "active snapshot"):
            self.repository.store_embeddings(
                project_id,
                (self._embedding(chunks[0], model="model-c", axis=2),),
            )

        with self.engine.connect() as connection:
            stored = connection.execute(
                sa.select(
                    knowledge_chunks.c.embedding_model,
                    knowledge_chunks.c.embedding,
                ).where(
                    knowledge_chunks.c.project_id == project_id,
                    knowledge_chunks.c.chunk_id == chunks[0].chunk_id,
                )
            ).one()
        self.assertEqual(stored.embedding_model, "model-b")
        self.assertEqual(tuple(stored.embedding), axis_vector(1))


if __name__ == "__main__":
    unittest.main()
