"""Add project-scoped PostgreSQL task and job storage.

Revision ID: 20260730_0009
Revises: 20260730_0008
Create Date: 2026-07-30
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260730_0009"
down_revision: str | Sequence[str] | None = "20260730_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_store_state",
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column(
            "initialized",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "project_id"],
            [
                "project_ownership.organization_id",
                "project_ownership.project_id",
            ],
            name="fk_task_store_state_project",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "organization_id",
            "project_id",
            name="pk_task_store_state",
        ),
    )
    op.create_table(
        "article_tasks",
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column(
            "customer",
            sa.Text(),
            nullable=False,
            server_default=sa.text("''"),
        ),
        sa.Column(
            "topic_index",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "revision",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("position", sa.BigInteger(), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "record_updated_at",
            sa.Text(),
            nullable=False,
            server_default=sa.text("''"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "btrim(task_id) <> ''",
            name="ck_article_tasks_task_id_nonempty",
        ),
        sa.CheckConstraint(
            "revision >= 0 AND position >= 0",
            name="ck_article_tasks_revision_position",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "project_id"],
            [
                "project_ownership.organization_id",
                "project_ownership.project_id",
            ],
            name="fk_article_tasks_project",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "organization_id",
            "project_id",
            "task_id",
            name="pk_article_tasks",
        ),
    )
    op.create_index(
        "ix_article_tasks_customer",
        "article_tasks",
        [
            "organization_id",
            "project_id",
            "customer",
            "topic_index",
            "position",
        ],
    )
    op.create_index(
        "ix_article_tasks_record_updated",
        "article_tasks",
        ["organization_id", "project_id", "record_updated_at"],
    )
    op.create_table(
        "job_batches",
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("batch_id", sa.Text(), nullable=False),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column(
            "customer",
            sa.Text(),
            nullable=False,
            server_default=sa.text("''"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "btrim(batch_id) <> '' AND btrim(operation) <> ''",
            name="ck_job_batches_identity_nonempty",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "project_id"],
            [
                "project_ownership.organization_id",
                "project_ownership.project_id",
            ],
            name="fk_job_batches_project",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "organization_id",
            "project_id",
            "batch_id",
            name="pk_job_batches",
        ),
    )
    op.create_index(
        "ix_job_batches_project_created",
        "job_batches",
        ["organization_id", "project_id", "created_at"],
    )
    op.create_table(
        "background_jobs",
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("job_id", sa.Text(), nullable=False),
        sa.Column("batch_id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column(
            "customer",
            sa.Text(),
            nullable=False,
            server_default=sa.text("''"),
        ),
        sa.Column(
            "topic_index",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "topic",
            sa.Text(),
            nullable=False,
            server_default=sa.text("''"),
        ),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "request",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("source_revision", sa.Integer(), nullable=False),
        sa.Column("result_revision", sa.Integer(), nullable=True),
        sa.Column(
            "attempts",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "max_attempts",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("4"),
        ),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "cancel_requested",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "error",
            sa.Text(),
            nullable=False,
            server_default=sa.text("''"),
        ),
        sa.Column("worker_id", sa.Text(), nullable=True),
        sa.Column(
            "lease_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "btrim(job_id) <> '' AND btrim(operation) <> ''",
            name="ck_background_jobs_identity_nonempty",
        ),
        sa.CheckConstraint(
            "status IN "
            "('queued', 'running', 'retry_wait', 'succeeded', "
            "'failed', 'cancelled', 'conflict')",
            name="ck_background_jobs_status",
        ),
        sa.CheckConstraint(
            "source_revision >= 0 AND "
            "(result_revision IS NULL OR result_revision >= 0) AND "
            "attempts >= 0 AND max_attempts > 0",
            name="ck_background_jobs_attempts_revisions",
        ),
        sa.CheckConstraint(
            "(status = 'running' AND worker_id IS NOT NULL "
            "AND lease_expires_at IS NOT NULL) OR "
            "(status <> 'running' AND worker_id IS NULL "
            "AND lease_expires_at IS NULL)",
            name="ck_background_jobs_lease_state",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "project_id", "batch_id"],
            [
                "job_batches.organization_id",
                "job_batches.project_id",
                "job_batches.batch_id",
            ],
            name="fk_background_jobs_batch",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "project_id", "task_id"],
            [
                "article_tasks.organization_id",
                "article_tasks.project_id",
                "article_tasks.task_id",
            ],
            name="fk_background_jobs_task",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "organization_id",
            "project_id",
            "job_id",
            name="pk_background_jobs",
        ),
    )
    op.create_index(
        "ix_background_jobs_batch",
        "background_jobs",
        ["organization_id", "project_id", "batch_id", "created_at"],
    )
    op.create_index(
        "ix_background_jobs_runnable",
        "background_jobs",
        [
            "organization_id",
            "project_id",
            "status",
            "available_at",
            "created_at",
        ],
    )
    op.create_index(
        "uq_background_jobs_active_task",
        "background_jobs",
        ["organization_id", "project_id", "task_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('queued', 'running', 'retry_wait')"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_background_jobs_active_task",
        table_name="background_jobs",
    )
    op.drop_index(
        "ix_background_jobs_runnable",
        table_name="background_jobs",
    )
    op.drop_index(
        "ix_background_jobs_batch",
        table_name="background_jobs",
    )
    op.drop_table("background_jobs")
    op.drop_index(
        "ix_job_batches_project_created",
        table_name="job_batches",
    )
    op.drop_table("job_batches")
    op.drop_index(
        "ix_article_tasks_record_updated",
        table_name="article_tasks",
    )
    op.drop_index(
        "ix_article_tasks_customer",
        table_name="article_tasks",
    )
    op.drop_table("article_tasks")
    op.drop_table("task_store_state")
