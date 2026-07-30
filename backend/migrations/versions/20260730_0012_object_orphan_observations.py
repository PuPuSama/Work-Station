"""Track continuously unreferenced project objects before cleanup.

Revision ID: 20260730_0012
Revises: 20260730_0011
Create Date: 2026-07-31
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260730_0012"
down_revision: str | Sequence[str] | None = "20260730_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "object_orphan_observations",
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column(
            "object_last_modified_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("registered_asset_count", sa.Integer(), nullable=False),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("sighting_count", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "btrim(object_key) <> '' AND btrim(fingerprint) <> ''",
            name="ck_object_orphan_observations_identity_nonempty",
        ),
        sa.CheckConstraint(
            "byte_size >= 0 AND registered_asset_count >= 0 "
            "AND sighting_count > 0",
            name="ck_object_orphan_observations_counts",
        ),
        sa.CheckConstraint(
            "last_seen_at >= first_seen_at",
            name="ck_object_orphan_observations_seen_order",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "project_id"],
            [
                "project_ownership.organization_id",
                "project_ownership.project_id",
            ],
            name="fk_object_orphan_observations_project",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "organization_id",
            "project_id",
            "object_key",
            name="pk_object_orphan_observations",
        ),
    )
    op.create_index(
        "ix_object_orphan_observations_eligibility",
        "object_orphan_observations",
        [
            "organization_id",
            "project_id",
            "first_seen_at",
            "sighting_count",
        ],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_object_orphan_observations_eligibility",
        table_name="object_orphan_observations",
    )
    op.drop_table("object_orphan_observations")
