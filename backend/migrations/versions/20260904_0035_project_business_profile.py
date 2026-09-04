"""Add a project-level company and business background field."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260904_0035"
down_revision = "20260901_0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column(
            "project_business_profile",
            sa.Text(),
            nullable=False,
            server_default=sa.text("''"),
        ),
    )


def downgrade() -> None:
    op.drop_column("projects", "project_business_profile")
