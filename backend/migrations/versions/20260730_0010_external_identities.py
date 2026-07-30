"""Add provider-neutral external identity mappings.

Revision ID: 20260730_0010
Revises: 20260730_0009
Create Date: 2026-07-30
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260730_0010"
down_revision: str | Sequence[str] | None = "20260730_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "external_identities",
        sa.Column("issuer", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'active'"),
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
            "btrim(issuer) <> '' AND btrim(subject) <> ''",
            name="ck_external_identities_identity_nonempty",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'revoked')",
            name="ck_external_identities_status",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "user_id"],
            [
                "workspace_users.organization_id",
                "workspace_users.user_id",
            ],
            name="fk_external_identities_workspace_user",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "issuer",
            "subject",
            name="pk_external_identities",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "user_id",
            "issuer",
            name="uq_external_identities_user_issuer",
        ),
    )
    op.create_index(
        "ix_external_identities_workspace_user",
        "external_identities",
        ["organization_id", "user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_external_identities_workspace_user",
        table_name="external_identities",
    )
    op.drop_table("external_identities")
