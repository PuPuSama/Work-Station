"""Allow immutable Project Prompt snapshots for Server Humanize jobs.

Revision ID: 20260731_0016
Revises: 20260731_0015
Create Date: 2026-07-31
"""

from collections.abc import Sequence

from alembic import op


revision: str = "20260731_0016"
down_revision: str | Sequence[str] | None = "20260731_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TABLE_CONSTRAINTS = (
    ("project_prompt_defaults", "ck_project_prompt_defaults_kind"),
    ("project_prompt_versions", "ck_project_prompt_versions_kind"),
    ("project_prompt_heads", "ck_project_prompt_heads_kind"),
)


def _replace_kind_checks(expression: str) -> None:
    for table_name, constraint_name in _TABLE_CONSTRAINTS:
        op.drop_constraint(
            constraint_name,
            table_name,
            type_="check",
        )
        op.create_check_constraint(
            constraint_name,
            table_name,
            expression,
        )


def upgrade() -> None:
    _replace_kind_checks(
        "kind IN ('outline', 'article', 'review', 'humanize')"
    )


def downgrade() -> None:
    # PostgreSQL refuses this downgrade if humanize rows still exist. That
    # fail-closed behavior preserves immutable Prompt history.
    _replace_kind_checks(
        "kind IN ('outline', 'article', 'review')"
    )
