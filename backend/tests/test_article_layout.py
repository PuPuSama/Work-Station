from __future__ import annotations

import sys
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services.article_validation import (  # noqa: E402
    ArticleStructureError,
    validate_article_layout,
)


VALID_ARTICLE = """# Buyer Guide

This opening prepares the reader for the comparison.

## Main Section

Body copy.

## FAQ

**Q: What should a buyer check first?**

A: Check the application requirements first.

**Q: When should a buyer request a sample?**

A: Request one before approval when fit must be confirmed.

**Q: Why should a buyer compare suppliers?**

A: Compare capability, quality control, delivery, and support.
"""


class ArticleLayoutTests(unittest.TestCase):
    def test_accepts_one_final_h2_faq_with_three_bold_questions(self) -> None:
        validate_article_layout(VALID_ARTICLE)

    def test_rejects_faq_below_h2_level(self) -> None:
        candidate = VALID_ARTICLE.replace("## FAQ", "### FAQ")
        with self.assertRaisesRegex(ArticleStructureError, "exactly as '## FAQ'"):
            validate_article_layout(candidate)

    def test_rejects_an_h2_after_faq(self) -> None:
        candidate = VALID_ARTICLE + "\n## Conclusion\n\nClosing copy.\n"
        with self.assertRaisesRegex(ArticleStructureError, "final H2"):
            validate_article_layout(candidate)

    def test_rejects_unbolded_question(self) -> None:
        candidate = VALID_ARTICLE.replace(
            "**Q: What should a buyer check first?**",
            "Q: What should a buyer check first?",
        )
        with self.assertRaisesRegex(ArticleStructureError, "complete bold Markdown line"):
            validate_article_layout(candidate)

    def test_rejects_fewer_than_three_pairs(self) -> None:
        candidate = VALID_ARTICLE.rsplit("\n\n**Q:", 1)[0] + "\n"
        with self.assertRaisesRegex(ArticleStructureError, "exactly three Q/A pairs"):
            validate_article_layout(candidate)

    def test_rejects_copy_after_the_last_answer(self) -> None:
        candidate = VALID_ARTICLE + "\nA final summary must not appear here.\n"
        with self.assertRaisesRegex(ArticleStructureError, "no content after"):
            validate_article_layout(candidate)


if __name__ == "__main__":
    unittest.main()
