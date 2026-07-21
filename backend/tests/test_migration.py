from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
import unittest.mock
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace


BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from models import (  # noqa: E402
    SCHEMA_VERSION,
    STATUS_DOCX_EXPORTED,
    STATUS_DRAFT_READY,
    STATUS_NEW,
    ArticleImage,
    SourceLink,
    TaskRecord,
)
from services.task_identity import article_source_key  # noqa: E402
from services.topics import make_task_id  # noqa: E402
from storage import (  # noqa: E402
    RevisionConflictError,
    TaskStore,
    migrate_task_payload,
    migrate_v1_to_v2,
)


def v1_task(**overrides: object) -> dict[str, object]:
    task: dict[str, object] = {
        "id": "task-1",
        "week_folder": "7.6-7.10-owner",
        "customer": "example.com",
        "topic_index": 1,
        "topic": "Example topic",
        "competitor_keyword": "",
        "competitor_blog": "",
        "status": STATUS_NEW,
        "task_dir": "D:/article/example/topic_001",
        "title_candidates": [],
        "selected_title": "",
        "outline": "",
        "article": "",
        "products": [],
        "hero_image": "D:/article/example/hero.jpg",
        "docx_path": "",
        "zero_gpt_report": "",
        "created_at": "2026-07-09T10:00:00",
        "updated_at": "2026-07-09T10:00:00",
    }
    task.update(overrides)
    return task


class MigrationUnitTests(unittest.TestCase):
    def test_draft_ready_copies_legacy_article_and_report(self) -> None:
        original = v1_task(
            status=STATUS_DRAFT_READY,
            article="# Title\n\nInitial body.",
            zero_gpt_report="Manual note only",
            future_field={"must": "survive"},
            products=[{"name": "P1", "future_nested": "keep"}],
        )

        migrated = migrate_v1_to_v2(original)
        task = TaskRecord.model_validate(migrated)
        dumped = task.model_dump(mode="json")

        self.assertEqual(task.schema_version, SCHEMA_VERSION)
        self.assertEqual(task.initial_article, original["article"])
        self.assertTrue(task.initial_article_hash)
        self.assertEqual(task.initial_ai_check.report, "Manual note only")
        self.assertFalse(task.initial_ai_check.confirmed)
        self.assertEqual(task.hero_image, "D:/article/example/hero.jpg")
        self.assertEqual(dumped["future_field"], {"must": "survive"})
        self.assertEqual(dumped["products"][0]["future_nested"], "keep")
        self.assertNotIn("schema_version", original)

    def test_legacy_export_remains_terminal_without_faking_new_checks(self) -> None:
        migrated = migrate_v1_to_v2(
            v1_task(
                status=STATUS_DOCX_EXPORTED,
                article="Legacy exported article",
                docx_path="D:/article/export.docx",
            )
        )

        self.assertEqual(migrated["status"], STATUS_DOCX_EXPORTED)
        self.assertTrue(migrated["legacy_export"])
        self.assertEqual(migrated["docx_path"], "D:/article/export.docx")
        self.assertFalse(migrated["initial_ai_check"]["confirmed"])
        self.assertFalse(migrated["final_ai_check"]["confirmed"])

    def test_future_schema_is_not_downgraded(self) -> None:
        future = v1_task(schema_version=7, future_only="value")
        migrated, changed = migrate_task_payload(future)
        self.assertFalse(changed)
        self.assertEqual(migrated["schema_version"], 7)
        self.assertEqual(migrated["future_only"], "value")

    def test_schema_v2_is_upgraded_with_writing_context_defaults(self) -> None:
        migrated, changed = migrate_task_payload(v1_task(schema_version=2))

        self.assertTrue(changed)
        self.assertEqual(migrated["schema_version"], SCHEMA_VERSION)
        self.assertEqual(migrated["project_introduction"], "")
        self.assertEqual(migrated["project_notes"], "")
        self.assertEqual(migrated["topic_notes"], "")
        self.assertFalse(migrated["use_outline_custom_prompt"])
        self.assertTrue(migrated["include_project_introduction"])
        self.assertEqual(migrated["outline_prompt_selection"], "system")
        self.assertEqual(migrated["article_prompt_selection"], "system")

    def test_nested_models_are_validated_on_assignment(self) -> None:
        task = TaskRecord.model_validate(v1_task())
        task.source_links = [{"anchor": "site", "url": "https://example.com"}]
        task.images = [{"role": "hero", "source_path": "D:/hero.jpg"}]

        self.assertIsInstance(task.source_links[0], SourceLink)
        self.assertIsInstance(task.images[0], ArticleImage)


class TaskStoreMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_path = Path(self.temp_dir.name) / "tasks.json"
        self.config = SimpleNamespace(data_file=self.data_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_load_backs_up_and_persists_v2_without_unknown_field_loss(self) -> None:
        original_records = [
            v1_task(
                status=STATUS_DRAFT_READY,
                article="Initial article",
                unknown_extension={"nested": [1, 2, 3]},
            )
        ]
        original_json = json.dumps(original_records, ensure_ascii=False, indent=2)
        self.data_path.write_text(original_json, encoding="utf-8")

        store = TaskStore(self.config)
        loaded = store.load()

        self.assertEqual(len(loaded), 1)
        self.assertTrue(store.migration_backup_path.exists())
        self.assertEqual(
            store.migration_backup_path.read_text(encoding="utf-8"), original_json
        )

        self.assertFalse(self.data_path.exists())
        self.assertTrue(store.database_path.exists())
        self.assertTrue(store.monolith_backup_path.exists())
        persisted = store.repository.load_all()
        self.assertEqual(persisted[0]["schema_version"], SCHEMA_VERSION)
        self.assertEqual(persisted[0]["initial_article"], "Initial article")
        self.assertEqual(
            persisted[0]["unknown_extension"], {"nested": [1, 2, 3]}
        )
        self.assertEqual(
            store.monolith_backup_path.read_text(encoding="utf-8"), original_json
        )

    def test_put_preserves_extensions_and_rejects_stale_revision(self) -> None:
        self.data_path.write_text(
            json.dumps([v1_task(extra_owner="future-client")]),
            encoding="utf-8",
        )
        store = TaskStore(self.config)
        first = store.get("task-1")
        stale = store.get("task-1")

        first.topic = "Updated once"
        saved = store.put(first)
        self.assertEqual(saved.revision, 1)
        self.assertEqual(saved.model_dump()["extra_owner"], "future-client")

        stale.topic = "Stale update"
        with self.assertRaises(RevisionConflictError):
            store.put(stale)

        current = store.get("task-1")
        self.assertEqual(current.topic, "Updated once")
        self.assertEqual(current.model_dump()["extra_owner"], "future-client")

    def test_legacy_import_is_idempotent_and_uses_sqlite_after_archiving(self) -> None:
        original_json = json.dumps(
            [v1_task(id="task-1", topic="Imported once")],
            ensure_ascii=False,
        )
        self.data_path.write_text(original_json, encoding="utf-8")

        first_store = TaskStore(self.config)
        first = first_store.load()
        self.assertEqual([task.topic for task in first], ["Imported once"])
        self.assertFalse(self.data_path.exists())

        # A second instance reads the row store, not the archived monolith.
        second_store = TaskStore(self.config)
        second = second_store.load()
        self.assertEqual([task.id for task in second], ["task-1"])
        self.assertEqual(
            second_store.monolith_backup_path.read_text(encoding="utf-8"),
            original_json,
        )

    def test_single_task_put_does_not_replace_unrelated_rows(self) -> None:
        store = TaskStore(self.config)
        store.save(
            [
                TaskRecord.model_validate(v1_task(id="task-a", topic="A")),
                TaskRecord.model_validate(v1_task(id="task-b", topic="B")),
            ]
        )
        original_replace_all = store.repository.replace_all

        first = store.get("task-a")
        first.topic = "A changed"
        with unittest.mock.patch.object(
            store.repository,
            "replace_all",
            wraps=original_replace_all,
        ) as replace_all:
            store.put(first)

        replace_all.assert_not_called()
        self.assertEqual(store.get("task-a").topic, "A changed")
        self.assertEqual(store.get("task-b").topic, "B")

    def test_concurrent_updates_to_different_tasks_do_not_lose_data(self) -> None:
        store = TaskStore(self.config)
        store.save(
            [
                TaskRecord.model_validate(v1_task(id="task-a", topic="A")),
                TaskRecord.model_validate(v1_task(id="task-b", topic="B")),
            ]
        )
        original_write = store._write_records

        def slow_write(tasks):
            time.sleep(0.02)
            return original_write(tasks)

        def update(task_id: str, topic: str) -> None:
            task = store.get(task_id)
            task.topic = topic
            store.put(task)

        with unittest.mock.patch.object(store, "_write_records", side_effect=slow_write):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(update, "task-a", "A updated"),
                    pool.submit(update, "task-b", "B updated"),
                ]
                for future in futures:
                    future.result(timeout=5)

        current = {task.id: task.topic for task in store.load()}
        self.assertEqual(current, {"task-a": "A updated", "task-b": "B updated"})

    def test_upsert_refreshes_source_but_preserves_workflow_and_extensions(self) -> None:
        self.data_path.write_text(
            json.dumps(
                [
                    v1_task(
                        status=STATUS_DRAFT_READY,
                        article="Keep this article",
                        selected_title="Keep this title",
                        extension_flag=True,
                    )
                ]
            ),
            encoding="utf-8",
        )
        store = TaskStore(self.config)
        incoming = TaskRecord.model_validate(
            v1_task(topic="Fresh spreadsheet topic", updated_at="2026-07-10T12:00:00")
        )

        merged = store.upsert_many([incoming])[0]

        self.assertEqual(merged.topic, "Fresh spreadsheet topic")
        self.assertEqual(merged.status, STATUS_DRAFT_READY)
        self.assertEqual(merged.article, "Keep this article")
        self.assertEqual(merged.selected_title, "Keep this title")
        self.assertTrue(merged.model_dump()["extension_flag"])

    def test_weekly_duplicates_collapse_into_canonical_tasks_and_copy_files(self) -> None:
        old_root = Path(self.temp_dir.name) / "7.6-7.10-owner"
        old_task_one = old_root / "example.com" / "topic_001"
        old_task_two = old_root / "example.com" / "topic_002"
        old_task_one.mkdir(parents=True)
        old_task_two.mkdir(parents=True)
        (old_task_one / "export.docx").write_bytes(b"legacy export")
        (old_task_one / "source_row.json").write_text(
            "old source", encoding="utf-8"
        )

        records = [
            v1_task(
                id="old-one-new",
                week_folder="7.6-7.10-owner",
                topic_index=1,
                topic="Repeated topic",
                status=STATUS_NEW,
                task_dir=str(old_task_one),
                updated_at="2026-07-10T10:00:00",
            ),
            v1_task(
                id="old-one-complete",
                week_folder="7.13-7.17-owner",
                topic_index=1,
                topic="Repeated topic",
                status=STATUS_DOCX_EXPORTED,
                article="Finished article",
                docx_path=str(old_task_one / "export.docx"),
                task_dir=str(old_task_one),
                updated_at="2026-07-17T10:00:00",
            ),
            v1_task(
                id="old-two",
                week_folder="7.13-7.17-owner",
                topic_index=2,
                topic="Repeated topic",
                status=STATUS_DRAFT_READY,
                article="Different row draft",
                task_dir=str(old_task_two),
                updated_at="2026-07-17T11:00:00",
            ),
        ]
        self.data_path.write_text(json.dumps(records), encoding="utf-8")

        canonical_root = Path(self.temp_dir.name) / "example.com"
        canonical_one = canonical_root / "topic_001"
        canonical_two = canonical_root / "topic_002"
        canonical_one.mkdir(parents=True)
        (canonical_one / "source_row.json").write_text(
            "current source", encoding="utf-8"
        )
        incoming = [
            TaskRecord.model_validate(
                v1_task(
                    id=make_task_id("example.com", 1, "Repeated topic"),
                    week_folder="全部项目",
                    topic_index=1,
                    topic="Repeated topic",
                    task_dir=str(canonical_one),
                )
            ),
            TaskRecord.model_validate(
                v1_task(
                    id=make_task_id("example.com", 2, "Repeated topic"),
                    week_folder="全部项目",
                    topic_index=2,
                    topic="Repeated topic",
                    task_dir=str(canonical_two),
                )
            ),
        ]

        store = TaskStore(self.config)
        synced = store.upsert_many(incoming)

        self.assertEqual(len(synced), 2)
        self.assertEqual(len(store.load()), 2)
        self.assertTrue(store.weekly_backup_path.is_file())
        by_index = {task.topic_index: task for task in synced}
        self.assertEqual(by_index[1].status, STATUS_DOCX_EXPORTED)
        self.assertEqual(by_index[1].article, "Finished article")
        self.assertEqual(by_index[1].synced_from_task_id, "old-one-complete")
        self.assertEqual(by_index[1].docx_path, str(canonical_one / "export.docx"))
        self.assertEqual(by_index[2].status, STATUS_DRAFT_READY)
        self.assertEqual(by_index[2].article, "Different row draft")
        self.assertNotEqual(by_index[1].source_key, by_index[2].source_key)
        self.assertEqual(
            (canonical_one / "export.docx").read_bytes(), b"legacy export"
        )
        self.assertEqual(
            (canonical_one / "source_row.json").read_text(encoding="utf-8"),
            "current source",
        )

    def test_source_identity_keeps_duplicate_topic_rows_distinct(self) -> None:
        first = article_source_key("www.example.com", "Same topic", 1)
        second = article_source_key("example.com", "Same topic", 2)

        self.assertNotEqual(first, second)
        self.assertEqual(
            make_task_id("example.com", 1, "Same topic"), first[:12]
        )


class RealTaskDataTests(unittest.TestCase):
    def test_current_records_validate_without_losing_existing_keys(self) -> None:
        data_path = PROJECT_DIR / "data" / "tasks.json"
        backup_path = PROJECT_DIR / "data" / "tasks.monolith.backup.json"
        sqlite_path = data_path.with_suffix(".sqlite3")
        source_path = data_path if data_path.exists() else backup_path
        if not source_path.exists() or (not data_path.exists() and not sqlite_path.exists()):
            self.skipTest("Ignored local task data is not present in this checkout.")

        raw = json.loads(source_path.read_text(encoding="utf-8"))
        current = {
            task.id: task.model_dump(mode="json")
            for task in TaskStore(SimpleNamespace(data_file=data_path)).load()
        }
        # The local task store grows whenever another weekly topic library is
        # scanned. Keep the original baseline while allowing later weeks.
        self.assertGreaterEqual(len(raw), 774)
        self.assertGreaterEqual(len(current), 774)

        for original in raw:
            dumped = current[original["id"]]
            for key in original:
                self.assertIn(key, dumped)
            self.assertIn("hero_image", dumped)


if __name__ == "__main__":
    unittest.main()
