from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services.article_validation import (  # noqa: E402
    ArticleStructureError,
    validate_humanized_article,
)
from services.generator import humanize_article  # noqa: E402


SOURCE = """# Industrial Buyer Guide

This opening helps you compare the Alpha Unit for a 25 kg application.

## What Should You Check?

- Confirm the 25 kg load.
- Review the target phrase.

## FAQ

**Q: What load is used in this example?**

A: The confirmed load is 25 kg.

**Q: Which product is named in this guide?**

A: The guide names the Alpha Unit.

**Q: Which phrase must remain exact?**

A: The required phrase is target phrase.
"""


class FakeLLM:
    def __init__(self, response: str):
        self.response = response

    def chat(self, _messages, temperature=0.7, max_tokens=1800):
        return self.response


def task():
    return SimpleNamespace(
        article=SOURCE,
        selected_title="Industrial Buyer Guide",
        topic="target phrase",
        competitor_keyword="target phrase",
        products=[SimpleNamespace(name="Alpha Unit")],
    )


class HumanizeValidationTests(unittest.TestCase):
    def run_humanize(self, candidate: str) -> str:
        with tempfile.TemporaryDirectory() as directory:
            prompt = Path(directory) / "humanize.txt"
            prompt.write_text("Rules\n{{ARTICLE}}", encoding="utf-8")
            config = SimpleNamespace(
                humanize_prompt_path=prompt,
            )
            return humanize_article(config, task(), SOURCE, llm=FakeLLM(candidate))

    def test_accepts_rephrasing_that_preserves_locked_assets(self) -> None:
        candidate = SOURCE.replace(
            "This opening helps you compare",
            "Use this opening when you compare",
        )
        self.assertEqual(self.run_humanize(candidate), candidate.strip())

    def test_rejects_changed_numeric_fact(self) -> None:
        candidate = SOURCE.replace("25 kg", "30 kg")
        with self.assertRaisesRegex(ArticleStructureError, "numeric facts"):
            self.run_humanize(candidate)

    def test_rejects_removed_exact_product_or_keyword(self) -> None:
        candidate = SOURCE.replace("Alpha Unit", "the product")
        with self.assertRaisesRegex(ArticleStructureError, "required exact phrase"):
            self.run_humanize(candidate)

    def test_rejects_question_that_loses_markdown_bold(self) -> None:
        candidate = SOURCE.replace(
            "**Q: What load is used in this example?**",
            "Q: What load is used in this example?",
        )
        with self.assertRaisesRegex(ArticleStructureError, "complete bold Markdown line"):
            self.run_humanize(candidate)

    def test_legacy_source_can_move_and_bold_faq_during_repair(self) -> None:
        legacy_source = SOURCE.replace("**Q:", "Q:").replace("?**", "?")
        legacy_source = legacy_source.replace(
            "## FAQ",
            "## FAQ",
        ) + "\n## Conclusion\n\nUse the confirmed facts to make the next decision.\n"
        repaired = SOURCE.replace(
            "## FAQ",
            "## Conclusion\n\nUse the confirmed facts to make the next decision.\n\n## FAQ",
        )

        validate_humanized_article(
            legacy_source,
            repaired,
            required_phrases=("Alpha Unit", "target phrase"),
        )


if __name__ == "__main__":
    unittest.main()
