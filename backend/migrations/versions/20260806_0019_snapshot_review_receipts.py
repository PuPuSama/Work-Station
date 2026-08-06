"""Bind Server review decisions to immutable source snapshots.

Revision ID: 20260806_0019
Revises: 20260731_0018
Create Date: 2026-08-06
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260806_0019"
down_revision: str | Sequence[str] | None = "20260731_0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "knowledge_sources",
        sa.Column("pending_snapshot_id", sa.Text(), nullable=True),
    )
    op.create_check_constraint(
        "ck_knowledge_sources_distinct_snapshot_pointers",
        "knowledge_sources",
        "pending_snapshot_id IS NULL "
        "OR current_snapshot_id IS NULL "
        "OR pending_snapshot_id <> current_snapshot_id",
    )
    op.create_foreign_key(
        "fk_knowledge_sources_pending_snapshot",
        "knowledge_sources",
        "source_snapshots",
        ["project_id", "source_id", "pending_snapshot_id"],
        ["project_id", "source_id", "snapshot_id"],
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_index(
        "ix_knowledge_sources_pending_snapshot",
        "knowledge_sources",
        ["project_id", "pending_snapshot_id"],
        postgresql_where=sa.text("pending_snapshot_id IS NOT NULL"),
    )

    op.create_table(
        "source_snapshot_review_receipts",
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
    op.execute(
        "CREATE INDEX ix_snapshot_review_receipts_latest "
        "ON source_snapshot_review_receipts "
        "(project_id, source_id, snapshot_id, review_version DESC)"
    )

    # Grandfather only bytes that were already serving. An old Source-level
    # approval must never authorize any non-current Snapshot.
    op.execute(
        """
        INSERT INTO source_snapshot_review_receipts (
            project_id,
            source_id,
            snapshot_id,
            review_version,
            receipt_id,
            decision,
            source_kind,
            trust_tier,
            reason,
            reviewer_kind,
            reviewer_id,
            reviewed_at
        )
        SELECT
            source.project_id,
            source.source_id,
            source.current_snapshot_id,
            1,
            'legacy-published-' || md5(
                source.project_id || chr(31) || source.source_id || chr(31)
                || source.current_snapshot_id
            ),
            'approve',
            source.source_kind,
            source.trust_tier,
            'Published before snapshot review receipt cutover.',
            'legacy_migration',
            NULL,
            source.updated_at
        FROM knowledge_sources AS source
        WHERE source.status = 'published'
          AND source.current_snapshot_id IS NOT NULL
        """
    )

    # A legacy decision is safe to bind only when there is exactly one possible
    # immutable Snapshot and no Current pointer. Multi-Snapshot sources require
    # a new explicit human review after cutover.
    op.execute(
        """
        WITH single_snapshot AS (
            SELECT
                project_id,
                source_id,
                min(snapshot_id) AS snapshot_id
            FROM source_snapshots
            GROUP BY project_id, source_id
            HAVING count(*) = 1
        )
        INSERT INTO source_snapshot_review_receipts (
            project_id,
            source_id,
            snapshot_id,
            review_version,
            receipt_id,
            decision,
            source_kind,
            trust_tier,
            reason,
            reviewer_kind,
            reviewer_id,
            reviewed_at
        )
        SELECT
            source.project_id,
            source.source_id,
            snapshot.snapshot_id,
            1,
            'legacy-review-' || md5(
                source.project_id || chr(31) || source.source_id || chr(31)
                || snapshot.snapshot_id
            ),
            source.metadata -> 'review' ->> 'decision',
            source.source_kind,
            source.trust_tier,
            CASE
                WHEN jsonb_typeof(
                    source.metadata -> 'review' -> 'reason'
                ) = 'string'
                AND char_length(
                    btrim(source.metadata -> 'review' ->> 'reason')
                ) BETWEEN 1 AND 500
                THEN btrim(source.metadata -> 'review' ->> 'reason')
                ELSE 'Legacy source review imported during snapshot receipt cutover.'
            END,
            'legacy_migration',
            NULL,
            source.updated_at
        FROM knowledge_sources AS source
        JOIN single_snapshot AS snapshot
          ON snapshot.project_id = source.project_id
         AND snapshot.source_id = source.source_id
        WHERE source.current_snapshot_id IS NULL
          AND jsonb_typeof(source.metadata -> 'review') = 'object'
          AND source.metadata -> 'review' ->> 'decision'
              IN ('approve', 'needs_review', 'reject')
        """
    )

    op.execute(
        """
        WITH single_snapshot AS (
            SELECT
                project_id,
                source_id,
                min(snapshot_id) AS snapshot_id
            FROM source_snapshots
            GROUP BY project_id, source_id
            HAVING count(*) = 1
        )
        UPDATE knowledge_sources AS source
        SET pending_snapshot_id = snapshot.snapshot_id
        FROM single_snapshot AS snapshot
        WHERE source.project_id = snapshot.project_id
          AND source.source_id = snapshot.source_id
          AND source.current_snapshot_id IS NULL
          AND source.status <> 'rejected'
          AND coalesce(
              source.metadata -> 'review' ->> 'decision',
              ''
          ) <> 'reject'
        """
    )
    op.execute(
        """
        UPDATE knowledge_sources AS source
        SET status = 'needs_review'
        WHERE source.current_snapshot_id IS NULL
          AND source.status = 'inbox'
          AND source.metadata -> 'review' ->> 'decision' = 'approve'
          AND (
              SELECT count(*)
              FROM source_snapshots AS snapshot
              WHERE snapshot.project_id = source.project_id
                AND snapshot.source_id = source.source_id
          ) > 1
        """
    )

    op.execute(
        """
        CREATE FUNCTION forbid_snapshot_review_receipt_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'source snapshot review receipts are append-only'
                USING ERRCODE = '55000';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_snapshot_review_receipts_append_only
        BEFORE UPDATE OR DELETE ON source_snapshot_review_receipts
        FOR EACH ROW EXECUTE FUNCTION forbid_snapshot_review_receipt_mutation()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_snapshot_review_receipts_append_only "
        "ON source_snapshot_review_receipts"
    )
    op.execute("DROP FUNCTION IF EXISTS forbid_snapshot_review_receipt_mutation()")
    op.drop_index(
        "ix_snapshot_review_receipts_latest",
        table_name="source_snapshot_review_receipts",
    )
    op.drop_table("source_snapshot_review_receipts")
    op.drop_constraint(
        "fk_knowledge_sources_pending_snapshot",
        "knowledge_sources",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_knowledge_sources_pending_snapshot",
        table_name="knowledge_sources",
    )
    op.drop_constraint(
        "ck_knowledge_sources_distinct_snapshot_pointers",
        "knowledge_sources",
        type_="check",
    )
    op.drop_column("knowledge_sources", "pending_snapshot_id")
