from __future__ import annotations

import hashlib
import os
import sys
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sqlalchemy as sa


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from knowledge_agent.contracts import (  # noqa: E402
    KnowledgeChunk,
    KnowledgeProject,
    KnowledgeSource,
    SourceSnapshot,
)
from knowledge_agent.database import create_knowledge_engine  # noqa: E402
from knowledge_agent.repository import PostgresKnowledgeRepository  # noqa: E402
from knowledge_agent.research_chat_repository import (  # noqa: E402
    PostgresResearchChatRepository,
    ResearchChatConflictError,
)
from knowledge_agent.schema import (  # noqa: E402
    knowledge_chunks,
    knowledge_sources,
    projects,
    research_conversations,
    source_snapshots,
)


DATABASE_URL_ENV = "ARTICLE_AGENT_DATABASE_URL"
NOW = datetime(2026, 7, 30, tzinfo=timezone.utc)


@unittest.skipUnless(
    os.environ.get(DATABASE_URL_ENV),
    f"{DATABASE_URL_ENV} is required for PostgreSQL integration tests",
)
class KnowledgeAgentM5ResearchChatPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = create_knowledge_engine(os.environ[DATABASE_URL_ENV])
        cls.knowledge = PostgresKnowledgeRepository(cls.engine)
        cls.chats = PostgresResearchChatRepository(cls.engine)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    def setUp(self) -> None:
        self.prefix = f"m5-chat-{uuid.uuid4().hex}"
        self.project_ids: set[str] = set()

    def tearDown(self) -> None:
        if not self.project_ids:
            return
        project_ids = tuple(self.project_ids)
        with self.engine.begin() as connection:
            connection.execute(
                research_conversations.delete().where(
                    research_conversations.c.project_id.in_(project_ids)
                )
            )
            connection.execute(
                knowledge_chunks.delete().where(
                    knowledge_chunks.c.project_id.in_(project_ids)
                )
            )
            connection.execute(
                source_snapshots.delete().where(
                    source_snapshots.c.project_id.in_(project_ids)
                )
            )
            connection.execute(
                knowledge_sources.delete().where(
                    knowledge_sources.c.project_id.in_(project_ids)
                )
            )
            connection.execute(
                projects.delete().where(projects.c.project_id.in_(project_ids))
            )

    def _chunk(self, suffix: str) -> tuple[str, str]:
        project_id = f"{self.prefix}-{suffix}"
        source_id = f"source-{suffix}"
        snapshot_id = f"snapshot-{suffix}"
        chunk_id = f"{snapshot_id}:0"
        self.project_ids.add(project_id)
        self.knowledge.upsert_project(
            KnowledgeProject(
                project_id=project_id,
                customer_name=f"Project {suffix}",
                official_domain=f"{suffix}.example.test",
            )
        )
        self.knowledge.upsert_source(
            KnowledgeSource(
                project_id=project_id,
                source_id=source_id,
                display_name=f"Source {suffix}",
                source_kind="knowledge_page",
                trust_tier="hard_fact",
                public_source=True,
                canonical_url=f"https://{suffix}.example.test/page",
            )
        )
        snapshot = SourceSnapshot(
            project_id=project_id,
            source_id=source_id,
            snapshot_id=snapshot_id,
            content_hash=hashlib.sha256(snapshot_id.encode()).hexdigest(),
            fetched_at=NOW,
            parser_name="m5-test",
            parser_version="1",
        )
        self.knowledge.store_snapshot(
            project_id,
            snapshot,
            (
                KnowledgeChunk(
                    project_id=project_id,
                    chunk_id=chunk_id,
                    source_id=source_id,
                    snapshot_id=snapshot_id,
                    text=f"Evidence {suffix}",
                ),
            ),
        )
        return project_id, chunk_id

    def test_exchange_is_ordered_idempotent_and_project_scoped(self) -> None:
        project_id, chunk_id = self._chunk("a")
        _other_project, other_chunk = self._chunk("b")
        expires = NOW + timedelta(days=30)

        conversation = self.chats.save_exchange(
            project_id=project_id,
            conversation_id=f"{self.prefix}-conversation",
            article_id="topic_006",
            request_id="request-1",
            question="What evidence is available?",
            answer="The source contains evidence.",
            cited_chunk_ids=(chunk_id,),
            expires_at=expires,
        )
        retried = self.chats.save_exchange(
            project_id=project_id,
            conversation_id=conversation.conversation_id,
            article_id="topic_006",
            request_id="request-1",
            question="What evidence is available?",
            answer="The source contains evidence.",
            cited_chunk_ids=(chunk_id,),
            expires_at=expires,
        )

        self.assertEqual(
            [message.role for message in retried.messages],
            ["user", "assistant"],
        )
        self.assertEqual(
            retried.messages[1].citations[0].chunk_id,
            chunk_id,
        )
        with self.assertRaises(ResearchChatConflictError):
            self.chats.save_exchange(
                project_id=project_id,
                conversation_id=f"{self.prefix}-cross-project",
                article_id=None,
                request_id="request-cross",
                question="Cross project?",
                answer="No.",
                cited_chunk_ids=(other_chunk,),
                expires_at=expires,
            )
        self.assertIsNone(
            self.chats.get_conversation(
                project_id,
                f"{self.prefix}-cross-project",
            )
        )

    def test_expired_conversation_is_pruned_without_touching_knowledge(self) -> None:
        project_id, chunk_id = self._chunk("expiry")
        conversation_id = f"{self.prefix}-expired"
        self.chats.save_exchange(
            project_id=project_id,
            conversation_id=conversation_id,
            article_id=None,
            request_id="request-expired",
            question="Old question",
            answer="Old answer",
            cited_chunk_ids=(chunk_id,),
            expires_at=NOW - timedelta(seconds=1),
        )

        self.assertEqual(self.chats.prune_expired(before=NOW), 1)
        self.assertIsNone(
            self.chats.get_conversation(project_id, conversation_id)
        )
        with self.engine.connect() as connection:
            self.assertEqual(
                connection.execute(
                    sa.select(sa.func.count())
                    .select_from(knowledge_chunks)
                    .where(knowledge_chunks.c.project_id == project_id)
                ).scalar_one(),
                1,
            )
