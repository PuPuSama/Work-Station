from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import app as app_module  # noqa: E402
from config import load_config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from models import ManualTitleGenerationRequest, TaskRecord  # noqa: E402
from services.job_queue import JobQueue  # noqa: E402
from services.project_prompts import ProjectPromptRepository  # noqa: E402
from services.task_identity import article_source_key  # noqa: E402
from storage import TaskStore  # noqa: E402


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

    def test_project_domain_update_migrates_records_and_owned_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_root = root / "projects"
            topic_library = root / "topic-library"
            knowledge_base = root / "knowledge"
            old_domain = "www.example.com"
            new_domain = "www.example-industrial.com"
            old_task_id = article_source_key(old_domain, "Example topic", 1)[:12]
            task_dir = output_root / old_domain / "topic_001"
            task_dir.mkdir(parents=True)
            (task_dir / "article.md").write_text("article", encoding="utf-8")
            topic_library.mkdir()
            old_workbook = topic_library / f"{old_domain}.xlsx"
            old_workbook.write_bytes(b"xlsx")
            old_knowledge = knowledge_base / old_domain
            old_knowledge.mkdir(parents=True)
            (old_knowledge / "company.txt").write_text("facts", encoding="utf-8")

            cfg = replace(
                load_config(),
                data_file=root / "data" / "tasks.json",
                output_root=output_root,
                topic_library=topic_library,
                knowledge_base=knowledge_base,
            )
            record = TaskRecord(
                id=old_task_id,
                week_folder=cfg.current_week_folder,
                customer=old_domain,
                topic_index=1,
                topic="Example topic",
                source_key=article_source_key(old_domain, "Example topic", 1),
                task_dir=str(task_dir),
                created_at="2026-07-28T00:00:00",
                updated_at="2026-07-28T00:00:00",
            )
            record.source = {"workbook": str(old_workbook)}
            TaskStore(cfg).save([record])
            ProjectPromptRepository(cfg.data_file).create(
                old_domain,
                "Article prompt",
                "article",
                "Write clearly.",
            )
            queue = JobQueue(cfg.data_file.with_name("job_queue.sqlite3"))

            with (
                patch.object(app_module, "config", return_value=cfg),
                patch.object(app_module.app.state, "job_queue", queue, create=True),
            ):
                response = TestClient(app_module.app).put(
                    f"/api/projects/{old_domain}/domain",
                    json={"new_domain": f"https://{new_domain}/"},
                )

            self.assertEqual(response.status_code, 200, response.text)
            new_task_id = article_source_key(new_domain, "Example topic", 1)[:12]
            renamed = TaskStore(cfg).get(new_task_id)
            self.assertEqual(renamed.customer, new_domain)
            self.assertEqual(
                Path(renamed.task_dir),
                output_root / new_domain / "topic_001",
            )
            self.assertEqual(
                Path(renamed.source["workbook"]),
                topic_library / f"{new_domain}.xlsx",
            )
            self.assertFalse((output_root / old_domain).exists())
            self.assertTrue((output_root / new_domain / "topic_001" / "article.md").exists())
            self.assertFalse(old_workbook.exists())
            self.assertTrue((topic_library / f"{new_domain}.xlsx").exists())
            self.assertFalse(old_knowledge.exists())
            self.assertTrue((knowledge_base / new_domain / "company.txt").exists())
            prompts = ProjectPromptRepository(cfg.data_file).list(new_domain)
            self.assertEqual([item.name for item in prompts.prompts], ["Article prompt"])

    def test_project_domain_update_rejects_paths_and_existing_projects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = replace(
                load_config(),
                data_file=root / "data" / "tasks.json",
                output_root=root / "projects",
                topic_library=root / "topics",
                knowledge_base=root / "knowledge",
            )
            records = []
            for domain in ("old.example.com", "taken.example.com"):
                task_dir = cfg.output_root / domain / "topic_001"
                task_dir.mkdir(parents=True)
                records.append(
                    TaskRecord(
                        id=article_source_key(domain, domain, 1)[:12],
                        week_folder=cfg.current_week_folder,
                        customer=domain,
                        topic_index=1,
                        topic=domain,
                        source_key=article_source_key(domain, domain, 1),
                        task_dir=str(task_dir),
                        created_at="2026-07-28T00:00:00",
                        updated_at="2026-07-28T00:00:00",
                    )
                )
            TaskStore(cfg).save(records)
            queue = JobQueue(cfg.data_file.with_name("job_queue.sqlite3"))

            with (
                patch.object(app_module, "config", return_value=cfg),
                patch.object(app_module.app.state, "job_queue", queue, create=True),
            ):
                client = TestClient(app_module.app)
                invalid = client.put(
                    "/api/projects/old.example.com/domain",
                    json={"new_domain": "new.example.com/blog"},
                )
                conflict = client.put(
                    "/api/projects/old.example.com/domain",
                    json={"new_domain": "taken.example.com"},
                )

            self.assertEqual(invalid.status_code, 422)
            self.assertEqual(conflict.status_code, 409)
            self.assertTrue((cfg.output_root / "old.example.com").exists())


if __name__ == "__main__":
    unittest.main()
