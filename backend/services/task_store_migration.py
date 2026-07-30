from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from services.postgres_task_repository import PostgresTaskRepository
from services.task_repository import TaskRecordRepository


class TaskStoreMigrationConflict(RuntimeError):
    """Raised when a non-empty target differs from the SQLite source."""


def _scoped_records(
    records: list[dict[str, Any]],
    target: PostgresTaskRepository,
) -> list[dict[str, Any]]:
    scoped: list[dict[str, Any]] = []
    for record in records:
        payload = dict(record)
        payload["organization_id"] = target.organization_id
        payload["project_id"] = target.project_id
        scoped.append(payload)
    return scoped


def _digest(records: list[dict[str, Any]]) -> str:
    canonical = json.dumps(
        records,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TaskStoreMigrationReport:
    organization_id: str
    project_id: str
    source_count: int
    target_before_count: int
    target_after_count: int
    imported: bool
    already_matched: bool
    source_digest: str
    target_digest: str


def migrate_task_store(
    source: TaskRecordRepository,
    target: PostgresTaskRepository,
    *,
    dry_run: bool = False,
) -> TaskStoreMigrationReport:
    """Copy once, compare exactly, and never overwrite a divergent target."""

    source_records = _scoped_records(source.load_all(), target)
    target_before = target.load_all()
    source_digest = _digest(source_records)
    target_before_digest = _digest(target_before)

    if target_before:
        if source_digest != target_before_digest:
            raise TaskStoreMigrationConflict(
                "PostgreSQL task target is non-empty and differs from source"
            )
        return TaskStoreMigrationReport(
            organization_id=target.organization_id,
            project_id=target.project_id,
            source_count=len(source_records),
            target_before_count=len(target_before),
            target_after_count=len(target_before),
            imported=False,
            already_matched=True,
            source_digest=source_digest,
            target_digest=target_before_digest,
        )

    if not dry_run:
        target.replace_all(source_records)
        target_after = target.load_all()
        target_after_digest = _digest(target_after)
        if target_after_digest != source_digest:
            raise TaskStoreMigrationConflict(
                "PostgreSQL task verification digest does not match source"
            )
    else:
        target_after = target_before
        target_after_digest = target_before_digest

    return TaskStoreMigrationReport(
        organization_id=target.organization_id,
        project_id=target.project_id,
        source_count=len(source_records),
        target_before_count=len(target_before),
        target_after_count=len(target_after),
        imported=not dry_run,
        already_matched=False,
        source_digest=source_digest,
        target_digest=target_after_digest,
    )


__all__ = [
    "TaskStoreMigrationConflict",
    "TaskStoreMigrationReport",
    "migrate_task_store",
]
