from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Protocol

from services.job_queue import ACTIVE_JOB_STATUSES
from services.job_queue_migration import (
    JobHistorySummary,
    summarize_job_history,
)
from services.postgres_job_queue import PostgresJobQueue
from services.postgres_task_repository import PostgresTaskRepository
from services.task_store_migration import (
    scope_task_records,
    task_records_digest,
)


class LoadableTaskRepository(Protocol):
    def load_all(self) -> list[dict[str, Any]]: ...


class ExportableJobQueue(Protocol):
    def export_batches(self) -> list[dict[str, Any]]: ...


def _record_id(record: dict[str, Any]) -> str:
    return str(record.get("id") or "")


def _canonical_record(record: dict[str, Any]) -> str:
    return json.dumps(
        record,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


@contextmanager
def _read_only_sqlite(
    path: Path,
) -> Iterator[sqlite3.Connection]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(resolved)
    connection = sqlite3.connect(
        f"{resolved.as_uri()}?mode=ro",
        uri=True,
        timeout=30,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    try:
        yield connection
    finally:
        connection.close()


class ReadOnlySQLiteTaskSource:
    """Read an existing Task SQLite database without schema initialization."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path.expanduser().resolve()

    def load_all(self) -> list[dict[str, Any]]:
        with _read_only_sqlite(self.database_path) as connection:
            rows = connection.execute(
                "SELECT payload FROM task_records ORDER BY position, id"
            ).fetchall()
        return [json.loads(str(row["payload"])) for row in rows]


class ReadOnlySQLiteJobSource:
    """Read an existing Job Queue database without recovery or mutation."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path.expanduser().resolve()

    @staticmethod
    def _job(row: sqlite3.Row) -> dict[str, Any]:
        payload = dict(row)
        payload["request"] = json.loads(
            str(payload.pop("request_json") or "{}")
        )
        payload["cancel_requested"] = bool(payload["cancel_requested"])
        return payload

    def export_batches(self) -> list[dict[str, Any]]:
        with _read_only_sqlite(self.database_path) as connection:
            batch_rows = connection.execute(
                "SELECT * FROM batches ORDER BY created_at, id"
            ).fetchall()
            result: list[dict[str, Any]] = []
            for batch_row in batch_rows:
                jobs = [
                    self._job(row)
                    for row in connection.execute(
                        "SELECT * FROM jobs WHERE batch_id = ? "
                        "ORDER BY created_at, topic_index, id",
                        (batch_row["id"],),
                    ).fetchall()
                ]
                status_counts: dict[str, int] = {}
                for job in jobs:
                    status = str(job["status"])
                    status_counts[status] = (
                        status_counts.get(status, 0) + 1
                    )
                active = sum(
                    status_counts.get(status, 0)
                    for status in ACTIVE_JOB_STATUSES
                )
                if active:
                    status = (
                        "running"
                        if status_counts.get("running", 0)
                        else "queued"
                    )
                elif jobs and status_counts.get("succeeded", 0) == len(jobs):
                    status = "succeeded"
                elif jobs and status_counts.get("cancelled", 0) == len(jobs):
                    status = "cancelled"
                else:
                    status = "completed_with_errors"
                result.append(
                    {
                        **dict(batch_row),
                        "status": status,
                        "total": len(jobs),
                        "completed": len(jobs) - active,
                        "status_counts": status_counts,
                        "jobs": jobs,
                    }
                )
        return result


def _index_records(
    records: list[dict[str, Any]],
) -> tuple[dict[str, str], int, tuple[str, ...]]:
    indexed: dict[str, str] = {}
    invalid_count = 0
    duplicates: set[str] = set()
    for record in records:
        record_id = _record_id(record)
        if not record_id:
            invalid_count += 1
            continue
        if record_id in indexed:
            duplicates.add(record_id)
        indexed[record_id] = _canonical_record(record)
    return indexed, invalid_count, tuple(sorted(duplicates))


@dataclass(frozen=True)
class TaskDualReadReport:
    source_count: int
    target_count: int
    source_digest: str
    target_digest: str
    order_matches: bool
    source_only_ids: tuple[str, ...]
    target_only_ids: tuple[str, ...]
    changed_ids: tuple[str, ...]
    source_invalid_id_count: int
    target_invalid_id_count: int
    source_duplicate_ids: tuple[str, ...]
    target_duplicate_ids: tuple[str, ...]

    @property
    def matches(self) -> bool:
        return (
            self.source_count == self.target_count
            and self.source_digest == self.target_digest
            and self.order_matches
            and not self.source_only_ids
            and not self.target_only_ids
            and not self.changed_ids
            and self.source_invalid_id_count == 0
            and self.target_invalid_id_count == 0
            and not self.source_duplicate_ids
            and not self.target_duplicate_ids
        )


@dataclass(frozen=True)
class JobDualReadReport:
    source: JobHistorySummary
    target: JobHistorySummary
    source_active_job_ids: tuple[str, ...]

    @property
    def matches(self) -> bool:
        return (
            not self.source_active_job_ids
            and self.source == self.target
        )


@dataclass(frozen=True)
class ServerCutoverReadinessReport:
    organization_id: str
    project_id: str
    tasks: TaskDualReadReport
    jobs: JobDualReadReport

    @property
    def ready_for_single_write(self) -> bool:
        return self.tasks.matches and self.jobs.matches

    def public_values(self) -> dict[str, object]:
        """Return identifiers and digests only, never Task or Job content."""

        return {
            "organization_id": self.organization_id,
            "project_id": self.project_id,
            "ready_for_single_write": self.ready_for_single_write,
            "tasks": {
                "matches": self.tasks.matches,
                "source_count": self.tasks.source_count,
                "target_count": self.tasks.target_count,
                "source_digest": self.tasks.source_digest,
                "target_digest": self.tasks.target_digest,
                "order_matches": self.tasks.order_matches,
                "source_only_ids": list(self.tasks.source_only_ids),
                "target_only_ids": list(self.tasks.target_only_ids),
                "changed_ids": list(self.tasks.changed_ids),
                "source_invalid_id_count": (
                    self.tasks.source_invalid_id_count
                ),
                "target_invalid_id_count": (
                    self.tasks.target_invalid_id_count
                ),
                "source_duplicate_ids": list(
                    self.tasks.source_duplicate_ids
                ),
                "target_duplicate_ids": list(
                    self.tasks.target_duplicate_ids
                ),
            },
            "jobs": {
                "matches": self.jobs.matches,
                "source_batch_count": self.jobs.source.batch_count,
                "target_batch_count": self.jobs.target.batch_count,
                "source_job_count": self.jobs.source.job_count,
                "target_job_count": self.jobs.target.job_count,
                "source_status_counts": self.jobs.source.status_counts,
                "target_status_counts": self.jobs.target.status_counts,
                "source_digest": self.jobs.source.content_digest,
                "target_digest": self.jobs.target.content_digest,
                "source_active_job_ids": list(
                    self.jobs.source_active_job_ids
                ),
            },
        }


def compare_task_stores(
    source: LoadableTaskRepository,
    target: PostgresTaskRepository,
) -> TaskDualReadReport:
    """Read both stores once and report exact scoped differences."""

    source_records = scope_task_records(source.load_all(), target)
    target_records = target.load_all()
    source_order = tuple(map(_record_id, source_records))
    target_order = tuple(map(_record_id, target_records))
    (
        source_by_id,
        source_invalid_id_count,
        source_duplicate_ids,
    ) = _index_records(source_records)
    (
        target_by_id,
        target_invalid_id_count,
        target_duplicate_ids,
    ) = _index_records(target_records)
    source_ids = set(source_by_id)
    target_ids = set(target_by_id)
    shared = source_ids & target_ids
    return TaskDualReadReport(
        source_count=len(source_records),
        target_count=len(target_records),
        source_digest=task_records_digest(source_records),
        target_digest=task_records_digest(target_records),
        order_matches=source_order == target_order,
        source_only_ids=tuple(sorted(source_ids - target_ids)),
        target_only_ids=tuple(sorted(target_ids - source_ids)),
        changed_ids=tuple(
            sorted(
                task_id
                for task_id in shared
                if source_by_id[task_id] != target_by_id[task_id]
            )
        ),
        source_invalid_id_count=source_invalid_id_count,
        target_invalid_id_count=target_invalid_id_count,
        source_duplicate_ids=source_duplicate_ids,
        target_duplicate_ids=target_duplicate_ids,
    )


def compare_job_queues(
    source: ExportableJobQueue,
    target: PostgresJobQueue,
) -> JobDualReadReport:
    """Read job history from both stores without claiming or changing jobs."""

    source_batches = source.export_batches()
    target_batches = target.export_batches()
    active_ids = tuple(
        sorted(
            str(job.get("id") or "")
            for batch in source_batches
            for job in batch.get("jobs", [])
            if str(job.get("status") or "") in ACTIVE_JOB_STATUSES
        )
    )
    return JobDualReadReport(
        source=summarize_job_history(source_batches),
        target=summarize_job_history(target_batches),
        source_active_job_ids=active_ids,
    )


def build_server_cutover_report(
    *,
    task_source: LoadableTaskRepository,
    task_target: PostgresTaskRepository,
    job_source: ExportableJobQueue,
    job_target: PostgresJobQueue,
) -> ServerCutoverReadinessReport:
    if (
        task_target.organization_id != job_target.organization_id
        or task_target.project_id != job_target.project_id
    ):
        raise ValueError("Task and Job targets must use the same scope")
    return ServerCutoverReadinessReport(
        organization_id=task_target.organization_id,
        project_id=task_target.project_id,
        tasks=compare_task_stores(task_source, task_target),
        jobs=compare_job_queues(job_source, job_target),
    )


__all__ = [
    "JobDualReadReport",
    "LoadableTaskRepository",
    "ReadOnlySQLiteJobSource",
    "ReadOnlySQLiteTaskSource",
    "ServerCutoverReadinessReport",
    "TaskDualReadReport",
    "build_server_cutover_report",
    "compare_job_queues",
    "compare_task_stores",
]
