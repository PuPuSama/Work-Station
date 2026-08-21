from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import ValidationError

from models import TaskRecord
from server_project_http import (
    InitialAiCheckUpdateRequest,
    confirm_project_task_initial_ai,
)
from services.access_control import ActorIdentity
from storage import content_hash


ARTICLE = "# Title\n\nOpening paragraph.\n\n## Section\n\n### One\n\nBody.\n\n### Two\n\nBody."


class FakeTaskStore:
    def __init__(self, task: TaskRecord) -> None:
        self.task = task

    def get(self, task_id: str) -> TaskRecord:
        if task_id != self.task.id:
            raise KeyError(task_id)
        return self.task


def _task(task_id: str) -> TaskRecord:
    return TaskRecord(
        id=task_id,
        week_folder="server",
        customer="example.com",
        topic_index=1,
        topic="Example",
        status="draft_ready",
        task_dir="/server/task",
        article=ARTICLE,
        initial_article=ARTICLE,
        initial_article_word_count=12,
        initial_article_hash=content_hash(ARTICLE),
        created_at="2026-08-21T00:00:00+00:00",
        updated_at="2026-08-21T00:00:00+00:00",
    )


class ServerInitialAiSkipTests(unittest.TestCase):
    actor = ActorIdentity("org-a", "reviewer-a")
    authorized = SimpleNamespace(actor=actor, project_id="example.com")
    request = SimpleNamespace()

    def _confirm(self, task: TaskRecord, score: float) -> TaskRecord:
        with (
            patch(
                "server_project_http._require_project_permission",
                return_value=self.authorized,
            ),
            patch(
                "server_project_http._task_store",
                return_value=FakeTaskStore(task),
            ),
            patch(
                "server_project_http._server_app_config",
                return_value=SimpleNamespace(ai_pass_threshold=30),
            ),
            patch(
                "server_project_http._save_audited_task",
                return_value=task,
            ),
        ):
            return confirm_project_task_initial_ai(
                "example.com",
                task.id,
                InitialAiCheckUpdateRequest(revision=0, score=score),
                self.request,
                authorized=self.authorized,
            )

    def test_score_below_threshold_skips_humanization_and_second_check(self) -> None:
        task = self._confirm(_task("passing-task"), 12.5)

        self.assertEqual(task.status, "final_ai_checked")
        self.assertTrue(task.humanization_skipped)
        self.assertEqual(task.humanized_article, ARTICLE)
        self.assertEqual(task.humanized_article_hash, content_hash(ARTICLE))
        self.assertTrue(task.final_ai_check.confirmed)
        self.assertEqual(task.final_ai_check.score, 12.5)
        self.assertEqual(task.final_ai_check.screenshot_asset_id, "")

    def test_score_at_threshold_keeps_humanization_route(self) -> None:
        task = self._confirm(_task("threshold-task"), 30)

        self.assertEqual(task.status, "initial_ai_checked")
        self.assertFalse(task.humanization_skipped)
        self.assertEqual(task.humanized_article, "")
        self.assertFalse(task.final_ai_check.confirmed)

    def test_confirmed_initial_ai_rate_requires_a_score(self) -> None:
        with self.assertRaises(ValidationError):
            InitialAiCheckUpdateRequest(revision=0, confirmed=True)


if __name__ == "__main__":
    unittest.main()
