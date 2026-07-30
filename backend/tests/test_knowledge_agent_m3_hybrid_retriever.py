from __future__ import annotations

import hashlib
import os
import sys
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

import sqlalchemy as sa


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from knowledge_agent import (  # noqa: E402
    EMBEDDING_DIMENSIONS,
    BasicHybridRetriever,
    ChunkEmbedding,
    EmbeddingBatch,
    HybridRetrievalConfig,
    KnowledgeChunk,
    KnowledgeProduct,
    KnowledgeProject,
    KnowledgeSource,
    PostgresKnowledgeRepository,
    PostgresProductCatalogRepository,
    ProductSourceEvidence,
    RetrievalHit,
    RetrievalQuery,
    SourceSnapshot,
    create_knowledge_engine,
)
from knowledge_agent.schema import (  # noqa: E402
    knowledge_chunks,
    knowledge_product_source_evidence,
    knowledge_products,
    knowledge_sources,
    projects,
    source_snapshots,
)


MODEL_ID = "m3-hybrid-test-model"


def vector(x: float, y: float = 0.0) -> tuple[float, ...]:
    values = [0.0] * EMBEDDING_DIMENSIONS
    values[0] = x
    values[1] = y
    return tuple(values)


class FakeEmbeddingProvider:
    model_id = MODEL_ID
    dimensions = EMBEDDING_DIMENSIONS

    def __init__(self, query_vector: tuple[float, ...] | None = None) -> None:
        self.query_vector = query_vector or vector(1.0)
        self.calls: list[tuple[str, ...]] = []

    def embed(self, texts: Sequence[str]) -> EmbeddingBatch:
        requested = tuple(texts)
        self.calls.append(requested)
        return EmbeddingBatch(
            vectors=tuple(self.query_vector for _text in requested),
            model=self.model_id,
        )


class BoostReranker:
    def __init__(self, scores: Mapping[str, float]) -> None:
        self.scores = scores

    def rerank(
        self,
        query: RetrievalQuery,
        candidates: Sequence[RetrievalHit],
    ) -> Mapping[str, float]:
        del query, candidates
        return self.scores


@unittest.skipUnless(
    os.environ.get("ARTICLE_AGENT_DATABASE_URL"),
    "ARTICLE_AGENT_DATABASE_URL is required for PostgreSQL integration tests",
)
class BasicHybridRetrieverIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = create_knowledge_engine(os.environ["ARTICLE_AGENT_DATABASE_URL"])

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    def setUp(self) -> None:
        self.prefix = f"m3-{uuid.uuid4().hex}"
        self.project_ids: set[str] = set()
        self.repository = PostgresKnowledgeRepository(self.engine)
        self.catalog = PostgresProductCatalogRepository(self.engine)
        self.provider = FakeEmbeddingProvider()

    def tearDown(self) -> None:
        if not self.project_ids:
            return
        project_ids = tuple(self.project_ids)
        with self.engine.begin() as connection:
            connection.execute(
                knowledge_product_source_evidence.delete().where(
                    knowledge_product_source_evidence.c.project_id.in_(project_ids)
                )
            )
            connection.execute(
                knowledge_products.delete().where(
                    knowledge_products.c.project_id.in_(project_ids)
                )
            )
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
                customer_name=suffix,
                official_domain=f"{suffix}.{self.prefix}.example.test",
            )
        )
        return project_id

    def _published_source(
        self,
        *,
        project_id: str,
        suffix: str,
        source_kind: str,
        text: str,
        embedding: tuple[float, ...],
        metadata: dict[str, object] | None = None,
        model_id: str = MODEL_ID,
        publish: bool = True,
    ) -> tuple[str, str, str]:
        source_id = f"{self.prefix}-{suffix}"
        snapshot_id = f"{source_id}:snapshot"
        chunk_id = f"{snapshot_id}:000000"
        self.repository.upsert_source(
            KnowledgeSource(
                project_id=project_id,
                source_id=source_id,
                display_name=suffix,
                source_kind=source_kind,  # type: ignore[arg-type]
                trust_tier=(
                    "reference_material"
                    if source_kind == "official_blog"
                    else "hard_fact"
                ),
                canonical_url=f"https://{project_id}/{suffix}/",
                public_source=True,
                metadata={"fixture": suffix},
            )
        )
        snapshot = SourceSnapshot(
            project_id=project_id,
            source_id=source_id,
            snapshot_id=snapshot_id,
            content_hash=hashlib.sha256(text.encode()).hexdigest(),
            fetched_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
            parser_name="m3-test",
            parser_version="1",
        )
        chunk = KnowledgeChunk(
            project_id=project_id,
            chunk_id=chunk_id,
            source_id=source_id,
            snapshot_id=snapshot_id,
            text=text,
            heading_path=("Fasteners", suffix),
            metadata={"fixture": suffix, **dict(metadata or {})},
        )
        self.repository.store_snapshot(project_id, snapshot, (chunk,))
        self.repository.store_embeddings(
            project_id,
            (
                ChunkEmbedding(
                    project_id=project_id,
                    chunk_id=chunk_id,
                    snapshot_id=snapshot_id,
                    embedding_model=model_id,
                    vector=embedding,
                ),
            ),
        )
        if publish:
            self.repository.activate_snapshot(
                project_id,
                source_id,
                snapshot_id,
                model_id,
            )
        return source_id, snapshot_id, chunk_id

    def test_rrf_combines_channels_and_never_crosses_project_or_model(self) -> None:
        project_a = self._project("a")
        project_b = self._project("b")
        _detail_source, _detail_snapshot, detail_chunk = self._published_source(
            project_id=project_a,
            suffix="detail",
            source_kind="product_detail",
            text="Official wood screw dimensions for timber construction.",
            embedding=vector(0.8, 0.6),
        )
        _blog_source, _blog_snapshot, blog_chunk = self._published_source(
            project_id=project_a,
            suffix="blog",
            source_kind="official_blog",
            text="Selecting industrial fasteners for demanding projects.",
            embedding=vector(1.0),
        )
        self._published_source(
            project_id=project_a,
            suffix="wrong-model",
            source_kind="product_detail",
            text="Official wood screw timber exact phrase.",
            embedding=vector(1.0),
            model_id="other-model",
        )
        self._published_source(
            project_id=project_b,
            suffix="foreign",
            source_kind="product_detail",
            text="Official wood screw timber exact phrase.",
            embedding=vector(1.0),
        )
        _inbox_source, _inbox_snapshot, inbox_chunk = self._published_source(
            project_id=project_a,
            suffix="inbox",
            source_kind="product_detail",
            text="Official wood screw timber exact phrase.",
            embedding=vector(1.0),
            publish=False,
        )
        detail_source = f"{self.prefix}-detail"
        inactive_snapshot = f"{detail_source}:candidate"
        inactive_chunk = f"{inactive_snapshot}:000000"
        candidate_text = "Official wood screw timber newest exact phrase."
        self.repository.store_snapshot(
            project_a,
            SourceSnapshot(
                project_id=project_a,
                source_id=detail_source,
                snapshot_id=inactive_snapshot,
                content_hash=hashlib.sha256(candidate_text.encode()).hexdigest(),
                fetched_at=datetime(2026, 7, 31, tzinfo=timezone.utc),
                parser_name="m3-test",
                parser_version="1",
            ),
            (
                KnowledgeChunk(
                    project_id=project_a,
                    chunk_id=inactive_chunk,
                    source_id=detail_source,
                    snapshot_id=inactive_snapshot,
                    text=candidate_text,
                ),
            ),
        )
        self.repository.store_embeddings(
            project_a,
            (
                ChunkEmbedding(
                    project_id=project_a,
                    chunk_id=inactive_chunk,
                    snapshot_id=inactive_snapshot,
                    embedding_model=MODEL_ID,
                    vector=vector(1.0),
                ),
            ),
        )

        hits = BasicHybridRetriever(self.engine, self.provider).retrieve(
            RetrievalQuery(
                project_id=project_a,
                text="wood screw timber",
                limit=5,
            )
        )

        self.assertEqual(hits[0].chunk.chunk_id, detail_chunk)
        self.assertEqual(
            {hit.chunk.chunk_id for hit in hits},
            {detail_chunk, blog_chunk},
        )
        self.assertNotIn(inbox_chunk, {hit.chunk.chunk_id for hit in hits})
        self.assertNotIn(inactive_chunk, {hit.chunk.chunk_id for hit in hits})
        self.assertTrue(all(hit.project_id == project_a for hit in hits))
        self.assertEqual(hits[0].explanation["method"], "rrf")
        self.assertIsNotNone(hits[0].explanation["vector_rank"])
        self.assertIsNotNone(hits[0].explanation["lexical_rank"])
        self.assertIsNotNone(hits[0].provenance)
        assert hits[0].provenance is not None
        self.assertEqual(hits[0].provenance.source_kind, "product_detail")
        self.assertEqual(self.provider.calls, [("wood screw timber",)])

    def test_filters_cover_source_metadata_heading_and_product_identity(self) -> None:
        project_id = self._project("filters")
        source_id, snapshot_id, detail_chunk = self._published_source(
            project_id=project_id,
            suffix="detail",
            source_kind="product_detail",
            text="Official wood screw dimensions for timber construction.",
            embedding=vector(0.9, 0.1),
            metadata={"product_family": "wood-screws"},
        )
        self._published_source(
            project_id=project_id,
            suffix="blog",
            source_kind="official_blog",
            text="Wood screw timber selection guide.",
            embedding=vector(1.0),
        )
        product_id = f"{self.prefix}-wood-screw"
        self.catalog.upsert_product(
            KnowledgeProduct(
                project_id=project_id,
                product_id=product_id,
                name="Wood Screw",
                canonical_url=f"https://{project_id}/detail/",
            )
        )
        self.catalog.store_source_evidence(
            ProductSourceEvidence(
                project_id=project_id,
                product_id=product_id,
                source_id=source_id,
                snapshot_id=snapshot_id,
                relation="primary_detail",
                confidence=0.9,
                reason="Official detail page.",
            )
        )

        hits = BasicHybridRetriever(self.engine, self.provider).retrieve(
            RetrievalQuery(
                project_id=project_id,
                text="wood screw timber",
                filters={
                    "source_kinds": ("product_detail",),
                    "trust_tiers": ("hard_fact",),
                    "public_source": True,
                    "product_ids": (product_id,),
                    "heading_contains": ("Fasteners",),
                    "chunk_metadata": {"product_family": "wood-screws"},
                    "source_metadata": {"fixture": "detail"},
                },
            )
        )

        self.assertEqual([hit.chunk.chunk_id for hit in hits], [detail_chunk])

    def test_optional_reranker_changes_order_but_cannot_inject_chunks(self) -> None:
        project_id = self._project("rerank")
        _source_a, _snapshot_a, detail_chunk = self._published_source(
            project_id=project_id,
            suffix="detail",
            source_kind="product_detail",
            text="Wood screw timber specification.",
            embedding=vector(0.9, 0.1),
        )
        _source_b, _snapshot_b, blog_chunk = self._published_source(
            project_id=project_id,
            suffix="blog",
            source_kind="official_blog",
            text="Wood screw timber guide.",
            embedding=vector(0.8, 0.2),
        )
        retriever = BasicHybridRetriever(
            self.engine,
            self.provider,
            config=HybridRetrievalConfig(reranker_weight=1.0),
            reranker=BoostReranker(
                {
                    detail_chunk: 0.1,
                    blog_chunk: 1.0,
                }
            ),
        )

        hits = retriever.retrieve(
            RetrievalQuery(
                project_id=project_id,
                text="wood screw timber",
            )
        )

        self.assertEqual(hits[0].chunk.chunk_id, blog_chunk)
        self.assertEqual(hits[0].score, 1.0)
        self.assertEqual(hits[0].explanation["reranker_score"], 1.0)

    def test_migration_installs_the_simple_text_gin_index(self) -> None:
        with self.engine.connect() as connection:
            index = connection.execute(
                sa.text(
                    """
                    SELECT indexdef
                    FROM pg_indexes
                    WHERE schemaname = current_schema()
                      AND indexname = 'ix_knowledge_chunks_search_text'
                    """
                )
            ).scalar_one()
        normalized = str(index).casefold()
        self.assertIn("using gin", normalized)
        self.assertIn("to_tsvector('simple'::regconfig, text)", normalized)


class BasicHybridRetrieverContractTests(unittest.TestCase):
    def test_unknown_filter_is_rejected_before_database_access(self) -> None:
        provider = FakeEmbeddingProvider()
        engine = sa.create_engine("sqlite://")
        retriever = BasicHybridRetriever(engine, provider)

        with self.assertRaisesRegex(ValueError, "unsupported retrieval filters"):
            retriever.retrieve(
                RetrievalQuery(
                    project_id="project-a",
                    text="wood screw",
                    filters={"silently_ignored": True},
                )
            )
        self.assertEqual(provider.calls, [])


if __name__ == "__main__":
    unittest.main()
