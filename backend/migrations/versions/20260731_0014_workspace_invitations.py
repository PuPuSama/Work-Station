"""Add one-time workspace invitations for verified external identities.

Revision ID: 20260731_0014
Revises: 20260731_0013
Create Date: 2026-07-31
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260731_0014"
down_revision: str | Sequence[str] | None = "20260731_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workspace_invitations",
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("invitation_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("issuer", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "accepted_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "created_by_user_id",
            sa.Text(),
            nullable=False,
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
            "btrim(invitation_id) <> '' AND btrim(user_id) <> '' "
            "AND btrim(issuer) <> '' AND btrim(created_by_user_id) <> ''",
            name="ck_workspace_invitations_identity_nonempty",
        ),
        sa.CheckConstraint(
            "token_hash ~ '^[0-9a-f]{64}$'",
            name="ck_workspace_invitations_token_hash",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'accepted', 'revoked')",
            name="ck_workspace_invitations_status",
        ),
        sa.CheckConstraint(
            "(status = 'accepted') = (accepted_at IS NOT NULL)",
            name="ck_workspace_invitations_acceptance",
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name="ck_workspace_invitations_expiry",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "user_id"],
            [
                "workspace_users.organization_id",
                "workspace_users.user_id",
            ],
            name="fk_workspace_invitations_target",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "created_by_user_id"],
            [
                "workspace_users.organization_id",
                "workspace_users.user_id",
            ],
            name="fk_workspace_invitations_creator",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "organization_id",
            "invitation_id",
            name="pk_workspace_invitations",
        ),
        sa.UniqueConstraint(
            "token_hash",
            name="uq_workspace_invitations_token_hash",
        ),
    )
    op.create_index(
        "ix_workspace_invitations_directory",
        "workspace_invitations",
        ["organization_id", "status", "invitation_id"],
    )
    op.create_index(
        "uq_workspace_invitations_pending_target_issuer",
        "workspace_invitations",
        ["organization_id", "user_id", "issuer"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_workspace_invitations_pending_target_issuer",
        table_name="workspace_invitations",
    )
    op.drop_index(
        "ix_workspace_invitations_directory",
        table_name="workspace_invitations",
    )
    op.drop_table("workspace_invitations")
