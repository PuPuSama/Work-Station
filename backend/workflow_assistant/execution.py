from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from collections.abc import Callable, Mapping
from typing import Any

from services.access_control import (
    ActorIdentity,
    ProjectAccessDenied,
    ProjectAccessService,
)

from .repository import (
    PostgresWorkflowAssistantRepository,
    WorkflowPlan,
    WorkflowPlanStep,
)
from .policy import ALLOWED_ACTION_KINDS
from .tools import (
    WorkflowToolAuthorizationError,
    WorkflowToolError,
    WorkflowToolHumanGateRequired,
    WorkflowToolInvocation,
    WorkflowToolRegistry,
)


class WorkflowExecutionError(RuntimeError):
    """A plan cannot proceed without human or service intervention."""


class WorkflowExecutionConflict(WorkflowExecutionError):
    """Permission, task activity, or revision changed before execution."""


class WorkflowExecutionHumanGate(WorkflowExecutionConflict):
    """A step must wait for a second explicit human confirmation."""


@dataclass(frozen=True, slots=True)
class StepExecutionResult:
    step_id: str
    status: str
    output_summary: dict[str, Any]
    error_code: str | None = None
    background_job_id: str | None = None


@dataclass(frozen=True, slots=True)
class WorkflowExecutionResult:
    plan_id: str
    revision: int
    results: tuple[StepExecutionResult, ...]


JobStatusResolver = Callable[
    [ActorIdentity, WorkflowPlanStep],
    Mapping[str, Any],
]


def _available_dispatch_slots(
    plan: WorkflowPlan,
    *,
    max_concurrency: int,
) -> int:
    limit = min(max_concurrency, plan.concurrency_limit)
    active_steps = sum(
        step.status in {"running", "waiting_job"} for step in plan.steps
    )
    return max(0, limit - active_steps)


def _article_lane_key(step: WorkflowPlanStep) -> tuple[str, str]:
    """Identify one article chain before and after dynamic Task binding."""

    if step.article_task_id:
        return step.project_id, f"task:{step.article_task_id}"
    source_id = str(
        step.input_summary.get("create_task_step_id") or ""
    ).strip()
    if source_id:
        return step.project_id, f"create:{source_id}"
    if step.action_kind == "create_task":
        return step.project_id, f"create:{step.step_id}"
    return step.project_id, "project"


def _should_wait_for_review(plan: WorkflowPlan) -> bool:
    """Return true only when a human gate is the plan's next real blocker.

    A different article chain may reach a gate while durable Jobs from other
    chains are still running.  The plan must remain runnable in that case so
    the coordinator can reconcile those Jobs and continue their downstream
    work.  Likewise, every ready step must get one coordinator pass before the
    whole plan asks for attention: ordinary steps execute, while unconfirmed
    hard gates are only persisted as ``waiting_review``.
    """

    if not any(step.status == "waiting_review" for step in plan.steps):
        return False
    if any(step.status in {"running", "waiting_job"} for step in plan.steps):
        return False
    paused_projects = set(plan.paused_project_ids)
    for step in plan.steps:
        if step.status != "pending" or step.project_id in paused_projects:
            continue
        if not WorkflowExecutionCoordinator._predecessors_succeeded(
            step=step,
            steps=plan.steps,
        ):
            continue
        return False
    return True


def _permission_for_action(action_kind: str) -> str:
    if action_kind in {
        "list_projects",
        "list_tasks",
        "read_project_context",
        "evidence_query",
        "read_plan_status",
    }:
        return "project.view"
    if action_kind in {"review"}:
        # The Assistant review action is a composite operation: generate a
        # review, reject risky suggestions, and apply safe edits. The final
        # commit uses the same article.edit permission as the manual apply
        # endpoint, not the weaker review-only permission.
        return "article.edit"
    if action_kind in {"start_research"}:
        return "knowledge.publish"
    if action_kind in {
        "prepare_images",
        "export_docx",
        "generate_tdk",
        "package_delivery",
    }:
        return "article.deliver"
    return "article.edit"


class WorkflowExecutionCoordinator:
    """Reauthorize and execute already-confirmed typed steps.

    The coordinator deliberately does not know how to build a Job request. A
    write handler is supplied by the existing Server service adapter and can
    therefore enforce its normal Task revision/CAS contract.
    """

    def __init__(
        self,
        *,
        repository: PostgresWorkflowAssistantRepository,
        access: ProjectAccessService,
        tools: WorkflowToolRegistry,
        max_concurrency: int = 3,
        job_status_resolver: JobStatusResolver | None = None,
    ) -> None:
        if not 1 <= max_concurrency <= 32:
            raise ValueError("max_concurrency must be between 1 and 32")
        self._repository = repository
        self._access = access
        self._tools = tools
        self._job_status_resolver = job_status_resolver
        self.max_concurrency = max_concurrency

    def authorize_plan(self, *, actor: ActorIdentity, plan: WorkflowPlan) -> None:
        if plan.status not in {"queued", "running", "paused"}:
            raise WorkflowExecutionConflict("plan is not executable in its current state")
        if plan.status == "paused":
            raise WorkflowExecutionConflict("plan is paused")
        if not plan.approved_by or plan.approved_by != actor.user_id:
            raise WorkflowExecutionConflict("plan has not been confirmed by this actor")
        for project_id in plan.project_ids:
            try:
                self._access.require(actor, project_id, "project.view")
            except (ProjectAccessDenied, ValueError) as exc:
                raise WorkflowExecutionConflict("plan project authorization changed") from exc
        for step in plan.steps:
            permission = _permission_for_action(step.action_kind)
            try:
                self._access.require(actor, step.project_id, permission)  # type: ignore[arg-type]
            except (ProjectAccessDenied, ValueError) as exc:
                raise WorkflowExecutionConflict("step project authorization changed") from exc

    def execute_plan(
        self,
        *,
        actor: ActorIdentity,
        plan_id: str,
    ) -> WorkflowExecutionResult:
        plan = self._repository.get_plan(actor=actor, plan_id=plan_id)
        # Re-authorize before changing the durable plan state.  A revoked
        # actor must not leave a queued plan marked as running merely because
        # the worker reached its dispatch boundary.
        self.authorize_plan(actor=actor, plan=plan)
        if plan.status == "queued":
            plan = self._repository.set_plan_status(
                actor=actor,
                plan_id=plan.plan_id,
                expected_revision=plan.revision,
                new_status="running",
            )
        self.authorize_plan(actor=actor, plan=plan)
        plan = self.reconcile_waiting_jobs(actor=actor, plan=plan)
        # A failed step only closes the suffix of its own article lane.  Keep
        # that boundary durable before selecting the next wave so unrelated
        # article lanes can continue in the same plan.
        plan = self._skip_failed_step_dependents(actor=actor, plan=plan)
        paused_projects = set(plan.paused_project_ids)
        pending = tuple(
            step
            for step in plan.steps
            if step.status == "pending" and step.project_id not in paused_projects
        )
        if not pending:
            return self._finalize_plan(actor=actor, plan=plan, results=())

        # A plan sequence is global for display/audit, but article steps from
        # different projects/tasks may run in parallel. Enforce ordering
        # within the same project/task chain so a later write cannot race its
        # prerequisite title, outline, or article revision.
        ready = tuple(
            step
            for step in pending
            if self._predecessors_succeeded(step=step, steps=plan.steps)
        )
        hard_gates = tuple(
            step
            for step in ready
            if step.hard_gate and not step.human_gate_confirmed
        )
        pending = tuple(step for step in ready if step not in hard_gates)
        if hard_gates and not pending:
            for step in hard_gates:
                self._repository.hold_step_for_review(
                    actor=actor,
                    plan_id=plan.plan_id,
                    step_id=step.step_id,
                )
            plan = self._repository.get_plan(actor=actor, plan_id=plan.plan_id)
            results = tuple(
                StepExecutionResult(
                    step_id=step.step_id,
                    status="waiting_review",
                    output_summary={},
                    error_code="human_confirmation_required",
                )
                for step in hard_gates
            )
            finalized = self._finalize_plan(
                actor=actor,
                plan=plan,
                results=results,
            )
            for step in hard_gates:
                self._repository.append_event(
                    actor=actor,
                    plan_id=plan.plan_id,
                    event_kind="step_waiting_review",
                    public_payload={"step_id": step.step_id},
                )
            return finalized

        if not pending:
            return self._finalize_plan(actor=actor, plan=plan, results=())

        # A queued Server Job outlives the short coordinator invocation that
        # launched it. Count both process-local running claims and durable
        # waiting steps before dispatching a later wave; limiting only
        # ThreadPool workers allowed another process to add Jobs while earlier
        # work was still active.
        concurrency_limit = min(self.max_concurrency, plan.concurrency_limit)
        available_slots = _available_dispatch_slots(
            plan,
            max_concurrency=self.max_concurrency,
        )
        if available_slots == 0:
            return self._finalize_plan(actor=actor, plan=plan, results=())
        pending = pending[:available_slots]

        results: list[StepExecutionResult] = []
        with ThreadPoolExecutor(
            max_workers=min(concurrency_limit, len(pending)),
            thread_name_prefix="workflow-assistant",
        ) as executor:
            futures: dict[Future[StepExecutionResult], WorkflowPlanStep] = {}
            for step in pending:
                if not self._repository.claim_step(
                    actor=actor,
                    plan_id=plan.plan_id,
                    step_id=step.step_id,
                ):
                    continue
                futures[
                    executor.submit(
                        self._execute_step,
                        actor,
                        plan,
                        step,
                    )
                ] = step
            for future in as_completed(futures):
                step = futures[future]
                try:
                    result = future.result()
                    # The underlying Server service performs its own
                    # authorization before submission.  Check the same
                    # project/action permission again at the assistant commit
                    # boundary so a revocation cannot be hidden by a late
                    # worker result.
                    self._reauthorize_step(actor=actor, step=step)
                    if result.status == "succeeded":
                        self._bind_created_task_steps(
                            actor=actor,
                            plan=plan,
                            step=step,
                            output=result.output_summary,
                        )
                    committed = self._repository.finish_step(
                        actor=actor,
                        plan_id=plan.plan_id,
                        step_id=step.step_id,
                        status=result.status,  # type: ignore[arg-type]
                        output_summary=result.output_summary,
                        background_job_id=result.background_job_id,
                    )
                    if not committed:
                        # A concurrent cancellation closes running steps
                        # before this late result can be committed.  Do not
                        # append a success event or advance a Task revision
                        # after the plan has crossed that terminal boundary.
                        results.append(
                            StepExecutionResult(
                                step_id=step.step_id,
                                status="cancelled",
                                output_summary={},
                                error_code="plan_cancelled",
                            )
                        )
                        continue
                    if result.status in {"succeeded", "skipped"}:
                        self._advance_task_chain_revision(
                            actor=actor,
                            plan=plan,
                            step=step,
                            output=result.output_summary,
                        )
                    self._repository.append_event(
                        actor=actor,
                        plan_id=plan.plan_id,
                        event_kind=(
                            "step_waiting_job"
                            if result.status == "waiting_job"
                            else (
                                "step_skipped"
                                if result.status == "skipped"
                                else "step_succeeded"
                            )
                        ),
                        public_payload={
                            "step_id": step.step_id,
                            **(
                                {"background_job_id": result.background_job_id}
                                if result.background_job_id
                                else {}
                            ),
                        },
                    )
                    results.append(result)
                except WorkflowExecutionHumanGate as exc:
                    committed = self._repository.finish_step(
                        actor=actor,
                        plan_id=plan.plan_id,
                        step_id=step.step_id,
                        status="waiting_review",
                        standardized_error_code="human_confirmation_required",
                    )
                    if not committed:
                        results.append(
                            StepExecutionResult(
                                step_id=step.step_id,
                                status="cancelled",
                                output_summary={},
                                error_code="plan_cancelled",
                            )
                        )
                        continue
                    self._repository.append_event(
                        actor=actor,
                        plan_id=plan.plan_id,
                        event_kind="step_waiting_review",
                        public_payload={"step_id": step.step_id},
                    )
                    results.append(
                        StepExecutionResult(
                            step_id=step.step_id,
                            status="waiting_review",
                            output_summary={},
                            error_code=str(exc),
                        )
                    )
                except WorkflowExecutionError as exc:
                    committed = self._repository.finish_step(
                        actor=actor,
                        plan_id=plan.plan_id,
                        step_id=step.step_id,
                        status="failed",
                        standardized_error_code=type(exc).__name__,
                    )
                    if not committed:
                        results.append(
                            StepExecutionResult(
                                step_id=step.step_id,
                                status="cancelled",
                                output_summary={},
                                error_code="plan_cancelled",
                            )
                        )
                        continue
                    self._repository.append_event(
                        actor=actor,
                        plan_id=plan.plan_id,
                        event_kind="step_failed",
                        public_payload={
                            "step_id": step.step_id,
                            "error_code": type(exc).__name__,
                        },
                    )
                    results.append(
                        StepExecutionResult(
                            step_id=step.step_id,
                            status="failed",
                            output_summary={},
                            error_code=type(exc).__name__,
                        )
                    )
        plan = self._repository.get_plan(actor=actor, plan_id=plan.plan_id)
        plan = self._skip_failed_step_dependents(actor=actor, plan=plan)
        return self._finalize_plan(
            actor=actor,
            plan=plan,
            results=tuple(sorted(results, key=lambda result: result.step_id)),
        )

    def _skip_failed_step_dependents(
        self,
        *,
        actor: ActorIdentity,
        plan: WorkflowPlan,
    ) -> WorkflowPlan:
        """Persist blocked suffixes without stopping other article lanes."""

        skip_blocked = getattr(
            self._repository,
            "skip_steps_blocked_by_failure",
            None,
        )
        if not callable(skip_blocked):
            # Keep small test/double repositories compatible with the
            # coordinator. The production PostgreSQL repository always
            # provides this durable transition.
            return plan
        changed = False
        for step in plan.steps:
            if step.status != "failed":
                continue
            blocked = skip_blocked(
                actor=actor,
                plan_id=plan.plan_id,
                failed_step_id=step.step_id,
            )
            changed = changed or bool(blocked)
        if not changed:
            return plan
        return self._repository.get_plan(actor=actor, plan_id=plan.plan_id)

    def reconcile_waiting_jobs(
        self,
        *,
        actor: ActorIdentity,
        plan: WorkflowPlan,
    ) -> WorkflowPlan:
        """Re-authorize and commit terminal states from existing Server Jobs."""

        waiting = tuple(
            step
            for step in plan.steps
            if step.status == "waiting_job"
        )
        if not waiting:
            return plan
        if self._job_status_resolver is None:
            return plan
        self.authorize_plan(actor=actor, plan=plan)
        for step in waiting:
            try:
                status = dict(self._job_status_resolver(actor, step))
                job_status = str(status.get("status") or "").strip()
                if job_status in {"queued", "running", "retry_wait"}:
                    continue
                if job_status == "waiting_review":
                    self._reauthorize_step(actor=actor, step=step)
                    committed = self._repository.finish_step(
                        actor=actor,
                        plan_id=plan.plan_id,
                        step_id=step.step_id,
                        status="waiting_review",
                        output_summary=status,
                        standardized_error_code="human_confirmation_required",
                        background_job_id=step.background_job_id,
                        retry_count=self._retry_count(status),
                        human_gate_confirmed=False,
                    )
                    if not committed:
                        continue
                    self._repository.append_event(
                        actor=actor,
                        plan_id=plan.plan_id,
                        event_kind="step_waiting_review",
                        public_payload={
                            "step_id": step.step_id,
                            **(
                                {"background_job_id": step.background_job_id}
                                if step.background_job_id
                                else {}
                            ),
                        },
                    )
                    continue
                if job_status == "succeeded":
                    self._reauthorize_step(actor=actor, step=step)
                    committed = self._repository.finish_step(
                        actor=actor,
                        plan_id=plan.plan_id,
                        step_id=step.step_id,
                        status="succeeded",
                        output_summary=status,
                        background_job_id=step.background_job_id,
                        retry_count=self._retry_count(status),
                    )
                    if not committed:
                        # Cancellation can close a waiting step while the
                        # underlying Job is reaching a terminal state.  Do
                        # not publish a late success or advance a Task CAS
                        # revision after that durable boundary.
                        continue
                    self._advance_task_chain_revision(
                        actor=actor,
                        plan=plan,
                        step=step,
                        output=status,
                    )
                    self._repository.append_event(
                        actor=actor,
                        plan_id=plan.plan_id,
                        event_kind="step_succeeded",
                        public_payload={"step_id": step.step_id},
                    )
                    continue
                if job_status in {"failed", "cancelled", "conflict"}:
                    committed = self._repository.finish_step(
                        actor=actor,
                        plan_id=plan.plan_id,
                        step_id=step.step_id,
                        status=(
                            "cancelled"
                            if job_status == "cancelled"
                            else "failed"
                        ),
                        output_summary=status,
                        standardized_error_code=(
                            "background_job_failed"
                            if job_status == "failed"
                            else (
                                "background_job_cancelled"
                                if job_status == "cancelled"
                                else "background_job_conflict"
                            )
                        ),
                        background_job_id=step.background_job_id,
                        retry_count=self._retry_count(status),
                    )
                    if not committed:
                        continue
                    self._repository.append_event(
                        actor=actor,
                        plan_id=plan.plan_id,
                        event_kind=(
                            "step_failed"
                            if job_status in {"failed", "conflict"}
                            else "step_cancelled"
                        ),
                        public_payload={
                            "step_id": step.step_id,
                            "error_code": (
                                "background_job_failed"
                                if job_status == "failed"
                                else (
                                    "background_job_cancelled"
                                    if job_status == "cancelled"
                                    else "background_job_conflict"
                                )
                            ),
                        },
                    )
                    continue
                raise WorkflowExecutionError("background job returned an unknown status")
            except WorkflowExecutionConflict:
                committed = self._repository.finish_step(
                    actor=actor,
                    plan_id=plan.plan_id,
                    step_id=step.step_id,
                    status="failed",
                    standardized_error_code="authorization_changed",
                    background_job_id=step.background_job_id,
                )
                if committed:
                    self._repository.append_event(
                        actor=actor,
                        plan_id=plan.plan_id,
                        event_kind="step_failed",
                        public_payload={
                            "step_id": step.step_id,
                            "error_code": "authorization_changed",
                        },
                    )
            except Exception:
                committed = self._repository.finish_step(
                    actor=actor,
                    plan_id=plan.plan_id,
                    step_id=step.step_id,
                    status="failed",
                    standardized_error_code="background_job_status_unavailable",
                    background_job_id=step.background_job_id,
                )
                if committed:
                    self._repository.append_event(
                        actor=actor,
                        plan_id=plan.plan_id,
                        event_kind="step_failed",
                        public_payload={
                            "step_id": step.step_id,
                            "error_code": "background_job_status_unavailable",
                        },
                    )
        return self._repository.get_plan(actor=actor, plan_id=plan.plan_id)

    def _reauthorize_step(
        self,
        *,
        actor: ActorIdentity,
        step: WorkflowPlanStep,
    ) -> None:
        try:
            self._access.require(
                actor,
                step.project_id,
                _permission_for_action(step.action_kind),
            )
        except Exception as exc:
            raise WorkflowExecutionConflict(
                "step project authorization changed before commit"
            ) from exc

    @staticmethod
    def _retry_count(status: Mapping[str, Any]) -> int | None:
        attempts = status.get("attempts")
        if isinstance(attempts, bool) or not isinstance(attempts, int):
            return None
        return max(0, attempts - 1)

    def _advance_task_chain_revision(
        self,
        *,
        actor: ActorIdentity,
        plan: WorkflowPlan,
        step: WorkflowPlanStep,
        output: Mapping[str, Any],
    ) -> None:
        if not step.article_task_id or step.expected_task_revision is None:
            return
        value = output.get("result_revision")
        if isinstance(value, bool) or not isinstance(value, int):
            return
        if value <= step.expected_task_revision:
            return
        self._repository.advance_task_chain_revision(
            actor=actor,
            plan_id=plan.plan_id,
            project_id=step.project_id,
            article_task_id=step.article_task_id,
            after_sequence=step.sequence,
            from_revision=step.expected_task_revision,
            to_revision=value,
        )

    def _bind_created_task_steps(
        self,
        *,
        actor: ActorIdentity,
        plan: WorkflowPlan,
        step: WorkflowPlanStep,
        output: Mapping[str, Any],
    ) -> None:
        """Attach intake-created Task IDs before later steps are claimed."""

        if step.action_kind != "create_task":
            return
        raw_task_ids = output.get("task_ids")
        raw_revisions = output.get("revisions")
        if not isinstance(raw_task_ids, list) or not isinstance(raw_revisions, list):
            raise WorkflowExecutionError(
                "created Task result did not contain identities"
            )
        targets = tuple(
            candidate
            for candidate in plan.steps
            if candidate.status == "pending"
            and str(candidate.input_summary.get("create_task_step_id") or "")
            == step.step_id
        )
        if not targets:
            return
        if len(raw_task_ids) == 1 and len(targets) > 1:
            resolved_task_ids = raw_task_ids * len(targets)
            resolved_revisions = raw_revisions * len(targets)
        elif len(raw_task_ids) == len(targets) and len(raw_revisions) == len(targets):
            resolved_task_ids = raw_task_ids
            resolved_revisions = raw_revisions
        else:
            raise WorkflowExecutionError(
                "created Task count does not match the planned article suffix"
            )
        assignments: list[tuple[str, str, int]] = []
        for target, raw_task_id, raw_revision in zip(
            targets,
            resolved_task_ids,
            resolved_revisions,
            strict=True,
        ):
            if not isinstance(raw_task_id, str) or not raw_task_id.strip():
                raise WorkflowExecutionError("created Task identity is invalid")
            if isinstance(raw_revision, bool) or not isinstance(raw_revision, int):
                raise WorkflowExecutionError("created Task revision is invalid")
            assignments.append(
                (target.step_id, raw_task_id.strip(), raw_revision)
            )
        self._repository.bind_created_task_steps(
            actor=actor,
            plan_id=plan.plan_id,
            create_step_id=step.step_id,
            assignments=assignments,
        )

    @staticmethod
    def _predecessors_succeeded(
        *,
        step: WorkflowPlanStep,
        steps: tuple[WorkflowPlanStep, ...],
    ) -> bool:
        key = _article_lane_key(step)
        terminal = {"succeeded", "skipped"}
        return all(
            previous.status in terminal
            for previous in steps
            if previous.sequence < step.sequence
            and _article_lane_key(previous) == key
        )

    def _finalize_plan(
        self,
        *,
        actor: ActorIdentity,
        plan: WorkflowPlan,
        results: tuple[StepExecutionResult, ...],
    ) -> WorkflowExecutionResult:
        """Derive the public plan status from persisted step states."""

        if plan.status in {"queued", "running"}:
            statuses = {step.status for step in plan.steps}
            paused_projects = set(plan.paused_project_ids)
            has_ready_pending = any(
                step.status == "pending"
                and step.project_id not in paused_projects
                and self._predecessors_succeeded(step=step, steps=plan.steps)
                for step in plan.steps
            )
            active = bool(statuses & {"running", "waiting_job"}) or has_ready_pending
            if active:
                # There may be a failed article lane while other lanes still
                # have work to dispatch. Pending steps whose predecessor is
                # failed or waiting for review are not active work and do not
                # prevent the plan from surfacing that review state.
                pass
            elif _should_wait_for_review(plan):
                plan = self._repository.set_plan_status(
                    actor=actor,
                    plan_id=plan.plan_id,
                    expected_revision=plan.revision,
                    new_status="waiting_review",
                )
            elif "failed" in statuses and statuses.issubset(
                {"succeeded", "skipped", "failed", "cancelled"}
            ):
                plan = self._repository.set_plan_status(
                    actor=actor,
                    plan_id=plan.plan_id,
                    expected_revision=plan.revision,
                    new_status="failed",
                )
            elif statuses and statuses.issubset({"succeeded", "skipped", "cancelled"}):
                plan = self._repository.set_plan_status(
                    actor=actor,
                    plan_id=plan.plan_id,
                    expected_revision=plan.revision,
                    new_status="completed",
                )
        return WorkflowExecutionResult(
            plan_id=plan.plan_id,
            revision=plan.revision,
            results=results,
        )

    def _execute_step(
        self,
        actor: ActorIdentity,
        plan: WorkflowPlan,
        step: WorkflowPlanStep,
    ) -> StepExecutionResult:
        action_kind = step.action_kind
        if action_kind not in ALLOWED_ACTION_KINDS:
            raise WorkflowExecutionConflict("unknown plan action")
        try:
            output = self._tools.invoke(
                WorkflowToolInvocation(
                    actor=actor,
                    plan_id=plan.plan_id,
                    step_id=step.step_id,
                    action_kind=action_kind,  # type: ignore[arg-type]
                    project_id=step.project_id,
                    article_task_id=step.article_task_id,
                    expected_task_revision=step.expected_task_revision,
                    input_summary=step.input_summary,
                    pinned_prompt_version=step.pinned_prompt_version,
                    pinned_knowledge_snapshot=step.pinned_knowledge_snapshot,
                    hard_gate=step.hard_gate,
                    confirmed=bool(plan.approved_by),
                    human_gate_confirmed=step.human_gate_confirmed,
                )
            )
        except WorkflowToolHumanGateRequired as exc:
            raise WorkflowExecutionHumanGate(str(exc)) from exc
        except WorkflowToolAuthorizationError as exc:
            raise WorkflowExecutionConflict(str(exc)) from exc
        except WorkflowToolError as exc:
            raise WorkflowExecutionError(str(exc)) from exc
        workflow_status = str(output.get("_workflow_status") or "succeeded")
        if workflow_status not in {"succeeded", "waiting_job", "skipped"}:
            raise WorkflowExecutionError("workflow tool returned an invalid step status")
        background_job_id = output.get("job_id")
        public_output = {
            str(key): value
            for key, value in output.items()
            if not str(key).startswith("_")
        }
        return StepExecutionResult(
            step_id=step.step_id,
            status=workflow_status,
            output_summary=public_output,
            background_job_id=(
                str(background_job_id).strip()
                if background_job_id is not None
                and str(background_job_id).strip()
                else None
            ),
        )


__all__ = [
    "StepExecutionResult",
    "WorkflowExecutionConflict",
    "WorkflowExecutionCoordinator",
    "WorkflowExecutionError",
    "WorkflowExecutionHumanGate",
    "WorkflowExecutionResult",
    "JobStatusResolver",
]
