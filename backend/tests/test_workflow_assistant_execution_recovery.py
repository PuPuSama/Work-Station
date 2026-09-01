from __future__ import annotations

import sys
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterator


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services.access_control import ActorIdentity  # noqa: E402
from workflow_assistant.execution import (  # noqa: E402
    WorkflowExecutionCoordinator,
    WorkflowExecutionResult,
    _available_dispatch_slots,
)
from workflow_assistant.repository import (  # noqa: E402
    BLOCKED_BY_FAILED_STEP_ERROR_CODE,
    WorkflowExecutionCandidate,
    WorkflowPlan,
    WorkflowPlanStep,
)
from workflow_assistant.runner import WorkflowAssistantRunner  # noqa: E402


def _step(
    step_id: str,
    *,
    status: str,
    background_job_id: str | None = None,
) -> WorkflowPlanStep:
    return WorkflowPlanStep(
        step_id=step_id,
        sequence=1,
        action_kind="start_research",
        project_id="project-a",
        article_task_id="task-a",
        expected_task_revision=3,
        pinned_prompt_version={},
        pinned_knowledge_snapshot={},
        status=status,  # type: ignore[arg-type]
        background_job_id=background_job_id,
        retry_count=0,
        hard_gate=False,
        human_gate_confirmed=False,
        input_summary={},
        output_summary={},
        standardized_error_code=None,
    )


def _plan(*steps: WorkflowPlanStep) -> WorkflowPlan:
    return WorkflowPlan(
        organization_id="org-a",
        plan_id="plan-a",
        creator_user_id="user-a",
        conversation_id="conversation-a",
        title="Durable plan",
        natural_language_request="Run durable work",
        normalized_plan={},
        plan_hash="hash-a",
        revision=1,
        status="running",
        project_ids=("project-a",),
        paused_project_ids=(),
        steps=steps,
        concurrency_limit=3,
        budget_warning=False,
        attention_state="none",
        approved_by="user-a",
        approved_at=None,
    )


class _AllowAccess:
    def require(self, *_args: object, **_kwargs: object) -> None:
        return None


class _ReviewRepository:
    def __init__(self, plan: WorkflowPlan) -> None:
        self.plan = plan
        self.finish_calls: list[dict[str, Any]] = []
        self.claim_calls: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []

    def finish_step(self, **kwargs: Any) -> bool:
        self.finish_calls.append(dict(kwargs))
        self.plan = replace(
            self.plan,
            steps=tuple(
                replace(
                    step,
                    status=kwargs["status"],
                    background_job_id=kwargs.get("background_job_id"),
                    output_summary=dict(kwargs.get("output_summary") or {}),
                    standardized_error_code=kwargs.get(
                        "standardized_error_code"
                    ),
                )
                if step.step_id == kwargs["step_id"]
                else step
                for step in self.plan.steps
            ),
        )
        return True

    def claim_step(self, **kwargs: Any) -> bool:
        self.claim_calls.append(dict(kwargs))
        return False

    def append_event(self, **kwargs: Any) -> None:
        self.events.append(dict(kwargs))

    def get_plan(self, **_kwargs: Any) -> WorkflowPlan:
        return self.plan


class _GateRepository:
    def __init__(self, plan: WorkflowPlan) -> None:
        self.plan = plan
        self.claim_calls: list[str] = []
        self.status_transitions: list[str] = []
        self.events: list[dict[str, Any]] = []

    def get_plan(self, **_kwargs: Any) -> WorkflowPlan:
        return self.plan

    def hold_step_for_review(self, **kwargs: Any) -> bool:
        step_id = str(kwargs["step_id"])
        changed = False
        steps = []
        for step in self.plan.steps:
            if step.step_id == step_id and step.status == "pending" and step.hard_gate:
                step = replace(
                    step,
                    status="waiting_review",
                    standardized_error_code="human_confirmation_required",
                )
                changed = True
            steps.append(step)
        self.plan = replace(self.plan, steps=tuple(steps))
        return changed

    def claim_step(self, **kwargs: Any) -> bool:
        step_id = str(kwargs["step_id"])
        self.claim_calls.append(step_id)
        changed = False
        steps = []
        for step in self.plan.steps:
            if step.step_id == step_id and step.status == "pending":
                step = replace(step, status="running")
                changed = True
            steps.append(step)
        self.plan = replace(self.plan, steps=tuple(steps))
        return changed

    def finish_step(self, **kwargs: Any) -> bool:
        step_id = str(kwargs["step_id"])
        changed = False
        steps = []
        for step in self.plan.steps:
            if step.step_id == step_id and step.status == "running":
                step = replace(
                    step,
                    status=kwargs["status"],
                    background_job_id=kwargs.get("background_job_id"),
                    output_summary=dict(kwargs.get("output_summary") or {}),
                    standardized_error_code=kwargs.get(
                        "standardized_error_code"
                    ),
                )
                changed = True
            steps.append(step)
        self.plan = replace(self.plan, steps=tuple(steps))
        return changed

    def skip_steps_blocked_by_failure(self, **kwargs: Any) -> tuple[str, ...]:
        failed_step_id = str(kwargs["failed_step_id"])
        failed = next(
            step for step in self.plan.steps if step.step_id == failed_step_id
        )
        if failed.status != "failed":
            return ()
        blocked_ids: list[str] = []
        steps = []
        for step in self.plan.steps:
            if (
                step.status == "pending"
                and step.sequence > failed.sequence
                and step.project_id == failed.project_id
                and (step.article_task_id or "") == (failed.article_task_id or "")
            ):
                step = replace(
                    step,
                    status="skipped",
                    background_job_id=None,
                    output_summary={},
                    standardized_error_code=BLOCKED_BY_FAILED_STEP_ERROR_CODE,
                )
                blocked_ids.append(step.step_id)
            steps.append(step)
        self.plan = replace(self.plan, steps=tuple(steps))
        return tuple(blocked_ids)

    def set_plan_status(self, **kwargs: Any) -> WorkflowPlan:
        new_status = str(kwargs["new_status"])
        self.status_transitions.append(new_status)
        self.plan = replace(
            self.plan,
            status=new_status,  # type: ignore[arg-type]
            revision=self.plan.revision + 1,
        )
        return self.plan

    def append_event(self, **kwargs: Any) -> None:
        self.events.append(dict(kwargs))


class _RecordingTools:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def invoke(self, invocation: Any) -> dict[str, Any]:
        action_kind = str(invocation.action_kind)
        self.calls.append(action_kind)
        if action_kind == "package_delivery":
            raise AssertionError("an unconfirmed hard gate must never execute")
        return {}


class _FailOneArticleTools:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    def invoke(self, invocation: Any) -> dict[str, Any]:
        self.calls.append((str(invocation.action_kind), invocation.article_task_id))
        if invocation.article_task_id == "task-a":
            from workflow_assistant.tools import WorkflowToolError

            raise WorkflowToolError("article-a failed")
        return {}


class _LockedRunnerRepository:
    def __init__(self, *, acquired: bool) -> None:
        self.acquired = acquired
        self.calls: list[str] = []

    @contextmanager
    def plan_execution_lock(self, **_kwargs: Any) -> Iterator[bool]:
        self.calls.append("lock")
        yield self.acquired

    def recover_interrupted_steps(self, **kwargs: Any) -> None:
        self.calls.append("recover")
        if "before" in kwargs:
            raise AssertionError("advisory-lock recovery must not use startup time")


class _FixedCoordinator:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def execute_plan(
        self,
        *,
        actor: ActorIdentity,
        plan_id: str,
    ) -> WorkflowExecutionResult:
        del actor
        self.calls.append("execute")
        return WorkflowExecutionResult(plan_id=plan_id, revision=1, results=())


class WorkflowAssistantExecutionRecoveryTests(unittest.TestCase):
    def test_running_claims_and_waiting_jobs_both_consume_dispatch_slots(self) -> None:
        plan = _plan(
            _step("running", status="running"),
            replace(
                _step("waiting", status="waiting_job", background_job_id="job-a"),
                sequence=2,
            ),
            replace(_step("pending", status="pending"), sequence=3),
        )

        self.assertEqual(
            _available_dispatch_slots(plan, max_concurrency=3),
            1,
        )

    def test_paused_lane_reconciles_job_without_dispatching_pending_step(self) -> None:
        waiting = _step(
            "paused-job",
            status="waiting_job",
            background_job_id="job-paused",
        )
        pending = replace(
            _step("pending", status="pending"),
            sequence=2,
            article_task_id="task-b",
        )
        repository = _ReviewRepository(
            replace(
                _plan(waiting, pending),
                paused_project_ids=("project-a",),
            )
        )
        coordinator = WorkflowExecutionCoordinator(
            repository=repository,  # type: ignore[arg-type]
            access=_AllowAccess(),  # type: ignore[arg-type]
            tools=object(),  # type: ignore[arg-type]
            job_status_resolver=lambda _actor, _step: {
                "status": "cancelled",
                "attempts": 1,
            },
        )

        coordinator.execute_plan(
            actor=ActorIdentity("org-a", "user-a"),
            plan_id="plan-a",
        )

        self.assertEqual(repository.plan.steps[0].status, "cancelled")
        self.assertEqual(repository.plan.steps[1].status, "pending")
        self.assertEqual(repository.claim_calls, [])
        self.assertEqual(repository.events[0]["event_kind"], "step_cancelled")

    def test_losing_plan_lock_never_recovers_another_live_worker(self) -> None:
        repository = _LockedRunnerRepository(acquired=False)
        calls: list[str] = []
        runner = WorkflowAssistantRunner(
            repository=repository,  # type: ignore[arg-type]
            coordinator=_FixedCoordinator(calls),  # type: ignore[arg-type]
        )

        runner._run_candidate(  # noqa: SLF001
            WorkflowExecutionCandidate("org-a", "user-a", "plan-a", "running")
        )

        self.assertEqual(repository.calls, ["lock"])
        self.assertEqual(calls, [])

    def test_plan_owner_recovers_before_dispatch_without_time_heuristic(self) -> None:
        repository = _LockedRunnerRepository(acquired=True)
        calls = repository.calls
        runner = WorkflowAssistantRunner(
            repository=repository,  # type: ignore[arg-type]
            coordinator=_FixedCoordinator(calls),  # type: ignore[arg-type]
        )

        runner._run_candidate(  # noqa: SLF001
            WorkflowExecutionCandidate("org-a", "user-a", "plan-a", "running")
        )

        self.assertEqual(calls, ["lock", "recover", "execute"])

    def test_new_hard_gate_does_not_hide_another_active_job(self) -> None:
        waiting = replace(
            _step(
                "active-job",
                status="waiting_job",
                background_job_id="job-active",
            ),
            sequence=1,
            article_task_id="task-b",
        )
        gate = replace(
            _step("package", status="pending"),
            sequence=2,
            action_kind="package_delivery",
            article_task_id="task-a",
            hard_gate=True,
        )
        repository = _GateRepository(_plan(waiting, gate))
        coordinator = WorkflowExecutionCoordinator(
            repository=repository,  # type: ignore[arg-type]
            access=_AllowAccess(),  # type: ignore[arg-type]
            tools=object(),  # type: ignore[arg-type]
        )

        result = coordinator.execute_plan(
            actor=ActorIdentity("org-a", "user-a"),
            plan_id="plan-a",
        )

        self.assertEqual(repository.plan.status, "running")
        self.assertEqual(repository.status_transitions, [])
        self.assertEqual(repository.plan.steps[0].status, "waiting_job")
        self.assertEqual(repository.plan.steps[0].background_job_id, "job-active")
        self.assertEqual(repository.plan.steps[1].status, "waiting_review")
        self.assertFalse(repository.plan.steps[1].human_gate_confirmed)
        self.assertEqual(result.results[0].step_id, "package")
        self.assertEqual(result.results[0].status, "waiting_review")
        self.assertEqual(repository.events[0]["event_kind"], "step_waiting_review")

    def test_ready_gate_is_held_before_plan_stops_for_existing_gates(self) -> None:
        existing_gate = replace(
            _step("existing-gate", status="waiting_review"),
            sequence=1,
            action_kind="package_delivery",
            article_task_id="task-a",
            hard_gate=True,
            standardized_error_code="human_confirmation_required",
        )
        predecessor = replace(
            _step("predecessor", status="pending"),
            sequence=2,
            action_kind="generate_tdk",
            article_task_id="task-b",
        )
        ready_after_predecessor = replace(
            _step("new-gate", status="pending"),
            sequence=3,
            action_kind="package_delivery",
            article_task_id="task-b",
            hard_gate=True,
        )
        blocked_after_gate = replace(
            _step("blocked-write", status="pending"),
            sequence=4,
            action_kind="export_docx",
            article_task_id="task-b",
        )
        repository = _GateRepository(
            _plan(
                existing_gate,
                predecessor,
                ready_after_predecessor,
                blocked_after_gate,
            )
        )
        tools = _RecordingTools()
        coordinator = WorkflowExecutionCoordinator(
            repository=repository,  # type: ignore[arg-type]
            access=_AllowAccess(),  # type: ignore[arg-type]
            tools=tools,  # type: ignore[arg-type]
        )

        first = coordinator.execute_plan(
            actor=ActorIdentity("org-a", "user-a"),
            plan_id="plan-a",
        )

        self.assertEqual(first.results[0].status, "succeeded")
        self.assertEqual(repository.plan.status, "running")
        self.assertEqual(repository.plan.steps[2].status, "pending")
        self.assertEqual(repository.status_transitions, [])
        self.assertEqual(tools.calls, ["generate_tdk"])

        second = coordinator.execute_plan(
            actor=ActorIdentity("org-a", "user-a"),
            plan_id="plan-a",
        )

        self.assertEqual(second.results[0].status, "waiting_review")
        self.assertEqual(repository.plan.status, "waiting_review")
        self.assertEqual(repository.plan.steps[2].status, "waiting_review")
        self.assertFalse(repository.plan.steps[2].human_gate_confirmed)
        self.assertEqual(repository.plan.steps[3].status, "pending")
        self.assertEqual(repository.claim_calls, ["predecessor"])
        self.assertEqual(repository.status_transitions, ["waiting_review"])
        self.assertEqual(tools.calls, ["generate_tdk"])

    def test_reconciled_failure_does_not_hide_an_unapproved_gate(self) -> None:
        failed = replace(
            _step("failed-job", status="failed"),
            sequence=1,
            article_task_id="task-b",
            standardized_error_code="background_job_failed",
        )
        gate = replace(
            _step("package", status="waiting_review"),
            sequence=2,
            action_kind="package_delivery",
            article_task_id="task-a",
            hard_gate=True,
            standardized_error_code="human_confirmation_required",
        )
        repository = _GateRepository(_plan(failed, gate))
        coordinator = WorkflowExecutionCoordinator(
            repository=repository,  # type: ignore[arg-type]
            access=_AllowAccess(),  # type: ignore[arg-type]
            tools=object(),  # type: ignore[arg-type]
        )

        coordinator.execute_plan(
            actor=ActorIdentity("org-a", "user-a"),
            plan_id="plan-a",
        )

        self.assertEqual(repository.plan.status, "waiting_review")
        self.assertEqual(repository.status_transitions, ["waiting_review"])
        self.assertFalse(repository.plan.steps[1].human_gate_confirmed)

    def test_one_failed_article_lane_does_not_stop_another_lane(self) -> None:
        failed = replace(
            _step("failed", status="pending"),
            sequence=1,
            article_task_id="task-a",
        )
        blocked = replace(
            _step("blocked", status="pending"),
            sequence=2,
            article_task_id="task-a",
        )
        independent = replace(
            _step("independent", status="pending"),
            sequence=3,
            article_task_id="task-b",
        )
        repository = _GateRepository(_plan(failed, blocked, independent))
        tools = _FailOneArticleTools()
        coordinator = WorkflowExecutionCoordinator(
            repository=repository,  # type: ignore[arg-type]
            access=_AllowAccess(),  # type: ignore[arg-type]
            tools=tools,  # type: ignore[arg-type]
        )

        result = coordinator.execute_plan(
            actor=ActorIdentity("org-a", "user-a"),
            plan_id="plan-a",
        )

        self.assertEqual(
            {task_id for _action, task_id in tools.calls},
            {"task-a", "task-b"},
        )
        statuses = {step.step_id: step.status for step in repository.plan.steps}
        self.assertEqual(statuses["failed"], "failed")
        self.assertEqual(statuses["blocked"], "skipped")
        self.assertEqual(statuses["independent"], "succeeded")
        self.assertEqual(repository.plan.status, "failed")
        self.assertEqual(
            [item.status for item in result.results],
            ["failed", "succeeded"],
        )

    def test_research_waiting_review_preserves_job_and_safe_projection(self) -> None:
        waiting = _step(
            "research",
            status="waiting_job",
            background_job_id="job-research",
        )
        repository = _ReviewRepository(_plan(waiting))
        status = {
            "status": "waiting_review",
            "job_id": "job-research",
            "attempts": 1,
            "research_thread_id": "thread-a",
            "retrieval_plan_id": "retrieval-a",
            "review_required": True,
        }
        coordinator = WorkflowExecutionCoordinator(
            repository=repository,  # type: ignore[arg-type]
            access=_AllowAccess(),  # type: ignore[arg-type]
            tools=object(),  # type: ignore[arg-type]
            job_status_resolver=lambda _actor, _step: status,
        )

        result = coordinator.reconcile_waiting_jobs(
            actor=ActorIdentity("org-a", "user-a"),
            plan=repository.plan,
        )

        self.assertEqual(result.steps[0].status, "waiting_review")
        self.assertEqual(result.steps[0].background_job_id, "job-research")
        self.assertEqual(result.steps[0].output_summary, status)
        self.assertEqual(
            result.steps[0].standardized_error_code,
            "human_confirmation_required",
        )
        self.assertEqual(repository.finish_calls[0]["retry_count"], 0)
        self.assertEqual(repository.events[0]["event_kind"], "step_waiting_review")
        self.assertEqual(
            repository.events[0]["public_payload"],
            {"step_id": "research", "background_job_id": "job-research"},
        )

    def test_missing_article_result_is_a_specific_terminal_failure(self) -> None:
        waiting = replace(
            _step(
                "article",
                status="waiting_job",
                background_job_id="article-job",
            ),
            action_kind="generate_article",
        )
        repository = _ReviewRepository(_plan(waiting))
        status = {
            "status": "failed",
            "job_id": "article-job",
            "article_result_missing": True,
            "article_ready": False,
        }
        coordinator = WorkflowExecutionCoordinator(
            repository=repository,  # type: ignore[arg-type]
            access=_AllowAccess(),  # type: ignore[arg-type]
            tools=object(),  # type: ignore[arg-type]
            job_status_resolver=lambda _actor, _step: status,
        )

        result = coordinator.reconcile_waiting_jobs(
            actor=ActorIdentity("org-a", "user-a"),
            plan=repository.plan,
        )

        self.assertEqual(result.steps[0].status, "failed")
        self.assertEqual(
            result.steps[0].standardized_error_code,
            "article_result_missing",
        )
        self.assertEqual(
            repository.events[0]["public_payload"]["error_code"],
            "article_result_missing",
        )


if __name__ == "__main__":
    unittest.main()
