from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol
from uuid import uuid4

from services.llm import LLMClient

from .contracts import RetrievalHit, RetrievalQuery
from .hybrid_retriever import BasicHybridRetriever
from .research_chat_repository import (
    PostgresResearchChatRepository,
    ResearchConversation,
    ResearchMessage,
)


RESEARCH_CHAT_RETENTION_DAYS = 30


class ResearchChatError(RuntimeError):
    """Safe public error raised by read-only research chat."""


class ResearchCitationValidationError(ResearchChatError):
    """Raised when a provider cites evidence it was not supplied."""


class ResearchAnswerProviderError(ResearchChatError):
    """Raised without exposing provider exception text or credentials."""


@dataclass(frozen=True, slots=True)
class ResearchAnswer:
    text: str
    cited_chunk_ids: tuple[str, ...]


class ResearchAnswerProvider(Protocol):
    def answer(
        self,
        *,
        question: str,
        evidence_hits: Sequence[RetrievalHit],
        recent_messages: Sequence[ResearchMessage],
    ) -> ResearchAnswer: ...


class LlmResearchAnswerProvider:
    """Bound an existing generation model to supplied chunk IDs and JSON output."""

    def __init__(self, client: LLMClient) -> None:
        self._client = client

    @property
    def ready(self) -> bool:
        return self._client.ready

    def answer(
        self,
        *,
        question: str,
        evidence_hits: Sequence[RetrievalHit],
        recent_messages: Sequence[ResearchMessage],
    ) -> ResearchAnswer:
        evidence = "\n\n".join(
            f"[{hit.chunk.chunk_id}]\n{hit.chunk.text}"
            for hit in evidence_hits
        )
        history = "\n".join(
            f"{message.role}: {message.content}"
            for message in recent_messages[-6:]
        )
        prompt = (
            "Answer only from EVIDENCE. If the evidence is insufficient, say so. "
            "Return strict JSON with keys answer and citations. citations must be "
            "an array containing only exact chunk IDs shown below.\n\n"
            f"RECENT MESSAGES\n{history or '(none)'}\n\n"
            f"EVIDENCE\n{evidence}\n\nQUESTION\n{question}"
        )
        try:
            raw = self._client.chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "You are a read-only research assistant. Never invent "
                            "facts, URLs, sources, or citation IDs."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0,
                max_tokens=1200,
            )
            payload = _parse_answer_json(raw)
        except Exception as exc:
            raise ResearchAnswerProviderError(
                "Research answer generation failed. Inspect restricted server logs."
            ) from exc
        return ResearchAnswer(
            text=str(payload["answer"]).strip(),
            cited_chunk_ids=tuple(
                str(item).strip()
                for item in payload["citations"]
                if str(item).strip()
            ),
        )


class ResearchChatService:
    """Retrieve published project knowledge, validate citations, then persist."""

    def __init__(
        self,
        *,
        retriever: BasicHybridRetriever,
        provider: ResearchAnswerProvider,
        conversations: PostgresResearchChatRepository,
    ) -> None:
        self._retriever = retriever
        self._provider = provider
        self._conversations = conversations

    def ask(
        self,
        *,
        project_id: str,
        question: str,
        request_id: str,
        conversation_id: str | None = None,
        article_id: str | None = None,
        limit: int = 8,
    ) -> ResearchConversation:
        question = question.strip()
        request_id = request_id.strip()
        if not question:
            raise ValueError("question is required")
        if not request_id:
            raise ValueError("request_id is required")
        conversation_id = (
            conversation_id.strip()
            if conversation_id and conversation_id.strip()
            else f"chat_{uuid4().hex}"
        )
        existing = self._conversations.get_conversation(
            project_id,
            conversation_id,
        )
        if existing is not None:
            if existing.article_id != article_id:
                raise ResearchCitationValidationError(
                    "conversation identity already belongs to another article"
                )
            prior = [
                message
                for message in existing.messages
                if message.request_id == request_id
            ]
            if prior:
                if prior[0].content != question:
                    raise ResearchCitationValidationError(
                        "request identity already belongs to another question"
                    )
                return existing
        hits = self._retriever.retrieve(
            RetrievalQuery(
                project_id=project_id,
                text=question,
                limit=limit,
            )
        )
        recent = () if existing is None else existing.messages[-6:]
        if hits:
            answer = self._provider.answer(
                question=question,
                evidence_hits=hits,
                recent_messages=recent,
            )
        else:
            answer = ResearchAnswer(
                text="当前项目已发布的资料中没有找到足够证据。",
                cited_chunk_ids=(),
            )
        allowed_ids = {hit.chunk.chunk_id for hit in hits}
        unknown = sorted(set(answer.cited_chunk_ids) - allowed_ids)
        if unknown:
            raise ResearchCitationValidationError(
                "answer cited chunks outside the supplied evidence set"
            )
        if not answer.text.strip():
            raise ResearchCitationValidationError("answer must not be empty")
        return self._conversations.save_exchange(
            project_id=project_id,
            conversation_id=conversation_id,
            article_id=article_id,
            request_id=request_id,
            question=question,
            answer=answer.text,
            cited_chunk_ids=answer.cited_chunk_ids,
            expires_at=datetime.now(timezone.utc)
            + timedelta(days=RESEARCH_CHAT_RETENTION_DAYS),
        )


def _parse_answer_json(raw: str) -> dict[str, object]:
    value = raw.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", value, re.DOTALL)
    if fenced:
        value = fenced.group(1)
    payload = json.loads(value)
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("answer"), str)
        or not isinstance(payload.get("citations"), list)
        or any(not isinstance(item, str) for item in payload["citations"])
    ):
        raise ValueError("research answer must contain answer and citations")
    return payload
