"""Create immutable knowledge assets and snapshot evidence links.

Revision ID: 20260730_0002
Revises: 20260728_0001
Create Date: 2026-07-30
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260730_0002"
down_revision: str | Sequence[str] | None = "20260728_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "knowledge_assets",
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("asset_id", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("artifact_uri", sa.Text(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column(
            "metadata",
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
            "btrim(asset_id) <> ''",
            name="ck_knowledge_assets_asset_id_nonempty",
        ),
        sa.CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_knowledge_assets_content_hash_sha256",
        ),
        sa.CheckConstraint(
            "btrim(artifact_uri) <> ''",
            name="ck_knowledge_assets_artifact_uri_nonempty",
        ),
        sa.CheckConstraint(
            "btrim(content_type) <> ''",
            name="ck_knowledge_assets_content_type_nonempty",
        ),
        sa.CheckConstraint(
            "byte_size > 0",
            name="ck_knowledge_assets_byte_size_positive",
        ),
        sa.CheckConstraint(
            "(width IS NULL AND height IS NULL) "
            "OR (width > 0 AND height > 0)",
            name="ck_knowledge_assets_dimensions",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.project_id"],
            name="fk_knowledge_assets_project",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "project_id",
            "asset_id",
            name="pk_knowledge_assets",
        ),
        sa.UniqueConstraint(
            "project_id",
            "content_hash",
            name="uq_knowledge_assets_project_content_hash",
        ),
    )

    op.create_table(
        "snapshot_assets",
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("snapshot_id", sa.Text(), nullable=False),
        sa.Column("asset_id", sa.Text(), nullable=False),
        sa.Column("evidence_kind", sa.Text(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("alt_text", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("caption", sa.Text(), nullable=True),
        sa.Column(
            "locator",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "metadata",
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
            "evidence_kind IN "
            "('embedded', 'json_ld', 'gallery', 'body', 'featured_media', "
            "'wordpress_media', 'manual_upload')",
            name="ck_snapshot_assets_evidence_kind",
        ),
        sa.CheckConstraint(
            "ordinal >= 0",
            name="ck_snapshot_assets_ordinal_nonnegative",
        ),
        sa.CheckConstraint(
            "source_url IS NULL OR btrim(source_url) <> ''",
            name="ck_snapshot_assets_source_url_nonempty",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "source_id", "snapshot_id"],
            [
                "source_snapshots.project_id",
                "source_snapshots.source_id",
                "source_snapshots.snapshot_id",
            ],
            name="fk_snapshot_assets_snapshot",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "asset_id"],
            ["knowledge_assets.project_id", "knowledge_assets.asset_id"],
            name="fk_snapshot_assets_asset",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "project_id",
            "source_id",
            "snapshot_id",
            "asset_id",
            name="pk_snapshot_assets",
        ),
        sa.UniqueConstraint(
            "project_id",
            "source_id",
            "snapshot_id",
            "ordinal",
            name="uq_snapshot_assets_snapshot_ordinal",
        ),
    )
    op.create_index(
        "ix_snapshot_assets_asset",
        "snapshot_assets",
        ["project_id", "asset_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_snapshot_assets_asset", table_name="snapshot_assets")
    op.drop_table("snapshot_assets")
    op.drop_table("knowledge_assets")
