from __future__ import annotations

import sys
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from models import PromptSnapshot, TaskRecord  # noqa: E402
from services.server_seo_review_settings import (  # noqa: E402
    ServerSeoReviewSettingsError,
    apply_server_seo_review_settings,
)


def _task() -> TaskRecord:
    return TaskRecord(
        id="topic-1",
        week_folder="server",
        customer="project-a",
        topic_index=1,
        topic="Guide",
        status="new",
        task_dir="/server/topic-1",
        created_at="2026-07-31T00:00:00+00:00",
        updated_at="2026-07-31T00:00:00+00:00",
    )


def _snapshot(kind: str = "review") -> PromptSnapshot:
    return PromptSnapshot(
        prompt_id="system-review",
        name="System review",
        kind=kind,  # type: ignore[arg-type]
        content="Review safely.",
        version=3,
        source="system",
        captured_at="2026-07-31T00:00:00+00:00",
    )


class ServerSeoReviewSettingsTests(unittest.TestCase):
    def test_normalizes_keywords_and_records_resolved_prompt_identity(
        self,
    ) -> None:
        task = _task()

        result = apply_server_seo_review_settings(
            task,
            primary_keyword="  buyer   guide ",
            long_tail_keywords=[
                " fasteners   guide ",
                "FASTENERS GUIDE",
                " sourcing checks ",
            ],
            prompt_selection=" project_default ",
            resolved_prompt=_snapshot(),
        )

        self.assertEqual(result, (2, "system", 3))
        self.assertEqual(task.seo_primary_keyword, "buyer guide")
        self.assertEqual(
            task.seo_long_tail_keywords,
            ["fasteners guide", "sourcing checks"],
        )
        self.assertEqual(
            task.seo_review_prompt_selection,
            "project_default",
        )

    def test_rejects_non_review_prompt_and_oversized_normalized_keyword(
        self,
    ) -> None:
        with self.assertRaisesRegex(
            ServerSeoReviewSettingsError,
            "kind",
        ):
            apply_server_seo_review_settings(
                _task(),
                primary_keyword="",
                long_tail_keywords=[],
                prompt_selection="system",
                resolved_prompt=_snapshot("article"),
            )
        with self.assertRaisesRegex(
            ServerSeoReviewSettingsError,
            "long-tail",
        ):
            apply_server_seo_review_settings(
                _task(),
                primary_keyword="",
                long_tail_keywords=["x" * 241],
                prompt_selection="system",
                resolved_prompt=_snapshot(),
            )


if __name__ == "__main__":
    unittest.main()
