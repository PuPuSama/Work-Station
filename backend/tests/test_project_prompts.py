from __future__ import annotations

import sys
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services.project_prompts import (  # noqa: E402
    ProjectPromptRepository,
    PromptInUseError,
)
from models import GenerateOutlineRequest, STATUS_TITLE_SELECTED, TaskRecord  # noqa: E402
from storage import TaskStore  # noqa: E402
import app as app_module  # noqa: E402


class ProjectPromptRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repository = ProjectPromptRepository(
            Path(self.temporary.name) / "tasks.json"
        )

    def test_project_defaults_resolve_latest_prompt_version(self) -> None:
        created = self.repository.create(
            "example.com",
            "Comparison outline",
            "outline",
            "Build a comparison-led outline.",
        )
        defaults = self.repository.set_defaults("example.com", created.id, "")
        self.assertEqual(defaults.default_outline_prompt_id, created.id)

        updated = self.repository.update(
            "example.com",
            created.id,
            "Comparison outline",
            "Build a buyer-first comparison outline.",
        )
        snapshot = self.repository.resolve(
            "example.com", "outline", "project_default"
        )

        self.assertEqual(updated.version, 2)
        self.assertEqual(snapshot.version, 2)
        self.assertEqual(snapshot.source, "project_default")
        self.assertIn("buyer-first", snapshot.content)

    def test_used_prompt_cannot_be_deleted_but_can_be_disabled(self) -> None:
        created = self.repository.create(
            "example.com",
            "Direct article",
            "article",
            "Write direct prose.",
        )
        self.repository.set_defaults("example.com", "", created.id)
        self.repository.mark_used("example.com", created.id)

        with self.assertRaises(PromptInUseError):
            self.repository.delete("example.com", created.id)

        disabled = self.repository.set_active("example.com", created.id, False)
        library = self.repository.list("example.com")
        self.assertFalse(disabled.active)
        self.assertEqual(library.defaults.default_article_prompt_id, "")
        self.assertEqual(
            self.repository.resolve("example.com", "article", created.id).source,
            "system",
        )

    def test_review_prompt_can_be_project_default(self) -> None:
        created = self.repository.create(
            "example.com",
            "SEO quality review",
            "review",
            "Score the article and return a complete revision.",
        )

        defaults = self.repository.set_defaults("example.com", "", "", created.id)
        snapshot = self.repository.resolve("example.com", "review", "project_default")

        self.assertEqual(defaults.default_review_prompt_id, created.id)
        self.assertEqual(snapshot.prompt_id, created.id)
        self.assertEqual(snapshot.kind, "review")

    def test_legacy_prompt_table_is_migrated_to_support_review_kind(self) -> None:
        legacy_path = Path(self.temporary.name) / "legacy.json"
        database_path = legacy_path.with_suffix(".sqlite3")
        connection = sqlite3.connect(database_path)
        try:
            connection.executescript(
                """
                CREATE TABLE project_prompts (
                    id TEXT PRIMARY KEY,
                    customer_key TEXT NOT NULL,
                    customer TEXT NOT NULL,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('outline', 'article')),
                    content TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    use_count INTEGER NOT NULL DEFAULT 0,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE project_prompt_defaults (
                    customer_key TEXT PRIMARY KEY,
                    customer TEXT NOT NULL,
                    default_outline_prompt_id TEXT NOT NULL DEFAULT '',
                    default_article_prompt_id TEXT NOT NULL DEFAULT ''
                );
                INSERT INTO project_prompts(
                    id, customer_key, customer, name, kind, content, created_at, updated_at
                ) VALUES (
                    'old', 'example.com', 'example.com', 'Old article', 'article',
                    'Keep this prompt.', '2026-07-01', '2026-07-01'
                );
                """
            )
            connection.commit()
        finally:
            connection.close()

        migrated = ProjectPromptRepository(legacy_path)
        review = migrated.create(
            "example.com",
            "Review",
            "review",
            "Review this article.",
        )

        self.assertEqual(migrated.get("example.com", "old").content, "Keep this prompt.")
        self.assertEqual(review.kind, "review")
        self.assertEqual(migrated.list("example.com").defaults.default_review_prompt_id, "")

    def test_unused_prompt_can_be_deleted(self) -> None:
        created = self.repository.create(
            "example.com", "Temporary", "outline", "Temporary instructions."
        )
        self.repository.delete("example.com", created.id)
        self.assertEqual(self.repository.list("example.com").prompts, [])

    def test_project_prompt_data_can_be_deleted_with_project(self) -> None:
        created = self.repository.create(
            "example.com", "Project article", "article", "Write direct prose."
        )
        self.repository.set_defaults("example.com", "", created.id)

        self.repository.delete_customer("example.com")

        library = self.repository.list("example.com")
        self.assertEqual(library.prompts, [])
        self.assertEqual(library.defaults.default_article_prompt_id, "")

    def test_outline_generation_captures_selected_project_prompt_snapshot(self) -> None:
        data_path = Path(self.temporary.name) / "workflow-tasks.json"
        config = SimpleNamespace(data_file=data_path)
        task_store = TaskStore(config)
        task = TaskRecord(
            id="prompt-task",
            week_folder="current",
            customer="example.com",
            topic_index=1,
            topic="Fastener selection",
            selected_title="Fastener Selection Guide",
            status=STATUS_TITLE_SELECTED,
            task_dir=str(Path(self.temporary.name) / "topic_001"),
            created_at="2026-07-21T00:00:00",
            updated_at="2026-07-21T00:00:00",
        )
        task_store.put(task)
        repository = ProjectPromptRepository(data_path)
        prompt = repository.create(
            "example.com",
            "Buyer comparison",
            "outline",
            "Compare buyer risks before recommendations.",
        )
        repository.set_defaults("example.com", prompt.id, "")

        with (
            patch.object(app_module, "config", return_value=config),
            patch.object(app_module, "store", return_value=task_store),
            patch.object(app_module, "prompt_store", return_value=repository),
            patch.object(
                app_module,
                "generate_outline",
                return_value="## Buyer Risks\n\n### Risk One\n\n### Risk Two\n\n## FAQ",
            ) as generate,
        ):
            saved = app_module.perform_outline_generation(
                task.id,
                GenerateOutlineRequest(revision=task.revision),
            )

        self.assertEqual(saved.last_outline_prompt_snapshot.prompt_id, prompt.id)
        self.assertEqual(saved.last_outline_prompt_snapshot.version, 1)
        self.assertEqual(
            generate.call_args.kwargs["base_prompt"],
            "Compare buyer risks before recommendations.",
        )
        self.assertEqual(repository.get("example.com", prompt.id).use_count, 1)


if __name__ == "__main__":
    unittest.main()
