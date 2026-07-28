"""Create the project-scoped knowledge schema.

Revision ID: 20260728_0001
Revises:
Create Date: 2026-07-28
"""

from collections.abc import Sequence

from alembic import op
from pgvector.sqlalchemy import Vector
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260728_0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "projects",
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("customer_name", sa.Text(), nullable=False),
        sa.Column("official_domain", sa.Text(), nullable=False),
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
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "btrim(project_id) <> ''",
            name="ck_projects_project_id_nonempty",
        ),
        sa.CheckConstraint(
            "btrim(customer_name) <> ''",
            name="ck_projects_customer_name_nonempty",
        ),
        sa.CheckConstraint(
            "btrim(official_domain) <> ''",
            name="ck_projects_official_domain_nonempty",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'archived')",
            name="ck_projects_status",
        ),
        sa.PrimaryKeyConstraint("project_id", name="pk_projects"),
    )

    op.create_table(
        "knowledge_sources",
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("source_kind", sa.Text(), nullable=False),
        sa.Column("trust_tier", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'inbox'"),
        ),
        sa.Column(
            "public_source",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("canonical_url", sa.Text(), nullable=True),
        sa.Column("current_snapshot_id", sa.Text(), nullable=True),
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
            "btrim(source_id) <> ''",
            name="ck_knowledge_sources_source_id_nonempty",
        ),
        sa.CheckConstraint(
            "btrim(display_name) <> ''",
            name="ck_knowledge_sources_display_name_nonempty",
        ),
        sa.CheckConstraint(
            "source_kind IN "
            "('private_file', 'product_detail', 'product_category', "
            "'official_blog', 'knowledge_page')",
            name="ck_knowledge_sources_source_kind",
        ),
        sa.CheckConstraint(
            "trust_tier IN "
            "('hard_fact', 'reference_material', 'writing_instruction')",
            name="ck_knowledge_sources_trust_tier",
        ),
        sa.CheckConstraint(
            "status IN "
            "('inbox', 'published', 'needs_review', 'rejected', 'stale')",
            name="ck_knowledge_sources_status",
        ),
        sa.CheckConstraint(
            "status <> 'published' OR current_snapshot_id IS NOT NULL",
            name="ck_knowledge_sources_published_snapshot",
        ),
        sa.CheckConstraint(
            "NOT public_source OR "
            "(canonical_url IS NOT NULL AND btrim(canonical_url) <> '')",
            name="ck_knowledge_sources_public_canonical_url",
        ),
        sa.CheckConstraint(
            "canonical_url IS NULL OR btrim(canonical_url) <> ''",
            name="ck_knowledge_sources_canonical_url_nonempty",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.project_id"],
            name="fk_knowledge_sources_project",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "project_id",
            "source_id",
            name="pk_knowledge_sources",
        ),
    )
    op.create_index(
        "uq_knowledge_sources_project_canonical_url",
        "knowledge_sources",
        ["project_id", "canonical_url"],
        unique=True,
        postgresql_where=sa.text("canonical_url IS NOT NULL"),
    )
    op.create_index(
        "ix_knowledge_sources_retrieval_scope",
        "knowledge_sources",
        ["project_id", "status", "current_snapshot_id"],
        unique=False,
    )

    op.create_table(
        "source_snapshots",
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("snapshot_id", sa.Text(), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("parser_name", sa.Text(), nullable=False),
        sa.Column("parser_version", sa.Text(), nullable=False),
        sa.Column("raw_artifact_uri", sa.Text(), nullable=True),
        sa.Column("normalized_artifact_uri", sa.Text(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
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
            "btrim(snapshot_id) <> ''",
            name="ck_source_snapshots_snapshot_id_nonempty",
        ),
        sa.CheckConstraint(
            "btrim(content_hash) <> ''",
            name="ck_source_snapshots_content_hash_nonempty",
        ),
        sa.CheckConstraint(
            "btrim(parser_name) <> ''",
            name="ck_source_snapshots_parser_name_nonempty",
        ),
        sa.CheckConstraint(
            "btrim(parser_version) <> ''",
            name="ck_source_snapshots_parser_version_nonempty",
        ),
        sa.CheckConstraint(
            "raw_artifact_uri IS NULL OR btrim(raw_artifact_uri) <> ''",
            name="ck_source_snapshots_raw_artifact_uri_nonempty",
        ),
        sa.CheckConstraint(
            "normalized_artifact_uri IS NULL "
            "OR btrim(normalized_artifact_uri) <> ''",
            name="ck_source_snapshots_normalized_artifact_uri_nonempty",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "source_id"],
            ["knowledge_sources.project_id", "knowledge_sources.source_id"],
            name="fk_source_snapshots_source",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "project_id",
            "snapshot_id",
            name="pk_source_snapshots",
        ),
        sa.UniqueConstraint(
            "project_id",
            "source_id",
            "snapshot_id",
            name="uq_source_snapshots_source_snapshot",
        ),
        sa.UniqueConstraint(
            "project_id",
            "source_id",
            "content_hash",
            "parser_name",
            "parser_version",
            name="uq_source_snapshots_content_parser",
        ),
    )

    op.create_foreign_key(
        "fk_knowledge_sources_current_snapshot",
        "knowledge_sources",
        "source_snapshots",
        ["project_id", "source_id", "current_snapshot_id"],
        ["project_id", "source_id", "snapshot_id"],
        deferrable=True,
        initially="DEFERRED",
    )

    op.create_table(
        "knowledge_chunks",
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("chunk_id", sa.Text(), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("snapshot_id", sa.Text(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column(
            "heading_path",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column("text", sa.Text(), nullable=False),
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
        sa.Column("embedding_model", sa.Text(), nullable=True),
        sa.Column("embedding", Vector(1536), nullable=True),
        sa.Column("embedded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "btrim(chunk_id) <> ''",
            name="ck_knowledge_chunks_chunk_id_nonempty",
        ),
        sa.CheckConstraint(
            "left(chunk_id, char_length(snapshot_id) + 1) = snapshot_id || ':'",
            name="ck_knowledge_chunks_snapshot_prefix",
        ),
        sa.CheckConstraint(
            "ordinal >= 0",
            name="ck_knowledge_chunks_ordinal_nonnegative",
        ),
        sa.CheckConstraint(
            "btrim(text) <> ''",
            name="ck_knowledge_chunks_text_nonempty",
        ),
        sa.CheckConstraint(
            "("
            "embedding_model IS NULL AND embedding IS NULL AND embedded_at IS NULL"
            ") OR ("
            "embedding_model IS NOT NULL "
            "AND btrim(embedding_model) <> '' "
            "AND embedding IS NOT NULL "
            "AND embedded_at IS NOT NULL"
            ")",
            name="ck_knowledge_chunks_embedding_state",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "source_id", "snapshot_id"],
            [
                "source_snapshots.project_id",
                "source_snapshots.source_id",
                "source_snapshots.snapshot_id",
            ],
            name="fk_knowledge_chunks_snapshot",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "project_id",
            "chunk_id",
            name="pk_knowledge_chunks",
        ),
        sa.UniqueConstraint(
            "project_id",
            "source_id",
            "snapshot_id",
            "ordinal",
            name="uq_knowledge_chunks_snapshot_ordinal",
        ),
    )
    op.create_index(
        "ix_knowledge_chunks_retrieval_scope",
        "knowledge_chunks",
        ["project_id", "source_id", "snapshot_id", "embedding_model"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("knowledge_chunks")
    op.drop_constraint(
        "fk_knowledge_sources_current_snapshot",
        "knowledge_sources",
        type_="foreignkey",
    )
    op.drop_table("source_snapshots")
    op.drop_table("knowledge_sources")
    op.drop_table("projects")
