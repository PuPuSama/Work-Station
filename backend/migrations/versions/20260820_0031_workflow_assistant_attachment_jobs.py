"""Add durable Workflow Assistant attachment jobs.

Revision ID: 20260820_0031
Revises: 20260820_0030
Create Date: 2026-08-20
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260820_0031"
down_revision = "20260820_0030"
branch_labels = None
depends_on = None


def _constraint_exists(table_name: str, constraint_name: str, kind: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if kind == "unique":
        constraints = inspector.get_unique_constraints(table_name)
    elif kind == "check":
        constraints = inspector.get_check_constraints(table_name)
    elif kind == "foreignkey":
        constraints = inspector.get_foreign_keys(table_name)
    else:  # pragma: no cover - migration authoring error
        raise ValueError(f"unsupported constraint kind: {kind}")
    return any(item.get("name") == constraint_name for item in constraints)


def _ensure_unique(table_name: str, constraint_name: str, columns: list[str]) -> None:
    if not _constraint_exists(table_name, constraint_name, "unique"):
        op.create_unique_constraint(constraint_name, table_name, columns)


def _replace_check(table_name: str, constraint_name: str, expression: str) -> None:
    if _constraint_exists(table_name, constraint_name, "check"):
        op.drop_constraint(constraint_name, table_name, type_="check")
    op.create_check_constraint(constraint_name, table_name, expression)


def _replace_foreign_key(
    table_name: str,
    constraint_name: str,
    referred_table: str,
    local_columns: list[str],
    referred_columns: list[str],
    *,
    ondelete: str,
) -> None:
    if _constraint_exists(table_name, constraint_name, "foreignkey"):
        op.drop_constraint(constraint_name, table_name, type_="foreignkey")
    op.create_foreign_key(
        constraint_name,
        table_name,
        referred_table,
        local_columns,
        referred_columns,
        ondelete=ondelete,
    )


def _reconcile_legacy_m2_schema() -> None:
    """Repair databases stamped at 0030 by an earlier M2 schema draft.

    A development database may already report revision 0030 while it lacks the
    creator-scoped keys and temporary lifecycle states required by the final
    M2 contract.  Reconcile those additive boundaries before creating Job
    foreign keys so the upgrade remains safe for both clean and legacy stores.
    """

    _ensure_unique(
        "assistant_conversations",
        "uq_assistant_conversations_creator_scope",
        ["organization_id", "conversation_id", "creator_user_id"],
    )
    _ensure_unique(
        "workflow_plans",
        "uq_workflow_plans_creator_scope",
        ["organization_id", "plan_id", "creator_user_id"],
    )
    _ensure_unique(
        "assistant_attachments",
        "uq_assistant_attachments_creator_scope",
        ["organization_id", "attachment_id", "creator_user_id"],
    )
    _replace_check(
        "assistant_attachments",
        "ck_assistant_attachments_status",
        "status IN ('uploading', 'uploaded', 'classifying', 'needs_user_choice', "
        "'proposal_ready', 'importing', 'imported', 'rejected', 'expired', "
        "'rejecting', 'expiring', 'failed')",
    )
    _replace_foreign_key(
        "assistant_attachments",
        "fk_assistant_attachments_conversation",
        "assistant_conversations",
        ["organization_id", "conversation_id", "creator_user_id"],
        ["organization_id", "conversation_id", "creator_user_id"],
        ondelete="RESTRICT",
    )
    _replace_foreign_key(
        "assistant_attachments",
        "fk_assistant_attachments_plan",
        "workflow_plans",
        ["organization_id", "plan_id", "creator_user_id"],
        ["organization_id", "plan_id", "creator_user_id"],
        ondelete="RESTRICT",
    )
    _replace_foreign_key(
        "assistant_import_proposals",
        "fk_assistant_import_proposals_attachment",
        "assistant_attachments",
        ["organization_id", "attachment_id", "creator_user_id"],
        ["organization_id", "attachment_id", "creator_user_id"],
        ondelete="CASCADE",
    )
    _replace_foreign_key(
        "assistant_import_proposals",
        "fk_assistant_import_proposals_plan",
        "workflow_plans",
        ["organization_id", "plan_id", "creator_user_id"],
        ["organization_id", "plan_id", "creator_user_id"],
        ondelete="RESTRICT",
    )


def upgrade() -> None:
    _reconcile_legacy_m2_schema()
    op.create_table(
        "assistant_attachment_jobs",
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("job_id", sa.Text(), nullable=False),
        sa.Column("requested_by_user_id", sa.Text(), nullable=False),
        sa.Column("project_id", sa.Text(), nullable=True),
        sa.Column("attachment_id", sa.Text(), nullable=False),
        sa.Column("proposal_id", sa.Text(), nullable=True),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("expected_attachment_revision", sa.Integer(), nullable=False),
        sa.Column("expected_proposal_revision", sa.Integer(), nullable=True),
        sa.Column(
            "request_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "result_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("result_attachment_revision", sa.Integer(), nullable=True),
        sa.Column("result_proposal_revision", sa.Integer(), nullable=True),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'queued'"),
        ),
        sa.Column(
            "attempts", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "max_attempts", sa.Integer(), nullable=False, server_default=sa.text("4")
        ),
        sa.Column(
            "cancel_requested",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("standardized_error_code", sa.Text(), nullable=True),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("worker_id", sa.Text(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "btrim(job_id) <> '' AND btrim(requested_by_user_id) <> '' "
            "AND btrim(attachment_id) <> '' AND btrim(operation) <> '' "
            "AND btrim(idempotency_key) <> ''",
            name="ck_assistant_attachment_jobs_identity_nonempty",
        ),
        sa.CheckConstraint(
            "operation IN ('classify_attachment', 'preview_import_proposal', "
            "'execute_import_proposal')",
            name="ck_assistant_attachment_jobs_operation",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'retry_wait', 'succeeded', "
            "'failed', 'cancelled', 'conflict')",
            name="ck_assistant_attachment_jobs_status",
        ),
        sa.CheckConstraint(
            "expected_attachment_revision >= 0 "
            "AND (expected_proposal_revision IS NULL "
            "OR expected_proposal_revision >= 0) "
            "AND (result_attachment_revision IS NULL "
            "OR result_attachment_revision >= 0) "
            "AND (result_proposal_revision IS NULL "
            "OR result_proposal_revision >= 0) "
            "AND attempts >= 0 AND max_attempts > 0 AND attempts <= max_attempts",
            name="ck_assistant_attachment_jobs_revisions_attempts",
        ),
        sa.CheckConstraint(
            "(operation = 'classify_attachment' AND proposal_id IS NULL "
            "AND expected_proposal_revision IS NULL) OR "
            "(operation = 'preview_import_proposal' AND project_id IS NOT NULL "
            "AND proposal_id IS NULL AND expected_proposal_revision IS NULL) OR "
            "(operation = 'execute_import_proposal' AND project_id IS NOT NULL "
            "AND proposal_id IS NOT NULL "
            "AND expected_proposal_revision IS NOT NULL)",
            name="ck_assistant_attachment_jobs_target_shape",
        ),
        sa.CheckConstraint(
            "((status = 'running') = (worker_id IS NOT NULL "
            "AND lease_expires_at IS NOT NULL))",
            name="ck_assistant_attachment_jobs_lease_state",
        ),
        sa.CheckConstraint(
            "(status IN ('succeeded', 'failed', 'cancelled', 'conflict')) "
            "= (finished_at IS NOT NULL)",
            name="ck_assistant_attachment_jobs_finished_state",
        ),
        sa.CheckConstraint(
            "standardized_error_code IS NULL "
            "OR btrim(standardized_error_code) <> ''",
            name="ck_assistant_attachment_jobs_error_nonempty",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "requested_by_user_id"],
            ["workspace_users.organization_id", "workspace_users.user_id"],
            name="fk_assistant_attachment_jobs_requester",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "project_id"],
            ["project_ownership.organization_id", "project_ownership.project_id"],
            name="fk_assistant_attachment_jobs_project",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "attachment_id", "requested_by_user_id"],
            [
                "assistant_attachments.organization_id",
                "assistant_attachments.attachment_id",
                "assistant_attachments.creator_user_id",
            ],
            name="fk_assistant_attachment_jobs_attachment",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "proposal_id"],
            [
                "assistant_import_proposals.organization_id",
                "assistant_import_proposals.proposal_id",
            ],
            name="fk_assistant_attachment_jobs_proposal",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "organization_id", "job_id", name="pk_assistant_attachment_jobs"
        ),
        sa.UniqueConstraint(
            "organization_id",
            "requested_by_user_id",
            "operation",
            "idempotency_key",
            name="uq_assistant_attachment_jobs_idempotency",
        ),
    )
    op.create_index(
        "ix_assistant_attachment_jobs_claim",
        "assistant_attachment_jobs",
        ["status", "available_at", "created_at"],
    )
    op.create_index(
        "uq_assistant_attachment_jobs_active_attachment",
        "assistant_attachment_jobs",
        ["organization_id", "attachment_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running', 'retry_wait')"),
    )
    op.create_index(
        "uq_assistant_attachment_jobs_active_proposal",
        "assistant_attachment_jobs",
        ["organization_id", "proposal_id"],
        unique=True,
        postgresql_where=sa.text(
            "proposal_id IS NOT NULL "
            "AND status IN ('queued', 'running', 'retry_wait')"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_assistant_attachment_jobs_active_proposal",
        table_name="assistant_attachment_jobs",
    )
    op.drop_index(
        "uq_assistant_attachment_jobs_active_attachment",
        table_name="assistant_attachment_jobs",
    )
    op.drop_index(
        "ix_assistant_attachment_jobs_claim",
        table_name="assistant_attachment_jobs",
    )
    op.drop_table("assistant_attachment_jobs")
