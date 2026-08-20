"""Add Workflow Assistant M1 PostgreSQL storage.

Revision ID: 20260818_0025
Revises: 20260817_0024
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260818_0025"
down_revision: str | Sequence[str] | None = "20260817_0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    # LangGraph's PostgreSQL saver is shared by the existing Knowledge Agent
    # and Workflow Assistant. Keep its schema under Alembic control so a
    # clean Server database can execute durable graphs without application
    # startup creating or altering tables. IF NOT EXISTS preserves databases
    # that were initialized earlier through the LangGraph setup command.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS checkpoint_migrations (
            v INTEGER PRIMARY KEY
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS checkpoints (
            thread_id TEXT NOT NULL,
            checkpoint_ns TEXT NOT NULL DEFAULT '',
            checkpoint_id TEXT NOT NULL,
            parent_checkpoint_id TEXT,
            type TEXT,
            checkpoint JSONB NOT NULL,
            metadata JSONB NOT NULL DEFAULT '{}',
            PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS checkpoint_blobs (
            thread_id TEXT NOT NULL,
            checkpoint_ns TEXT NOT NULL DEFAULT '',
            channel TEXT NOT NULL,
            version TEXT NOT NULL,
            type TEXT NOT NULL,
            blob BYTEA,
            PRIMARY KEY (thread_id, checkpoint_ns, channel, version)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS checkpoint_writes (
            thread_id TEXT NOT NULL,
            checkpoint_ns TEXT NOT NULL DEFAULT '',
            checkpoint_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            task_path TEXT NOT NULL DEFAULT '',
            idx INTEGER NOT NULL,
            channel TEXT NOT NULL,
            type TEXT,
            blob BYTEA NOT NULL,
            PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS checkpoints_thread_id_idx "
        "ON checkpoints(thread_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS checkpoint_blobs_thread_id_idx "
        "ON checkpoint_blobs(thread_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS checkpoint_writes_thread_id_idx "
        "ON checkpoint_writes(thread_id)"
    )
    # ``CREATE TABLE IF NOT EXISTS`` does not upgrade LangGraph tables that
    # were created by an older saver release. Apply the two historical
    # additive changes before recording migrations 0..9 as present, otherwise
    # an existing database can be stamped current while still lacking the
    # columns/nullability expected by the installed saver.
    op.execute(
        "ALTER TABLE checkpoint_blobs "
        "ALTER COLUMN blob DROP NOT NULL"
    )
    op.execute(
        "ALTER TABLE checkpoint_writes "
        "ADD COLUMN IF NOT EXISTS task_path TEXT NOT NULL DEFAULT ''"
    )
    op.execute(
        "INSERT INTO checkpoint_migrations(v) "
        "SELECT generate_series(0, 9) ON CONFLICT (v) DO NOTHING"
    )

    op.create_table(
        "project_topics",
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("topic_id", sa.Text(), nullable=False),
        sa.Column("topic", sa.Text(), nullable=False),
        sa.Column("primary_keyword", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("competitor_keyword", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'published'")),
        sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("btrim(topic_id) <> '' AND btrim(topic) <> ''", name="ck_project_topics_identity_nonempty"),
        sa.CheckConstraint("status IN ('published', 'archived')", name="ck_project_topics_status"),
        sa.CheckConstraint("revision >= 0", name="ck_project_topics_revision"),
        sa.ForeignKeyConstraint(["organization_id", "project_id"], ["project_ownership.organization_id", "project_ownership.project_id"], name="fk_project_topics_project", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("organization_id", "project_id", "topic_id", name="pk_project_topics"),
    )
    op.create_index(
        "ix_project_topics_project_status",
        "project_topics",
        ["organization_id", "project_id", "status", "topic_id"],
    )

    op.create_table(
        "assistant_conversations",
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("conversation_id", sa.Text(), nullable=False),
        sa.Column("creator_user_id", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("last_project_ids", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "btrim(conversation_id) <> '' AND btrim(creator_user_id) <> '' AND btrim(title) <> ''",
            name="ck_assistant_conversations_identity_nonempty",
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.organization_id"], name="fk_assistant_conversations_organization", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "creator_user_id"], ["workspace_users.organization_id", "workspace_users.user_id"], name="fk_assistant_conversations_creator", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("organization_id", "conversation_id", name="pk_assistant_conversations"),
        sa.UniqueConstraint("conversation_id", name="uq_assistant_conversations_conversation_id"),
    )
    op.create_index("ix_assistant_conversations_creator", "assistant_conversations", ["organization_id", "creator_user_id", "updated_at"])
    op.create_index("ix_assistant_conversations_expiry", "assistant_conversations", ["expires_at"])

    op.create_table(
        "assistant_messages",
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("conversation_id", sa.Text(), nullable=False),
        sa.Column("message_id", sa.Text(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("sanitized_content", sa.Text(), nullable=False),
        sa.Column("request_id", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("btrim(message_id) <> '' AND btrim(request_id) <> '' AND btrim(idempotency_key) <> '' AND btrim(sanitized_content) <> ''", name="ck_assistant_messages_identity_nonempty"),
        sa.CheckConstraint("role IN ('user', 'assistant', 'system')", name="ck_assistant_messages_role"),
        sa.CheckConstraint("sequence > 0", name="ck_assistant_messages_sequence"),
        sa.ForeignKeyConstraint(["organization_id", "conversation_id"], ["assistant_conversations.organization_id", "assistant_conversations.conversation_id"], name="fk_assistant_messages_conversation", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("organization_id", "conversation_id", "message_id", name="pk_assistant_messages"),
        sa.UniqueConstraint("organization_id", "conversation_id", "sequence", name="uq_assistant_messages_sequence"),
        sa.UniqueConstraint("organization_id", "conversation_id", "idempotency_key", name="uq_assistant_messages_idempotency"),
    )

    op.create_table(
        "workflow_plans",
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("plan_id", sa.Text(), nullable=False),
        sa.Column("creator_user_id", sa.Text(), nullable=False),
        sa.Column("conversation_id", sa.Text(), nullable=False),
        sa.Column("source_idempotency_key", sa.Text(), nullable=True),
        sa.Column("natural_language_request", sa.Text(), nullable=False),
        sa.Column("normalized_plan", JSONB, nullable=False),
        sa.Column("plan_hash", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'draft'")),
        sa.Column("concurrency_limit", sa.Integer(), nullable=False, server_default=sa.text("3")),
        sa.Column("budget_warning", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("approved_by", sa.Text(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attention_state", sa.Text(), nullable=False, server_default=sa.text("'none'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("btrim(plan_id) <> '' AND btrim(creator_user_id) <> '' AND btrim(natural_language_request) <> ''", name="ck_workflow_plans_identity_nonempty"),
        sa.CheckConstraint("source_idempotency_key IS NULL OR btrim(source_idempotency_key) <> ''", name="ck_workflow_plans_source_idempotency_nonempty"),
        sa.CheckConstraint("plan_hash ~ '^[0-9a-f]{64}$'", name="ck_workflow_plans_hash"),
        sa.CheckConstraint("revision >= 0 AND concurrency_limit > 0", name="ck_workflow_plans_revision_limit"),
        sa.CheckConstraint("status IN ('draft', 'awaiting_confirmation', 'queued', 'running', 'waiting_review', 'paused', 'completed', 'failed', 'cancelled')", name="ck_workflow_plans_status"),
        sa.CheckConstraint("attention_state IN ('none', 'user_confirmation', 'error', 'unread')", name="ck_workflow_plans_attention_state"),
        sa.CheckConstraint("approved_at IS NULL OR approved_by IS NOT NULL", name="ck_workflow_plans_approval_identity"),
        sa.ForeignKeyConstraint(["organization_id", "creator_user_id"], ["workspace_users.organization_id", "workspace_users.user_id"], name="fk_workflow_plans_creator", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "conversation_id"], ["assistant_conversations.organization_id", "assistant_conversations.conversation_id"], name="fk_workflow_plans_conversation", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "approved_by"], ["workspace_users.organization_id", "workspace_users.user_id"], name="fk_workflow_plans_approver", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("organization_id", "plan_id", name="pk_workflow_plans"),
        sa.UniqueConstraint("plan_id", name="uq_workflow_plans_plan_id"),
        sa.UniqueConstraint("organization_id", "conversation_id", "source_idempotency_key", name="uq_workflow_plans_source_idempotency"),
    )
    op.create_index("ix_workflow_plans_creator_status", "workflow_plans", ["organization_id", "creator_user_id", "status", "updated_at"])

    op.create_table(
        "workflow_plan_projects",
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("plan_id", sa.Text(), nullable=False),
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("authorization_snapshot", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("paused", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.ForeignKeyConstraint(["organization_id", "plan_id"], ["workflow_plans.organization_id", "workflow_plans.plan_id"], name="fk_workflow_plan_projects_plan", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id", "project_id"], ["project_ownership.organization_id", "project_ownership.project_id"], name="fk_workflow_plan_projects_project", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("organization_id", "plan_id", "project_id", name="pk_workflow_plan_projects"),
    )

    op.create_table(
        "workflow_plan_steps",
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("plan_id", sa.Text(), nullable=False),
        sa.Column("step_id", sa.Text(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("action_kind", sa.Text(), nullable=False),
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("article_task_id", sa.Text(), nullable=True),
        sa.Column("expected_task_revision", sa.Integer(), nullable=True),
        sa.Column("pinned_prompt_version", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("pinned_knowledge_snapshot", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("background_job_id", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("hard_gate", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("human_gate_confirmed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("input_summary", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("output_summary", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("standardized_error_code", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("btrim(step_id) <> '' AND btrim(action_kind) <> '' AND btrim(project_id) <> ''", name="ck_workflow_plan_steps_identity_nonempty"),
        sa.CheckConstraint("sequence > 0 AND retry_count >= 0 AND (expected_task_revision IS NULL OR expected_task_revision >= 0)", name="ck_workflow_plan_steps_sequence_retry"),
        sa.CheckConstraint("status IN ('pending', 'running', 'waiting_job', 'waiting_review', 'succeeded', 'failed', 'skipped', 'cancelled')", name="ck_workflow_plan_steps_status"),
        sa.ForeignKeyConstraint(["organization_id", "plan_id", "project_id"], ["workflow_plan_projects.organization_id", "workflow_plan_projects.plan_id", "workflow_plan_projects.project_id"], name="fk_workflow_plan_steps_project", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id", "project_id", "article_task_id"], ["article_tasks.organization_id", "article_tasks.project_id", "article_tasks.task_id"], name="fk_workflow_plan_steps_task", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("organization_id", "plan_id", "step_id", name="pk_workflow_plan_steps"),
        sa.UniqueConstraint("organization_id", "plan_id", "sequence", name="uq_workflow_plan_steps_sequence"),
    )

    op.create_table(
        "workflow_plan_events",
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("plan_id", sa.Text(), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("event_kind", sa.Text(), nullable=False),
        sa.Column("public_payload", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("sequence > 0 AND btrim(event_kind) <> ''", name="ck_workflow_plan_events_identity"),
        sa.ForeignKeyConstraint(["organization_id", "plan_id"], ["workflow_plans.organization_id", "workflow_plans.plan_id"], name="fk_workflow_plan_events_plan", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("organization_id", "plan_id", "sequence", name="pk_workflow_plan_events"),
    )

    op.create_table(
        "assistant_usage_events",
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("usage_event_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("project_id", sa.Text(), nullable=True),
        sa.Column("plan_id", sa.Text(), nullable=True),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("operation_kind", sa.Text(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("estimated_cost", sa.Numeric(18, 8), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("btrim(usage_event_id) <> '' AND btrim(user_id) <> '' AND btrim(provider) <> '' AND btrim(model) <> '' AND btrim(operation_kind) <> ''", name="ck_assistant_usage_identity_nonempty"),
        sa.CheckConstraint("input_tokens >= 0 AND output_tokens >= 0 AND (estimated_cost IS NULL OR estimated_cost >= 0)", name="ck_assistant_usage_counts"),
        sa.ForeignKeyConstraint(["organization_id", "user_id"], ["workspace_users.organization_id", "workspace_users.user_id"], name="fk_assistant_usage_user", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "project_id"], ["project_ownership.organization_id", "project_ownership.project_id"], name="fk_assistant_usage_project", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "plan_id"], ["workflow_plans.organization_id", "workflow_plans.plan_id"], name="fk_assistant_usage_plan", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("organization_id", "usage_event_id", name="pk_assistant_usage_events"),
    )
    op.create_index("ix_assistant_usage_scope", "assistant_usage_events", ["organization_id", "user_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_assistant_usage_scope", table_name="assistant_usage_events")
    op.drop_table("assistant_usage_events")
    op.drop_table("workflow_plan_events")
    op.drop_table("workflow_plan_steps")
    op.drop_table("workflow_plan_projects")
    op.drop_index("ix_workflow_plans_creator_status", table_name="workflow_plans")
    op.drop_table("workflow_plans")
    op.drop_table("assistant_messages")
    op.drop_index("ix_assistant_conversations_expiry", table_name="assistant_conversations")
    op.drop_index("ix_assistant_conversations_creator", table_name="assistant_conversations")
    op.drop_table("assistant_conversations")
    op.drop_index("ix_project_topics_project_status", table_name="project_topics")
    op.drop_table("project_topics")
    # Shared LangGraph checkpoint rows may predate M1 and may still be needed
    # by the Knowledge Agent, so an M1 downgrade deliberately retains them.
