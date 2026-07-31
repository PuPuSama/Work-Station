"""Add optimistic revision to authoritative Project metadata.

Revision ID: 20260731_0018
Revises: 20260731_0017
Create Date: 2026-07-31
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260731_0018"
down_revision: str | Sequence[str] | None = "20260731_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column(
            "revision",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.create_check_constraint(
        "ck_projects_revision_nonnegative",
        "projects",
        "revision >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_projects_revision_nonnegative",
        "projects",
        type_="check",
    )
    op.drop_column("projects", "revision")
