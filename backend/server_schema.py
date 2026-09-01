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


workspace_user_llm_settings = sa.Table(
    "workspace_user_llm_settings",
    metadata,
    sa.Column("organization_id", sa.Text(), nullable=False),
    sa.Column("user_id", sa.Text(), nullable=False),
    sa.Column("model", sa.Text(), nullable=False),
    sa.Column("reasoning_effort", sa.Text(), nullable=False),
    sa.Column(
        "revision",
        sa.Integer(),
        nullable=False,
        server_default=sa.text("0"),
    ),
    sa.Column(
        "updated_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    ),
    sa.CheckConstraint(
        "btrim(model) <> '' AND btrim(reasoning_effort) <> ''",
        name="ck_workspace_user_llm_settings_values_nonempty",
    ),
    sa.CheckConstraint(
        "revision >= 0",
        name="ck_workspace_user_llm_settings_revision_nonnegative",
    ),
    sa.ForeignKeyConstraint(
        ["organization_id", "user_id"],
        ["workspace_users.organization_id", "workspace_users.user_id"],
        name="fk_workspace_user_llm_settings_user",
        ondelete="CASCADE",
    ),
    sa.PrimaryKeyConstraint(
        "organization_id",
        "user_id",
        name="pk_workspace_user_llm_settings",
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
        "session_version",
        sa.BigInteger(),
        nullable=False,
        server_default=sa.text("1"),
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
    sa.CheckConstraint(
        "session_version > 0",
        name="ck_workspace_users_session_version",
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


external_identities = sa.Table(
    "external_identities",
    metadata,
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
        server_default=sa.func.now(),
    ),
    sa.Column(
        "updated_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
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

sa.Index(
    "ix_external_identities_workspace_user",
    external_identities.c.organization_id,
    external_identities.c.user_id,
)


workspace_invitations = sa.Table(
    "workspace_invitations",
    metadata,
    sa.Column("organization_id", sa.Text(), nullable=False),
    sa.Column("invitation_id", sa.Text(), nullable=False),
    # Legacy invitations may target a pre-provisioned user. New invitations
    # leave this nullable and provision the workspace user at redemption.
    sa.Column("user_id", sa.Text(), nullable=True),
    sa.Column("team_id", sa.Text(), nullable=True),
    sa.Column("issuer", sa.Text(), nullable=False),
    sa.Column("token_hash", sa.Text(), nullable=False),
    sa.Column(
        "status",
        sa.Text(),
        nullable=False,
        server_default=sa.text("'pending'"),
    ),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("created_by_user_id", sa.Text(), nullable=False),
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
        "btrim(invitation_id) <> '' "
        "AND (user_id IS NULL OR btrim(user_id) <> '') "
        "AND (team_id IS NULL OR btrim(team_id) <> '') "
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
        ["organization_id", "team_id"],
        ["teams.organization_id", "teams.team_id"],
        name="fk_workspace_invitations_team",
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

sa.Index(
    "ix_workspace_invitations_directory",
    workspace_invitations.c.organization_id,
    workspace_invitations.c.status,
    workspace_invitations.c.invitation_id,
)

sa.Index(
    "uq_workspace_invitations_pending_target_issuer",
    workspace_invitations.c.organization_id,
    workspace_invitations.c.user_id,
    workspace_invitations.c.issuer,
    unique=True,
    postgresql_where=workspace_invitations.c.status == "pending",
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
    sa.Column("owner_user_id", sa.Text(), nullable=True),
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
    sa.CheckConstraint(
        "owner_user_id IS NULL OR btrim(owner_user_id) <> ''",
        name="ck_project_ownership_owner_nonempty",
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
    sa.ForeignKeyConstraint(
        ["organization_id", "owner_user_id"],
        ["workspace_users.organization_id", "workspace_users.user_id"],
        name="fk_project_ownership_owner",
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


project_prompt_heads = sa.Table(
    "project_prompt_heads",
    metadata,
    sa.Column("organization_id", sa.Text(), nullable=False),
    sa.Column("project_id", sa.Text(), nullable=False),
    sa.Column("prompt_id", sa.Text(), nullable=False),
    sa.Column("kind", sa.Text(), nullable=False),
    sa.Column(
        "status",
        sa.Text(),
        nullable=False,
        server_default=sa.text("'active'"),
    ),
    sa.Column(
        "current_version",
        sa.Integer(),
        nullable=False,
        server_default=sa.text("1"),
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
        "btrim(prompt_id) <> ''",
        name="ck_project_prompt_heads_identity_nonempty",
    ),
    sa.CheckConstraint(
        "kind IN ('outline', 'article', 'review', 'humanize')",
        name="ck_project_prompt_heads_kind",
    ),
    sa.CheckConstraint(
        "status IN ('active', 'archived')",
        name="ck_project_prompt_heads_status",
    ),
    sa.CheckConstraint(
        "current_version > 0",
        name="ck_project_prompt_heads_current_version",
    ),
    sa.ForeignKeyConstraint(
        ["organization_id", "project_id"],
        ["project_ownership.organization_id", "project_ownership.project_id"],
        name="fk_project_prompt_heads_project",
        ondelete="RESTRICT",
    ),
    sa.PrimaryKeyConstraint(
        "organization_id", "project_id", "prompt_id",
        name="pk_project_prompt_heads",
    ),
    sa.UniqueConstraint(
        "organization_id", "project_id", "prompt_id", "kind",
        name="uq_project_prompt_heads_kind",
    ),
    sa.UniqueConstraint(
        "organization_id", "project_id", "prompt_id", "current_version",
        name="uq_project_prompt_heads_current_version",
    ),
)

project_prompt_versions = sa.Table(
    "project_prompt_versions",
    metadata,
    sa.Column("organization_id", sa.Text(), nullable=False),
    sa.Column("project_id", sa.Text(), nullable=False),
    sa.Column("prompt_id", sa.Text(), nullable=False),
    sa.Column("kind", sa.Text(), nullable=False),
    sa.Column("version", sa.Integer(), nullable=False),
    sa.Column("name", sa.Text(), nullable=False),
    sa.Column("content", sa.Text(), nullable=False),
    sa.Column("content_hash", sa.Text(), nullable=False),
    sa.Column("created_by_user_id", sa.Text(), nullable=False),
    sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    ),
    sa.CheckConstraint(
        "kind IN ('outline', 'article', 'review', 'humanize')",
        name="ck_project_prompt_versions_kind",
    ),
    sa.CheckConstraint(
        "version > 0 AND btrim(name) <> '' AND btrim(content) <> ''",
        name="ck_project_prompt_versions_content",
    ),
    sa.CheckConstraint(
        "content_hash ~ '^[0-9a-f]{64}$'",
        name="ck_project_prompt_versions_hash",
    ),
    sa.ForeignKeyConstraint(
        ["organization_id", "project_id", "prompt_id", "kind"],
        [
            "project_prompt_heads.organization_id",
            "project_prompt_heads.project_id",
            "project_prompt_heads.prompt_id",
            "project_prompt_heads.kind",
        ],
        name="fk_project_prompt_versions_head",
        ondelete="CASCADE",
    ),
    sa.ForeignKeyConstraint(
        ["organization_id", "created_by_user_id"],
        ["workspace_users.organization_id", "workspace_users.user_id"],
        name="fk_project_prompt_versions_creator",
        ondelete="RESTRICT",
    ),
    sa.PrimaryKeyConstraint(
        "organization_id", "project_id", "prompt_id", "version",
        name="pk_project_prompt_versions",
    ),
    sa.UniqueConstraint(
        "organization_id", "project_id", "prompt_id", "kind", "version",
        name="uq_project_prompt_versions_kind",
    ),
)

project_prompt_heads.append_constraint(
    sa.ForeignKeyConstraint(
        [
            "organization_id",
            "project_id",
            "prompt_id",
            "kind",
            "current_version",
        ],
        [
            "project_prompt_versions.organization_id",
            "project_prompt_versions.project_id",
            "project_prompt_versions.prompt_id",
            "project_prompt_versions.kind",
            "project_prompt_versions.version",
        ],
        name="fk_project_prompt_heads_current_version",
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
        use_alter=True,
    )
)

project_prompt_defaults = sa.Table(
    "project_prompt_defaults",
    metadata,
    sa.Column("organization_id", sa.Text(), nullable=False),
    sa.Column("project_id", sa.Text(), nullable=False),
    sa.Column("kind", sa.Text(), nullable=False),
    sa.Column("prompt_id", sa.Text(), nullable=False),
    sa.Column("version", sa.Integer(), nullable=False),
    sa.Column(
        "updated_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    ),
    sa.CheckConstraint(
        "kind IN ('outline', 'article', 'review', 'humanize')",
        name="ck_project_prompt_defaults_kind",
    ),
    sa.CheckConstraint(
        "version > 0",
        name="ck_project_prompt_defaults_version",
    ),
    sa.ForeignKeyConstraint(
        ["organization_id", "project_id"],
        ["project_ownership.organization_id", "project_ownership.project_id"],
        name="fk_project_prompt_defaults_project",
        ondelete="RESTRICT",
    ),
    sa.ForeignKeyConstraint(
        [
            "organization_id",
            "project_id",
            "prompt_id",
            "kind",
            "version",
        ],
        [
            "project_prompt_versions.organization_id",
            "project_prompt_versions.project_id",
            "project_prompt_versions.prompt_id",
            "project_prompt_versions.kind",
            "project_prompt_versions.version",
        ],
        name="fk_project_prompt_defaults_version",
        ondelete="RESTRICT",
    ),
    sa.PrimaryKeyConstraint(
        "organization_id", "project_id", "kind",
        name="pk_project_prompt_defaults",
    ),
)

sa.Index(
    "ix_project_prompt_heads_directory",
    project_prompt_heads.c.organization_id,
    project_prompt_heads.c.project_id,
    project_prompt_heads.c.status,
    project_prompt_heads.c.kind,
    project_prompt_heads.c.prompt_id,
)
sa.Index(
    "ix_project_prompt_versions_history",
    project_prompt_versions.c.organization_id,
    project_prompt_versions.c.project_id,
    project_prompt_versions.c.prompt_id,
    project_prompt_versions.c.version,
)


task_store_state = sa.Table(
    "task_store_state",
    metadata,
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
        server_default=sa.func.now(),
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


task_intakes = sa.Table(
    "task_intakes",
    metadata,
    sa.Column("organization_id", sa.Text(), nullable=False),
    sa.Column("project_id", sa.Text(), nullable=False),
    sa.Column("intake_id", sa.Text(), nullable=False),
    sa.Column("intake_kind", sa.Text(), nullable=False),
    sa.Column("source_name", sa.Text(), nullable=False),
    sa.Column("payload_digest", sa.Text(), nullable=False),
    sa.Column("task_count", sa.Integer(), nullable=False),
    sa.Column(
        "task_ids",
        postgresql.JSONB(astext_type=sa.Text()),
        nullable=False,
    ),
    sa.Column("created_by_user_id", sa.Text(), nullable=False),
    sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    ),
    sa.CheckConstraint(
        "btrim(intake_id) <> '' AND btrim(source_name) <> '' "
        "AND btrim(created_by_user_id) <> ''",
        name="ck_task_intakes_identity_nonempty",
    ),
    sa.CheckConstraint(
        "intake_kind IN ('manual', 'row_import')",
        name="ck_task_intakes_kind",
    ),
    sa.CheckConstraint(
        "payload_digest ~ '^[0-9a-f]{64}$'",
        name="ck_task_intakes_payload_digest",
    ),
    sa.CheckConstraint(
        "task_count > 0",
        name="ck_task_intakes_task_count",
    ),
    sa.CheckConstraint(
        "jsonb_typeof(task_ids) = 'array' "
        "AND jsonb_array_length(task_ids) = task_count",
        name="ck_task_intakes_task_ids",
    ),
    sa.ForeignKeyConstraint(
        ["organization_id", "project_id"],
        [
            "project_ownership.organization_id",
            "project_ownership.project_id",
        ],
        name="fk_task_intakes_project",
        ondelete="RESTRICT",
    ),
    sa.ForeignKeyConstraint(
        ["organization_id", "created_by_user_id"],
        [
            "workspace_users.organization_id",
            "workspace_users.user_id",
        ],
        name="fk_task_intakes_creator",
        ondelete="RESTRICT",
    ),
    sa.PrimaryKeyConstraint(
        "organization_id",
        "project_id",
        "intake_id",
        name="pk_task_intakes",
    ),
)

sa.Index(
    "ix_task_intakes_project_created",
    task_intakes.c.organization_id,
    task_intakes.c.project_id,
    task_intakes.c.created_at,
    task_intakes.c.intake_id,
)


article_tasks = sa.Table(
    "article_tasks",
    metadata,
    sa.Column("organization_id", sa.Text(), nullable=False),
    sa.Column("project_id", sa.Text(), nullable=False),
    sa.Column("task_id", sa.Text(), nullable=False),
    sa.Column("customer", sa.Text(), nullable=False, server_default=sa.text("''")),
    sa.Column("topic_index", sa.Integer(), nullable=False, server_default=sa.text("0")),
    sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("0")),
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
        server_default=sa.func.now(),
    ),
    sa.Column(
        "updated_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
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

sa.Index(
    "ix_article_tasks_customer",
    article_tasks.c.organization_id,
    article_tasks.c.project_id,
    article_tasks.c.customer,
    article_tasks.c.topic_index,
    article_tasks.c.position,
)
sa.Index(
    "ix_article_tasks_record_updated",
    article_tasks.c.organization_id,
    article_tasks.c.project_id,
    article_tasks.c.record_updated_at,
)


job_batches = sa.Table(
    "job_batches",
    metadata,
    sa.Column("organization_id", sa.Text(), nullable=False),
    sa.Column("project_id", sa.Text(), nullable=False),
    sa.Column("batch_id", sa.Text(), nullable=False),
    sa.Column("operation", sa.Text(), nullable=False),
    sa.Column("customer", sa.Text(), nullable=False, server_default=sa.text("''")),
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

sa.Index(
    "ix_job_batches_project_created",
    job_batches.c.organization_id,
    job_batches.c.project_id,
    job_batches.c.created_at,
)


background_jobs = sa.Table(
    "background_jobs",
    metadata,
    sa.Column("organization_id", sa.Text(), nullable=False),
    sa.Column("project_id", sa.Text(), nullable=False),
    sa.Column("job_id", sa.Text(), nullable=False),
    sa.Column("batch_id", sa.Text(), nullable=False),
    sa.Column("task_id", sa.Text(), nullable=False),
    sa.Column("requested_by_user_id", sa.Text(), nullable=True),
    sa.Column("customer", sa.Text(), nullable=False, server_default=sa.text("''")),
    sa.Column("topic_index", sa.Integer(), nullable=False, server_default=sa.text("0")),
    sa.Column("topic", sa.Text(), nullable=False, server_default=sa.text("''")),
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
    sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
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
        server_default=sa.func.now(),
    ),
    sa.Column(
        "cancel_requested",
        sa.Boolean(),
        nullable=False,
        server_default=sa.false(),
    ),
    sa.Column("error", sa.Text(), nullable=False, server_default=sa.text("''")),
    sa.Column("worker_id", sa.Text(), nullable=True),
    sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    ),
    sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column(
        "updated_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    ),
    sa.CheckConstraint(
        "btrim(job_id) <> '' AND btrim(operation) <> ''",
        name="ck_background_jobs_identity_nonempty",
    ),
    sa.CheckConstraint(
        "requested_by_user_id IS NULL "
        "OR btrim(requested_by_user_id) <> ''",
        name="ck_background_jobs_requester_nonempty",
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
    sa.ForeignKeyConstraint(
        ["organization_id", "requested_by_user_id"],
        [
            "workspace_users.organization_id",
            "workspace_users.user_id",
        ],
        name="fk_background_jobs_requester",
        ondelete="RESTRICT",
    ),
    sa.PrimaryKeyConstraint(
        "organization_id",
        "project_id",
        "job_id",
        name="pk_background_jobs",
    ),
)

sa.Index(
    "ix_background_jobs_batch",
    background_jobs.c.organization_id,
    background_jobs.c.project_id,
    background_jobs.c.batch_id,
    background_jobs.c.created_at,
)
sa.Index(
    "ix_background_jobs_runnable",
    background_jobs.c.organization_id,
    background_jobs.c.project_id,
    background_jobs.c.status,
    background_jobs.c.available_at,
    background_jobs.c.created_at,
)
sa.Index(
    "ix_background_jobs_requester",
    background_jobs.c.organization_id,
    background_jobs.c.project_id,
    background_jobs.c.requested_by_user_id,
    background_jobs.c.created_at,
)
sa.Index(
    "uq_background_jobs_active_task",
    background_jobs.c.organization_id,
    background_jobs.c.project_id,
    background_jobs.c.task_id,
    unique=True,
    postgresql_where=background_jobs.c.status.in_(
        ("queued", "running", "retry_wait")
    ),
)

object_orphan_observations = sa.Table(
    "object_orphan_observations",
    metadata,
    sa.Column("organization_id", sa.Text(), nullable=False),
    sa.Column("project_id", sa.Text(), nullable=False),
    sa.Column("object_key", sa.Text(), nullable=False),
    sa.Column("fingerprint", sa.Text(), nullable=False),
    sa.Column("byte_size", sa.BigInteger(), nullable=False),
    sa.Column("object_last_modified_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("registered_asset_count", sa.Integer(), nullable=False),
    sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
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

sa.Index(
    "ix_object_orphan_observations_eligibility",
    object_orphan_observations.c.organization_id,
    object_orphan_observations.c.project_id,
    object_orphan_observations.c.first_seen_at,
    object_orphan_observations.c.sighting_count,
)


assistant_conversations = sa.Table(
    "assistant_conversations",
    metadata,
    sa.Column("organization_id", sa.Text(), nullable=False),
    sa.Column("conversation_id", sa.Text(), nullable=False),
    sa.Column("creator_user_id", sa.Text(), nullable=False),
    sa.Column("title", sa.Text(), nullable=False),
    sa.Column(
        "last_project_ids",
        postgresql.JSONB(astext_type=sa.Text()),
        nullable=False,
        server_default=sa.text("'[]'::jsonb"),
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
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint(
        "btrim(conversation_id) <> '' AND btrim(creator_user_id) <> '' "
        "AND btrim(title) <> ''",
        name="ck_assistant_conversations_identity_nonempty",
    ),
    sa.ForeignKeyConstraint(
        ["organization_id"],
        ["organizations.organization_id"],
        name="fk_assistant_conversations_organization",
        ondelete="RESTRICT",
    ),
    sa.ForeignKeyConstraint(
        ["organization_id", "creator_user_id"],
        ["workspace_users.organization_id", "workspace_users.user_id"],
        name="fk_assistant_conversations_creator",
        ondelete="RESTRICT",
    ),
    sa.PrimaryKeyConstraint(
        "organization_id",
        "conversation_id",
        name="pk_assistant_conversations",
    ),
    sa.UniqueConstraint(
        "conversation_id",
        name="uq_assistant_conversations_conversation_id",
    ),
    sa.UniqueConstraint(
        "organization_id",
        "conversation_id",
        "creator_user_id",
        name="uq_assistant_conversations_creator_scope",
    ),
)

sa.Index(
    "ix_assistant_conversations_creator",
    assistant_conversations.c.organization_id,
    assistant_conversations.c.creator_user_id,
    assistant_conversations.c.updated_at,
)
sa.Index(
    "ix_assistant_conversations_expiry",
    assistant_conversations.c.expires_at,
)


assistant_messages = sa.Table(
    "assistant_messages",
    metadata,
    sa.Column("organization_id", sa.Text(), nullable=False),
    sa.Column("conversation_id", sa.Text(), nullable=False),
    sa.Column("message_id", sa.Text(), nullable=False),
    sa.Column("sequence", sa.Integer(), nullable=False),
    sa.Column("role", sa.Text(), nullable=False),
    sa.Column("sanitized_content", sa.Text(), nullable=False),
    sa.Column("request_id", sa.Text(), nullable=False),
    sa.Column("idempotency_key", sa.Text(), nullable=False),
    sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    ),
    sa.CheckConstraint(
        "btrim(message_id) <> '' AND btrim(request_id) <> '' "
        "AND btrim(idempotency_key) <> '' "
        "AND btrim(sanitized_content) <> ''",
        name="ck_assistant_messages_identity_nonempty",
    ),
    sa.CheckConstraint(
        "role IN ('user', 'assistant', 'system')",
        name="ck_assistant_messages_role",
    ),
    sa.CheckConstraint(
        "sequence > 0",
        name="ck_assistant_messages_sequence",
    ),
    sa.ForeignKeyConstraint(
        ["organization_id", "conversation_id"],
        [
            "assistant_conversations.organization_id",
            "assistant_conversations.conversation_id",
        ],
        name="fk_assistant_messages_conversation",
        ondelete="CASCADE",
    ),
    sa.PrimaryKeyConstraint(
        "organization_id",
        "conversation_id",
        "message_id",
        name="pk_assistant_messages",
    ),
    sa.UniqueConstraint(
        "organization_id",
        "conversation_id",
        "sequence",
        name="uq_assistant_messages_sequence",
    ),
    sa.UniqueConstraint(
        "organization_id",
        "conversation_id",
        "idempotency_key",
        name="uq_assistant_messages_idempotency",
    ),
)


project_topics = sa.Table(
    "project_topics",
    metadata,
    sa.Column("organization_id", sa.Text(), nullable=False),
    sa.Column("project_id", sa.Text(), nullable=False),
    sa.Column("topic_id", sa.Text(), nullable=False),
    sa.Column("topic", sa.Text(), nullable=False),
    sa.Column("primary_keyword", sa.Text(), nullable=False, server_default=sa.text("''")),
    sa.Column("competitor_keyword", sa.Text(), nullable=False, server_default=sa.text("''")),
    sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'published'")),
    sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("0")),
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
        "btrim(topic_id) <> '' AND btrim(topic) <> ''",
        name="ck_project_topics_identity_nonempty",
    ),
    sa.CheckConstraint(
        "status IN ('published', 'archived')",
        name="ck_project_topics_status",
    ),
    sa.CheckConstraint("revision >= 0", name="ck_project_topics_revision"),
    sa.ForeignKeyConstraint(
        ["organization_id", "project_id"],
        ["project_ownership.organization_id", "project_ownership.project_id"],
        name="fk_project_topics_project",
        ondelete="RESTRICT",
    ),
    sa.PrimaryKeyConstraint(
        "organization_id",
        "project_id",
        "topic_id",
        name="pk_project_topics",
    ),
)

sa.Index(
    "ix_project_topics_project_status",
    project_topics.c.organization_id,
    project_topics.c.project_id,
    project_topics.c.status,
    project_topics.c.topic_id,
)


workflow_plans = sa.Table(
    "workflow_plans",
    metadata,
    sa.Column("organization_id", sa.Text(), nullable=False),
    sa.Column("plan_id", sa.Text(), nullable=False),
    sa.Column("creator_user_id", sa.Text(), nullable=False),
    sa.Column("conversation_id", sa.Text(), nullable=False),
    sa.Column("source_idempotency_key", sa.Text(), nullable=True),
    sa.Column("natural_language_request", sa.Text(), nullable=False),
    sa.Column(
        "normalized_plan",
        postgresql.JSONB(astext_type=sa.Text()),
        nullable=False,
    ),
    sa.Column("plan_hash", sa.Text(), nullable=False),
    sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("0")),
    sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'draft'")),
    sa.Column(
        "concurrency_limit",
        sa.Integer(),
        nullable=False,
        server_default=sa.text("5"),
    ),
    sa.Column(
        "budget_warning",
        sa.Boolean(),
        nullable=False,
        server_default=sa.false(),
    ),
    sa.Column("approved_by", sa.Text(), nullable=True),
    sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column(
        "attention_state",
        sa.Text(),
        nullable=False,
        server_default=sa.text("'none'"),
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
        "btrim(plan_id) <> '' AND btrim(creator_user_id) <> '' "
        "AND btrim(natural_language_request) <> ''",
        name="ck_workflow_plans_identity_nonempty",
    ),
    sa.CheckConstraint(
        "source_idempotency_key IS NULL OR btrim(source_idempotency_key) <> ''",
        name="ck_workflow_plans_source_idempotency_nonempty",
    ),
    sa.CheckConstraint(
        "plan_hash ~ '^[0-9a-f]{64}$'",
        name="ck_workflow_plans_hash",
    ),
    sa.CheckConstraint(
        "revision >= 0 AND concurrency_limit > 0",
        name="ck_workflow_plans_revision_limit",
    ),
    sa.CheckConstraint(
        "status IN ('draft', 'awaiting_confirmation', 'queued', 'running', "
        "'waiting_review', 'paused', 'completed', 'failed', 'cancelled')",
        name="ck_workflow_plans_status",
    ),
    sa.CheckConstraint(
        "attention_state IN ('none', 'user_confirmation', 'error', 'unread')",
        name="ck_workflow_plans_attention_state",
    ),
    sa.CheckConstraint(
        "approved_at IS NULL OR approved_by IS NOT NULL",
        name="ck_workflow_plans_approval_identity",
    ),
    sa.ForeignKeyConstraint(
        ["organization_id", "creator_user_id"],
        ["workspace_users.organization_id", "workspace_users.user_id"],
        name="fk_workflow_plans_creator",
        ondelete="RESTRICT",
    ),
    sa.ForeignKeyConstraint(
        ["organization_id", "conversation_id"],
        [
            "assistant_conversations.organization_id",
            "assistant_conversations.conversation_id",
        ],
        name="fk_workflow_plans_conversation",
        ondelete="RESTRICT",
    ),
    sa.ForeignKeyConstraint(
        ["organization_id", "approved_by"],
        ["workspace_users.organization_id", "workspace_users.user_id"],
        name="fk_workflow_plans_approver",
        ondelete="RESTRICT",
    ),
    sa.PrimaryKeyConstraint(
        "organization_id",
        "plan_id",
        name="pk_workflow_plans",
    ),
    sa.UniqueConstraint("plan_id", name="uq_workflow_plans_plan_id"),
    sa.UniqueConstraint(
        "organization_id",
        "plan_id",
        "creator_user_id",
        name="uq_workflow_plans_creator_scope",
    ),
    sa.UniqueConstraint(
        "organization_id",
        "conversation_id",
        "source_idempotency_key",
        name="uq_workflow_plans_source_idempotency",
    ),
)

sa.Index(
    "ix_workflow_plans_creator_status",
    workflow_plans.c.organization_id,
    workflow_plans.c.creator_user_id,
    workflow_plans.c.status,
    workflow_plans.c.updated_at,
)


workflow_assistant_dispatches = sa.Table(
    "workflow_assistant_dispatches",
    metadata,
    sa.Column("organization_id", sa.Text(), nullable=False),
    sa.Column("dispatch_id", sa.Text(), nullable=False),
    sa.Column("creator_user_id", sa.Text(), nullable=False),
    sa.Column("conversation_id", sa.Text(), nullable=False),
    sa.Column("request_id", sa.Text(), nullable=False),
    sa.Column("idempotency_key", sa.Text(), nullable=False),
    sa.Column("content", sa.Text(), nullable=False),
    sa.Column(
        "project_ids",
        postgresql.JSONB(astext_type=sa.Text()),
        nullable=False,
        server_default=sa.text("'[]'::jsonb"),
    ),
    sa.Column(
        "article_task_ids",
        postgresql.JSONB(astext_type=sa.Text()),
        nullable=False,
        server_default=sa.text("'[]'::jsonb"),
    ),
    sa.Column(
        "article_task_selection_locked",
        sa.Boolean(),
        nullable=False,
        server_default=sa.false(),
    ),
    sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'queued'")),
    sa.Column("plan_id", sa.Text(), nullable=True),
    sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
    sa.Column("max_attempts", sa.Integer(), nullable=False, server_default=sa.text("3")),
    sa.Column(
        "available_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    ),
    sa.Column("worker_id", sa.Text(), nullable=True),
    sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("error_code", sa.Text(), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.CheckConstraint(
        "btrim(dispatch_id) <> '' AND btrim(creator_user_id) <> '' "
        "AND btrim(conversation_id) <> '' AND btrim(request_id) <> '' "
        "AND btrim(idempotency_key) <> '' AND btrim(content) <> ''",
        name="ck_workflow_assistant_dispatches_identity_nonempty",
    ),
    sa.CheckConstraint(
        "jsonb_typeof(project_ids) = 'array' "
        "AND jsonb_typeof(article_task_ids) = 'array'",
        name="ck_workflow_assistant_dispatches_scope_arrays",
    ),
    sa.CheckConstraint(
        "status IN ('queued', 'running', 'succeeded', 'failed')",
        name="ck_workflow_assistant_dispatches_status",
    ),
    sa.CheckConstraint(
        "attempts >= 0 AND max_attempts > 0",
        name="ck_workflow_assistant_dispatches_attempts",
    ),
    sa.CheckConstraint(
        "(status = 'running' AND worker_id IS NOT NULL "
        "AND lease_expires_at IS NOT NULL) OR "
        "(status <> 'running' AND worker_id IS NULL "
        "AND lease_expires_at IS NULL)",
        name="ck_workflow_assistant_dispatches_lease_state",
    ),
    sa.ForeignKeyConstraint(
        ["organization_id", "creator_user_id"],
        ["workspace_users.organization_id", "workspace_users.user_id"],
        name="fk_workflow_assistant_dispatches_creator",
        ondelete="RESTRICT",
    ),
    sa.ForeignKeyConstraint(
        ["organization_id", "conversation_id"],
        [
            "assistant_conversations.organization_id",
            "assistant_conversations.conversation_id",
        ],
        name="fk_workflow_assistant_dispatches_conversation",
        ondelete="CASCADE",
    ),
    sa.ForeignKeyConstraint(
        ["organization_id", "plan_id"],
        ["workflow_plans.organization_id", "workflow_plans.plan_id"],
        name="fk_workflow_assistant_dispatches_plan",
        ondelete="SET NULL",
    ),
    sa.PrimaryKeyConstraint(
        "organization_id",
        "dispatch_id",
        name="pk_workflow_assistant_dispatches",
    ),
    sa.UniqueConstraint(
        "organization_id",
        "conversation_id",
        "idempotency_key",
        name="uq_workflow_assistant_dispatches_idempotency",
    ),
)

sa.Index(
    "ix_workflow_assistant_dispatches_runnable",
    workflow_assistant_dispatches.c.status,
    workflow_assistant_dispatches.c.available_at,
    workflow_assistant_dispatches.c.created_at,
)
sa.Index(
    "ix_workflow_assistant_dispatches_creator",
    workflow_assistant_dispatches.c.organization_id,
    workflow_assistant_dispatches.c.creator_user_id,
    workflow_assistant_dispatches.c.updated_at,
)


assistant_attachments = sa.Table(
    "assistant_attachments",
    metadata,
    sa.Column("organization_id", sa.Text(), nullable=False),
    sa.Column("attachment_id", sa.Text(), nullable=False),
    sa.Column("creator_user_id", sa.Text(), nullable=False),
    sa.Column("conversation_id", sa.Text(), nullable=False),
    sa.Column("proposed_project_id", sa.Text(), nullable=True),
    sa.Column("plan_id", sa.Text(), nullable=True),
    sa.Column("idempotency_key", sa.Text(), nullable=False),
    sa.Column("object_key", sa.Text(), nullable=False),
    sa.Column("original_filename", sa.Text(), nullable=False),
    sa.Column("mime_type", sa.Text(), nullable=False),
    sa.Column("byte_size", sa.BigInteger(), nullable=False),
    sa.Column("sha256", sa.Text(), nullable=False),
    sa.Column("classification", sa.Text(), nullable=True),
    sa.Column(
        "classification_payload",
        postgresql.JSONB(astext_type=sa.Text()),
        nullable=False,
        server_default=sa.text("'{}'::jsonb"),
    ),
    sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("0")),
    sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'uploaded'")),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
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
        "btrim(attachment_id) <> '' AND btrim(creator_user_id) <> '' "
        "AND btrim(idempotency_key) <> '' AND btrim(object_key) <> '' "
        "AND btrim(original_filename) <> '' AND btrim(mime_type) <> ''",
        name="ck_assistant_attachments_identity_nonempty",
    ),
    sa.CheckConstraint(
        "byte_size >= 0 AND revision >= 0",
        name="ck_assistant_attachments_size_revision",
    ),
    sa.CheckConstraint(
        "sha256 ~ '^[0-9a-f]{64}$'",
        name="ck_assistant_attachments_sha256",
    ),
    sa.CheckConstraint(
        "classification IS NULL OR classification IN "
        "('knowledge_source', 'prompt_asset', 'task_workbook', "
        "'project_notes', 'topic_library', 'unsupported', "
        "'needs_user_choice')",
        name="ck_assistant_attachments_classification",
    ),
    sa.CheckConstraint(
        "status IN ('uploading', 'uploaded', 'classifying', 'needs_user_choice', "
        "'proposal_ready', 'importing', 'imported', 'rejected', "
        "'expired', 'rejecting', 'expiring', 'failed')",
        name="ck_assistant_attachments_status",
    ),
    sa.CheckConstraint(
        "expires_at > created_at",
        name="ck_assistant_attachments_expiry",
    ),
    sa.ForeignKeyConstraint(
        ["organization_id"],
        ["organizations.organization_id"],
        name="fk_assistant_attachments_organization",
        ondelete="RESTRICT",
    ),
    sa.ForeignKeyConstraint(
        ["organization_id", "creator_user_id"],
        ["workspace_users.organization_id", "workspace_users.user_id"],
        name="fk_assistant_attachments_creator",
        ondelete="RESTRICT",
    ),
    sa.ForeignKeyConstraint(
        ["organization_id", "conversation_id", "creator_user_id"],
        [
            "assistant_conversations.organization_id",
            "assistant_conversations.conversation_id",
            "assistant_conversations.creator_user_id",
        ],
        name="fk_assistant_attachments_conversation",
        ondelete="RESTRICT",
    ),
    sa.ForeignKeyConstraint(
        ["organization_id", "proposed_project_id"],
        ["project_ownership.organization_id", "project_ownership.project_id"],
        name="fk_assistant_attachments_proposed_project",
        ondelete="RESTRICT",
    ),
    sa.ForeignKeyConstraint(
        ["organization_id", "plan_id", "creator_user_id"],
        [
            "workflow_plans.organization_id",
            "workflow_plans.plan_id",
            "workflow_plans.creator_user_id",
        ],
        name="fk_assistant_attachments_plan",
        ondelete="RESTRICT",
    ),
    sa.PrimaryKeyConstraint(
        "organization_id",
        "attachment_id",
        name="pk_assistant_attachments",
    ),
    sa.UniqueConstraint(
        "organization_id",
        "creator_user_id",
        "conversation_id",
        "idempotency_key",
        name="uq_assistant_attachments_idempotency",
    ),
    sa.UniqueConstraint(
        "organization_id",
        "attachment_id",
        "creator_user_id",
        name="uq_assistant_attachments_creator_scope",
    ),
)

sa.Index(
    "ix_assistant_attachments_creator_status",
    assistant_attachments.c.organization_id,
    assistant_attachments.c.creator_user_id,
    assistant_attachments.c.status,
    assistant_attachments.c.updated_at,
)
sa.Index(
    "ix_assistant_attachments_expiry",
    assistant_attachments.c.status,
    assistant_attachments.c.expires_at,
)
sa.Index(
    "ix_assistant_attachments_project",
    assistant_attachments.c.organization_id,
    assistant_attachments.c.proposed_project_id,
    assistant_attachments.c.updated_at,
)


assistant_import_proposals = sa.Table(
    "assistant_import_proposals",
    metadata,
    sa.Column("organization_id", sa.Text(), nullable=False),
    sa.Column("proposal_id", sa.Text(), nullable=False),
    sa.Column("attachment_id", sa.Text(), nullable=False),
    sa.Column("creator_user_id", sa.Text(), nullable=False),
    sa.Column("target_project_id", sa.Text(), nullable=True),
    sa.Column("plan_id", sa.Text(), nullable=True),
    sa.Column("target_kind", sa.Text(), nullable=False),
    sa.Column("idempotency_key", sa.Text(), nullable=False),
    sa.Column("execution_idempotency_key", sa.Text(), nullable=True),
    sa.Column(
        "normalized_diff",
        postgresql.JSONB(astext_type=sa.Text()),
        nullable=False,
        server_default=sa.text("'{}'::jsonb"),
    ),
    sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("0")),
    sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'draft'")),
    sa.Column("confirmed_by", sa.Text(), nullable=True),
    sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column(
        "resulting_entity_refs",
        postgresql.JSONB(astext_type=sa.Text()),
        nullable=False,
        server_default=sa.text("'[]'::jsonb"),
    ),
    sa.Column("standardized_error_code", sa.Text(), nullable=True),
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
        "btrim(proposal_id) <> '' AND btrim(attachment_id) <> '' "
        "AND btrim(creator_user_id) <> '' AND btrim(target_kind) <> '' "
        "AND btrim(idempotency_key) <> ''",
        name="ck_assistant_import_proposals_identity_nonempty",
    ),
    sa.CheckConstraint(
        "revision >= 0",
        name="ck_assistant_import_proposals_revision",
    ),
    sa.CheckConstraint(
        "target_kind IN ('knowledge_source', 'prompt_asset', 'task_workbook', "
        "'project_notes', 'topic_library', 'needs_user_choice')",
        name="ck_assistant_import_proposals_target_kind",
    ),
    sa.CheckConstraint(
        "status IN ('draft', 'awaiting_confirmation', 'confirmed', 'running', "
        "'waiting_publication', 'completed', 'failed', 'cancelled')",
        name="ck_assistant_import_proposals_status",
    ),
    sa.CheckConstraint(
        "(confirmed_at IS NULL) = (confirmed_by IS NULL)",
        name="ck_assistant_import_proposals_confirmation",
    ),
    sa.CheckConstraint(
        "status NOT IN ('confirmed', 'running', 'waiting_publication', "
        "'completed') OR (target_project_id IS NOT NULL "
        "AND target_kind <> 'needs_user_choice' "
        "AND confirmed_by IS NOT NULL AND confirmed_at IS NOT NULL)",
        name="ck_assistant_import_proposals_runnable_target",
    ),
    sa.CheckConstraint(
        "standardized_error_code IS NULL "
        "OR btrim(standardized_error_code) <> ''",
        name="ck_assistant_import_proposals_error_nonempty",
    ),
    sa.ForeignKeyConstraint(
        ["organization_id"],
        ["organizations.organization_id"],
        name="fk_assistant_import_proposals_organization",
        ondelete="RESTRICT",
    ),
    sa.ForeignKeyConstraint(
        ["organization_id", "attachment_id", "creator_user_id"],
        [
            "assistant_attachments.organization_id",
            "assistant_attachments.attachment_id",
            "assistant_attachments.creator_user_id",
        ],
        name="fk_assistant_import_proposals_attachment",
        ondelete="CASCADE",
    ),
    sa.ForeignKeyConstraint(
        ["organization_id", "creator_user_id"],
        ["workspace_users.organization_id", "workspace_users.user_id"],
        name="fk_assistant_import_proposals_creator",
        ondelete="RESTRICT",
    ),
    sa.ForeignKeyConstraint(
        ["organization_id", "target_project_id"],
        ["project_ownership.organization_id", "project_ownership.project_id"],
        name="fk_assistant_import_proposals_target_project",
        ondelete="RESTRICT",
    ),
    sa.ForeignKeyConstraint(
        ["organization_id", "plan_id", "creator_user_id"],
        [
            "workflow_plans.organization_id",
            "workflow_plans.plan_id",
            "workflow_plans.creator_user_id",
        ],
        name="fk_assistant_import_proposals_plan",
        ondelete="RESTRICT",
    ),
    sa.ForeignKeyConstraint(
        ["organization_id", "confirmed_by"],
        ["workspace_users.organization_id", "workspace_users.user_id"],
        name="fk_assistant_import_proposals_confirmer",
        ondelete="RESTRICT",
    ),
    sa.PrimaryKeyConstraint(
        "organization_id",
        "proposal_id",
        name="pk_assistant_import_proposals",
    ),
    sa.UniqueConstraint(
        "organization_id",
        "attachment_id",
        "idempotency_key",
        name="uq_assistant_import_proposals_idempotency",
    ),
)

sa.Index(
    "ix_assistant_import_proposals_creator_status",
    assistant_import_proposals.c.organization_id,
    assistant_import_proposals.c.creator_user_id,
    assistant_import_proposals.c.status,
    assistant_import_proposals.c.updated_at,
)
sa.Index(
    "ix_assistant_import_proposals_attachment",
    assistant_import_proposals.c.organization_id,
    assistant_import_proposals.c.attachment_id,
    assistant_import_proposals.c.updated_at,
)
sa.Index(
    "ix_assistant_import_proposals_project",
    assistant_import_proposals.c.organization_id,
    assistant_import_proposals.c.target_project_id,
    assistant_import_proposals.c.status,
)


assistant_attachment_jobs = sa.Table(
    "assistant_attachment_jobs",
    metadata,
    sa.Column("organization_id", sa.Text(), nullable=False),
    sa.Column("job_id", sa.Text(), nullable=False),
    sa.Column("requested_by_user_id", sa.Text(), nullable=False),
    sa.Column("project_id", sa.Text(), nullable=True),
    sa.Column("attachment_id", sa.Text(), nullable=False),
    sa.Column("proposal_id", sa.Text(), nullable=True),
    sa.Column("operation", sa.Text(), nullable=False),
    sa.Column("idempotency_key", sa.Text(), nullable=False),
    sa.Column("expected_attachment_revision", sa.Integer(), nullable=False),
    sa.Column("expected_proposal_revision", sa.Integer(), nullable=True),
    sa.Column(
        "request_payload",
        postgresql.JSONB(astext_type=sa.Text()),
        nullable=False,
        server_default=sa.text("'{}'::jsonb"),
    ),
    sa.Column(
        "result_payload",
        postgresql.JSONB(astext_type=sa.Text()),
        nullable=False,
        server_default=sa.text("'{}'::jsonb"),
    ),
    sa.Column("result_attachment_revision", sa.Integer(), nullable=True),
    sa.Column("result_proposal_revision", sa.Integer(), nullable=True),
    sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'queued'")),
    sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
    sa.Column("max_attempts", sa.Integer(), nullable=False, server_default=sa.text("4")),
    sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
    sa.Column("standardized_error_code", sa.Text(), nullable=True),
    sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("worker_id", sa.Text(), nullable=True),
    sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
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
        "btrim(job_id) <> '' AND btrim(requested_by_user_id) <> '' "
        "AND btrim(attachment_id) <> '' AND btrim(operation) <> '' "
        "AND btrim(idempotency_key) <> ''",
        name="ck_assistant_attachment_jobs_identity_nonempty",
    ),
    sa.CheckConstraint(
        "operation IN ('classify_attachment', 'preview_import_proposal', "
        "'execute_import_proposal')",
        name="ck_assistant_attachment_jobs_operation",
    ),
    sa.CheckConstraint(
        "status IN ('queued', 'running', 'retry_wait', 'succeeded', 'failed', "
        "'cancelled', 'conflict')",
        name="ck_assistant_attachment_jobs_status",
    ),
    sa.CheckConstraint(
        "expected_attachment_revision >= 0 "
        "AND (expected_proposal_revision IS NULL OR expected_proposal_revision >= 0) "
        "AND (result_attachment_revision IS NULL OR result_attachment_revision >= 0) "
        "AND (result_proposal_revision IS NULL OR result_proposal_revision >= 0) "
        "AND attempts >= 0 AND max_attempts > 0 AND attempts <= max_attempts",
        name="ck_assistant_attachment_jobs_revisions_attempts",
    ),
    sa.CheckConstraint(
        "(operation = 'classify_attachment' AND proposal_id IS NULL "
        "AND expected_proposal_revision IS NULL) OR "
        "(operation = 'preview_import_proposal' AND project_id IS NOT NULL "
        "AND proposal_id IS NULL AND expected_proposal_revision IS NULL) OR "
        "(operation = 'execute_import_proposal' AND project_id IS NOT NULL "
        "AND proposal_id IS NOT NULL AND expected_proposal_revision IS NOT NULL)",
        name="ck_assistant_attachment_jobs_target_shape",
    ),
    sa.CheckConstraint(
        "((status = 'running') = (worker_id IS NOT NULL "
        "AND lease_expires_at IS NOT NULL))",
        name="ck_assistant_attachment_jobs_lease_state",
    ),
    sa.CheckConstraint(
        "(status IN ('succeeded', 'failed', 'cancelled', 'conflict')) "
        "= (finished_at IS NOT NULL)",
        name="ck_assistant_attachment_jobs_finished_state",
    ),
    sa.CheckConstraint(
        "standardized_error_code IS NULL OR btrim(standardized_error_code) <> ''",
        name="ck_assistant_attachment_jobs_error_nonempty",
    ),
    sa.ForeignKeyConstraint(
        ["organization_id", "requested_by_user_id"],
        ["workspace_users.organization_id", "workspace_users.user_id"],
        name="fk_assistant_attachment_jobs_requester",
        ondelete="RESTRICT",
    ),
    sa.ForeignKeyConstraint(
        ["organization_id", "project_id"],
        ["project_ownership.organization_id", "project_ownership.project_id"],
        name="fk_assistant_attachment_jobs_project",
        ondelete="RESTRICT",
    ),
    sa.ForeignKeyConstraint(
        ["organization_id", "attachment_id", "requested_by_user_id"],
        [
            "assistant_attachments.organization_id",
            "assistant_attachments.attachment_id",
            "assistant_attachments.creator_user_id",
        ],
        name="fk_assistant_attachment_jobs_attachment",
        ondelete="CASCADE",
    ),
    sa.ForeignKeyConstraint(
        ["organization_id", "proposal_id"],
        ["assistant_import_proposals.organization_id", "assistant_import_proposals.proposal_id"],
        name="fk_assistant_attachment_jobs_proposal",
        ondelete="CASCADE",
    ),
    sa.PrimaryKeyConstraint(
        "organization_id", "job_id", name="pk_assistant_attachment_jobs"
    ),
    sa.UniqueConstraint(
        "organization_id",
        "requested_by_user_id",
        "operation",
        "idempotency_key",
        name="uq_assistant_attachment_jobs_idempotency",
    ),
)

sa.Index(
    "ix_assistant_attachment_jobs_claim",
    assistant_attachment_jobs.c.status,
    assistant_attachment_jobs.c.available_at,
    assistant_attachment_jobs.c.created_at,
)
sa.Index(
    "uq_assistant_attachment_jobs_active_attachment",
    assistant_attachment_jobs.c.organization_id,
    assistant_attachment_jobs.c.attachment_id,
    unique=True,
    postgresql_where=assistant_attachment_jobs.c.status.in_(
        ("queued", "running", "retry_wait")
    ),
)
sa.Index(
    "uq_assistant_attachment_jobs_active_proposal",
    assistant_attachment_jobs.c.organization_id,
    assistant_attachment_jobs.c.proposal_id,
    unique=True,
    postgresql_where=sa.and_(
        assistant_attachment_jobs.c.proposal_id.is_not(None),
        assistant_attachment_jobs.c.status.in_(("queued", "running", "retry_wait")),
    ),
)


workflow_plan_projects = sa.Table(
    "workflow_plan_projects",
    metadata,
    sa.Column("organization_id", sa.Text(), nullable=False),
    sa.Column("plan_id", sa.Text(), nullable=False),
    sa.Column("project_id", sa.Text(), nullable=False),
    sa.Column(
        "authorization_snapshot",
        postgresql.JSONB(astext_type=sa.Text()),
        nullable=False,
        server_default=sa.text("'{}'::jsonb"),
    ),
    sa.Column("paused", sa.Boolean(), nullable=False, server_default=sa.false()),
    sa.ForeignKeyConstraint(
        ["organization_id", "plan_id"],
        ["workflow_plans.organization_id", "workflow_plans.plan_id"],
        name="fk_workflow_plan_projects_plan",
        ondelete="CASCADE",
    ),
    sa.ForeignKeyConstraint(
        ["organization_id", "project_id"],
        ["project_ownership.organization_id", "project_ownership.project_id"],
        name="fk_workflow_plan_projects_project",
        ondelete="RESTRICT",
    ),
    sa.PrimaryKeyConstraint(
        "organization_id",
        "plan_id",
        "project_id",
        name="pk_workflow_plan_projects",
    ),
)


workflow_plan_steps = sa.Table(
    "workflow_plan_steps",
    metadata,
    sa.Column("organization_id", sa.Text(), nullable=False),
    sa.Column("plan_id", sa.Text(), nullable=False),
    sa.Column("step_id", sa.Text(), nullable=False),
    sa.Column("sequence", sa.Integer(), nullable=False),
    sa.Column("action_kind", sa.Text(), nullable=False),
    sa.Column("project_id", sa.Text(), nullable=False),
    sa.Column("article_task_id", sa.Text(), nullable=True),
    sa.Column("expected_task_revision", sa.Integer(), nullable=True),
    sa.Column(
        "pinned_prompt_version",
        postgresql.JSONB(astext_type=sa.Text()),
        nullable=False,
        server_default=sa.text("'{}'::jsonb"),
    ),
    sa.Column(
        "pinned_knowledge_snapshot",
        postgresql.JSONB(astext_type=sa.Text()),
        nullable=False,
        server_default=sa.text("'{}'::jsonb"),
    ),
    sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'pending'")),
    sa.Column("background_job_id", sa.Text(), nullable=True),
    sa.Column("retry_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
    sa.Column("hard_gate", sa.Boolean(), nullable=False, server_default=sa.false()),
    sa.Column(
        "human_gate_confirmed",
        sa.Boolean(),
        nullable=False,
        server_default=sa.false(),
    ),
    sa.Column(
        "input_summary",
        postgresql.JSONB(astext_type=sa.Text()),
        nullable=False,
        server_default=sa.text("'{}'::jsonb"),
    ),
    sa.Column(
        "output_summary",
        postgresql.JSONB(astext_type=sa.Text()),
        nullable=False,
        server_default=sa.text("'{}'::jsonb"),
    ),
    sa.Column("standardized_error_code", sa.Text(), nullable=True),
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
        "btrim(step_id) <> '' AND btrim(action_kind) <> '' "
        "AND btrim(project_id) <> ''",
        name="ck_workflow_plan_steps_identity_nonempty",
    ),
    sa.CheckConstraint(
        "sequence > 0 AND retry_count >= 0 "
        "AND (expected_task_revision IS NULL OR expected_task_revision >= 0)",
        name="ck_workflow_plan_steps_sequence_retry",
    ),
    sa.CheckConstraint(
        "status IN ('pending', 'running', 'waiting_job', 'waiting_review', "
        "'succeeded', 'failed', 'skipped', 'cancelled')",
        name="ck_workflow_plan_steps_status",
    ),
    sa.ForeignKeyConstraint(
        ["organization_id", "plan_id", "project_id"],
        [
            "workflow_plan_projects.organization_id",
            "workflow_plan_projects.plan_id",
            "workflow_plan_projects.project_id",
        ],
        name="fk_workflow_plan_steps_project",
        ondelete="CASCADE",
    ),
    sa.ForeignKeyConstraint(
        ["organization_id", "project_id", "article_task_id"],
        ["article_tasks.organization_id", "article_tasks.project_id", "article_tasks.task_id"],
        name="fk_workflow_plan_steps_task",
        ondelete="RESTRICT",
    ),
    sa.PrimaryKeyConstraint(
        "organization_id",
        "plan_id",
        "step_id",
        name="pk_workflow_plan_steps",
    ),
    sa.UniqueConstraint(
        "organization_id",
        "plan_id",
        "sequence",
        name="uq_workflow_plan_steps_sequence",
    ),
)


workflow_plan_events = sa.Table(
    "workflow_plan_events",
    metadata,
    sa.Column("organization_id", sa.Text(), nullable=False),
    sa.Column("plan_id", sa.Text(), nullable=False),
    sa.Column("sequence", sa.BigInteger(), nullable=False),
    sa.Column("event_kind", sa.Text(), nullable=False),
    sa.Column(
        "public_payload",
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
        "sequence > 0 AND btrim(event_kind) <> ''",
        name="ck_workflow_plan_events_identity",
    ),
    sa.ForeignKeyConstraint(
        ["organization_id", "plan_id"],
        ["workflow_plans.organization_id", "workflow_plans.plan_id"],
        name="fk_workflow_plan_events_plan",
        ondelete="CASCADE",
    ),
    sa.PrimaryKeyConstraint(
        "organization_id",
        "plan_id",
        "sequence",
        name="pk_workflow_plan_events",
    ),
)


assistant_usage_events = sa.Table(
    "assistant_usage_events",
    metadata,
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
    sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    ),
    sa.CheckConstraint(
        "btrim(usage_event_id) <> '' AND btrim(user_id) <> '' "
        "AND btrim(provider) <> '' AND btrim(model) <> '' "
        "AND btrim(operation_kind) <> ''",
        name="ck_assistant_usage_identity_nonempty",
    ),
    sa.CheckConstraint(
        "input_tokens >= 0 AND output_tokens >= 0 "
        "AND (estimated_cost IS NULL OR estimated_cost >= 0)",
        name="ck_assistant_usage_counts",
    ),
    sa.ForeignKeyConstraint(
        ["organization_id", "user_id"],
        ["workspace_users.organization_id", "workspace_users.user_id"],
        name="fk_assistant_usage_user",
        ondelete="RESTRICT",
    ),
    sa.ForeignKeyConstraint(
        ["organization_id", "project_id"],
        ["project_ownership.organization_id", "project_ownership.project_id"],
        name="fk_assistant_usage_project",
        ondelete="RESTRICT",
    ),
    sa.ForeignKeyConstraint(
        ["organization_id", "plan_id"],
        ["workflow_plans.organization_id", "workflow_plans.plan_id"],
        name="fk_assistant_usage_plan",
        ondelete="RESTRICT",
    ),
    sa.PrimaryKeyConstraint(
        "organization_id",
        "usage_event_id",
        name="pk_assistant_usage_events",
    ),
)

sa.Index(
    "ix_assistant_usage_scope",
    assistant_usage_events.c.organization_id,
    assistant_usage_events.c.user_id,
    assistant_usage_events.c.created_at,
)


__all__ = [
    "article_tasks",
    "assistant_attachment_jobs",
    "assistant_attachments",
    "assistant_conversations",
    "assistant_import_proposals",
    "assistant_messages",
    "assistant_usage_events",
    "audit_events",
    "background_jobs",
    "external_identities",
    "job_batches",
    "organizations",
    "workspace_user_llm_settings",
    "object_orphan_observations",
    "project_memberships",
    "project_ownership",
    "project_prompt_defaults",
    "project_prompt_heads",
    "project_prompt_versions",
    "team_memberships",
    "teams",
    "task_intakes",
    "task_store_state",
    "workflow_plan_events",
    "workflow_plan_projects",
    "workflow_plan_steps",
    "workflow_plans",
    "workspace_users",
]
