"""Add a revocable version to every server Actor session.

Revision ID: 20260731_0013
Revises: 20260730_0012
Create Date: 2026-07-31
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260731_0013"
down_revision: str | Sequence[str] | None = "20260730_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "workspace_users",
        sa.Column(
            "session_version",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )
    op.create_check_constraint(
        "ck_workspace_users_session_version",
        "workspace_users",
        "session_version > 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_workspace_users_session_version",
        "workspace_users",
        type_="check",
    )
    op.drop_column("workspace_users", "session_version")
