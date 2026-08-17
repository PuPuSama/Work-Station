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

    # Normalize historical rows before adding the new write-boundary indexes.
    # Older deployments allowed multiple leads per team and memberships in
    # multiple teams, so creating the indexes directly would make a real
    # production upgrade fail. Prefer the recorded team manager, then the
    # stable user id, and keep the result deterministic and repeatable.
    op.execute(
        """
        WITH ranked_leads AS (
            SELECT
                membership.ctid AS row_id,
                ROW_NUMBER() OVER (
                    PARTITION BY membership.organization_id,
                                 membership.team_id
                    ORDER BY
                        (membership.user_id = team.manager_user_id) DESC,
                        membership.user_id
                ) AS row_number
            FROM team_memberships AS membership
            JOIN teams AS team
              ON team.organization_id = membership.organization_id
             AND team.team_id = membership.team_id
            WHERE membership.role = 'team_lead'
        )
        UPDATE team_memberships AS membership
           SET role = 'member', updated_at = CURRENT_TIMESTAMP
          FROM ranked_leads
         WHERE membership.ctid = ranked_leads.row_id
           AND ranked_leads.row_number > 1
        """
    )
    op.execute(
        """
        WITH retained_leads AS (
            SELECT DISTINCT ON (membership.organization_id,
                                membership.team_id)
                   membership.organization_id,
                   membership.team_id,
                   membership.user_id
              FROM team_memberships AS membership
              JOIN teams AS team
                ON team.organization_id = membership.organization_id
               AND team.team_id = membership.team_id
             WHERE membership.role = 'team_lead'
             ORDER BY membership.organization_id,
                      membership.team_id,
                      (membership.user_id = team.manager_user_id) DESC,
                      membership.user_id
        )
        UPDATE teams AS team
           SET manager_user_id = retained.user_id,
               updated_at = CURRENT_TIMESTAMP
          FROM retained_leads AS retained
         WHERE team.organization_id = retained.organization_id
           AND team.team_id = retained.team_id
           AND team.status = 'active'
           AND team.manager_user_id IS DISTINCT FROM retained.user_id
        """
    )
    op.execute(
        """
        WITH ranked_memberships AS (
            SELECT
                membership.ctid AS row_id,
                ROW_NUMBER() OVER (
                    PARTITION BY membership.organization_id,
                                 membership.user_id
                    ORDER BY
                        EXISTS (
                            SELECT 1
                              FROM project_ownership AS ownership
                             WHERE ownership.organization_id
                                   = membership.organization_id
                               AND ownership.owning_team_id
                                   = membership.team_id
                               AND ownership.owner_user_id
                                   = membership.user_id
                        ) DESC,
                        EXISTS (
                            SELECT 1
                              FROM teams AS team
                             WHERE team.organization_id
                                   = membership.organization_id
                               AND team.team_id = membership.team_id
                               AND team.manager_user_id
                                   = membership.user_id
                        ) DESC,
                        (membership.role = 'team_lead') DESC,
                        membership.team_id
                ) AS row_number
            FROM team_memberships AS membership
        )
        DELETE FROM team_memberships AS membership
              USING ranked_memberships
             WHERE membership.ctid = ranked_memberships.row_id
               AND ranked_memberships.row_number > 1
        """
    )
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
