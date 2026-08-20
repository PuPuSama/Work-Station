from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from server_schema import (  # noqa: E402
    assistant_attachments,
    assistant_import_proposals,
)


class WorkflowAssistantM2SchemaTests(unittest.TestCase):
    def test_attachment_contract_has_scope_plan_cas_and_idempotency(self) -> None:
        self.assertEqual(
            set(assistant_attachments.c.keys()),
            {
                "organization_id",
                "attachment_id",
                "creator_user_id",
                "conversation_id",
                "proposed_project_id",
                "plan_id",
                "idempotency_key",
                "object_key",
                "original_filename",
                "mime_type",
                "byte_size",
                "sha256",
                "classification",
                "classification_payload",
                "revision",
                "status",
                "expires_at",
                "created_at",
                "updated_at",
            },
        )
        self.assertEqual(
            {column.name for column in assistant_attachments.primary_key.columns},
            {"organization_id", "attachment_id"},
        )
        unique_columns = {
            tuple(column.name for column in constraint.columns)
            for constraint in assistant_attachments.constraints
            if isinstance(constraint, sa.UniqueConstraint)
        }
        self.assertIn(
            (
                "organization_id",
                "creator_user_id",
                "conversation_id",
                "idempotency_key",
            ),
            unique_columns,
        )
        foreign_keys = {
            constraint.name: tuple(element.target_fullname for element in constraint.elements)
            for constraint in assistant_attachments.foreign_key_constraints
        }
        self.assertEqual(
            foreign_keys["fk_assistant_attachments_plan"],
            (
                "workflow_plans.organization_id",
                "workflow_plans.plan_id",
                "workflow_plans.creator_user_id",
            ),
        )
        self.assertEqual(
            foreign_keys["fk_assistant_attachments_conversation"],
            (
                "assistant_conversations.organization_id",
                "assistant_conversations.conversation_id",
                "assistant_conversations.creator_user_id",
            ),
        )
        self.assertEqual(
            foreign_keys["fk_assistant_attachments_organization"],
            ("organizations.organization_id",),
        )
        self.assertEqual(
            foreign_keys["fk_assistant_attachments_proposed_project"],
            ("project_ownership.organization_id", "project_ownership.project_id"),
        )

    def test_proposal_contract_has_attachment_scope_confirmation_and_cas(self) -> None:
        required = {
            "organization_id",
            "proposal_id",
            "attachment_id",
            "creator_user_id",
            "target_project_id",
            "plan_id",
            "target_kind",
            "idempotency_key",
            "normalized_diff",
            "revision",
            "status",
            "confirmed_by",
            "confirmed_at",
            "resulting_entity_refs",
            "standardized_error_code",
        }
        self.assertTrue(required.issubset(assistant_import_proposals.c.keys()))
        self.assertEqual(
            {column.name for column in assistant_import_proposals.primary_key.columns},
            {"organization_id", "proposal_id"},
        )
        foreign_keys = {
            constraint.name: tuple(element.target_fullname for element in constraint.elements)
            for constraint in assistant_import_proposals.foreign_key_constraints
        }
        self.assertEqual(
            foreign_keys["fk_assistant_import_proposals_attachment"],
            (
                "assistant_attachments.organization_id",
                "assistant_attachments.attachment_id",
                "assistant_attachments.creator_user_id",
            ),
        )
        attachment_fk = next(
            constraint
            for constraint in assistant_import_proposals.foreign_key_constraints
            if constraint.name == "fk_assistant_import_proposals_attachment"
        )
        self.assertEqual("CASCADE", attachment_fk.ondelete)
        self.assertEqual(
            foreign_keys["fk_assistant_import_proposals_organization"],
            ("organizations.organization_id",),
        )
        self.assertEqual(
            foreign_keys["fk_assistant_import_proposals_target_project"],
            ("project_ownership.organization_id", "project_ownership.project_id"),
        )
        checks = {
            constraint.name: str(constraint.sqltext)
            for constraint in assistant_import_proposals.constraints
            if isinstance(constraint, sa.CheckConstraint)
        }
        self.assertIn("revision >= 0", checks["ck_assistant_import_proposals_revision"])
        self.assertIn("confirmed_at IS NULL", checks["ck_assistant_import_proposals_confirmation"])
        self.assertIn(
            "target_project_id IS NOT NULL",
            checks["ck_assistant_import_proposals_runnable_target"],
        )
        self.assertIn(
            "target_kind <> 'needs_user_choice'",
            checks["ck_assistant_import_proposals_runnable_target"],
        )

    def test_postgresql_ddl_compiles_for_both_tables_and_indexes(self) -> None:
        dialect = postgresql.dialect()
        for table in (assistant_attachments, assistant_import_proposals):
            ddl = str(CreateTable(table).compile(dialect=dialect))
            self.assertIn(f"CREATE TABLE {table.name}", ddl)
            for index in table.indexes:
                self.assertIn(
                    "CREATE INDEX",
                    str(CreateIndex(index).compile(dialect=dialect)),
                )

    def test_migration_is_single_head_after_0029(self) -> None:
        migration_path = (
            BACKEND_DIR
            / "migrations"
            / "versions"
            / "20260820_0030_workflow_assistant_m2_imports.py"
        )
        spec = importlib.util.spec_from_file_location("m2_import_migration", migration_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader if spec else None)
        module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(module)  # type: ignore[union-attr]

        self.assertEqual(module.revision, "20260820_0030")
        self.assertEqual(module.down_revision, "20260820_0029")


if __name__ == "__main__":
    unittest.main()
