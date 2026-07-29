from __future__ import annotations

import tempfile
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import app as app_module  # noqa: E402
from models import (  # noqa: E402
    ArticleVersion,
    PromptSnapshot,
    SeoReviewChange,
    SeoReviewChangeUpdateRequest,
    SeoReviewDimension,
    SeoReviewFinalizeRequest,
    SeoReviewPreviewRequest,
    SeoReviewRequest,
    SeoReviewSettingsUpdateRequest,
    STATUS_DRAFT_READY,
    TaskRecord,
)
from services.project_prompts import ProjectPromptRepository  # noqa: E402
from services.seo_review import GeneratedSeoReview  # noqa: E402
from storage import TaskStore, content_hash  # noqa: E402


ARTICLE = """# Industrial Roof Ladder Selection

Choosing the right configuration starts with the work area and access conditions.

## Evaluate the Application

### Confirm the Work Area

Review the surface and access point before requesting a quote.

### Match the Operating Need

The configuration should reflect transport and routine inspection needs.

## FAQ

**Q: What should a buyer confirm first?**
A: Confirm the intended work area and access conditions.

**Q: When should specifications be reviewed?**
A: Review them before requesting a formal quotation.

**Q: Why does supplier evidence matter?**
A: It helps verify that stated capabilities match the application.
"""

TARGET = "The configuration should reflect transport and routine inspection needs."
PROPOSED = (
    "Buyers should match the configuration to transport and routine inspection "
    "needs before requesting a quotation."
)


class SeoReviewApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.config = SimpleNamespace(data_file=root / "tasks.json", week_owner="Operator")
        self.store = TaskStore(self.config)
        self.prompts = ProjectPromptRepository(self.config.data_file)
        self.task = TaskRecord(
            id="seo-review-api",
            week_folder="current",
            customer="example.com",
            topic_index=1,
            topic="Roof ladder selection",
            selected_title="Industrial Roof Ladder Selection",
            status=STATUS_DRAFT_READY,
            task_dir=str(root / "topic_001"),
            initial_article=ARTICLE,
            initial_article_hash=content_hash(ARTICLE),
            initial_article_word_count=90,
            article=ARTICLE,
            article_versions=[ArticleVersion(kind="initial", content=ARTICLE)],
            created_at="2026-07-29T00:00:00",
            updated_at="2026-07-29T00:00:00",
        )
        self.store.put(self.task)

    def generated(self, snapshot: PromptSnapshot) -> GeneratedSeoReview:
        return GeneratedSeoReview(
            score=82.0,
            dimensions=[
                SeoReviewDimension(
                    key="search_intent",
                    name="Search intent",
                    score=7.2,
                    target_score=8,
                    main_issue="Add a clearer buyer action.",
                    needs_revision=True,
                )
            ],
            publish_ready=False,
            publish_recommendation="Revise before publishing.",
            report="# Complete report\n\nFull findings.",
            changes=[
                SeoReviewChange(
                    id="change-001",
                    operation="replace",
                    dimension_key="search_intent",
                    title="Clarify buyer action",
                    rationale="Close the search intent loop.",
                    target_text=TARGET,
                    model_proposed_text=PROPOSED,
                    reviewed_text=PROPOSED,
                    source_start=ARTICLE.strip().find(TARGET),
                    source_end=ARTICLE.strip().find(TARGET) + len(TARGET),
                )
            ],
            prompt_snapshot=snapshot,
        )

    def test_review_decisions_preview_and_application_are_versioned(self) -> None:
        prompt = self.prompts.create(
            "example.com",
            "Project SEO review",
            "review",
            "Review strictly.",
        )
        with (
            patch.object(app_module, "config", return_value=self.config),
            patch.object(app_module, "store", return_value=self.store),
            patch.object(app_module, "prompt_store", return_value=self.prompts),
        ):
            settings = app_module.update_seo_review_settings(
                self.task.id,
                SeoReviewSettingsUpdateRequest(
                    revision=0,
                    primary_keyword="roof ladders",
                    long_tail_keywords=["roof ladder selection", "roof ladder selection"],
                    prompt_selection=prompt.id,
                ),
            )
            snapshot = self.prompts.resolve("example.com", "review", prompt.id)
            with patch.object(
                app_module,
                "generate_seo_review",
                return_value=self.generated(snapshot),
            ):
                reviewed = app_module.perform_seo_review(
                    self.task.id,
                    SeoReviewRequest(
                        revision=settings.revision,
                        primary_keyword=settings.seo_primary_keyword,
                        long_tail_keywords=settings.seo_long_tail_keywords,
                        prompt_selection=prompt.id,
                        prompt_snapshot=snapshot,
                    ),
                )

            review = reviewed.seo_reviews[0]
            decided = app_module.update_seo_review_change(
                self.task.id,
                review.id,
                review.changes[0].id,
                SeoReviewChangeUpdateRequest(
                    revision=reviewed.revision,
                    decision="accepted",
                    reviewed_text=PROPOSED,
                ),
            )
            preview = app_module.preview_seo_review_changes(
                self.task.id,
                review.id,
                SeoReviewPreviewRequest(revision=decided.revision),
            )
            applied = app_module.apply_seo_review_changes(
                self.task.id,
                review.id,
                SeoReviewFinalizeRequest(
                    revision=decided.revision,
                    preview_hash=preview.article_hash,
                    confirm_pending=True,
                ),
            )

        self.assertEqual(reviewed.status, STATUS_DRAFT_READY)
        self.assertEqual(reviewed.seo_long_tail_keywords, ["roof ladder selection"])
        self.assertEqual(reviewed.seo_reviews[0].report, "# Complete report\n\nFull findings.")
        self.assertIn(PROPOSED, preview.article)
        self.assertEqual(applied.seo_reviews[0].status, "applied")
        self.assertEqual(applied.seo_reviews[0].changes[0].decision, "accepted")
        self.assertIn(PROPOSED, applied.initial_article)
        self.assertEqual(len(applied.seo_reviews), 1)
        self.assertTrue((Path(applied.task_dir) / "seo-reviews").is_dir())

    def test_complete_without_changes_locks_review_and_keeps_article(self) -> None:
        snapshot = PromptSnapshot(kind="review", source="system")
        with (
            patch.object(app_module, "config", return_value=self.config),
            patch.object(app_module, "store", return_value=self.store),
            patch.object(app_module, "prompt_store", return_value=self.prompts),
            patch.object(
                app_module,
                "generate_seo_review",
                return_value=self.generated(snapshot),
            ),
        ):
            reviewed = app_module.perform_seo_review(
                self.task.id,
                SeoReviewRequest(revision=0, prompt_selection="system"),
            )
            completed = app_module.complete_seo_review_without_changes(
                self.task.id,
                reviewed.seo_reviews[0].id,
                SeoReviewFinalizeRequest(
                    revision=reviewed.revision,
                    confirm_pending=True,
                ),
            )

        self.assertEqual(completed.initial_article, ARTICLE)
        self.assertEqual(completed.seo_reviews[0].status, "completed")
        self.assertEqual(completed.seo_reviews[0].changes[0].decision, "pending")

    def test_preview_hash_and_source_hash_prevent_stale_application(self) -> None:
        snapshot = PromptSnapshot(kind="review", source="system")
        with (
            patch.object(app_module, "config", return_value=self.config),
            patch.object(app_module, "store", return_value=self.store),
            patch.object(app_module, "prompt_store", return_value=self.prompts),
            patch.object(
                app_module,
                "generate_seo_review",
                return_value=self.generated(snapshot),
            ),
        ):
            reviewed = app_module.perform_seo_review(
                self.task.id,
                SeoReviewRequest(revision=0, prompt_selection="system"),
            )
            review = reviewed.seo_reviews[0]
            decided = app_module.update_seo_review_change(
                self.task.id,
                review.id,
                review.changes[0].id,
                SeoReviewChangeUpdateRequest(
                    revision=reviewed.revision,
                    decision="accepted",
                    reviewed_text=PROPOSED,
                ),
            )
            with self.assertRaises(HTTPException) as invalid_preview:
                app_module.apply_seo_review_changes(
                    self.task.id,
                    review.id,
                    SeoReviewFinalizeRequest(
                        revision=decided.revision,
                        preview_hash="not-the-preview-hash",
                        confirm_pending=True,
                    ),
                )
            self.assertEqual(invalid_preview.exception.status_code, 409)

            changed = self.store.get(self.task.id)
            changed.initial_article = changed.initial_article.replace(
                "Choosing the right configuration",
                "A buyer choosing the right configuration",
            )
            changed.initial_article_hash = content_hash(changed.initial_article)
            self.store.put(changed)
            with self.assertRaises(HTTPException) as stale:
                app_module.preview_seo_review_changes(
                    self.task.id,
                    review.id,
                    SeoReviewPreviewRequest(),
                )
            self.assertEqual(stale.exception.status_code, 409)
            self.assertIn("旧 Diff", stale.exception.detail)

    def test_single_review_queue_snapshot_captures_default_prompt_content(self) -> None:
        task = self.store.get(self.task.id)
        task.seo_review_prompt_selection = "system"

        with (
            patch.object(app_module, "store", return_value=self.store),
            patch.object(app_module, "prompt_store", return_value=self.prompts),
        ):
            request = app_module.batch_request_snapshot(task, "seo_review")

        self.assertEqual(request["prompt_snapshot"]["kind"], "review")
        self.assertEqual(request["prompt_snapshot"]["name"], "系统默认 SEO 质量复检")
        self.assertIn("E-E-A-T", request["prompt_snapshot"]["content"])
        self.assertEqual(app_module.batch_preflight_issue(task, "seo_review"), "")


if __name__ == "__main__":
    unittest.main()
