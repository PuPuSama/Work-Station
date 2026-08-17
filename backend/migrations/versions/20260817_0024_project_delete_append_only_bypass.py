"""Allow the audited project deletion transaction to remove project receipts.

Revision ID: 20260817_0024
Revises: 20260817_0023
Create Date: 2026-08-17
"""

from collections.abc import Sequence

from alembic import op


revision: str = "20260817_0024"
down_revision: str | Sequence[str] | None = "20260817_0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION prevent_audit_event_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF current_setting('article_agent.project_deletion', true) = 'on' THEN
                IF TG_OP = 'DELETE' THEN
                    RETURN OLD;
                END IF;
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'audit_events is append-only';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION forbid_snapshot_review_receipt_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF current_setting('article_agent.project_deletion', true) = 'on' THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION 'source snapshot review receipts are append-only'
                USING ERRCODE = '55000';
        END;
        $$
        """
    )


def downgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION prevent_audit_event_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'audit_events is append-only';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION forbid_snapshot_review_receipt_mutation()
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
