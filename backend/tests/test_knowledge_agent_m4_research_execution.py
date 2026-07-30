from __future__ import annotations

import os
import sys
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

from langgraph.checkpoint.postgres import PostgresSaver


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from knowledge_agent import (  # noqa: E402
    CandidateIngestionResult,
    KnowledgeProject,
    PostgresKnowledgeRepository,
    PostgresResearchRunRepository,
    PostgresResearchTelemetry,
    PostgresRetrievalPlanRepository,
    ResearchCandidate,
    ResearchExecutionError,
    ResearchGraphExecutionService,
    ResearchGraphRequest,
    ResearchGraphSessionFactory,
    RetrievalPlan,
    RetrievalScope,
    ScopeEvidenceObservation,
    create_knowledge_engine,
)
from knowledge_agent.checkpoint_setup import psycopg_connection_url  # noqa: E402
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


class FakePlans:
    def __init__(self, scope_id: str) -> None:
        self.scope_id = scope_id

    def scope_ids(self, **kwargs):
        return (self.scope_id,)


class SequencedEvidence:
    def __init__(
        self,
        observations: tuple[ScopeEvidenceObservation, ...],
        *,
        error: Exception | None = None,
    ) -> None:
        self.observations = observations
        self.error = error
        self.calls = 0

    def retrieve_scope(self, **kwargs):
        self.calls += 1
        if self.error is not None:
            raise self.error
        index = min(self.calls - 1, len(self.observations) - 1)
        return self.observations[index]


class FakeDiscovery:
    def __init__(self, candidates: tuple[ResearchCandidate, ...] = ()) -> None:
        self.candidates = candidates

    def discover(self, **kwargs):
        return self.candidates


class FakeIngestion:
    def ingest(self, **kwargs):
        return CandidateIngestionResult(published_source_ids=("source-1",))


def observation(pack_id: str, sufficiency: str) -> ScopeEvidenceObservation:
    return ScopeEvidenceObservation(
        evidence_pack_id=pack_id,
        sufficiency=sufficiency,  # type: ignore[arg-type]
        gap_reasons=(() if sufficiency == "sufficient" else ("missing fact",)),
        chunk_ids=((f"chunk-{pack_id}",) if sufficiency == "sufficient" else ()),
    )


class KnowledgeAgentM4ResearchExecutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        database_url = os.environ.get(DATABASE_URL_ENV, "").strip()
        if not database_url:
            raise unittest.SkipTest(
                f"{DATABASE_URL_ENV} is not set; PostgreSQL integration tests skipped"
            )
        cls.database_url = database_url
        cls.engine = create_knowledge_engine(database_url)
        cls.knowledge = PostgresKnowledgeRepository(cls.engine)
        cls.plans = PostgresRetrievalPlanRepository(cls.engine)
        cls.runs = PostgresResearchRunRepository(cls.engine)

    @classmethod
    def tearDownClass(cls) -> None:
        if hasattr(cls, "engine"):
            cls.engine.dispose()

    def setUp(self) -> None:
        self.prefix = f"m4-execution-{uuid.uuid4().hex}"
        self.project_id = self.prefix
        self.thread_ids: set[str] = set()
        self.knowledge.upsert_project(
            KnowledgeProject(
                project_id=self.project_id,
                customer_name="M4 Execution",
                official_domain=f"{self.prefix}.example.test",
            )
        )
        self.plan_id = f"{self.prefix}-plan"
        self.scope_id = f"{self.prefix}-scope"
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
                    scope_type="h2_section",
                    scope_key="overview",
                    title="Overview",
                    query_variants=("overview",),
                    minimum_hits=1,
                ),
            ),
            created_at=NOW,
        )
        self.plans.save_retrieval_plan(self.plan)

    def tearDown(self) -> None:
        with PostgresSaver.from_conn_string(
            psycopg_connection_url(self.database_url)
        ) as saver:
            for thread_id in self.thread_ids:
                saver.delete_thread(thread_id)
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

    def _request(self, suffix: str) -> ResearchGraphRequest:
        thread_id = f"{self.prefix}-{suffix}"
        self.thread_ids.add(thread_id)
        return ResearchGraphRequest(
            organization_id=f"{self.prefix}-org",
            project_id=self.project_id,
            article_id=self.plan.article_id,
            outline_version=self.plan.outline_version,
            retrieval_plan_id=self.plan_id,
            thread_id=thread_id,
        )

    def _service(
        self,
        *,
        evidence: SequencedEvidence,
        discovery: FakeDiscovery | None = None,
        telemetry: bool = False,
    ) -> ResearchGraphExecutionService:
        return ResearchGraphExecutionService(
            sessions=ResearchGraphSessionFactory(
                database_url=self.database_url,
                plans=FakePlans(self.scope_id),
                evidence=evidence,
                discovery=discovery or FakeDiscovery(),
                ingestion=FakeIngestion(),
                telemetry=(
                    PostgresResearchTelemetry(self.runs)
                    if telemetry
                    else None
                ),
            ),
            runs=self.runs,
        )

    def test_queue_projection_completes_and_records_timeline(self) -> None:
        request = self._request("complete")
        service = self._service(
            evidence=SequencedEvidence((observation("pack-1", "sufficient"),))
        )

        queued = service.enqueue(request, metadata={"task_id": "task-1"})
        completed = service.execute_start(request)

        self.assertEqual(queued.status, "queued")
        self.assertEqual(completed.status, "completed")
        self.assertEqual(completed.evidence_pack_ids, ("pack-1",))
        self.assertEqual(
            [event.event_type for event in self.runs.list_events(
                self.project_id,
                request.thread_id,
            )],
            ["queued", "node_completed", "completed"],
        )

    def test_interrupted_run_resumes_through_new_checkpoint_session(self) -> None:
        request = self._request("review")
        candidate = ResearchCandidate(
            candidate_id="candidate-1",
            url=f"https://{self.prefix}.example.test/product",
            page_type="unknown",
            needs_review=True,
            evidence={"same_site": True},
        )
        service = self._service(
            evidence=SequencedEvidence(
                (
                    observation("before", "weak"),
                    observation("after", "sufficient"),
                )
            ),
            discovery=FakeDiscovery((candidate,)),
        )
        service.enqueue(request)

        waiting = service.execute_start(request)
        resumed = service.execute_resume(
            project_id=self.project_id,
            thread_id=request.thread_id,
            approved_urls=(candidate.url,),
        )

        self.assertEqual(waiting.status, "waiting_for_review")
        self.assertEqual(resumed.status, "completed")
        event_types = [
            event.event_type
            for event in self.runs.list_events(
                self.project_id,
                request.thread_id,
            )
        ]
        self.assertIn("interrupted", event_types)
        self.assertIn("resumed", event_types)
        self.assertEqual(event_types[-1], "completed")

    def test_provider_error_is_sanitized_in_run_queue_and_event(self) -> None:
        request = self._request("failed")
        secret = "sk-never-persist-this"
        service = self._service(
            evidence=SequencedEvidence(
                (observation("unused", "missing"),),
                error=RuntimeError(f"provider rejected {secret}"),
            ),
            telemetry=True,
        )
        service.enqueue(request)

        with self.assertRaisesRegex(
            ResearchExecutionError,
            "^Research execution failed\\.$",
        ) as raised:
            service.execute_start(request)

        run = self.runs.get_run(self.project_id, request.thread_id)
        self.assertEqual(run.status, "failed")  # type: ignore[union-attr]
        persisted = str(run) + str(  # type: ignore[arg-type]
            self.runs.list_events(self.project_id, request.thread_id)
        )
        self.assertNotIn(secret, persisted)
        self.assertNotIn(secret, str(raised.exception))
        retry_events = [
            event
            for event in self.runs.list_events(
                self.project_id,
                request.thread_id,
            )
            if event.event_type == "tool_call"
            and event.node_name == "retrieve_knowledge"
        ]
        self.assertEqual([event.attempt for event in retry_events], [1, 2, 3])
        self.assertTrue(
            all(event.details["duration_ms"] >= 0 for event in retry_events)
        )
        self.assertTrue(
            all(event.details["error_code"] == "RuntimeError" for event in retry_events)
        )


if __name__ == "__main__":
    unittest.main()
