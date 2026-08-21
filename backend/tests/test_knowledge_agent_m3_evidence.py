from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from knowledge_agent import (  # noqa: E402
    DefaultEvidencePackBuilder,
    EvidenceLink,
    EvidencePackRequest,
    HardFactSentenceTarget,
    KnowledgeChunk,
    SentenceEvidenceTarget,
    RetrievalHit,
    RetrievalPlan,
    RetrievalProvenance,
    RetrievalScope,
    calculate_knowledge_coverage,
)
from knowledge_agent.retrieval_plan_generation import (  # noqa: E402
    generate_retrieval_plan,
)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def hit(
    chunk_id: str,
    *,
    source_id: str,
    trust_tier: str = "reference_material",
    public: bool = False,
    source_kind: str = "knowledge_page",
) -> RetrievalHit:
    snapshot_id = f"snap-{source_id}"
    return RetrievalHit(
        chunk=KnowledgeChunk(
            project_id="project-a",
            chunk_id=chunk_id,
            source_id=source_id,
            snapshot_id=snapshot_id,
            text=f"Evidence {chunk_id}",
        ),
        score=0.8,
        provenance=RetrievalProvenance(
            project_id="project-a",
            source_id=source_id,
            snapshot_id=snapshot_id,
            display_name=f"Source {source_id}",
            source_kind=source_kind,  # type: ignore[arg-type]
            trust_tier=trust_tier,  # type: ignore[arg-type]
            public_source=public,
            canonical_url=f"https://example.test/{source_id}" if public else None,
        ),
    )


class RetrievalPlanContractTests(unittest.TestCase):
    def test_plan_binds_scopes_to_outline_and_project(self) -> None:
        scope = RetrievalScope(
            project_id="project-a",
            retrieval_plan_id="plan-1",
            scope_id="scope-1",
            ordinal=0,
            scope_type="h2_section",
            scope_key="materials",
            title="Materials",
            query_variants=("fastener materials",),
        )
        plan = RetrievalPlan(
            project_id="project-a",
            retrieval_plan_id="plan-1",
            article_id="article-1",
            outline_version=3,
            scopes=(scope,),
        )

        request = plan.scopes[0].evidence_request(
            article_id=plan.article_id,
            outline_version=plan.outline_version,
        )
        self.assertEqual(request.retrieval_plan_id, "plan-1")
        self.assertEqual(request.scope_id, "scope-1")
        self.assertEqual(request.outline_version, 3)

    def test_plan_rejects_foreign_scope_and_more_than_two_gap_rounds(self) -> None:
        foreign_scope = RetrievalScope(
            project_id="project-b",
            retrieval_plan_id="plan-1",
            scope_id="scope-1",
            ordinal=0,
            scope_type="faq",
            scope_key="faq",
            title="FAQ",
            query_variants=("faq",),
        )
        with self.assertRaisesRegex(ValueError, "same plan"):
            RetrievalPlan(
                project_id="project-a",
                retrieval_plan_id="plan-1",
                article_id="article-1",
                outline_version=1,
                scopes=(foreign_scope,),
            )
        with self.assertRaisesRegex(ValueError, "between 0 and 2"):
            RetrievalPlan(
                project_id="project-b",
                retrieval_plan_id="plan-1",
                article_id="article-1",
                outline_version=1,
                scopes=(foreign_scope,),
                max_gap_fill_rounds=3,
            )


class RetrievalPlanGenerationTests(unittest.TestCase):
    def _plan(self, **overrides):
        payload = {
            "project_id": "project-a",
            "article_id": "topic_001",
            "task_id": "task-a",
            "outline_version": 1,
            "outline": "## Materials\n\n## Installation",
            "topic": "Commercial flooring selection",
            "products": (
                {
                    "name": "OakShield Pro",
                    "url": "https://example.test/products/oakshield-pro",
                },
            ),
        }
        payload.update(overrides)
        return generate_retrieval_plan(**payload)

    def test_plan_identity_is_stable_for_same_content(self) -> None:
        first = self._plan()
        second = self._plan()

        self.assertEqual(first.retrieval_plan_id, second.retrieval_plan_id)
        self.assertEqual(
            first.metadata["content_fingerprint"],
            second.metadata["content_fingerprint"],
        )

    def test_plan_identity_changes_when_snapshot_content_changes(self) -> None:
        first = self._plan()

        changed_outline = self._plan(
            outline="## Materials\n\n## Maintenance",
        )
        changed_topic = self._plan(topic="Hospitality flooring procurement")
        changed_product = self._plan(
            products=(
                {
                    "name": "OakShield Pro",
                    "url": "https://example.test/products/oakshield-pro-v2",
                },
            ),
        )
        changed_task = self._plan(task_id="task-b")

        self.assertNotEqual(
            first.retrieval_plan_id,
            changed_outline.retrieval_plan_id,
        )
        self.assertNotEqual(
            first.retrieval_plan_id,
            changed_topic.retrieval_plan_id,
        )
        self.assertNotEqual(
            first.retrieval_plan_id,
            changed_product.retrieval_plan_id,
        )
        self.assertNotEqual(
            first.retrieval_plan_id,
            changed_task.retrieval_plan_id,
        )

    def test_plan_tracks_h3_claims_and_product_ids(self) -> None:
        plan = self._plan(
            outline=(
                "## Compare Storage Options\n"
                "### Capacity and Power Specifications\n"
                "### Procurement Selection Logic\n"
                "## FAQ\n"
                "### What should buyers confirm?"
            ),
            products=(
                {
                    "product_id": "product-a",
                    "name": "OakShield Pro",
                    "url": "https://example.test/products/oakshield-pro",
                    "article_role": "primary_solution",
                },
            ),
            article_brief={
                "brief_id": "brief-a",
                "input_hash": "a" * 64,
                "knowledge_snapshot_fingerprint": "b" * 64,
            },
        )
        section = plan.scopes[0]
        requirements = section.metadata["claim_requirements"]
        self.assertEqual(len(requirements), 2)
        self.assertTrue(requirements[0]["require_hard_fact"])
        self.assertEqual(
            next(scope for scope in plan.scopes if scope.scope_key == "faq").scope_type,
            "faq",
        )
        product_scope = next(
            scope for scope in plan.scopes if scope.scope_type == "product_fact"
        )
        self.assertEqual(product_scope.filters["product_ids"], ["product-a"])
        self.assertEqual(plan.metadata["article_brief"]["brief_id"], "brief-a")
        self.assertFalse(plan.metadata["product_coverage"][0]["mentioned_in_outline"])


class EvidencePackBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.request = EvidencePackRequest(
            project_id="project-a",
            article_id="article-1",
            outline_version=1,
            scope_type="product_fact",
            scope_key="dimensions",
            query_variants=("product dimensions",),
            retrieval_plan_id="plan-1",
            scope_id="scope-1",
        )

    def test_builder_is_deterministic_and_carries_provenance(self) -> None:
        hits = (
            hit(
                "snap-source-1:0",
                source_id="source-1",
                trust_tier="hard_fact",
                public=True,
            ),
            hit("snap-source-2:0", source_id="source-2"),
        )
        builder = DefaultEvidencePackBuilder(
            minimum_hits=2,
            minimum_distinct_sources=2,
            require_hard_fact=True,
        )

        first = builder.build(self.request, hits)
        second = builder.build(self.request, hits)

        self.assertEqual(first.evidence_pack_id, second.evidence_pack_id)
        self.assertEqual(first.sufficiency, "sufficient")
        self.assertEqual(first.hard_fact_chunk_ids, ("snap-source-1:0",))
        self.assertEqual(
            first.public_citation_urls,
            ("https://example.test/source-1",),
        )

    def test_builder_distinguishes_missing_and_weak(self) -> None:
        builder = DefaultEvidencePackBuilder(
            minimum_hits=2,
            minimum_distinct_sources=2,
            require_hard_fact=True,
        )

        self.assertEqual(builder.build(self.request, ()).sufficiency, "missing")
        weak = builder.build(
            self.request,
            (hit("snap-source-1:0", source_id="source-1"),),
        )
        self.assertEqual(weak.sufficiency, "weak")
        self.assertEqual(len(weak.gap_reasons), 3)

    def test_builder_excludes_official_blog_from_evidence(self) -> None:
        blog_hit = hit(
            "snap-blog:0",
            source_id="blog",
            public=True,
            source_kind="official_blog",
        )

        pack = DefaultEvidencePackBuilder(minimum_hits=1).build(
            self.request,
            (blog_hit,),
        )

        self.assertEqual(pack.hits, ())
        self.assertEqual(pack.sufficiency, "missing")
        self.assertEqual(pack.public_citation_urls, ())


class KnowledgeCoverageTests(unittest.TestCase):
    def test_coverage_ignores_short_stale_and_non_sentence_fact_links(self) -> None:
        first_hash = digest("paragraph one")
        second_hash = digest("paragraph two")
        first_sentence_hash = digest("sentence one")
        second_sentence_hash = digest("sentence two")
        links = (
            EvidenceLink(
                project_id="project-a",
                evidence_link_id="link-1",
                article_id="article-1",
                paragraph_id="p1",
                sentence_id="s1",
                paragraph_hash=first_hash,
                chunk_id="snapshot:0",
                support_scope="sentence",
                metadata={"sentence_hash": first_sentence_hash},
            ),
            EvidenceLink(
                project_id="project-a",
                evidence_link_id="link-2",
                article_id="article-1",
                paragraph_id="p2",
                sentence_id="s2",
                paragraph_hash=digest("old paragraph two"),
                chunk_id="snapshot:1",
                support_scope="sentence",
                metadata={"sentence_hash": digest("old sentence two")},
            ),
            EvidenceLink(
                project_id="project-a",
                evidence_link_id="link-3",
                article_id="article-1",
                paragraph_id="p1",
                sentence_id="s1",
                paragraph_hash=first_hash,
                chunk_id="snapshot:0",
                support_scope="sentence",
                claim_type="hard_fact",
                metadata={"sentence_hash": first_sentence_hash},
            ),
        )

        report = calculate_knowledge_coverage(
            project_id="project-a",
            article_id="article-1",
            sentences=(
                SentenceEvidenceTarget(
                    "p1", "s1", first_hash, first_sentence_hash, 10
                ),
                SentenceEvidenceTarget(
                    "p2", "s2", second_hash, second_sentence_hash, 12
                ),
                SentenceEvidenceTarget(
                    "short",
                    "short-sentence",
                    digest("short"),
                    digest("short sentence"),
                    4,
                ),
            ),
            hard_fact_sentences=(
                HardFactSentenceTarget(
                    "p1", "s1", first_hash, first_sentence_hash
                ),
                HardFactSentenceTarget(
                    "p2", "s2", second_hash, second_sentence_hash
                ),
            ),
            links=links,
        )

        self.assertEqual(report.eligible_sentences, 2)
        self.assertEqual(report.supported_sentences, 1)
        self.assertEqual(report.sentence_coverage, 0.5)
        self.assertEqual(report.hard_fact_coverage, 0.5)

    def test_hard_fact_requires_sentence_level_link(self) -> None:
        with self.assertRaisesRegex(ValueError, "sentence-level"):
            EvidenceLink(
                project_id="project-a",
                evidence_link_id="link-1",
                article_id="article-1",
                paragraph_id="p1",
                paragraph_hash=digest("paragraph"),
                chunk_id="snapshot:0",
                claim_type="hard_fact",
            )


if __name__ == "__main__":
    unittest.main()
