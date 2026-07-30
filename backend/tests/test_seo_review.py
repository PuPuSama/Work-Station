from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from models import PromptSnapshot, SeoReviewRun, TaskRecord  # noqa: E402
from services.seo_review import (  # noqa: E402
    SeoReviewError,
    build_review_candidate,
    build_seo_review_prompt,
    parse_seo_review_response,
    update_review_change,
)


ARTICLE = """# Industrial Roof Ladder Selection

Choosing a roof ladder requires a clear review of access conditions and buyer responsibilities.

## Evaluate the Application

### Confirm the Work Area

Review the surface, access point, and expected handling conditions before requesting a quote.

### Match the Operating Need

The selected configuration should reflect storage, transport, and routine inspection needs.

## FAQ

**Q: What should a buyer confirm first?**
A: Confirm the intended work area and access conditions.

**Q: When should specifications be reviewed?**
A: Review them before requesting a formal quotation.

**Q: Why does supplier evidence matter?**
A: It helps the buyer verify that stated capabilities match the application.
"""

TARGET = (
    "The selected configuration should reflect storage, transport, and routine "
    "inspection needs."
)
PROPOSED = (
    "Buyers should match the selected configuration to storage, transport, and "
    "routine inspection needs before requesting a quotation."
)


def task() -> TaskRecord:
    return TaskRecord(
        id="seo-review",
        week_folder="current",
        customer="example.com",
        brand_name="Example",
        topic_index=1,
        topic="Roof Ladders - What Buyers Need to Know",
        selected_title="Industrial Roof Ladder Selection",
        task_dir="unused",
        created_at="2026-07-29T00:00:00",
        updated_at="2026-07-29T00:00:00",
    )


def snapshot(content: str = "Review the article strictly.") -> PromptSnapshot:
    return PromptSnapshot(
        prompt_id="prompt",
        name="SEO review",
        kind="review",
        content=content,
        version=2,
        source="library",
        captured_at="2026-07-29T00:00:00",
    )


def response(
    *,
    target_text: str = TARGET,
    proposed_text: str = PROPOSED,
    hard_problem: bool = False,
) -> str:
    return json.dumps(
        {
            "publish_ready": False,
            "publish_recommendation": "Revise the weak dimensions before publishing.",
            "dimensions": [
                {
                    "key": "eeat",
                    "name": "E-E-A-T",
                    "score": 9,
                    "target_score": 9,
                    "main_issue": "Evidence is adequate.",
                    "needs_revision": False,
                },
                {
                    "key": "search_intent",
                    "name": "Search intent",
                    "score": 7,
                    "target_score": 8,
                    "main_issue": "The conclusion needs a clearer buyer action.",
                    "needs_revision": True,
                },
                {
                    "key": "information_gain",
                    "name": "Information gain",
                    "score": 8,
                    "target_score": 8,
                    "main_issue": "Buyer guidance is useful.",
                    "needs_revision": False,
                },
                {
                    "key": "structure",
                    "name": "Structure",
                    "score": 8,
                    "target_score": 8,
                    "main_issue": "The hierarchy is coherent.",
                    "needs_revision": False,
                },
                {
                    "key": "keyword_quality",
                    "name": "Keyword quality",
                    "score": 7,
                    "target_score": 7,
                    "main_issue": "No forced phrase is present.",
                    "needs_revision": False,
                },
            ],
            "report": "# SEO Review\n\nComplete report content.",
            "changes": [
                {
                    "operation": "replace",
                    "dimension_key": "search_intent",
                    "title": "Clarify the buyer action",
                    "rationale": "Close the search intent loop.",
                    "target_text": target_text,
                    "proposed_text": proposed_text,
                    "hard_problem": hard_problem,
                }
            ],
        },
        ensure_ascii=False,
    )


class SeoReviewServiceTests(unittest.TestCase):
    def test_parses_report_and_independently_applicable_change(self) -> None:
        result = parse_seo_review_response(
            response(),
            source_article=ARTICLE.strip(),
            prompt_snapshot=snapshot(),
        )

        self.assertEqual(result.score, 78.0)
        self.assertEqual(len(result.dimensions), 5)
        self.assertIn("Complete report", result.report)
        self.assertEqual(len(result.changes), 1)
        self.assertTrue(result.changes[0].applicable)
        self.assertEqual(result.changes[0].decision, "pending")

    def test_string_false_is_not_treated_as_true(self) -> None:
        payload = json.loads(response())
        payload["publish_ready"] = "false"

        result = parse_seo_review_response(
            json.dumps(payload),
            source_article=ARTICLE.strip(),
            prompt_snapshot=snapshot(),
        )

        self.assertFalse(result.publish_ready)

    def test_unlocatable_change_is_retained_without_losing_report(self) -> None:
        result = parse_seo_review_response(
            response(target_text="This paragraph does not exist."),
            source_article=ARTICLE.strip(),
            prompt_snapshot=snapshot(),
        )

        self.assertEqual(result.report, "# SEO Review\n\nComplete report content.")
        self.assertEqual(len(result.changes), 1)
        self.assertFalse(result.changes[0].applicable)
        self.assertIn("无法在源正文中唯一定位", result.changes[0].validation_errors[0])

    def test_high_risk_number_change_requires_second_confirmation(self) -> None:
        result = parse_seo_review_response(
            response(proposed_text=f"{PROPOSED} The rated capacity is 250 kg."),
            source_article=ARTICLE.strip(),
            prompt_snapshot=snapshot(),
        )
        change = result.changes[0]
        self.assertEqual([risk.kind for risk in change.risks], ["number"])

        with self.assertRaisesRegex(SeoReviewError, "二次确认"):
            update_review_change(
                change,
                reviewed_text=change.reviewed_text,
                decision="accepted",
                confirm_risks=False,
            )

        accepted = update_review_change(
            change,
            reviewed_text=change.reviewed_text,
            decision="accepted",
            confirm_risks=True,
            decided_at="2026-07-29T01:00:00",
            decided_by="operator",
        )
        self.assertTrue(accepted.risk_confirmed)

    def test_accepted_change_builds_complete_candidate(self) -> None:
        generated = parse_seo_review_response(
            response(),
            source_article=ARTICLE.strip(),
            prompt_snapshot=snapshot(),
        )
        accepted = update_review_change(
            generated.changes[0],
            reviewed_text=generated.changes[0].reviewed_text,
            decision="accepted",
            confirm_risks=False,
            decided_at="2026-07-29T01:00:00",
            decided_by="operator",
        )
        review = SeoReviewRun(
            id="run",
            source_article=ARTICLE.strip(),
            source_article_hash="hash",
            source_revision=1,
            score=generated.score,
            dimensions=generated.dimensions,
            publish_ready=False,
            publish_recommendation=generated.publish_recommendation,
            report=generated.report,
            changes=[accepted],
            prompt_snapshot=generated.prompt_snapshot,
            created_at="2026-07-29T00:00:00",
        )

        candidate, change_ids = build_review_candidate(review)

        self.assertIn(PROPOSED, candidate)
        self.assertNotIn(TARGET, candidate)
        self.assertEqual(change_ids, [accepted.id])

    def test_missing_keywords_do_not_turn_topic_into_long_tail_keyword(self) -> None:
        with patch(
            "services.seo_review.collect_customer_context",
            return_value="No project facts supplied.",
        ):
            prompt, _ = build_seo_review_prompt(
                SimpleNamespace(),
                task(),
                ARTICLE,
                prompt_snapshot=snapshot(
                    "Check 主关键词【填写主关键词】and 长尾关键词【填写长尾关键词】."
                ),
                primary_keyword="",
                long_tail_keywords=[],
            )

        self.assertIn("不得因为运营人员未提供主关键词而扣分", prompt)
        self.assertIn("不得把文章话题或标题原句", prompt)
        self.assertIn("Roof Ladders - What Buyers Need to Know", prompt)


if __name__ == "__main__":
    unittest.main()
