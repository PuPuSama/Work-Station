from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from models import PromptSnapshot, TaskRecord  # noqa: E402
from services.job_queue import (  # noqa: E402
    JobConflict,
    is_retryable_error,
)
from services.server_outline_generation import (  # noqa: E402
    PublishedGenerationContextChunk,
)
from services.server_seo_review_generation import (  # noqa: E402
    LlmServerSeoReviewProvider,
    ReviewTemplateReference,
    SeoReviewGenerationUnavailable,
    apply_generated_seo_review,
)
from services.seo_review import parse_seo_review_response  # noqa: E402
from storage import content_hash  # noqa: E402


ARTICLE = """# Buyer Guide

This introduction explains the buying decision.

## Checks

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


def review_json() -> str:
    return json.dumps(
        {
            "publish_ready": True,
            "publish_recommendation": "Ready after human review.",
            "dimensions": [
                {
                    "key": "eeat",
                    "name": "E-E-A-T",
                    "score": 9,
                    "target_score": 9,
                    "main_issue": "",
                    "needs_revision": False,
                }
            ],
            "report": "## Review\n\nNo blocking issue.",
            "changes": [],
        }
    )


class StubReviewLlm:
    ready = True

    def __init__(
        self,
        *,
        result: str = "",
        error: Exception | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.calls: list[dict[str, object]] = []

    def chat(self, messages, temperature=0.7, max_tokens=1800):
        self.calls.append(
            {
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
        )
        if self.error is not None:
            raise self.error
        return self.result


def task() -> TaskRecord:
    return TaskRecord(
        id="topic-1",
        week_folder="server",
        customer="project-a",
        topic_index=1,
        topic="Buyer Guide",
        status="draft_ready",
        task_dir="/server/topic-1",
        initial_article=ARTICLE,
        initial_article_hash=content_hash(ARTICLE),
        article=ARTICLE,
        seo_primary_keyword="buyer guide",
        created_at="2026-07-31T00:00:00+00:00",
        updated_at="2026-07-31T00:00:00+00:00",
    )


def snapshot() -> PromptSnapshot:
    return PromptSnapshot(
        kind="review",
        source="system",
        captured_at="2026-07-31T00:00:00+00:00",
    )


class ServerSeoReviewGenerationTests(unittest.TestCase):
    def test_provider_uses_only_injected_published_context(self) -> None:
        llm = StubReviewLlm(result=review_json())
        provider = LlmServerSeoReviewProvider(
            object(),  # type: ignore[arg-type]
            llm=llm,
        )
        chunks = (
            PublishedGenerationContextChunk(
                chunk_id="chunk-a",
                heading_path=("Facts",),
                text="Published server fact.",
                canonical_url="https://project-a/facts",
            ),
        )

        with patch(
            "services.seo_review.collect_customer_context",
            side_effect=AssertionError("local context must not be read"),
        ):
            generated = provider.generate(
                task(),
                article=ARTICLE,
                prompt_snapshot=snapshot(),
                context_chunks=chunks,
            )

        self.assertTrue(generated.publish_ready)
        self.assertTrue(generated.prompt_snapshot.content.strip())
        prompt = str(llm.calls[0]["messages"])
        self.assertIn("Published server fact.", prompt)
        self.assertEqual(llm.calls[0]["temperature"], 0.2)

    def test_provider_sanitizes_gateway_and_invalid_output(self) -> None:
        secret = "private-gateway-secret"
        provider = LlmServerSeoReviewProvider(
            object(),  # type: ignore[arg-type]
            llm=StubReviewLlm(error=RuntimeError(secret)),
        )
        with self.assertRaises(SeoReviewGenerationUnavailable) as raised:
            provider.generate(
                task(),
                article=ARTICLE,
                prompt_snapshot=snapshot(),
                context_chunks=(),
            )
        self.assertNotIn(secret, str(raised.exception))

        invalid = LlmServerSeoReviewProvider(
            object(),  # type: ignore[arg-type]
            llm=StubReviewLlm(result="not json"),
        )
        with self.assertRaisesRegex(
            SeoReviewGenerationUnavailable,
            "invalid result",
        ) as raised:
            invalid.generate(
                task(),
                article=ARTICLE,
                prompt_snapshot=snapshot(),
                context_chunks=(),
            )
        self.assertTrue(is_retryable_error(raised.exception))

    def test_apply_appends_open_run_without_applying_changes(self) -> None:
        current = task()
        generated = parse_seo_review_response(
            review_json(),
            source_article=ARTICLE,
            prompt_snapshot=snapshot(),
        )

        run = apply_generated_seo_review(
            current,
            job_id="job-a",
            source_revision=0,
            article=ARTICLE,
            generated=generated,
        )

        self.assertEqual(run.status, "open")
        self.assertEqual(current.initial_article, ARTICLE)
        self.assertEqual(current.article, ARTICLE)
        self.assertEqual(len(current.seo_reviews), 1)
        with self.assertRaisesRegex(
            JobConflict,
            "already exists",
        ):
            apply_generated_seo_review(
                current,
                job_id="job-a",
                source_revision=0,
                article=ARTICLE,
                generated=generated,
            )
        drifted = task()
        drifted.revision = 1
        with self.assertRaisesRegex(
            JobConflict,
            "revision changed",
        ):
            apply_generated_seo_review(
                drifted,
                job_id="job-b",
                source_revision=0,
                article=ARTICLE,
                generated=generated,
            )

    def test_system_template_reference_rejects_drift(self) -> None:
        reference = ReviewTemplateReference.current()
        with patch(
            "services.server_seo_review_generation.load_prompt_template",
            return_value="changed rubric",
        ):
            with self.assertRaisesRegex(
                JobConflict,
                "template changed",
            ):
                reference.verify_current()


if __name__ == "__main__":
    unittest.main()
