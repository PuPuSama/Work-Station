from __future__ import annotations

import sys
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from models import (  # noqa: E402
    STATUS_DOCX_EXPORTED,
    STATUS_DRAFT_READY,
    STATUS_FINAL_AI_CHECKED,
    STATUS_HUMANIZED_READY,
    STATUS_IMAGES_READY,
    STATUS_INITIAL_AI_CHECKED,
    STATUS_LINKS_VERIFIED,
    STATUS_NEW,
    STATUS_OUTLINE_CONFIRMED,
    STATUS_OUTLINE_READY,
    STATUS_TITLE_SELECTED,
    STATUS_TITLES_READY,
    AICheck,
    ArticleImage,
    ArticleVersion,
    LinkValidation,
    Product,
    SourceLink,
    TaskRecord,
    WorkflowError,
)
from workflow.state_machine import (  # noqa: E402
    ACTION_CLEAR_WORKFLOW_ERROR,
    ACTION_CONFIRM_INITIAL_AI,
    ACTION_EXPORT_DOCX,
    ACTION_GENERATE_ARTICLE,
    ACTION_GENERATE_OUTLINE,
    ACTION_GENERATE_TDK,
    ACTION_PACKAGE_DELIVERY,
    ACTION_HUMANIZE_ARTICLE,
    ACTION_PREPARE_IMAGES,
    ACTION_RESTORE_LINKS,
    ACTION_REWRITE_FROM_SCRATCH,
    ACTION_UPDATE_ARTICLE,
    ACTION_UPDATE_HUMANIZED,
    ACTION_UPDATE_IMAGES,
    ACTION_UPDATE_OUTLINE,
    ACTION_UPDATE_PRODUCTS,
    LEGAL_TRANSITIONS,
    InvalidWorkflowTransition,
    WorkflowActionNotAllowed,
    allowed_actions,
    can_transition,
    ensure_action_allowed,
    invalidate_downstream,
    reset_for_full_rewrite,
    transition_task,
)


def task_at(status: str) -> TaskRecord:
    return TaskRecord(
        id="task-1",
        week_folder="7.6-7.10-owner",
        customer="example.com",
        topic_index=1,
        topic="Topic",
        status=status,
        task_dir="D:/article/example/topic_001",
        created_at="2026-07-09T10:00:00",
        updated_at="2026-07-09T10:00:00",
    )


class TransitionTests(unittest.TestCase):
    def test_only_adjacent_forward_transitions_are_legal(self) -> None:
        ordered = [
            STATUS_NEW,
            STATUS_TITLES_READY,
            STATUS_TITLE_SELECTED,
            STATUS_OUTLINE_READY,
            STATUS_OUTLINE_CONFIRMED,
            STATUS_DRAFT_READY,
            STATUS_INITIAL_AI_CHECKED,
            STATUS_HUMANIZED_READY,
            STATUS_FINAL_AI_CHECKED,
            STATUS_LINKS_VERIFIED,
            STATUS_IMAGES_READY,
            STATUS_DOCX_EXPORTED,
        ]
        self.assertEqual(set(LEGAL_TRANSITIONS), set(ordered))
        for current, target in zip(ordered, ordered[1:]):
            self.assertTrue(can_transition(current, target))
            self.assertFalse(can_transition(target, current))

        self.assertFalse(can_transition(STATUS_DRAFT_READY, STATUS_LINKS_VERIFIED))
        with self.assertRaises(InvalidWorkflowTransition):
            transition_task(task_at(STATUS_DRAFT_READY), STATUS_LINKS_VERIFIED)

    def test_transition_clears_previous_workflow_error(self) -> None:
        task = task_at(STATUS_DRAFT_READY)
        task.workflow_error = WorkflowError(code="old_error")
        transition_task(task, STATUS_INITIAL_AI_CHECKED)
        self.assertEqual(task.status, STATUS_INITIAL_AI_CHECKED)
        self.assertIsNone(task.workflow_error)


class AllowedActionsTests(unittest.TestCase):
    def test_external_humanized_article_can_skip_initial_check_and_model_rewrite(self) -> None:
        self.assertIn(ACTION_UPDATE_HUMANIZED, allowed_actions(task_at(STATUS_DRAFT_READY)))
        self.assertIn(
            ACTION_UPDATE_HUMANIZED,
            allowed_actions(task_at(STATUS_INITIAL_AI_CHECKED)),
        )

    def test_humanized_article_can_be_reopened_after_downstream_completion(self) -> None:
        for status in (
            STATUS_FINAL_AI_CHECKED,
            STATUS_LINKS_VERIFIED,
            STATUS_IMAGES_READY,
            STATUS_DOCX_EXPORTED,
        ):
            with self.subTest(status=status):
                self.assertIn(ACTION_UPDATE_HUMANIZED, allowed_actions(task_at(status)))

    def test_tdk_generation_is_the_last_workflow_action(self) -> None:
        self.assertNotIn(ACTION_GENERATE_TDK, allowed_actions(task_at(STATUS_IMAGES_READY)))
        self.assertIn(ACTION_GENERATE_TDK, allowed_actions(task_at(STATUS_DOCX_EXPORTED)))
        self.assertIn(ACTION_PACKAGE_DELIVERY, allowed_actions(task_at(STATUS_DOCX_EXPORTED)))

    def test_manual_first_version_does_not_require_generated_article(self) -> None:
        for status in (
            STATUS_TITLE_SELECTED,
            STATUS_OUTLINE_READY,
            STATUS_OUTLINE_CONFIRMED,
        ):
            with self.subTest(status=status):
                self.assertIn(ACTION_UPDATE_ARTICLE, allowed_actions(task_at(status)))

    def test_article_can_be_regenerated_from_every_downstream_status(self) -> None:
        for status in (
            STATUS_OUTLINE_CONFIRMED,
            STATUS_DRAFT_READY,
            STATUS_INITIAL_AI_CHECKED,
            STATUS_HUMANIZED_READY,
            STATUS_FINAL_AI_CHECKED,
            STATUS_LINKS_VERIFIED,
            STATUS_IMAGES_READY,
            STATUS_DOCX_EXPORTED,
        ):
            with self.subTest(status=status):
                self.assertIn(ACTION_GENERATE_ARTICLE, allowed_actions(task_at(status)))

    def test_first_version_can_be_manually_replaced_from_every_downstream_status(self) -> None:
        for status in (
            STATUS_DRAFT_READY,
            STATUS_INITIAL_AI_CHECKED,
            STATUS_HUMANIZED_READY,
            STATUS_FINAL_AI_CHECKED,
            STATUS_LINKS_VERIFIED,
            STATUS_IMAGES_READY,
            STATUS_DOCX_EXPORTED,
        ):
            with self.subTest(status=status):
                self.assertIn(ACTION_UPDATE_ARTICLE, allowed_actions(task_at(status)))

    def test_outline_can_be_rewritten_from_every_stage_after_title_selection(self) -> None:
        for status in (
            STATUS_TITLE_SELECTED,
            STATUS_OUTLINE_READY,
            STATUS_OUTLINE_CONFIRMED,
            STATUS_DRAFT_READY,
            STATUS_INITIAL_AI_CHECKED,
            STATUS_HUMANIZED_READY,
            STATUS_FINAL_AI_CHECKED,
            STATUS_LINKS_VERIFIED,
            STATUS_IMAGES_READY,
            STATUS_DOCX_EXPORTED,
        ):
            with self.subTest(status=status):
                actions = allowed_actions(task_at(status))
                self.assertIn(ACTION_GENERATE_OUTLINE, actions)
                self.assertIn(ACTION_UPDATE_OUTLINE, actions)

    def test_images_can_be_reopened_after_export(self) -> None:
        actions = allowed_actions(task_at(STATUS_DOCX_EXPORTED))
        self.assertIn(ACTION_UPDATE_IMAGES, actions)
        self.assertIn(ACTION_PREPARE_IMAGES, actions)

    def test_products_can_be_reopened_from_every_downstream_stage(self) -> None:
        for status in (
            STATUS_TITLE_SELECTED,
            STATUS_OUTLINE_READY,
            STATUS_OUTLINE_CONFIRMED,
            STATUS_DRAFT_READY,
            STATUS_INITIAL_AI_CHECKED,
            STATUS_HUMANIZED_READY,
            STATUS_FINAL_AI_CHECKED,
            STATUS_LINKS_VERIFIED,
            STATUS_IMAGES_READY,
            STATUS_DOCX_EXPORTED,
        ):
            with self.subTest(status=status):
                self.assertIn(ACTION_UPDATE_PRODUCTS, allowed_actions(task_at(status)))

    def test_manual_first_version_can_recover_a_failed_generation(self) -> None:
        task = task_at(STATUS_OUTLINE_READY)
        task.workflow_error = WorkflowError(
            code="compression_failed",
            message="Generated article is still over the word limit.",
            stage="article",
            blocking=True,
            recoverable=True,
        )
        self.assertIn(ACTION_UPDATE_ARTICLE, allowed_actions(task))

    def test_export_requires_images_ready(self) -> None:
        self.assertNotIn(ACTION_EXPORT_DOCX, allowed_actions(task_at(STATUS_LINKS_VERIFIED)))
        self.assertIn(ACTION_EXPORT_DOCX, allowed_actions(task_at(STATUS_IMAGES_READY)))
        with self.assertRaises(WorkflowActionNotAllowed):
            ensure_action_allowed(task_at(STATUS_DRAFT_READY), ACTION_EXPORT_DOCX)

    def test_obsolete_compression_error_no_longer_blocks_actions(self) -> None:
        task = task_at(STATUS_DRAFT_READY)
        task.workflow_error = WorkflowError(
            code="compression_failed",
            message="Still above 1600 words",
            blocking=True,
            recoverable=True,
        )
        actions = allowed_actions(task)
        self.assertIn(ACTION_UPDATE_ARTICLE, actions)
        self.assertIn(ACTION_CONFIRM_INITIAL_AI, actions)

    def test_nonrecoverable_blocking_error_still_allows_full_rewrite(self) -> None:
        task = task_at(STATUS_DRAFT_READY)
        task.workflow_error = WorkflowError(
            code="corrupt_source", blocking=True, recoverable=False
        )
        self.assertEqual(allowed_actions(task), [ACTION_REWRITE_FROM_SCRATCH])

    def test_full_rewrite_is_available_from_every_status(self) -> None:
        for status in LEGAL_TRANSITIONS:
            with self.subTest(status=status):
                self.assertIn(
                    ACTION_REWRITE_FROM_SCRATCH,
                    allowed_actions(task_at(status)),
                )

    def test_stage_failures_expose_the_direct_retry_action(self) -> None:
        cases = (
            (STATUS_INITIAL_AI_CHECKED, "humanize_failed", ACTION_HUMANIZE_ARTICLE),
            (STATUS_FINAL_AI_CHECKED, "link_restore_failed", ACTION_RESTORE_LINKS),
            (STATUS_LINKS_VERIFIED, "image_prepare_failed", ACTION_PREPARE_IMAGES),
            (STATUS_IMAGES_READY, "export_failed", ACTION_EXPORT_DOCX),
        )
        for status, code, retry_action in cases:
            with self.subTest(code=code):
                task = task_at(status)
                task.workflow_error = WorkflowError(code=code, blocking=True)
                self.assertIn(retry_action, allowed_actions(task))


class InvalidationTests(unittest.TestCase):
    def populated_task(self) -> TaskRecord:
        task = task_at(STATUS_DOCX_EXPORTED)
        task.raw_draft_article = "raw"
        task.initial_article = "edited initial"
        task.initial_article_hash = "initial-hash"
        task.initial_ai_check = AICheck(
            confirmed=True, report="before", article_hash="initial-hash"
        )
        task.humanized_article = "humanized"
        task.humanized_article_hash = "humanized-hash"
        task.final_ai_check = AICheck(
            confirmed=True,
            report="after",
            screenshot_asset_id="asset-final-ai",
            screenshot_content_hash="f" * 64,
            screenshot_filename="final-ai-rate.png",
            screenshot_width=640,
            screenshot_height=360,
            article_hash="humanized-hash",
        )
        task.source_links = [SourceLink(anchor="site", url="https://example.com")]
        task.linked_article = "linked"
        task.link_validation = LinkValidation(passed=True, preserved_count=1)
        task.images = [
            ArticleImage(
                id="hero",
                role="hero",
                source_path="D:/source.jpg",
                source_asset_id="source-asset",
                prepared_path="D:/title.webp",
                prepared_asset_id="prepared-asset",
                prepared_content_hash="a" * 64,
                width=320,
                height=240,
                filename="title.webp",
                marker="img.title.webp",
                status="ready",
            )
        ]
        task.final_article = "final"
        task.docx_path = "D:/export.docx"
        task.docx_asset_id = "asset-docx"
        task.docx_content_hash = "d" * 64
        task.docx_filename = "Article.docx"
        task.tdk = {
            "title": "Example",
            "description": "Example description",
            "keywords": ["one", "two", "three", "four", "five", "six"],
        }
        task.tdk_path = "D:/D.docx"
        task.tdk_asset_id = "asset-tdk"
        task.tdk_content_hash = "e" * 64
        task.tdk_filename = "D.docx"
        task.delivery_package_path = "D:/article/example.com"
        task.delivery_package_asset_id = "asset-delivery"
        task.delivery_package_content_hash = "c" * 64
        task.delivery_package_filename = "example.com-topic_001.zip"
        task.legacy_export = True
        task.workflow_error = WorkflowError(code="old")
        task.article_versions = [
            ArticleVersion(kind="raw", content="raw"),
            ArticleVersion(kind="initial", content="edited initial"),
            ArticleVersion(kind="humanized", content="humanized"),
            ArticleVersion(kind="linked", content="linked"),
            ArticleVersion(kind="final", content="final"),
        ]
        return task

    def test_initial_article_edit_invalidates_every_dependent_result(self) -> None:
        task = self.populated_task()
        invalidate_downstream(task, "initial_article")

        self.assertEqual(task.status, STATUS_DRAFT_READY)
        self.assertEqual(task.article, "edited initial")
        self.assertEqual(task.initial_article, "edited initial")
        self.assertFalse(task.initial_ai_check.confirmed)
        self.assertEqual(task.source_links, [])
        self.assertEqual(task.humanized_article, "")
        self.assertFalse(task.final_ai_check.confirmed)
        self.assertEqual(
            task.final_ai_check.screenshot_asset_id,
            "",
        )
        self.assertEqual(task.linked_article, "")
        self.assertFalse(task.link_validation.passed)
        self.assertEqual(task.images, [])
        self.assertEqual(task.docx_path, "")
        self.assertEqual(task.docx_asset_id, "")
        self.assertEqual(task.docx_content_hash, "")
        self.assertEqual(task.docx_filename, "")
        self.assertEqual(task.tdk_path, "")
        self.assertEqual(task.tdk_asset_id, "")
        self.assertEqual(task.tdk_content_hash, "")
        self.assertEqual(task.tdk_filename, "")
        self.assertEqual(task.tdk.title, "")
        self.assertEqual(task.delivery_package_path, "")
        self.assertEqual(task.delivery_package_asset_id, "")
        self.assertEqual(task.delivery_package_content_hash, "")
        self.assertEqual(task.delivery_package_filename, "")
        self.assertFalse(task.legacy_export)
        self.assertIsNone(task.workflow_error)
        self.assertEqual(
            [v.kind for v in task.article_versions],
            ["raw", "initial", "humanized", "linked", "final"],
        )

    def test_legacy_article_edit_becomes_the_new_initial_version(self) -> None:
        task = self.populated_task()
        task.article = "legacy endpoint edit"
        invalidate_downstream(task, "article")

        self.assertEqual(task.status, STATUS_DRAFT_READY)
        self.assertEqual(task.initial_article, "legacy endpoint edit")
        self.assertEqual(task.article, "legacy endpoint edit")

    def test_humanized_edit_preserves_initial_check_but_clears_later_work(self) -> None:
        task = self.populated_task()
        invalidate_downstream(task, "humanized_article")

        self.assertEqual(task.status, STATUS_HUMANIZED_READY)
        self.assertTrue(task.initial_ai_check.confirmed)
        self.assertEqual(len(task.source_links), 1)
        self.assertFalse(task.final_ai_check.confirmed)
        self.assertEqual(
            task.final_ai_check.screenshot_asset_id,
            "",
        )
        self.assertEqual(task.linked_article, "")
        self.assertEqual(task.images, [])
        self.assertEqual(task.docx_path, "")
        self.assertEqual(task.docx_asset_id, "")
        self.assertEqual(task.tdk_path, "")
        self.assertEqual(task.tdk_asset_id, "")
        self.assertEqual(task.delivery_package_path, "")
        self.assertEqual(task.delivery_package_asset_id, "")
        self.assertEqual(task.delivery_package_content_hash, "")
        self.assertEqual(task.delivery_package_filename, "")
        self.assertEqual(
            [v.kind for v in task.article_versions],
            ["raw", "initial", "humanized", "linked", "final"],
        )

    def test_confirmed_final_check_returns_to_final_checked_and_clears_links(self) -> None:
        task = self.populated_task()
        task.final_ai_check = AICheck(confirmed=True, article_hash="new-humanized-hash")
        invalidate_downstream(task, "final_ai_check")

        self.assertEqual(task.status, STATUS_FINAL_AI_CHECKED)
        self.assertTrue(task.final_ai_check.confirmed)
        self.assertEqual(task.linked_article, "")
        self.assertFalse(task.link_validation.passed)
        self.assertEqual(task.images, [])

    def test_image_edit_preserves_sources_but_invalidates_prepared_files_and_export(self) -> None:
        task = self.populated_task()
        invalidate_downstream(task, "images")

        self.assertEqual(task.status, STATUS_LINKS_VERIFIED)
        self.assertEqual(len(task.images), 1)
        image = task.images[0]
        self.assertEqual(image.source_path, "D:/source.jpg")
        self.assertEqual(image.source_asset_id, "source-asset")
        self.assertEqual(image.prepared_path, "")
        self.assertEqual(image.prepared_asset_id, "")
        self.assertEqual(image.prepared_content_hash, "")
        self.assertIsNone(image.width)
        self.assertIsNone(image.height)
        self.assertEqual(image.filename, "")
        self.assertEqual(image.marker, "")
        self.assertEqual(image.status, "pending")
        self.assertEqual(task.docx_path, "")
        self.assertEqual(task.docx_asset_id, "")
        self.assertEqual(task.delivery_package_asset_id, "")

    def test_final_article_edit_only_invalidates_export(self) -> None:
        task = self.populated_task()
        task.final_article = "manually edited final"
        invalidate_downstream(task, "final_article")

        self.assertEqual(task.status, STATUS_IMAGES_READY)
        self.assertEqual(task.final_article, "manually edited final")
        self.assertEqual(task.article, "manually edited final")
        self.assertEqual(task.docx_path, "")
        self.assertEqual(task.docx_asset_id, "")
        self.assertEqual(task.docx_content_hash, "")
        self.assertEqual(task.docx_filename, "")
        self.assertEqual(task.delivery_package_asset_id, "")
        self.assertEqual(task.images[0].prepared_path, "D:/title.webp")

    def test_unknown_invalidation_stage_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            invalidate_downstream(self.populated_task(), "made_up_stage")

    def test_full_rewrite_clears_workflow_but_preserves_source_identity(self) -> None:
        task = self.populated_task()
        task.title_candidates = ["Old title"]
        task.selected_title = "Old title"
        task.products = [Product(name="Old product", url="https://example.com/old")]
        task.outline = "Old outline"
        task.article = "Old compatibility article"
        task.hero_image = "D:/old-hero.jpg"
        task.zero_gpt_report = "Old report"
        task.compression = {"required": True}

        reset_for_full_rewrite(task)

        self.assertEqual(task.status, STATUS_NEW)
        self.assertEqual(task.id, "task-1")
        self.assertEqual(task.topic, "Topic")
        self.assertEqual(task.task_dir, "D:/article/example/topic_001")
        self.assertEqual(task.title_candidates, [])
        self.assertEqual(task.selected_title, "")
        self.assertEqual(task.products, [])
        self.assertEqual(task.outline, "")
        self.assertEqual(task.article, "")
        self.assertEqual(task.raw_draft_article, "")
        self.assertEqual(task.initial_article, "")
        self.assertEqual(task.humanized_article, "")
        self.assertEqual(task.linked_article, "")
        self.assertEqual(task.final_article, "")
        self.assertEqual(task.article_versions, [])
        self.assertFalse(task.initial_ai_check.confirmed)
        self.assertFalse(task.final_ai_check.confirmed)
        self.assertEqual(task.source_links, [])
        self.assertFalse(task.link_validation.passed)
        self.assertEqual(task.hero_image, "")
        self.assertEqual(task.images, [])
        self.assertEqual(task.docx_path, "")
        self.assertEqual(task.tdk_path, "")
        self.assertEqual(task.tdk_asset_id, "")
        self.assertEqual(task.delivery_package_path, "")
        self.assertEqual(task.delivery_package_asset_id, "")
        self.assertEqual(task.delivery_package_content_hash, "")
        self.assertEqual(task.delivery_package_filename, "")
        self.assertEqual(task.zero_gpt_report, "")
        self.assertFalse(hasattr(task, "compression"))
        self.assertIsNone(task.workflow_error)


if __name__ == "__main__":
    unittest.main()
