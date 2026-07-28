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
from models import AICheckUpdateRequest, STATUS_DRAFT_READY, TaskRecord  # noqa: E402
from storage import content_hash  # noqa: E402


class InitialAiPassTests(unittest.TestCase):
    def test_passing_initial_score_skips_humanization_and_recheck(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            article = "# Title\n\nOpening paragraph.\n\n## Section\n\n### One\n\nBody.\n\n### Two\n\nBody.\n\n## FAQ"
            task = TaskRecord(
                id="ai-pass",
                week_folder="current",
                customer="example.com",
                topic_index=1,
                topic="Example",
                status=STATUS_DRAFT_READY,
                task_dir=directory,
                article=article,
                initial_article=article,
                initial_article_word_count=12,
                initial_article_hash=content_hash(article),
                created_at="2026-07-28T00:00:00",
                updated_at="2026-07-28T00:00:00",
            )

            with (
                patch.object(app_module, "get_task_or_404", return_value=task),
                patch.object(app_module, "require_action"),
                patch.object(app_module, "initial_readiness_issues", return_value=[]),
                patch.object(
                    app_module,
                    "config",
                    return_value=SimpleNamespace(ai_pass_threshold=30),
                ),
                patch.object(app_module, "save_task", side_effect=lambda current, revision: current),
            ):
                saved = app_module.confirm_initial_ai(
                    task.id,
                    AICheckUpdateRequest(score=12.5, report="Passed"),
                )

            self.assertEqual(saved.status, "final_ai_checked")
            self.assertTrue(saved.humanization_skipped)
            self.assertEqual(saved.humanized_article, article)
            self.assertTrue(saved.final_ai_check.confirmed)
            self.assertEqual(saved.final_ai_check.score, 12.5)

    def test_score_at_threshold_keeps_normal_humanization_route(self) -> None:
        task = TaskRecord(
            id="ai-threshold",
            week_folder="current",
            customer="example.com",
            topic_index=1,
            topic="Example",
            status=STATUS_DRAFT_READY,
            task_dir=".",
            article="Article",
            initial_article="Article",
            initial_article_hash=content_hash("Article"),
            created_at="2026-07-28T00:00:00",
            updated_at="2026-07-28T00:00:00",
        )
        with (
            patch.object(app_module, "get_task_or_404", return_value=task),
            patch.object(app_module, "require_action"),
            patch.object(app_module, "initial_readiness_issues", return_value=[]),
            patch.object(
                app_module,
                "config",
                return_value=SimpleNamespace(ai_pass_threshold=30),
            ),
            patch.object(app_module, "save_task", side_effect=lambda current, revision: current),
            patch.object(app_module, "write_json_artifact"),
        ):
            saved = app_module.confirm_initial_ai(
                task.id,
                AICheckUpdateRequest(score=30, report="At threshold"),
            )

        self.assertEqual(saved.status, "initial_ai_checked")
        self.assertFalse(saved.humanization_skipped)
        self.assertFalse(saved.final_ai_check.confirmed)


if __name__ == "__main__":
    unittest.main()
