"""Store local workspace-user password hashes."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260908_0036"
down_revision = "20260904_0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workspace_users",
        sa.Column("password_hash", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("workspace_users", "password_hash")
