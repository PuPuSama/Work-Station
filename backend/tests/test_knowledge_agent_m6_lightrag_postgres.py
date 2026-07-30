from __future__ import annotations

import hashlib
import os
import sys
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from knowledge_agent.contracts import (  # noqa: E402
    EMBEDDING_DIMENSIONS,
    ChunkEmbedding,
    KnowledgeChunk,
    KnowledgeProject,
    KnowledgeSource,
    RetrievalQuery,
    SourceSnapshot,
)
from knowledge_agent.database import create_knowledge_engine  # noqa: E402
from knowledge_agent.lightrag_retriever import (  # noqa: E402
    LightRAGCandidate,
    LightRAGKnowledgeRetriever,
)
from knowledge_agent.repository import PostgresKnowledgeRepository  # noqa: E402
from knowledge_agent.schema import (  # noqa: E402
    knowledge_chunks,
    knowledge_sources,
    projects,
    source_snapshots,
)


DATABASE_URL_ENV = "ARTICLE_AGENT_DATABASE_URL"
NOW = datetime(2026, 7, 30, tzinfo=timezone.utc)
MODEL = "m6-gate-model"


class _CandidateProvider:
    def __init__(self, candidates):
        self.candidates = candidates
        self.calls = []

    def search(self, *, project_id, text, limit):
        self.calls.append((project_id, text, limit))
        return self.candidates


def vector() -> tuple[float, ...]:
    return (1.0,) + (0.0,) * (EMBEDDING_DIMENSIONS - 1)


@unittest.skipUnless(
    os.environ.get(DATABASE_URL_ENV),
    f"{DATABASE_URL_ENV} is required for PostgreSQL integration tests",
)
class KnowledgeAgentM6LightRAGPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = create_knowledge_engine(os.environ[DATABASE_URL_ENV])
        cls.repository = PostgresKnowledgeRepository(cls.engine)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    def setUp(self) -> None:
        self.prefix = f"m6-lightrag-{uuid.uuid4().hex}"
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

    def _project(self, suffix: str) -> str:
        project_id = f"{self.prefix}-{suffix}"
        self.project_ids.add(project_id)
        self.repository.upsert_project(
            KnowledgeProject(
                project_id=project_id,
                customer_name=f"M6 {suffix}",
                official_domain=f"{suffix}.example.test",
            )
        )
        return project_id

    def _snapshot(
        self,
        project_id: str,
        source_id: str,
        suffix: str,
        *,
        activate: bool,
    ) -> str:
        if suffix == "v1":
            self.repository.upsert_source(
                KnowledgeSource(
                    project_id=project_id,
                    source_id=source_id,
                    display_name=f"Source {source_id}",
                    source_kind="product_detail",
                    trust_tier="hard_fact",
                    public_source=True,
                    canonical_url=f"https://{project_id}/product",
                )
            )
        snapshot_id = f"{source_id}-{suffix}"
        chunk_id = f"{snapshot_id}:0"
        snapshot = SourceSnapshot(
            project_id=project_id,
            source_id=source_id,
            snapshot_id=snapshot_id,
            content_hash=hashlib.sha256(snapshot_id.encode()).hexdigest(),
            fetched_at=NOW,
            parser_name="m6-test",
            parser_version="1",
        )
        self.repository.store_snapshot(
            project_id,
            snapshot,
            (
                KnowledgeChunk(
                    project_id=project_id,
                    chunk_id=chunk_id,
                    source_id=source_id,
                    snapshot_id=snapshot_id,
                    text=f"Evidence {suffix}",
                ),
            ),
        )
        self.repository.store_embeddings(
            project_id,
            (
                ChunkEmbedding(
                    project_id=project_id,
                    chunk_id=chunk_id,
                    snapshot_id=snapshot_id,
                    embedding_model=MODEL,
                    vector=vector(),
                ),
            ),
        )
        if activate:
            self.repository.activate_snapshot(
                project_id,
                source_id,
                snapshot_id,
                MODEL,
            )
        return chunk_id

    def test_external_candidates_are_rechecked_against_project_and_current_snapshot(
        self,
    ) -> None:
        project_a = self._project("a")
        project_b = self._project("b")
        source_a = f"{self.prefix}-source-a"
        stale_a = self._snapshot(
            project_a,
            source_a,
            "v1",
            activate=True,
        )
        current_a = self._snapshot(
            project_a,
            source_a,
            "v2",
            activate=True,
        )
        cross_project = self._snapshot(
            project_b,
            f"{self.prefix}-source-b",
            "v1",
            activate=True,
        )
        provider = _CandidateProvider(
            (
                LightRAGCandidate(cross_project, 0.99),
                LightRAGCandidate(stale_a, 0.98),
                LightRAGCandidate(current_a, 0.80),
            )
        )
        retriever = LightRAGKnowledgeRetriever(self.engine, provider)

        hits = retriever.retrieve(
            RetrievalQuery(
                project_id=project_a,
                text="product facts",
                limit=5,
            )
        )

        self.assertEqual(
            [hit.chunk.chunk_id for hit in hits],
            [current_a],
        )
        self.assertEqual(hits[0].project_id, project_a)
        self.assertEqual(hits[0].explanation["candidate_rank"], 3)
        self.assertEqual(
            provider.calls,
            [(project_a, "product facts", 20)],
        )

    def test_nonempty_filters_are_rejected_instead_of_ignored(self) -> None:
        provider = _CandidateProvider(())
        retriever = LightRAGKnowledgeRetriever(self.engine, provider)

        with self.assertRaisesRegex(ValueError, "metadata filters"):
            retriever.retrieve(
                RetrievalQuery(
                    project_id="example.com",
                    text="facts",
                    filters={"source_kinds": ["product_detail"]},
                )
            )
        self.assertEqual(provider.calls, [])
