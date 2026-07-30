"""Persist research graph runs, events, and bounded gap-fill attempts.

Revision ID: 20260730_0006
Revises: 20260730_0005
Create Date: 2026-07-30
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260730_0006"
down_revision: str | Sequence[str] | None = "20260730_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "research_graph_runs",
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
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
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
            "(status IN ('completed', 'completed_with_warnings', 'failed', "
            "'cancelled')) = (finished_at IS NOT NULL)",
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
    op.create_index(
        "ix_research_graph_runs_article",
        "research_graph_runs",
        ["project_id", "article_id", "outline_version", "created_at"],
        unique=False,
    )

    op.create_table(
        "research_graph_events",
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
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "sequence > 0 AND attempt > 0",
            name="ck_research_graph_events_sequence_attempt",
        ),
        sa.CheckConstraint(
            "event_type IN ('queued', 'node_completed', 'interrupted', "
            "'resumed', 'failed', 'completed', 'tool_call')",
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

    op.create_table(
        "gap_fill_attempts",
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
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "round_number BETWEEN 1 AND 2",
            name="ck_gap_fill_attempts_round",
        ),
        sa.CheckConstraint(
            "btrim(attempt_id) <> '' AND btrim(reason) <> '' "
            "AND btrim(query) <> ''",
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


def downgrade() -> None:
    op.drop_table("gap_fill_attempts")
    op.drop_table("research_graph_events")
    op.drop_index(
        "ix_research_graph_runs_article",
        table_name="research_graph_runs",
    )
    op.drop_table("research_graph_runs")
