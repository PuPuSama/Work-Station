"""Allow project prompts to be edited and deleted from the library.

Revision ID: 20260812_0020
Revises: 20260806_0019
Create Date: 2026-08-12
"""

from collections.abc import Sequence

from alembic import op


revision: str = "20260812_0020"
down_revision: str | Sequence[str] | None = "20260806_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS "
        "trg_project_prompt_versions_append_only "
        "ON project_prompt_versions"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS "
        "prevent_project_prompt_version_mutation()"
    )


def downgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION prevent_project_prompt_version_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'project_prompt_versions is append-only';
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_project_prompt_versions_append_only
        BEFORE UPDATE OR DELETE ON project_prompt_versions
        FOR EACH ROW
        EXECUTE FUNCTION prevent_project_prompt_version_mutation()
        """
    )
