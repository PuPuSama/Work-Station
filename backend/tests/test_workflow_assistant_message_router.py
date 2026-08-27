from __future__ import annotations

import json
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from knowledge_agent.research_chat_repository import (
    ResearchCitation,
    ResearchConversation,
    ResearchMessage,
)
from services.access_control import ActorIdentity
from workflow_assistant.context import AssistantProjectContext, AssistantWorkspaceContext
from workflow_assistant.contracts import AssistantMessageRequest
from workflow_assistant.http import _knowledge_reply, append_message
from workflow_assistant.message_router import AssistantMessageRouter, render_knowledge_answer
from workflow_assistant.repository import AssistantConversation, AssistantMessage


class FakeClient:
    ready = True

    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls: list[list[dict[str, object]]] = []

    def chat(self, messages, temperature=0.7, max_tokens=1800):
        del temperature, max_tokens
        self.calls.append(messages)
        return self.responses.pop(0)


class FakeFactory:
    ready = True

    def __init__(self, client: FakeClient) -> None:
        self.client_instance = client
        self.identities: list[tuple[str, str]] = []

    def client(self, organization_id: str, user_id: str) -> FakeClient:
        self.identities.append((organization_id, user_id))
        return self.client_instance


def _context(*project_ids: str) -> AssistantWorkspaceContext:
    return AssistantWorkspaceContext(
        projects=tuple(
            AssistantProjectContext(
                project_id=project_id,
                customer_name=project_id.split(".")[0].upper(),
                official_domain=project_id,
                project_notes="",
                revision=1,
                effective_role="owner",
                tasks=(),
                prompts=(),
                knowledge=(),
            )
            for project_id in project_ids
        )
    )


class FakeAssistantRepository:
    def __init__(self) -> None:
        self.messages: list[AssistantMessage] = []
        self.conversation = AssistantConversation(
            organization_id="org-a",
            conversation_id="conversation-1",
            creator_user_id="user-a",
            title="Assistant conversation",
            project_ids=("soropower.com",),
        )

    def get_conversation(self, *, actor, conversation_id, include_messages=True):
        del actor, include_messages
        if conversation_id != self.conversation.conversation_id:
            raise AssertionError("unexpected conversation")
        return replace(self.conversation, messages=tuple(self.messages))

    def get_message_by_idempotency(
        self,
        *,
        actor,
        conversation_id,
        idempotency_key,
    ):
        del actor, conversation_id
        return next(
            (
                message
                for message in self.messages
                if message.idempotency_key == idempotency_key
            ),
            None,
        )

    def append_message(
        self,
        *,
        actor,
        conversation_id,
        role,
        content,
        request_id,
        idempotency_key,
    ):
        del actor, conversation_id
        message = AssistantMessage(
            message_id=f"message-{len(self.messages) + 1}",
            sequence=len(self.messages) + 1,
            role=role,
            content=content,
            request_id=request_id,
            idempotency_key=idempotency_key,
        )
        self.messages.append(message)
        return message

    def get_plan_by_idempotency(self, **kwargs):
        del kwargs
        return None


class FakeContextResolver:
    def resolve(self, **kwargs):
        del kwargs
        return _context("soropower.com")


class WorkflowAssistantMessageRouterTests(unittest.TestCase):
    actor = ActorIdentity("org-a", "user-a")

    def test_identity_question_is_chat_without_model_call(self) -> None:
        client = FakeClient([])
        router = AssistantMessageRouter(FakeFactory(client))

        intent = router.route(
            actor=self.actor,
            request="你是谁？",
            context=_context("soropower.com"),
        )

        self.assertEqual(intent.kind, "chat")
        self.assertEqual(client.calls, [])
        self.assertIn("Article Agent", router.chat_reply(actor=self.actor, request="你是谁？"))

    def test_explicit_write_request_is_always_workflow(self) -> None:
        client = FakeClient([])
        router = AssistantMessageRouter(FakeFactory(client))

        intent = router.route(
            actor=self.actor,
            request="帮我写一篇文章",
            context=_context("soropower.com"),
        )

        self.assertEqual(intent.kind, "workflow")
        self.assertEqual(client.calls, [])

    def test_short_follow_up_uses_active_plan_context(self) -> None:
        client = FakeClient([])
        router = AssistantMessageRouter(FakeFactory(client))

        intent = router.route(
            actor=self.actor,
            request="继续",
            context=_context("soropower.com"),
            has_active_plan=True,
        )

        self.assertEqual(intent.kind, "workflow")
        self.assertEqual(client.calls, [])

    def test_short_follow_up_without_active_plan_can_stay_chat(self) -> None:
        client = FakeClient([json.dumps({"kind": "chat", "project_id": None})])
        router = AssistantMessageRouter(FakeFactory(client))

        intent = router.route(
            actor=self.actor,
            request="继续",
            context=_context("soropower.com"),
        )

        self.assertEqual(intent.kind, "chat")

    def test_knowledge_fallback_uses_only_selected_project(self) -> None:
        router = AssistantMessageRouter(None)

        intent = router.route(
            actor=self.actor,
            request="知识库里有哪些产品参数？",
            context=_context("soropower.com"),
        )

        self.assertEqual(intent.kind, "knowledge_qa")
        self.assertEqual(intent.project_id, "soropower.com")

    def test_model_routes_knowledge_question_to_exact_project(self) -> None:
        client = FakeClient(
            [
                json.dumps(
                    {"kind": "knowledge_qa", "project_id": "yehuinm.com"}
                )
            ]
        )
        factory = FakeFactory(client)
        router = AssistantMessageRouter(factory)

        intent = router.route(
            actor=self.actor,
            request="YEHUI 的工程木地板适合哪些项目？",
            context=_context("soropower.com", "yehuinm.com"),
        )

        self.assertEqual(intent.kind, "knowledge_qa")
        self.assertEqual(intent.project_id, "yehuinm.com")
        self.assertEqual(factory.identities, [("org-a", "user-a")])

    def test_invalid_classifier_output_fails_safe_to_chat(self) -> None:
        router = AssistantMessageRouter(FakeFactory(FakeClient(["not-json"])))

        intent = router.route(
            actor=self.actor,
            request="这个功能是什么意思？",
            context=_context("soropower.com"),
        )

        self.assertEqual(intent.kind, "chat")

    def test_general_chat_uses_actor_scoped_model(self) -> None:
        client = FakeClient(["这个按钮用于确认计划后开始执行。"])
        factory = FakeFactory(client)
        router = AssistantMessageRouter(factory)

        answer = router.chat_reply(
            actor=self.actor,
            request="这个确认按钮有什么作用？",
            recent_messages=({"role": "user", "content": "你好"},),
        )

        self.assertEqual(answer, "这个按钮用于确认计划后开始执行。")
        self.assertEqual(factory.identities, [("org-a", "user-a")])

    def test_knowledge_answer_persists_deduplicated_sources(self) -> None:
        citation = ResearchCitation(
            chunk_id="chunk-1",
            source_id="source-1",
            snapshot_id="snapshot-1",
            display_name="Product Manual",
            canonical_url="https://example.com/manual",
            text="A factual paragraph.",
            ordinal=1,
            locator={"page_number": 2},
        )
        message = ResearchMessage(
            message_id="message-1",
            request_id="request-1",
            sequence=2,
            role="assistant",
            content="The rated range is 6–12 kW.",
            citations=(citation, citation),
        )

        rendered = render_knowledge_answer(message)

        self.assertIn("The rated range is 6–12 kW.", rendered)
        self.assertIn("资料依据：", rendered)
        self.assertEqual(rendered.count("https://example.com/manual"), 1)

    def test_knowledge_reply_isolates_project_and_article_conversations(self) -> None:
        calls: list[dict[str, object]] = []

        class FakeResearchChat:
            def ask(self, **kwargs):
                calls.append(kwargs)
                return ResearchConversation(
                    project_id=str(kwargs["project_id"]),
                    conversation_id=str(kwargs["conversation_id"]),
                    article_id=str(kwargs["article_id"]) if kwargs["article_id"] else None,
                    messages=(
                        ResearchMessage(
                            message_id="answer-1",
                            request_id=str(kwargs["request_id"]),
                            sequence=2,
                            role="assistant",
                            content="Supported answer.",
                        ),
                    ),
                )

        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    knowledge_agent_runtime=SimpleNamespace(
                        research_chat=FakeResearchChat()
                    )
                )
            )
        )

        for article_id in (None, "task-1"):
            _knowledge_reply(
                request,
                project_id="soropower.com",
                question="What is the rated range?",
                conversation_id="assistant-conversation-1",
                request_id=f"request-{article_id or 'project'}",
                article_id=article_id,
            )

        self.assertNotEqual(calls[0]["conversation_id"], calls[1]["conversation_id"])
        self.assertTrue(str(calls[0]["conversation_id"]).startswith("assistant_chat_"))
        self.assertLessEqual(len(str(calls[0]["conversation_id"])), 200)

    def test_message_endpoint_returns_planless_chat_reply(self) -> None:
        repository = FakeAssistantRepository()
        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    article_agent_config=SimpleNamespace(
                        workflow_assistant_enabled=True
                    ),
                    server_llm_client_factory=None,
                )
            )
        )
        payload = AssistantMessageRequest(
            content="你是谁？",
            request_id="request-chat-1",
            idempotency_key="message-chat-1",
        )

        with (
            patch("workflow_assistant.http._repository", return_value=repository),
            patch(
                "workflow_assistant.http._context",
                return_value=FakeContextResolver(),
            ),
        ):
            response = append_message(
                "conversation-1",
                payload,
                request,
                actor=self.actor,
            )

        self.assertIsNone(response.plan)
        self.assertIn("Article Agent", response.message.content)
        self.assertEqual([message.role for message in repository.messages], ["user", "assistant"])

    def test_message_endpoint_returns_planless_knowledge_reply(self) -> None:
        repository = FakeAssistantRepository()

        class FakeResearchChat:
            def ask(self, **kwargs):
                return ResearchConversation(
                    project_id=str(kwargs["project_id"]),
                    conversation_id=str(kwargs["conversation_id"]),
                    article_id=None,
                    messages=(
                        ResearchMessage(
                            message_id="answer-1",
                            request_id=str(kwargs["request_id"]),
                            sequence=2,
                            role="assistant",
                            content="The product supports a 6–12 kW rated range.",
                        ),
                    ),
                )

        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    article_agent_config=SimpleNamespace(
                        workflow_assistant_enabled=True
                    ),
                    server_llm_client_factory=None,
                    knowledge_agent_runtime=SimpleNamespace(
                        research_chat=FakeResearchChat()
                    ),
                )
            )
        )
        payload = AssistantMessageRequest(
            content="知识库里有哪些产品参数？",
            request_id="request-knowledge-1",
            idempotency_key="message-knowledge-1",
        )

        with (
            patch("workflow_assistant.http._repository", return_value=repository),
            patch(
                "workflow_assistant.http._context",
                return_value=FakeContextResolver(),
            ),
        ):
            response = append_message(
                "conversation-1",
                payload,
                request,
                actor=self.actor,
            )

        self.assertIsNone(response.plan)
        self.assertIn("6–12 kW", response.message.content)
        self.assertEqual([message.role for message in repository.messages], ["user", "assistant"])


if __name__ == "__main__":
    unittest.main()
