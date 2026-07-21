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

    def test_unused_prompt_can_be_deleted(self) -> None:
        created = self.repository.create(
            "example.com", "Temporary", "outline", "Temporary instructions."
        )
        self.repository.delete("example.com", created.id)
        self.assertEqual(self.repository.list("example.com").prompts, [])

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
