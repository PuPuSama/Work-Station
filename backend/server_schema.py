from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from knowledge_agent.schema import metadata, projects


organizations = sa.Table(
    "organizations",
    metadata,
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


workspace_users = sa.Table(
    "workspace_users",
    metadata,
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

sa.Index(
    "ix_workspace_users_user_id",
    workspace_users.c.user_id,
)


teams = sa.Table(
    "teams",
    metadata,
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
        [
            "workspace_users.organization_id",
            "workspace_users.user_id",
        ],
        name="fk_teams_manager",
        ondelete="RESTRICT",
    ),
    sa.PrimaryKeyConstraint(
        "organization_id",
        "team_id",
        name="pk_teams",
    ),
)


team_memberships = sa.Table(
    "team_memberships",
    metadata,
    sa.Column("organization_id", sa.Text(), nullable=False),
    sa.Column("team_id", sa.Text(), nullable=False),
    sa.Column("user_id", sa.Text(), nullable=False),
    sa.Column("role", sa.Text(), nullable=False),
    sa.Column("granted_by_user_id", sa.Text(), nullable=True),
    sa.Column(
        "granted_at",
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
        [
            "workspace_users.organization_id",
            "workspace_users.user_id",
        ],
        name="fk_team_memberships_user",
        ondelete="RESTRICT",
    ),
    sa.ForeignKeyConstraint(
        ["organization_id", "granted_by_user_id"],
        [
            "workspace_users.organization_id",
            "workspace_users.user_id",
        ],
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

sa.Index(
    "ix_team_memberships_user",
    team_memberships.c.organization_id,
    team_memberships.c.user_id,
)


project_ownership = sa.Table(
    "project_ownership",
    metadata,
    sa.Column("project_id", sa.Text(), primary_key=True),
    sa.Column("organization_id", sa.Text(), nullable=False),
    sa.Column("owning_team_id", sa.Text(), nullable=True),
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
        "owning_team_id IS NULL OR btrim(owning_team_id) <> ''",
        name="ck_project_ownership_team_nonempty",
    ),
    sa.ForeignKeyConstraint(
        ["project_id"],
        [projects.c.project_id],
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

sa.Index(
    "ix_project_ownership_team",
    project_ownership.c.organization_id,
    project_ownership.c.owning_team_id,
)


project_memberships = sa.Table(
    "project_memberships",
    metadata,
    sa.Column("organization_id", sa.Text(), nullable=False),
    sa.Column("project_id", sa.Text(), nullable=False),
    sa.Column("user_id", sa.Text(), nullable=False),
    sa.Column("role", sa.Text(), nullable=False),
    sa.Column("granted_by_user_id", sa.Text(), nullable=False),
    sa.Column(
        "granted_at",
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
        [
            "workspace_users.organization_id",
            "workspace_users.user_id",
        ],
        name="fk_project_memberships_user",
        ondelete="RESTRICT",
    ),
    sa.ForeignKeyConstraint(
        ["organization_id", "granted_by_user_id"],
        [
            "workspace_users.organization_id",
            "workspace_users.user_id",
        ],
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

sa.Index(
    "ix_project_memberships_user",
    project_memberships.c.organization_id,
    project_memberships.c.user_id,
    project_memberships.c.project_id,
)


audit_events = sa.Table(
    "audit_events",
    metadata,
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
        server_default=sa.func.now(),
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
        [
            "workspace_users.organization_id",
            "workspace_users.user_id",
        ],
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

sa.Index(
    "ix_audit_events_organization_created",
    audit_events.c.organization_id,
    audit_events.c.created_at,
)
sa.Index(
    "ix_audit_events_project_created",
    audit_events.c.organization_id,
    audit_events.c.project_id,
    audit_events.c.created_at,
)


__all__ = [
    "audit_events",
    "organizations",
    "project_memberships",
    "project_ownership",
    "team_memberships",
    "teams",
    "workspace_users",
]
