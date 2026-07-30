"""Add the M3 full-text expression index used by hybrid retrieval.

Revision ID: 20260730_0004
Revises: 20260730_0003
Create Date: 2026-07-30
"""

from collections.abc import Sequence

from alembic import op


revision: str = "20260730_0004"
down_revision: str | Sequence[str] | None = "20260730_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX ix_knowledge_chunks_search_text
        ON knowledge_chunks
        USING gin (
            to_tsvector('simple'::regconfig, text)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX ix_knowledge_chunks_search_text")
