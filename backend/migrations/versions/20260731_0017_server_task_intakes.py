"""Add idempotent Server Task intake receipts.

Revision ID: 20260731_0017
Revises: 20260731_0016
Create Date: 2026-07-31
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260731_0017"
down_revision: str | Sequence[str] | None = "20260731_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_intakes",
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("intake_id", sa.Text(), nullable=False),
        sa.Column("intake_kind", sa.Text(), nullable=False),
        sa.Column("source_name", sa.Text(), nullable=False),
        sa.Column("payload_digest", sa.Text(), nullable=False),
        sa.Column("task_count", sa.Integer(), nullable=False),
        sa.Column(
            "task_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("created_by_user_id", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "btrim(intake_id) <> '' AND btrim(source_name) <> '' "
            "AND btrim(created_by_user_id) <> ''",
            name="ck_task_intakes_identity_nonempty",
        ),
        sa.CheckConstraint(
            "intake_kind IN ('manual', 'row_import')",
            name="ck_task_intakes_kind",
        ),
        sa.CheckConstraint(
            "payload_digest ~ '^[0-9a-f]{64}$'",
            name="ck_task_intakes_payload_digest",
        ),
        sa.CheckConstraint(
            "task_count > 0",
            name="ck_task_intakes_task_count",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(task_ids) = 'array' "
            "AND jsonb_array_length(task_ids) = task_count",
            name="ck_task_intakes_task_ids",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "project_id"],
            [
                "project_ownership.organization_id",
                "project_ownership.project_id",
            ],
            name="fk_task_intakes_project",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "created_by_user_id"],
            [
                "workspace_users.organization_id",
                "workspace_users.user_id",
            ],
            name="fk_task_intakes_creator",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "organization_id",
            "project_id",
            "intake_id",
            name="pk_task_intakes",
        ),
    )
    op.create_index(
        "ix_task_intakes_project_created",
        "task_intakes",
        [
            "organization_id",
            "project_id",
            "created_at",
            "intake_id",
        ],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_task_intakes_project_created",
        table_name="task_intakes",
    )
    op.drop_table("task_intakes")
