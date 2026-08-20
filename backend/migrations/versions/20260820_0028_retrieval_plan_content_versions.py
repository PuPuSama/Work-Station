"""Allow immutable content revisions within one outline version.

Revision ID: 20260820_0028
Revises: 20260819_0027
Create Date: 2026-08-20
"""

from collections.abc import Sequence

from alembic import op


revision: str = "20260820_0028"
down_revision: str | Sequence[str] | None = "20260819_0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "uq_retrieval_plans_article_version",
        "retrieval_plans",
        type_="unique",
    )
    op.create_index(
        "ix_retrieval_plans_article_version",
        "retrieval_plans",
        ["project_id", "article_id", "outline_version"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_retrieval_plans_article_version",
        table_name="retrieval_plans",
    )
    op.create_unique_constraint(
        "uq_retrieval_plans_article_version",
        "retrieval_plans",
        ["project_id", "article_id", "outline_version"],
    )
