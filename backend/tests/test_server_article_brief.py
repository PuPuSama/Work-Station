from __future__ import annotations

import sys
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from models import ArticleBrief, ArticleBriefFact, TaskRecord  # noqa: E402
from services.server_article_brief import (  # noqa: E402
    ArticleBriefDraft,
    ArticleBriefUnavailable,
    LlmServerArticleBriefProvider,
    ServerArticleBriefService,
    article_brief_input_hash,
    build_article_brief_prompt,
)
from services.server_outline_generation import (  # noqa: E402
    PublishedOutlineContextChunk,
)


def make_task() -> TaskRecord:
    return TaskRecord(
        id="task-brief",
        week_folder="server",
        customer="example.test",
        topic_index=1,
        topic="How to choose an industrial energy storage system",
        primary_keyword="industrial energy storage",
        selected_title="How to Choose an Industrial Energy Storage System",
        project_introduction="Industrial energy storage for commercial facilities.",
        project_notes="Write for B2B procurement teams.",
        topic_notes="Explain lifecycle and integration concerns.",
        status="title_selected",
        task_dir="server/task-brief",
        created_at="2026-08-21T00:00:00+00:00",
        updated_at="2026-08-21T00:00:00+00:00",
    )


def make_chunks() -> tuple[PublishedOutlineContextChunk, ...]:
    return (
        PublishedOutlineContextChunk(
            chunk_id="chunk-a",
            heading_path=("Energy Storage", "Capacity"),
            text="The system supports load shifting for commercial facilities.",
            canonical_url="https://example.test/storage",
            source_kind="private_file",
        ),
        PublishedOutlineContextChunk(
            chunk_id="chunk-b",
            heading_path=("Energy Storage", "Integration"),
            text="Integration planning should account for the facility load profile.",
            canonical_url="https://example.test/integration",
            source_kind="product_detail",
        ),
    )


class StubBriefLlm:
    ready = True
    model = "brief-model"

    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[list[dict[str, object]]] = []

    def chat(self, messages, temperature=0.7, max_tokens=1800):
        del temperature, max_tokens
        self.calls.append(messages)
        return self.response


class BriefEngine:
    def __init__(self, rows) -> None:
        self.rows = rows

    def connect(self):
        rows = self.rows

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, _statement):
                class Result:
                    def mappings(self):
                        return self

                    def all(self):
                        return list(rows)

                return Result()

        return Connection()


class RecordingBriefProvider:
    ready = True

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, task, *, context_chunks):
        self.calls += 1
        return ArticleBriefDraft(
            article_intent="Help procurement teams compare storage options before issuing an RFQ.",
            target_buyers=["commercial procurement teams"],
            buyer_problems=["uncertain capacity and integration fit"],
            required_capabilities=["load-profile matching"],
            selection_dimensions=["capacity and integration"],
            recommended_product_roles=["primary_solution"],
            available_facts=[
                ArticleBriefFact(
                    fact="The system supports load shifting for commercial facilities.",
                    chunk_ids=[context_chunks[0].chunk_id],
                )
            ],
            missing_evidence=["verified round-trip efficiency range"],
        )


class StubBriefContext:
    def __init__(self, chunks) -> None:
        self.chunks = tuple(chunks)
        self.calls = 0

    def select(self, *, project_id, query, limit):
        del project_id, query, limit
        self.calls += 1
        return self.chunks


class ArticleBriefTests(unittest.TestCase):
    def test_prompt_contains_operator_context_and_chunk_identity(self) -> None:
        prompt = build_article_brief_prompt(make_task(), context_chunks=make_chunks())
        self.assertIn("B2B procurement teams", prompt)
        self.assertIn("chunk-a", prompt)
        self.assertIn("return exactly one json object", prompt.lower())

    def test_provider_requires_facts_to_use_supplied_chunk_ids(self) -> None:
        response = (
            '{"article_intent":"Compare options",'
            '"target_buyers":["buyers"],"buyer_problems":["fit"],'
            '"required_capabilities":["capacity"],"selection_dimensions":["cost"],'
            '"recommended_product_roles":["primary_solution"],'
            '"available_facts":[{"fact":"Unsupported","chunk_ids":["unknown"]}],'
            '"missing_evidence":[]}'
        )
        provider = LlmServerArticleBriefProvider(
            __import__("config").load_config(),
            llm=StubBriefLlm(response),
        )
        with self.assertRaises(ArticleBriefUnavailable):
            provider.generate(make_task(), context_chunks=make_chunks())

    def test_service_reuses_matching_brief_and_rebuilds_after_input_change(self) -> None:
        task = make_task()
        provider = RecordingBriefProvider()
        context = StubBriefContext(make_chunks())
        service = ServerArticleBriefService(
            BriefEngine(
                [
                    {
                        "source_id": "source-a",
                        "current_snapshot_id": "snapshot-a",
                        "source_kind": "private_file",
                    }
                ]
            ),
            provider=provider,
            context=context,
        )
        first = service.ensure_current(task, project_id="example.test")
        task.article_brief = first
        second = service.ensure_current(task, project_id="example.test")
        self.assertEqual(first.brief_id, second.brief_id)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(context.calls, 4)

        task.topic_notes = "Use a different integration example."
        self.assertNotEqual(article_brief_input_hash(task), first.input_hash)
        service.ensure_current(task, project_id="example.test")
        self.assertEqual(provider.calls, 2)

    def test_brief_serializes_in_task_json(self) -> None:
        task = make_task()
        task.article_brief = ArticleBrief(
            brief_id="brief-1",
            task_id=task.id,
            input_hash="a" * 64,
            title_hash="b" * 64,
            knowledge_snapshot_fingerprint="c" * 64,
            article_intent="Compare options.",
            context_chunk_ids=["chunk-a"],
        )
        restored = TaskRecord.model_validate(task.model_dump(mode="json"))
        self.assertEqual(restored.article_brief.brief_id, "brief-1")
        self.assertEqual(restored.article_brief.context_chunk_ids, ["chunk-a"])


if __name__ == "__main__":
    unittest.main()
