"""Merge Workflow Assistant M1 with the latest main migration chain.

Revision ID: 20260820_0029
Revises: 20260818_0025, 20260820_0028
Create Date: 2026-08-20
"""

from collections.abc import Sequence


revision: str = "20260820_0029"
down_revision: str | Sequence[str] | None = (
    "20260818_0025",
    "20260820_0028",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join the independent additive branches without changing schema."""


def downgrade() -> None:
    """Split the migration graph without changing schema."""
