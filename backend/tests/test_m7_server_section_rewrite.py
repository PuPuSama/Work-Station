from __future__ import annotations

import sys
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from models import TaskRecord  # noqa: E402
from services.server_section_rewrite import (  # noqa: E402
    SectionRewriteError,
    replace_markdown_section,
    rewrite_initial_article_section,
)


ARTICLE = """# Example Buyer Guide

This introduction points readers to [example.com](https://example.com/) before the detailed guidance.

## Buyer Checks

### Confirm the application

Keep the original application guidance.

### Compare evidence

Keep the original evidence guidance.

## FAQ

**Q: What should buyers send?**

A: Send requirements and quantities.

**Q: When should buyers request samples?**

A: Request samples before approval.

**Q: Why compare supplier capability?**

A: Capability affects quality and support.
"""


class ServerSectionRewriteTests(unittest.TestCase):
    def test_replaces_only_target_body_and_ignores_fenced_headings(
        self,
    ) -> None:
        replacement = """### Confirm the application

Use rewritten application guidance.

```markdown
## This is code, not a sibling section
```

### Compare evidence

Use rewritten evidence guidance."""

        result = replace_markdown_section(
            ARTICLE,
            heading_path=["Buyer Checks"],
            replacement_body=replacement,
        )

        original_prefix = ARTICLE.split("## Buyer Checks", 1)[0]
        original_suffix = "## FAQ" + ARTICLE.split("## FAQ", 1)[1]
        self.assertTrue(result.startswith(original_prefix))
        self.assertTrue(result.endswith(original_suffix))
        self.assertIn("Use rewritten application guidance.", result)
        self.assertNotIn("Keep the original application guidance.", result)

        malformed_sibling = ARTICLE.replace(
            "## FAQ",
            "# Unexpected sibling title\n\nMust remain.\n\n## FAQ",
        )
        bounded = replace_markdown_section(
            malformed_sibling,
            heading_path=["Buyer Checks"],
            replacement_body=replacement,
        )
        self.assertIn(
            "# Unexpected sibling title\n\nMust remain.",
            bounded,
        )

    def test_rejects_scope_escape_and_ambiguous_target(self) -> None:
        with self.assertRaisesRegex(
            SectionRewriteError,
            "at or above",
        ):
            replace_markdown_section(
                ARTICLE,
                heading_path=["Buyer Checks"],
                replacement_body="## FAQ\n\nInjected sibling.",
            )
        with self.assertRaisesRegex(
            SectionRewriteError,
            "at or above",
        ):
            replace_markdown_section(
                ARTICLE,
                heading_path=["Buyer Checks"],
                replacement_body="# Replaced article title",
            )

        duplicate = ARTICLE.replace(
            "## FAQ",
            "## Buyer Checks\n\nDuplicate.\n\n## FAQ",
        )
        with self.assertRaisesRegex(
            SectionRewriteError,
            "ambiguous",
        ):
            replace_markdown_section(
                duplicate,
                heading_path=["Buyer Checks"],
                replacement_body="### Safe child\n\nReplacement.",
            )

    def test_task_mutation_snapshots_before_and_after(self) -> None:
        task = TaskRecord(
            id="section-rewrite",
            week_folder="server",
            customer="example.com",
            topic_index=1,
            topic="Buyer guide",
            status="humanized_ready",
            selected_title="Example Buyer Guide",
            initial_article=ARTICLE,
            humanized_article="downstream copy",
            article="downstream copy",
            task_dir="/server/section-rewrite",
            created_at="2026-07-30T00:00:00+00:00",
            updated_at="2026-07-30T00:00:00+00:00",
        )

        rewrite_initial_article_section(
            task,
            heading_path=["Buyer Checks"],
            replacement_body="""### Confirm the application

Use revised application guidance.

### Compare evidence

Use revised evidence guidance.""",
        )

        self.assertEqual(task.status, "draft_ready")
        self.assertEqual(task.article, task.initial_article)
        self.assertEqual(task.humanized_article, "")
        self.assertEqual(
            [item.source_kind for item in task.article_versions],
            ["before_section_rewrite", "section_rewrite"],
        )
        self.assertEqual(task.article_versions[0].content, ARTICLE)
        self.assertEqual(
            task.article_versions[1].content,
            task.initial_article,
        )


if __name__ == "__main__":
    unittest.main()
