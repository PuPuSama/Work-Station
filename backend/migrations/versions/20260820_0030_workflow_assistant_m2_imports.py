"""Add temporary assistant attachments and import proposals.

Revision ID: 20260820_0030
Revises: 20260820_0029
Create Date: 2026-08-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260820_0030"
down_revision: str | Sequence[str] | None = "20260820_0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_assistant_conversations_creator_scope",
        "assistant_conversations",
        ["organization_id", "conversation_id", "creator_user_id"],
    )
    op.create_unique_constraint(
        "uq_workflow_plans_creator_scope",
        "workflow_plans",
        ["organization_id", "plan_id", "creator_user_id"],
    )
    op.create_table(
        "assistant_attachments",
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("attachment_id", sa.Text(), nullable=False),
        sa.Column("creator_user_id", sa.Text(), nullable=False),
        sa.Column("conversation_id", sa.Text(), nullable=False),
        sa.Column("proposed_project_id", sa.Text(), nullable=True),
        sa.Column("plan_id", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("original_filename", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.Text(), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("classification", sa.Text(), nullable=True),
        sa.Column(
            "classification_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'uploaded'")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "btrim(attachment_id) <> '' AND btrim(creator_user_id) <> '' "
            "AND btrim(idempotency_key) <> '' AND btrim(object_key) <> '' "
            "AND btrim(original_filename) <> '' AND btrim(mime_type) <> ''",
            name="ck_assistant_attachments_identity_nonempty",
        ),
        sa.CheckConstraint("byte_size >= 0 AND revision >= 0", name="ck_assistant_attachments_size_revision"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_assistant_attachments_sha256"),
        sa.CheckConstraint(
            "classification IS NULL OR classification IN "
            "('knowledge_source', 'prompt_asset', 'task_workbook', "
            "'project_notes', 'topic_library', 'unsupported', 'needs_user_choice')",
            name="ck_assistant_attachments_classification",
        ),
        sa.CheckConstraint(
            "status IN ('uploading', 'uploaded', 'classifying', 'needs_user_choice', "
            "'proposal_ready', 'importing', 'imported', 'rejected', 'expired', "
            "'rejecting', 'expiring', 'failed')",
            name="ck_assistant_attachments_status",
        ),
        sa.CheckConstraint("expires_at > created_at", name="ck_assistant_attachments_expiry"),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.organization_id"],
            name="fk_assistant_attachments_organization",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "creator_user_id"],
            ["workspace_users.organization_id", "workspace_users.user_id"],
            name="fk_assistant_attachments_creator",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "conversation_id", "creator_user_id"],
            ["assistant_conversations.organization_id", "assistant_conversations.conversation_id", "assistant_conversations.creator_user_id"],
            name="fk_assistant_attachments_conversation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "proposed_project_id"],
            ["project_ownership.organization_id", "project_ownership.project_id"],
            name="fk_assistant_attachments_proposed_project",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "plan_id", "creator_user_id"],
            ["workflow_plans.organization_id", "workflow_plans.plan_id", "workflow_plans.creator_user_id"],
            name="fk_assistant_attachments_plan",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("organization_id", "attachment_id", name="pk_assistant_attachments"),
        sa.UniqueConstraint(
            "organization_id",
            "creator_user_id",
            "conversation_id",
            "idempotency_key",
            name="uq_assistant_attachments_idempotency",
        ),
        sa.UniqueConstraint(
            "organization_id", "attachment_id", "creator_user_id",
            name="uq_assistant_attachments_creator_scope",
        ),
    )
    op.create_index(
        "ix_assistant_attachments_creator_status",
        "assistant_attachments",
        ["organization_id", "creator_user_id", "status", "updated_at"],
    )
    op.create_index(
        "ix_assistant_attachments_expiry",
        "assistant_attachments",
        ["status", "expires_at"],
    )
    op.create_index(
        "ix_assistant_attachments_project",
        "assistant_attachments",
        ["organization_id", "proposed_project_id", "updated_at"],
    )

    op.create_table(
        "assistant_import_proposals",
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("proposal_id", sa.Text(), nullable=False),
        sa.Column("attachment_id", sa.Text(), nullable=False),
        sa.Column("creator_user_id", sa.Text(), nullable=False),
        sa.Column("target_project_id", sa.Text(), nullable=True),
        sa.Column("plan_id", sa.Text(), nullable=True),
        sa.Column("target_kind", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("normalized_diff", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'draft'")),
        sa.Column("confirmed_by", sa.Text(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resulting_entity_refs", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("standardized_error_code", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "btrim(proposal_id) <> '' AND btrim(attachment_id) <> '' "
            "AND btrim(creator_user_id) <> '' AND btrim(target_kind) <> '' "
            "AND btrim(idempotency_key) <> ''",
            name="ck_assistant_import_proposals_identity_nonempty",
        ),
        sa.CheckConstraint("revision >= 0", name="ck_assistant_import_proposals_revision"),
        sa.CheckConstraint(
            "target_kind IN ('knowledge_source', 'prompt_asset', 'task_workbook', "
            "'project_notes', 'topic_library', 'needs_user_choice')",
            name="ck_assistant_import_proposals_target_kind",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'awaiting_confirmation', 'confirmed', 'running', "
            "'waiting_publication', 'completed', 'failed', 'cancelled')",
            name="ck_assistant_import_proposals_status",
        ),
        sa.CheckConstraint("(confirmed_at IS NULL) = (confirmed_by IS NULL)", name="ck_assistant_import_proposals_confirmation"),
        sa.CheckConstraint(
            "status NOT IN ('confirmed', 'running', 'waiting_publication', "
            "'completed') OR (target_project_id IS NOT NULL "
            "AND target_kind <> 'needs_user_choice' "
            "AND confirmed_by IS NOT NULL AND confirmed_at IS NOT NULL)",
            name="ck_assistant_import_proposals_runnable_target",
        ),
        sa.CheckConstraint(
            "standardized_error_code IS NULL OR btrim(standardized_error_code) <> ''",
            name="ck_assistant_import_proposals_error_nonempty",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.organization_id"],
            name="fk_assistant_import_proposals_organization",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "attachment_id", "creator_user_id"],
            ["assistant_attachments.organization_id", "assistant_attachments.attachment_id", "assistant_attachments.creator_user_id"],
            name="fk_assistant_import_proposals_attachment",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "creator_user_id"],
            ["workspace_users.organization_id", "workspace_users.user_id"],
            name="fk_assistant_import_proposals_creator",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "target_project_id"],
            ["project_ownership.organization_id", "project_ownership.project_id"],
            name="fk_assistant_import_proposals_target_project",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "plan_id", "creator_user_id"],
            ["workflow_plans.organization_id", "workflow_plans.plan_id", "workflow_plans.creator_user_id"],
            name="fk_assistant_import_proposals_plan",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "confirmed_by"],
            ["workspace_users.organization_id", "workspace_users.user_id"],
            name="fk_assistant_import_proposals_confirmer",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("organization_id", "proposal_id", name="pk_assistant_import_proposals"),
        sa.UniqueConstraint(
            "organization_id",
            "attachment_id",
            "idempotency_key",
            name="uq_assistant_import_proposals_idempotency",
        ),
    )
    op.create_index(
        "ix_assistant_import_proposals_creator_status",
        "assistant_import_proposals",
        ["organization_id", "creator_user_id", "status", "updated_at"],
    )
    op.create_index(
        "ix_assistant_import_proposals_attachment",
        "assistant_import_proposals",
        ["organization_id", "attachment_id", "updated_at"],
    )
    op.create_index(
        "ix_assistant_import_proposals_project",
        "assistant_import_proposals",
        ["organization_id", "target_project_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_assistant_import_proposals_project", table_name="assistant_import_proposals")
    op.drop_index("ix_assistant_import_proposals_attachment", table_name="assistant_import_proposals")
    op.drop_index("ix_assistant_import_proposals_creator_status", table_name="assistant_import_proposals")
    op.drop_table("assistant_import_proposals")
    op.drop_index("ix_assistant_attachments_project", table_name="assistant_attachments")
    op.drop_index("ix_assistant_attachments_expiry", table_name="assistant_attachments")
    op.drop_index("ix_assistant_attachments_creator_status", table_name="assistant_attachments")
    op.drop_table("assistant_attachments")
    op.drop_constraint(
        "uq_workflow_plans_creator_scope", "workflow_plans", type_="unique"
    )
    op.drop_constraint(
        "uq_assistant_conversations_creator_scope",
        "assistant_conversations",
        type_="unique",
    )
