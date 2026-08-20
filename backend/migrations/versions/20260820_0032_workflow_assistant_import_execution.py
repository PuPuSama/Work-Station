"""Persist Workflow Assistant import execution ownership."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260820_0032"
down_revision = "20260820_0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "assistant_import_proposals",
        sa.Column("execution_idempotency_key", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("assistant_import_proposals", "execution_idempotency_key")
