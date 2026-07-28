from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import app as app_module  # noqa: E402
from models import ManualTitleGenerationRequest, TaskRecord  # noqa: E402


class MemoryStore:
    def __init__(self, tasks: list[TaskRecord]):
        self.tasks = tasks

    def load(self) -> list[TaskRecord]:
        return list(self.tasks)

    def canonical_tasks(self, scope: str) -> list[TaskRecord]:
        return [task for task in self.tasks if task.week_folder == scope]

    def put(self, task: TaskRecord, *, expected_revision: int | None = None) -> TaskRecord:
        self.tasks.append(task)
        return task


class ProjectLifecycleTests(unittest.TestCase):
    def test_direct_title_request_creates_manual_task_with_ten_titles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_dir = root / "projects" / "example.com"
            existing = TaskRecord(
                id="existing",
                week_folder="current",
                customer="example.com",
                brand_name="Example",
                project_introduction="Manufacturer.",
                project_notes="Use direct prose.",
                topic_index=1,
                topic="Workbook topic",
                task_dir=str(project_dir / "topic_001"),
                created_at="2026-07-28T00:00:00",
                updated_at="2026-07-28T00:00:00",
            )
            store = MemoryStore([existing])
            config = SimpleNamespace(current_week_folder="current")
            candidates = [f"Title {index}" for index in range(1, 11)]

            with (
                patch.object(app_module, "require_project"),
                patch.object(app_module, "config", return_value=config),
                patch.object(app_module, "store", return_value=store),
                patch.object(app_module, "generate_titles", return_value=candidates),
            ):
                saved = app_module.create_direct_title_task(
                    "example.com",
                    ManualTitleGenerationRequest(
                        topic="Roof ladder sourcing",
                        instruction="Focus on contractors.",
                    ),
                )

            self.assertEqual(saved.source_kind, "manual")
            self.assertEqual(saved.status, "titles_ready")
            self.assertEqual(saved.title_candidates, candidates)
            self.assertEqual(saved.title_generation_instruction, "Focus on contractors.")
            self.assertTrue(Path(saved.task_dir).is_dir())

    def test_project_sources_are_moved_to_recoverable_trash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_root = root / "projects"
            topic_library = root / "topic-library"
            task_dir = output_root / "example.com" / "topic_001"
            task_dir.mkdir(parents=True)
            (task_dir / "article.md").write_text("article", encoding="utf-8")
            topic_library.mkdir()
            workbook = topic_library / "example.com.xlsx"
            workbook.write_bytes(b"xlsx")
            task = TaskRecord(
                id="task",
                week_folder="current",
                customer="example.com",
                topic_index=1,
                topic="Example",
                task_dir=str(task_dir),
                created_at="2026-07-28T00:00:00",
                updated_at="2026-07-28T00:00:00",
            )

            archived = app_module.archive_project_sources(
                SimpleNamespace(output_root=output_root, topic_library=topic_library),
                "example.com",
                [task],
            )

            self.assertFalse((output_root / "example.com").exists())
            self.assertFalse(workbook.exists())
            self.assertEqual(len(archived), 2)
            self.assertTrue(all(Path(path).exists() for path in archived))


if __name__ == "__main__":
    unittest.main()
