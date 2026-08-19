"""Make model settings private to each active workspace user.

Revision ID: 20260819_0027
Revises: 20260819_0026
Create Date: 2026-08-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260819_0027"
down_revision: str | Sequence[str] | None = "20260819_0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _create_user_settings_table() -> None:
    op.create_table(
        "workspace_user_llm_settings",
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
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
            name="ck_workspace_user_llm_settings_values_nonempty",
        ),
        sa.CheckConstraint(
            "revision >= 0",
            name="ck_workspace_user_llm_settings_revision_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["workspace_users.organization_id", "workspace_users.user_id"],
            name="fk_workspace_user_llm_settings_user",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "organization_id",
            "user_id",
            name="pk_workspace_user_llm_settings",
        ),
    )


def upgrade() -> None:
    _create_user_settings_table()
    # Preserve the old organization default for every active member. From
    # this migration onward, each member can change their own row safely.
    op.execute(
        """
        INSERT INTO workspace_user_llm_settings (
            organization_id,
            user_id,
            model,
            reasoning_effort,
            revision,
            updated_at
        )
        SELECT
            setting.organization_id,
            member.user_id,
            setting.model,
            setting.reasoning_effort,
            setting.revision,
            setting.updated_at
        FROM organization_llm_settings AS setting
        JOIN workspace_users AS member
          ON member.organization_id = setting.organization_id
         AND member.status = 'active'
        """
    )
    op.drop_table("organization_llm_settings")


def downgrade() -> None:
    # A downgrade can only restore one organization-level value. Keep the
    # most recently updated personal value as that compatibility fallback.
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
    op.execute(
        """
        INSERT INTO organization_llm_settings (
            organization_id,
            model,
            reasoning_effort,
            revision,
            updated_at
        )
        SELECT DISTINCT ON (organization_id)
            organization_id,
            model,
            reasoning_effort,
            revision,
            updated_at
        FROM workspace_user_llm_settings
        ORDER BY organization_id, updated_at DESC, user_id DESC
        """
    )
    op.drop_table("workspace_user_llm_settings")
