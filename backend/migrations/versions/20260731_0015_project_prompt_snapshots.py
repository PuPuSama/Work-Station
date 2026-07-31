"""Add immutable project prompt snapshots and explicit active pointers.

Revision ID: 20260731_0015
Revises: 20260731_0014
Create Date: 2026-07-31
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260731_0015"
down_revision: str | Sequence[str] | None = "20260731_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "project_prompt_heads",
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
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "btrim(prompt_id) <> ''",
            name="ck_project_prompt_heads_identity_nonempty",
        ),
        sa.CheckConstraint(
            "kind IN ('outline', 'article', 'review')",
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
            [
                "project_ownership.organization_id",
                "project_ownership.project_id",
            ],
            name="fk_project_prompt_heads_project",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "organization_id",
            "project_id",
            "prompt_id",
            name="pk_project_prompt_heads",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "project_id",
            "prompt_id",
            "kind",
            name="uq_project_prompt_heads_kind",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "project_id",
            "prompt_id",
            "current_version",
            name="uq_project_prompt_heads_current_version",
        ),
    )
    op.create_table(
        "project_prompt_versions",
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
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "kind IN ('outline', 'article', 'review')",
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
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "created_by_user_id"],
            [
                "workspace_users.organization_id",
                "workspace_users.user_id",
            ],
            name="fk_project_prompt_versions_creator",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "organization_id",
            "project_id",
            "prompt_id",
            "version",
            name="pk_project_prompt_versions",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "project_id",
            "prompt_id",
            "kind",
            "version",
            name="uq_project_prompt_versions_kind",
        ),
    )
    op.create_foreign_key(
        "fk_project_prompt_heads_current_version",
        "project_prompt_heads",
        "project_prompt_versions",
        [
            "organization_id",
            "project_id",
            "prompt_id",
            "kind",
            "current_version",
        ],
        [
            "organization_id",
            "project_id",
            "prompt_id",
            "kind",
            "version",
        ],
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_table(
        "project_prompt_defaults",
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("prompt_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "kind IN ('outline', 'article', 'review')",
            name="ck_project_prompt_defaults_kind",
        ),
        sa.CheckConstraint(
            "version > 0",
            name="ck_project_prompt_defaults_version",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "project_id"],
            [
                "project_ownership.organization_id",
                "project_ownership.project_id",
            ],
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
            "organization_id",
            "project_id",
            "kind",
            name="pk_project_prompt_defaults",
        ),
    )
    op.create_index(
        "ix_project_prompt_heads_directory",
        "project_prompt_heads",
        ["organization_id", "project_id", "status", "kind", "prompt_id"],
    )
    op.create_index(
        "ix_project_prompt_versions_history",
        "project_prompt_versions",
        ["organization_id", "project_id", "prompt_id", "version"],
    )
    op.execute(
        """
        CREATE FUNCTION prevent_project_prompt_version_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'project_prompt_versions is append-only';
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_project_prompt_versions_append_only
        BEFORE UPDATE OR DELETE ON project_prompt_versions
        FOR EACH ROW
        EXECUTE FUNCTION prevent_project_prompt_version_mutation()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS "
        "trg_project_prompt_versions_append_only "
        "ON project_prompt_versions"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS "
        "prevent_project_prompt_version_mutation()"
    )
    op.drop_index(
        "ix_project_prompt_versions_history",
        table_name="project_prompt_versions",
    )
    op.drop_index(
        "ix_project_prompt_heads_directory",
        table_name="project_prompt_heads",
    )
    op.drop_table("project_prompt_defaults")
    op.drop_constraint(
        "fk_project_prompt_heads_current_version",
        "project_prompt_heads",
        type_="foreignkey",
    )
    op.drop_table("project_prompt_versions")
    op.drop_table("project_prompt_heads")
