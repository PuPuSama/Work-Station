from __future__ import annotations

import os
import sys
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from knowledge_agent import (  # noqa: E402
    GapFillAttempt,
    KnowledgeProject,
    PostgresKnowledgeRepository,
    PostgresResearchRunRepository,
    PostgresRetrievalPlanRepository,
    ResearchGraphRequest,
    ResearchRunConflictError,
    RetrievalPlan,
    RetrievalScope,
    create_knowledge_engine,
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


class KnowledgeAgentM4ResearchRunPostgresTests(unittest.TestCase):
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

    @classmethod
    def tearDownClass(cls) -> None:
        if hasattr(cls, "engine"):
            cls.engine.dispose()

    def setUp(self) -> None:
        self.prefix = f"m4-run-{uuid.uuid4().hex}"
        self.project_ids: set[str] = set()

    def tearDown(self) -> None:
        if not self.project_ids:
            return
        project_ids = tuple(self.project_ids)
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
                    table.delete().where(table.c.project_id.in_(project_ids))
                )

    def _plan(self, suffix: str = "a") -> RetrievalPlan:
        project_id = f"{self.prefix}-{suffix}"
        self.project_ids.add(project_id)
        self.knowledge.upsert_project(
            KnowledgeProject(
                project_id=project_id,
                customer_name="M4 Research Run",
                official_domain=f"{suffix}.{self.prefix}.example.test",
            )
        )
        plan_id = f"{project_id}-plan"
        plan = RetrievalPlan(
            project_id=project_id,
            retrieval_plan_id=plan_id,
            article_id=f"{project_id}-article",
            outline_version=3,
            scopes=(
                RetrievalScope(
                    project_id=project_id,
                    retrieval_plan_id=plan_id,
                    scope_id=f"{project_id}-scope",
                    ordinal=0,
                    scope_type="product_fact",
                    scope_key="dimensions",
                    title="Dimensions",
                    query_variants=("product dimensions",),
                    minimum_hits=1,
                    require_hard_fact=True,
                ),
            ),
            created_at=NOW,
        )
        self.plans.save_retrieval_plan(plan)
        return plan

    def _request(
        self,
        plan: RetrievalPlan,
        *,
        thread_id: str | None = None,
    ) -> ResearchGraphRequest:
        return ResearchGraphRequest(
            organization_id=f"{self.prefix}-org",
            project_id=plan.project_id,
            article_id=plan.article_id,
            outline_version=plan.outline_version,
            retrieval_plan_id=plan.retrieval_plan_id,
            thread_id=thread_id or f"{self.prefix}-thread",
        )

    @staticmethod
    def _state(request: ResearchGraphRequest, **overrides: object) -> dict[str, object]:
        state: dict[str, object] = {
            "organization_id": request.organization_id,
            "project_id": request.project_id,
            "article_id": request.article_id,
            "outline_version": request.outline_version,
            "retrieval_plan_id": request.retrieval_plan_id,
            "thread_id": request.thread_id,
            "status": "running",
            "current_node": "retrieve_knowledge",
            "current_scope_id": f"{request.project_id}-scope",
            "gap_fill_round": 1,
            "max_gap_fill_rounds": request.max_gap_fill_rounds,
            "discovery_queries_used": 1,
            "max_discovery_queries": request.max_discovery_queries,
            "evidence_pack_ids": ["pack-1"],
            "warnings": [],
        }
        state.update(overrides)
        return state

    def test_run_is_idempotent_and_tracks_ordered_events_and_state(self) -> None:
        plan = self._plan()
        request = self._request(plan)

        created = self.runs.create_run(request, metadata={"owner": "worker"})
        retried = self.runs.create_run(request, metadata={"owner": "worker"})
        self.assertEqual(created.thread_id, retried.thread_id)
        self.assertEqual(retried.status, "queued")

        queued = self.runs.append_event(
            project_id=plan.project_id,
            thread_id=request.thread_id,
            event_type="queued",
            node_name="queued",
        )
        completed = self.runs.append_event(
            project_id=plan.project_id,
            thread_id=request.thread_id,
            event_type="node_completed",
            node_name="retrieve_knowledge",
            attempt=2,
            details={"hit_count": 2},
        )
        self.assertEqual((queued.sequence, completed.sequence), (1, 2))

        updated = self.runs.update_from_state(
            plan.project_id,
            request.thread_id,
            self._state(request),
        )
        self.assertEqual(updated.status, "running")
        self.assertEqual(updated.gap_fill_round, 1)
        self.assertEqual(updated.evidence_pack_ids, ("pack-1",))
        self.assertEqual(
            [event.sequence for event in self.runs.list_events(
                plan.project_id,
                request.thread_id,
            )],
            [1, 2],
        )

    def test_thread_id_is_globally_unique_across_projects(self) -> None:
        plan_a = self._plan("a")
        plan_b = self._plan("b")
        shared_thread = f"{self.prefix}-shared-thread"
        self.runs.create_run(self._request(plan_a, thread_id=shared_thread))

        with self.assertRaises(ResearchRunConflictError):
            self.runs.create_run(self._request(plan_b, thread_id=shared_thread))

    def test_run_rejects_wrong_plan_version_and_cross_project_state(self) -> None:
        plan = self._plan()
        request = self._request(plan)
        wrong_version = ResearchGraphRequest(
            organization_id=request.organization_id,
            project_id=request.project_id,
            article_id=request.article_id,
            outline_version=request.outline_version + 1,
            retrieval_plan_id=request.retrieval_plan_id,
            thread_id=f"{request.thread_id}-wrong",
        )
        with self.assertRaises(ResearchRunConflictError):
            self.runs.create_run(wrong_version)

        self.runs.create_run(request)
        foreign_state = self._state(request, project_id="foreign-project")
        with self.assertRaises(ResearchRunConflictError):
            self.runs.update_from_state(
                plan.project_id,
                request.thread_id,
                foreign_state,
            )
        self.assertEqual(
            self.runs.get_run(plan.project_id, request.thread_id).status,  # type: ignore[union-attr]
            "queued",
        )

    def test_gap_fill_attempt_is_idempotent_but_immutable(self) -> None:
        plan = self._plan()
        request = self._request(plan)
        self.runs.create_run(request)
        attempt = GapFillAttempt(
            project_id=plan.project_id,
            thread_id=request.thread_id,
            retrieval_plan_id=plan.retrieval_plan_id,
            scope_id=plan.scopes[0].scope_id,
            round_number=1,
            attempt_id=f"{self.prefix}-attempt",
            reason="missing hard fact",
            channel="official_site",
            query="site:example.test dimensions",
            discovered_urls=("https://example.test/product",),
            result="improved",
            cost_usage={"queries": 1},
        )

        first = self.runs.record_gap_attempt(attempt)
        second = self.runs.record_gap_attempt(attempt)
        self.assertEqual(first.attempt_id, second.attempt_id)
        self.assertEqual(
            self.runs.list_gap_attempts(plan.project_id, request.thread_id),
            (first,),
        )
        changed = GapFillAttempt(
            project_id=attempt.project_id,
            thread_id=attempt.thread_id,
            retrieval_plan_id=attempt.retrieval_plan_id,
            scope_id=attempt.scope_id,
            round_number=attempt.round_number,
            attempt_id=attempt.attempt_id,
            reason=attempt.reason,
            channel=attempt.channel,
            query=attempt.query,
            discovered_urls=attempt.discovered_urls,
            result="no_change",
            cost_usage=attempt.cost_usage,
        )
        with self.assertRaises(ResearchRunConflictError):
            self.runs.record_gap_attempt(changed)

    def test_failure_diagnostic_never_persists_provider_message_or_key(self) -> None:
        plan = self._plan()
        request = self._request(plan)
        self.runs.create_run(request)
        secret = "sk-secret-must-not-survive"

        failed = self.runs.mark_failed(
            plan.project_id,
            request.thread_id,
            RuntimeError(f"provider rejected {secret}"),
        )
        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.error_code, "RuntimeError")
        self.assertNotIn(secret, failed.error_message or "")
        self.assertIsNotNone(failed.finished_at)

    def test_database_rejects_terminal_status_without_finished_at(self) -> None:
        plan = self._plan()
        request = self._request(plan)
        self.runs.create_run(request)

        with self.assertRaises(IntegrityError):
            with self.engine.begin() as connection:
                connection.execute(
                    research_graph_runs.update()
                    .where(
                        research_graph_runs.c.project_id == plan.project_id,
                        research_graph_runs.c.thread_id == request.thread_id,
                    )
                    .values(status="completed", finished_at=None)
                )


if __name__ == "__main__":
    unittest.main()
