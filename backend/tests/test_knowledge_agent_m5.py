from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import app as app_module  # noqa: E402
from knowledge_agent.retrieval_plan_generation import (  # noqa: E402
    generate_retrieval_plan,
)
from knowledge_agent.contracts import KnowledgeChunk, RetrievalHit  # noqa: E402
from knowledge_agent.research_chat import (  # noqa: E402
    ResearchAnswer,
    ResearchChatService,
    ResearchCitationValidationError,
)
from knowledge_agent.research_chat_repository import (  # noqa: E402
    ResearchConversation,
)
from knowledge_agent.research_stream import (  # noqa: E402
    encode_sse,
    resolve_after_sequence,
)
from models import (  # noqa: E402
    ArticleVersion,
    Product,
    STATUS_OUTLINE_CONFIRMED,
    STATUS_OUTLINE_READY,
    TaskRecord,
)


def task(*, status: str = STATUS_OUTLINE_CONFIRMED) -> TaskRecord:
    return TaskRecord(
        id="task-6",
        week_folder="week-1",
        customer="www.example.com",
        brand_name="Example",
        topic_index=6,
        topic="How to choose fasteners",
        status=status,
        task_dir="tasks/task-6",
        outline="## Buyer checklist\n\n### Material\n\n## FAQ",
        products=[
            Product(
                product_id="p-1",
                name="Carbon Steel Screw",
                url="https://example.com/products/carbon-steel-screw",
            )
        ],
        article_versions=[
            ArticleVersion(
                kind="outline",
                content="## Buyer checklist",
                source_kind="generated",
            ),
            ArticleVersion(
                kind="outline",
                content="## Buyer checklist\n\n## FAQ",
                source_kind="manual_confirmed",
            ),
        ],
        created_at="2026-07-30T00:00:00+00:00",
        updated_at="2026-07-30T00:00:00+00:00",
    )


class RetrievalPlanGenerationTests(unittest.TestCase):
    def test_confirmed_outline_creates_ordered_h2_faq_and_product_scopes(self) -> None:
        plan = generate_retrieval_plan(
            project_id="example.com",
            article_id="topic_006",
            task_id="task-6",
            outline_version=2,
            outline="## Buyer checklist\n\n### Material\n\n## FAQ",
            topic="How to choose fasteners",
            products=[
                {
                    "name": "Carbon Steel Screw",
                    "url": "https://example.com/products/screw",
                }
            ],
        )

        self.assertEqual(plan.retrieval_plan_id, "plan-topic-006-outline-v2")
        self.assertEqual(
            [scope.scope_type for scope in plan.scopes],
            ["h2_section", "faq", "product_fact"],
        )
        self.assertEqual(
            [scope.ordinal for scope in plan.scopes],
            [0, 1, 2],
        )
        self.assertTrue(plan.scopes[-1].require_hard_fact)
        self.assertEqual(
            plan.scopes[-1].filters,
            {"canonical_urls": ["https://example.com/products/screw"]},
        )
        self.assertEqual(plan.metadata["task_id"], "task-6")
        self.assertNotIn("outline", plan.metadata)

    def test_empty_outline_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "confirmed outline"):
            generate_retrieval_plan(
                project_id="example.com",
                article_id="topic_006",
                task_id="task-6",
                outline_version=1,
                outline=" ",
                topic="Fasteners",
            )


class ResearchStreamContractTests(unittest.TestCase):
    def test_sse_frame_has_event_id_and_compact_json(self) -> None:
        frame = encode_sse(
            event="research_event",
            event_id=7,
            data={"sequence": 7, "label": "资料"},
        )

        self.assertEqual(
            frame,
            'id: 7\nevent: research_event\ndata: {"sequence":7,"label":"资料"}\n\n',
        )

    def test_cursor_prefers_query_and_rejects_invalid_header(self) -> None:
        self.assertEqual(resolve_after_sequence(9, "3"), 9)
        self.assertEqual(resolve_after_sequence(None, "3"), 3)
        with self.assertRaisesRegex(ValueError, "Last-Event-ID"):
            resolve_after_sequence(None, "secret")


class _ChatRetriever:
    def __init__(self) -> None:
        self.queries = []

    def retrieve(self, query):
        self.queries.append(query)
        return (
            RetrievalHit(
                chunk=KnowledgeChunk(
                    project_id=query.project_id,
                    chunk_id="snapshot-1:0",
                    source_id="source-1",
                    snapshot_id="snapshot-1",
                    text="Carbon steel is listed for the product.",
                ),
                score=0.9,
            ),
        )


class _AnswerProvider:
    def __init__(self, citation: str = "snapshot-1:0") -> None:
        self.citation = citation

    def answer(self, *, question, evidence_hits, recent_messages):
        return ResearchAnswer(
            text="The published product page lists carbon steel.",
            cited_chunk_ids=(self.citation,),
        )


class _ConversationRepository:
    def __init__(self) -> None:
        self.saved = []

    def get_conversation(self, project_id, conversation_id):
        return None

    def save_exchange(self, **values):
        self.saved.append(values)
        return ResearchConversation(
            project_id=values["project_id"],
            conversation_id=values["conversation_id"],
            article_id=values["article_id"],
        )


class ResearchChatServiceTests(unittest.TestCase):
    def test_question_is_project_scoped_and_valid_citation_is_persisted(self) -> None:
        retriever = _ChatRetriever()
        conversations = _ConversationRepository()
        service = ResearchChatService(
            retriever=retriever,  # type: ignore[arg-type]
            provider=_AnswerProvider(),
            conversations=conversations,  # type: ignore[arg-type]
        )

        result = service.ask(
            project_id="example.com",
            article_id="topic_006",
            conversation_id="chat-1",
            request_id="request-1",
            question="What material is used?",
        )

        self.assertEqual(result.conversation_id, "chat-1")
        self.assertEqual(retriever.queries[0].project_id, "example.com")
        self.assertEqual(
            conversations.saved[0]["cited_chunk_ids"],
            ("snapshot-1:0",),
        )

    def test_provider_cannot_cite_an_unsupplied_chunk(self) -> None:
        conversations = _ConversationRepository()
        service = ResearchChatService(
            retriever=_ChatRetriever(),  # type: ignore[arg-type]
            provider=_AnswerProvider("other-project:0"),
            conversations=conversations,  # type: ignore[arg-type]
        )

        with self.assertRaisesRegex(
            ResearchCitationValidationError,
            "outside the supplied evidence",
        ):
            service.ask(
                project_id="example.com",
                conversation_id="chat-1",
                request_id="request-1",
                question="What material is used?",
            )
        self.assertEqual(conversations.saved, [])


class _PlanRepository:
    def __init__(self) -> None:
        self.plan = None

    def save_retrieval_plan(self, plan) -> None:
        self.plan = plan

    def get_retrieval_plan(self, project_id: str, retrieval_plan_id: str):
        if (
            self.plan is not None
            and self.plan.project_id == project_id
            and self.plan.retrieval_plan_id == retrieval_plan_id
        ):
            return self.plan
        return None


class TaskRetrievalPlanEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_runtime = getattr(
            app_module.app.state,
            "knowledge_agent_runtime",
            None,
        )
        self.plans = _PlanRepository()
        self.projects = []
        app_module.app.state.knowledge_agent_runtime = SimpleNamespace(
            repository=SimpleNamespace(
                upsert_project=lambda project: self.projects.append(project)
            ),
            retrieval_plan_repository=self.plans,
        )

    def tearDown(self) -> None:
        app_module.app.state.knowledge_agent_runtime = self.previous_runtime

    def test_task_outline_is_frozen_with_project_and_article_identity(self) -> None:
        with patch.object(app_module, "get_task_or_404", return_value=task()):
            response = app_module.create_task_retrieval_plan(
                "www.example.com",
                "task-6",
            )

        self.assertEqual(response.project_id, "example.com")
        self.assertEqual(response.article_id, "topic_006")
        self.assertEqual(response.outline_version, 1)
        self.assertEqual(self.projects[0].project_id, "example.com")
        self.assertEqual(self.plans.plan.metadata["task_id"], "task-6")

    def test_unconfirmed_outline_and_cross_project_task_are_rejected(self) -> None:
        with patch.object(
            app_module,
            "get_task_or_404",
            return_value=task(status=STATUS_OUTLINE_READY),
        ):
            with self.assertRaisesRegex(Exception, "Confirm the outline"):
                app_module.create_task_retrieval_plan("example.com", "task-6")
        with patch.object(app_module, "get_task_or_404", return_value=task()):
            with self.assertRaisesRegex(Exception, "requested project"):
                app_module.create_task_retrieval_plan("other.test", "task-6")
