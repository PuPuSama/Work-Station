from __future__ import annotations

import unittest
from pathlib import Path


class WorkflowAssistantMigrationContractTests(unittest.TestCase):
    def test_legacy_langgraph_tables_are_upgraded_before_stamp(self) -> None:
        migration = (
            Path(__file__).resolve().parents[1]
            / "migrations"
            / "versions"
            / "20260818_0025_workflow_assistant_m1.py"
        ).read_text(encoding="utf-8")

        drop_not_null = migration.index(
            '"ALTER COLUMN blob DROP NOT NULL"'
        )
        add_task_path = migration.index(
            '"ADD COLUMN IF NOT EXISTS task_path TEXT NOT NULL DEFAULT \'\'"'
        )
        stamp = migration.index(
            '"INSERT INTO checkpoint_migrations(v) "'
        )

        self.assertLess(drop_not_null, stamp)
        self.assertLess(add_task_path, stamp)


if __name__ == "__main__":
    unittest.main()
