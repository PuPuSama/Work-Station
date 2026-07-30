"""Add the trusted requesting Actor to PostgreSQL jobs.

Revision ID: 20260730_0011
Revises: 20260730_0010
Create Date: 2026-07-30
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260730_0011"
down_revision: str | Sequence[str] | None = "20260730_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "background_jobs",
        sa.Column("requested_by_user_id", sa.Text(), nullable=True),
    )
    op.create_check_constraint(
        "ck_background_jobs_requester_nonempty",
        "background_jobs",
        "requested_by_user_id IS NULL "
        "OR btrim(requested_by_user_id) <> ''",
    )
    op.create_foreign_key(
        "fk_background_jobs_requester",
        "background_jobs",
        "workspace_users",
        ["organization_id", "requested_by_user_id"],
        ["organization_id", "user_id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_background_jobs_requester",
        "background_jobs",
        [
            "organization_id",
            "project_id",
            "requested_by_user_id",
            "created_at",
        ],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_background_jobs_requester",
        table_name="background_jobs",
    )
    op.drop_constraint(
        "fk_background_jobs_requester",
        "background_jobs",
        type_="foreignkey",
    )
    op.drop_constraint(
        "ck_background_jobs_requester_nonempty",
        "background_jobs",
        type_="check",
    )
    op.drop_column("background_jobs", "requested_by_user_id")
