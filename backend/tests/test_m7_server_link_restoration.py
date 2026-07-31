from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from models import AICheck, ArticleImage, TaskRecord  # noqa: E402
from services.job_queue import JobConflict  # noqa: E402
from services.server_link_restoration import (  # noqa: E402
    LinkRestorationUnavailable,
    LinkTemplateReference,
    LlmServerLinkRestorationProvider,
    apply_restored_links,
)
from storage import content_hash  # noqa: E402


SOURCE_ARTICLE = """# Guide

Read the [official guide](https://example.com/guide) before choosing.

## Checks

Keep this paragraph unchanged.
"""

CANDIDATE_ARTICLE = """# Guide

Read the official guide before choosing.

## Checks

Keep this paragraph unchanged.
"""


class StubLinkLlm:
    ready = True

    def __init__(
        self,
        *,
        result: str = SOURCE_ARTICLE,
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


def task_for_link_restore() -> TaskRecord:
    source_hash = content_hash(SOURCE_ARTICLE)
    candidate_hash = content_hash(CANDIDATE_ARTICLE)
    return TaskRecord(
        id="topic-1",
        week_folder="server",
        customer="project-a",
        topic_index=1,
        topic="Guide",
        status="final_ai_checked",
        task_dir="/server/topic-1",
        initial_article=SOURCE_ARTICLE,
        initial_article_hash=source_hash,
        humanized_article=CANDIDATE_ARTICLE,
        humanized_article_hash=candidate_hash,
        article=CANDIDATE_ARTICLE,
        final_ai_check=AICheck(
            confirmed=True,
            article_hash=candidate_hash,
        ),
        images=[
            ArticleImage(
                id="later-image",
                source_asset_id="private-asset",
            )
        ],
        docx_asset_id="later-docx",
        created_at="2026-07-31T00:00:00+00:00",
        updated_at="2026-07-31T00:00:00+00:00",
    )


class ServerLinkRestorationUnitTests(unittest.TestCase):
    def test_provider_uses_zero_temperature_and_never_leaks_gateway_secret(
        self,
    ) -> None:
        secret = "gateway-secret-value"
        llm = StubLinkLlm(error=RuntimeError(secret))
        provider = LlmServerLinkRestorationProvider(
            object(),  # type: ignore[arg-type]
            llm=llm,
        )

        with self.assertRaises(LinkRestorationUnavailable) as raised:
            provider.restore(
                source_article=SOURCE_ARTICLE,
                candidate_article=CANDIDATE_ARTICLE,
                missing_links=[
                    {
                        "anchor": "official guide",
                        "url": "https://example.com/guide",
                        "count": 1,
                        "heading": "Guide",
                        "context": "Read the official guide",
                    }
                ],
            )

        self.assertNotIn(secret, str(raised.exception))
        self.assertEqual(llm.calls[0]["temperature"], 0.0)

    def test_provider_does_not_call_model_when_no_links_are_missing(
        self,
    ) -> None:
        llm = StubLinkLlm(error=AssertionError("must not call"))
        provider = LlmServerLinkRestorationProvider(
            object(),  # type: ignore[arg-type]
            llm=llm,
        )

        result = provider.restore(
            source_article=SOURCE_ARTICLE,
            candidate_article=SOURCE_ARTICLE,
            missing_links=[],
        )

        self.assertEqual(result, SOURCE_ARTICLE)
        self.assertEqual(llm.calls, [])

    def test_provider_rejects_empty_output_without_mock_fallback(
        self,
    ) -> None:
        provider = LlmServerLinkRestorationProvider(
            object(),  # type: ignore[arg-type]
            llm=StubLinkLlm(result=""),
        )

        with self.assertRaisesRegex(
            LinkRestorationUnavailable,
            "invalid result",
        ):
            provider.restore(
                source_article=SOURCE_ARTICLE,
                candidate_article=CANDIDATE_ARTICLE,
                missing_links=[
                    {
                        "anchor": "official guide",
                        "url": "https://example.com/guide",
                        "count": 1,
                        "heading": "Guide",
                        "context": "Read the official guide",
                    }
                ],
            )

    def test_apply_restored_links_commits_only_exact_link_set_and_text(
        self,
    ) -> None:
        task = task_for_link_restore()

        source_count, restored_count = apply_restored_links(
            task,
            source_article=SOURCE_ARTICLE,
            candidate_article=CANDIDATE_ARTICLE,
            restored_article=SOURCE_ARTICLE,
            prompt_version=LinkTemplateReference.current().content_hash,
        )

        self.assertEqual((source_count, restored_count), (1, 1))
        self.assertEqual(task.status, "links_verified")
        self.assertEqual(task.article, SOURCE_ARTICLE.strip())
        self.assertTrue(task.link_validation.passed)
        self.assertTrue(task.link_validation.visible_text_unchanged)
        self.assertEqual(task.images, [])
        self.assertEqual(task.docx_asset_id, "")
        self.assertEqual(task.article_versions[-1].kind, "linked")
        self.assertEqual(
            task.article_versions[-1].source_hash,
            content_hash(CANDIDATE_ARTICLE),
        )

    def test_apply_restored_links_rejects_copy_or_url_change(self) -> None:
        task = task_for_link_restore()

        with self.assertRaisesRegex(
            LinkRestorationUnavailable,
            "invalid result",
        ):
            apply_restored_links(
                task,
                source_article=SOURCE_ARTICLE,
                candidate_article=CANDIDATE_ARTICLE,
                restored_article=SOURCE_ARTICLE.replace(
                    "Keep this paragraph unchanged.",
                    "Changed copy.",
                ),
                prompt_version=LinkTemplateReference.current().content_hash,
            )

        self.assertEqual(task.status, "final_ai_checked")
        self.assertEqual(task.linked_article, "")

        task = task_for_link_restore()
        with self.assertRaisesRegex(
            LinkRestorationUnavailable,
            "invalid result",
        ):
            apply_restored_links(
                task,
                source_article=SOURCE_ARTICLE,
                candidate_article=CANDIDATE_ARTICLE,
                restored_article=SOURCE_ARTICLE.replace(
                    "https://example.com/guide",
                    "https://attacker.example/guide",
                ),
                prompt_version=LinkTemplateReference.current().content_hash,
            )
        self.assertEqual(task.linked_article, "")

    def test_template_reference_rejects_drift_and_malformed_identity(
        self,
    ) -> None:
        reference = LinkTemplateReference.current()
        with patch(
            "services.server_link_restoration.load_prompt_template",
            return_value="changed template",
        ):
            with self.assertRaisesRegex(
                JobConflict,
                "template changed",
            ):
                reference.verify_current()
        with self.assertRaisesRegex(
            JobConflict,
            "identity is invalid",
        ):
            LinkTemplateReference.from_mapping(
                {
                    "template_name": "restore_links",
                    "template_hash": "not-a-hash",
                }
            )


if __name__ == "__main__":
    unittest.main()
