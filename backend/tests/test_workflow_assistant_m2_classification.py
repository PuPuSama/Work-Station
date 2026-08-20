from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import get_args

from pydantic import ValidationError


BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from models import PromptKind  # noqa: E402
from workflow_assistant.classification import (  # noqa: E402
    AttachmentClassification,
    AttachmentClassificationKind,
)


class WorkflowAssistantM2ClassificationTests(unittest.TestCase):
    def test_classification_kind_is_closed(self) -> None:
        self.assertEqual(
            set(get_args(AttachmentClassificationKind)),
            {
                "knowledge_source",
                "prompt_asset",
                "task_workbook",
                "project_notes",
                "topic_library",
                "unsupported",
                "needs_user_choice",
            },
        )
        with self.assertRaises(ValidationError):
            AttachmentClassification(
                classification="executable",  # type: ignore[arg-type]
                target_project_id="project-a",
                reason="Not an allowed business type.",
                confidence=1,
            )

    def test_prompt_kinds_reuse_the_existing_closed_contract(self) -> None:
        self.assertEqual(
            set(get_args(PromptKind)),
            {"outline", "article", "review", "humanize"},
        )
        with self.assertRaises(ValidationError):
            AttachmentClassification(
                classification="prompt_asset",
                target_project_id="project-a",
                prompt_kind="title",  # type: ignore[arg-type]
                reason="The model invented a prompt kind.",
                confidence=0.9,
            )

    def test_definite_prompt_requires_project_and_prompt_kind(self) -> None:
        accepted = AttachmentClassification(
            classification="prompt_asset",
            target_project_id="project-a",
            prompt_kind="outline",
            reason="Explicitly labelled outline prompt for project A.",
            confidence=0.98,
        )
        self.assertEqual(accepted.prompt_kind, "outline")

        for missing in ("target_project_id", "prompt_kind"):
            payload = {
                "classification": "prompt_asset",
                "target_project_id": "project-a",
                "prompt_kind": "outline",
                "reason": "Incomplete target.",
                "confidence": 0.5,
            }
            payload[missing] = None
            with self.subTest(missing=missing), self.assertRaisesRegex(
                ValidationError,
                "must need user choice",
            ):
                AttachmentClassification.model_validate(payload)

    def test_missing_project_ambiguity_and_incompatible_structure_require_choice(self) -> None:
        cases = (
            {"target_project_id": None},
            {"target_project_id": "project-a", "is_ambiguous": True},
            {"target_project_id": "project-a", "structure_compatible": False},
            {"target_project_id": "project-a", "affects_multiple_projects": True},
        )
        for fields in cases:
            with self.subTest(fields=fields), self.assertRaisesRegex(
                ValidationError,
                "must need user choice",
            ):
                AttachmentClassification(
                    classification="knowledge_source",
                    reason="Unsafe definitive classification.",
                    confidence=0.6,
                    **fields,
                )

    def test_needs_user_choice_records_the_unresolved_choice(self) -> None:
        missing_project = AttachmentClassification(
            classification="needs_user_choice",
            reason="Choose the target project.",
            confidence=0.5,
        )
        self.assertIsNone(missing_project.target_project_id)

        ambiguous = AttachmentClassification(
            classification="needs_user_choice",
            target_project_id="project-a",
            candidate_classifications=["project_notes", "prompt_asset"],
            is_ambiguous=True,
            reason="The document mixes instructions and project notes.",
            confidence=0.5,
        )
        self.assertTrue(ambiguous.is_ambiguous)

    def test_unsupported_attachment_has_no_import_target(self) -> None:
        accepted = AttachmentClassification(
            classification="unsupported",
            reason="Executable attachment is outside the contract.",
            confidence=1,
        )
        self.assertEqual(accepted.classification, "unsupported")

        with self.assertRaisesRegex(ValidationError, "cannot name an import target"):
            AttachmentClassification(
                classification="unsupported",
                target_project_id="project-a",
                reason="Unsafe target.",
                confidence=1,
            )

    def test_extra_fields_are_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            AttachmentClassification.model_validate(
                {
                    "classification": "knowledge_source",
                    "target_project_id": "project-a",
                    "reason": "Known source.",
                    "confidence": 0.8,
                    "tool_name": "shell",
                }
            )


if __name__ == "__main__":
    unittest.main()
