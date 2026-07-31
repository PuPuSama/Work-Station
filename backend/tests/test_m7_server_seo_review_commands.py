from __future__ import annotations

import sys
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from models import (  # noqa: E402
    ArticleVersion,
    PromptSnapshot,
    SeoReviewChange,
    SeoReviewDimension,
    SeoReviewRun,
    TaskRecord,
)
from services.server_seo_review_commands import (  # noqa: E402
    ServerSeoReviewConflict,
    ServerSeoReviewValidationError,
    apply_server_seo_review,
    build_server_seo_review_preview,
    complete_server_seo_review,
    update_server_seo_review_change,
)
from storage import content_hash  # noqa: E402


ARTICLE = """# Buyer Guide

This introduction explains the buying decision.

## Buyer Checks

### Confirm fit

Keep the application requirements.

### Compare evidence

Keep the supplier evidence.

## FAQ

**Q: What should buyers send?**

A: Send requirements.

**Q: When should buyers request samples?**

A: Before approval.

**Q: Why compare capability?**

A: It affects support.
"""
TARGET = "Keep the supplier evidence."
PROPOSED = "Compare supplier evidence before approval."


def task_with_review() -> TaskRecord:
    article = ARTICLE.strip()
    start = article.index(TARGET)
    review = SeoReviewRun(
        id="review-a",
        source_article=article,
        source_article_hash=content_hash(article),
        source_revision=0,
        score=80,
        dimensions=[
            SeoReviewDimension(
                key="intent",
                name="Intent",
                score=8,
                target_score=9,
            )
        ],
        report="Review report",
        changes=[
            SeoReviewChange(
                id="change-a",
                operation="replace",
                title="Clarify evidence",
                target_text=TARGET,
                model_proposed_text=PROPOSED,
                reviewed_text=PROPOSED,
                source_start=start,
                source_end=start + len(TARGET),
            )
        ],
        prompt_snapshot=PromptSnapshot(
            kind="review",
            source="system",
            content="rubric",
        ),
        created_at="2026-07-31T00:00:00+00:00",
    )
    return TaskRecord(
        id="task-a",
        week_folder="server",
        customer="example.com",
        topic_index=1,
        topic="Buyer Guide",
        selected_title="Buyer Guide",
        status="draft_ready",
        task_dir="/server/task-a",
        initial_article=article,
        initial_article_hash=content_hash(article),
        article=article,
        article_versions=[
            ArticleVersion(kind="initial", content=article)
        ],
        seo_reviews=[review],
        created_at="2026-07-31T00:00:00+00:00",
        updated_at="2026-07-31T00:00:00+00:00",
    )


class ServerSeoReviewCommandTests(unittest.TestCase):
    def test_decision_preview_and_apply_are_separate(self) -> None:
        task = task_with_review()
        decision = update_server_seo_review_change(
            task,
            review_id="review-a",
            change_id="change-a",
            decision="accepted",
            reviewed_text=PROPOSED,
            confirm_risks=False,
            actor_user_id="reviewer-a",
        )
        preview = build_server_seo_review_preview(
            task,
            review_id="review-a",
        )

        self.assertEqual(decision.decision, "accepted")
        self.assertIn(PROPOSED, preview.article)
        self.assertEqual(task.initial_article, ARTICLE.strip())

        summary = apply_server_seo_review(
            task,
            review_id="review-a",
            preview_hash=preview.article_hash,
            confirm_pending=True,
            actor_user_id="editor-a",
        )

        self.assertEqual(summary.accepted_count, 1)
        self.assertEqual(task.seo_reviews[0].status, "applied")
        self.assertEqual(task.seo_reviews[0].applied_revision, 1)
        self.assertIn(PROPOSED, task.initial_article)
        self.assertEqual(task.status, "draft_ready")
        self.assertEqual(
            task.article_versions[-1].source_kind,
            "seo_review:review-a",
        )

    def test_stale_preview_and_finalized_review_fail_closed(self) -> None:
        task = task_with_review()
        update_server_seo_review_change(
            task,
            review_id="review-a",
            change_id="change-a",
            decision="accepted",
            reviewed_text=PROPOSED,
            confirm_risks=False,
            actor_user_id="reviewer-a",
        )
        with self.assertRaisesRegex(
            ServerSeoReviewConflict,
            "preview changed",
        ):
            apply_server_seo_review(
                task,
                review_id="review-a",
                preview_hash="0" * 64,
                confirm_pending=True,
                actor_user_id="editor-a",
            )
        self.assertEqual(task.initial_article, ARTICLE.strip())
        task.seo_reviews[0].status = "completed"
        with self.assertRaisesRegex(
            ServerSeoReviewConflict,
            "finalized",
        ):
            build_server_seo_review_preview(
                task,
                review_id="review-a",
            )

    def test_complete_requires_no_accepted_change_and_pending_ack(self) -> None:
        task = task_with_review()
        with self.assertRaisesRegex(
            ServerSeoReviewConflict,
            "pending",
        ):
            complete_server_seo_review(
                task,
                review_id="review-a",
                confirm_pending=False,
                actor_user_id="reviewer-a",
            )
        summary = complete_server_seo_review(
            task,
            review_id="review-a",
            confirm_pending=True,
            actor_user_id="reviewer-a",
        )
        self.assertEqual(summary.pending_count, 1)
        self.assertEqual(task.seo_reviews[0].status, "completed")
        self.assertEqual(task.initial_article, ARTICLE.strip())

    def test_apply_requires_accepted_change(self) -> None:
        task = task_with_review()
        preview = build_server_seo_review_preview(
            task,
            review_id="review-a",
        )
        with self.assertRaisesRegex(
            ServerSeoReviewValidationError,
            "no accepted",
        ):
            apply_server_seo_review(
                task,
                review_id="review-a",
                preview_hash=preview.article_hash,
                confirm_pending=True,
                actor_user_id="editor-a",
            )


if __name__ == "__main__":
    unittest.main()
