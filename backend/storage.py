from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any, Iterable, Mapping

from config import AppConfig
from models import (
    SCHEMA_VERSION,
    STATUS_DOCX_EXPORTED,
    STATUS_DRAFT_READY,
    AICheck,
    TaskRecord,
)


_TASK_STORE_LOCK = RLock()


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest() if content else ""


V2_DEFAULTS: dict[str, Any] = {
    "revision": 0,
    "workflow_error": None,
    "hero_image": "",
    "raw_draft_article": "",
    "initial_article": "",
    "humanized_article": "",
    "linked_article": "",
    "final_article": "",
    "article_versions": [],
    "raw_draft_word_count": 0,
    "raw_draft_hash": "",
    "initial_article_word_count": 0,
    "initial_article_hash": "",
    "humanized_article_word_count": 0,
    "humanized_article_hash": "",
    "linked_article_word_count": 0,
    "linked_article_hash": "",
    "final_article_word_count": 0,
    "final_article_hash": "",
    "initial_ai_check": AICheck().model_dump(mode="json"),
    "final_ai_check": AICheck().model_dump(mode="json"),
    "source_links": [],
    "link_validation": {
        "passed": False,
        "source_count": 0,
        "preserved_count": 0,
        "missing_links": [],
        "unexpected_links": [],
        "visible_text_unchanged": None,
        "article_hash": "",
        "verified_at": "",
        "error": "",
    },
    "images": [],
    "transition_added": False,
    "legacy_export": False,
}


class RevisionConflictError(RuntimeError):
    def __init__(self, task_id: str, expected: int, actual: int):
        self.task_id = task_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"Task {task_id} revision conflict: expected {expected}, current {actual}."
        )


def migrate_v1_to_v2(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return a schema-v2 copy without dropping fields unknown to this app."""

    migrated = copy.deepcopy(dict(payload))
    for key, default in V2_DEFAULTS.items():
        migrated.setdefault(key, copy.deepcopy(default))

    status = str(migrated.get("status") or "")
    legacy_article = str(migrated.get("article") or "")

    if status == STATUS_DRAFT_READY and legacy_article:
        migrated["initial_article"] = migrated.get("initial_article") or legacy_article
        migrated["initial_article_hash"] = (
            migrated.get("initial_article_hash")
            or content_hash(str(migrated["initial_article"]))
        )

    if status == STATUS_DOCX_EXPORTED:
        # Old exports stay complete. They do not pretend that the two new
        # ZeroGPT checks and link/image gates have already been performed.
        migrated["status"] = STATUS_DOCX_EXPORTED
        migrated["legacy_export"] = True

    legacy_report = str(migrated.get("zero_gpt_report") or "")
    if legacy_report:
        initial_check = copy.deepcopy(migrated.get("initial_ai_check") or {})
        initial_check.setdefault("confirmed", False)
        initial_check.setdefault("score", None)
        initial_check["report"] = initial_check.get("report") or legacy_report
        initial_check.setdefault("confirmed_at", "")
        initial_check.setdefault("article_hash", "")
        migrated["initial_ai_check"] = initial_check

    migrated["schema_version"] = SCHEMA_VERSION
    return migrated


def migrate_task_payload(payload: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
    """Migrate supported old schemas and leave future schemas untouched."""

    if not isinstance(payload, Mapping):
        raise TypeError("Every task record must be a JSON object.")

    version = payload.get("schema_version", 1)
    try:
        version_number = int(version)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid task schema_version: {version!r}") from exc

    if version_number <= 1:
        return migrate_v1_to_v2(payload), True
    return copy.deepcopy(dict(payload)), False


class TaskStore:
    """JSON task storage with atomic writes, migration, and revision checks."""

    SOURCE_REFRESH_FIELDS = {
        "week_folder",
        "customer",
        "topic_index",
        "topic",
        "competitor_keyword",
        "competitor_blog",
        "task_dir",
        "updated_at",
    }

    def __init__(self, config: AppConfig):
        self.path = config.data_file
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.migration_backup_path = self.path.with_name(
            f"{self.path.stem}.v1.backup{self.path.suffix}"
        )

    def _write_records(self, tasks: Iterable[TaskRecord]) -> None:
        records = [task.model_dump(mode="json") for task in tasks]
        tmp = self.path.with_name(f"{self.path.name}.tmp")
        tmp.write_text(
            json.dumps(records, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    def _backup_v1(self, original: str) -> None:
        if not self.migration_backup_path.exists():
            self.migration_backup_path.write_text(original, encoding="utf-8")

    def load(self) -> list[TaskRecord]:
        with _TASK_STORE_LOCK:
            if not self.path.exists():
                return []

            original = self.path.read_text(encoding="utf-8")
            raw = json.loads(original)
            if not isinstance(raw, list):
                raise ValueError("Task data file must contain a JSON array.")

            migrated_any = False
            tasks: list[TaskRecord] = []
            for item in raw:
                migrated, changed = migrate_task_payload(item)
                tasks.append(TaskRecord.model_validate(migrated))
                migrated_any = migrated_any or changed

            if migrated_any:
                self._backup_v1(original)
                self._write_records(tasks)
            return tasks

    def save(self, tasks: Iterable[TaskRecord]) -> None:
        with _TASK_STORE_LOCK:
            self._write_records(tasks)

    def get(self, task_id: str) -> TaskRecord:
        for task in self.load():
            if task.id == task_id:
                return task
        raise KeyError(task_id)

    def put(
        self,
        task: TaskRecord,
        *,
        expected_revision: int | None = None,
    ) -> TaskRecord:
        with _TASK_STORE_LOCK:
            tasks = self.load()
            replaced = False
            for index, existing in enumerate(tasks):
                if existing.id != task.id:
                    continue

                expected = task.revision if expected_revision is None else expected_revision
                if expected != existing.revision:
                    raise RevisionConflictError(task.id, expected, existing.revision)

                # Even a caller which reconstructed a known model must not erase
                # top-level extension fields already stored on this task.
                existing_extra = existing.model_extra or {}
                incoming_extra = task.model_extra or {}
                for key, value in existing_extra.items():
                    if key not in incoming_extra:
                        setattr(task, key, copy.deepcopy(value))

                task.revision = existing.revision + 1
                task.schema_version = max(SCHEMA_VERSION, task.schema_version)
                task.updated_at = now_iso()
                tasks[index] = task
                replaced = True
                break

            if not replaced:
                if expected_revision not in (None, 0):
                    raise RevisionConflictError(task.id, expected_revision, 0)
                task.schema_version = max(SCHEMA_VERSION, task.schema_version)
                task.revision = max(0, task.revision)
                task.updated_at = now_iso()
                tasks.append(task)

            self.save(tasks)
            return task

    def upsert_many(self, incoming: Iterable[TaskRecord]) -> list[TaskRecord]:
        with _TASK_STORE_LOCK:
            existing = {task.id: task for task in self.load()}
            for task in incoming:
                if task.id in existing:
                    previous = existing[task.id]
                    merged = previous.model_dump(mode="json")
                    scanned = task.model_dump(mode="json")
                    for field in self.SOURCE_REFRESH_FIELDS:
                        if field in scanned:
                            merged[field] = scanned[field]
                    merged["schema_version"] = max(
                        SCHEMA_VERSION, int(merged.get("schema_version", SCHEMA_VERSION))
                    )
                    existing[task.id] = TaskRecord.model_validate(merged)
                else:
                    task.schema_version = SCHEMA_VERSION
                    existing[task.id] = task
            tasks = list(existing.values())
            self.save(tasks)
            return tasks


def write_text_artifact(task: TaskRecord, filename: str, content: str) -> Path:
    path = Path(task.task_dir) / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def write_json_artifact(task: TaskRecord, filename: str, content: object) -> Path:
    path = Path(task.task_dir) / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
