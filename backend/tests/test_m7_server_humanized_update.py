from __future__ import annotations

import sys
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from models import TaskRecord  # noqa: E402
from services.server_humanized_update import (  # noqa: E402
    ServerHumanizedArticleError,
    apply_reviewed_humanized_article,
)


ARTICLE = """# Buyer Guide

This introduction transitions into the detailed guidance.

## Buyer Checks

### Confirm requirements

Confirm the original application before selection.

### Compare evidence

Compare evidence before approval.

## FAQ

**Q: What should buyers confirm?**

A: Buyers should confirm requirements.

**Q: Why compare evidence?**

A: Evidence supports a reliable decision.

**Q: When should approval happen?**

A: Approval should follow the checks.
"""


def task() -> TaskRecord:
    return TaskRecord(
        id="server-humanized-task",
        week_folder="server",
        customer="example.test",
        topic_index=1,
        topic="Buyer guide",
        status="initial_ai_checked",
        initial_article=ARTICLE.strip(),
        article=ARTICLE.strip(),
        task_dir="/server/server-humanized-task",
        created_at="2026-07-31T00:00:00+00:00",
        updated_at="2026-07-31T00:00:00+00:00",
    )


class ServerHumanizedUpdateTests(unittest.TestCase):
    def test_applies_reviewed_copy_and_invalidates_downstream(self) -> None:
        record = task()
        record.final_article = "stale final"
        candidate = ARTICLE.replace(
            "Confirm the original application",
            "Confirm the reviewed application",
        )

        result = apply_reviewed_humanized_article(
            record,
            article=candidate,
        )

        self.assertEqual(result, candidate.strip())
        self.assertEqual(record.status, "humanized_ready")
        self.assertEqual(record.humanized_article, candidate.strip())
        self.assertEqual(record.article, candidate.strip())
        self.assertEqual(record.final_article, "")
        self.assertEqual(record.article_versions[-1].kind, "humanized")
        self.assertEqual(
            record.article_versions[-1].source_kind,
            "external_manual",
        )

    def test_rejects_heading_change_before_mutation(self) -> None:
        record = task()
        with self.assertRaisesRegex(
            ServerHumanizedArticleError,
            "heading hierarchy",
        ):
            apply_reviewed_humanized_article(
                record,
                article=ARTICLE.replace(
                    "## Buyer Checks",
                    "## Changed Buyer Checks",
                ),
            )
        self.assertEqual(record.humanized_article, "")
        self.assertEqual(record.status, "initial_ai_checked")
        self.assertEqual(record.article_versions, [])


if __name__ == "__main__":
    unittest.main()
