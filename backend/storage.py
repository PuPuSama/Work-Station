from __future__ import annotations

import copy
import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any, Iterable, Mapping

from config import AppConfig
from models import (
    SCHEMA_VERSION,
    STATUS_DOCX_EXPORTED,
    STATUS_DRAFT_READY,
    WORKFLOW_STATUSES,
    AICheck,
    TaskRecord,
)
from services.task_identity import article_source_key, normalized_customer
from services.task_repository import SQLiteTaskRepository, TaskRecordRepository


_TASK_STORE_LOCK = RLock()


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest() if content else ""


def _migrate_seo_review_records(records: object) -> list[dict[str, Any]]:
    if not isinstance(records, list):
        return []
    migrated: list[dict[str, Any]] = []
    for index, raw_record in enumerate(records):
        if not isinstance(raw_record, Mapping):
            continue
        record = copy.deepcopy(dict(raw_record))
        record.setdefault("status", "open")
        record.setdefault("finalized_at", "")
        record.setdefault("finalized_by", "")
        record.setdefault("applied_article_hash", "")
        record.setdefault("applied_revision", None)
        source = str(record.get("source_article") or "").strip()
        if source:
            record["source_article"] = source
            record["source_article_hash"] = content_hash(source)
        if "changes" not in record:
            revised = str(record.get("revised_article") or "").strip()
            changes: list[dict[str, Any]] = []
            if source and revised and source != revised:
                changes.append(
                    {
                        "id": f"legacy-{index + 1:03d}",
                        "operation": "structure",
                        "dimension_key": "legacy",
                        "title": "旧版完整修改稿",
                        "rationale": "由旧版整篇修改稿迁移而来，作为一个整体修改组审核。",
                        "target_text": source,
                        "model_proposed_text": revised,
                        "reviewed_text": revised,
                        "source_start": 0,
                        "source_end": len(source),
                        "hard_problem": False,
                        "applicable": True,
                        "validation_errors": [],
                        "risks": [],
                        "decision": "pending",
                        "decided_at": "",
                        "decided_by": "",
                        "risk_confirmed": False,
                        "risk_confirmed_at": "",
                        "updated_at": "",
                        "raw_payload": None,
                    }
                )
            record["changes"] = changes
        migrated.append(record)
    return migrated


V2_DEFAULTS: dict[str, Any] = {
    "revision": 0,
    "workflow_error": None,
    "brand_name": "",
    "project_introduction": "",
    "project_notes": "",
    "topic_notes": "",
    "outline_custom_prompt": "",
    "outline_draft": "",
    "article_custom_prompt": "",
    "use_outline_custom_prompt": False,
    "use_article_custom_prompt": False,
    # Existing tasks keep the historical system-template behavior. Newly
    # created tasks use TaskRecord's project_default model defaults.
    "outline_prompt_selection": "system",
    "article_prompt_selection": "system",
    "seo_review_prompt_selection": "system",
    "last_outline_prompt_snapshot": None,
    "last_article_prompt_snapshot": None,
    "include_project_introduction": True,
    "include_project_notes": True,
    "include_topic_notes": True,
    "source_key": "",
    "source_kind": "xlsx",
    "synced_from_task_id": "",
    "synced_from_week": "",
    "hero_image": "",
    "raw_draft_article": "",
    "initial_article": "",
    "humanized_article": "",
    "humanization_skipped": False,
    "linked_article": "",
    "final_article": "",
    "article_versions": [],
    "seo_primary_keyword": "",
    "seo_long_tail_keywords": [],
    "seo_reviews": [],
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
    """Return a current-schema copy without dropping unknown fields.

    The historical function name is retained for import compatibility. Its
    defaults now cover every supported legacy schema up to ``SCHEMA_VERSION``.
    """

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

    migrated["seo_reviews"] = _migrate_seo_review_records(
        migrated.get("seo_reviews")
    )
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

    if version_number < SCHEMA_VERSION:
        return migrate_v1_to_v2(payload), True
    return copy.deepcopy(dict(payload)), False


class TaskStore:
    """Task workflow storage with migration and optimistic revision checks."""

    SOURCE_REFRESH_FIELDS = {
        "week_folder",
        "customer",
        "source_key",
        "source_kind",
        "topic_index",
        "topic",
        "competitor_keyword",
        "competitor_blog",
        "task_dir",
    }
    HISTORY_SYNC_IDENTITY_FIELDS = {
        "id",
        "week_folder",
        "customer",
        "brand_name",
        "project_introduction",
        "project_notes",
        "source_key",
        "source_kind",
        "topic_index",
        "topic",
        "competitor_keyword",
        "competitor_blog",
        "task_dir",
        "created_at",
        "updated_at",
        "revision",
        "schema_version",
    }

    def __init__(
        self,
        config: AppConfig,
        *,
        repository: TaskRecordRepository | None = None,
        legacy_import_enabled: bool = True,
    ):
        self.path = config.data_file
        if repository is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        local_repository = (
            SQLiteTaskRepository(self.path) if repository is None else None
        )
        self.repository: TaskRecordRepository = repository or local_repository
        self.database_path = (
            local_repository.database_path
            if local_repository is not None
            else None
        )
        self.legacy_import_enabled = legacy_import_enabled and repository is None
        self.migration_backup_path = self.path.with_name(
            f"{self.path.stem}.v1.backup{self.path.suffix}"
        )
        self.monolith_backup_path = self.path.with_name(
            f"{self.path.stem}.monolith.backup{self.path.suffix}"
        )
        self.weekly_backup_path = self.path.with_name(
            f"{self.path.stem}.weekly.backup{self.path.suffix}"
        )

    def _write_records(self, tasks: Iterable[TaskRecord]) -> None:
        records = [task.model_dump(mode="json") for task in tasks]
        self.repository.replace_all(records)

    def _archive_monolith(self, original: str) -> None:
        if not self.monolith_backup_path.exists():
            self.monolith_backup_path.write_text(original, encoding="utf-8")
        if self.path.exists():
            self.path.unlink()

    def _backup_v1(self, original: str) -> None:
        if not self.migration_backup_path.exists():
            self.migration_backup_path.write_text(original, encoding="utf-8")

    def load(self) -> list[TaskRecord]:
        with _TASK_STORE_LOCK:
            original = ""
            imported_monolith = False
            if self.repository.is_initialized():
                raw = self.repository.load_all()
            elif self.legacy_import_enabled and self.path.exists():
                original = self.path.read_text(encoding="utf-8")
                raw = json.loads(original)
                if not isinstance(raw, list):
                    raise ValueError("Task data file must contain a JSON array.")
                imported_monolith = True
            else:
                return []

            migrated_any = False
            tasks: list[TaskRecord] = []
            for item in raw:
                migrated, changed = migrate_task_payload(item)
                tasks.append(TaskRecord.model_validate(migrated))
                migrated_any = migrated_any or changed

            if migrated_any and original:
                self._backup_v1(original)
            if migrated_any or imported_monolith:
                self._write_records(tasks)
            if imported_monolith:
                # Archive only after the SQLite transaction has committed.
                self._archive_monolith(original)
            return tasks

    def save(self, tasks: Iterable[TaskRecord]) -> None:
        with _TASK_STORE_LOCK:
            original = (
                self.path.read_text(encoding="utf-8")
                if self.legacy_import_enabled and self.path.exists()
                else ""
            )
            self._write_records(tasks)
            if original:
                self._archive_monolith(original)

    def get(self, task_id: str) -> TaskRecord:
        with _TASK_STORE_LOCK:
            # Trigger one-time legacy import before using the direct row lookup.
            if not self.repository.is_initialized():
                self.load()
            payload = self.repository.get(task_id)
            if payload is None:
                raise KeyError(task_id)
            migrated, changed = migrate_task_payload(payload)
            task = TaskRecord.model_validate(migrated)
            if changed:
                self.repository.upsert(task.model_dump(mode="json"))
            return task

    def put(
        self,
        task: TaskRecord,
        *,
        expected_revision: int | None = None,
    ) -> TaskRecord:
        with _TASK_STORE_LOCK:
            original_revision = task.revision
            original_updated_at = task.updated_at
            try:
                existing = self.get(task.id)
            except KeyError:
                existing = None

            if existing is not None:
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
            else:
                if expected_revision not in (None, 0):
                    raise RevisionConflictError(task.id, expected_revision, 0)
                task.schema_version = max(SCHEMA_VERSION, task.schema_version)
                task.revision = max(0, task.revision)
                task.updated_at = now_iso()
            atomic_put = getattr(self.repository, "put_if_revision", None)
            if callable(atomic_put):
                persisted = atomic_put(
                    task.model_dump(mode="json"),
                    expected_revision=(
                        existing.revision if existing is not None else None
                    ),
                )
                if not persisted:
                    task.revision = original_revision
                    task.updated_at = original_updated_at
                    current = self.repository.get(task.id)
                    actual = (
                        int(current.get("revision") or 0)
                        if current is not None
                        else 0
                    )
                    expected = (
                        expected_revision
                        if expected_revision is not None
                        else original_revision
                    )
                    raise RevisionConflictError(task.id, expected, actual)
            else:
                self.repository.upsert(task.model_dump(mode="json"))
            return task

    def update_customer_brand(self, customer: str, brand_name: str) -> int:
        normalized = normalized_customer(customer)
        cleaned_brand = " ".join(str(brand_name or "").split())
        with _TASK_STORE_LOCK:
            tasks = self.load()
            matched = [
                task for task in tasks if normalized_customer(task.customer) == normalized
            ]
            if not matched:
                raise KeyError(customer)

            updated_at = now_iso()
            for task in matched:
                task.brand_name = cleaned_brand
                task.source_key = article_source_key(
                    task.customer,
                    task.topic,
                    task.topic_index,
                )
                task.revision += 1
                task.updated_at = updated_at
            self.repository.upsert_many(
                task.model_dump(mode="json") for task in matched
            )
            return len(matched)

    def update_customer_context(
        self,
        customer: str,
        project_introduction: str,
        project_notes: str,
    ) -> int:
        normalized = normalized_customer(customer)
        cleaned_introduction = str(project_introduction or "").replace("\r\n", "\n").strip()
        cleaned_notes = str(project_notes or "").replace("\r\n", "\n").strip()
        with _TASK_STORE_LOCK:
            tasks = self.load()
            matched = [
                task for task in tasks if normalized_customer(task.customer) == normalized
            ]
            if not matched:
                raise KeyError(customer)

            updated_at = now_iso()
            for task in matched:
                task.project_introduction = cleaned_introduction
                task.project_notes = cleaned_notes
                task.revision += 1
                task.updated_at = updated_at
            self.repository.upsert_many(
                task.model_dump(mode="json") for task in matched
            )
            return len(matched)

    def rename_customer(
        self,
        customer: str,
        new_customer: str,
        *,
        path_replacements: Iterable[tuple[Path, Path]] = (),
    ) -> tuple[list[TaskRecord], dict[str, str]]:
        """Rename one project while preserving workflow history and file references."""

        current_key = normalized_customer(customer)
        new_key = normalized_customer(new_customer)
        replacements = list(path_replacements)
        with _TASK_STORE_LOCK:
            tasks = self.load()
            matched = [
                task
                for task in tasks
                if normalized_customer(task.customer) == current_key
            ]
            if not matched:
                raise KeyError(customer)
            matched_ids = {task.id for task in matched}
            if current_key != new_key and any(
                normalized_customer(task.customer) == new_key
                for task in tasks
                if task.id not in matched_ids
            ):
                raise ValueError(f"Project already exists: {new_customer}")

            updated_at = now_iso()
            id_mapping: dict[str, str] = {}
            renamed: list[TaskRecord] = []
            for task in matched:
                payload: Any = task.model_dump(mode="json")
                for source, destination in replacements:
                    payload = self._remap_history_paths(
                        payload,
                        source,
                        destination,
                    )
                candidate = TaskRecord.model_validate(payload)
                old_id = candidate.id
                candidate.customer = new_customer
                candidate.source_key = (
                    f"manual:{article_source_key(new_customer, candidate.topic, candidate.topic_index)}"
                    if candidate.source_kind == "manual"
                    else article_source_key(
                        new_customer,
                        candidate.topic,
                        candidate.topic_index,
                    )
                )
                if candidate.source_kind != "manual":
                    candidate.id = candidate.source_key[:12]
                id_mapping[old_id] = candidate.id
                candidate.revision += 1
                candidate.updated_at = updated_at
                renamed.append(candidate)

            new_ids = [task.id for task in renamed]
            if len(new_ids) != len(set(new_ids)):
                raise ValueError("The new domain would create duplicate task identifiers.")
            existing_ids = {task.id for task in tasks if task.id not in matched_ids}
            collision = existing_ids.intersection(new_ids)
            if collision:
                raise ValueError(
                    f"The new domain conflicts with existing task: {sorted(collision)[0]}"
                )

            for task in renamed:
                if task.synced_from_task_id in id_mapping:
                    task.synced_from_task_id = id_mapping[task.synced_from_task_id]

            renamed_by_old_id = {
                old.id: new for old, new in zip(matched, renamed, strict=True)
            }
            next_tasks = [
                renamed_by_old_id.get(task.id, task)
                for task in tasks
            ]
            self._write_records(next_tasks)
            return renamed, id_mapping

    def delete_customer(self, customer: str) -> list[TaskRecord]:
        normalized = normalized_customer(customer)
        with _TASK_STORE_LOCK:
            matched = [
                task
                for task in self.load()
                if normalized_customer(task.customer) == normalized
            ]
            if not matched:
                raise KeyError(customer)
            self.repository.delete_many(task.id for task in matched)
            return matched

    def _inherit_history(
        self,
        current: TaskRecord,
        historical: TaskRecord,
    ) -> TaskRecord:
        self._copy_missing_history_files(historical, current)
        inherited = self._remap_history_paths(
            historical.model_dump(mode="json"),
            Path(historical.task_dir),
            Path(current.task_dir),
        )
        current_payload = current.model_dump(mode="json")
        for field in self.HISTORY_SYNC_IDENTITY_FIELDS:
            inherited[field] = copy.deepcopy(current_payload[field])

        inherited["brand_name"] = current.brand_name or historical.brand_name
        inherited["project_introduction"] = (
            current.project_introduction or historical.project_introduction
        )
        inherited["project_notes"] = current.project_notes or historical.project_notes
        inherited["synced_from_task_id"] = historical.id
        inherited["synced_from_week"] = historical.week_folder
        for key, value in (current.model_extra or {}).items():
            inherited[key] = copy.deepcopy(value)
        return TaskRecord.model_validate(inherited)

    @classmethod
    def _remap_history_paths(
        cls,
        value: Any,
        source: Path,
        destination: Path,
    ) -> Any:
        """Point copied nested artifact paths at the canonical task folder."""

        if isinstance(value, dict):
            return {
                key: cls._remap_history_paths(item, source, destination)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [
                cls._remap_history_paths(item, source, destination)
                for item in value
            ]
        if not isinstance(value, str) or not value:
            return value

        normalized_value = value.replace("\\", "/")
        normalized_source = str(source).replace("\\", "/").rstrip("/")
        if normalized_value.casefold() == normalized_source.casefold():
            return str(destination)
        prefix = f"{normalized_source}/"
        if normalized_value.casefold().startswith(prefix.casefold()):
            relative = normalized_value[len(prefix) :]
            return str(destination.joinpath(*relative.split("/")))
        return value

    @staticmethod
    def _copy_missing_history_files(
        historical: TaskRecord,
        current: TaskRecord,
    ) -> None:
        """Copy legacy artifacts into the canonical directory without overwrites."""

        source = Path(historical.task_dir)
        destination = Path(current.task_dir)
        if not source.is_dir():
            return
        try:
            if source.resolve() == destination.resolve():
                return
        except OSError:
            return

        destination.mkdir(parents=True, exist_ok=True)
        for source_path in source.rglob("*"):
            relative = source_path.relative_to(source)
            # The scanner has just written current workbook metadata.  Never
            # replace it with last week's source row.
            if relative == Path("source_row.json"):
                continue
            destination_path = destination / relative
            if source_path.is_dir():
                destination_path.mkdir(parents=True, exist_ok=True)
            elif source_path.is_file() and not destination_path.exists():
                destination_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_path, destination_path)

    def upsert_many(self, incoming: Iterable[TaskRecord]) -> list[TaskRecord]:
        with _TASK_STORE_LOCK:
            incoming = list(incoming)
            if not incoming:
                return self.load()

            loaded = self.load()
            for task in loaded:
                task.source_key = article_source_key(
                    task.customer,
                    task.topic,
                    task.topic_index,
                )

            scope = incoming[0].week_folder
            if self.legacy_import_enabled and any(
                task.week_folder != scope for task in loaded
            ):
                if not self.weekly_backup_path.exists():
                    self.weekly_backup_path.write_text(
                        json.dumps(
                            [task.model_dump(mode="json") for task in loaded],
                            ensure_ascii=False,
                            indent=2,
                        ),
                        encoding="utf-8",
                    )

            existing = {
                task.id: task for task in loaded if task.week_folder == scope
            }
            brand_by_customer: dict[str, str] = {}
            context_by_customer: dict[str, tuple[str, str]] = {}
            for task in sorted(loaded, key=lambda item: item.updated_at):
                if task.brand_name:
                    brand_by_customer[normalized_customer(task.customer)] = task.brand_name
                if task.project_introduction or task.project_notes:
                    context_by_customer[normalized_customer(task.customer)] = (
                        task.project_introduction,
                        task.project_notes,
                    )

            history_by_source: dict[str, list[TaskRecord]] = {}
            status_rank = {
                status: index for index, status in enumerate(WORKFLOW_STATUSES)
            }
            for task in loaded:
                history_by_source.setdefault(task.source_key, []).append(task)
            for historical in history_by_source.values():
                historical.sort(
                    key=lambda item: (
                        status_rank.get(item.status, -1),
                        item.updated_at,
                        item.created_at,
                    ),
                    reverse=True,
                )

            for task in incoming:
                task.source_key = article_source_key(
                    task.customer,
                    task.topic,
                    task.topic_index,
                )
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
                    candidate = TaskRecord.model_validate(merged)
                else:
                    task.schema_version = SCHEMA_VERSION
                    candidate = task

                customer_key = normalized_customer(candidate.customer)
                if not candidate.brand_name and customer_key in brand_by_customer:
                    candidate.brand_name = brand_by_customer[customer_key]
                if customer_key in context_by_customer:
                    introduction, notes = context_by_customer[customer_key]
                    if not candidate.project_introduction:
                        candidate.project_introduction = introduction
                    if not candidate.project_notes:
                        candidate.project_notes = notes

                # A canonical task is created only once.  On that first sync,
                # inherit the furthest legacy workflow for the same customer
                # and topic so completed articles do not restart as new merely
                # because they were produced in a dated folder.
                if task.id not in existing:
                    historical = next(
                        (
                            item
                            for item in history_by_source.get(candidate.source_key, [])
                            if item.id != candidate.id
                        ),
                        None,
                    )
                    if historical is not None:
                        candidate = self._inherit_history(candidate, historical)

                existing[task.id] = candidate
            # The topic library is the current source of truth.  Save exactly
            # one canonical record for every scanned source row; dated copies
            # have already served as migration input and remain available in
            # the one-time backup above.
            incoming_ids = list(dict.fromkeys(task.id for task in incoming))
            tasks = [existing[task_id] for task_id in incoming_ids]
            tasks.extend(
                task
                for task in loaded
                if task.week_folder == scope
                and task.source_kind == "manual"
                and task.id not in incoming_ids
            )
            self.save(tasks)
            return tasks

    def canonical_tasks(self, scope: str) -> list[TaskRecord]:
        """Return persistent project tasks while hiding retained weekly rows."""

        return [task for task in self.load() if task.week_folder == scope]


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
