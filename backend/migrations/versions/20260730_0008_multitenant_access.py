"""Add explicit organization, team, project access, and audit boundaries.

Revision ID: 20260730_0008
Revises: 20260730_0007
Create Date: 2026-07-30
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260730_0008"
down_revision: str | Sequence[str] | None = "20260730_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> tuple[sa.Column, sa.Column]:
    return (
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
    )


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column("organization_id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "data_residency_policy",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'unspecified'"),
        ),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        *_timestamps(),
        sa.CheckConstraint(
            "btrim(organization_id) <> '' AND btrim(name) <> ''",
            name="ck_organizations_identity_nonempty",
        ),
        sa.CheckConstraint(
            "btrim(data_residency_policy) <> ''",
            name="ck_organizations_residency_nonempty",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'suspended')",
            name="ck_organizations_status",
        ),
    )
    op.create_table(
        "workspace_users",
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column(
            "organization_role",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'member'"),
        ),
        *_timestamps(),
        sa.CheckConstraint(
            "btrim(user_id) <> '' AND btrim(display_name) <> ''",
            name="ck_workspace_users_identity_nonempty",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'disabled')",
            name="ck_workspace_users_status",
        ),
        sa.CheckConstraint(
            "organization_role IN ('org_admin', 'member')",
            name="ck_workspace_users_organization_role",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.organization_id"],
            name="fk_workspace_users_organization",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "organization_id",
            "user_id",
            name="pk_workspace_users",
        ),
    )
    op.create_index(
        "ix_workspace_users_user_id",
        "workspace_users",
        ["user_id"],
    )
    op.create_table(
        "teams",
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("team_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("manager_user_id", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        *_timestamps(),
        sa.CheckConstraint(
            "btrim(team_id) <> '' AND btrim(name) <> ''",
            name="ck_teams_identity_nonempty",
        ),
        sa.CheckConstraint(
            "manager_user_id IS NULL OR btrim(manager_user_id) <> ''",
            name="ck_teams_manager_nonempty",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'archived')",
            name="ck_teams_status",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.organization_id"],
            name="fk_teams_organization",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "manager_user_id"],
            ["workspace_users.organization_id", "workspace_users.user_id"],
            name="fk_teams_manager",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "organization_id",
            "team_id",
            name="pk_teams",
        ),
    )
    op.create_table(
        "team_memberships",
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("team_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("granted_by_user_id", sa.Text(), nullable=True),
        sa.Column(
            "granted_at",
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
            "role IN ('team_lead', 'member')",
            name="ck_team_memberships_role",
        ),
        sa.CheckConstraint(
            "granted_by_user_id IS NULL OR btrim(granted_by_user_id) <> ''",
            name="ck_team_memberships_grantor_nonempty",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "team_id"],
            ["teams.organization_id", "teams.team_id"],
            name="fk_team_memberships_team",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["workspace_users.organization_id", "workspace_users.user_id"],
            name="fk_team_memberships_user",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "granted_by_user_id"],
            ["workspace_users.organization_id", "workspace_users.user_id"],
            name="fk_team_memberships_grantor",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "organization_id",
            "team_id",
            "user_id",
            name="pk_team_memberships",
        ),
    )
    op.create_index(
        "ix_team_memberships_user",
        "team_memberships",
        ["organization_id", "user_id"],
    )
    op.create_table(
        "project_ownership",
        sa.Column("project_id", sa.Text(), primary_key=True),
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("owning_team_id", sa.Text(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "owning_team_id IS NULL OR btrim(owning_team_id) <> ''",
            name="ck_project_ownership_team_nonempty",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.project_id"],
            name="fk_project_ownership_project",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.organization_id"],
            name="fk_project_ownership_organization",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "owning_team_id"],
            ["teams.organization_id", "teams.team_id"],
            name="fk_project_ownership_team",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "project_id",
            name="uq_project_ownership_organization_project",
        ),
    )
    op.create_index(
        "ix_project_ownership_team",
        "project_ownership",
        ["organization_id", "owning_team_id"],
    )
    op.create_table(
        "project_memberships",
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("granted_by_user_id", sa.Text(), nullable=False),
        sa.Column(
            "granted_at",
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
            "role IN ('editor', 'reviewer', 'viewer')",
            name="ck_project_memberships_role",
        ),
        sa.CheckConstraint(
            "btrim(granted_by_user_id) <> ''",
            name="ck_project_memberships_grantor_nonempty",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "project_id"],
            [
                "project_ownership.organization_id",
                "project_ownership.project_id",
            ],
            name="fk_project_memberships_project",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["workspace_users.organization_id", "workspace_users.user_id"],
            name="fk_project_memberships_user",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "granted_by_user_id"],
            ["workspace_users.organization_id", "workspace_users.user_id"],
            name="fk_project_memberships_grantor",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "organization_id",
            "project_id",
            "user_id",
            name="pk_project_memberships",
        ),
    )
    op.create_index(
        "ix_project_memberships_user",
        "project_memberships",
        ["organization_id", "user_id", "project_id"],
    )
    op.create_table(
        "audit_events",
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column("actor_user_id", sa.Text(), nullable=True),
        sa.Column("project_id", sa.Text(), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("target_type", sa.Text(), nullable=False),
        sa.Column("target_id", sa.Text(), nullable=False),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "btrim(event_id) <> '' AND btrim(action) <> '' "
            "AND btrim(target_type) <> '' AND btrim(target_id) <> ''",
            name="ck_audit_events_identity_nonempty",
        ),
        sa.CheckConstraint(
            "actor_user_id IS NULL OR btrim(actor_user_id) <> ''",
            name="ck_audit_events_actor_nonempty",
        ),
        sa.CheckConstraint(
            "project_id IS NULL OR btrim(project_id) <> ''",
            name="ck_audit_events_project_nonempty",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.organization_id"],
            name="fk_audit_events_organization",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "actor_user_id"],
            ["workspace_users.organization_id", "workspace_users.user_id"],
            name="fk_audit_events_actor",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "project_id"],
            [
                "project_ownership.organization_id",
                "project_ownership.project_id",
            ],
            name="fk_audit_events_project",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "organization_id",
            "event_id",
            name="pk_audit_events",
        ),
    )
    op.create_index(
        "ix_audit_events_organization_created",
        "audit_events",
        ["organization_id", "created_at"],
    )
    op.create_index(
        "ix_audit_events_project_created",
        "audit_events",
        ["organization_id", "project_id", "created_at"],
    )
    op.execute(
        """
        CREATE FUNCTION prevent_audit_event_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'audit_events is append-only';
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_audit_events_append_only
        BEFORE UPDATE OR DELETE ON audit_events
        FOR EACH ROW
        EXECUTE FUNCTION prevent_audit_event_mutation()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_audit_events_append_only ON audit_events"
    )
    op.execute("DROP FUNCTION IF EXISTS prevent_audit_event_mutation()")
    op.drop_index(
        "ix_audit_events_project_created",
        table_name="audit_events",
    )
    op.drop_index(
        "ix_audit_events_organization_created",
        table_name="audit_events",
    )
    op.drop_table("audit_events")
    op.drop_index(
        "ix_project_memberships_user",
        table_name="project_memberships",
    )
    op.drop_table("project_memberships")
    op.drop_index(
        "ix_project_ownership_team",
        table_name="project_ownership",
    )
    op.drop_table("project_ownership")
    op.drop_index(
        "ix_team_memberships_user",
        table_name="team_memberships",
    )
    op.drop_table("team_memberships")
    op.drop_table("teams")
    op.drop_index(
        "ix_workspace_users_user_id",
        table_name="workspace_users",
    )
    op.drop_table("workspace_users")
    op.drop_table("organizations")
