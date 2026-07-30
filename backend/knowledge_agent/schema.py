from __future__ import annotations

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql


metadata = sa.MetaData()


projects = sa.Table(
    "projects",
    metadata,
    sa.Column("project_id", sa.Text(), primary_key=True),
    sa.Column("customer_name", sa.Text(), nullable=False),
    sa.Column("official_domain", sa.Text(), nullable=False),
    sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'active'")),
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
    sa.CheckConstraint("btrim(project_id) <> ''", name="ck_projects_project_id_nonempty"),
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
)


knowledge_sources = sa.Table(
    "knowledge_sources",
    metadata,
    sa.Column("project_id", sa.Text(), nullable=False),
    sa.Column("source_id", sa.Text(), nullable=False),
    sa.Column("display_name", sa.Text(), nullable=False),
    sa.Column("source_kind", sa.Text(), nullable=False),
    sa.Column("trust_tier", sa.Text(), nullable=False),
    sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'inbox'")),
    sa.Column(
        "public_source",
        sa.Boolean(),
        nullable=False,
        server_default=sa.false(),
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
        server_default=sa.func.now(),
    ),
    sa.Column(
        "updated_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
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
        "status IN ('inbox', 'published', 'needs_review', 'rejected', 'stale')",
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

sa.Index(
    "uq_knowledge_sources_project_canonical_url",
    knowledge_sources.c.project_id,
    knowledge_sources.c.canonical_url,
    unique=True,
    postgresql_where=knowledge_sources.c.canonical_url.is_not(None),
)
sa.Index(
    "ix_knowledge_sources_retrieval_scope",
    knowledge_sources.c.project_id,
    knowledge_sources.c.status,
    knowledge_sources.c.current_snapshot_id,
)


source_snapshots = sa.Table(
    "source_snapshots",
    metadata,
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
        server_default=sa.func.now(),
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

knowledge_sources.append_constraint(
    sa.ForeignKeyConstraint(
        [
            knowledge_sources.c.project_id,
            knowledge_sources.c.source_id,
            knowledge_sources.c.current_snapshot_id,
        ],
        [
            source_snapshots.c.project_id,
            source_snapshots.c.source_id,
            source_snapshots.c.snapshot_id,
        ],
        name="fk_knowledge_sources_current_snapshot",
        deferrable=True,
        initially="DEFERRED",
    )
)


knowledge_chunks = sa.Table(
    "knowledge_chunks",
    metadata,
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
        server_default=sa.func.now(),
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

sa.Index(
    "ix_knowledge_chunks_retrieval_scope",
    knowledge_chunks.c.project_id,
    knowledge_chunks.c.source_id,
    knowledge_chunks.c.snapshot_id,
    knowledge_chunks.c.embedding_model,
)
sa.Index(
    "ix_knowledge_chunks_search_text",
    sa.func.to_tsvector(
        sa.literal_column("'simple'::regconfig"),
        knowledge_chunks.c.text,
    ),
    postgresql_using="gin",
)


knowledge_assets = sa.Table(
    "knowledge_assets",
    metadata,
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
        server_default=sa.func.now(),
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


snapshot_assets = sa.Table(
    "snapshot_assets",
    metadata,
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
        server_default=sa.func.now(),
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

sa.Index(
    "ix_snapshot_assets_asset",
    snapshot_assets.c.project_id,
    snapshot_assets.c.asset_id,
)


knowledge_products = sa.Table(
    "knowledge_products",
    metadata,
    sa.Column("project_id", sa.Text(), nullable=False),
    sa.Column("product_id", sa.Text(), nullable=False),
    sa.Column("name", sa.Text(), nullable=False),
    sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'inbox'")),
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
        server_default=sa.func.now(),
    ),
    sa.Column(
        "updated_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
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

sa.Index(
    "uq_knowledge_products_project_canonical_url",
    knowledge_products.c.project_id,
    knowledge_products.c.canonical_url,
    unique=True,
    postgresql_where=knowledge_products.c.canonical_url.is_not(None),
)
sa.Index(
    "ix_knowledge_products_project_status",
    knowledge_products.c.project_id,
    knowledge_products.c.status,
)


knowledge_product_source_evidence = sa.Table(
    "knowledge_product_source_evidence",
    metadata,
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
        server_default=sa.func.now(),
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


knowledge_product_asset_evidence = sa.Table(
    "knowledge_product_asset_evidence",
    metadata,
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
        server_default=sa.func.now(),
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

sa.Index(
    "ix_product_asset_evidence_product",
    knowledge_product_asset_evidence.c.project_id,
    knowledge_product_asset_evidence.c.product_id,
)
