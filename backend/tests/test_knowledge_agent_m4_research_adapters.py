from __future__ import annotations

import os
import sys
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from knowledge_agent import (  # noqa: E402
    KnowledgeProject,
    KnowledgeSource,
    PostgresKnowledgeRepository,
    PostgresResearchRunRepository,
    PostgresRetrievalPlanRepository,
    ResearchGraphRequest,
    RetrievalPlan,
    RetrievalScope,
    SourceSnapshot,
    create_knowledge_engine,
)
from knowledge_agent.research_adapters import (  # noqa: E402
    OfficialCandidateIngestionAdapter,
    PostgresProjectDirectory,
    TavilyOfficialDiscoveryAdapter,
)
from knowledge_agent.schema import (  # noqa: E402
    gap_fill_attempts,
    projects,
    research_graph_events,
    research_graph_runs,
    retrieval_plans,
    retrieval_scopes,
)


DATABASE_URL_ENV = "ARTICLE_AGENT_DATABASE_URL"
NOW = datetime(2026, 7, 30, tzinfo=timezone.utc)


class FakeSearch:
    def __init__(self, results: tuple[SimpleNamespace, ...]) -> None:
        self.results = results
        self.calls: list[tuple[str, str, int]] = []

    def search(self, query: str, host: str, max_results: int = 5):
        self.calls.append((query, host, max_results))
        return SimpleNamespace(results=self.results, request_id="request-1")


class FakeSourceRepository:
    def __init__(self) -> None:
        self.sources: dict[tuple[str, str], KnowledgeSource] = {}

    def upsert_source(self, source: KnowledgeSource) -> None:
        self.sources[(source.project_id, source.source_id)] = source


class FakeLibrary:
    def __init__(self, repository: FakeSourceRepository) -> None:
        self.repository = repository

    def get_source(self, project_id: str, source_id: str) -> KnowledgeSource | None:
        return self.repository.sources.get((project_id, source_id))


class FakeWebIngestion:
    def __init__(
        self,
        repository: FakeSourceRepository,
        *,
        confidence: float,
    ) -> None:
        self.repository = repository
        self.confidence = confidence
        self.calls = 0

    def ingest_url(
        self,
        *,
        project_id: str,
        site_url: str,
        url: str,
        metadata: dict[str, object],
    ):
        self.calls += 1
        source = KnowledgeSource(
            project_id=project_id,
            source_id="source-1",
            display_name="Official Product",
            source_kind="product_detail",
            trust_tier="hard_fact",
            canonical_url=url,
            public_source=True,
            metadata=metadata,
        )
        self.repository.upsert_source(source)
        snapshot = SourceSnapshot(
            project_id=project_id,
            source_id=source.source_id,
            snapshot_id="snapshot-1",
            content_hash="a" * 64,
            fetched_at=NOW,
            parser_name="test",
            parser_version="1",
        )
        return SimpleNamespace(
            source=source,
            snapshot=snapshot,
            classification=SimpleNamespace(confidence=self.confidence),
        )


class FakePublication:
    def __init__(self, repository: FakeSourceRepository) -> None:
        self.repository = repository
        self.calls = 0

    def publish(
        self,
        *,
        project_id: str,
        source_id: str,
        snapshot_id: str,
    ):
        self.calls += 1
        return SimpleNamespace(source_id=source_id)


class KnowledgeAgentM4ResearchAdapterTests(unittest.TestCase):
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
        cls.runs = PostgresResearchRunRepository(cls.engine)
        cls.projects = PostgresProjectDirectory(cls.engine)

    @classmethod
    def tearDownClass(cls) -> None:
        if hasattr(cls, "engine"):
            cls.engine.dispose()

    def setUp(self) -> None:
        self.prefix = f"m4-adapter-{uuid.uuid4().hex}"
        self.project_id = self.prefix
        self.plan_id = f"{self.prefix}-plan"
        self.scope_id = f"{self.prefix}-scope"
        self.thread_id = f"{self.prefix}-thread"
        self.attempt_id = f"{self.prefix}-attempt"
        self.knowledge.upsert_project(
            KnowledgeProject(
                project_id=self.project_id,
                customer_name="M4 Adapter",
                official_domain="example.test",
            )
        )
        self.plan = RetrievalPlan(
            project_id=self.project_id,
            retrieval_plan_id=self.plan_id,
            article_id=f"{self.prefix}-article",
            outline_version=1,
            scopes=(
                RetrievalScope(
                    project_id=self.project_id,
                    retrieval_plan_id=self.plan_id,
                    scope_id=self.scope_id,
                    ordinal=0,
                    scope_type="product_fact",
                    scope_key="dimensions",
                    title="Product dimensions",
                    query_variants=("fastener dimensions",),
                    minimum_hits=1,
                ),
            ),
            created_at=NOW,
        )
        self.plans.save_retrieval_plan(self.plan)
        self.request = ResearchGraphRequest(
            organization_id=f"{self.prefix}-org",
            project_id=self.project_id,
            article_id=self.plan.article_id,
            outline_version=self.plan.outline_version,
            retrieval_plan_id=self.plan_id,
            thread_id=self.thread_id,
        )
        self.runs.create_run(self.request)

    def tearDown(self) -> None:
        with self.engine.begin() as connection:
            for table in (
                gap_fill_attempts,
                research_graph_events,
                research_graph_runs,
                retrieval_scopes,
                retrieval_plans,
                projects,
            ):
                connection.execute(
                    table.delete().where(table.c.project_id == self.project_id)
                )

    def _discover(self, search: FakeSearch):
        adapter = TavilyOfficialDiscoveryAdapter(
            projects=self.projects,
            plans=self.plans,
            search=search,
            attempts=self.runs,
        )
        candidates = adapter.discover(
            project_id=self.project_id,
            thread_id=self.thread_id,
            article_id=self.plan.article_id,
            retrieval_plan_id=self.plan_id,
            scope_id=self.scope_id,
            round_number=1,
            gap_reasons=("missing hard fact",),
            attempt_id=self.attempt_id,
        )
        return adapter, candidates

    def test_discovery_keeps_same_site_urls_and_reuses_attempt_receipt(self) -> None:
        search = FakeSearch(
            (
                SimpleNamespace(
                    url="https://example.test/products/a",
                    score=0.9,
                ),
                SimpleNamespace(
                    url="https://evil.test/copied-page",
                    score=0.99,
                ),
                SimpleNamespace(
                    url="https://example.test/products/a",
                    score=0.7,
                ),
            )
        )
        adapter, first = self._discover(search)
        second = adapter.discover(
            project_id=self.project_id,
            thread_id=self.thread_id,
            article_id=self.plan.article_id,
            retrieval_plan_id=self.plan_id,
            scope_id=self.scope_id,
            round_number=1,
            gap_reasons=("missing hard fact",),
            attempt_id=self.attempt_id,
        )

        self.assertEqual([item.url for item in first], [
            "https://example.test/products/a",
        ])
        self.assertTrue(first[0].needs_review)
        self.assertEqual([item.url for item in second], [first[0].url])
        self.assertEqual(len(search.calls), 1)
        self.assertNotIn("copied-page", str(self.runs.get_gap_attempt_by_id(
            self.attempt_id
        )))

    def test_approved_high_confidence_candidate_publishes_once(self) -> None:
        _, candidates = self._discover(
            FakeSearch(
                (
                    SimpleNamespace(
                        url="https://example.test/products/a",
                        score=0.9,
                    ),
                )
            )
        )
        repository = FakeSourceRepository()
        web = FakeWebIngestion(repository, confidence=0.91)
        publication = FakePublication(repository)
        authorization_checks: list[str] = []
        adapter = OfficialCandidateIngestionAdapter(
            projects=self.projects,
            web_ingestion=web,  # type: ignore[arg-type]
            repository=repository,  # type: ignore[arg-type]
            library=FakeLibrary(repository),  # type: ignore[arg-type]
            publication=publication,  # type: ignore[arg-type]
            attempts=self.runs,
            authorize_candidate=lambda: authorization_checks.append(
                "checked"
            ),
        )

        first = adapter.ingest(
            project_id=self.project_id,
            thread_id=self.thread_id,
            retrieval_plan_id=self.plan_id,
            scope_id=self.scope_id,
            round_number=1,
            candidates=candidates,
            approved_urls=(candidates[0].url,),
            attempt_id=self.attempt_id,
        )
        retried = adapter.ingest(
            project_id=self.project_id,
            thread_id=self.thread_id,
            retrieval_plan_id=self.plan_id,
            scope_id=self.scope_id,
            round_number=1,
            candidates=candidates,
            approved_urls=(candidates[0].url,),
            attempt_id=self.attempt_id,
        )

        self.assertEqual(first.published_source_ids, ("source-1",))
        self.assertEqual(retried.published_source_ids, ("source-1",))
        self.assertEqual(web.calls, 1)
        self.assertEqual(publication.calls, 1)
        self.assertEqual(authorization_checks, ["checked"])
        self.assertEqual(
            self.runs.get_gap_attempt_by_id(self.attempt_id).result,  # type: ignore[union-attr]
            "improved",
        )

    def test_low_confidence_candidate_stays_in_review_inbox(self) -> None:
        _, candidates = self._discover(
            FakeSearch(
                (
                    SimpleNamespace(
                        url="https://example.test/uncertain",
                        score=0.5,
                    ),
                )
            )
        )
        repository = FakeSourceRepository()
        publication = FakePublication(repository)
        adapter = OfficialCandidateIngestionAdapter(
            projects=self.projects,
            web_ingestion=FakeWebIngestion(
                repository,
                confidence=0.55,
            ),  # type: ignore[arg-type]
            repository=repository,  # type: ignore[arg-type]
            library=FakeLibrary(repository),  # type: ignore[arg-type]
            publication=publication,  # type: ignore[arg-type]
            attempts=self.runs,
        )

        result = adapter.ingest(
            project_id=self.project_id,
            thread_id=self.thread_id,
            retrieval_plan_id=self.plan_id,
            scope_id=self.scope_id,
            round_number=1,
            candidates=candidates,
            approved_urls=(candidates[0].url,),
            attempt_id=self.attempt_id,
        )

        self.assertEqual(result.published_source_ids, ())
        self.assertEqual(
            result.needs_review_candidate_ids,
            (candidates[0].candidate_id,),
        )
        self.assertEqual(publication.calls, 0)
        self.assertEqual(
            repository.sources[(self.project_id, "source-1")].status,
            "needs_review",
        )
        self.assertEqual(
            self.runs.get_gap_attempt_by_id(self.attempt_id).result,  # type: ignore[union-attr]
            "blocked",
        )


if __name__ == "__main__":
    unittest.main()
