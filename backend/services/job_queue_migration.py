from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import json
from typing import Any, Protocol

from services.job_queue import ACTIVE_JOB_STATUSES
from services.postgres_job_queue import PostgresJobQueue


class ExportableJobQueue(Protocol):
    def export_batches(self) -> list[dict[str, Any]]: ...


class JobQueueMigrationConflict(RuntimeError):
    """Raised when safe single-queue cutover conditions are not met."""


@dataclass(frozen=True)
class JobHistorySummary:
    batch_count: int
    job_count: int
    status_counts: dict[str, int]
    batch_ids: tuple[str, ...]
    job_ids: tuple[str, ...]
    content_digest: str


@dataclass(frozen=True)
class JobQueueMigrationReport:
    organization_id: str
    project_id: str
    source: JobHistorySummary
    target_before: JobHistorySummary
    target_after: JobHistorySummary
    imported: bool
    already_matched: bool


def _summary(batches: list[dict[str, Any]]) -> JobHistorySummary:
    jobs = [
        dict(job)
        for batch in batches
        for job in batch.get("jobs", [])
    ]
    canonical = [
        {
            "id": str(batch.get("id") or ""),
            "operation": str(batch.get("operation") or ""),
            "customer": str(batch.get("customer") or ""),
            "jobs": sorted(
                (
                    {
                        "id": str(job.get("id") or ""),
                        "task_id": str(job.get("task_id") or ""),
                        "customer": str(job.get("customer") or ""),
                        "topic_index": int(job.get("topic_index") or 0),
                        "topic": str(job.get("topic") or ""),
                        "operation": str(job.get("operation") or ""),
                        "status": str(job.get("status") or ""),
                        "request": dict(job.get("request") or {}),
                        "source_revision": int(
                            job.get("source_revision") or 0
                        ),
                        "result_revision": job.get("result_revision"),
                        "attempts": int(job.get("attempts") or 0),
                        "max_attempts": int(job.get("max_attempts") or 4),
                        "cancel_requested": bool(
                            job.get("cancel_requested")
                        ),
                        "error": str(job.get("error") or ""),
                    }
                    for job in batch.get("jobs", [])
                ),
                key=lambda item: item["id"],
            ),
        }
        for batch in sorted(batches, key=lambda item: str(item.get("id") or ""))
    ]
    content_digest = hashlib.sha256(
        json.dumps(
            canonical,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return JobHistorySummary(
        batch_count=len(batches),
        job_count=len(jobs),
        status_counts=dict(
            sorted(Counter(str(job.get("status") or "") for job in jobs).items())
        ),
        batch_ids=tuple(sorted(str(batch.get("id") or "") for batch in batches)),
        job_ids=tuple(sorted(str(job.get("id") or "") for job in jobs)),
        content_digest=content_digest,
    )


def migrate_terminal_job_history(
    source: ExportableJobQueue,
    target: PostgresJobQueue,
    *,
    dry_run: bool = False,
) -> JobQueueMigrationReport:
    source_batches = source.export_batches()
    source_summary = _summary(source_batches)
    active_ids = tuple(
        str(job.get("id") or "")
        for batch in source_batches
        for job in batch.get("jobs", [])
        if str(job.get("status") or "") in ACTIVE_JOB_STATUSES
    )
    if active_ids:
        raise JobQueueMigrationConflict(
            "SQLite job queue still contains active jobs"
        )

    target_before_batches = target.export_batches()
    target_before = _summary(target_before_batches)
    if target_before.job_count or target_before.batch_count:
        if target_before != source_summary:
            raise JobQueueMigrationConflict(
                "PostgreSQL job target differs from SQLite history"
            )
        return JobQueueMigrationReport(
            organization_id=target.organization_id,
            project_id=target.project_id,
            source=source_summary,
            target_before=target_before,
            target_after=target_before,
            imported=False,
            already_matched=True,
        )

    if not dry_run:
        try:
            target.import_terminal_batches(source_batches)
        except (ValueError, KeyError) as exc:
            raise JobQueueMigrationConflict(
                "PostgreSQL job history import failed validation"
            ) from exc
        target_after = _summary(target.export_batches())
        if target_after != source_summary:
            raise JobQueueMigrationConflict(
                "PostgreSQL job history verification differs from source"
            )
    else:
        target_after = target_before

    return JobQueueMigrationReport(
        organization_id=target.organization_id,
        project_id=target.project_id,
        source=source_summary,
        target_before=target_before,
        target_after=target_after,
        imported=not dry_run,
        already_matched=False,
    )


__all__ = [
    "JobHistorySummary",
    "JobQueueMigrationConflict",
    "JobQueueMigrationReport",
    "migrate_terminal_job_history",
]
