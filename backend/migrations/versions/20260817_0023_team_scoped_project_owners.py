"""Add team-scoped invitations and one project owner metadata.

Revision ID: 20260817_0023
Revises: 20260813_0022
Create Date: 2026-08-17
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260817_0023"
down_revision: str | Sequence[str] | None = "20260813_0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "workspace_invitations",
        "user_id",
        existing_type=sa.Text(),
        nullable=True,
    )
    op.add_column(
        "workspace_invitations",
        sa.Column("team_id", sa.Text(), nullable=True),
    )
    op.drop_constraint(
        "ck_workspace_invitations_identity_nonempty",
        "workspace_invitations",
        type_="check",
    )
    op.create_check_constraint(
        "ck_workspace_invitations_identity_nonempty",
        "workspace_invitations",
        "btrim(invitation_id) <> '' "
        "AND (user_id IS NULL OR btrim(user_id) <> '') "
        "AND (team_id IS NULL OR btrim(team_id) <> '') "
        "AND btrim(issuer) <> '' AND btrim(created_by_user_id) <> ''",
    )
    op.create_foreign_key(
        "fk_workspace_invitations_team",
        "workspace_invitations",
        "teams",
        ["organization_id", "team_id"],
        ["organization_id", "team_id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_workspace_invitations_team_directory",
        "workspace_invitations",
        ["organization_id", "team_id", "status", "invitation_id"],
    )

    op.add_column(
        "project_ownership",
        sa.Column("owner_user_id", sa.Text(), nullable=True),
    )
    op.create_check_constraint(
        "ck_project_ownership_owner_nonempty",
        "project_ownership",
        "owner_user_id IS NULL OR btrim(owner_user_id) <> ''",
    )
    op.create_foreign_key(
        "fk_project_ownership_owner",
        "project_ownership",
        "workspace_users",
        ["organization_id", "owner_user_id"],
        ["organization_id", "user_id"],
        ondelete="RESTRICT",
    )

    # New users belong to at most one team and each team has at most one
    # active lead. Existing rows are left intact and are still readable as
    # legacy access data; the unique indexes enforce the new write boundary.
    op.create_index(
        "uq_team_memberships_one_team_per_user",
        "team_memberships",
        ["organization_id", "user_id"],
        unique=True,
    )
    op.create_index(
        "uq_team_memberships_one_lead_per_team",
        "team_memberships",
        ["organization_id", "team_id"],
        unique=True,
        postgresql_where=sa.text("role = 'team_lead'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_team_memberships_one_lead_per_team",
        table_name="team_memberships",
    )
    op.drop_index(
        "uq_team_memberships_one_team_per_user",
        table_name="team_memberships",
    )
    op.drop_constraint(
        "fk_project_ownership_owner",
        "project_ownership",
        type_="foreignkey",
    )
    op.drop_constraint(
        "ck_project_ownership_owner_nonempty",
        "project_ownership",
        type_="check",
    )
    op.drop_column("project_ownership", "owner_user_id")
    op.drop_index(
        "ix_workspace_invitations_team_directory",
        table_name="workspace_invitations",
    )
    op.drop_constraint(
        "fk_workspace_invitations_team",
        "workspace_invitations",
        type_="foreignkey",
    )
    op.drop_constraint(
        "ck_workspace_invitations_identity_nonempty",
        "workspace_invitations",
        type_="check",
    )
    op.create_check_constraint(
        "ck_workspace_invitations_identity_nonempty",
        "workspace_invitations",
        "btrim(invitation_id) <> '' AND btrim(user_id) <> '' "
        "AND btrim(issuer) <> '' AND btrim(created_by_user_id) <> ''",
    )
    op.drop_column("workspace_invitations", "team_id")
    op.alter_column(
        "workspace_invitations",
        "user_id",
        existing_type=sa.Text(),
        nullable=False,
    )
