"""Persist natural-language planning requests outside the HTTP request.

Revision ID: 20260826_0033
Revises: 20260820_0032
Create Date: 2026-08-26
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260826_0033"
down_revision = "20260820_0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workflow_assistant_dispatches",
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("dispatch_id", sa.Text(), nullable=False),
        sa.Column("creator_user_id", sa.Text(), nullable=False),
        sa.Column("conversation_id", sa.Text(), nullable=False),
        sa.Column("request_id", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "project_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "article_task_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "article_task_selection_locked",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'queued'"),
        ),
        sa.Column("plan_id", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default=sa.text("3")),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("worker_id", sa.Text(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "btrim(dispatch_id) <> '' AND btrim(creator_user_id) <> '' "
            "AND btrim(conversation_id) <> '' AND btrim(request_id) <> '' "
            "AND btrim(idempotency_key) <> '' AND btrim(content) <> ''",
            name="ck_workflow_assistant_dispatches_identity_nonempty",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(project_ids) = 'array' "
            "AND jsonb_typeof(article_task_ids) = 'array'",
            name="ck_workflow_assistant_dispatches_scope_arrays",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name="ck_workflow_assistant_dispatches_status",
        ),
        sa.CheckConstraint(
            "attempts >= 0 AND max_attempts > 0",
            name="ck_workflow_assistant_dispatches_attempts",
        ),
        sa.CheckConstraint(
            "(status = 'running' AND worker_id IS NOT NULL "
            "AND lease_expires_at IS NOT NULL) OR "
            "(status <> 'running' AND worker_id IS NULL "
            "AND lease_expires_at IS NULL)",
            name="ck_workflow_assistant_dispatches_lease_state",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "creator_user_id"],
            ["workspace_users.organization_id", "workspace_users.user_id"],
            name="fk_workflow_assistant_dispatches_creator",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "conversation_id"],
            [
                "assistant_conversations.organization_id",
                "assistant_conversations.conversation_id",
            ],
            name="fk_workflow_assistant_dispatches_conversation",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "plan_id"],
            ["workflow_plans.organization_id", "workflow_plans.plan_id"],
            name="fk_workflow_assistant_dispatches_plan",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint(
            "organization_id",
            "dispatch_id",
            name="pk_workflow_assistant_dispatches",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "conversation_id",
            "idempotency_key",
            name="uq_workflow_assistant_dispatches_idempotency",
        ),
    )
    op.create_index(
        "ix_workflow_assistant_dispatches_runnable",
        "workflow_assistant_dispatches",
        ["status", "available_at", "created_at"],
    )
    op.create_index(
        "ix_workflow_assistant_dispatches_creator",
        "workflow_assistant_dispatches",
        ["organization_id", "creator_user_id", "updated_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_workflow_assistant_dispatches_creator",
        table_name="workflow_assistant_dispatches",
    )
    op.drop_index(
        "ix_workflow_assistant_dispatches_runnable",
        table_name="workflow_assistant_dispatches",
    )
    op.drop_table("workflow_assistant_dispatches")
