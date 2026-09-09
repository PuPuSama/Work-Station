"""Store project WordPress credentials as encrypted server-side values."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260907_0037"
down_revision = "20260907_0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "wordpress_project_credentials",
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("app_password_ciphertext", sa.Text(), nullable=False),
        sa.Column(
            "key_version",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'v1'"),
        ),
        sa.Column(
            "revision",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
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
        sa.PrimaryKeyConstraint(
            "project_id",
            name="pk_wordpress_project_credentials",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.project_id"],
            name="fk_wordpress_project_credentials_project",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "btrim(username) <> '' AND btrim(app_password_ciphertext) <> ''",
            name="ck_wordpress_project_credentials_values_nonempty",
        ),
        sa.CheckConstraint(
            "revision >= 0",
            name="ck_wordpress_project_credentials_revision_nonnegative",
        ),
    )


def downgrade() -> None:
    op.drop_table("wordpress_project_credentials")
