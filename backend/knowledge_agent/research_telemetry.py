from __future__ import annotations

from typing import Literal

from .research_runs import PostgresResearchRunRepository


class PostgresResearchTelemetry:
    """Persist bounded node-attempt telemetry without provider error messages."""

    def __init__(self, runs: PostgresResearchRunRepository) -> None:
        self._runs = runs

    def record_node_attempt(
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
    ) -> None:
        self._runs.append_node_attempt(
            project_id=project_id,
            thread_id=thread_id,
            node_name=node_name,
            scope_id=scope_id,
            operation_id=operation_id,
            duration_ms=duration_ms,
            outcome=outcome,
            error_code=error_code,
        )
