from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services.access_control import ActorIdentity  # noqa: E402
from workflow_assistant.attachments import AssistantAttachment  # noqa: E402
from workflow_assistant.import_proposals import (  # noqa: E402
    ImportProposal,
    ImportProposalConflict,
    ImportProposalNotFound,
    ImportProposalService,
    ImportProposalValidationError,
    normalized_json_object,
    proposal_review_status,
)


class FakeProposalRepository:
    def __init__(self) -> None:
        self.records: dict[str, ImportProposal] = {}
        self.create_by_key: dict[tuple[str, str, str], ImportProposal] = {}

    def create(self, proposal: ImportProposal) -> ImportProposal:
        key = (
            proposal.organization_id,
            proposal.attachment_id,
            proposal.idempotency_key,
        )
        existing = self.create_by_key.get(key)
        if existing is not None:
            if (
                existing.target_project_id,
                existing.target_kind,
                existing.normalized_diff,
            ) != (
                proposal.target_project_id,
                proposal.target_kind,
                proposal.normalized_diff,
            ):
                raise ImportProposalConflict("idempotency conflict")
            return existing
        self.records[proposal.proposal_id] = proposal
        self.create_by_key[key] = proposal
        return proposal

    def get_for_actor(
        self, *, actor: ActorIdentity, proposal_id: str
    ) -> ImportProposal | None:
        record = self.records.get(proposal_id)
        if record is None:
            return None
        if (
            record.organization_id != actor.organization_id
            or record.creator_user_id != actor.user_id
        ):
            return None
        return record

    def revise(self, **kwargs: object) -> ImportProposal:
        actor = kwargs["actor"]
        assert isinstance(actor, ActorIdentity)
        proposal_id = str(kwargs["proposal_id"])
        current = self.get_for_actor(actor=actor, proposal_id=proposal_id)
        if current is None:
            raise ImportProposalNotFound("not found")
        expected = int(kwargs["expected_revision"])
        if current.revision != expected:
            raise ImportProposalConflict(
                "revision conflict", current_revision=current.revision
            )
        target_kind = kwargs["target_kind"]
        target_project_id = kwargs["target_project_id"]
        updated = replace(
            current,
            target_kind=target_kind,  # type: ignore[arg-type]
            target_project_id=(
                str(target_project_id) if target_project_id is not None else None
            ),
            normalized_diff=dict(kwargs["normalized_diff"]),  # type: ignore[arg-type]
            revision=expected + 1,
            status=proposal_review_status(
                target_kind=target_kind,  # type: ignore[arg-type]
                target_project_id=(
                    str(target_project_id) if target_project_id is not None else None
                ),
                normalized_diff=kwargs["normalized_diff"],  # type: ignore[arg-type]
            ),
            updated_at=kwargs["now"],  # type: ignore[arg-type]
        )
        self.records[proposal_id] = updated
        return updated

    def confirm(self, **kwargs: object) -> ImportProposal:
        actor = kwargs["actor"]
        assert isinstance(actor, ActorIdentity)
        proposal_id = str(kwargs["proposal_id"])
        current = self.get_for_actor(actor=actor, proposal_id=proposal_id)
        if current is None:
            raise ImportProposalNotFound("not found")
        expected = int(kwargs["expected_revision"])
        if current.revision != expected:
            raise ImportProposalConflict(
                "revision conflict", current_revision=current.revision
            )
        if current.status != "awaiting_confirmation":
            raise ImportProposalConflict("proposal is not awaiting confirmation")
        kwargs["authorize_target"]()  # type: ignore[operator]
        updated = replace(
            current,
            target_project_id=str(kwargs["target_project_id"]),
            revision=expected + 1,
            status="confirmed",
            confirmed_by=actor.user_id,
            confirmed_at=kwargs["now"],  # type: ignore[arg-type]
            updated_at=kwargs["now"],  # type: ignore[arg-type]
        )
        self.records[proposal_id] = updated
        return updated

    def cancel(self, **kwargs: object) -> ImportProposal:
        actor = kwargs["actor"]
        assert isinstance(actor, ActorIdentity)
        proposal_id = str(kwargs["proposal_id"])
        current = self.get_for_actor(actor=actor, proposal_id=proposal_id)
        if current is None:
            raise ImportProposalNotFound("not found")
        expected = int(kwargs["expected_revision"])
        if current.revision != expected:
            raise ImportProposalConflict(
                "revision conflict", current_revision=current.revision
            )
        updated = replace(
            current,
            revision=expected + 1,
            status="cancelled",
            updated_at=kwargs["now"],  # type: ignore[arg-type]
        )
        self.records[proposal_id] = updated
        return updated


class ImportProposalServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 8, 20, 8, tzinfo=timezone.utc)
        self.actor = ActorIdentity("org-one", "user-one")
        self.other_actor = ActorIdentity("org-one", "user-two")
        self.attachment = self.make_attachment()
        self.attachments = {self.attachment.attachment_id: self.attachment}
        self.authorization_calls: list[tuple[str, str, str, str]] = []
        self.repository = FakeProposalRepository()
        self.service = ImportProposalService(
            repository=self.repository,
            attachment_loader=self.load_attachment,
            authorize_project=lambda actor, project, kind, stage: (
                self.authorization_calls.append(
                    (actor.user_id, project, kind, stage)
                )
            ),
        )

    def make_attachment(
        self,
        *,
        classification: str = "knowledge_source",
        classification_payload: dict[str, object] | None = None,
        status: str = "proposal_ready",
        creator_user_id: str = "user-one",
    ) -> AssistantAttachment:
        payload = classification_payload or {
            "schema_version": 1,
            "classification": {
                "classification": classification,
                "target_project_id": "project-a",
                "reason": "Explicit project knowledge source.",
                "confidence": 0.99,
            },
            "source": {"text_preview": "private"},
            "model_identity": "model-one",
            "source_sha256": "a" * 64,
        }
        return AssistantAttachment(
            attachment_id="attachment-one",
            organization_id="org-one",
            creator_user_id=creator_user_id,
            conversation_id="conversation-one",
            proposed_project_id="project-a",
            plan_id=None,
            idempotency_key="upload-one",
            object_key="private/attachment-one",
            original_filename="source.md",
            mime_type="text/markdown",
            byte_size=10,
            sha256="a" * 64,
            classification=classification,
            classification_payload=payload,
            revision=2,
            status=status,  # type: ignore[arg-type]
            expires_at=self.now + timedelta(days=7),
            created_at=self.now,
            updated_at=self.now,
        )

    def load_attachment(
        self, actor: ActorIdentity, attachment_id: str
    ) -> AssistantAttachment | None:
        record = self.attachments.get(attachment_id)
        if record is None:
            return None
        if (
            record.organization_id != actor.organization_id
            or record.creator_user_id != actor.user_id
        ):
            return None
        return record

    def test_json_diff_is_detached_and_rejects_non_json_values(self) -> None:
        source = {"create": [{"name": "Pump"}], "count": 1}
        safe = normalized_json_object(source)
        source["count"] = 2
        self.assertEqual(safe["count"], 1)
        for invalid in (
            {"score": float("nan")},
            {"payload": b"unsafe"},
            {"payload": object()},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(
                ImportProposalValidationError
            ):
                normalized_json_object(invalid)

    def test_create_is_actor_scoped_classification_bound_and_authorized(self) -> None:
        created = self.service.create(
            actor=self.actor,
            attachment_id=self.attachment.attachment_id,
            target_project_id="project-a",
            target_kind="knowledge_source",
            normalized_diff={"create": [{"source": "source.md"}]},
            idempotency_key="proposal-one",
            proposal_id="proposal-one",
            now=self.now,
        )
        replay = self.service.create(
            actor=self.actor,
            attachment_id=self.attachment.attachment_id,
            target_project_id="project-a",
            target_kind="knowledge_source",
            normalized_diff={"create": [{"source": "source.md"}]},
            idempotency_key="proposal-one",
            proposal_id="proposal-loser",
            now=self.now,
        )

        self.assertEqual(replay, created)
        self.assertEqual(created.status, "awaiting_confirmation")
        self.assertEqual(
            self.authorization_calls,
            [
                ("user-one", "project-a", "knowledge_source", "preview"),
                ("user-one", "project-a", "knowledge_source", "preview"),
            ],
        )
        with self.assertRaises(ImportProposalNotFound):
            self.service.create(
                actor=self.other_actor,
                attachment_id=self.attachment.attachment_id,
                target_project_id="project-a",
                target_kind="knowledge_source",
                normalized_diff={},
                idempotency_key="cross-user",
                now=self.now,
            )

    def test_conflicts_and_truncated_workbooks_remain_draft(self) -> None:
        for index, diff in enumerate(
            (
                {"create": [], "conflicts": [{"reason": "duplicate"}], "invalid": []},
                {
                    "workbook": {"truncated": True},
                    "create": [{"topic": "Only the visible prefix"}],
                    "conflicts": [],
                    "invalid": [],
                },
            )
        ):
            with self.subTest(diff=diff):
                created = self.service.create(
                    actor=self.actor,
                    attachment_id=self.attachment.attachment_id,
                    target_project_id="project-a",
                    target_kind="knowledge_source",
                    normalized_diff=diff,
                    idempotency_key=f"unresolved-{index}",
                    proposal_id=f"unresolved-{index}",
                    now=self.now,
                )
                self.assertEqual("draft", created.status)
                with self.assertRaises(ImportProposalConflict):
                    self.service.confirm(
                        actor=self.actor,
                        proposal_id=created.proposal_id,
                        expected_revision=created.revision,
                        target_project_id="project-a",
                        now=self.now + timedelta(minutes=1),
                    )
        with self.assertRaisesRegex(
            ImportProposalValidationError, "does not match"
        ):
            self.service.create(
                actor=self.actor,
                attachment_id=self.attachment.attachment_id,
                target_project_id="project-a",
                target_kind="topic_library",
                normalized_diff={},
                idempotency_key="wrong-kind",
                now=self.now,
            )

    def test_prompt_asset_requires_existing_explicit_prompt_kind(self) -> None:
        prompt = self.make_attachment(
            classification="prompt_asset",
            classification_payload={
                "classification": "prompt_asset",
                "target_project_id": "project-a",
                "prompt_kind": "outline",
                "reason": "Explicit outline prompt.",
                "confidence": 0.99,
            },
        )
        self.attachments[prompt.attachment_id] = prompt
        for diff in ({}, {"prompt_kind": "title"}, {"prompt_kind": "article"}):
            with self.subTest(diff=diff), self.assertRaises(
                ImportProposalValidationError
            ):
                self.service.create(
                    actor=self.actor,
                    attachment_id=prompt.attachment_id,
                    target_project_id="project-a",
                    target_kind="prompt_asset",
                    normalized_diff=diff,
                    idempotency_key=f"prompt-{len(diff)}",
                    now=self.now,
                )

    def test_needs_choice_can_stay_draft_then_revision_reauthorizes_target(self) -> None:
        choice = self.make_attachment(
            classification="needs_user_choice",
            classification_payload={
                "classification": "needs_user_choice",
                "candidate_classifications": [
                    "project_notes",
                    "topic_library",
                ],
                "is_ambiguous": True,
                "reason": "Choose notes or topics and a project.",
                "confidence": 0.5,
            },
            status="needs_user_choice",
        )
        self.attachments[choice.attachment_id] = choice
        draft = self.service.create(
            actor=self.actor,
            attachment_id=choice.attachment_id,
            target_kind="needs_user_choice",
            normalized_diff={"unresolved": ["kind", "project"]},
            idempotency_key="choice-one",
            proposal_id="choice-proposal",
            now=self.now,
        )
        self.assertEqual(draft.status, "draft")
        self.assertEqual(self.authorization_calls, [])

        revised = self.service.revise(
            actor=self.actor,
            proposal_id=draft.proposal_id,
            expected_revision=0,
            target_project_id="project-a",
            target_kind="topic_library",
            normalized_diff={"create": [{"topic": "Pump sizing"}]},
            now=self.now + timedelta(minutes=1),
        )
        self.assertEqual(revised.status, "awaiting_confirmation")
        self.assertEqual(revised.revision, 1)
        self.assertEqual(
            self.authorization_calls,
            [("user-one", "project-a", "topic_library", "preview")],
        )

    def test_confirm_reloads_attachment_reauthorizes_and_only_releases_proposal(self) -> None:
        created = self.service.create(
            actor=self.actor,
            attachment_id=self.attachment.attachment_id,
            target_project_id="project-a",
            target_kind="knowledge_source",
            normalized_diff={"create": [{"source": "source.md"}]},
            idempotency_key="confirm-one",
            proposal_id="confirm-proposal",
            now=self.now,
        )
        self.authorization_calls.clear()

        confirmed = self.service.confirm(
            actor=self.actor,
            proposal_id=created.proposal_id,
            expected_revision=created.revision,
            target_project_id="project-a",
            now=self.now + timedelta(minutes=1),
        )

        self.assertEqual(confirmed.status, "confirmed")
        self.assertEqual(confirmed.resulting_entity_refs, ())
        self.assertIsNone(confirmed.standardized_error_code)
        self.assertEqual(
            self.authorization_calls,
            [("user-one", "project-a", "knowledge_source", "confirm")],
        )
        # Confirmation did not mutate/import/publish the source attachment.
        self.assertEqual(self.attachments[self.attachment.attachment_id].status, "proposal_ready")

    def test_confirm_fails_if_current_attachment_classification_changed(self) -> None:
        created = self.service.create(
            actor=self.actor,
            attachment_id=self.attachment.attachment_id,
            target_project_id="project-a",
            target_kind="knowledge_source",
            normalized_diff={},
            idempotency_key="changed-one",
            proposal_id="changed-proposal",
            now=self.now,
        )
        self.authorization_calls.clear()
        self.attachments[self.attachment.attachment_id] = replace(
            self.attachment,
            classification="topic_library",
            classification_payload={
                "classification": "topic_library",
                "target_project_id": "project-a",
                "reason": "Reclassified.",
                "confidence": 1,
            },
        )
        with self.assertRaises(ImportProposalValidationError):
            self.service.confirm(
                actor=self.actor,
                proposal_id=created.proposal_id,
                expected_revision=0,
                target_project_id="project-a",
                now=self.now + timedelta(minutes=1),
            )
        self.assertEqual(self.authorization_calls, [])

    def test_confirm_fails_if_current_attachment_expired(self) -> None:
        created = self.service.create(
            actor=self.actor,
            attachment_id=self.attachment.attachment_id,
            target_project_id="project-a",
            target_kind="knowledge_source",
            normalized_diff={},
            idempotency_key="expiry-one",
            proposal_id="expiry-proposal",
            now=self.now,
        )
        self.authorization_calls.clear()
        self.attachments[self.attachment.attachment_id] = replace(
            self.attachment,
            expires_at=self.now + timedelta(seconds=30),
        )
        with self.assertRaises(ImportProposalConflict) as caught:
            self.service.confirm(
                actor=self.actor,
                proposal_id=created.proposal_id,
                expected_revision=0,
                target_project_id="project-a",
                now=self.now + timedelta(minutes=1),
            )
        self.assertEqual(caught.exception.code, "attachment_not_available")
        self.assertEqual(self.authorization_calls, [])


if __name__ == "__main__":
    unittest.main()
