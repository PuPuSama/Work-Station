from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping
from sqlalchemy.exc import IntegrityError

from .schema import (
    knowledge_chunks,
    knowledge_sources,
    research_conversations,
    research_message_citations,
    research_messages,
)


class ResearchChatRepositoryError(RuntimeError):
    """Base error for project-scoped research conversation persistence."""


class ResearchChatConflictError(ResearchChatRepositoryError):
    """Raised when an idempotency identity is reused with different content."""


@dataclass(frozen=True, slots=True)
class ResearchCitation:
    chunk_id: str
    source_id: str
    snapshot_id: str
    display_name: str
    canonical_url: str | None
    text: str
    ordinal: int
    locator: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ResearchMessage:
    message_id: str
    request_id: str
    sequence: int
    role: str
    content: str
    citations: tuple[ResearchCitation, ...] = ()
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ResearchConversation:
    project_id: str
    conversation_id: str
    article_id: str | None
    messages: tuple[ResearchMessage, ...] = ()
    created_at: datetime | None = None
    updated_at: datetime | None = None
    expires_at: datetime | None = None


class PostgresResearchChatRepository:
    """Persist only final exchanges and chunk identities, never provider prompts."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def get_conversation(
        self,
        project_id: str,
        conversation_id: str,
    ) -> ResearchConversation | None:
        with self._engine.connect() as connection:
            return self._get(connection, project_id, conversation_id)

    def save_exchange(
        self,
        *,
        project_id: str,
        conversation_id: str,
        article_id: str | None,
        request_id: str,
        question: str,
        answer: str,
        cited_chunk_ids: Sequence[str],
        expires_at: datetime,
    ) -> ResearchConversation:
        if expires_at.tzinfo is None or expires_at.utcoffset() is None:
            raise ValueError("expires_at must be timezone-aware")
        try:
            with self._engine.begin() as connection:
                existing = self._get(connection, project_id, conversation_id)
                if existing is None:
                    connection.execute(
                        research_conversations.insert().values(
                            project_id=project_id,
                            conversation_id=conversation_id,
                            article_id=article_id,
                            expires_at=expires_at,
                        )
                    )
                    next_sequence = 1
                else:
                    if existing.article_id != article_id:
                        raise ResearchChatConflictError(
                            "conversation identity already belongs to another article"
                        )
                    prior = [
                        message
                        for message in existing.messages
                        if message.request_id == request_id
                    ]
                    if prior:
                        if (
                            len(prior) != 2
                            or prior[0].content != question
                            or prior[1].content != answer
                            or tuple(
                                citation.chunk_id
                                for citation in prior[1].citations
                            )
                            != tuple(cited_chunk_ids)
                        ):
                            raise ResearchChatConflictError(
                                "request identity already has different content"
                            )
                        return existing
                    next_sequence = (
                        max(
                            (message.sequence for message in existing.messages),
                            default=0,
                        )
                        + 1
                    )
                user_message_id = f"msg-{request_id}-user"
                assistant_message_id = f"msg-{request_id}-assistant"
                connection.execute(
                    research_messages.insert(),
                    [
                        {
                            "project_id": project_id,
                            "conversation_id": conversation_id,
                            "message_id": user_message_id,
                            "request_id": request_id,
                            "sequence": next_sequence,
                            "role": "user",
                            "content": question,
                            "expires_at": expires_at,
                        },
                        {
                            "project_id": project_id,
                            "conversation_id": conversation_id,
                            "message_id": assistant_message_id,
                            "request_id": request_id,
                            "sequence": next_sequence + 1,
                            "role": "assistant",
                            "content": answer,
                            "expires_at": expires_at,
                        },
                    ],
                )
                if cited_chunk_ids:
                    connection.execute(
                        research_message_citations.insert(),
                        [
                            {
                                "project_id": project_id,
                                "conversation_id": conversation_id,
                                "message_id": assistant_message_id,
                                "chunk_id": chunk_id,
                                "ordinal": ordinal,
                            }
                            for ordinal, chunk_id in enumerate(
                                dict.fromkeys(cited_chunk_ids),
                                start=1,
                            )
                        ],
                    )
                connection.execute(
                    research_conversations.update()
                    .where(
                        research_conversations.c.project_id == project_id,
                        research_conversations.c.conversation_id
                        == conversation_id,
                    )
                    .values(updated_at=sa.func.now(), expires_at=expires_at)
                )
                persisted = self._get(connection, project_id, conversation_id)
                if persisted is None:
                    raise ResearchChatRepositoryError(
                        "conversation disappeared during exchange persistence"
                    )
                return persisted
        except IntegrityError as exc:
            raise ResearchChatConflictError(
                "research exchange violates project, message, or chunk identity"
            ) from exc

    def prune_expired(self, *, before: datetime) -> int:
        """Delete expired conversation details while preserving research runs."""

        if before.tzinfo is None or before.utcoffset() is None:
            raise ValueError("before must be timezone-aware")
        with self._engine.begin() as connection:
            result = connection.execute(
                research_conversations.delete().where(
                    research_conversations.c.expires_at < before
                )
            )
            return int(result.rowcount or 0)

    @staticmethod
    def _get(
        connection: sa.Connection,
        project_id: str,
        conversation_id: str,
    ) -> ResearchConversation | None:
        row = connection.execute(
            sa.select(research_conversations).where(
                research_conversations.c.project_id == project_id,
                research_conversations.c.conversation_id == conversation_id,
            )
        ).mappings().one_or_none()
        if row is None:
            return None
        message_rows = connection.execute(
            sa.select(research_messages)
            .where(
                research_messages.c.project_id == project_id,
                research_messages.c.conversation_id == conversation_id,
            )
            .order_by(research_messages.c.sequence)
        ).mappings().all()
        citations_by_message: dict[str, list[ResearchCitation]] = {}
        citation_rows = connection.execute(
            sa.select(
                research_message_citations.c.message_id,
                research_message_citations.c.chunk_id,
                research_message_citations.c.ordinal,
                knowledge_chunks.c.source_id,
                knowledge_chunks.c.snapshot_id,
                knowledge_chunks.c.text,
                knowledge_chunks.c.locator,
                knowledge_sources.c.display_name,
                knowledge_sources.c.canonical_url,
            )
            .select_from(
                research_message_citations.join(
                    knowledge_chunks,
                    sa.and_(
                        knowledge_chunks.c.project_id
                        == research_message_citations.c.project_id,
                        knowledge_chunks.c.chunk_id
                        == research_message_citations.c.chunk_id,
                    ),
                ).join(
                    knowledge_sources,
                    sa.and_(
                        knowledge_sources.c.project_id
                        == knowledge_chunks.c.project_id,
                        knowledge_sources.c.source_id
                        == knowledge_chunks.c.source_id,
                    ),
                )
            )
            .where(
                research_message_citations.c.project_id == project_id,
                research_message_citations.c.conversation_id == conversation_id,
            )
            .order_by(
                research_message_citations.c.message_id,
                research_message_citations.c.ordinal,
            )
        ).mappings()
        for citation in citation_rows:
            citations_by_message.setdefault(
                str(citation["message_id"]),
                [],
            ).append(_citation_from_row(citation))
        return ResearchConversation(
            project_id=str(row["project_id"]),
            conversation_id=str(row["conversation_id"]),
            article_id=(
                str(row["article_id"]) if row["article_id"] is not None else None
            ),
            messages=tuple(
                ResearchMessage(
                    message_id=str(message["message_id"]),
                    request_id=str(message["request_id"]),
                    sequence=int(message["sequence"]),
                    role=str(message["role"]),
                    content=str(message["content"]),
                    citations=tuple(
                        citations_by_message.get(str(message["message_id"]), ())
                    ),
                    created_at=message["created_at"],  # type: ignore[arg-type]
                )
                for message in message_rows
            ),
            created_at=row["created_at"],  # type: ignore[arg-type]
            updated_at=row["updated_at"],  # type: ignore[arg-type]
            expires_at=row["expires_at"],  # type: ignore[arg-type]
        )


def _citation_from_row(row: RowMapping) -> ResearchCitation:
    return ResearchCitation(
        chunk_id=str(row["chunk_id"]),
        source_id=str(row["source_id"]),
        snapshot_id=str(row["snapshot_id"]),
        display_name=str(row["display_name"]),
        canonical_url=(
            str(row["canonical_url"]) if row["canonical_url"] is not None else None
        ),
        text=str(row["text"]),
        ordinal=int(row["ordinal"]),
        locator=dict(row["locator"] or {}),
    )
