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
    sa.Column(
        "project_notes",
        sa.Text(),
        nullable=False,
        server_default=sa.text("''"),
    ),
    sa.Column(
        "project_business_profile",
        sa.Text(),
        nullable=False,
        server_default=sa.text("''"),
    ),
    sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'active'")),
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
    sa.CheckConstraint(
        "revision >= 0",
        name="ck_projects_revision_nonnegative",
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
    sa.Column("pending_snapshot_id", sa.Text(), nullable=True),
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
        "pending_snapshot_id IS NULL "
        "OR current_snapshot_id IS NULL "
        "OR pending_snapshot_id <> current_snapshot_id",
        name="ck_knowledge_sources_distinct_snapshot_pointers",
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
sa.Index(
    "ix_knowledge_sources_pending_snapshot",
    knowledge_sources.c.project_id,
    knowledge_sources.c.pending_snapshot_id,
    postgresql_where=knowledge_sources.c.pending_snapshot_id.is_not(None),
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

knowledge_sources.append_constraint(
    sa.ForeignKeyConstraint(
        [
            knowledge_sources.c.project_id,
            knowledge_sources.c.source_id,
            knowledge_sources.c.pending_snapshot_id,
        ],
        [
            source_snapshots.c.project_id,
            source_snapshots.c.source_id,
            source_snapshots.c.snapshot_id,
        ],
        name="fk_knowledge_sources_pending_snapshot",
        deferrable=True,
        initially="DEFERRED",
    )
)


source_snapshot_review_receipts = sa.Table(
    "source_snapshot_review_receipts",
    metadata,
    sa.Column("project_id", sa.Text(), nullable=False),
    sa.Column("source_id", sa.Text(), nullable=False),
    sa.Column("snapshot_id", sa.Text(), nullable=False),
    sa.Column("review_version", sa.Integer(), nullable=False),
    sa.Column("receipt_id", sa.Text(), nullable=False),
    sa.Column("decision", sa.Text(), nullable=False),
    sa.Column("source_kind", sa.Text(), nullable=False),
    sa.Column("trust_tier", sa.Text(), nullable=False),
    sa.Column("reason", sa.Text(), nullable=False),
    sa.Column("reviewer_kind", sa.Text(), nullable=False),
    sa.Column("reviewer_id", sa.Text(), nullable=True),
    sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    ),
    sa.CheckConstraint(
        "review_version > 0",
        name="ck_snapshot_review_receipts_version_positive",
    ),
    sa.CheckConstraint(
        "btrim(receipt_id) <> ''",
        name="ck_snapshot_review_receipts_receipt_id_nonempty",
    ),
    sa.CheckConstraint(
        "decision IN ('approve', 'needs_review', 'reject')",
        name="ck_snapshot_review_receipts_decision",
    ),
    sa.CheckConstraint(
        "source_kind IN "
        "('private_file', 'product_detail', 'product_category', "
        "'official_blog', 'knowledge_page')",
        name="ck_snapshot_review_receipts_source_kind",
    ),
    sa.CheckConstraint(
        "trust_tier IN "
        "('hard_fact', 'reference_material', 'writing_instruction')",
        name="ck_snapshot_review_receipts_trust_tier",
    ),
    sa.CheckConstraint(
        "char_length(btrim(reason)) BETWEEN 1 AND 500",
        name="ck_snapshot_review_receipts_reason_length",
    ),
    sa.CheckConstraint(
        "reviewer_kind IN ('user', 'automation', 'legacy_migration')",
        name="ck_snapshot_review_receipts_reviewer_kind",
    ),
    sa.CheckConstraint(
        "(reviewer_kind = 'legacy_migration' AND reviewer_id IS NULL) "
        "OR (reviewer_kind IN ('user', 'automation') "
        "AND reviewer_id IS NOT NULL AND btrim(reviewer_id) <> '')",
        name="ck_snapshot_review_receipts_reviewer_identity",
    ),
    sa.ForeignKeyConstraint(
        ["project_id", "source_id", "snapshot_id"],
        [
            "source_snapshots.project_id",
            "source_snapshots.source_id",
            "source_snapshots.snapshot_id",
        ],
        name="fk_snapshot_review_receipts_snapshot",
        ondelete="RESTRICT",
    ),
    sa.PrimaryKeyConstraint(
        "project_id",
        "source_id",
        "snapshot_id",
        "review_version",
        name="pk_source_snapshot_review_receipts",
    ),
    sa.UniqueConstraint(
        "project_id",
        "receipt_id",
        name="uq_snapshot_review_receipts_project_receipt",
    ),
)

sa.Index(
    "ix_snapshot_review_receipts_latest",
    source_snapshot_review_receipts.c.project_id,
    source_snapshot_review_receipts.c.source_id,
    source_snapshot_review_receipts.c.snapshot_id,
    source_snapshot_review_receipts.c.review_version.desc(),
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


retrieval_plans = sa.Table(
    "retrieval_plans",
    metadata,
    sa.Column("project_id", sa.Text(), nullable=False),
    sa.Column("retrieval_plan_id", sa.Text(), nullable=False),
    sa.Column("article_id", sa.Text(), nullable=False),
    sa.Column("outline_version", sa.Integer(), nullable=False),
    sa.Column("max_gap_fill_rounds", sa.Integer(), nullable=False),
    sa.Column(
        "metadata",
        postgresql.JSONB(astext_type=sa.Text()),
        nullable=False,
        server_default=sa.text("'{}'::jsonb"),
    ),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint(
        "btrim(retrieval_plan_id) <> ''",
        name="ck_retrieval_plans_id_nonempty",
    ),
    sa.CheckConstraint(
        "btrim(article_id) <> ''",
        name="ck_retrieval_plans_article_id_nonempty",
    ),
    sa.CheckConstraint(
        "outline_version > 0",
        name="ck_retrieval_plans_outline_version_positive",
    ),
    sa.CheckConstraint(
        "max_gap_fill_rounds BETWEEN 0 AND 2",
        name="ck_retrieval_plans_gap_rounds",
    ),
    sa.ForeignKeyConstraint(
        ["project_id"],
        ["projects.project_id"],
        name="fk_retrieval_plans_project",
        ondelete="RESTRICT",
    ),
    sa.PrimaryKeyConstraint(
        "project_id",
        "retrieval_plan_id",
        name="pk_retrieval_plans",
    ),
    sa.UniqueConstraint(
        "project_id",
        "retrieval_plan_id",
        "article_id",
        "outline_version",
        name="uq_retrieval_plans_article_version_identity",
    ),
)

sa.Index(
    "ix_retrieval_plans_article_version",
    retrieval_plans.c.project_id,
    retrieval_plans.c.article_id,
    retrieval_plans.c.outline_version,
)


retrieval_scopes = sa.Table(
    "retrieval_scopes",
    metadata,
    sa.Column("project_id", sa.Text(), nullable=False),
    sa.Column("retrieval_plan_id", sa.Text(), nullable=False),
    sa.Column("scope_id", sa.Text(), nullable=False),
    sa.Column("ordinal", sa.Integer(), nullable=False),
    sa.Column("scope_type", sa.Text(), nullable=False),
    sa.Column("scope_key", sa.Text(), nullable=False),
    sa.Column("title", sa.Text(), nullable=False),
    sa.Column("query_variants", postgresql.ARRAY(sa.Text()), nullable=False),
    sa.Column(
        "filters",
        postgresql.JSONB(astext_type=sa.Text()),
        nullable=False,
        server_default=sa.text("'{}'::jsonb"),
    ),
    sa.Column("minimum_hits", sa.Integer(), nullable=False),
    sa.Column("minimum_distinct_sources", sa.Integer(), nullable=False),
    sa.Column("require_hard_fact", sa.Boolean(), nullable=False),
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
        "btrim(scope_id) <> '' AND btrim(scope_key) <> '' AND btrim(title) <> ''",
        name="ck_retrieval_scopes_text_nonempty",
    ),
    sa.CheckConstraint(
        "ordinal >= 0",
        name="ck_retrieval_scopes_ordinal_nonnegative",
    ),
    sa.CheckConstraint(
        "scope_type IN ('introduction', 'h2_section', 'product_fact', 'faq')",
        name="ck_retrieval_scopes_type",
    ),
    sa.CheckConstraint(
        "cardinality(query_variants) > 0",
        name="ck_retrieval_scopes_queries_nonempty",
    ),
    sa.CheckConstraint(
        "minimum_hits > 0 AND minimum_distinct_sources > 0",
        name="ck_retrieval_scopes_minimums_positive",
    ),
    sa.ForeignKeyConstraint(
        ["project_id", "retrieval_plan_id"],
        ["retrieval_plans.project_id", "retrieval_plans.retrieval_plan_id"],
        name="fk_retrieval_scopes_plan",
        ondelete="RESTRICT",
    ),
    sa.PrimaryKeyConstraint(
        "project_id",
        "retrieval_plan_id",
        "scope_id",
        name="pk_retrieval_scopes",
    ),
    sa.UniqueConstraint(
        "project_id",
        "retrieval_plan_id",
        "ordinal",
        name="uq_retrieval_scopes_plan_ordinal",
    ),
    sa.UniqueConstraint(
        "project_id",
        "retrieval_plan_id",
        "scope_type",
        "scope_key",
        name="uq_retrieval_scopes_plan_scope_key",
    ),
)


evidence_packs = sa.Table(
    "evidence_packs",
    metadata,
    sa.Column("project_id", sa.Text(), nullable=False),
    sa.Column("evidence_pack_id", sa.Text(), nullable=False),
    sa.Column("retrieval_plan_id", sa.Text(), nullable=False),
    sa.Column("scope_id", sa.Text(), nullable=False),
    sa.Column("article_id", sa.Text(), nullable=False),
    sa.Column("outline_version", sa.Integer(), nullable=False),
    sa.Column("sufficiency", sa.Text(), nullable=False),
    sa.Column("gap_reasons", postgresql.ARRAY(sa.Text()), nullable=False),
    sa.Column("hard_fact_chunk_ids", postgresql.ARRAY(sa.Text()), nullable=False),
    sa.Column("public_citation_urls", postgresql.ARRAY(sa.Text()), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint(
        "btrim(evidence_pack_id) <> ''",
        name="ck_evidence_packs_id_nonempty",
    ),
    sa.CheckConstraint(
        "sufficiency IN ('sufficient', 'weak', 'missing')",
        name="ck_evidence_packs_sufficiency",
    ),
    sa.ForeignKeyConstraint(
        [
            "project_id",
            "retrieval_plan_id",
            "article_id",
            "outline_version",
        ],
        [
            "retrieval_plans.project_id",
            "retrieval_plans.retrieval_plan_id",
            "retrieval_plans.article_id",
            "retrieval_plans.outline_version",
        ],
        name="fk_evidence_packs_plan_version",
        ondelete="RESTRICT",
    ),
    sa.ForeignKeyConstraint(
        ["project_id", "retrieval_plan_id", "scope_id"],
        [
            "retrieval_scopes.project_id",
            "retrieval_scopes.retrieval_plan_id",
            "retrieval_scopes.scope_id",
        ],
        name="fk_evidence_packs_scope",
        ondelete="RESTRICT",
    ),
    sa.PrimaryKeyConstraint(
        "project_id",
        "evidence_pack_id",
        name="pk_evidence_packs",
    ),
)

sa.Index(
    "ix_evidence_packs_article_version",
    evidence_packs.c.project_id,
    evidence_packs.c.article_id,
    evidence_packs.c.outline_version,
)


evidence_pack_hits = sa.Table(
    "evidence_pack_hits",
    metadata,
    sa.Column("project_id", sa.Text(), nullable=False),
    sa.Column("evidence_pack_id", sa.Text(), nullable=False),
    sa.Column("chunk_id", sa.Text(), nullable=False),
    sa.Column("rank", sa.Integer(), nullable=False),
    sa.Column("score", sa.Float(), nullable=False),
    sa.Column(
        "provenance",
        postgresql.JSONB(astext_type=sa.Text()),
        nullable=False,
        server_default=sa.text("'{}'::jsonb"),
    ),
    sa.Column(
        "explanation",
        postgresql.JSONB(astext_type=sa.Text()),
        nullable=False,
        server_default=sa.text("'{}'::jsonb"),
    ),
    sa.CheckConstraint("rank > 0", name="ck_evidence_pack_hits_rank_positive"),
    sa.CheckConstraint(
        "score >= -1 AND score <= 1",
        name="ck_evidence_pack_hits_score_range",
    ),
    sa.ForeignKeyConstraint(
        ["project_id", "evidence_pack_id"],
        ["evidence_packs.project_id", "evidence_packs.evidence_pack_id"],
        name="fk_evidence_pack_hits_pack",
        ondelete="RESTRICT",
    ),
    sa.ForeignKeyConstraint(
        ["project_id", "chunk_id"],
        ["knowledge_chunks.project_id", "knowledge_chunks.chunk_id"],
        name="fk_evidence_pack_hits_chunk",
        ondelete="RESTRICT",
    ),
    sa.PrimaryKeyConstraint(
        "project_id",
        "evidence_pack_id",
        "chunk_id",
        name="pk_evidence_pack_hits",
    ),
    sa.UniqueConstraint(
        "project_id",
        "evidence_pack_id",
        "rank",
        name="uq_evidence_pack_hits_rank",
    ),
)


evidence_links = sa.Table(
    "evidence_links",
    metadata,
    sa.Column("project_id", sa.Text(), nullable=False),
    sa.Column("evidence_link_id", sa.Text(), nullable=False),
    sa.Column("article_id", sa.Text(), nullable=False),
    sa.Column("paragraph_id", sa.Text(), nullable=False),
    sa.Column("sentence_id", sa.Text(), nullable=True),
    sa.Column("paragraph_hash", sa.Text(), nullable=False),
    sa.Column("chunk_id", sa.Text(), nullable=False),
    sa.Column("support_scope", sa.Text(), nullable=False),
    sa.Column("claim_type", sa.Text(), nullable=False),
    sa.Column("support_type", sa.Text(), nullable=False),
    sa.Column("visible_words", sa.Integer(), nullable=False),
    sa.Column("public_citation_url", sa.Text(), nullable=True),
    sa.Column("validation_status", sa.Text(), nullable=False),
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
        "btrim(evidence_link_id) <> '' AND btrim(article_id) <> '' "
        "AND btrim(paragraph_id) <> ''",
        name="ck_evidence_links_identity_nonempty",
    ),
    sa.CheckConstraint(
        "paragraph_hash ~ '^[0-9a-f]{64}$'",
        name="ck_evidence_links_paragraph_hash",
    ),
    sa.CheckConstraint(
        "support_scope IN ('paragraph', 'sentence')",
        name="ck_evidence_links_support_scope",
    ),
    sa.CheckConstraint(
        "(support_scope = 'sentence') = (sentence_id IS NOT NULL)",
        name="ck_evidence_links_sentence_scope",
    ),
    sa.CheckConstraint(
        "claim_type IN ('reference', 'hard_fact')",
        name="ck_evidence_links_claim_type",
    ),
    sa.CheckConstraint(
        "claim_type <> 'hard_fact' OR support_scope = 'sentence'",
        name="ck_evidence_links_hard_fact_sentence",
    ),
    sa.CheckConstraint(
        "support_type IN ('direct', 'paraphrase', 'contextual')",
        name="ck_evidence_links_support_type",
    ),
    sa.CheckConstraint(
        "validation_status IN ('valid', 'needs_review', 'invalid')",
        name="ck_evidence_links_validation_status",
    ),
    sa.CheckConstraint(
        "visible_words >= 0",
        name="ck_evidence_links_visible_words_nonnegative",
    ),
    sa.ForeignKeyConstraint(
        ["project_id", "chunk_id"],
        ["knowledge_chunks.project_id", "knowledge_chunks.chunk_id"],
        name="fk_evidence_links_chunk",
        ondelete="RESTRICT",
    ),
    sa.PrimaryKeyConstraint(
        "project_id",
        "evidence_link_id",
        name="pk_evidence_links",
    ),
)

sa.Index(
    "ix_evidence_links_article_status",
    evidence_links.c.project_id,
    evidence_links.c.article_id,
    evidence_links.c.validation_status,
)
sa.Index(
    "ix_evidence_links_chunk",
    evidence_links.c.project_id,
    evidence_links.c.chunk_id,
)


research_graph_runs = sa.Table(
    "research_graph_runs",
    metadata,
    sa.Column("project_id", sa.Text(), nullable=False),
    sa.Column("thread_id", sa.Text(), nullable=False),
    sa.Column("organization_id", sa.Text(), nullable=False),
    sa.Column("retrieval_plan_id", sa.Text(), nullable=False),
    sa.Column("article_id", sa.Text(), nullable=False),
    sa.Column("outline_version", sa.Integer(), nullable=False),
    sa.Column("status", sa.Text(), nullable=False),
    sa.Column("current_node", sa.Text(), nullable=False),
    sa.Column("current_scope_id", sa.Text(), nullable=True),
    sa.Column("gap_fill_round", sa.Integer(), nullable=False),
    sa.Column("max_gap_fill_rounds", sa.Integer(), nullable=False),
    sa.Column("discovery_queries_used", sa.Integer(), nullable=False),
    sa.Column("max_discovery_queries", sa.Integer(), nullable=False),
    sa.Column("evidence_pack_ids", postgresql.ARRAY(sa.Text()), nullable=False),
    sa.Column("warnings", postgresql.ARRAY(sa.Text()), nullable=False),
    sa.Column("error_code", sa.Text(), nullable=True),
    sa.Column("error_message", sa.Text(), nullable=True),
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
    sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint(
        "btrim(thread_id) <> '' AND btrim(organization_id) <> ''",
        name="ck_research_graph_runs_identity_nonempty",
    ),
    sa.CheckConstraint(
        "status IN ('queued', 'running', 'waiting_for_review', 'completed', "
        "'completed_with_warnings', 'failed', 'cancelled')",
        name="ck_research_graph_runs_status",
    ),
    sa.CheckConstraint(
        "btrim(current_node) <> ''",
        name="ck_research_graph_runs_current_node_nonempty",
    ),
    sa.CheckConstraint(
        "gap_fill_round BETWEEN 0 AND 2 "
        "AND max_gap_fill_rounds BETWEEN 0 AND 2",
        name="ck_research_graph_runs_gap_rounds",
    ),
    sa.CheckConstraint(
        "discovery_queries_used >= 0 AND max_discovery_queries >= 0 "
        "AND discovery_queries_used <= max_discovery_queries",
        name="ck_research_graph_runs_discovery_budget",
    ),
    sa.CheckConstraint(
        "(status IN ('completed', 'completed_with_warnings', 'failed', 'cancelled')) "
        "= (finished_at IS NOT NULL)",
        name="ck_research_graph_runs_finished_state",
    ),
    sa.CheckConstraint(
        "(status = 'failed') = (error_code IS NOT NULL)",
        name="ck_research_graph_runs_failure_code",
    ),
    sa.ForeignKeyConstraint(
        [
            "project_id",
            "retrieval_plan_id",
            "article_id",
            "outline_version",
        ],
        [
            "retrieval_plans.project_id",
            "retrieval_plans.retrieval_plan_id",
            "retrieval_plans.article_id",
            "retrieval_plans.outline_version",
        ],
        name="fk_research_graph_runs_plan_version",
        ondelete="RESTRICT",
    ),
    sa.PrimaryKeyConstraint(
        "project_id",
        "thread_id",
        name="pk_research_graph_runs",
    ),
    sa.UniqueConstraint(
        "thread_id",
        name="uq_research_graph_runs_thread_id",
    ),
)

sa.Index(
    "ix_research_graph_runs_article",
    research_graph_runs.c.project_id,
    research_graph_runs.c.article_id,
    research_graph_runs.c.outline_version,
    research_graph_runs.c.created_at,
)


research_graph_events = sa.Table(
    "research_graph_events",
    metadata,
    sa.Column("project_id", sa.Text(), nullable=False),
    sa.Column("thread_id", sa.Text(), nullable=False),
    sa.Column("sequence", sa.Integer(), nullable=False),
    sa.Column("event_type", sa.Text(), nullable=False),
    sa.Column("node_name", sa.Text(), nullable=False),
    sa.Column("scope_id", sa.Text(), nullable=True),
    sa.Column("attempt", sa.Integer(), nullable=False),
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
        "sequence > 0 AND attempt > 0",
        name="ck_research_graph_events_sequence_attempt",
    ),
    sa.CheckConstraint(
        "event_type IN ('queued', 'node_completed', 'interrupted', 'resumed', "
        "'failed', 'completed', 'tool_call')",
        name="ck_research_graph_events_type",
    ),
    sa.CheckConstraint(
        "btrim(node_name) <> ''",
        name="ck_research_graph_events_node_nonempty",
    ),
    sa.ForeignKeyConstraint(
        ["project_id", "thread_id"],
        ["research_graph_runs.project_id", "research_graph_runs.thread_id"],
        name="fk_research_graph_events_run",
        ondelete="RESTRICT",
    ),
    sa.PrimaryKeyConstraint(
        "project_id",
        "thread_id",
        "sequence",
        name="pk_research_graph_events",
    ),
)


gap_fill_attempts = sa.Table(
    "gap_fill_attempts",
    metadata,
    sa.Column("project_id", sa.Text(), nullable=False),
    sa.Column("thread_id", sa.Text(), nullable=False),
    sa.Column("retrieval_plan_id", sa.Text(), nullable=False),
    sa.Column("scope_id", sa.Text(), nullable=False),
    sa.Column("round_number", sa.Integer(), nullable=False),
    sa.Column("attempt_id", sa.Text(), nullable=False),
    sa.Column("reason", sa.Text(), nullable=False),
    sa.Column("channel", sa.Text(), nullable=False),
    sa.Column("query", sa.Text(), nullable=False),
    sa.Column("discovered_urls", postgresql.ARRAY(sa.Text()), nullable=False),
    sa.Column("published_source_ids", postgresql.ARRAY(sa.Text()), nullable=False),
    sa.Column("result", sa.Text(), nullable=False),
    sa.Column(
        "cost_usage",
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
        "round_number BETWEEN 1 AND 2",
        name="ck_gap_fill_attempts_round",
    ),
    sa.CheckConstraint(
        "btrim(attempt_id) <> '' AND btrim(reason) <> '' AND btrim(query) <> ''",
        name="ck_gap_fill_attempts_text_nonempty",
    ),
    sa.CheckConstraint(
        "channel IN ('official_site', 'tavily_discovery')",
        name="ck_gap_fill_attempts_channel",
    ),
    sa.CheckConstraint(
        "result IN ('pending', 'improved', 'no_change', 'blocked')",
        name="ck_gap_fill_attempts_result",
    ),
    sa.ForeignKeyConstraint(
        ["project_id", "thread_id"],
        ["research_graph_runs.project_id", "research_graph_runs.thread_id"],
        name="fk_gap_fill_attempts_run",
        ondelete="RESTRICT",
    ),
    sa.ForeignKeyConstraint(
        ["project_id", "retrieval_plan_id", "scope_id"],
        [
            "retrieval_scopes.project_id",
            "retrieval_scopes.retrieval_plan_id",
            "retrieval_scopes.scope_id",
        ],
        name="fk_gap_fill_attempts_scope",
        ondelete="RESTRICT",
    ),
    sa.PrimaryKeyConstraint(
        "project_id",
        "thread_id",
        "scope_id",
        "round_number",
        name="pk_gap_fill_attempts",
    ),
    sa.UniqueConstraint(
        "attempt_id",
        name="uq_gap_fill_attempts_attempt_id",
    ),
)


research_conversations = sa.Table(
    "research_conversations",
    metadata,
    sa.Column("project_id", sa.Text(), nullable=False),
    sa.Column("conversation_id", sa.Text(), nullable=False),
    sa.Column("article_id", sa.Text(), nullable=True),
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
        "btrim(conversation_id) <> ''",
        name="ck_research_conversations_identity_nonempty",
    ),
    sa.CheckConstraint(
        "article_id IS NULL OR btrim(article_id) <> ''",
        name="ck_research_conversations_article_nonempty",
    ),
    sa.ForeignKeyConstraint(
        ["project_id"],
        ["projects.project_id"],
        name="fk_research_conversations_project",
        ondelete="RESTRICT",
    ),
    sa.PrimaryKeyConstraint(
        "project_id",
        "conversation_id",
        name="pk_research_conversations",
    ),
    sa.UniqueConstraint(
        "conversation_id",
        name="uq_research_conversations_conversation_id",
    ),
)

sa.Index(
    "ix_research_conversations_expiry",
    research_conversations.c.project_id,
    research_conversations.c.expires_at,
)


research_messages = sa.Table(
    "research_messages",
    metadata,
    sa.Column("project_id", sa.Text(), nullable=False),
    sa.Column("conversation_id", sa.Text(), nullable=False),
    sa.Column("message_id", sa.Text(), nullable=False),
    sa.Column("request_id", sa.Text(), nullable=False),
    sa.Column("sequence", sa.Integer(), nullable=False),
    sa.Column("role", sa.Text(), nullable=False),
    sa.Column("content", sa.Text(), nullable=False),
    sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    ),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint(
        "btrim(message_id) <> '' AND btrim(request_id) <> '' "
        "AND btrim(content) <> ''",
        name="ck_research_messages_text_nonempty",
    ),
    sa.CheckConstraint(
        "role IN ('user', 'assistant')",
        name="ck_research_messages_role",
    ),
    sa.CheckConstraint(
        "sequence > 0",
        name="ck_research_messages_sequence",
    ),
    sa.ForeignKeyConstraint(
        ["project_id", "conversation_id"],
        [
            "research_conversations.project_id",
            "research_conversations.conversation_id",
        ],
        name="fk_research_messages_conversation",
        ondelete="CASCADE",
    ),
    sa.PrimaryKeyConstraint(
        "project_id",
        "conversation_id",
        "message_id",
        name="pk_research_messages",
    ),
    sa.UniqueConstraint(
        "project_id",
        "conversation_id",
        "request_id",
        "role",
        name="uq_research_messages_request_role",
    ),
    sa.UniqueConstraint(
        "project_id",
        "conversation_id",
        "sequence",
        name="uq_research_messages_sequence",
    ),
)

sa.Index(
    "ix_research_messages_expiry",
    research_messages.c.project_id,
    research_messages.c.expires_at,
)


research_message_citations = sa.Table(
    "research_message_citations",
    metadata,
    sa.Column("project_id", sa.Text(), nullable=False),
    sa.Column("conversation_id", sa.Text(), nullable=False),
    sa.Column("message_id", sa.Text(), nullable=False),
    sa.Column("chunk_id", sa.Text(), nullable=False),
    sa.Column("ordinal", sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(
        ["project_id", "conversation_id", "message_id"],
        [
            "research_messages.project_id",
            "research_messages.conversation_id",
            "research_messages.message_id",
        ],
        name="fk_research_message_citations_message",
        ondelete="CASCADE",
    ),
    sa.ForeignKeyConstraint(
        ["project_id", "chunk_id"],
        ["knowledge_chunks.project_id", "knowledge_chunks.chunk_id"],
        name="fk_research_message_citations_chunk",
        ondelete="RESTRICT",
    ),
    sa.PrimaryKeyConstraint(
        "project_id",
        "conversation_id",
        "message_id",
        "chunk_id",
        name="pk_research_message_citations",
    ),
    sa.UniqueConstraint(
        "project_id",
        "conversation_id",
        "message_id",
        "ordinal",
        name="uq_research_message_citations_ordinal",
    ),
    sa.CheckConstraint(
        "ordinal > 0",
        name="ck_research_message_citations_ordinal",
    ),
)
