from __future__ import annotations

import sys
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from workflow_assistant.context import (  # noqa: E402
    AssistantProjectContext,
    AssistantPublishedTopicContext,
    AssistantTaskContext,
    AssistantWorkspaceContext,
)
from workflow_assistant.fixed_workflow import build_fixed_article_plan  # noqa: E402
from workflow_assistant.policy import (  # noqa: E402
    AssistantPolicyError,
    bind_plan_context,
)


def _task(
    task_id: str,
    *,
    status: str = "new",
    selected_title: str | None = None,
    title_candidates: int = 0,
    product_candidates: int = 0,
    confirmed_products: int = 0,
    manual_completed: bool = False,
) -> AssistantTaskContext:
    return AssistantTaskContext(
        task_id=task_id,
        topic=f"Topic {task_id}",
        primary_keyword=f"keyword {task_id}",
        competitor_keyword="",
        status=status,
        revision=1,
        selected_title=selected_title,
        title_candidate_count=title_candidates,
        product_candidate_count=product_candidates,
        confirmed_product_count=confirmed_products,
        manual_completed=manual_completed,
    )


def _context(
    *tasks: AssistantTaskContext,
    published_topics: tuple[AssistantPublishedTopicContext, ...] = (),
) -> AssistantWorkspaceContext:
    return AssistantWorkspaceContext(
        projects=(
            AssistantProjectContext(
                project_id="project-a",
                customer_name="Project A",
                official_domain="project-a.example",
                project_notes="",
                revision=1,
                effective_role="owner",
                tasks=tuple(tasks),
                prompts=(),
                knowledge=(),
                published_topics=published_topics,
            ),
        )
    )


class FixedArticleWorkflowTests(unittest.TestCase):
    def test_no_task_auto_selects_a_published_topic_and_binds_the_article_chain(self) -> None:
        context = _context(
            published_topics=(
                AssistantPublishedTopicContext(
                    topic_id="topic-1",
                    topic="How to choose a heat exchanger",
                    primary_keyword="heat exchanger selection",
                    competitor_keyword="",
                ),
            )
        )

        plan = build_fixed_article_plan(
            "写一篇文章",
            context,
            selected_task_ids=(),
            selection_locked=False,
        )

        self.assertEqual(len(plan.steps), 15)
        self.assertEqual(plan.steps[0].action_kind, "create_task")
        self.assertEqual(
            plan.steps[0].input_summary["published_topic_id"],
            "topic-1",
        )
        self.assertEqual(
            plan.steps[0].input_summary["topic"],
            "How to choose a heat exchanger",
        )
        self.assertEqual(
            plan.steps[0].input_summary["bind_step_ids"],
            [step.step_id for step in plan.steps[1:]],
        )
        self.assertTrue(all(step.article_task_id is None for step in plan.steps[1:]))

        bound = bind_plan_context(plan, context=context)
        self.assertEqual(
            bound.steps[1].input_summary["create_task_step_id"],
            "fixed-1-create-task",
        )
        self.assertTrue(bound.steps[7].input_summary["use_evidence_pack"])

    def test_no_task_auto_selection_requires_enough_published_topics(self) -> None:
        with self.assertRaisesRegex(AssistantPolicyError, "没有足够的可用已发布话题"):
            build_fixed_article_plan(
                "写 2 篇文章",
                _context(
                    published_topics=(
                        AssistantPublishedTopicContext(
                            topic_id="topic-1",
                            topic="Only topic",
                            primary_keyword="only keyword",
                            competitor_keyword="",
                        ),
                    )
                ),
                selected_task_ids=(),
                selection_locked=False,
            )

    def test_builds_complete_chain_without_planner(self) -> None:
        plan = build_fixed_article_plan(
            "面向采购经理写一篇文章",
            _context(_task("task-a")),
            selected_task_ids=("task-a",),
            selection_locked=True,
        )

        self.assertEqual(
            [step.action_kind for step in plan.steps],
            [
                "generate_titles",
                "select_title",
                "generate_products",
                "confirm_products",
                "generate_outline",
                "start_research",
                "generate_article",
                "review",
                "humanize",
                "restore_links",
                "prepare_images",
                "export_docx",
                "generate_tdk",
                "package_delivery",
            ],
        )
        self.assertEqual(len(plan.steps), 14)
        self.assertEqual(
            plan.steps[4].input_summary["writing_instruction"],
            "面向采购经理写一篇文章",
        )
        self.assertTrue(plan.steps[6].input_summary["use_evidence_pack"])

    def test_explicit_skip_review_omits_only_review(self) -> None:
        task = _task(
            "task-a",
            status="outline_confirmed",
            selected_title="Selected title",
            confirmed_products=2,
        )
        plan = build_fixed_article_plan(
            "继续写正文，这篇不用复检",
            _context(task),
            selected_task_ids=("task-a",),
            selection_locked=True,
        )

        self.assertEqual(
            [step.action_kind for step in plan.steps],
            [
                "start_research",
                "generate_article",
                "humanize",
                "restore_links",
                "prepare_images",
                "export_docx",
                "generate_tdk",
                "package_delivery",
            ],
        )

    def test_new_article_with_skip_review_keeps_the_other_thirteen_steps(self) -> None:
        plan = build_fixed_article_plan(
            "写一篇文章，这次跳过复检",
            _context(_task("task-a")),
            selected_task_ids=("task-a",),
            selection_locked=True,
        )

        self.assertEqual(len(plan.steps), 13)
        self.assertNotIn("review", [step.action_kind for step in plan.steps])

    def test_docx_exported_task_can_finish_tdk_and_delivery(self) -> None:
        plan = build_fixed_article_plan(
            "完成交付包",
            _context(
                _task(
                    "task-a",
                    status="docx_exported",
                    selected_title="Selected title",
                    confirmed_products=1,
                    manual_completed=True,
                )
            ),
            selected_task_ids=("task-a",),
            selection_locked=True,
        )

        self.assertEqual(
            [step.action_kind for step in plan.steps],
            ["generate_tdk", "package_delivery"],
        )

    def test_no_selection_defaults_to_one_new_task_per_project(self) -> None:
        plan = build_fixed_article_plan(
            "开始写文章",
            _context(_task("task-a"), _task("task-b")),
        )

        self.assertEqual(
            {step.article_task_id for step in plan.steps},
            {"task-a"},
        )

    def test_completed_or_manual_task_is_not_silently_reused(self) -> None:
        with self.assertRaises(AssistantPolicyError):
            build_fixed_article_plan(
                "写一篇文章",
                _context(
                    _task(
                        "task-a",
                        status="draft_ready",
                        selected_title="Selected title",
                        confirmed_products=1,
                        manual_completed=True,
                    )
                ),
                selected_task_ids=("task-a",),
                selection_locked=True,
            )


if __name__ == "__main__":
    unittest.main()
