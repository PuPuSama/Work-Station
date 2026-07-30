"""Persist outline-scoped retrieval plans, evidence packs, and evidence links.

Revision ID: 20260730_0005
Revises: 20260730_0004
Create Date: 2026-07-30
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260730_0005"
down_revision: str | Sequence[str] | None = "20260730_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "retrieval_plans",
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
        sa.UniqueConstraint(
            "project_id",
            "article_id",
            "outline_version",
            name="uq_retrieval_plans_article_version",
        ),
    )

    op.create_table(
        "retrieval_scopes",
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
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "btrim(scope_id) <> '' AND btrim(scope_key) <> '' "
            "AND btrim(title) <> ''",
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

    op.create_table(
        "evidence_packs",
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
    op.create_index(
        "ix_evidence_packs_article_version",
        "evidence_packs",
        ["project_id", "article_id", "outline_version"],
        unique=False,
    )

    op.create_table(
        "evidence_pack_hits",
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
        sa.CheckConstraint(
            "rank > 0",
            name="ck_evidence_pack_hits_rank_positive",
        ),
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

    op.create_table(
        "evidence_links",
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
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
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
    op.create_index(
        "ix_evidence_links_article_status",
        "evidence_links",
        ["project_id", "article_id", "validation_status"],
        unique=False,
    )
    op.create_index(
        "ix_evidence_links_chunk",
        "evidence_links",
        ["project_id", "chunk_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_evidence_links_chunk", table_name="evidence_links")
    op.drop_index("ix_evidence_links_article_status", table_name="evidence_links")
    op.drop_table("evidence_links")
    op.drop_table("evidence_pack_hits")
    op.drop_index(
        "ix_evidence_packs_article_version",
        table_name="evidence_packs",
    )
    op.drop_table("evidence_packs")
    op.drop_table("retrieval_scopes")
    op.drop_table("retrieval_plans")
