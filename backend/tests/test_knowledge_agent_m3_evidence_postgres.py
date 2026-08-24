from __future__ import annotations

import hashlib
import os
import sys
import unittest
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sqlalchemy as sa


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from knowledge_agent import (  # noqa: E402
    EMBEDDING_DIMENSIONS,
    ChunkEmbedding,
    DefaultEvidencePackBuilder,
    EvidenceConflictError,
    EvidenceLink,
    EvidenceTargetError,
    KnowledgeChunk,
    KnowledgeProject,
    KnowledgeSource,
    PostgresEvidenceLinkRepository,
    PostgresEvidencePackRepository,
    PostgresKnowledgeRepository,
    PostgresRetrievalPlanRepository,
    RetrievalHit,
    RetrievalPlan,
    RetrievalProvenance,
    RetrievalScope,
    SourceSnapshot,
    create_knowledge_engine,
)
from knowledge_agent.schema import (  # noqa: E402
    evidence_links,
    evidence_pack_hits,
    evidence_packs,
    knowledge_chunks,
    knowledge_sources,
    projects,
    retrieval_plans,
    retrieval_scopes,
    source_snapshots,
)


DATABASE_URL_ENV = "ARTICLE_AGENT_DATABASE_URL"
MODEL_ID = "m3-evidence-test-model"
NOW = datetime(2026, 7, 30, tzinfo=timezone.utc)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def vector() -> tuple[float, ...]:
    return (1.0,) + (0.0,) * (EMBEDDING_DIMENSIONS - 1)


class KnowledgeAgentM3EvidencePostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        database_url = os.environ.get(DATABASE_URL_ENV, "").strip()
        if not database_url:
            raise unittest.SkipTest(
                f"{DATABASE_URL_ENV} is not set; PostgreSQL integration tests skipped"
            )
        cls.engine = create_knowledge_engine(database_url)
        cls.knowledge = PostgresKnowledgeRepository(cls.engine)
        cls.plans = PostgresRetrievalPlanRepository(cls.engine)
        cls.packs = PostgresEvidencePackRepository(cls.engine)
        cls.links = PostgresEvidenceLinkRepository(cls.engine)

    @classmethod
    def tearDownClass(cls) -> None:
        if hasattr(cls, "engine"):
            cls.engine.dispose()

    def setUp(self) -> None:
        self.prefix = f"m3-evidence-{uuid.uuid4().hex}"
        self.project_ids: set[str] = set()

    def tearDown(self) -> None:
        if not self.project_ids:
            return
        project_ids = tuple(self.project_ids)
        with self.engine.begin() as connection:
            for table in (
                evidence_links,
                evidence_pack_hits,
                evidence_packs,
                retrieval_scopes,
                retrieval_plans,
                knowledge_chunks,
                source_snapshots,
                knowledge_sources,
                projects,
            ):
                connection.execute(
                    table.delete().where(table.c.project_id.in_(project_ids))
                )

    def _project(self, suffix: str = "a") -> str:
        project_id = f"{self.prefix}-{suffix}"
        self.project_ids.add(project_id)
        self.knowledge.upsert_project(
            KnowledgeProject(
                project_id=project_id,
                customer_name="M3 Evidence",
                official_domain=f"{suffix}.{self.prefix}.example.test",
            )
        )
        return project_id

    def _published_chunk(
        self,
        project_id: str,
        source_suffix: str,
        snapshot_suffix: str,
        *,
        trust_tier: str = "hard_fact",
        public: bool = True,
        source_kind: str = "knowledge_page",
    ) -> tuple[str, RetrievalHit]:
        source_id = f"{self.prefix}-{source_suffix}"
        snapshot_id = f"{self.prefix}-{snapshot_suffix}"
        chunk_id = f"{snapshot_id}:0"
        canonical_url = (
            f"https://{source_suffix}.{self.prefix}.example.test/page"
            if public
            else None
        )
        if not self._source_exists(project_id, source_id):
            self.knowledge.upsert_source(
                KnowledgeSource(
                    project_id=project_id,
                    source_id=source_id,
                    display_name=f"Source {source_suffix}",
                    source_kind=source_kind,  # type: ignore[arg-type]
                    trust_tier=trust_tier,  # type: ignore[arg-type]
                    public_source=public,
                    canonical_url=canonical_url,
                )
            )
        snapshot = SourceSnapshot(
            project_id=project_id,
            source_id=source_id,
            snapshot_id=snapshot_id,
            content_hash=digest(snapshot_id),
            fetched_at=NOW,
            parser_name="test",
            parser_version="1",
        )
        chunk = KnowledgeChunk(
            project_id=project_id,
            source_id=source_id,
            snapshot_id=snapshot_id,
            chunk_id=chunk_id,
            text=f"Evidence for {snapshot_suffix}",
        )
        self.knowledge.store_snapshot(project_id, snapshot, (chunk,))
        self.knowledge.store_embeddings(
            project_id,
            (
                ChunkEmbedding(
                    project_id=project_id,
                    chunk_id=chunk_id,
                    snapshot_id=snapshot_id,
                    embedding_model=MODEL_ID,
                    vector=vector(),
                ),
            ),
        )
        self.knowledge.activate_snapshot(
            project_id,
            source_id,
            snapshot_id,
            MODEL_ID,
        )
        return chunk_id, RetrievalHit(
            chunk=chunk,
            score=0.9,
            provenance=RetrievalProvenance(
                project_id=project_id,
                source_id=source_id,
                snapshot_id=snapshot_id,
                display_name=f"Source {source_suffix}",
                source_kind=source_kind,  # type: ignore[arg-type]
                trust_tier=trust_tier,  # type: ignore[arg-type]
                public_source=public,
                canonical_url=canonical_url,
                fetched_at=NOW,
            ),
            explanation={"rrf": 0.9},
        )

    def _source_exists(self, project_id: str, source_id: str) -> bool:
        with self.engine.connect() as connection:
            return (
                connection.execute(
                    sa.select(sa.literal(True)).where(
                        sa.exists().where(
                            knowledge_sources.c.project_id == project_id,
                            knowledge_sources.c.source_id == source_id,
                        )
                    )
                ).scalar_one_or_none()
                is not None
            )

    def _plan(self, project_id: str) -> RetrievalPlan:
        plan_id = f"{self.prefix}-plan"
        return RetrievalPlan(
            project_id=project_id,
            retrieval_plan_id=plan_id,
            article_id=f"{self.prefix}-article",
            outline_version=2,
            scopes=(
                RetrievalScope(
                    project_id=project_id,
                    retrieval_plan_id=plan_id,
                    scope_id=f"{self.prefix}-scope",
                    ordinal=0,
                    scope_type="product_fact",
                    scope_key="dimensions",
                    title="Dimensions",
                    query_variants=("fastener dimensions",),
                    minimum_hits=1,
                    require_hard_fact=True,
                ),
            ),
            created_at=NOW,
        )

    def test_plan_and_pack_are_idempotent_and_round_trip(self) -> None:
        project_id = self._project()
        _, retrieval_hit = self._published_chunk(
            project_id,
            "official",
            "snapshot-1",
        )
        plan = self._plan(project_id)
        self.plans.save_retrieval_plan(plan)
        self.plans.save_retrieval_plan(plan)
        self.assertEqual(self.plans.get_retrieval_plan(project_id, plan.retrieval_plan_id), plan)

        request = plan.scopes[0].evidence_request(
            article_id=plan.article_id,
            outline_version=plan.outline_version,
        )
        builder = DefaultEvidencePackBuilder(
            minimum_hits=1,
            require_hard_fact=True,
        )
        pack = builder.build(request, (retrieval_hit,))
        self.packs.save_evidence_pack(pack)
        retry_hit = replace(
            retrieval_hit,
            score=retrieval_hit.score + 0.00001,
            explanation={
                **dict(retrieval_hit.explanation),
                "vector_similarity": 0.90003,
            },
        )
        rebuilt = replace(
            builder.build(request, (retry_hit,)),
            created_at=pack.created_at + timedelta(seconds=1),
        )
        self.packs.save_evidence_pack(rebuilt)

        self.assertEqual(
            self.packs.get_evidence_pack(project_id, pack.evidence_pack_id),
            pack,
        )

    def test_distinct_plans_can_share_one_outline_version(self) -> None:
        project_id = self._project()
        first = self._plan(project_id)
        second_plan_id = f"{self.prefix}-plan-rewritten"
        second = replace(
            first,
            retrieval_plan_id=second_plan_id,
            scopes=(
                replace(
                    first.scopes[0],
                    retrieval_plan_id=second_plan_id,
                    scope_id=f"{self.prefix}-scope-rewritten",
                    title="Rewritten dimensions",
                ),
            ),
            metadata={"content_fingerprint": "rewritten"},
        )

        self.plans.save_retrieval_plan(first)
        self.plans.save_retrieval_plan(second)

        self.assertEqual(
            self.plans.get_retrieval_plan(
                project_id,
                first.retrieval_plan_id,
            ),
            first,
        )
        self.assertEqual(
            self.plans.get_retrieval_plan(
                project_id,
                second.retrieval_plan_id,
            ),
            second,
        )

    def test_plan_conflict_and_outline_mismatch_are_rejected(self) -> None:
        project_id = self._project()
        _, retrieval_hit = self._published_chunk(
            project_id,
            "official",
            "snapshot-1",
        )
        plan = self._plan(project_id)
        self.plans.save_retrieval_plan(plan)
        conflicting_scope = replace(plan.scopes[0], title="Changed title")
        with self.assertRaises(EvidenceConflictError):
            self.plans.save_retrieval_plan(replace(plan, scopes=(conflicting_scope,)))

        mismatched_request = replace(
            plan.scopes[0].evidence_request(
                article_id=plan.article_id,
                outline_version=plan.outline_version,
            ),
            outline_version=3,
        )
        pack = DefaultEvidencePackBuilder(minimum_hits=1).build(
            mismatched_request,
            (retrieval_hit,),
        )
        with self.assertRaises(EvidenceTargetError):
            self.packs.save_evidence_pack(pack)

    def test_link_requires_current_published_chunk_and_marks_stale_hash(self) -> None:
        project_id = self._project()
        old_chunk_id, old_hit = self._published_chunk(
            project_id,
            "official",
            "snapshot-old",
        )
        current_chunk_id, current_hit = self._published_chunk(
            project_id,
            "official",
            "snapshot-current",
        )
        paragraph_hash = digest("current paragraph")
        old_link = EvidenceLink(
            project_id=project_id,
            evidence_link_id=f"{self.prefix}-old-link",
            article_id=f"{self.prefix}-article",
            paragraph_id="p1",
            paragraph_hash=paragraph_hash,
            chunk_id=old_chunk_id,
            public_citation_url=old_hit.provenance.canonical_url,  # type: ignore[union-attr]
        )
        with self.assertRaisesRegex(EvidenceTargetError, "current published"):
            self.links.save_evidence_link(old_link)

        link = replace(
            old_link,
            evidence_link_id=f"{self.prefix}-current-link",
            chunk_id=current_chunk_id,
            public_citation_url=current_hit.provenance.canonical_url,  # type: ignore[union-attr]
        )
        self.links.save_evidence_link(link)
        self.links.save_evidence_link(link)
        changed = self.links.mark_paragraph_links_for_review(
            project_id,
            link.article_id,
            link.paragraph_id,
            digest("edited paragraph"),
        )
        self.assertEqual(changed, 1)
        self.assertEqual(
            self.links.list_evidence_links(project_id, link.article_id)[0].validation_status,
            "needs_review",
        )

    def test_hard_fact_link_rejects_reference_material(self) -> None:
        project_id = self._project()
        chunk_id, retrieval_hit = self._published_chunk(
            project_id,
            "reference",
            "snapshot-reference",
            trust_tier="reference_material",
        )
        with self.assertRaisesRegex(EvidenceTargetError, "hard_fact source"):
            self.links.save_evidence_link(
                EvidenceLink(
                    project_id=project_id,
                    evidence_link_id=f"{self.prefix}-fact-link",
                    article_id=f"{self.prefix}-article",
                    paragraph_id="p1",
                    sentence_id="s1",
                    paragraph_hash=digest("product is 20 mm"),
                    chunk_id=chunk_id,
                    support_scope="sentence",
                    claim_type="hard_fact",
                    public_citation_url=retrieval_hit.provenance.canonical_url,  # type: ignore[union-attr]
                )
            )

    def test_official_blog_cannot_be_saved_as_evidence_link(self) -> None:
        project_id = self._project()
        chunk_id, retrieval_hit = self._published_chunk(
            project_id,
            "blog",
            "snapshot-blog",
            trust_tier="reference_material",
            source_kind="official_blog",
        )

        with self.assertRaisesRegex(EvidenceTargetError, "writing references"):
            self.links.save_evidence_link(
                EvidenceLink(
                    project_id=project_id,
                    evidence_link_id=f"{self.prefix}-blog-link",
                    article_id=f"{self.prefix}-article",
                    paragraph_id="p1",
                    paragraph_hash=digest("editorial reference"),
                    chunk_id=chunk_id,
                    public_citation_url=(
                        retrieval_hit.provenance.canonical_url  # type: ignore[union-attr]
                    ),
                )
            )


if __name__ == "__main__":
    unittest.main()
