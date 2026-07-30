from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine, RowMapping

from server_schema import background_jobs, job_batches
from services.access_control import (
    ActorIdentity,
    PostgresProjectAccessRepository,
    ProjectAccessDenied,
    ProjectAccessFacts,
    decide_project_permission,
)
from services.audit_log import (
    AuditEvent,
    AuditEventWriter,
    PostgresAuditEventWriter,
)
from services.authorized_job_queue import worker_permission_for
from services.job_queue import ACTIVE_JOB_STATUSES, ActiveJobError
from services.postgres_job_queue import PostgresJobQueue


SERVER_JOB_CONTROL_OPERATIONS = frozenset({"product_rediscovery"})


def _required_text(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized


def _iso(value: object) -> str:
    if not isinstance(value, datetime):
        raise ValueError("timestamp is required")
    return value.isoformat()


def _optional_iso(value: object) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


class ServerJobControlUnavailable(RuntimeError):
    """The project Job control plane could not safely complete an operation."""


class ServerJobControlConflict(RuntimeError):
    """The requested state transition is invalid for the current Job state."""


@dataclass(frozen=True, slots=True)
class ServerJobSummary:
    """Public Job projection; private request, requester, and error stay internal."""

    job_id: str
    batch_id: str
    task_id: str
    operation: str
    status: str
    source_revision: int
    result_revision: int | None
    attempts: int
    max_attempts: int
    cancel_requested: bool
    has_error: bool
    created_at: str
    started_at: str | None
    finished_at: str | None
    updated_at: str


@dataclass(frozen=True, slots=True)
class ServerBatchSummary:
    """Public Batch projection for one explicitly scoped project."""

    batch_id: str
    operation: str
    status: str
    total: int
    completed: int
    status_counts: dict[str, int]
    jobs: tuple[ServerJobSummary, ...]
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class ServerBatchPage:
    """Stable keyset page ordered by creation time and Batch identity."""

    items: tuple[ServerBatchSummary, ...]
    next_after_batch_id: str | None = None


class PostgresServerJobControlService:
    """Read and mutate migrated PostgreSQL Jobs behind project authorization.

    Reads are SQL-scoped to the Actor organization and route project. Mutations
    lock every revocable access fact before locking Job state, then append the
    command audit event in the same transaction as the queue transition.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        access_repository: PostgresProjectAccessRepository | None = None,
        audit: AuditEventWriter | None = None,
    ) -> None:
        self._engine = engine
        self._access = (
            access_repository
            if access_repository is not None
            else PostgresProjectAccessRepository(engine)
        )
        self._audit = (
            audit if audit is not None else PostgresAuditEventWriter()
        )

    @staticmethod
    def _scope(
        actor: ActorIdentity,
        project_id: str,
    ) -> tuple[str, str]:
        return (
            actor.organization_id,
            _required_text(project_id, "project_id"),
        )

    def _require_view(
        self,
        connection: Connection,
        actor: ActorIdentity,
        project_id: str,
    ) -> None:
        facts = self._access.resolve_project_access_in_connection(
            connection,
            actor,
            project_id,
        )
        if not decide_project_permission(facts, "project.view").allowed:
            raise ProjectAccessDenied("project access denied")

    def _lock_mutation_access(
        self,
        connection: Connection,
        actor: ActorIdentity,
        project_id: str,
    ) -> ProjectAccessFacts:
        facts = self._access.lock_project_access_in_connection(
            connection,
            actor,
            project_id,
        )
        if (
            facts is None
            or not decide_project_permission(
                facts,
                "project.view",
            ).allowed
        ):
            raise ProjectAccessDenied("project access denied")
        return facts

    @staticmethod
    def _require_operation_permission(
        facts: ProjectAccessFacts,
        operation: str,
    ) -> None:
        if operation not in SERVER_JOB_CONTROL_OPERATIONS:
            raise KeyError(operation)
        if not decide_project_permission(
            facts,
            worker_permission_for(operation),
        ).allowed:
            raise ProjectAccessDenied("project access denied")

    def _queue(
        self,
        organization_id: str,
        project_id: str,
    ) -> PostgresJobQueue:
        return PostgresJobQueue(
            self._engine,
            organization_id=organization_id,
            project_id=project_id,
            worker_id="server-job-control",
            terminal_audit=self._audit,
        )

    @staticmethod
    def _job_summary(
        row: Mapping[str, object] | RowMapping,
    ) -> ServerJobSummary:
        return ServerJobSummary(
            job_id=str(row["job_id"]),
            batch_id=str(row["batch_id"]),
            task_id=str(row["task_id"]),
            operation=str(row["operation"]),
            status=str(row["status"]),
            source_revision=int(row["source_revision"]),
            result_revision=(
                None
                if row["result_revision"] is None
                else int(row["result_revision"])
            ),
            attempts=int(row["attempts"]),
            max_attempts=int(row["max_attempts"]),
            cancel_requested=bool(row["cancel_requested"]),
            has_error=bool(str(row["error"]).strip()),
            created_at=_iso(row["created_at"]),
            started_at=_optional_iso(row["started_at"]),
            finished_at=_optional_iso(row["finished_at"]),
            updated_at=_iso(row["updated_at"]),
        )

    @classmethod
    def _batch_summary(
        cls,
        batch: Mapping[str, object] | RowMapping,
        jobs: list[Mapping[str, object] | RowMapping],
    ) -> ServerBatchSummary:
        operation = str(batch["operation"])
        if operation not in SERVER_JOB_CONTROL_OPERATIONS or any(
            str(job["operation"]) != operation for job in jobs
        ):
            raise ServerJobControlUnavailable(
                "server job control data is inconsistent"
            )
        public_jobs = tuple(cls._job_summary(job) for job in jobs)
        status_counts: dict[str, int] = {}
        for job in public_jobs:
            status_counts[job.status] = status_counts.get(job.status, 0) + 1
        active = sum(
            status_counts.get(status, 0) for status in ACTIVE_JOB_STATUSES
        )
        if active:
            status = "running" if status_counts.get("running", 0) else "queued"
        elif public_jobs and status_counts.get("succeeded", 0) == len(
            public_jobs
        ):
            status = "succeeded"
        elif public_jobs and status_counts.get("cancelled", 0) == len(
            public_jobs
        ):
            status = "cancelled"
        else:
            status = "completed_with_errors"
        return ServerBatchSummary(
            batch_id=str(batch["batch_id"]),
            operation=operation,
            status=status,
            total=len(public_jobs),
            completed=len(public_jobs) - active,
            status_counts=status_counts,
            jobs=public_jobs,
            created_at=_iso(batch["created_at"]),
            updated_at=_iso(batch["updated_at"]),
        )

    @staticmethod
    def _controlled_batch(
        connection: Connection,
        *,
        organization_id: str,
        project_id: str,
        batch_id: str,
        lock: bool = False,
    ) -> RowMapping:
        statement = sa.select(job_batches).where(
            job_batches.c.organization_id == organization_id,
            job_batches.c.project_id == project_id,
            job_batches.c.batch_id == batch_id,
            job_batches.c.operation.in_(SERVER_JOB_CONTROL_OPERATIONS),
        )
        if lock:
            statement = statement.with_for_update()
        row = connection.execute(statement).mappings().one_or_none()
        if row is None:
            raise KeyError(batch_id)
        return row

    @staticmethod
    def _controlled_job(
        connection: Connection,
        *,
        organization_id: str,
        project_id: str,
        job_id: str,
        lock: bool = False,
    ) -> RowMapping:
        statement = sa.select(background_jobs).where(
            background_jobs.c.organization_id == organization_id,
            background_jobs.c.project_id == project_id,
            background_jobs.c.job_id == job_id,
            background_jobs.c.operation.in_(SERVER_JOB_CONTROL_OPERATIONS),
        )
        if lock:
            statement = statement.with_for_update()
        row = connection.execute(statement).mappings().one_or_none()
        if row is None:
            raise KeyError(job_id)
        return row

    @staticmethod
    def _batch_jobs(
        connection: Connection,
        *,
        organization_id: str,
        project_id: str,
        batch_ids: tuple[str, ...],
        lock: bool = False,
    ) -> dict[str, list[RowMapping]]:
        if not batch_ids:
            return {}
        statement = (
            sa.select(background_jobs)
            .where(
                background_jobs.c.organization_id == organization_id,
                background_jobs.c.project_id == project_id,
                background_jobs.c.batch_id.in_(batch_ids),
            )
            .order_by(
                background_jobs.c.created_at,
                background_jobs.c.topic_index,
                background_jobs.c.job_id,
            )
        )
        if lock:
            statement = statement.with_for_update()
        rows = connection.execute(statement).mappings()
        grouped: dict[str, list[RowMapping]] = {
            batch_id: [] for batch_id in batch_ids
        }
        for row in rows:
            grouped[str(row["batch_id"])].append(row)
        return grouped

    def list_batches(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        limit: int = 20,
        after_batch_id: str | None = None,
    ) -> ServerBatchPage:
        organization_id, normalized_project = self._scope(actor, project_id)
        page_limit = max(1, min(int(limit), 100))
        cursor_id = (
            _required_text(after_batch_id, "after_batch_id")
            if after_batch_id is not None
            else None
        )
        try:
            with self._engine.connect() as connection:
                self._require_view(
                    connection,
                    actor,
                    normalized_project,
                )
                conditions: list[sa.ColumnElement[bool]] = [
                    job_batches.c.organization_id == organization_id,
                    job_batches.c.project_id == normalized_project,
                    job_batches.c.operation.in_(
                        SERVER_JOB_CONTROL_OPERATIONS
                    ),
                ]
                if cursor_id is not None:
                    cursor = self._controlled_batch(
                        connection,
                        organization_id=organization_id,
                        project_id=normalized_project,
                        batch_id=cursor_id,
                    )
                    conditions.append(
                        sa.or_(
                            job_batches.c.created_at < cursor["created_at"],
                            sa.and_(
                                job_batches.c.created_at
                                == cursor["created_at"],
                                job_batches.c.batch_id < cursor_id,
                            ),
                        )
                    )
                rows = connection.execute(
                    sa.select(job_batches)
                    .where(*conditions)
                    .order_by(
                        job_batches.c.created_at.desc(),
                        job_batches.c.batch_id.desc(),
                    )
                    .limit(page_limit + 1)
                ).mappings().all()
                visible = rows[:page_limit]
                batch_ids = tuple(str(row["batch_id"]) for row in visible)
                jobs = self._batch_jobs(
                    connection,
                    organization_id=organization_id,
                    project_id=normalized_project,
                    batch_ids=batch_ids,
                )
                items = tuple(
                    self._batch_summary(
                        row,
                        jobs[str(row["batch_id"])],
                    )
                    for row in visible
                )
                return ServerBatchPage(
                    items=items,
                    next_after_batch_id=(
                        items[-1].batch_id
                        if len(rows) > page_limit and items
                        else None
                    ),
                )
        except (KeyError, ProjectAccessDenied):
            raise
        except ServerJobControlUnavailable:
            raise
        except Exception as exc:
            raise ServerJobControlUnavailable(
                "server job control is unavailable"
            ) from exc

    def get_batch(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        batch_id: str,
    ) -> ServerBatchSummary:
        organization_id, normalized_project = self._scope(actor, project_id)
        normalized_batch = _required_text(batch_id, "batch_id")
        try:
            with self._engine.connect() as connection:
                self._require_view(
                    connection,
                    actor,
                    normalized_project,
                )
                batch = self._controlled_batch(
                    connection,
                    organization_id=organization_id,
                    project_id=normalized_project,
                    batch_id=normalized_batch,
                )
                jobs = self._batch_jobs(
                    connection,
                    organization_id=organization_id,
                    project_id=normalized_project,
                    batch_ids=(normalized_batch,),
                )
                return self._batch_summary(
                    batch,
                    jobs[normalized_batch],
                )
        except (KeyError, ProjectAccessDenied):
            raise
        except ServerJobControlUnavailable:
            raise
        except Exception as exc:
            raise ServerJobControlUnavailable(
                "server job control is unavailable"
            ) from exc

    def _append_command_audit(
        self,
        connection: Connection,
        *,
        actor: ActorIdentity,
        project_id: str,
        action: str,
        target_type: str,
        target_id: str,
        details: Mapping[str, object],
    ) -> None:
        self._audit.append(
            connection,
            AuditEvent(
                organization_id=actor.organization_id,
                event_id=f"jobctl_{uuid4().hex}",
                actor_user_id=actor.user_id,
                project_id=project_id,
                action=action,
                target_type=target_type,
                target_id=target_id,
                details=details,
            ),
        )

    def cancel_job(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        job_id: str,
    ) -> ServerJobSummary:
        organization_id, normalized_project = self._scope(actor, project_id)
        normalized_job = _required_text(job_id, "job_id")
        try:
            with self._engine.begin() as connection:
                facts = self._lock_mutation_access(
                    connection,
                    actor,
                    normalized_project,
                )
                before = self._controlled_job(
                    connection,
                    organization_id=organization_id,
                    project_id=normalized_project,
                    job_id=normalized_job,
                    lock=True,
                )
                operation = str(before["operation"])
                self._require_operation_permission(facts, operation)
                internal = self._queue(
                    organization_id,
                    normalized_project,
                ).request_cancel_in_transaction(connection, normalized_job)
                after = self._controlled_job(
                    connection,
                    organization_id=organization_id,
                    project_id=normalized_project,
                    job_id=normalized_job,
                )
                self._append_command_audit(
                    connection,
                    actor=actor,
                    project_id=normalized_project,
                    action="background_job.cancel_requested",
                    target_type="background_job",
                    target_id=normalized_job,
                    details={
                        "operation": operation,
                        "from_status": str(before["status"]),
                        "to_status": str(after["status"]),
                        "state_changed": (
                            str(before["status"]) != str(after["status"])
                            or bool(before["cancel_requested"])
                            != bool(after["cancel_requested"])
                        ),
                    },
                )
                if str(internal["operation"]) != operation:
                    raise ServerJobControlUnavailable(
                        "server job control data is inconsistent"
                    )
                return self._job_summary(after)
        except (KeyError, ProjectAccessDenied):
            raise
        except ServerJobControlUnavailable:
            raise
        except Exception as exc:
            raise ServerJobControlUnavailable(
                "server job cancellation is unavailable"
            ) from exc

    def cancel_batch(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        batch_id: str,
    ) -> ServerBatchSummary:
        organization_id, normalized_project = self._scope(actor, project_id)
        normalized_batch = _required_text(batch_id, "batch_id")
        try:
            with self._engine.begin() as connection:
                facts = self._lock_mutation_access(
                    connection,
                    actor,
                    normalized_project,
                )
                batch = self._controlled_batch(
                    connection,
                    organization_id=organization_id,
                    project_id=normalized_project,
                    batch_id=normalized_batch,
                    lock=True,
                )
                operation = str(batch["operation"])
                self._require_operation_permission(facts, operation)
                before_jobs = self._batch_jobs(
                    connection,
                    organization_id=organization_id,
                    project_id=normalized_project,
                    batch_ids=(normalized_batch,),
                    lock=True,
                )[normalized_batch]
                if any(
                    str(job["operation"]) != operation
                    for job in before_jobs
                ):
                    raise ServerJobControlUnavailable(
                        "server job control data is inconsistent"
                    )
                affected = sum(
                    1
                    for job in before_jobs
                    if str(job["status"]) in ACTIVE_JOB_STATUSES
                    and not bool(job["cancel_requested"])
                )
                self._queue(
                    organization_id,
                    normalized_project,
                ).cancel_batch_in_transaction(connection, normalized_batch)
                after_batch = self._controlled_batch(
                    connection,
                    organization_id=organization_id,
                    project_id=normalized_project,
                    batch_id=normalized_batch,
                )
                after_jobs = self._batch_jobs(
                    connection,
                    organization_id=organization_id,
                    project_id=normalized_project,
                    batch_ids=(normalized_batch,),
                )[normalized_batch]
                self._append_command_audit(
                    connection,
                    actor=actor,
                    project_id=normalized_project,
                    action="background_batch.cancel_requested",
                    target_type="background_batch",
                    target_id=normalized_batch,
                    details={
                        "operation": operation,
                        "affected_job_count": affected,
                    },
                )
                return self._batch_summary(after_batch, after_jobs)
        except (KeyError, ProjectAccessDenied):
            raise
        except ServerJobControlUnavailable:
            raise
        except Exception as exc:
            raise ServerJobControlUnavailable(
                "server batch cancellation is unavailable"
            ) from exc

    def retry_job(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        job_id: str,
    ) -> ServerJobSummary:
        organization_id, normalized_project = self._scope(actor, project_id)
        normalized_job = _required_text(job_id, "job_id")
        try:
            with self._engine.begin() as connection:
                facts = self._lock_mutation_access(
                    connection,
                    actor,
                    normalized_project,
                )
                before = self._controlled_job(
                    connection,
                    organization_id=organization_id,
                    project_id=normalized_project,
                    job_id=normalized_job,
                    lock=True,
                )
                operation = str(before["operation"])
                self._require_operation_permission(facts, operation)
                try:
                    internal = self._queue(
                        organization_id,
                        normalized_project,
                    ).retry_job_in_transaction(connection, normalized_job)
                except (ActiveJobError, ValueError) as exc:
                    raise ServerJobControlConflict(
                        "job cannot be retried"
                    ) from exc
                after = self._controlled_job(
                    connection,
                    organization_id=organization_id,
                    project_id=normalized_project,
                    job_id=normalized_job,
                )
                self._append_command_audit(
                    connection,
                    actor=actor,
                    project_id=normalized_project,
                    action="background_job.retried",
                    target_type="background_job",
                    target_id=normalized_job,
                    details={
                        "operation": operation,
                        "from_status": str(before["status"]),
                        "to_status": str(after["status"]),
                        "attempts_reset_from": int(before["attempts"]),
                    },
                )
                if str(internal["operation"]) != operation:
                    raise ServerJobControlUnavailable(
                        "server job control data is inconsistent"
                    )
                return self._job_summary(after)
        except (
            KeyError,
            ProjectAccessDenied,
            ServerJobControlConflict,
        ):
            raise
        except ServerJobControlUnavailable:
            raise
        except Exception as exc:
            raise ServerJobControlUnavailable(
                "server job retry is unavailable"
            ) from exc


__all__ = [
    "PostgresServerJobControlService",
    "SERVER_JOB_CONTROL_OPERATIONS",
    "ServerBatchPage",
    "ServerBatchSummary",
    "ServerJobControlConflict",
    "ServerJobControlUnavailable",
    "ServerJobSummary",
]
