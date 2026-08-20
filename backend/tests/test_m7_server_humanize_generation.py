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
from services.article_validation import visible_word_count  # noqa: E402
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

LOCKED_ARTICLE = ARTICLE.replace(
    "This introduction explains the buying decision.",
    "This introduction explains the buying decision. Consult the "
    "[reference](https://example.com/spec).\n\n"
    "![Buyer Guide](images/buyer-guide.webp)\n\n"
    "img.Buyer Guide.webp",
    1,
)


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


class SequentialLlm(StubLlm):
    def __init__(self, results: list[str]):
        super().__init__()
        self.results = list(results)

    def chat(self, messages, temperature=0.7, max_tokens=1800):
        self.calls.append(messages)
        return self.results.pop(0)


class StubLlmFactory:
    def __init__(self, client: StubLlm, *, ready: bool = True):
        self.ready = ready
        self._client = client
        self.calls: list[tuple[str, str]] = []

    def client(self, organization_id: str, user_id: str) -> StubLlm:
        self.calls.append((organization_id, user_id))
        return self._client


def article_with_word_count(target: int, *, base: str = ARTICLE.strip()) -> str:
    current = visible_word_count(base)
    if target < current:
        raise ValueError("target is below fixture minimum")
    return base.replace(
        "This introduction explains the buying decision.",
        "This introduction explains the buying decision. "
        + " ".join("detail" for _ in range(target - current)),
        1,
    )


def task(article: str = ARTICLE.strip()) -> TaskRecord:
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

        with self.assertLogs(
            "services.server_humanize_generation",
            level="WARNING",
        ) as logs:
            with self.assertRaises(HumanizeGenerationUnavailable) as raised:
                provider.generate(
                    task(),
                    source_article=ARTICLE.strip(),
                    prompt_snapshot=prompt(),
                )

        self.assertNotIn(secret, str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        diagnostics = "\n".join(logs.output)
        self.assertIn("category=provider_runtime", diagnostics)
        self.assertIn("source_words=", diagnostics)
        self.assertIn("candidate_words=0", diagnostics)
        self.assertNotIn(secret, diagnostics)

    def test_provider_uses_requesting_users_llm_factory_client(self) -> None:
        selected_client = StubLlm(result=ARTICLE.strip())
        fallback_client = StubLlm(result="# fallback must not run")
        fallback_client.ready = False
        factory = StubLlmFactory(selected_client)
        provider = LlmServerHumanizeProvider(
            object(),  # type: ignore[arg-type]
            llm=fallback_client,
            llm_factory=factory,  # type: ignore[arg-type]
        )

        self.assertTrue(provider.ready)
        result = provider.generate_for_organization(
            task(),
            organization_id="org-a",
            user_id="user-a",
            source_article=ARTICLE.strip(),
            prompt_snapshot=prompt(),
        )

        self.assertEqual(result, ARTICLE.strip())
        self.assertEqual(factory.calls, [("org-a", "user-a")])
        self.assertEqual(len(selected_client.calls), 1)
        self.assertEqual(fallback_client.calls, [])

    def test_provider_strips_one_outer_markdown_fence(self) -> None:
        provider = LlmServerHumanizeProvider(
            object(),  # type: ignore[arg-type]
            llm=StubLlm(result=f"```markdown\n{ARTICLE.strip()}\n```"),
        )

        result = provider.generate(
            task(),
            source_article=ARTICLE.strip(),
            prompt_snapshot=prompt(),
        )

        self.assertEqual(result, ARTICLE.strip())

    def test_provider_retries_an_initial_result_that_changes_structure(self) -> None:
        llm = SequentialLlm(["# Wrong heading", ARTICLE.strip()])
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
        self.assertEqual(len(llm.calls), 2)
        retry_prompt = llm.calls[1][1]["content"]  # type: ignore[index]
        self.assertIn("previous result was empty or changed locked content", retry_prompt)

    def test_provider_semantically_rewrites_an_oversized_article_once(self) -> None:
        source = article_with_word_count(1300)
        oversized = article_with_word_count(1500)
        corrected = article_with_word_count(1100)
        llm = SequentialLlm([oversized, corrected])
        provider = LlmServerHumanizeProvider(
            object(),  # type: ignore[arg-type]
            llm=llm,
        )

        result = provider.generate(
            task(),
            source_article=source,
            prompt_snapshot=prompt(),
        )

        self.assertEqual(visible_word_count(result), 1100)
        self.assertEqual(len(llm.calls), 2)
        correction_prompt = llm.calls[1][1]["content"]  # type: ignore[index]
        self.assertIn("Semantically compress", correction_prompt)
        self.assertIn("do not mechanically truncate", correction_prompt)
        self.assertIn("approximately 360 words", correction_prompt)

    def test_provider_accepts_small_overshoot_without_retry_or_truncation(self) -> None:
        source = article_with_word_count(1808)
        accepted = article_with_word_count(1237)
        llm = SequentialLlm([accepted])
        provider = LlmServerHumanizeProvider(
            object(),  # type: ignore[arg-type]
            llm=llm,
        )

        result = provider.generate(
            task(),
            source_article=source,
            prompt_snapshot=prompt(),
        )

        self.assertEqual(result, accepted)
        self.assertEqual(visible_word_count(result), 1237)
        self.assertTrue(result.endswith("A: It affects support."))
        self.assertEqual(len(llm.calls), 1)

    def test_repeated_overshoot_accepts_wide_tolerance_without_truncation(
        self,
    ) -> None:
        source = article_with_word_count(1808)
        accepted = article_with_word_count(1399)
        llm = SequentialLlm(
            [
                article_with_word_count(1451),
                article_with_word_count(1419),
                accepted,
            ]
        )
        provider = LlmServerHumanizeProvider(
            object(),  # type: ignore[arg-type]
            llm=llm,
        )

        result = provider.generate(
            task(),
            source_article=source,
            prompt_snapshot=prompt(),
        )

        self.assertEqual(result, accepted)
        self.assertEqual(visible_word_count(result), 1399)
        self.assertTrue(result.endswith("A: It affects support."))
        self.assertEqual(len(llm.calls), 3)
        final_prompt = llm.calls[2][1]["content"]  # type: ignore[index]
        self.assertIn("has 1419 visible English words", final_prompt)
        self.assertIn("preferred 1000-1200 target", final_prompt)
        self.assertIn("do not mechanically truncate", final_prompt)

    def test_word_correction_keeps_best_valid_progress(self) -> None:
        source = article_with_word_count(1300)
        llm = SequentialLlm(
            [
                article_with_word_count(1500),
                article_with_word_count(1450),
                article_with_word_count(1470),
                article_with_word_count(1399),
            ]
        )
        provider = LlmServerHumanizeProvider(
            object(),  # type: ignore[arg-type]
            llm=llm,
        )

        result = provider.generate(
            task(),
            source_article=source,
            prompt_snapshot=prompt(),
        )

        self.assertEqual(visible_word_count(result), 1399)
        third_correction_prompt = llm.calls[3][1]["content"]  # type: ignore[index]
        self.assertIn("has 1450 visible English words", third_correction_prompt)
        self.assertNotIn("has 1470 visible English words", third_correction_prompt)

    def test_word_correction_rejects_changed_links_and_image_markers(self) -> None:
        source = article_with_word_count(1300, base=LOCKED_ARTICLE.strip())
        oversized = article_with_word_count(1500, base=LOCKED_ARTICLE.strip())
        invalid_link = article_with_word_count(
            1100,
            base=LOCKED_ARTICLE.strip(),
        ).replace(
            "[reference](https://example.com/spec)",
            "reference",
            1,
        )
        invalid_marker = article_with_word_count(
            1100,
            base=LOCKED_ARTICLE.strip(),
        ).replace("img.Buyer Guide.webp", "img.changed.webp", 1)
        corrected = article_with_word_count(1100, base=LOCKED_ARTICLE.strip())
        llm = SequentialLlm([oversized, invalid_link, invalid_marker, corrected])
        provider = LlmServerHumanizeProvider(
            object(),  # type: ignore[arg-type]
            llm=llm,
        )

        with self.assertLogs(
            "services.server_humanize_generation",
            level="WARNING",
        ) as logs:
            result = provider.generate(
                task(),
                source_article=source,
                prompt_snapshot=prompt(),
            )

        self.assertEqual(result, corrected)
        diagnostics = "\n".join(logs.output)
        self.assertIn("category=correction_locked_content", diagnostics)
        self.assertIn("candidate_words=1100", diagnostics)
        self.assertEqual(diagnostics.count("category=correction_locked_content"), 2)
        self.assertNotIn("https://example.com/spec", diagnostics)
        self.assertNotIn("img.Buyer Guide.webp", diagnostics)

    def test_provider_rejects_repeated_results_beyond_wide_tolerance(self) -> None:
        source = article_with_word_count(1300)
        llm = SequentialLlm(
            [
                article_with_word_count(1500),
                article_with_word_count(1401),
                article_with_word_count(1401),
                article_with_word_count(1401),
                article_with_word_count(1401),
                article_with_word_count(1401),
                article_with_word_count(1401),
            ]
        )
        provider = LlmServerHumanizeProvider(
            object(),  # type: ignore[arg-type]
            llm=llm,
        )

        with self.assertRaisesRegex(
            HumanizeGenerationUnavailable,
            "invalid result",
        ):
            provider.generate(
                task(),
                source_article=source,
                prompt_snapshot=prompt(),
            )
        self.assertEqual(len(llm.calls), 7)

    def test_word_correction_changes_direction_after_undershooting(self) -> None:
        source = article_with_word_count(1300)
        llm = SequentialLlm(
            [
                article_with_word_count(1500),
                article_with_word_count(850),
                article_with_word_count(1100),
            ]
        )
        provider = LlmServerHumanizeProvider(
            object(),  # type: ignore[arg-type]
            llm=llm,
        )

        result = provider.generate(
            task(),
            source_article=source,
            prompt_snapshot=prompt(),
        )

        self.assertEqual(visible_word_count(result), 1100)
        expansion_prompt = llm.calls[2][1]["content"]  # type: ignore[index]
        self.assertIn("Semantically expand", expansion_prompt)

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

    def test_apply_rejects_changed_image_marker_before_mutation(self) -> None:
        source = LOCKED_ARTICLE.strip()
        current = task(source)

        with self.assertRaisesRegex(
            JobConflict,
            "humanize result is invalid",
        ) as raised:
            apply_generated_humanized_article(
                current,
                source_revision=0,
                source_article=source,
                candidate=source.replace(
                    "img.Buyer Guide.webp",
                    "img.changed.webp",
                    1,
                ),
            )

        self.assertEqual(current.status, "initial_ai_checked")
        self.assertEqual(current.humanized_article, "")
        self.assertEqual(current.article_versions, [])
        self.assertIsNone(raised.exception.__cause__)

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
