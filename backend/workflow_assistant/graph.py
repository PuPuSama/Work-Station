from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping
from typing import Any, TypedDict

from services.access_control import ActorIdentity

from .execution import WorkflowExecutionCoordinator


@dataclass(frozen=True, slots=True)
class WorkflowGraphState:
    plan_id: str
    organization_id: str = ""
    user_id: str = ""
    last_event_sequence: int = 0
    waiting_for_confirmation: bool = False
    waiting_for_job: bool = False
    waiting_for_review: bool = False
    revision: int = 0


class _CheckpointState(TypedDict, total=False):
    """Checkpoint-safe orchestration projection.

    The full plan and step rows stay in PostgreSQL.  LangGraph stores only
    identities and public status flags, so a restart can safely re-enter the
    coordinator without copying prompts, article content, or model traces.
    """

    plan_id: str
    organization_id: str
    user_id: str
    last_event_sequence: int
    execution_started: bool
    waiting_for_confirmation: bool
    waiting_for_job: bool
    waiting_for_review: bool
    revision: int
    results: list[dict[str, Any]]
    error_code: str


class WorkflowAssistantGraph:
    """Small LangGraph adapter; business state remains in PostgreSQL."""

    def __init__(self, coordinator: WorkflowExecutionCoordinator) -> None:
        self._coordinator = coordinator

    def compile(self, *, checkpointer: Any | None = None) -> Any:
        """Compile a graph only when LangGraph is installed.

        The graph contains orchestration nodes only.  It never replaces the
        PostgreSQL plan state machine and never persists model thought traces.
        """

        try:
            from langgraph.graph import END, StateGraph
            from langgraph.types import interrupt
        except ImportError as exc:
            raise RuntimeError("LangGraph is required for workflow execution") from exc

        graph = StateGraph(_CheckpointState)
        checkpoint_enabled = checkpointer is not None

        def execute(state: _CheckpointState) -> dict[str, Any]:
            state = dict(state)
            state["execution_started"] = True
            organization_id = str(state.get("organization_id") or "").strip()
            user_id = str(state.get("user_id") or "").strip()
            plan_id = str(state.get("plan_id") or "").strip()
            if organization_id and user_id and plan_id:
                result = self._coordinator.execute_plan(
                    actor=ActorIdentity(organization_id, user_id),
                    plan_id=plan_id,
                )
                state["revision"] = result.revision
                state["results"] = [
                    {
                        "step_id": item.step_id,
                        "status": item.status,
                        "error_code": item.error_code,
                        **(
                            {"background_job_id": item.background_job_id}
                            if item.background_job_id
                            else {}
                        ),
                    }
                    for item in result.results
                ]
                state["waiting_for_job"] = any(
                    item.status == "waiting_job" for item in result.results
                )
                state["waiting_for_review"] = any(
                    item.status == "waiting_review" for item in result.results
                )
            return state

        def wait_for_review(state: _CheckpointState) -> dict[str, Any]:
            state = dict(state)
            if not state.get("waiting_for_review"):
                return state
            # A Postgres/InMemory checkpointer turns this into a real
            # durable human gate.  The no-checkpointer adapter remains safe
            # for local contract tests: it exposes the waiting state and lets
            # the caller drive the repository transition directly.
            if checkpoint_enabled:
                decision = interrupt(
                    {
                        "kind": "human_confirmation",
                        "plan_id": str(state.get("plan_id") or ""),
                        "reason": "human_confirmation_required",
                    }
                )
                approved = decision is True or (
                    isinstance(decision, Mapping)
                    and decision.get("approved") is True
                )
                state["waiting_for_review"] = not approved
            return state

        def wait_for_job(state: _CheckpointState) -> dict[str, Any]:
            state = dict(state)
            # The process-local runner polls durable Job rows.  This node only
            # records the public wait state in a checkpoint; it must not sleep
            # or duplicate the Server Job queue.
            state["waiting_for_job"] = bool(state.get("waiting_for_job", False))
            return state

        def route_after_execute(state: _CheckpointState) -> str:
            if state.get("waiting_for_review"):
                return "wait_for_review"
            if state.get("waiting_for_job"):
                return "wait_for_job"
            return "finish"

        def route_after_review(state: _CheckpointState) -> str:
            # Without a checkpointer this adapter is only a public-state
            # projection and must terminate instead of looping forever. With
            # a checkpointer, an approved interrupt resumes execution; a
            # rejected/unchanged decision remains a visible wait state.
            if checkpoint_enabled and not state.get("waiting_for_review"):
                return "execute"
            return "finish"

        graph.add_node("execute", execute)
        graph.add_node("wait_for_review", wait_for_review)
        graph.add_node("wait_for_job", wait_for_job)
        graph.add_node("finish", lambda state: dict(state))
        graph.set_entry_point("execute")
        graph.add_conditional_edges(
            "execute",
            route_after_execute,
            {
                "wait_for_review": "wait_for_review",
                "wait_for_job": "wait_for_job",
                "finish": "finish",
            },
        )
        graph.add_conditional_edges(
            "wait_for_review",
            route_after_review,
            {"execute": "execute", "finish": "finish"},
        )
        graph.add_edge("wait_for_job", END)
        graph.add_edge("finish", END)
        return graph.compile(checkpointer=checkpointer)


__all__ = ["WorkflowAssistantGraph", "WorkflowGraphState"]
