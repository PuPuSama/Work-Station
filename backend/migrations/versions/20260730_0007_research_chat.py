"""Persist read-only research conversations and chunk citations.

Revision ID: 20260730_0007
Revises: 20260730_0006
Create Date: 2026-07-30
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260730_0007"
down_revision: str | Sequence[str] | None = "20260730_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "research_conversations",
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("conversation_id", sa.Text(), nullable=False),
        sa.Column("article_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "btrim(conversation_id) <> ''",
            name="ck_research_conversations_identity_nonempty",
        ),
        sa.CheckConstraint(
            "article_id IS NULL OR btrim(article_id) <> ''",
            name="ck_research_conversations_article_nonempty",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.project_id"],
            name="fk_research_conversations_project",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "project_id",
            "conversation_id",
            name="pk_research_conversations",
        ),
        sa.UniqueConstraint(
            "conversation_id",
            name="uq_research_conversations_conversation_id",
        ),
    )
    op.create_index(
        "ix_research_conversations_expiry",
        "research_conversations",
        ["project_id", "expires_at"],
    )
    op.create_table(
        "research_messages",
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("conversation_id", sa.Text(), nullable=False),
        sa.Column("message_id", sa.Text(), nullable=False),
        sa.Column("request_id", sa.Text(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "btrim(message_id) <> '' AND btrim(request_id) <> '' "
            "AND btrim(content) <> ''",
            name="ck_research_messages_text_nonempty",
        ),
        sa.CheckConstraint(
            "role IN ('user', 'assistant')",
            name="ck_research_messages_role",
        ),
        sa.CheckConstraint(
            "sequence > 0",
            name="ck_research_messages_sequence",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "conversation_id"],
            [
                "research_conversations.project_id",
                "research_conversations.conversation_id",
            ],
            name="fk_research_messages_conversation",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "project_id",
            "conversation_id",
            "message_id",
            name="pk_research_messages",
        ),
        sa.UniqueConstraint(
            "project_id",
            "conversation_id",
            "request_id",
            "role",
            name="uq_research_messages_request_role",
        ),
        sa.UniqueConstraint(
            "project_id",
            "conversation_id",
            "sequence",
            name="uq_research_messages_sequence",
        ),
    )
    op.create_index(
        "ix_research_messages_expiry",
        "research_messages",
        ["project_id", "expires_at"],
    )
    op.create_table(
        "research_message_citations",
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("conversation_id", sa.Text(), nullable=False),
        sa.Column("message_id", sa.Text(), nullable=False),
        sa.Column("chunk_id", sa.Text(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id", "conversation_id", "message_id"],
            [
                "research_messages.project_id",
                "research_messages.conversation_id",
                "research_messages.message_id",
            ],
            name="fk_research_message_citations_message",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "chunk_id"],
            ["knowledge_chunks.project_id", "knowledge_chunks.chunk_id"],
            name="fk_research_message_citations_chunk",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "project_id",
            "conversation_id",
            "message_id",
            "chunk_id",
            name="pk_research_message_citations",
        ),
        sa.UniqueConstraint(
            "project_id",
            "conversation_id",
            "message_id",
            "ordinal",
            name="uq_research_message_citations_ordinal",
        ),
        sa.CheckConstraint(
            "ordinal > 0",
            name="ck_research_message_citations_ordinal",
        ),
    )


def downgrade() -> None:
    op.drop_table("research_message_citations")
    op.drop_index("ix_research_messages_expiry", table_name="research_messages")
    op.drop_table("research_messages")
    op.drop_index(
        "ix_research_conversations_expiry",
        table_name="research_conversations",
    )
    op.drop_table("research_conversations")
