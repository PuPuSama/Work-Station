from __future__ import annotations

import logging
from contextlib import nullcontext
from dataclasses import dataclass
from threading import Event, Thread
from typing import Callable
from uuid import uuid4

from services.access_control import ActorIdentity
from services.job_queue import is_retryable_error

from .execution import WorkflowExecutionCoordinator
from .graph import WorkflowAssistantGraph
from .repository import (
    PostgresWorkflowAssistantRepository,
    WorkflowAssistantConflict,
    WorkflowAssistantDispatch,
    WorkflowExecutionCandidate,
)


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WorkflowAssistantRunnerStopReport:
    stopped: bool
    alive: bool


class WorkflowAssistantRunner:
    """Durable process-local dispatcher for confirmed assistant plans.

    The plan, step, and Job rows remain the source of truth. The thread only
    wakes the coordinator; after a restart it discovers queued/running plans
    again from PostgreSQL and re-authorizes the creator before every dispatch.
    """

    def __init__(
        self,
        *,
        repository: PostgresWorkflowAssistantRepository,
        coordinator: WorkflowExecutionCoordinator,
        database_url: str | None = None,
        poll_interval_seconds: float = 1.0,
        planning_dispatcher: Callable[[WorkflowAssistantDispatch, str], None]
        | None = None,
    ) -> None:
        if not 0.1 <= poll_interval_seconds <= 60.0:
            raise ValueError("poll_interval_seconds must be between 0.1 and 60")
        self._repository = repository
        self._coordinator = coordinator
        self._database_url = str(database_url or "").strip()
        self._poll_interval_seconds = float(poll_interval_seconds)
        self._worker_id = f"workflow-assistant-{uuid4().hex}"
        self._planning_dispatcher = planning_dispatcher
        self._stop = Event()
        self._wake_event = Event()
        self._thread: Thread | None = None
        self._stop_report: WorkflowAssistantRunnerStopReport | None = None
        self._graph = None

    @property
    def running(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._wake_event.set()
        self._thread = Thread(
            target=self._run,
            name="workflow-assistant-runner",
            daemon=True,
        )
        self._thread.start()

    def wake(self) -> None:
        """Prompt the dispatcher after a confirmation or external Job event."""

        self._wake_event.set()

    def stop(self, *, timeout_seconds: float = 10.0) -> WorkflowAssistantRunnerStopReport:
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative")
        self._stop.set()
        self._wake_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout_seconds)
        report = WorkflowAssistantRunnerStopReport(
            stopped=True,
            alive=bool(thread is not None and thread.is_alive()),
        )
        self._stop_report = report
        return report

    def _run(self) -> None:
        if not self._database_url:
            self._poll()
            return

        try:
            from langgraph.checkpoint.postgres import PostgresSaver
        except ImportError:
            LOGGER.warning(
                "LangGraph Postgres checkpointer is unavailable; "
                "using the PostgreSQL workflow state machine directly"
            )
            self._poll()
            return

        connection_url = self._database_url.replace(
            "postgresql+psycopg://",
            "postgresql://",
            1,
        )
        while not self._stop.is_set():
            try:
                with PostgresSaver.from_conn_string(connection_url) as checkpointer:
                    self._graph = WorkflowAssistantGraph(
                        self._coordinator
                    ).compile(checkpointer=checkpointer)
                    self._poll()
                return
            except Exception as exc:
                self._graph = None
                if self._stop.is_set():
                    return
                LOGGER.warning(
                    "workflow assistant checkpointer unavailable; "
                    "using PostgreSQL state machine fallback (%s)",
                    type(exc).__name__,
                )
                LOGGER.debug(
                    "workflow assistant checkpointer failure details",
                    exc_info=True,
                )
                # LangGraph is an execution checkpoint enhancement. The
                # PostgreSQL plan/step CAS state machine remains authoritative
                # and can safely continue when a checkpointer extension is
                # unavailable (for example, an un-migrated local database).
                # This also prevents a missing checkpointer table from
                # stopping all confirmed plans forever.
                self._poll()
                return

    def _poll(self) -> None:
        while not self._stop.is_set():
            self._drain_once()
            self._wake_event.wait(self._poll_interval_seconds)
            self._wake_event.clear()

    def _drain_once(self) -> None:
        self._drain_planning_dispatches()
        try:
            candidates = self._repository.list_execution_candidates()
        except Exception as exc:
            # A transient database failure must not kill the dispatcher. The
            # next wake/poll will rediscover the durable plan.
            LOGGER.warning(
                "workflow assistant execution candidate scan failed: type=%s",
                type(exc).__name__,
            )
            return
        for candidate in candidates:
            if self._stop.is_set():
                return
            self._run_candidate(candidate)

    def _drain_planning_dispatches(self) -> None:
        dispatcher = self._planning_dispatcher
        if dispatcher is None:
            return
        try:
            candidates = self._repository.list_planning_dispatches(limit=20)
        except Exception as exc:
            # The normal plan dispatcher must continue to be useful when a
            # migration is being applied or the database briefly refuses a
            # connection. The next poll will retry the planning inbox.
            LOGGER.warning(
                "workflow assistant planning dispatch scan failed: type=%s",
                type(exc).__name__,
            )
            return
        for candidate in candidates:
            if self._stop.is_set():
                return
            try:
                claimed = self._repository.claim_planning_dispatch(
                    dispatch_id=candidate.dispatch_id,
                    worker_id=self._worker_id,
                )
            except Exception as exc:
                LOGGER.warning(
                    "workflow assistant planning dispatch claim failed: "
                    "dispatch_id=%s type=%s",
                    candidate.dispatch_id,
                    type(exc).__name__,
                )
                continue
            if claimed is None:
                continue
            try:
                dispatcher(claimed, self._worker_id)
            except Exception as exc:
                # Planner implementations already perform short provider
                # retries. Keep the durable hand-off retryable across a
                # proxy timeout, worker restart, or transient pool failure.
                try:
                    self._repository.fail_planning_dispatch(
                        dispatch=claimed,
                        worker_id=self._worker_id,
                        error_code=type(exc).__name__,
                        retry_delay_seconds=min(
                            30,
                            2 ** max(0, claimed.attempts - 1),
                        ),
                    )
                except Exception:
                    LOGGER.warning(
                        "workflow assistant planning failure could not be persisted: "
                        "dispatch_id=%s",
                        claimed.dispatch_id,
                        exc_info=True,
                    )

    def _run_candidate(self, candidate: WorkflowExecutionCandidate) -> None:
        actor = ActorIdentity(
            candidate.organization_id,
            candidate.creator_user_id,
        )
        lock = getattr(self._repository, "plan_execution_lock", None)
        try:
            guard = (
                lock(actor=actor, plan_id=candidate.plan_id)
                if callable(lock)
                else nullcontext(True)
            )
            with guard as acquired:
                if not acquired:
                    return
                self._run_candidate_under_lock(candidate=candidate, actor=actor)
        except Exception as exc:
            # A transient lock connection failure does not change durable
            # plan state. Another poll or process can safely retry.
            LOGGER.warning(
                "workflow assistant plan dispatch guard failed: "
                "plan_id=%s type=%s",
                candidate.plan_id,
                type(exc).__name__,
            )
            return

    def _run_candidate_under_lock(
        self,
        *,
        candidate: WorkflowExecutionCandidate,
        actor: ActorIdentity,
    ) -> None:
        try:
            recover = getattr(
                self._repository,
                "recover_interrupted_steps",
                None,
            )
            if callable(recover):
                recover(
                    actor=actor,
                    plan_id=candidate.plan_id,
                )
            if self._graph is None:
                self._coordinator.execute_plan(
                    actor=actor,
                    plan_id=candidate.plan_id,
                )
            else:
                self._invoke_graph(candidate=candidate, actor=actor)
        except WorkflowAssistantConflict as exc:
            # Multiple web workers may discover the same queued plan.  The
            # first worker that advances queued -> running owns execution;
            # a stale dispatcher must not turn that expected CAS conflict
            # into a plan-level failure.
            try:
                current = self._repository.get_plan(
                    actor=actor,
                    plan_id=candidate.plan_id,
                )
            except Exception:
                current = None
            if (
                candidate.status == "queued"
                and current is not None
                and current.status in {"queued", "running"}
            ):
                # A pause/resume of one project lane increments the plan CAS
                # revision without changing the overall queued/running
                # state. Let the next dispatcher pass reload that durable
                # lane state instead of turning the expected race into a
                # plan failure.
                return
            self._mark_failed(
                actor=actor,
                plan_id=candidate.plan_id,
                error_code=type(exc).__name__,
            )
        except Exception as exc:
            if is_retryable_error(exc):
                LOGGER.warning(
                    "workflow assistant dispatch temporarily unavailable; "
                    "plan will be retried: plan_id=%s type=%s",
                    candidate.plan_id,
                    type(exc).__name__,
                )
                return
            LOGGER.warning(
                "workflow assistant plan execution failed: "
                "plan_id=%s type=%s",
                candidate.plan_id,
                type(exc).__name__,
            )
            self._mark_failed(
                actor=actor,
                plan_id=candidate.plan_id,
                error_code=type(exc).__name__,
            )

    def _mark_failed(
        self,
        *,
        actor: ActorIdentity,
        plan_id: str,
        error_code: str,
    ) -> None:
        try:
            plan = self._repository.get_plan(actor=actor, plan_id=plan_id)
            if plan.status not in {"queued", "running"}:
                return
            self._repository.append_event(
                actor=actor,
                plan_id=plan_id,
                event_kind="runner_failed",
                public_payload={"error_code": error_code},
            )
            self._repository.set_plan_status(
                actor=actor,
                plan_id=plan_id,
                expected_revision=plan.revision,
                new_status="failed",
            )
        except Exception as exc:
            # The original failure is already represented by the durable Job
            # or step where possible. Never let error reporting crash the
            # process-wide dispatcher.
            LOGGER.warning(
                "workflow assistant plan failure could not be persisted: "
                "plan_id=%s type=%s",
                plan_id,
                type(exc).__name__,
            )
            return

    def _invoke_graph(
        self,
        *,
        candidate: WorkflowExecutionCandidate,
        actor: ActorIdentity,
    ) -> None:
        graph = self._graph
        if graph is None:
            raise RuntimeError("workflow graph is unavailable")
        config = {
            "configurable": {
                "thread_id": (
                    "workflow-assistant:"
                    f"{candidate.organization_id}:"
                    f"{candidate.creator_user_id}:"
                    f"{candidate.plan_id}"
                )
            }
        }
        snapshot = graph.get_state(config)
        if "wait_for_review" in tuple(getattr(snapshot, "next", ())):
            plan = self._repository.get_plan(
                actor=actor,
                plan_id=candidate.plan_id,
            )
            approved = any(
                step.hard_gate
                and step.human_gate_confirmed
                and step.status == "pending"
                for step in plan.steps
            )
            if approved:
                from langgraph.types import Command

                graph.invoke(
                    Command(resume={"approved": True}),
                    config=config,
                )
                return
            # A plain pause/resume must not approve an interrupted human gate.
            self._coordinator.execute_plan(
                actor=actor,
                plan_id=candidate.plan_id,
            )
            return
        graph.invoke(
            {
                "plan_id": candidate.plan_id,
                "organization_id": candidate.organization_id,
                "user_id": candidate.creator_user_id,
                "last_event_sequence": 0,
                "waiting_for_confirmation": False,
                "waiting_for_job": False,
                "waiting_for_review": False,
                "revision": 0,
            },
            config=config,
        )


__all__ = [
    "WorkflowAssistantRunner",
    "WorkflowAssistantRunnerStopReport",
]
