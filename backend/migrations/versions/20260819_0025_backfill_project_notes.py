"""Backfill Project notes from historical Server Task snapshots.

Revision ID: 20260819_0025
Revises: 20260817_0024
Create Date: 2026-08-19
"""

from collections.abc import Sequence

from alembic import op


revision: str = "20260819_0025"
down_revision: str | Sequence[str] | None = "20260817_0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The editable project column was introduced after the original Server
    # task import. Preserve an existing project value and use the newest
    # non-empty task snapshot only for projects that are still blank.
    op.execute(
        """
        WITH latest_notes AS (
            SELECT project_id, project_notes
            FROM (
                SELECT
                    project_id,
                    btrim(payload ->> 'project_notes') AS project_notes,
                    row_number() OVER (
                        PARTITION BY project_id
                        ORDER BY updated_at DESC, task_id DESC
                    ) AS row_number
                FROM article_tasks
                WHERE nullif(btrim(payload ->> 'project_notes'), '') IS NOT NULL
            ) snapshots
            WHERE row_number = 1
        )
        UPDATE projects AS project
        SET
            project_notes = latest_notes.project_notes,
            revision = project.revision + 1,
            updated_at = now()
        FROM latest_notes
        WHERE project.project_id = latest_notes.project_id
          AND nullif(btrim(project.project_notes), '') IS NULL
        """
    )


def downgrade() -> None:
    # Historical snapshots are retained; do not erase operator notes during
    # a downgrade because the project value may have been edited afterwards.
    pass
