from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping
from uuid import NAMESPACE_URL, uuid4, uuid5

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine, RowMapping
from sqlalchemy.exc import IntegrityError

from server_schema import (
    article_tasks,
    background_jobs,
    job_batches,
)
from services.audit_log import (
    AuditEvent,
    AuditEventWriter,
)
from services.job_queue import (
    ACTIVE_JOB_STATUSES,
    RETRY_DELAYS_SECONDS,
    ActiveJobError,
    JobStateTransitionError,
    TERMINAL_JOB_STATUSES,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _required_text(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized


def _iso(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return ""


def _epoch(value: object) -> float:
    if isinstance(value, datetime):
        return value.timestamp()
    return 0.0


def _timestamp(value: object, *, fallback: datetime | None = None) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (float, int)):
        parsed = datetime.fromtimestamp(float(value), tz=timezone.utc)
    elif str(value or "").strip():
        parsed = datetime.fromisoformat(str(value).strip())
    elif fallback is not None:
        parsed = fallback
    else:
        raise ValueError("timestamp is required")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class PendingJobAuthorization:
    """Minimal metadata safe to inspect before a Worker receives Job input."""

    job_id: str
    operation: str
    requested_by_user_id: str | None


class PostgresJobQueue:
    """Project-scoped PostgreSQL queue with SKIP LOCKED worker leases."""

    def __init__(
        self,
        engine: Engine,
        *,
        organization_id: str,
        project_id: str,
        worker_id: str | None = None,
        lease_seconds: int = 15 * 60,
        terminal_audit: AuditEventWriter | None = None,
    ) -> None:
        self._engine = engine
        self.organization_id = _required_text(
            organization_id,
            "organization_id",
        )
        self.project_id = _required_text(project_id, "project_id")
        self.worker_id = _required_text(
            worker_id or uuid4().hex,
            "worker_id",
        )
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be greater than zero")
        self.lease_seconds = int(lease_seconds)
        self._terminal_audit = terminal_audit

    def _job_scope(self) -> tuple[sa.ColumnElement[bool], ...]:
        return (
            background_jobs.c.organization_id == self.organization_id,
            background_jobs.c.project_id == self.project_id,
        )

    def _batch_scope(self) -> tuple[sa.ColumnElement[bool], ...]:
        return (
            job_batches.c.organization_id == self.organization_id,
            job_batches.c.project_id == self.project_id,
        )

    def recover_interrupted(
        self,
        operations: Iterable[str] | None = None,
    ) -> int:
        selected_operations = tuple(dict.fromkeys(operations or ()))
        current = _now()
        conditions: list[sa.ColumnElement[bool]] = [
            *self._job_scope(),
            background_jobs.c.status == "running",
            background_jobs.c.lease_expires_at <= current,
        ]
        if selected_operations:
            conditions.append(
                background_jobs.c.operation.in_(selected_operations)
            )
        cancelled = background_jobs.c.cancel_requested.is_(True)
        with self._engine.begin() as connection:
            rows = connection.execute(
                background_jobs.update()
                .where(*conditions)
                .values(
                    status=sa.case(
                        (cancelled, "cancelled"),
                        else_="queued",
                    ),
                    available_at=current,
                    finished_at=sa.case(
                        (cancelled, current),
                        else_=None,
                    ),
                    worker_id=None,
                    lease_expires_at=None,
                    updated_at=current,
                )
                .returning(background_jobs)
            ).mappings().all()
            for row in rows:
                self._append_terminal_audit(connection, row)
        return len(rows)

    def active_task_ids(self, task_ids: Iterable[str]) -> set[str]:
        values = tuple(dict.fromkeys(str(task_id) for task_id in task_ids))
        if not values:
            return set()
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(background_jobs.c.task_id)
                .where(
                    *self._job_scope(),
                    background_jobs.c.task_id.in_(values),
                    background_jobs.c.status.in_(ACTIVE_JOB_STATUSES),
                )
                .distinct()
            ).scalars()
            return {str(task_id) for task_id in rows}

    def delete_customer(self, customer: str) -> None:
        with self._engine.begin() as connection:
            active = connection.execute(
                sa.select(background_jobs.c.job_id)
                .where(
                    *self._job_scope(),
                    background_jobs.c.customer == customer,
                    background_jobs.c.status.in_(ACTIVE_JOB_STATUSES),
                )
                .limit(1)
            ).scalar_one_or_none()
            if active is not None:
                raise ActiveJobError(f"project:{customer}")
            connection.execute(
                job_batches.delete().where(
                    *self._batch_scope(),
                    job_batches.c.customer == customer,
                )
            )

    def rename_customer(
        self,
        customer: str,
        new_customer: str,
        task_id_mapping: dict[str, str],
    ) -> None:
        if not task_id_mapping:
            return
        current = _now()
        with self._engine.begin() as connection:
            for old_id, new_id in task_id_mapping.items():
                connection.execute(
                    background_jobs.update()
                    .where(
                        *self._job_scope(),
                        background_jobs.c.task_id == old_id,
                    )
                    .values(
                        task_id=new_id,
                        customer=new_customer,
                        updated_at=current,
                    )
                )
            connection.execute(
                job_batches.update()
                .where(
                    *self._batch_scope(),
                    job_batches.c.customer == customer,
                )
                .values(customer=new_customer, updated_at=current)
            )

    def create_batch(
        self,
        operation: str,
        items: list[dict[str, Any]],
        *,
        customer: str = "",
        requested_by_user_id: str | None = None,
    ) -> dict[str, Any]:
        with self._engine.begin() as connection:
            return self.create_batch_in_transaction(
                connection,
                operation,
                items,
                customer=customer,
                requested_by_user_id=requested_by_user_id,
            )

    def create_batch_in_transaction(
        self,
        connection: Connection,
        operation: str,
        items: list[dict[str, Any]],
        *,
        customer: str = "",
        requested_by_user_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a batch inside a caller-owned authorization/audit transaction."""

        if not connection.in_transaction():
            raise ValueError("job creation requires a business transaction")
        normalized_operation = _required_text(operation, "operation")
        normalized_requester = (
            _required_text(
                requested_by_user_id,
                "requested_by_user_id",
            )
            if requested_by_user_id is not None
            else None
        )
        if not items:
            raise ValueError("A batch requires at least one job.")
        task_ids = tuple(
            _required_text(str(item["task_id"]), "task_id")
            for item in items
        )
        if len(task_ids) != len(set(task_ids)):
            raise ActiveJobError(task_ids[0])
        batch_id = uuid4().hex
        current = _now()
        try:
            known_task_ids = set(
                connection.execute(
                    sa.select(article_tasks.c.task_id).where(
                        article_tasks.c.organization_id
                        == self.organization_id,
                        article_tasks.c.project_id == self.project_id,
                        article_tasks.c.task_id.in_(task_ids),
                    )
                ).scalars()
            )
            missing = next(
                (
                    task_id
                    for task_id in task_ids
                    if task_id not in known_task_ids
                ),
                None,
            )
            if missing is not None:
                raise KeyError(missing)
            active = connection.execute(
                sa.select(background_jobs.c.task_id)
                .where(
                    *self._job_scope(),
                    background_jobs.c.task_id.in_(task_ids),
                    background_jobs.c.status.in_(ACTIVE_JOB_STATUSES),
                )
                .limit(1)
            ).scalar_one_or_none()
            if active is not None:
                raise ActiveJobError(str(active))

            connection.execute(
                job_batches.insert().values(
                    organization_id=self.organization_id,
                    project_id=self.project_id,
                    batch_id=batch_id,
                    operation=normalized_operation,
                    customer=customer,
                    created_at=current,
                    updated_at=current,
                )
            )
            connection.execute(
                background_jobs.insert(),
                tuple(
                    {
                        "organization_id": self.organization_id,
                        "project_id": self.project_id,
                        "job_id": uuid4().hex,
                        "batch_id": batch_id,
                        "task_id": task_id,
                        "requested_by_user_id": normalized_requester,
                        "customer": str(item.get("customer", "")),
                        "topic_index": int(item.get("topic_index", 0)),
                        "topic": str(item.get("topic", "")),
                        "operation": normalized_operation,
                        "status": "queued",
                        "request": dict(item.get("request", {})),
                        "source_revision": int(item["source_revision"]),
                        "available_at": current,
                        "created_at": current,
                        "updated_at": current,
                    }
                    for task_id, item in zip(task_ids, items, strict=True)
                ),
            )
        except IntegrityError as exc:
            raise ActiveJobError(task_ids[0]) from exc
        return self._get_batch_in_connection(connection, batch_id)

    def claim_jobs(
        self,
        limit: int,
        operations: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        selected_operations = tuple(dict.fromkeys(operations or ()))
        current = _now()
        conditions: list[sa.ColumnElement[bool]] = [
            *self._job_scope(),
            background_jobs.c.status.in_(("queued", "retry_wait")),
            background_jobs.c.available_at <= current,
            background_jobs.c.cancel_requested.is_(False),
        ]
        if selected_operations:
            conditions.append(
                background_jobs.c.operation.in_(selected_operations)
            )
        with self._engine.begin() as connection:
            rows = connection.execute(
                sa.select(background_jobs)
                .where(*conditions)
                .order_by(
                    background_jobs.c.available_at,
                    background_jobs.c.created_at,
                    background_jobs.c.topic_index,
                    background_jobs.c.job_id,
                )
                .limit(limit)
                .with_for_update(skip_locked=True)
            ).mappings().all()
            claimed: list[dict[str, Any]] = []
            for row in rows:
                current_row = connection.execute(
                    background_jobs.update()
                    .where(
                        *self._job_scope(),
                        background_jobs.c.job_id == row["job_id"],
                        background_jobs.c.status.in_(
                            ("queued", "retry_wait")
                        ),
                    )
                    .values(
                        status="running",
                        attempts=background_jobs.c.attempts + 1,
                        started_at=current,
                        worker_id=self.worker_id,
                        lease_expires_at=current
                        + timedelta(seconds=self.lease_seconds),
                        updated_at=current,
                    )
                    .returning(background_jobs)
                ).mappings().one_or_none()
                if current_row is not None:
                    claimed.append(self._job_dict(current_row))
        return claimed

    def list_claim_candidates(
        self,
        limit: int,
        operations: Iterable[str] | None = None,
    ) -> list[PendingJobAuthorization]:
        if limit <= 0:
            return []
        selected_operations = tuple(dict.fromkeys(operations or ()))
        current = _now()
        conditions: list[sa.ColumnElement[bool]] = [
            *self._job_scope(),
            background_jobs.c.status.in_(("queued", "retry_wait")),
            background_jobs.c.available_at <= current,
            background_jobs.c.cancel_requested.is_(False),
        ]
        if selected_operations:
            conditions.append(
                background_jobs.c.operation.in_(selected_operations)
            )
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(
                    background_jobs.c.job_id,
                    background_jobs.c.operation,
                    background_jobs.c.requested_by_user_id,
                )
                .where(*conditions)
                .order_by(
                    background_jobs.c.available_at,
                    background_jobs.c.created_at,
                    background_jobs.c.topic_index,
                    background_jobs.c.job_id,
                )
                .limit(limit)
            ).mappings().all()
        return [
            PendingJobAuthorization(
                job_id=str(row["job_id"]),
                operation=str(row["operation"]),
                requested_by_user_id=(
                    str(row["requested_by_user_id"])
                    if row["requested_by_user_id"] is not None
                    else None
                ),
            )
            for row in rows
        ]

    def claim_jobs_by_id(
        self,
        job_ids: Iterable[str],
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        values = tuple(
            dict.fromkeys(
                _required_text(str(job_id), "job_id")
                for job_id in job_ids
            )
        )
        if limit <= 0 or not values:
            return []
        current = _now()
        with self._engine.begin() as connection:
            rows = connection.execute(
                sa.select(background_jobs)
                .where(
                    *self._job_scope(),
                    background_jobs.c.job_id.in_(values),
                    background_jobs.c.status.in_(
                        ("queued", "retry_wait")
                    ),
                    background_jobs.c.available_at <= current,
                    background_jobs.c.cancel_requested.is_(False),
                )
                .order_by(
                    background_jobs.c.available_at,
                    background_jobs.c.created_at,
                    background_jobs.c.topic_index,
                    background_jobs.c.job_id,
                )
                .limit(limit)
                .with_for_update(skip_locked=True)
            ).mappings().all()
            claimed: list[dict[str, Any]] = []
            for row in rows:
                current_row = connection.execute(
                    background_jobs.update()
                    .where(
                        *self._job_scope(),
                        background_jobs.c.job_id == row["job_id"],
                        background_jobs.c.status.in_(("queued", "retry_wait")),
                    )
                    .values(
                        status="running",
                        attempts=background_jobs.c.attempts + 1,
                        started_at=current,
                        worker_id=self.worker_id,
                        lease_expires_at=current
                        + timedelta(seconds=self.lease_seconds),
                        updated_at=current,
                    )
                    .returning(background_jobs)
                ).mappings().one_or_none()
                if current_row is not None:
                    claimed.append(self._job_dict(current_row))
        return claimed

    def reject_pending_authorization(
        self,
        job_id: str,
    ) -> bool:
        """Reject a pending Job without reading its private request payload."""

        current = _now()
        with self._engine.begin() as connection:
            row = connection.execute(
                background_jobs.update()
                .where(
                    *self._job_scope(),
                    background_jobs.c.job_id == job_id,
                    background_jobs.c.status.in_(
                        ("queued", "retry_wait")
                    ),
                )
                .values(
                    status="conflict",
                    error="job actor is not authorized",
                    finished_at=current,
                    worker_id=None,
                    lease_expires_at=None,
                    updated_at=current,
                )
                .returning(background_jobs)
            ).mappings().one_or_none()
            if row is not None:
                self._touch_batch(connection, job_id, current)
                self._append_terminal_audit(connection, row)
        return row is not None

    def renew_lease(self, job_id: str) -> bool:
        current = _now()
        with self._engine.begin() as connection:
            result = connection.execute(
                background_jobs.update()
                .where(
                    *self._job_scope(),
                    background_jobs.c.job_id == job_id,
                    background_jobs.c.status == "running",
                    background_jobs.c.worker_id == self.worker_id,
                )
                .values(
                    lease_expires_at=current
                    + timedelta(seconds=self.lease_seconds),
                    updated_at=current,
                )
            )
        return bool(result.rowcount)

    def is_cancel_requested(self, job_id: str) -> bool:
        with self._engine.connect() as connection:
            row = connection.execute(
                sa.select(
                    background_jobs.c.cancel_requested,
                    background_jobs.c.status,
                ).where(
                    *self._job_scope(),
                    background_jobs.c.job_id == job_id,
                )
            ).one_or_none()
        return bool(
            row
            and (
                bool(row.cancel_requested)
                or str(row.status) == "cancelled"
            )
        )

    def mark_succeeded(self, job_id: str, result_revision: int) -> None:
        self._mark_running(
            job_id,
            status="succeeded",
            result_revision=int(result_revision),
        )

    def mark_cancelled(self, job_id: str) -> None:
        current = _now()
        with self._engine.begin() as connection:
            row = connection.execute(
                background_jobs.update()
                .where(
                    *self._job_scope(),
                    background_jobs.c.job_id == job_id,
                    background_jobs.c.status.in_(
                        ("queued", "retry_wait", "running")
                    ),
                    sa.or_(
                        background_jobs.c.status != "running",
                        background_jobs.c.worker_id == self.worker_id,
                    ),
                )
                .values(
                    status="cancelled",
                    cancel_requested=True,
                    finished_at=current,
                    worker_id=None,
                    lease_expires_at=None,
                    updated_at=current,
                )
                .returning(background_jobs)
            ).mappings().one_or_none()
            if row is not None:
                self._touch_batch(connection, job_id, current)
                self._append_terminal_audit(connection, row)

    def mark_interrupted(self, job_id: str) -> None:
        """Release this worker's claim for a controlled service shutdown."""

        current = _now()
        with self._engine.begin() as connection:
            result = connection.execute(
                background_jobs.update()
                .where(
                    *self._job_scope(),
                    background_jobs.c.job_id == job_id,
                    background_jobs.c.status == "running",
                    background_jobs.c.worker_id == self.worker_id,
                    background_jobs.c.cancel_requested.is_(False),
                )
                .values(
                    status="queued",
                    available_at=current,
                    error="",
                    started_at=None,
                    finished_at=None,
                    worker_id=None,
                    lease_expires_at=None,
                    updated_at=current,
                )
            )
            if result.rowcount:
                self._touch_batch(connection, job_id, current)

    def mark_conflict(self, job_id: str, error: str) -> None:
        self._mark_running(
            job_id,
            status="conflict",
            error=str(error or "")[:4000],
        )

    def mark_failed(
        self,
        job_id: str,
        error: str,
        *,
        retryable: bool,
    ) -> str:
        normalized_error = str(error or "Unknown batch job error")[:4000]
        current = _now()
        with self._engine.begin() as connection:
            row = connection.execute(
                sa.select(
                    background_jobs.c.attempts,
                    background_jobs.c.max_attempts,
                    background_jobs.c.cancel_requested,
                )
                .where(
                    *self._job_scope(),
                    background_jobs.c.job_id == job_id,
                    background_jobs.c.status == "running",
                    background_jobs.c.worker_id == self.worker_id,
                )
                .with_for_update()
            ).one_or_none()
            if row is None:
                return "failed"
            values: dict[str, object] = {
                "error": normalized_error,
                "worker_id": None,
                "lease_expires_at": None,
                "updated_at": current,
            }
            if row.cancel_requested:
                status = "cancelled"
                values.update(status=status, finished_at=current)
            elif retryable and int(row.attempts) < int(row.max_attempts):
                retry_number = max(1, int(row.attempts))
                delay_index = min(
                    retry_number - 1,
                    len(RETRY_DELAYS_SECONDS) - 1,
                )
                status = "retry_wait"
                values.update(
                    status=status,
                    available_at=current
                    + timedelta(seconds=RETRY_DELAYS_SECONDS[delay_index]),
                    finished_at=None,
                )
            else:
                status = "failed"
                values.update(status=status, finished_at=current)
            updated = connection.execute(
                background_jobs.update()
                .where(
                    *self._job_scope(),
                    background_jobs.c.job_id == job_id,
                    background_jobs.c.status == "running",
                    background_jobs.c.worker_id == self.worker_id,
                )
                .values(**values)
                .returning(background_jobs)
            ).mappings().one_or_none()
            self._touch_batch(connection, job_id, current)
            if (
                updated is not None
                and str(updated["status"]) in TERMINAL_JOB_STATUSES
            ):
                self._append_terminal_audit(connection, updated)
        return status

    def _mark_running(
        self,
        job_id: str,
        *,
        status: str,
        error: str = "",
        result_revision: int | None = None,
    ) -> None:
        current = _now()
        values: dict[str, object] = {
            "status": status,
            "error": error,
            "finished_at": current,
            "worker_id": None,
            "lease_expires_at": None,
            "updated_at": current,
        }
        if result_revision is not None:
            values["result_revision"] = result_revision
            values["cancel_requested"] = False
        with self._engine.begin() as connection:
            row = connection.execute(
                background_jobs.update()
                .where(
                    *self._job_scope(),
                    background_jobs.c.job_id == job_id,
                    background_jobs.c.status == "running",
                    background_jobs.c.worker_id == self.worker_id,
                )
                .values(**values)
                .returning(background_jobs)
            ).mappings().one_or_none()
            if row is not None:
                self._touch_batch(connection, job_id, current)
                self._append_terminal_audit(connection, row)

    def _append_terminal_audit(
        self,
        connection: Connection,
        row: Mapping[str, object] | RowMapping,
    ) -> None:
        if self._terminal_audit is None:
            return
        status = str(row["status"])
        if status not in TERMINAL_JOB_STATUSES:
            return
        job_id = str(row["job_id"])
        attempts = int(row["attempts"])
        details: dict[str, object] = {
            "operation": str(row["operation"]),
            "status": status,
            "attempts": attempts,
            "source_revision": int(row["source_revision"]),
        }
        if row["result_revision"] is not None:
            details["result_revision"] = int(row["result_revision"])
        identity = "\n".join(
            (
                self.organization_id,
                self.project_id,
                job_id,
                status,
                str(attempts),
            )
        )
        try:
            self._terminal_audit.append(
                connection,
                AuditEvent(
                    organization_id=self.organization_id,
                    event_id=f"job_{uuid5(NAMESPACE_URL, identity).hex}",
                    actor_user_id=(
                        None
                        if row["requested_by_user_id"] is None
                        else str(row["requested_by_user_id"])
                    ),
                    project_id=self.project_id,
                    action="background_job.terminal",
                    target_type="background_job",
                    target_id=job_id,
                    details=details,
                ),
            )
        except Exception as exc:
            raise JobStateTransitionError(
                "job terminal state could not be committed"
            ) from exc

    def request_cancel(self, job_id: str) -> dict[str, Any]:
        current = _now()
        with self._engine.begin() as connection:
            row = connection.execute(
                sa.select(background_jobs)
                .where(
                    *self._job_scope(),
                    background_jobs.c.job_id == job_id,
                )
                .with_for_update()
            ).one_or_none()
            if row is None:
                raise KeyError(job_id)
            if row.status in ("queued", "retry_wait"):
                cancelled = connection.execute(
                    background_jobs.update()
                    .where(
                        *self._job_scope(),
                        background_jobs.c.job_id == job_id,
                    )
                    .values(
                        status="cancelled",
                        cancel_requested=True,
                        finished_at=current,
                        worker_id=None,
                        lease_expires_at=None,
                        updated_at=current,
                    )
                    .returning(background_jobs)
                ).mappings().one()
                self._append_terminal_audit(connection, cancelled)
            elif row.status == "running":
                connection.execute(
                    background_jobs.update()
                    .where(
                        *self._job_scope(),
                        background_jobs.c.job_id == job_id,
                    )
                    .values(cancel_requested=True, updated_at=current)
                )
            self._touch_batch(connection, job_id, current)
        return self.get_job(job_id)

    def cancel_batch(self, batch_id: str) -> dict[str, Any]:
        current = _now()
        with self._engine.begin() as connection:
            exists = connection.execute(
                sa.select(job_batches.c.batch_id).where(
                    *self._batch_scope(),
                    job_batches.c.batch_id == batch_id,
                )
            ).scalar_one_or_none()
            if exists is None:
                raise KeyError(batch_id)
            queued = background_jobs.c.status.in_(("queued", "retry_wait"))
            active = background_jobs.c.status.in_(ACTIVE_JOB_STATUSES)
            rows = connection.execute(
                background_jobs.update()
                .where(
                    *self._job_scope(),
                    background_jobs.c.batch_id == batch_id,
                    active,
                )
                .values(
                    status=sa.case((queued, "cancelled"), else_=background_jobs.c.status),
                    cancel_requested=True,
                    finished_at=sa.case(
                        (queued, current),
                        else_=background_jobs.c.finished_at,
                    ),
                    worker_id=sa.case(
                        (queued, None),
                        else_=background_jobs.c.worker_id,
                    ),
                    lease_expires_at=sa.case(
                        (queued, None),
                        else_=background_jobs.c.lease_expires_at,
                    ),
                    updated_at=current,
                )
                .returning(background_jobs)
            ).mappings().all()
            for row in rows:
                self._append_terminal_audit(connection, row)
            connection.execute(
                job_batches.update()
                .where(
                    *self._batch_scope(),
                    job_batches.c.batch_id == batch_id,
                )
                .values(updated_at=current)
            )
        return self.get_batch(batch_id)

    def retry_job(
        self,
        job_id: str,
        *,
        source_revision: int | None = None,
        request: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        current = _now()
        with self._engine.begin() as connection:
            row = connection.execute(
                sa.select(
                    background_jobs.c.task_id,
                    background_jobs.c.status,
                )
                .where(
                    *self._job_scope(),
                    background_jobs.c.job_id == job_id,
                )
                .with_for_update()
            ).one_or_none()
            if row is None:
                raise KeyError(job_id)
            if row.status not in ("failed", "cancelled", "conflict"):
                raise ValueError(
                    "Only failed, cancelled, or conflicted jobs can be retried."
                )
            active = connection.execute(
                sa.select(background_jobs.c.job_id)
                .where(
                    *self._job_scope(),
                    background_jobs.c.task_id == row.task_id,
                    background_jobs.c.job_id != job_id,
                    background_jobs.c.status.in_(ACTIVE_JOB_STATUSES),
                )
                .limit(1)
            ).scalar_one_or_none()
            if active is not None:
                raise ActiveJobError(str(row.task_id))
            values: dict[str, object] = {
                "status": "queued",
                "attempts": 0,
                "available_at": current,
                "cancel_requested": False,
                "error": "",
                "started_at": None,
                "finished_at": None,
                "worker_id": None,
                "lease_expires_at": None,
                "updated_at": current,
            }
            if source_revision is not None:
                values["source_revision"] = int(source_revision)
            if request is not None:
                values["request"] = dict(request)
            connection.execute(
                background_jobs.update()
                .where(
                    *self._job_scope(),
                    background_jobs.c.job_id == job_id,
                )
                .values(**values)
            )
            self._touch_batch(connection, job_id, current)
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self._engine.connect() as connection:
            row = connection.execute(
                sa.select(background_jobs).where(
                    *self._job_scope(),
                    background_jobs.c.job_id == job_id,
                )
            ).mappings().one_or_none()
        if row is None:
            raise KeyError(job_id)
        return self._job_dict(row)

    def get_batch(self, batch_id: str) -> dict[str, Any]:
        with self._engine.connect() as connection:
            return self._get_batch_in_connection(connection, batch_id)

    def _get_batch_in_connection(
        self,
        connection: Connection,
        batch_id: str,
    ) -> dict[str, Any]:
        batch = connection.execute(
            sa.select(job_batches).where(
                *self._batch_scope(),
                job_batches.c.batch_id == batch_id,
            )
        ).mappings().one_or_none()
        if batch is None:
            raise KeyError(batch_id)
        jobs = connection.execute(
            sa.select(background_jobs)
            .where(
                *self._job_scope(),
                background_jobs.c.batch_id == batch_id,
            )
            .order_by(
                background_jobs.c.created_at,
                background_jobs.c.topic_index,
                background_jobs.c.job_id,
            )
        ).mappings().all()
        return self._batch_dict(batch, jobs)

    def list_batches(
        self,
        *,
        customer: str = "",
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        conditions: list[sa.ColumnElement[bool]] = [*self._batch_scope()]
        if customer:
            conditions.append(job_batches.c.customer == customer)
        with self._engine.connect() as connection:
            batches = connection.execute(
                sa.select(job_batches)
                .where(*conditions)
                .order_by(job_batches.c.created_at.desc())
                .limit(max(1, min(int(limit), 100)))
            ).mappings().all()
            result: list[dict[str, Any]] = []
            for batch in batches:
                jobs = connection.execute(
                    sa.select(background_jobs)
                    .where(
                        *self._job_scope(),
                        background_jobs.c.batch_id == batch["batch_id"],
                    )
                    .order_by(
                        background_jobs.c.created_at,
                        background_jobs.c.topic_index,
                        background_jobs.c.job_id,
                    )
                ).mappings().all()
                result.append(self._batch_dict(batch, jobs))
        return result

    def export_batches(self) -> list[dict[str, Any]]:
        with self._engine.connect() as connection:
            batches = connection.execute(
                sa.select(job_batches)
                .where(*self._batch_scope())
                .order_by(job_batches.c.created_at, job_batches.c.batch_id)
            ).mappings().all()
            result: list[dict[str, Any]] = []
            for batch in batches:
                jobs = connection.execute(
                    sa.select(background_jobs)
                    .where(
                        *self._job_scope(),
                        background_jobs.c.batch_id == batch["batch_id"],
                    )
                    .order_by(
                        background_jobs.c.created_at,
                        background_jobs.c.topic_index,
                        background_jobs.c.job_id,
                    )
                ).mappings().all()
                result.append(self._batch_dict(batch, jobs))
        return result

    def import_terminal_batches(
        self,
        batches: Iterable[Mapping[str, Any]],
    ) -> None:
        """Import drained SQLite history while preserving stable identities."""

        payloads = [dict(batch) for batch in batches]
        active = next(
            (
                str(job.get("id") or "")
                for batch in payloads
                for job in batch.get("jobs", [])
                if str(job.get("status") or "") in ACTIVE_JOB_STATUSES
            ),
            None,
        )
        if active is not None:
            raise ValueError(
                "active SQLite jobs must be drained before migration"
            )
        current = _now()
        with self._engine.begin() as connection:
            target_exists = connection.execute(
                sa.select(job_batches.c.batch_id)
                .where(*self._batch_scope())
                .limit(1)
            ).scalar_one_or_none()
            if target_exists is not None:
                raise ValueError(
                    "PostgreSQL job target must be empty before import"
                )
            for batch in payloads:
                batch_id = _required_text(
                    str(batch.get("id") or ""),
                    "batch id",
                )
                jobs = [dict(job) for job in batch.get("jobs", [])]
                created_at = _timestamp(
                    batch.get("created_at"),
                    fallback=current,
                )
                updated_at = _timestamp(
                    batch.get("updated_at"),
                    fallback=created_at,
                )
                connection.execute(
                    job_batches.insert().values(
                        organization_id=self.organization_id,
                        project_id=self.project_id,
                        batch_id=batch_id,
                        operation=_required_text(
                            str(batch.get("operation") or ""),
                            "operation",
                        ),
                        customer=str(batch.get("customer") or ""),
                        created_at=created_at,
                        updated_at=updated_at,
                    )
                )
                if not jobs:
                    continue
                connection.execute(
                    background_jobs.insert(),
                    tuple(
                        {
                            "organization_id": self.organization_id,
                            "project_id": self.project_id,
                            "job_id": _required_text(
                                str(job.get("id") or ""),
                                "job id",
                            ),
                            "batch_id": batch_id,
                            "task_id": _required_text(
                                str(job.get("task_id") or ""),
                                "task_id",
                            ),
                            # SQLite history has no trusted Actor boundary.
                            # Never promote an extension field from the legacy
                            # payload into a server identity.
                            "requested_by_user_id": None,
                            "customer": str(job.get("customer") or ""),
                            "topic_index": int(job.get("topic_index") or 0),
                            "topic": str(job.get("topic") or ""),
                            "operation": _required_text(
                                str(job.get("operation") or ""),
                                "operation",
                            ),
                            "status": str(job.get("status") or ""),
                            "request": dict(job.get("request") or {}),
                            "source_revision": int(
                                job.get("source_revision") or 0
                            ),
                            "result_revision": (
                                int(job["result_revision"])
                                if job.get("result_revision") is not None
                                else None
                            ),
                            "attempts": int(job.get("attempts") or 0),
                            "max_attempts": int(
                                job.get("max_attempts") or 4
                            ),
                            "available_at": _timestamp(
                                job.get("available_at"),
                                fallback=created_at,
                            ),
                            "cancel_requested": bool(
                                job.get("cancel_requested")
                            ),
                            "error": str(job.get("error") or "")[:4000],
                            "worker_id": None,
                            "lease_expires_at": None,
                            "created_at": _timestamp(
                                job.get("created_at"),
                                fallback=created_at,
                            ),
                            "started_at": (
                                _timestamp(job["started_at"])
                                if str(job.get("started_at") or "").strip()
                                else None
                            ),
                            "finished_at": (
                                _timestamp(job["finished_at"])
                                if str(job.get("finished_at") or "").strip()
                                else None
                            ),
                            "updated_at": _timestamp(
                                job.get("updated_at"),
                                fallback=updated_at,
                            ),
                        }
                        for job in jobs
                    ),
                )

    def _touch_batch(
        self,
        connection: Connection,
        job_id: str,
        current: datetime,
    ) -> None:
        batch_id = connection.execute(
            sa.select(background_jobs.c.batch_id).where(
                *self._job_scope(),
                background_jobs.c.job_id == job_id,
            )
        ).scalar_one_or_none()
        if batch_id is not None:
            connection.execute(
                job_batches.update()
                .where(
                    *self._batch_scope(),
                    job_batches.c.batch_id == batch_id,
                )
                .values(updated_at=current)
            )

    @staticmethod
    def _job_dict(row: Mapping[str, object] | RowMapping) -> dict[str, Any]:
        return {
            "id": str(row["job_id"]),
            "batch_id": str(row["batch_id"]),
            "task_id": str(row["task_id"]),
            "requested_by_user_id": (
                str(row["requested_by_user_id"])
                if row["requested_by_user_id"] is not None
                else None
            ),
            "customer": str(row["customer"]),
            "topic_index": int(row["topic_index"]),
            "topic": str(row["topic"]),
            "operation": str(row["operation"]),
            "status": str(row["status"]),
            "request": dict(row["request"] or {}),  # type: ignore[arg-type]
            "source_revision": int(row["source_revision"]),
            "result_revision": (
                int(row["result_revision"])
                if row["result_revision"] is not None
                else None
            ),
            "attempts": int(row["attempts"]),
            "max_attempts": int(row["max_attempts"]),
            "available_at": _epoch(row["available_at"]),
            "cancel_requested": bool(row["cancel_requested"]),
            "error": str(row["error"]),
            "created_at": _iso(row["created_at"]),
            "started_at": _iso(row["started_at"]),
            "finished_at": _iso(row["finished_at"]),
            "updated_at": _iso(row["updated_at"]),
            "organization_id": str(row["organization_id"]),
            "project_id": str(row["project_id"]),
        }

    def _batch_dict(
        self,
        row: Mapping[str, object] | RowMapping,
        job_rows: Iterable[Mapping[str, object] | RowMapping],
    ) -> dict[str, Any]:
        jobs = [self._job_dict(job) for job in job_rows]
        counts: dict[str, int] = {}
        for job in jobs:
            counts[job["status"]] = counts.get(job["status"], 0) + 1
        active_count = sum(
            counts.get(status, 0) for status in ACTIVE_JOB_STATUSES
        )
        succeeded = counts.get("succeeded", 0)
        if active_count:
            status = "running" if counts.get("running", 0) else "queued"
        elif jobs and succeeded == len(jobs):
            status = "succeeded"
        elif jobs and counts.get("cancelled", 0) == len(jobs):
            status = "cancelled"
        else:
            status = "completed_with_errors"
        return {
            "id": str(row["batch_id"]),
            "operation": str(row["operation"]),
            "customer": str(row["customer"]),
            "created_at": _iso(row["created_at"]),
            "updated_at": _iso(row["updated_at"]),
            "organization_id": str(row["organization_id"]),
            "project_id": str(row["project_id"]),
            "status": status,
            "total": len(jobs),
            "completed": len(jobs) - active_count,
            "status_counts": counts,
            "jobs": jobs,
        }


__all__ = ["PendingJobAuthorization", "PostgresJobQueue"]
