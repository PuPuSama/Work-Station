from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from server_schema import assistant_attachment_jobs  # noqa: E402


class AttachmentJobSchemaTests(unittest.TestCase):
    def test_schema_is_attachment_native_and_has_durable_controls(self) -> None:
        columns = set(assistant_attachment_jobs.c.keys())
        self.assertNotIn("task_id", columns)
        self.assertNotIn("article_task_id", columns)
        self.assertTrue(
            {
                "organization_id",
                "requested_by_user_id",
                "project_id",
                "attachment_id",
                "proposal_id",
                "operation",
                "idempotency_key",
                "expected_attachment_revision",
                "expected_proposal_revision",
                "status",
                "attempts",
                "max_attempts",
                "cancel_requested",
                "worker_id",
                "lease_expires_at",
                "standardized_error_code",
            }.issubset(columns)
        )
        index_names = {index.name for index in assistant_attachment_jobs.indexes}
        self.assertIn("uq_assistant_attachment_jobs_active_attachment", index_names)
        self.assertIn("uq_assistant_attachment_jobs_active_proposal", index_names)
        self.assertIn("ix_assistant_attachment_jobs_claim", index_names)
        target_shape = next(
            constraint
            for constraint in assistant_attachment_jobs.constraints
            if constraint.name == "ck_assistant_attachment_jobs_target_shape"
        )
        sql = str(target_shape.sqltext)
        self.assertIn("operation = 'preview_import_proposal'", sql)
        self.assertIn("proposal_id IS NULL", sql)

    def test_migration_is_additive_and_reversible(self) -> None:
        path = (
            BACKEND_DIR
            / "migrations"
            / "versions"
            / "20260820_0031_workflow_assistant_attachment_jobs.py"
        )
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        values = {
            node.targets[0].id: ast.literal_eval(node.value)
            for node in tree.body
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in {"revision", "down_revision"}
        }
        self.assertEqual(
            values,
            {
                "revision": "20260820_0031",
                "down_revision": "20260820_0030",
            },
        )
        self.assertIn('op.create_table(\n        "assistant_attachment_jobs"', source)
        self.assertIn('op.drop_table("assistant_attachment_jobs")', source)
        self.assertNotIn("article_task_id", source)


if __name__ == "__main__":
    unittest.main()
