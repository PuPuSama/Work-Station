from __future__ import annotations

import os
import sys
import unittest
import uuid
from pathlib import Path

import sqlalchemy as sa


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from knowledge_agent.database import create_knowledge_engine  # noqa: E402
from knowledge_agent.schema import projects  # noqa: E402
from server_schema import (  # noqa: E402
    article_tasks,
    assistant_conversations,
    assistant_messages,
    background_jobs,
    job_batches,
    organizations,
    project_ownership,
    project_topics,
    task_store_state,
    workflow_assistant_dispatches,
    workflow_plan_events,
    workflow_plan_projects,
    workflow_plan_steps,
    workflow_plans,
    workspace_users,
)
from services.access_control import ActorIdentity  # noqa: E402
from services.postgres_job_queue import PostgresJobQueue  # noqa: E402
from services.postgres_task_repository import PostgresTaskRepository  # noqa: E402
from workflow_assistant.contracts import PlanDraft, PlanStep  # noqa: E402
from workflow_assistant.context import WorkflowAssistantContextResolver  # noqa: E402
from workflow_assistant.execution import (  # noqa: E402
    StepExecutionResult,
    WorkflowExecutionResult,
)
from workflow_assistant.graph import WorkflowAssistantGraph  # noqa: E402
from workflow_assistant.repository import (  # noqa: E402
    PostgresWorkflowAssistantRepository,
    WorkflowAssistantConflict,
    WorkflowAssistantNotFound,
)


class FixedCoordinator:
    def __init__(self, status: str) -> None:
        self.status = status
        self.calls = 0

    def execute_plan(
        self,
        *,
        actor: ActorIdentity,
        plan_id: str,
    ) -> WorkflowExecutionResult:
        del actor
        self.calls += 1
        return WorkflowExecutionResult(
            plan_id=plan_id,
            revision=self.calls,
            results=(
                StepExecutionResult(
                    step_id="step-1",
                    status=self.status,  # type: ignore[arg-type]
                    output_summary={},
                    error_code=(
                        "human_confirmation_required"
                        if self.status == "waiting_review"
                        else None
                    ),
                ),
            ),
        )


@unittest.skipUnless(
    os.environ.get("ARTICLE_AGENT_DATABASE_URL"),
    "ARTICLE_AGENT_DATABASE_URL is required for PostgreSQL integration tests",
)
class WorkflowAssistantPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = create_knowledge_engine(
            os.environ["ARTICLE_AGENT_DATABASE_URL"]
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    def setUp(self) -> None:
        prefix = f"workflow-assistant-{uuid.uuid4().hex}"
        self.organization_id = f"{prefix}-org"
        self.owner_user_id = f"{prefix}-owner"
        self.other_user_id = f"{prefix}-other"
        self.project_a = f"{prefix}-a.example.test"
        self.project_b = f"{prefix}-b.example.test"
        self.actor = ActorIdentity(self.organization_id, self.owner_user_id)
        self.other_actor = ActorIdentity(
            self.organization_id,
            self.other_user_id,
        )
        with self.engine.begin() as connection:
            connection.execute(
                organizations.insert().values(
                    organization_id=self.organization_id,
                    name="Workflow Assistant Test",
                )
            )
            connection.execute(
                workspace_users.insert(),
                (
                    {
                        "organization_id": self.organization_id,
                        "user_id": self.owner_user_id,
                        "display_name": "Owner",
                    },
                    {
                        "organization_id": self.organization_id,
                        "user_id": self.other_user_id,
                        "display_name": "Other",
                    },
                ),
            )
            connection.execute(
                projects.insert(),
                (
                    {
                        "project_id": self.project_a,
                        "customer_name": "Project A",
                        "official_domain": self.project_a,
                    },
                    {
                        "project_id": self.project_b,
                        "customer_name": "Project B",
                        "official_domain": self.project_b,
                    },
                ),
            )
            connection.execute(
                project_ownership.insert(),
                (
                    {
                        "organization_id": self.organization_id,
                        "project_id": self.project_a,
                        "owner_user_id": self.owner_user_id,
                    },
                    {
                        "organization_id": self.organization_id,
                        "project_id": self.project_b,
                        "owner_user_id": self.owner_user_id,
                    },
                ),
            )

    def tearDown(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                background_jobs.delete().where(
                    background_jobs.c.organization_id == self.organization_id
                )
            )
            connection.execute(
                job_batches.delete().where(
                    job_batches.c.organization_id == self.organization_id
                )
            )
            connection.execute(
                workflow_assistant_dispatches.delete().where(
                    workflow_assistant_dispatches.c.organization_id
                    == self.organization_id
                )
            )
            connection.execute(
                workflow_plan_events.delete().where(
                    workflow_plan_events.c.organization_id
                    == self.organization_id
                )
            )
            connection.execute(
                workflow_plan_steps.delete().where(
                    workflow_plan_steps.c.organization_id
                    == self.organization_id
                )
            )
            connection.execute(
                workflow_plan_projects.delete().where(
                    workflow_plan_projects.c.organization_id
                    == self.organization_id
                )
            )
            connection.execute(
                workflow_plans.delete().where(
                    workflow_plans.c.organization_id == self.organization_id
                )
            )
            connection.execute(
                assistant_messages.delete().where(
                    assistant_messages.c.organization_id
                    == self.organization_id
                )
            )
            connection.execute(
                assistant_conversations.delete().where(
                    assistant_conversations.c.organization_id
                    == self.organization_id
                )
            )
            connection.execute(
                project_topics.delete().where(
                    project_topics.c.organization_id == self.organization_id
                )
            )
            connection.execute(
                article_tasks.delete().where(
                    article_tasks.c.organization_id == self.organization_id
                )
            )
            connection.execute(
                task_store_state.delete().where(
                    task_store_state.c.organization_id == self.organization_id
                )
            )
            connection.execute(
                project_ownership.delete().where(
                    project_ownership.c.organization_id
                    == self.organization_id
                )
            )
            connection.execute(
                projects.delete().where(
                    projects.c.project_id.in_((self.project_a, self.project_b))
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
                    organizations.c.organization_id
                    == self.organization_id
                )
            )

    def test_context_reads_only_published_topics_from_each_project(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                project_topics.insert(),
                (
                    {
                        "organization_id": self.organization_id,
                        "project_id": self.project_a,
                        "topic_id": "topic-a-published",
                        "topic": "Published A",
                        "status": "published",
                    },
                    {
                        "organization_id": self.organization_id,
                        "project_id": self.project_a,
                        "topic_id": "topic-a-archived",
                        "topic": "Archived A",
                        "status": "archived",
                    },
                    {
                        "organization_id": self.organization_id,
                        "project_id": self.project_b,
                        "topic_id": "topic-b-published",
                        "topic": "Published B",
                        "status": "published",
                    },
                ),
            )

        context = WorkflowAssistantContextResolver(self.engine).resolve(
            actor=self.actor,
            project_ids=[self.project_a, self.project_b],
        )

        topics_by_project = {
            project.project_id: tuple(
                topic.topic_id for topic in project.published_topics
            )
            for project in context.projects
        }
        self.assertEqual(
            topics_by_project,
            {
                self.project_a: ("topic-a-published",),
                self.project_b: ("topic-b-published",),
            },
        )

    def test_private_conversation_and_message_idempotency(self) -> None:
        repository = PostgresWorkflowAssistantRepository(self.engine)
        conversation = repository.create_conversation(
            actor=self.actor,
            title="Private plan",
            project_ids=(self.project_a,),
        )
        first = repository.append_message(
            actor=self.actor,
            conversation_id=conversation.conversation_id,
            role="user",
            content="Write two articles",
            request_id="request-1",
            idempotency_key="message-1",
        )
        replay = repository.append_message(
            actor=self.actor,
            conversation_id=conversation.conversation_id,
            role="user",
            content="Write two articles",
            request_id="request-1",
            idempotency_key="message-1",
        )

        self.assertEqual(replay.message_id, first.message_id)
        self.assertEqual(replay.sequence, 1)
        with self.assertRaises(WorkflowAssistantNotFound):
            repository.get_conversation(
                actor=self.other_actor,
                conversation_id=conversation.conversation_id,
            )

    def test_planning_dispatch_can_be_recovered_by_private_idempotency_key(self) -> None:
        repository = PostgresWorkflowAssistantRepository(self.engine)
        conversation = repository.create_conversation(
            actor=self.actor,
            title="Recover planning dispatch",
            project_ids=(self.project_a,),
        )
        dispatch = repository.enqueue_planning_dispatch(
            actor=self.actor,
            conversation_id=conversation.conversation_id,
            content="Write two articles",
            request_id="request-dispatch-1",
            idempotency_key="message-dispatch-1",
            project_ids=(self.project_a,),
        )

        recovered = repository.get_planning_dispatch_by_idempotency(
            actor=self.actor,
            conversation_id=conversation.conversation_id,
            idempotency_key="message-dispatch-1",
        )

        self.assertEqual(recovered.dispatch_id, dispatch.dispatch_id)
        self.assertEqual(recovered.status, "queued")
        with self.assertRaises(WorkflowAssistantNotFound):
            repository.get_planning_dispatch_by_idempotency(
                actor=self.other_actor,
                conversation_id=conversation.conversation_id,
                idempotency_key="message-dispatch-1",
            )

    def test_plan_execution_advisory_lock_is_global_across_connections(self) -> None:
        first = PostgresWorkflowAssistantRepository(self.engine)
        second = PostgresWorkflowAssistantRepository(self.engine)

        with first.plan_execution_lock(
            actor=self.actor,
            plan_id="global-lock-plan",
        ) as first_acquired:
            self.assertTrue(first_acquired)
            with second.plan_execution_lock(
                actor=self.actor,
                plan_id="global-lock-plan",
            ) as second_acquired:
                self.assertFalse(second_acquired)

        with second.plan_execution_lock(
            actor=self.actor,
            plan_id="global-lock-plan",
        ) as acquired_after_release:
            self.assertTrue(acquired_after_release)

    def _interrupted_job_fixture(
        self,
        *,
        step_id: str,
        task_id: str,
    ) -> tuple[
        PostgresWorkflowAssistantRepository,
        object,
        PostgresJobQueue,
        str,
    ]:
        PostgresTaskRepository(
            self.engine,
            organization_id=self.organization_id,
            project_id=self.project_a,
        ).upsert(
            {
                "id": task_id,
                "customer": self.project_a,
                "topic_index": 1,
                "topic": "Interrupted Job Recovery",
                "revision": 0,
                "updated_at": "2026-08-20T00:00:00+00:00",
            }
        )
        repository = PostgresWorkflowAssistantRepository(self.engine)
        conversation = repository.create_conversation(
            actor=self.actor,
            title="Interrupted Job",
            project_ids=(self.project_a,),
        )
        plan = repository.create_plan(
            actor=self.actor,
            conversation_id=conversation.conversation_id,
            plan=PlanDraft(
                title="Recover queued Job",
                natural_language_request="Generate titles",
                project_ids=[self.project_a],
                steps=[
                    PlanStep(
                        step_id=step_id,
                        sequence=1,
                        action_kind="generate_titles",
                        project_id=self.project_a,
                        article_task_id=task_id,
                        expected_task_revision=0,
                    )
                ],
            ),
        )
        plan = repository.confirm_plan(
            actor=self.actor,
            plan_id=plan.plan_id,
            expected_revision=plan.revision,
            expected_plan_hash=plan.plan_hash,
        )
        self.assertTrue(
            repository.claim_step(
                actor=self.actor,
                plan_id=plan.plan_id,
                step_id=step_id,
            )
        )
        queue = PostgresJobQueue(
            self.engine,
            organization_id=self.organization_id,
            project_id=self.project_a,
            worker_id=f"{self.owner_user_id}-recovery",
        )
        batch = queue.create_batch(
            "titles",
            [
                {
                    "task_id": task_id,
                    "customer": self.project_a,
                    "topic_index": 1,
                    "source_revision": 0,
                    "request": {"prompt_version": 1},
                }
            ],
            customer=self.project_a,
            requested_by_user_id=self.owner_user_id,
        )
        return repository, plan, queue, str(batch["jobs"][0]["id"])

    def test_interrupted_step_recovers_unique_already_queued_server_job(self) -> None:
        repository, plan, _queue, job_id = self._interrupted_job_fixture(
            step_id="recover-job",
            task_id="recover-job-task",
        )

        recovered = repository.recover_interrupted_steps(
            actor=self.actor,
            plan_id=plan.plan_id,  # type: ignore[attr-defined]
        )

        self.assertEqual(recovered.steps[0].status, "waiting_job")
        self.assertEqual(recovered.steps[0].background_job_id, job_id)
        self.assertIsNone(recovered.steps[0].standardized_error_code)

    def test_interrupted_step_never_guesses_between_multiple_matching_jobs(self) -> None:
        repository, plan, queue, first_job_id = self._interrupted_job_fixture(
            step_id="ambiguous-job",
            task_id="ambiguous-job-task",
        )
        with self.engine.begin() as connection:
            connection.execute(
                background_jobs.update()
                .where(
                    background_jobs.c.organization_id == self.organization_id,
                    background_jobs.c.project_id == self.project_a,
                    background_jobs.c.job_id == first_job_id,
                )
                .values(
                    status="failed",
                    finished_at=sa.func.now(),
                    updated_at=sa.func.now(),
                )
            )
        queue.create_batch(
            "titles",
            [
                {
                    "task_id": "ambiguous-job-task",
                    "customer": self.project_a,
                    "topic_index": 1,
                    "source_revision": 0,
                    "request": {"prompt_version": 1},
                }
            ],
            customer=self.project_a,
            requested_by_user_id=self.owner_user_id,
        )

        recovered = repository.recover_interrupted_steps(
            actor=self.actor,
            plan_id=plan.plan_id,  # type: ignore[attr-defined]
        )

        self.assertEqual(recovered.steps[0].status, "failed")
        self.assertIsNone(recovered.steps[0].background_job_id)
        self.assertEqual(
            recovered.steps[0].standardized_error_code,
            "worker_interrupted_ambiguous_job",
        )

    def test_waiting_job_can_commit_terminal_result(self) -> None:
        repository = PostgresWorkflowAssistantRepository(self.engine)
        conversation = repository.create_conversation(
            actor=self.actor,
            title="Waiting Job CAS",
            project_ids=(self.project_a,),
        )
        draft = PlanDraft(
            title="Generate titles",
            natural_language_request="Generate titles",
            project_ids=[self.project_a],
            steps=[
                PlanStep(
                    step_id="generate-titles",
                    sequence=1,
                    action_kind="generate_titles",
                    project_id=self.project_a,
                )
            ],
        )
        plan = repository.create_plan(
            actor=self.actor,
            conversation_id=conversation.conversation_id,
            plan=draft,
        )
        plan = repository.confirm_plan(
            actor=self.actor,
            plan_id=plan.plan_id,
            expected_revision=plan.revision,
            expected_plan_hash=plan.plan_hash,
        )
        self.assertTrue(
            repository.claim_step(
                actor=self.actor,
                plan_id=plan.plan_id,
                step_id="generate-titles",
            )
        )
        self.assertTrue(
            repository.finish_step(
                actor=self.actor,
                plan_id=plan.plan_id,
                step_id="generate-titles",
                status="waiting_job",
                background_job_id="job-1",
            )
        )
        self.assertTrue(
            repository.finish_step(
                actor=self.actor,
                plan_id=plan.plan_id,
                step_id="generate-titles",
                status="succeeded",
                background_job_id="job-1",
                output_summary={"status": "succeeded"},
            )
        )
        completed = repository.get_plan(
            actor=self.actor,
            plan_id=plan.plan_id,
        )
        self.assertEqual(completed.steps[0].status, "succeeded")

    def test_one_project_lane_can_pause_without_pausing_the_plan(self) -> None:
        repository = PostgresWorkflowAssistantRepository(self.engine)
        conversation = repository.create_conversation(
            actor=self.actor,
            title="Project lane pause",
            project_ids=(self.project_a, self.project_b),
        )
        draft = PlanDraft(
            title="Two project lanes",
            natural_language_request="Run one step per project",
            project_ids=[self.project_a, self.project_b],
            steps=[
                PlanStep(
                    step_id="project-a-step",
                    sequence=1,
                    action_kind="generate_titles",
                    project_id=self.project_a,
                ),
                PlanStep(
                    step_id="project-b-step",
                    sequence=2,
                    action_kind="generate_titles",
                    project_id=self.project_b,
                ),
            ],
        )
        plan = repository.create_plan(
            actor=self.actor,
            conversation_id=conversation.conversation_id,
            plan=draft,
        )
        plan = repository.confirm_plan(
            actor=self.actor,
            plan_id=plan.plan_id,
            expected_revision=plan.revision,
            expected_plan_hash=plan.plan_hash,
        )

        paused = repository.set_projects_paused(
            actor=self.actor,
            plan_id=plan.plan_id,
            expected_revision=plan.revision,
            project_ids=(self.project_a,),
            paused=True,
        )
        self.assertEqual(paused.status, "queued")
        self.assertEqual(paused.paused_project_ids, (self.project_a,))

        resumed = repository.set_projects_paused(
            actor=self.actor,
            plan_id=plan.plan_id,
            expected_revision=paused.revision,
            project_ids=(self.project_a,),
            paused=False,
        )
        self.assertEqual(resumed.paused_project_ids, ())

    def test_plain_resume_from_review_does_not_confirm_hard_gate(self) -> None:
        repository = PostgresWorkflowAssistantRepository(self.engine)
        conversation = repository.create_conversation(
            actor=self.actor,
            title="Safe review resume",
            project_ids=(self.project_a,),
        )
        plan = repository.create_plan(
            actor=self.actor,
            conversation_id=conversation.conversation_id,
            plan=PlanDraft(
                title="Package delivery",
                natural_language_request="Package the article",
                project_ids=[self.project_a],
                steps=[
                    PlanStep(
                        step_id="package-delivery",
                        sequence=1,
                        action_kind="package_delivery",
                        project_id=self.project_a,
                        hard_gate=True,
                    )
                ],
            ),
        )
        plan = repository.confirm_plan(
            actor=self.actor,
            plan_id=plan.plan_id,
            expected_revision=plan.revision,
            expected_plan_hash=plan.plan_hash,
        )
        plan = repository.set_plan_status(
            actor=self.actor,
            plan_id=plan.plan_id,
            expected_revision=plan.revision,
            new_status="running",
        )
        self.assertTrue(
            repository.hold_step_for_review(
                actor=self.actor,
                plan_id=plan.plan_id,
                step_id="package-delivery",
            )
        )
        plan = repository.set_plan_status(
            actor=self.actor,
            plan_id=plan.plan_id,
            expected_revision=plan.revision,
            new_status="waiting_review",
        )

        resumed = repository.set_plan_status(
            actor=self.actor,
            plan_id=plan.plan_id,
            expected_revision=plan.revision,
            new_status="running",
        )

        self.assertEqual(resumed.status, "running")
        self.assertEqual(resumed.steps[0].status, "waiting_review")
        self.assertFalse(resumed.steps[0].human_gate_confirmed)

    def test_retry_failed_steps_preserves_completed_steps(self) -> None:
        repository = PostgresWorkflowAssistantRepository(self.engine)
        conversation = repository.create_conversation(
            actor=self.actor,
            title="Retry failed steps",
            project_ids=(self.project_a,),
        )
        plan = repository.create_plan(
            actor=self.actor,
            conversation_id=conversation.conversation_id,
            plan=PlanDraft(
                title="Retry article workflow",
                natural_language_request="Write and export the article",
                project_ids=[self.project_a],
                steps=[
                    PlanStep(
                        step_id="completed-step",
                        sequence=1,
                        action_kind="generate_titles",
                        project_id=self.project_a,
                    ),
                    PlanStep(
                        step_id="failed-step",
                        sequence=2,
                        action_kind="generate_outline",
                        project_id=self.project_a,
                    ),
                    PlanStep(
                        step_id="blocked-step",
                        sequence=3,
                        action_kind="generate_article",
                        project_id=self.project_a,
                    ),
                ],
            ),
        )
        plan = repository.confirm_plan(
            actor=self.actor,
            plan_id=plan.plan_id,
            expected_revision=plan.revision,
            expected_plan_hash=plan.plan_hash,
        )
        plan = repository.set_plan_status(
            actor=self.actor,
            plan_id=plan.plan_id,
            expected_revision=plan.revision,
            new_status="running",
        )
        self.assertTrue(
            repository.claim_step(
                actor=self.actor,
                plan_id=plan.plan_id,
                step_id="completed-step",
            )
        )
        self.assertTrue(
            repository.finish_step(
                actor=self.actor,
                plan_id=plan.plan_id,
                step_id="completed-step",
                status="succeeded",
                output_summary={"title": "kept"},
            )
        )
        self.assertTrue(
            repository.claim_step(
                actor=self.actor,
                plan_id=plan.plan_id,
                step_id="failed-step",
            )
        )
        self.assertTrue(
            repository.finish_step(
                actor=self.actor,
                plan_id=plan.plan_id,
                step_id="failed-step",
                status="failed",
                output_summary={"provider": "failed"},
                standardized_error_code="provider_failed",
                background_job_id="failed-job",
            )
        )
        self.assertEqual(
            repository.skip_steps_blocked_by_failure(
                actor=self.actor,
                plan_id=plan.plan_id,
                failed_step_id="failed-step",
            ),
            ("blocked-step",),
        )
        failed_plan = repository.set_plan_status(
            actor=self.actor,
            plan_id=plan.plan_id,
            expected_revision=plan.revision,
            new_status="failed",
        )

        retried = repository.retry_failed_steps(
            actor=self.actor,
            plan_id=failed_plan.plan_id,
            expected_revision=failed_plan.revision,
            expected_plan_hash=failed_plan.plan_hash,
        )

        self.assertEqual(retried.status, "queued")
        completed = retried.steps[0]
        failed = retried.steps[1]
        self.assertEqual(completed.status, "succeeded")
        self.assertEqual(completed.output_summary, {"title": "kept"})
        self.assertEqual(failed.status, "pending")
        self.assertEqual(failed.retry_count, 1)
        self.assertIsNone(failed.background_job_id)
        self.assertEqual(failed.output_summary, {})
        self.assertIsNone(failed.standardized_error_code)
        blocked = retried.steps[2]
        self.assertEqual(blocked.status, "pending")
        self.assertEqual(blocked.retry_count, 1)
        self.assertIsNone(blocked.background_job_id)
        self.assertEqual(blocked.output_summary, {})
        self.assertIsNone(blocked.standardized_error_code)

    def test_retry_failed_steps_rebinds_internal_task_checkpoint_revision(self) -> None:
        task_id = f"{self.project_a}-retry-task"
        with self.engine.begin() as connection:
            connection.execute(
                article_tasks.insert().values(
                    organization_id=self.organization_id,
                    project_id=self.project_a,
                    task_id=task_id,
                    customer="Project A",
                    topic_index=0,
                    position=0,
                    revision=9,
                    payload={"status": "initial_ai_checked"},
                )
            )

        repository = PostgresWorkflowAssistantRepository(self.engine)
        conversation = repository.create_conversation(
            actor=self.actor,
            title="Retry stale task revision",
            project_ids=(self.project_a,),
        )
        plan = repository.create_plan(
            actor=self.actor,
            conversation_id=conversation.conversation_id,
            plan=PlanDraft(
                title="Retry humanize",
                natural_language_request="Retry the humanized article",
                project_ids=[self.project_a],
                steps=[
                    PlanStep(
                        step_id="humanize-step",
                        sequence=1,
                        action_kind="humanize",
                        project_id=self.project_a,
                        article_task_id=task_id,
                        expected_task_revision=8,
                    ),
                    PlanStep(
                        step_id="package-step",
                        sequence=2,
                        action_kind="package_delivery",
                        project_id=self.project_a,
                        article_task_id=task_id,
                        expected_task_revision=8,
                    ),
                ],
            ),
        )
        plan = repository.confirm_plan(
            actor=self.actor,
            plan_id=plan.plan_id,
            expected_revision=plan.revision,
            expected_plan_hash=plan.plan_hash,
        )
        plan = repository.set_plan_status(
            actor=self.actor,
            plan_id=plan.plan_id,
            expected_revision=plan.revision,
            new_status="running",
        )
        self.assertTrue(
            repository.claim_step(
                actor=self.actor,
                plan_id=plan.plan_id,
                step_id="humanize-step",
            )
        )
        self.assertTrue(
            repository.finish_step(
                actor=self.actor,
                plan_id=plan.plan_id,
                step_id="humanize-step",
                status="failed",
                output_summary={},
                standardized_error_code="background_job_failed",
            )
        )
        self.assertEqual(
            repository.skip_steps_blocked_by_failure(
                actor=self.actor,
                plan_id=plan.plan_id,
                failed_step_id="humanize-step",
            ),
            ("package-step",),
        )
        failed_plan = repository.set_plan_status(
            actor=self.actor,
            plan_id=plan.plan_id,
            expected_revision=plan.revision,
            new_status="failed",
        )

        retried = repository.retry_failed_steps(
            actor=self.actor,
            plan_id=failed_plan.plan_id,
            expected_revision=failed_plan.revision,
            expected_plan_hash=failed_plan.plan_hash,
        )

        self.assertEqual(
            [step.expected_task_revision for step in retried.steps],
            [9, 9],
        )
        self.assertEqual(
            [step.status for step in retried.steps],
            ["pending", "pending"],
        )

    def test_retry_failed_steps_rejects_unrelated_task_revision_change(self) -> None:
        task_id = f"{self.project_a}-changed-task"
        with self.engine.begin() as connection:
            connection.execute(
                article_tasks.insert().values(
                    organization_id=self.organization_id,
                    project_id=self.project_a,
                    task_id=task_id,
                    customer="Project A",
                    topic_index=0,
                    position=0,
                    revision=10,
                    payload={"status": "initial_ai_checked"},
                )
            )

        repository = PostgresWorkflowAssistantRepository(self.engine)
        conversation = repository.create_conversation(
            actor=self.actor,
            title="Reject changed task",
            project_ids=(self.project_a,),
        )
        plan = repository.create_plan(
            actor=self.actor,
            conversation_id=conversation.conversation_id,
            plan=PlanDraft(
                title="Reject stale retry",
                natural_language_request="Retry the article",
                project_ids=[self.project_a],
                steps=[
                    PlanStep(
                        step_id="humanize-step",
                        sequence=1,
                        action_kind="humanize",
                        project_id=self.project_a,
                        article_task_id=task_id,
                        expected_task_revision=8,
                    ),
                ],
            ),
        )
        plan = repository.confirm_plan(
            actor=self.actor,
            plan_id=plan.plan_id,
            expected_revision=plan.revision,
            expected_plan_hash=plan.plan_hash,
        )
        plan = repository.set_plan_status(
            actor=self.actor,
            plan_id=plan.plan_id,
            expected_revision=plan.revision,
            new_status="running",
        )
        self.assertTrue(
            repository.claim_step(
                actor=self.actor,
                plan_id=plan.plan_id,
                step_id="humanize-step",
            )
        )
        self.assertTrue(
            repository.finish_step(
                actor=self.actor,
                plan_id=plan.plan_id,
                step_id="humanize-step",
                status="failed",
                output_summary={},
                standardized_error_code="background_job_failed",
            )
        )
        failed_plan = repository.set_plan_status(
            actor=self.actor,
            plan_id=plan.plan_id,
            expected_revision=plan.revision,
            new_status="failed",
        )

        with self.assertRaises(WorkflowAssistantConflict):
            repository.retry_failed_steps(
                actor=self.actor,
                plan_id=failed_plan.plan_id,
                expected_revision=failed_plan.revision,
                expected_plan_hash=failed_plan.plan_hash,
            )

    def test_gap_fill_release_binds_resume_job_and_is_idempotent(self) -> None:
        repository = PostgresWorkflowAssistantRepository(self.engine)
        conversation = repository.create_conversation(
            actor=self.actor,
            title="Research gap fill",
            project_ids=(self.project_a,),
        )
        plan = repository.create_plan(
            actor=self.actor,
            conversation_id=conversation.conversation_id,
            plan=PlanDraft(
                title="Research article",
                natural_language_request="Research the article",
                project_ids=[self.project_a],
                steps=[
                    PlanStep(
                        step_id="research-step",
                        sequence=1,
                        action_kind="start_research",
                        project_id=self.project_a,
                    )
                ],
            ),
        )
        plan = repository.confirm_plan(
            actor=self.actor,
            plan_id=plan.plan_id,
            expected_revision=plan.revision,
            expected_plan_hash=plan.plan_hash,
        )
        plan = repository.set_plan_status(
            actor=self.actor,
            plan_id=plan.plan_id,
            expected_revision=plan.revision,
            new_status="running",
        )
        self.assertTrue(
            repository.claim_step(
                actor=self.actor,
                plan_id=plan.plan_id,
                step_id="research-step",
            )
        )
        self.assertTrue(
            repository.finish_step(
                actor=self.actor,
                plan_id=plan.plan_id,
                step_id="research-step",
                status="waiting_review",
                output_summary={
                    "research_thread_id": "thread-a",
                    "retrieval_plan_id": "retrieval-a",
                },
            )
        )
        plan = repository.set_plan_status(
            actor=self.actor,
            plan_id=plan.plan_id,
            expected_revision=plan.revision,
            new_status="waiting_review",
        )

        released = repository.release_research_gap_fill(
            actor=self.actor,
            plan_id=plan.plan_id,
            expected_revision=plan.revision,
            step_id="research-step",
            research_thread_id="thread-a",
            approved_candidate_ids=("candidate-a",),
            request_id="assistant-gap-fill-request-a",
            background_job_id="resume-job-a",
        )

        self.assertEqual(released.status, "running")
        self.assertEqual(released.steps[0].status, "waiting_job")
        self.assertEqual(released.steps[0].background_job_id, "resume-job-a")
        self.assertTrue(released.steps[0].human_gate_confirmed)
        self.assertEqual(
            released.steps[0].input_summary["approved_candidate_ids"],
            ["candidate-a"],
        )
        retried = repository.release_research_gap_fill(
            actor=self.actor,
            plan_id=plan.plan_id,
            expected_revision=plan.revision,
            step_id="research-step",
            research_thread_id="thread-a",
            approved_candidate_ids=("candidate-a",),
            request_id="assistant-gap-fill-request-a",
            background_job_id="resume-job-a",
        )
        self.assertEqual(retried.revision, released.revision)
        self.assertEqual(retried.steps[0].background_job_id, "resume-job-a")

    def test_revision_event_bounds_more_than_fifty_preserved_step_ids(self) -> None:
        repository = PostgresWorkflowAssistantRepository(self.engine)
        conversation = repository.create_conversation(
            actor=self.actor,
            title="Large preserved revision",
            project_ids=(self.project_a,),
        )
        steps = [
            PlanStep(
                step_id=f"preserved-{index:02d}",
                sequence=index,
                action_kind="generate_titles",
                project_id=self.project_a,
            )
            for index in range(1, 52)
        ]
        plan = repository.create_plan(
            actor=self.actor,
            conversation_id=conversation.conversation_id,
            plan=PlanDraft(
                title="Large preserved revision",
                natural_language_request="Generate many titles",
                project_ids=[self.project_a],
                steps=steps,
            ),
        )
        with self.engine.begin() as connection:
            connection.execute(
                workflow_plan_steps.update()
                .where(
                    workflow_plan_steps.c.organization_id == self.organization_id,
                    workflow_plan_steps.c.plan_id == plan.plan_id,
                )
                .values(status="succeeded")
            )

        revised = repository.revise_plan(
            actor=self.actor,
            plan_id=plan.plan_id,
            expected_revision=plan.revision,
            expected_plan_hash=plan.plan_hash,
            plan=PlanDraft(
                title="Large preserved revision",
                natural_language_request="Generate many titles with revised wording",
                project_ids=[self.project_a],
                steps=steps,
            ),
        )

        self.assertEqual(revised.status, "awaiting_confirmation")
        event = repository.list_events(
            actor=self.actor,
            plan_id=plan.plan_id,
        )[-1]
        self.assertEqual(event.event_kind, "plan_revised")
        self.assertEqual(event.public_payload["preserved_step_count"], 51)
        self.assertEqual(len(event.public_payload["preserved_step_ids"]), 50)
        self.assertTrue(event.public_payload["preserved_step_ids_truncated"])

    def test_postgres_checkpoint_resumes_after_graph_recreation(self) -> None:
        from langgraph.checkpoint.postgres import PostgresSaver
        from langgraph.types import Command

        database_url = os.environ["ARTICLE_AGENT_DATABASE_URL"].replace(
            "postgresql+psycopg://",
            "postgresql://",
            1,
        )
        thread_id = f"workflow-assistant:{uuid.uuid4().hex}"
        config = {"configurable": {"thread_id": thread_id}}
        first_coordinator = FixedCoordinator("waiting_review")
        with PostgresSaver.from_conn_string(database_url) as saver:
            first_graph = WorkflowAssistantGraph(
                first_coordinator  # type: ignore[arg-type]
            ).compile(checkpointer=saver)
            waiting = first_graph.invoke(
                {
                    "plan_id": "plan-1",
                    "organization_id": self.organization_id,
                    "user_id": self.owner_user_id,
                },
                config=config,
            )
        self.assertTrue(waiting["waiting_for_review"])

        resumed_coordinator = FixedCoordinator("succeeded")
        with PostgresSaver.from_conn_string(database_url) as saver:
            resumed_graph = WorkflowAssistantGraph(
                resumed_coordinator  # type: ignore[arg-type]
            ).compile(checkpointer=saver)
            completed = resumed_graph.invoke(
                Command(resume={"approved": True}),
                config=config,
            )
            saver.delete_thread(thread_id)

        self.assertFalse(completed["waiting_for_review"])
        self.assertEqual(completed["results"][0]["status"], "succeeded")
        self.assertEqual(first_coordinator.calls, 1)
        self.assertEqual(resumed_coordinator.calls, 1)


if __name__ == "__main__":
    unittest.main()
