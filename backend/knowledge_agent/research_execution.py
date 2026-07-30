from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator, Sequence

from langgraph.checkpoint.postgres import PostgresSaver

from .checkpoint_setup import psycopg_connection_url
from .research_graph import (
    BoundedResearchGraph,
    CandidateIngestionPort,
    OfficialDiscoveryPort,
    ResearchGraphRequest,
    ResearchTelemetryPort,
    RetrievalPlanPort,
    ScopeEvidencePort,
)
from .research_runs import (
    PostgresResearchRunRepository,
    ResearchGraphRun,
    ResearchRunConflictError,
    ResearchRunNotFound,
)


class ResearchExecutionError(RuntimeError):
    """Public execution failure whose message contains no provider details."""


class ResearchGraphSessionFactory:
    """Open one thread-safe Checkpointer connection per execution/query call."""

    def __init__(
        self,
        *,
        database_url: str,
        plans: RetrievalPlanPort,
        evidence: ScopeEvidencePort,
        discovery: OfficialDiscoveryPort,
        ingestion: CandidateIngestionPort,
        telemetry: ResearchTelemetryPort | None = None,
    ) -> None:
        self._database_url = psycopg_connection_url(database_url)
        self._plans = plans
        self._evidence = evidence
        self._discovery = discovery
        self._ingestion = ingestion
        self._telemetry = telemetry

    @contextmanager
    def open(self) -> Iterator[BoundedResearchGraph]:
        # setup() intentionally does not run here. Deployment/maintenance owns it.
        with PostgresSaver.from_conn_string(self._database_url) as checkpointer:
            yield BoundedResearchGraph(
                plans=self._plans,
                evidence=self._evidence,
                discovery=self._discovery,
                ingestion=self._ingestion,
                checkpointer=checkpointer,
                telemetry=self._telemetry,
            )


class ResearchGraphExecutionService:
    """Synchronize queue execution, LangGraph checkpoints, and the business view."""

    def __init__(
        self,
        *,
        sessions: ResearchGraphSessionFactory,
        runs: PostgresResearchRunRepository,
    ) -> None:
        self._sessions = sessions
        self._runs = runs

    def enqueue(
        self,
        request: ResearchGraphRequest,
        *,
        metadata: dict[str, object] | None = None,
    ) -> ResearchGraphRun:
        run = self._runs.create_run(request, metadata=metadata)
        if not self._runs.list_events(request.project_id, request.thread_id):
            self._runs.append_event(
                project_id=request.project_id,
                thread_id=request.thread_id,
                event_type="queued",
                node_name="queued",
                details={"outline_version": request.outline_version},
            )
        return run

    def execute_start(self, request: ResearchGraphRequest) -> ResearchGraphRun:
        run = self._runs.get_run(request.project_id, request.thread_id)
        if run is None:
            run = self.enqueue(request)
        if run.status in {"completed", "completed_with_warnings"}:
            return run
        if run.status in {"failed", "cancelled"}:
            raise ResearchRunConflictError("terminal research run cannot be restarted")
        if run.status == "waiting_for_review":
            return run

        self._runs.mark_started(request.project_id, request.thread_id)
        try:
            with self._sessions.open() as graph:
                checkpoint_state = graph.state(request.thread_id)
                state = (
                    graph.continue_run(request.thread_id)
                    if checkpoint_state
                    else graph.start(request)
                )
            return self._persist_state(request.project_id, request.thread_id, state)
        except Exception as exc:
            self._persist_failure(request.project_id, request.thread_id, exc)
            raise ResearchExecutionError("Research execution failed.") from exc

    def execute_resume(
        self,
        *,
        project_id: str,
        thread_id: str,
        approved_urls: Sequence[str],
    ) -> ResearchGraphRun:
        self.validate_resume(
            project_id=project_id,
            thread_id=thread_id,
            approved_urls=approved_urls,
        )
        self._runs.append_event(
            project_id=project_id,
            thread_id=thread_id,
            event_type="resumed",
            node_name="await_human_review",
            details={"approved_url_count": len(tuple(approved_urls))},
        )
        self._runs.mark_started(
            project_id,
            thread_id,
            current_node="await_human_review",
        )
        try:
            with self._sessions.open() as graph:
                state = graph.resume(thread_id, approved_urls=approved_urls)
            return self._persist_state(project_id, thread_id, state)
        except Exception as exc:
            self._persist_failure(project_id, thread_id, exc)
            raise ResearchExecutionError("Research execution failed.") from exc

    def validate_resume(
        self,
        *,
        project_id: str,
        thread_id: str,
        approved_urls: Sequence[str],
    ) -> None:
        run = self._runs.get_run(project_id, thread_id)
        if run is None:
            raise ResearchRunNotFound("research run was not found")
        if run.status != "waiting_for_review":
            raise ResearchRunConflictError(
                "research run is not waiting for candidate review"
            )
        state = self.checkpoint_state(project_id=project_id, thread_id=thread_id)
        known_urls = {
            str(candidate["url"])
            for candidate in state.get("discovered_candidates", [])  # type: ignore[union-attr]
            if isinstance(candidate, dict) and candidate.get("url")
        }
        approved = tuple(str(url).strip() for url in approved_urls)
        if any(not url for url in approved):
            raise ValueError("approved_urls must not contain blank values")
        if set(approved) - known_urls:
            raise ValueError("approved_urls contains unknown candidates")

    def checkpoint_state(
        self,
        *,
        project_id: str,
        thread_id: str,
    ) -> dict[str, object]:
        run = self._runs.get_run(project_id, thread_id)
        if run is None:
            raise ResearchRunNotFound("research run was not found")
        with self._sessions.open() as graph:
            state = graph.state(thread_id)
        if state and state.get("project_id") != project_id:
            raise ResearchRunConflictError(
                "checkpoint state does not belong to the requested project"
            )
        return state

    def _persist_state(
        self,
        project_id: str,
        thread_id: str,
        state: dict[str, object],
    ) -> ResearchGraphRun:
        run = self._runs.update_from_state(project_id, thread_id, state)
        self._runs.append_event(
            project_id=project_id,
            thread_id=thread_id,
            event_type="node_completed",
            node_name=run.current_node,
            scope_id=run.current_scope_id,
            details={
                "status": run.status,
                "gap_fill_round": run.gap_fill_round,
                "discovery_queries_used": run.discovery_queries_used,
            },
        )
        if run.status == "waiting_for_review":
            self._runs.append_event(
                project_id=project_id,
                thread_id=thread_id,
                event_type="interrupted",
                node_name="await_human_review",
                scope_id=run.current_scope_id,
            )
        elif run.status in {"completed", "completed_with_warnings"}:
            self._runs.append_event(
                project_id=project_id,
                thread_id=thread_id,
                event_type="completed",
                node_name=run.current_node,
                details={"warning_count": len(run.warnings)},
            )
        return run

    def _persist_failure(
        self,
        project_id: str,
        thread_id: str,
        error: BaseException,
    ) -> None:
        failed = self._runs.mark_failed(project_id, thread_id, error)
        self._runs.append_event(
            project_id=project_id,
            thread_id=thread_id,
            event_type="failed",
            node_name=failed.current_node,
            details={"error_code": failed.error_code},
        )
