from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection, Engine, RowMapping
from sqlalchemy.exc import IntegrityError

from .research_graph import ResearchGraphRequest
from .schema import gap_fill_attempts, research_graph_events, research_graph_runs


ResearchRunStatus = Literal[
    "queued",
    "running",
    "waiting_for_review",
    "completed",
    "completed_with_warnings",
    "failed",
    "cancelled",
]
ResearchEventType = Literal[
    "queued",
    "node_completed",
    "interrupted",
    "resumed",
    "failed",
    "completed",
    "tool_call",
]
GapFillResult = Literal["pending", "improved", "no_change", "blocked"]
TERMINAL_RESEARCH_STATUSES = frozenset(
    {"completed", "completed_with_warnings", "failed", "cancelled"}
)


class ResearchRunRepositoryError(RuntimeError):
    """Base error for M4 research-run business persistence."""


class ResearchRunConflictError(ResearchRunRepositoryError):
    """Raised when a stable run or attempt identity is reused with new content."""


class ResearchRunNotFound(ResearchRunRepositoryError):
    """Raised when a requested project-scoped run does not exist."""


@dataclass(frozen=True, slots=True)
class ResearchGraphRun:
    project_id: str
    thread_id: str
    organization_id: str
    retrieval_plan_id: str
    article_id: str
    outline_version: int
    status: ResearchRunStatus
    current_node: str
    current_scope_id: str | None
    gap_fill_round: int
    max_gap_fill_rounds: int
    discovery_queries_used: int
    max_discovery_queries: int
    evidence_pack_ids: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    error_code: str | None = None
    error_message: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    finished_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ResearchGraphEvent:
    project_id: str
    thread_id: str
    sequence: int
    event_type: ResearchEventType
    node_name: str
    scope_id: str | None
    attempt: int
    details: Mapping[str, object] = field(default_factory=dict)
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class GapFillAttempt:
    project_id: str
    thread_id: str
    retrieval_plan_id: str
    scope_id: str
    round_number: int
    attempt_id: str
    reason: str
    channel: Literal["official_site", "tavily_discovery"]
    query: str
    discovered_urls: tuple[str, ...] = ()
    published_source_ids: tuple[str, ...] = ()
    result: GapFillResult = "pending"
    cost_usage: Mapping[str, object] = field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None


def sanitized_research_error(error: BaseException) -> tuple[str, str]:
    """Return a stable public diagnostic without copying provider exception text."""

    code = type(error).__name__
    if not code or not code.replace("_", "").isalnum():
        code = "ResearchExecutionError"
    return code[:120], "Research execution failed. Inspect restricted server logs."


def _run_signature(run: ResearchGraphRun) -> tuple[object, ...]:
    return (
        run.project_id,
        run.thread_id,
        run.organization_id,
        run.retrieval_plan_id,
        run.article_id,
        run.outline_version,
        run.max_gap_fill_rounds,
        run.max_discovery_queries,
        dict(run.metadata),
    )


def _attempt_signature(attempt: GapFillAttempt) -> tuple[object, ...]:
    return (
        attempt.project_id,
        attempt.thread_id,
        attempt.retrieval_plan_id,
        attempt.scope_id,
        attempt.round_number,
        attempt.attempt_id,
        attempt.reason,
        attempt.channel,
        attempt.query,
        attempt.discovered_urls,
        attempt.published_source_ids,
        attempt.result,
        dict(attempt.cost_usage),
    )


def _attempt_core(attempt: GapFillAttempt) -> tuple[object, ...]:
    return (
        attempt.project_id,
        attempt.thread_id,
        attempt.retrieval_plan_id,
        attempt.scope_id,
        attempt.round_number,
        attempt.attempt_id,
        attempt.reason,
        attempt.channel,
        attempt.query,
        attempt.discovered_urls,
    )


def _run_from_row(row: Mapping[str, object] | RowMapping) -> ResearchGraphRun:
    return ResearchGraphRun(
        project_id=str(row["project_id"]),
        thread_id=str(row["thread_id"]),
        organization_id=str(row["organization_id"]),
        retrieval_plan_id=str(row["retrieval_plan_id"]),
        article_id=str(row["article_id"]),
        outline_version=int(row["outline_version"]),
        status=str(row["status"]),  # type: ignore[arg-type]
        current_node=str(row["current_node"]),
        current_scope_id=(
            str(row["current_scope_id"])
            if row["current_scope_id"] is not None
            else None
        ),
        gap_fill_round=int(row["gap_fill_round"]),
        max_gap_fill_rounds=int(row["max_gap_fill_rounds"]),
        discovery_queries_used=int(row["discovery_queries_used"]),
        max_discovery_queries=int(row["max_discovery_queries"]),
        evidence_pack_ids=tuple(row["evidence_pack_ids"]),  # type: ignore[arg-type]
        warnings=tuple(row["warnings"]),  # type: ignore[arg-type]
        error_code=(
            str(row["error_code"]) if row["error_code"] is not None else None
        ),
        error_message=(
            str(row["error_message"]) if row["error_message"] is not None else None
        ),
        metadata=dict(row["metadata"] or {}),  # type: ignore[arg-type]
        created_at=row["created_at"],  # type: ignore[arg-type]
        updated_at=row["updated_at"],  # type: ignore[arg-type]
        finished_at=row["finished_at"],  # type: ignore[arg-type]
    )


def _event_from_row(row: Mapping[str, object] | RowMapping) -> ResearchGraphEvent:
    return ResearchGraphEvent(
        project_id=str(row["project_id"]),
        thread_id=str(row["thread_id"]),
        sequence=int(row["sequence"]),
        event_type=str(row["event_type"]),  # type: ignore[arg-type]
        node_name=str(row["node_name"]),
        scope_id=str(row["scope_id"]) if row["scope_id"] is not None else None,
        attempt=int(row["attempt"]),
        details=dict(row["details"] or {}),  # type: ignore[arg-type]
        created_at=row["created_at"],  # type: ignore[arg-type]
    )


def _attempt_from_row(row: Mapping[str, object] | RowMapping) -> GapFillAttempt:
    return GapFillAttempt(
        project_id=str(row["project_id"]),
        thread_id=str(row["thread_id"]),
        retrieval_plan_id=str(row["retrieval_plan_id"]),
        scope_id=str(row["scope_id"]),
        round_number=int(row["round_number"]),
        attempt_id=str(row["attempt_id"]),
        reason=str(row["reason"]),
        channel=str(row["channel"]),  # type: ignore[arg-type]
        query=str(row["query"]),
        discovered_urls=tuple(row["discovered_urls"]),  # type: ignore[arg-type]
        published_source_ids=tuple(
            row["published_source_ids"]  # type: ignore[arg-type]
        ),
        result=str(row["result"]),  # type: ignore[arg-type]
        cost_usage=dict(row["cost_usage"] or {}),  # type: ignore[arg-type]
        created_at=row["created_at"],  # type: ignore[arg-type]
        updated_at=row["updated_at"],  # type: ignore[arg-type]
    )


class PostgresResearchRunRepository:
    """Project-scoped operational view over LangGraph checkpoint execution."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def create_run(
        self,
        request: ResearchGraphRequest,
        *,
        metadata: Mapping[str, object] | None = None,
    ) -> ResearchGraphRun:
        try:
            with self._engine.begin() as connection:
                return self.create_run_in_transaction(
                    connection,
                    request,
                    metadata=metadata,
                )
        except IntegrityError as exc:
            raise ResearchRunConflictError(
                "research run violates project, plan, or thread identity constraints"
            ) from exc

    def create_run_in_transaction(
        self,
        connection: Connection,
        request: ResearchGraphRequest,
        *,
        metadata: Mapping[str, object] | None = None,
    ) -> ResearchGraphRun:
        """Create an immutable run identity inside a caller-owned transaction."""

        requested = ResearchGraphRun(
            project_id=request.project_id,
            thread_id=request.thread_id,
            organization_id=request.organization_id,
            retrieval_plan_id=request.retrieval_plan_id,
            article_id=request.article_id,
            outline_version=request.outline_version,
            status="queued",
            current_node="queued",
            current_scope_id=None,
            gap_fill_round=0,
            max_gap_fill_rounds=request.max_gap_fill_rounds,
            discovery_queries_used=0,
            max_discovery_queries=request.max_discovery_queries,
            metadata=dict(metadata or {}),
        )
        existing = self._get(
            connection,
            requested.project_id,
            requested.thread_id,
        )
        if existing is not None:
            if _run_signature(existing) != _run_signature(requested):
                raise ResearchRunConflictError(
                    "research thread identity already has different content"
                )
            return existing
        row = connection.execute(
            research_graph_runs.insert()
            .values(
                project_id=requested.project_id,
                thread_id=requested.thread_id,
                organization_id=requested.organization_id,
                retrieval_plan_id=requested.retrieval_plan_id,
                article_id=requested.article_id,
                outline_version=requested.outline_version,
                status=requested.status,
                current_node=requested.current_node,
                current_scope_id=None,
                gap_fill_round=0,
                max_gap_fill_rounds=requested.max_gap_fill_rounds,
                discovery_queries_used=0,
                max_discovery_queries=requested.max_discovery_queries,
                evidence_pack_ids=list(request.initial_evidence_pack_ids),
                warnings=[],
                error_code=None,
                error_message=None,
                metadata=dict(requested.metadata),
                finished_at=None,
            )
            .returning(research_graph_runs)
        ).mappings().one()
        return _run_from_row(row)

    def get_run(self, project_id: str, thread_id: str) -> ResearchGraphRun | None:
        with self._engine.connect() as connection:
            return self._get(connection, project_id, thread_id)

    def get_run_in_transaction(
        self,
        connection: Connection,
        project_id: str,
        thread_id: str,
    ) -> ResearchGraphRun | None:
        """Read one run through the caller's transaction snapshot."""

        return self._get(connection, project_id, thread_id)

    def list_runs(
        self,
        project_id: str,
        *,
        article_id: str | None = None,
        limit: int = 50,
    ) -> tuple[ResearchGraphRun, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        statement = sa.select(research_graph_runs).where(
            research_graph_runs.c.project_id == project_id
        )
        if article_id is not None:
            statement = statement.where(
                research_graph_runs.c.article_id == article_id
            )
        with self._engine.connect() as connection:
            rows = connection.execute(
                statement.order_by(
                    research_graph_runs.c.created_at.desc(),
                    research_graph_runs.c.thread_id,
                ).limit(limit)
            ).mappings()
            return tuple(_run_from_row(row) for row in rows)

    def mark_started(
        self,
        project_id: str,
        thread_id: str,
        *,
        current_node: str = "start",
    ) -> ResearchGraphRun:
        with self._engine.begin() as connection:
            current = self._locked_run(connection, project_id, thread_id)
            if current.status in TERMINAL_RESEARCH_STATUSES:
                raise ResearchRunConflictError(
                    "terminal research run cannot be started again"
                )
            row = connection.execute(
                research_graph_runs.update()
                .where(
                    research_graph_runs.c.project_id == project_id,
                    research_graph_runs.c.thread_id == thread_id,
                )
                .values(
                    status="running",
                    current_node=current_node,
                    error_code=None,
                    error_message=None,
                    updated_at=sa.func.now(),
                    finished_at=None,
                )
                .returning(research_graph_runs)
            ).mappings().one()
            return _run_from_row(row)

    def update_from_state(
        self,
        project_id: str,
        thread_id: str,
        state: Mapping[str, object],
    ) -> ResearchGraphRun:
        if state.get("project_id") != project_id or state.get("thread_id") != thread_id:
            raise ResearchRunConflictError(
                "checkpoint state does not belong to the requested research run"
            )
        status = str(state["status"])
        if status not in {
            "running",
            "waiting_for_review",
            "completed",
            "completed_with_warnings",
        }:
            raise ResearchRunConflictError("checkpoint state has an invalid status")
        finished_at = (
            datetime.now(timezone.utc)
            if status in TERMINAL_RESEARCH_STATUSES
            else None
        )
        with self._engine.begin() as connection:
            current = self._locked_run(connection, project_id, thread_id)
            self._assert_state_identity(current, state)
            row = connection.execute(
                research_graph_runs.update()
                .where(
                    research_graph_runs.c.project_id == project_id,
                    research_graph_runs.c.thread_id == thread_id,
                )
                .values(
                    status=status,
                    current_node=str(state["current_node"]),
                    current_scope_id=(
                        str(state["current_scope_id"])
                        if state.get("current_scope_id")
                        else None
                    ),
                    gap_fill_round=int(state["gap_fill_round"]),
                    max_gap_fill_rounds=int(state["max_gap_fill_rounds"]),
                    discovery_queries_used=int(state["discovery_queries_used"]),
                    max_discovery_queries=int(state["max_discovery_queries"]),
                    evidence_pack_ids=list(state.get("evidence_pack_ids") or ()),
                    warnings=list(state.get("warnings") or ()),
                    error_code=None,
                    error_message=None,
                    updated_at=sa.func.now(),
                    finished_at=finished_at,
                )
                .returning(research_graph_runs)
            ).mappings().one()
            return _run_from_row(row)

    def mark_failed(
        self,
        project_id: str,
        thread_id: str,
        error: BaseException,
    ) -> ResearchGraphRun:
        error_code, error_message = sanitized_research_error(error)
        with self._engine.begin() as connection:
            self._locked_run(connection, project_id, thread_id)
            row = connection.execute(
                research_graph_runs.update()
                .where(
                    research_graph_runs.c.project_id == project_id,
                    research_graph_runs.c.thread_id == thread_id,
                )
                .values(
                    status="failed",
                    current_node="failed",
                    error_code=error_code,
                    error_message=error_message,
                    updated_at=sa.func.now(),
                    finished_at=sa.func.now(),
                )
                .returning(research_graph_runs)
            ).mappings().one()
            return _run_from_row(row)

    def restore_after_interruption(
        self,
        previous: ResearchGraphRun,
    ) -> ResearchGraphRun:
        """Restore the resumable projection after controlled worker stop.

        LangGraph owns its checkpoint transaction. The PostgreSQL business
        projection must return to the state that was valid before this
        execution attempt, rather than becoming terminal ``failed``.
        """

        if previous.status not in {
            "queued",
            "running",
            "waiting_for_review",
        }:
            raise ResearchRunConflictError(
                "interrupted research state is not resumable"
            )
        with self._engine.begin() as connection:
            current = self._locked_run(
                connection,
                previous.project_id,
                previous.thread_id,
            )
            if current.status in TERMINAL_RESEARCH_STATUSES:
                raise ResearchRunConflictError(
                    "terminal research run cannot be restored"
                )
            row = connection.execute(
                research_graph_runs.update()
                .where(
                    research_graph_runs.c.project_id
                    == previous.project_id,
                    research_graph_runs.c.thread_id
                    == previous.thread_id,
                )
                .values(
                    status=previous.status,
                    current_node=previous.current_node,
                    current_scope_id=previous.current_scope_id,
                    error_code=None,
                    error_message=None,
                    updated_at=sa.func.now(),
                    finished_at=None,
                )
                .returning(research_graph_runs)
            ).mappings().one()
            restored = _run_from_row(row)
            self.append_event_in_transaction(
                connection,
                project_id=previous.project_id,
                thread_id=previous.thread_id,
                event_type="interrupted",
                node_name=previous.current_node,
                scope_id=previous.current_scope_id,
                details={"restored_status": previous.status},
            )
            return restored

    def append_event(
        self,
        *,
        project_id: str,
        thread_id: str,
        event_type: ResearchEventType,
        node_name: str,
        scope_id: str | None = None,
        attempt: int = 1,
        details: Mapping[str, object] | None = None,
    ) -> ResearchGraphEvent:
        with self._engine.begin() as connection:
            return self.append_event_in_transaction(
                connection,
                project_id=project_id,
                thread_id=thread_id,
                event_type=event_type,
                node_name=node_name,
                scope_id=scope_id,
                attempt=attempt,
                details=details,
            )

    def append_event_in_transaction(
        self,
        connection: Connection,
        *,
        project_id: str,
        thread_id: str,
        event_type: ResearchEventType,
        node_name: str,
        scope_id: str | None = None,
        attempt: int = 1,
        details: Mapping[str, object] | None = None,
    ) -> ResearchGraphEvent:
        """Append a sequenced event inside a caller-owned transaction."""

        self._locked_run(connection, project_id, thread_id)
        sequence = connection.execute(
            sa.select(
                sa.func.coalesce(
                    sa.func.max(research_graph_events.c.sequence),
                    0,
                )
            ).where(
                research_graph_events.c.project_id == project_id,
                research_graph_events.c.thread_id == thread_id,
            )
        ).scalar_one()
        row = connection.execute(
            research_graph_events.insert()
            .values(
                project_id=project_id,
                thread_id=thread_id,
                sequence=int(sequence) + 1,
                event_type=event_type,
                node_name=node_name,
                scope_id=scope_id,
                attempt=attempt,
                details=dict(details or {}),
            )
            .returning(research_graph_events)
        ).mappings().one()
        return _event_from_row(row)

    def lock_run_in_transaction(
        self,
        connection: Connection,
        project_id: str,
        thread_id: str,
    ) -> ResearchGraphRun:
        """Lock one project-scoped run for a larger command transaction."""

        return self._locked_run(connection, project_id, thread_id)

    def append_node_attempt(
        self,
        *,
        project_id: str,
        thread_id: str,
        node_name: str,
        scope_id: str | None,
        operation_id: str,
        duration_ms: float,
        outcome: Literal["succeeded", "failed"],
        error_code: str | None,
    ) -> ResearchGraphEvent:
        if duration_ms < 0:
            raise ValueError("duration_ms must be non-negative")
        with self._engine.begin() as connection:
            self._locked_run(connection, project_id, thread_id)
            sequence = connection.execute(
                sa.select(
                    sa.func.coalesce(
                        sa.func.max(research_graph_events.c.sequence),
                        0,
                    )
                ).where(
                    research_graph_events.c.project_id == project_id,
                    research_graph_events.c.thread_id == thread_id,
                )
            ).scalar_one()
            attempt = connection.execute(
                sa.select(
                    sa.func.coalesce(
                        sa.func.max(research_graph_events.c.attempt),
                        0,
                    )
                ).where(
                    research_graph_events.c.project_id == project_id,
                    research_graph_events.c.thread_id == thread_id,
                    research_graph_events.c.event_type == "tool_call",
                    research_graph_events.c.node_name == node_name,
                    research_graph_events.c.details["operation_id"].astext
                    == operation_id,
                )
            ).scalar_one()
            row = connection.execute(
                research_graph_events.insert()
                .values(
                    project_id=project_id,
                    thread_id=thread_id,
                    sequence=int(sequence) + 1,
                    event_type="tool_call",
                    node_name=node_name,
                    scope_id=scope_id,
                    attempt=int(attempt) + 1,
                    details={
                        "operation_id": operation_id,
                        "outcome": outcome,
                        "duration_ms": duration_ms,
                        "error_code": error_code,
                    },
                )
                .returning(research_graph_events)
            ).mappings().one()
            return _event_from_row(row)

    def list_events(
        self,
        project_id: str,
        thread_id: str,
        *,
        after_sequence: int = 0,
    ) -> tuple[ResearchGraphEvent, ...]:
        if (
            isinstance(after_sequence, bool)
            or not isinstance(after_sequence, int)
            or after_sequence < 0
        ):
            raise ValueError("after_sequence must be a non-negative integer")
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(research_graph_events)
                .where(
                    research_graph_events.c.project_id == project_id,
                    research_graph_events.c.thread_id == thread_id,
                    research_graph_events.c.sequence > after_sequence,
                )
                .order_by(research_graph_events.c.sequence)
            ).mappings()
            return tuple(_event_from_row(row) for row in rows)

    def record_gap_attempt(self, attempt: GapFillAttempt) -> GapFillAttempt:
        try:
            with self._engine.begin() as connection:
                existing_row = connection.execute(
                    sa.select(gap_fill_attempts).where(
                        gap_fill_attempts.c.project_id == attempt.project_id,
                        gap_fill_attempts.c.thread_id == attempt.thread_id,
                        gap_fill_attempts.c.scope_id == attempt.scope_id,
                        gap_fill_attempts.c.round_number == attempt.round_number,
                    )
                ).mappings().one_or_none()
                if existing_row is not None:
                    existing = _attempt_from_row(existing_row)
                    if _attempt_core(existing) != _attempt_core(attempt):
                        raise ResearchRunConflictError(
                            "gap-fill attempt identity already has different content"
                        )
                    if attempt.result == "pending":
                        return existing
                    if existing.result != "pending":
                        if _attempt_signature(existing) != _attempt_signature(attempt):
                            raise ResearchRunConflictError(
                                "completed gap-fill attempt is immutable"
                            )
                        return existing
                    row = connection.execute(
                        gap_fill_attempts.update()
                        .where(
                            gap_fill_attempts.c.project_id == attempt.project_id,
                            gap_fill_attempts.c.thread_id == attempt.thread_id,
                            gap_fill_attempts.c.scope_id == attempt.scope_id,
                            gap_fill_attempts.c.round_number == attempt.round_number,
                        )
                        .values(
                            published_source_ids=list(
                                attempt.published_source_ids
                            ),
                            result=attempt.result,
                            cost_usage=dict(attempt.cost_usage),
                            updated_at=sa.func.now(),
                        )
                        .returning(gap_fill_attempts)
                    ).mappings().one()
                    return _attempt_from_row(row)
                row = connection.execute(
                    insert(gap_fill_attempts)
                    .values(
                        project_id=attempt.project_id,
                        thread_id=attempt.thread_id,
                        retrieval_plan_id=attempt.retrieval_plan_id,
                        scope_id=attempt.scope_id,
                        round_number=attempt.round_number,
                        attempt_id=attempt.attempt_id,
                        reason=attempt.reason,
                        channel=attempt.channel,
                        query=attempt.query,
                        discovered_urls=list(attempt.discovered_urls),
                        published_source_ids=list(attempt.published_source_ids),
                        result=attempt.result,
                        cost_usage=dict(attempt.cost_usage),
                    )
                    .returning(gap_fill_attempts)
                ).mappings().one()
                return _attempt_from_row(row)
        except IntegrityError as exc:
            raise ResearchRunConflictError(
                "gap-fill attempt violates run, scope, or retry identity constraints"
            ) from exc

    def get_gap_attempt_by_id(self, attempt_id: str) -> GapFillAttempt | None:
        with self._engine.connect() as connection:
            row = connection.execute(
                sa.select(gap_fill_attempts).where(
                    gap_fill_attempts.c.attempt_id == attempt_id
                )
            ).mappings().one_or_none()
            return _attempt_from_row(row) if row is not None else None

    def list_gap_attempts(
        self,
        project_id: str,
        thread_id: str,
    ) -> tuple[GapFillAttempt, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(gap_fill_attempts)
                .where(
                    gap_fill_attempts.c.project_id == project_id,
                    gap_fill_attempts.c.thread_id == thread_id,
                )
                .order_by(
                    gap_fill_attempts.c.scope_id,
                    gap_fill_attempts.c.round_number,
                )
            ).mappings()
            return tuple(_attempt_from_row(row) for row in rows)

    def prune_expired_details(self, *, before: datetime) -> dict[str, int]:
        """Delete old node telemetry and attempts while retaining run summaries."""

        if before.tzinfo is None or before.utcoffset() is None:
            raise ValueError("before must be timezone-aware")
        with self._engine.begin() as connection:
            events = connection.execute(
                research_graph_events.delete().where(
                    research_graph_events.c.created_at < before
                )
            )
            attempts = connection.execute(
                gap_fill_attempts.delete().where(
                    gap_fill_attempts.c.updated_at < before
                )
            )
            return {
                "research_graph_events": int(events.rowcount or 0),
                "gap_fill_attempts": int(attempts.rowcount or 0),
            }

    @staticmethod
    def _get(
        connection: sa.Connection,
        project_id: str,
        thread_id: str,
    ) -> ResearchGraphRun | None:
        row = connection.execute(
            sa.select(research_graph_runs).where(
                research_graph_runs.c.project_id == project_id,
                research_graph_runs.c.thread_id == thread_id,
            )
        ).mappings().one_or_none()
        return _run_from_row(row) if row is not None else None

    @staticmethod
    def _locked_run(
        connection: sa.Connection,
        project_id: str,
        thread_id: str,
    ) -> ResearchGraphRun:
        row = connection.execute(
            sa.select(research_graph_runs)
            .where(
                research_graph_runs.c.project_id == project_id,
                research_graph_runs.c.thread_id == thread_id,
            )
            .with_for_update()
        ).mappings().one_or_none()
        if row is None:
            raise ResearchRunNotFound("research run was not found")
        return _run_from_row(row)

    @staticmethod
    def _assert_state_identity(
        run: ResearchGraphRun,
        state: Mapping[str, object],
    ) -> None:
        identity = (
            ("organization_id", run.organization_id),
            ("retrieval_plan_id", run.retrieval_plan_id),
            ("article_id", run.article_id),
            ("outline_version", run.outline_version),
        )
        if any(state.get(name) != expected for name, expected in identity):
            raise ResearchRunConflictError(
                "checkpoint state changed immutable research-run identity"
            )
