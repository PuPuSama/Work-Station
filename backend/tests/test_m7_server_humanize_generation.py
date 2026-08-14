from __future__ import annotations

import sys
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from models import PromptSnapshot, TaskRecord  # noqa: E402
from services.server_humanize_generation import (  # noqa: E402
    HumanizeGenerationUnavailable,
    LlmServerHumanizeProvider,
    apply_generated_humanized_article,
)
from services.job_queue import JobConflict  # noqa: E402
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


class StubLlm:
    ready = True

    def __init__(self, result: str = "", error: Exception | None = None):
        self.result = result
        self.error = error
        self.calls: list[object] = []

    def chat(self, messages, temperature=0.7, max_tokens=1800):
        self.calls.append(messages)
        if self.error:
            raise self.error
        return self.result


def task() -> TaskRecord:
    article = ARTICLE.strip()
    return TaskRecord(
        id="task-a",
        week_folder="server",
        customer="example.com",
        topic_index=1,
        topic="Buyer Guide",
        status="initial_ai_checked",
        task_dir="/server/task-a",
        initial_article=article,
        initial_article_hash=content_hash(article),
        article=article,
        created_at="2026-07-31T00:00:00+00:00",
        updated_at="2026-07-31T00:00:00+00:00",
    )


def prompt() -> PromptSnapshot:
    return PromptSnapshot(
        prompt_id="humanize-a",
        name="Humanize",
        kind="humanize",
        content="Rewrite safely.\n\n{{ARTICLE}}",
        version=1,
        source="project_default",
    )


class ServerHumanizeGenerationTests(unittest.TestCase):
    def test_provider_uses_pinned_prompt_without_local_file(self) -> None:
        llm = StubLlm(result=ARTICLE.strip())
        provider = LlmServerHumanizeProvider(
            object(),  # type: ignore[arg-type]
            llm=llm,
        )

        result = provider.generate(
            task(),
            source_article=ARTICLE.strip(),
            prompt_snapshot=prompt(),
        )

        self.assertEqual(result, ARTICLE.strip())
        messages = llm.calls[0]  # type: ignore[assignment]
        system_prompt = messages[0]["content"]  # type: ignore[index]
        user_prompt = messages[1]["content"]  # type: ignore[index]
        self.assertIn("State supported facts directly", system_prompt)
        self.assertIn(
            "Never expose source, website, supplier, manufacturer, product-page",
            system_prompt,
        )
        self.assertIn("Preserve supplied img index-tag blocks exactly", system_prompt)
        self.assertIn(ARTICLE.strip(), user_prompt)
        self.assertNotIn("{{ARTICLE}}", user_prompt)

    def test_provider_errors_are_sanitized(self) -> None:
        secret = "private-gateway-secret"
        provider = LlmServerHumanizeProvider(
            object(),  # type: ignore[arg-type]
            llm=StubLlm(error=RuntimeError(secret)),
        )

        with self.assertRaises(HumanizeGenerationUnavailable) as raised:
            provider.generate(
                task(),
                source_article=ARTICLE.strip(),
                prompt_snapshot=prompt(),
            )

        self.assertNotIn(secret, str(raised.exception))

    def test_apply_creates_humanized_version_and_advances(self) -> None:
        current = task()

        rehumanizing = apply_generated_humanized_article(
            current,
            source_revision=0,
            source_article=ARTICLE.strip(),
            candidate=ARTICLE.strip(),
        )

        self.assertFalse(rehumanizing)
        self.assertEqual(current.status, "humanized_ready")
        self.assertEqual(current.humanized_article, ARTICLE.strip())
        self.assertEqual(current.article_versions[-1].kind, "humanized")

    def test_apply_revalidates_provider_output_before_mutation(self) -> None:
        current = task()

        with self.assertRaisesRegex(
            JobConflict,
            "humanize result is invalid",
        ):
            apply_generated_humanized_article(
                current,
                source_revision=0,
                source_article=ARTICLE.strip(),
                candidate="# Replaced article",
            )

        self.assertEqual(current.status, "initial_ai_checked")
        self.assertEqual(current.humanized_article, "")
        self.assertEqual(current.article_versions, [])

    def test_apply_rejects_inconsistent_stored_source_hash(self) -> None:
        current = task()
        current.initial_article_hash = "0" * 64

        with self.assertRaisesRegex(
            JobConflict,
            "source article identity is invalid",
        ):
            apply_generated_humanized_article(
                current,
                source_revision=0,
                source_article=ARTICLE.strip(),
                candidate=ARTICLE.strip(),
            )

        self.assertEqual(current.status, "initial_ai_checked")
        self.assertEqual(current.humanized_article, "")


if __name__ == "__main__":
    unittest.main()
