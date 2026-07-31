from __future__ import annotations

import os
import sys
import time
import unittest
import uuid
from pathlib import Path

import sqlalchemy as sa


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from knowledge_agent.database import create_knowledge_engine  # noqa: E402
from knowledge_agent.schema import (  # noqa: E402
    gap_fill_attempts,
    projects,
    research_graph_events,
    research_graph_runs,
    retrieval_plans,
    retrieval_scopes,
)
from models import TaskRecord  # noqa: E402
from server_schema import (  # noqa: E402
    article_tasks,
    audit_events,
    background_jobs,
    job_batches,
    organizations,
    project_memberships,
    project_ownership,
    task_store_state,
    workspace_users,
)
from services.access_control import (  # noqa: E402
    ActorIdentity,
    PostgresProjectAccessRepository,
    ProjectAccessService,
)
from services.postgres_job_queue import PostgresJobQueue  # noqa: E402
from services.postgres_task_repository import (  # noqa: E402
    PostgresTaskRepository,
)
from services.server_knowledge_research import (  # noqa: E402
    KNOWLEDGE_RESEARCH_OPERATION,
    ServerKnowledgeResearchRegistry,
    ServerKnowledgeResearchUnavailable,
)
from services.server_job_control import (  # noqa: E402
    PostgresServerJobControlService,
    ServerJobControlConflict,
)


class RecordingAuditWriter:
    def __init__(self) -> None:
        self.events = []

    def append(self, connection, event) -> None:
        if not connection.in_transaction():
            raise AssertionError("audit must share the command transaction")
        self.events.append(event)


class FailingAuditWriter:
    def append(self, connection, event) -> None:
        del connection, event
        raise RuntimeError("private-audit-secret")


class FakeResearchExecution:
    def __init__(self, engine) -> None:
        self.engine = engine
        self.candidates: list[dict[str, object]] = []
        self.start_calls: list[str] = []
        self.resume_calls: list[tuple[str, tuple[str, ...]]] = []

    def execute_start(self, request):
        self.start_calls.append(request.thread_id)
        return None

    def checkpoint_state(self, *, project_id, thread_id):
        del project_id, thread_id
        return {"discovered_candidates": list(self.candidates)}

    def validate_resume(self, *, project_id, thread_id, approved_urls):
        del project_id, thread_id
        known = {
            str(candidate["url"])
            for candidate in self.candidates
        }
        if set(approved_urls) - known:
            raise ValueError("approved_urls contains unknown candidates")

    def execute_resume(self, *, project_id, thread_id, approved_urls):
        del project_id
        self.resume_calls.append((thread_id, tuple(approved_urls)))
        return None


@unittest.skipUnless(
    os.environ.get("ARTICLE_AGENT_DATABASE_URL"),
    "ARTICLE_AGENT_DATABASE_URL is required for PostgreSQL integration tests",
)
class ServerKnowledgeResearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = create_knowledge_engine(
            os.environ["ARTICLE_AGENT_DATABASE_URL"]
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    def setUp(self) -> None:
        prefix = f"m7-research-{uuid.uuid4().hex}"
        self.organization_id = f"{prefix}-org"
        self.user_id = f"{prefix}-editor"
        self.project_id = f"{prefix}.example.test"
        self.task_id = f"{prefix}-task"
        with self.engine.begin() as connection:
            connection.execute(
                organizations.insert().values(
                    organization_id=self.organization_id,
                    name="Research Org",
                )
            )
            connection.execute(
                workspace_users.insert().values(
                    organization_id=self.organization_id,
                    user_id=self.user_id,
                    display_name="Research Editor",
                )
            )
            connection.execute(
                projects.insert().values(
                    project_id=self.project_id,
                    customer_name="Research Project",
                    official_domain=self.project_id,
                )
            )
            connection.execute(
                project_ownership.insert().values(
                    organization_id=self.organization_id,
                    project_id=self.project_id,
                )
            )
            connection.execute(
                project_memberships.insert().values(
                    organization_id=self.organization_id,
                    project_id=self.project_id,
                    user_id=self.user_id,
                    role="editor",
                    granted_by_user_id=self.user_id,
                )
            )
        self.task = TaskRecord(
            id=self.task_id,
            week_folder="week-1",
            customer=self.project_id,
            topic_index=6,
            topic="Fastener buyer guide",
            status="outline_confirmed",
            task_dir="server",
            revision=7,
            created_at="2026-07-31T00:00:00+00:00",
            updated_at="2026-07-31T00:00:00+00:00",
            outline=(
                "## Buyer requirements\n\n### Application\n\n"
                "## Product facts\n\n### Materials"
            ),
            article_versions=[
                {
                    "kind": "outline",
                    "content": "confirmed",
                    "source_kind": "manual_confirmed",
                }
            ],
        )
        PostgresTaskRepository(
            self.engine,
            organization_id=self.organization_id,
            project_id=self.project_id,
        ).upsert(self.task.model_dump())
        self.actor = ActorIdentity(self.organization_id, self.user_id)
        self.access = ProjectAccessService(
            PostgresProjectAccessRepository(self.engine)
        )
        self.execution = FakeResearchExecution(self.engine)
        self.audit = RecordingAuditWriter()
        self.registry = ServerKnowledgeResearchRegistry(
            self.engine,
            access=self.access,
            execution=self.execution,  # type: ignore[arg-type]
            audit=self.audit,
        )

    def tearDown(self) -> None:
        self.registry.stop()
        with self.engine.begin() as connection:
            connection.execute(
                research_graph_events.delete().where(
                    research_graph_events.c.project_id == self.project_id
                )
            )
            connection.execute(
                gap_fill_attempts.delete().where(
                    gap_fill_attempts.c.project_id == self.project_id
                )
            )
            connection.execute(
                research_graph_runs.delete().where(
                    research_graph_runs.c.project_id == self.project_id
                )
            )
            connection.execute(
                retrieval_scopes.delete().where(
                    retrieval_scopes.c.project_id == self.project_id
                )
            )
            connection.execute(
                retrieval_plans.delete().where(
                    retrieval_plans.c.project_id == self.project_id
                )
            )
            connection.execute(
                background_jobs.delete().where(
                    background_jobs.c.organization_id
                    == self.organization_id
                )
            )
            connection.execute(
                job_batches.delete().where(
                    job_batches.c.organization_id == self.organization_id
                )
            )
            connection.execute(
                audit_events.delete().where(
                    audit_events.c.organization_id == self.organization_id
                )
            )
            connection.execute(
                article_tasks.delete().where(
                    article_tasks.c.organization_id == self.organization_id
                )
            )
            connection.execute(
                task_store_state.delete().where(
                    task_store_state.c.organization_id
                    == self.organization_id
                )
            )
            connection.execute(
                project_memberships.delete().where(
                    project_memberships.c.organization_id
                    == self.organization_id
                )
            )
            connection.execute(
                project_ownership.delete().where(
                    project_ownership.c.organization_id
                    == self.organization_id
                )
            )
            connection.execute(
                workspace_users.delete().where(
                    workspace_users.c.organization_id
                    == self.organization_id
                )
            )
            connection.execute(
                organizations.delete().where(
                    organizations.c.organization_id == self.organization_id
                )
            )
            connection.execute(
                projects.delete().where(
                    projects.c.project_id == self.project_id
                )
            )

    def _wait_for_job(self, job_id: str) -> dict[str, object]:
        queue = PostgresJobQueue(
            self.engine,
            organization_id=self.organization_id,
            project_id=self.project_id,
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = queue.get_job(job_id)
            if str(job["status"]) not in {
                "queued",
                "retry_wait",
                "running",
            }:
                return job
            time.sleep(0.02)
        self.fail("research job did not reach a terminal state")

    def test_plan_and_start_use_confirmed_postgres_task_identity(self) -> None:
        plan = self.registry.create_plan_from_task(
            actor=self.actor,
            project_id=self.project_id,
            task_id=self.task_id,
        )
        self.assertEqual(plan.article_id, "topic_006")
        self.assertEqual(plan.metadata["task_id"], self.task_id)
        self.assertEqual(
            plan.metadata["generated_from"],
            "confirmed_task_outline",
        )

        queued = self.registry.enqueue_start(
            actor=self.actor,
            project_id=self.project_id,
            retrieval_plan_id=plan.retrieval_plan_id,
            request_id="start-request-1",
            max_discovery_queries=2,
        )
        job = self._wait_for_job(str(queued["job_id"]))
        self.assertEqual(job["task_id"], self.task_id)
        self.assertEqual(job["operation"], KNOWLEDGE_RESEARCH_OPERATION)
        self.assertEqual(
            job["request"]["thread_id"],
            queued["run"].thread_id,
        )
        self.assertNotIn("organization_id", job["request"])
        self.assertEqual(
            self.execution.start_calls,
            [queued["run"].thread_id],
        )
        repeated = self.registry.enqueue_start(
            actor=self.actor,
            project_id=self.project_id,
            retrieval_plan_id=plan.retrieval_plan_id,
            request_id="start-request-1",
            max_discovery_queries=2,
        )
        self.assertEqual(repeated["job_id"], queued["job_id"])
        queued_audit = next(
            event
            for event in self.audit.events
            if event.action == "knowledge.research.queued"
        )
        self.assertEqual(
            queued_audit.action,
            "knowledge.research.queued",
        )
        self.assertNotIn("approved_urls", queued_audit.details)

    def test_resume_uses_candidate_ids_and_creates_a_new_private_job(self) -> None:
        plan = self.registry.create_plan_from_task(
            actor=self.actor,
            project_id=self.project_id,
            task_id=self.task_id,
        )
        started = self.registry.enqueue_start(
            actor=self.actor,
            project_id=self.project_id,
            retrieval_plan_id=plan.retrieval_plan_id,
            request_id="start-request-2",
            max_discovery_queries=2,
        )
        self._wait_for_job(str(started["job_id"]))
        thread_id = started["run"].thread_id
        with self.engine.begin() as connection:
            connection.execute(
                research_graph_runs.update()
                .where(
                    research_graph_runs.c.project_id == self.project_id,
                    research_graph_runs.c.thread_id == thread_id,
                )
                .values(
                    status="waiting_for_review",
                    current_node="await_human_review",
                )
            )
        private_url = f"https://{self.project_id}/private-candidate"
        self.execution.candidates = [
            {
                "candidate_id": "candidate-safe-id",
                "url": private_url,
                "page_type": "unknown",
                "needs_review": True,
            }
        ]
        resumed = self.registry.enqueue_resume(
            actor=self.actor,
            project_id=self.project_id,
            thread_id=thread_id,
            request_id="resume-request-1",
            approved_candidate_ids=("candidate-safe-id",),
        )
        job = self._wait_for_job(str(resumed["job_id"]))
        self.assertNotEqual(resumed["job_id"], started["job_id"])
        self.assertEqual(job["request"]["approved_urls"], [private_url])
        self.assertEqual(
            self.execution.resume_calls,
            [(thread_id, (private_url,))],
        )
        resume_audit = next(
            event
            for event in reversed(self.audit.events)
            if event.action == "knowledge.research.queued"
            and event.details["research_action"] == "resume"
        )
        self.assertEqual(
            resume_audit.details["approved_candidate_count"],
            1,
        )
        self.assertNotIn(private_url, str(resume_audit.details))
        with self.engine.begin() as connection:
            connection.execute(
                research_graph_runs.update()
                .where(
                    research_graph_runs.c.project_id == self.project_id,
                    research_graph_runs.c.thread_id == thread_id,
                )
                .values(
                    status="completed",
                    current_node="completed",
                    finished_at=sa.func.now(),
                )
            )
        self.execution.candidates = []
        repeated = self.registry.enqueue_resume(
            actor=self.actor,
            project_id=self.project_id,
            thread_id=thread_id,
            request_id="resume-request-1",
            approved_candidate_ids=("candidate-safe-id",),
        )
        self.assertEqual(repeated["job_id"], resumed["job_id"])
        self.assertEqual(
            self.execution.resume_calls,
            [(thread_id, (private_url,))],
        )

    def test_audit_failure_rolls_back_run_event_and_job(self) -> None:
        plan = self.registry.create_plan_from_task(
            actor=self.actor,
            project_id=self.project_id,
            task_id=self.task_id,
        )
        failing = ServerKnowledgeResearchRegistry(
            self.engine,
            access=self.access,
            execution=self.execution,  # type: ignore[arg-type]
            audit=FailingAuditWriter(),
        )
        try:
            with self.assertRaisesRegex(
                ServerKnowledgeResearchUnavailable,
                "^knowledge research could not be queued$",
            ):
                failing.enqueue_start(
                    actor=self.actor,
                    project_id=self.project_id,
                    retrieval_plan_id=plan.retrieval_plan_id,
                    request_id="failing-start",
                    max_discovery_queries=2,
                )
        finally:
            failing.stop()
        with self.engine.connect() as connection:
            self.assertEqual(
                connection.execute(
                    sa.select(sa.func.count())
                    .select_from(research_graph_runs)
                    .where(
                        research_graph_runs.c.project_id
                        == self.project_id
                    )
                ).scalar_one(),
                0,
            )
            self.assertEqual(
                connection.execute(
                    sa.select(sa.func.count())
                    .select_from(background_jobs)
                    .where(
                        background_jobs.c.organization_id
                        == self.organization_id,
                        background_jobs.c.operation
                        == KNOWLEDGE_RESEARCH_OPERATION,
                    )
                ).scalar_one(),
                0,
            )

    def test_generic_cancel_and_retry_fail_closed_for_research(self) -> None:
        plan = self.registry.create_plan_from_task(
            actor=self.actor,
            project_id=self.project_id,
            task_id=self.task_id,
        )
        queued = self.registry.enqueue_start(
            actor=self.actor,
            project_id=self.project_id,
            retrieval_plan_id=plan.retrieval_plan_id,
            request_id="domain-control-start",
            max_discovery_queries=2,
        )
        self._wait_for_job(str(queued["job_id"]))
        control = PostgresServerJobControlService(self.engine)
        with self.assertRaisesRegex(
            ServerJobControlConflict,
            "generic cancellation is not available",
        ):
            control.cancel_job(
                actor=self.actor,
                project_id=self.project_id,
                job_id=str(queued["job_id"]),
            )
        with self.assertRaisesRegex(
            ServerJobControlConflict,
            "retry is not available",
        ):
            control.retry_job(
                actor=self.actor,
                project_id=self.project_id,
                job_id=str(queued["job_id"]),
            )


if __name__ == "__main__":
    unittest.main()
