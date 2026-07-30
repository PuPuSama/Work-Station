"""Create stable product identities and immutable source/asset evidence.

Revision ID: 20260730_0003
Revises: 20260730_0002
Create Date: 2026-07-30
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260730_0003"
down_revision: str | Sequence[str] | None = "20260730_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "knowledge_products",
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("product_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'inbox'"),
        ),
        sa.Column("canonical_url", sa.Text(), nullable=True),
        sa.Column(
            "category_path",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
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
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "btrim(product_id) <> ''",
            name="ck_knowledge_products_product_id_nonempty",
        ),
        sa.CheckConstraint(
            "btrim(name) <> ''",
            name="ck_knowledge_products_name_nonempty",
        ),
        sa.CheckConstraint(
            "status IN ('inbox', 'confirmed', 'rejected', 'stale')",
            name="ck_knowledge_products_status",
        ),
        sa.CheckConstraint(
            "canonical_url IS NULL OR btrim(canonical_url) <> ''",
            name="ck_knowledge_products_canonical_url_nonempty",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.project_id"],
            name="fk_knowledge_products_project",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "project_id",
            "product_id",
            name="pk_knowledge_products",
        ),
    )
    op.create_index(
        "uq_knowledge_products_project_canonical_url",
        "knowledge_products",
        ["project_id", "canonical_url"],
        unique=True,
        postgresql_where=sa.text("canonical_url IS NOT NULL"),
    )
    op.create_index(
        "ix_knowledge_products_project_status",
        "knowledge_products",
        ["project_id", "status"],
        unique=False,
    )

    op.create_table(
        "knowledge_product_source_evidence",
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("product_id", sa.Text(), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("snapshot_id", sa.Text(), nullable=False),
        sa.Column("relation", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
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
            "relation IN "
            "('primary_detail', 'category_listing', 'private_specification', "
            "'supporting_page')",
            name="ck_product_source_evidence_relation",
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_product_source_evidence_confidence",
        ),
        sa.CheckConstraint(
            "btrim(reason) <> ''",
            name="ck_product_source_evidence_reason_nonempty",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "product_id"],
            ["knowledge_products.project_id", "knowledge_products.product_id"],
            name="fk_product_source_evidence_product",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "source_id", "snapshot_id"],
            [
                "source_snapshots.project_id",
                "source_snapshots.source_id",
                "source_snapshots.snapshot_id",
            ],
            name="fk_product_source_evidence_snapshot",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "project_id",
            "product_id",
            "source_id",
            "snapshot_id",
            name="pk_product_source_evidence",
        ),
    )

    op.create_table(
        "knowledge_product_asset_evidence",
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("product_id", sa.Text(), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("snapshot_id", sa.Text(), nullable=False),
        sa.Column("asset_id", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
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
            "role IN ('candidate', 'primary', 'gallery', 'detail', 'hero')",
            name="ck_product_asset_evidence_role",
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_product_asset_evidence_confidence",
        ),
        sa.CheckConstraint(
            "btrim(reason) <> ''",
            name="ck_product_asset_evidence_reason_nonempty",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "product_id"],
            ["knowledge_products.project_id", "knowledge_products.product_id"],
            name="fk_product_asset_evidence_product",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "source_id", "snapshot_id", "asset_id"],
            [
                "snapshot_assets.project_id",
                "snapshot_assets.source_id",
                "snapshot_assets.snapshot_id",
                "snapshot_assets.asset_id",
            ],
            name="fk_product_asset_evidence_snapshot_asset",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "project_id",
            "product_id",
            "source_id",
            "snapshot_id",
            "asset_id",
            name="pk_product_asset_evidence",
        ),
    )
    op.create_index(
        "ix_product_asset_evidence_product",
        "knowledge_product_asset_evidence",
        ["project_id", "product_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_product_asset_evidence_product",
        table_name="knowledge_product_asset_evidence",
    )
    op.drop_table("knowledge_product_asset_evidence")
    op.drop_table("knowledge_product_source_evidence")
    op.drop_index(
        "ix_knowledge_products_project_status",
        table_name="knowledge_products",
    )
    op.drop_index(
        "uq_knowledge_products_project_canonical_url",
        table_name="knowledge_products",
    )
    op.drop_table("knowledge_products")
