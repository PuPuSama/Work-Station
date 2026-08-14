"""Cascade prompt-head deletion to its prompt versions.

Revision ID: 20260812_0021
Revises: 20260812_0020
Create Date: 2026-08-12
"""

from collections.abc import Sequence

from alembic import op


revision: str = "20260812_0021"
down_revision: str | Sequence[str] | None = "20260812_0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "fk_project_prompt_versions_head",
        "project_prompt_versions",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_project_prompt_versions_head",
        "project_prompt_versions",
        "project_prompt_heads",
        ["organization_id", "project_id", "prompt_id", "kind"],
        ["organization_id", "project_id", "prompt_id", "kind"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_project_prompt_versions_head",
        "project_prompt_versions",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_project_prompt_versions_head",
        "project_prompt_versions",
        "project_prompt_heads",
        ["organization_id", "project_id", "prompt_id", "kind"],
        ["organization_id", "project_id", "prompt_id", "kind"],
        ondelete="RESTRICT",
    )
