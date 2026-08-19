"""Persist the initial organization-scoped model settings.

Revision ID: 20260819_0026
Revises: 20260819_0025
Create Date: 2026-08-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260819_0026"
down_revision: str | Sequence[str] | None = "20260819_0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "organization_llm_settings",
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("reasoning_effort", sa.Text(), nullable=False),
        sa.Column(
            "revision",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "btrim(model) <> '' AND btrim(reasoning_effort) <> ''",
            name="ck_organization_llm_settings_values_nonempty",
        ),
        sa.CheckConstraint(
            "revision >= 0",
            name="ck_organization_llm_settings_revision_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.organization_id"],
            name="fk_organization_llm_settings_organization",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "organization_id",
            name="pk_organization_llm_settings",
        ),
    )


def downgrade() -> None:
    op.drop_table("organization_llm_settings")
