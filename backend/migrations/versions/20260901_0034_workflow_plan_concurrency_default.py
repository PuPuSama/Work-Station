"""Raise the default workflow plan concurrency to five."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260901_0034"
down_revision = "20260826_0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "workflow_plans",
        "concurrency_limit",
        existing_type=sa.Integer(),
        existing_nullable=False,
        server_default=sa.text("5"),
    )


def downgrade() -> None:
    op.alter_column(
        "workflow_plans",
        "concurrency_limit",
        existing_type=sa.Integer(),
        existing_nullable=False,
        server_default=sa.text("3"),
    )
