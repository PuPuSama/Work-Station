from __future__ import annotations

import unittest
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from services.access_control import ActorIdentity
from workflow_assistant.attachment_jobs import AttachmentJob, PendingAttachmentJobAuthorization
from workflow_assistant.attachment_review import AttachmentReviewWorkflowService
from workflow_assistant.attachments import AssistantAttachment
from workflow_assistant.import_proposals import ImportProposal


NOW = datetime(2026, 8, 20, tzinfo=timezone.utc)
ACTOR = ActorIdentity("org-1", "user-1")


def _attachment(*, choice: bool = False) -> AssistantAttachment:
    classification = {
        "classification": "needs_user_choice" if choice else "knowledge_source",
        "reason": "Reviewable project material.",
        "confidence": 0.8,
        "target_project_id": "project-1" if not choice else None,
        "prompt_kind": None,
        "candidate_classifications": (
            ["knowledge_source", "project_notes"] if choice else []
        ),
        "is_ambiguous": choice,
        "structure_compatible": True,
        "affects_multiple_projects": False,
    }
    return AssistantAttachment(
        attachment_id="attachment-1",
        organization_id=ACTOR.organization_id,
        creator_user_id=ACTOR.user_id,
        conversation_id="conversation-1",
        proposed_project_id=None if choice else "project-1",
        plan_id=None,
        idempotency_key="upload-1",
        object_key="temporary/object",
        original_filename="notes.md",
        mime_type="text/markdown",
        byte_size=10,
        sha256="a" * 64,
        classification=str(classification["classification"]),
        classification_payload={
            "schema_version": 1,
            "classification": classification,
        },
        revision=2,
        status="needs_user_choice" if choice else "proposal_ready",
        expires_at=NOW + timedelta(days=7),
        created_at=NOW,
        updated_at=NOW,
    )


class _AttachmentRepository:
    def __init__(self, attachment: AssistantAttachment) -> None:
        self.attachment = attachment
        self.resolved_payload: dict[str, object] | None = None

    def resolve_classification_choice(self, **kwargs: object) -> AssistantAttachment:
        self.resolved_payload = dict(kwargs["classification_payload"])  # type: ignore[arg-type]
        self.attachment = replace(
            self.attachment,
            classification=str(kwargs["classification"]),
            classification_payload=self.resolved_payload,
            revision=self.attachment.revision + 1,
            status="proposal_ready",
        )
        return self.attachment

    def get_by_id_for_actor(self, **_kwargs: object) -> AssistantAttachment:
        return self.attachment


class _Access:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def require(self, _actor: ActorIdentity, project_id: str, permission: str) -> None:
        self.calls.append((project_id, permission))


class AttachmentReviewWorkflowTests(unittest.TestCase):
    def service(self, attachment: AssistantAttachment) -> AttachmentReviewWorkflowService:
        service = object.__new__(AttachmentReviewWorkflowService)
        service._attachments = _AttachmentRepository(attachment)  # type: ignore[attr-defined]
        service._access = _Access()  # type: ignore[attr-defined]
        return service

    def test_explicit_choice_is_revisioned_before_preview_enqueue(self) -> None:
        service = self.service(_attachment(choice=True))
        resolved = service._resolve_choice(
            actor=ACTOR,
            attachment=service._attachments.attachment,  # type: ignore[attr-defined]
            target_kind="knowledge_source",
            target_project_id="project-1",
            prompt_kind=None,
            request_idempotency_key="preview-1",
            source_revision=2,
        )
        nested = resolved.classification_payload["classification"]
        self.assertEqual("knowledge_source", nested["classification"])  # type: ignore[index]
        self.assertEqual("project-1", nested["target_project_id"])  # type: ignore[index]
        self.assertEqual(3, resolved.revision)
        self.assertEqual(
            "explicit_user_choice",
            resolved.classification_payload["resolution"]["kind"],  # type: ignore[index]
        )

    def test_review_revision_only_allows_exact_entry_exclusions(self) -> None:
        original = {
            "schema_version": 1,
            "target_kind": "knowledge_source",
            "create": [{"title": "A", "content_hash": "a" * 64}],
            "update": [],
            "skip": [],
            "conflicts": [{"reason": "duplicate"}],
            "invalid": [],
        }
        reviewed = AttachmentReviewWorkflowService._reviewed_subset(
            original,
            {**original, "conflicts": []},
        )
        self.assertEqual([], reviewed["conflicts"])
        with self.assertRaises(ValueError):
            AttachmentReviewWorkflowService._reviewed_subset(
                original,
                {
                    **original,
                    "create": [{"title": "Injected", "content_hash": "b" * 64}],
                },
            )

    def test_projectless_job_still_reauthorizes_active_workspace_actor(self) -> None:
        service = self.service(_attachment())

        class Result:
            def scalar_one_or_none(self) -> None:
                return None

        class Connection:
            def execute(self, _statement: object) -> Result:
                return Result()

        service._engine = type(  # type: ignore[attr-defined]
            "Engine",
            (),
            {"connect": lambda _self: nullcontext(Connection())},
        )()
        candidate = PendingAttachmentJobAuthorization(
            job_id="job-projectless",
            organization_id=ACTOR.organization_id,
            requested_by_user_id=ACTOR.user_id,
            project_id=None,
            attachment_id="attachment-1",
            proposal_id=None,
            operation="classify_attachment",
        )
        with self.assertRaises(PermissionError):
            service._authorize_job(candidate, "execute")

    def test_incompatible_choice_fails_closed(self) -> None:
        attachment = _attachment(choice=True)
        payload = dict(attachment.classification_payload)
        nested = dict(payload["classification"])  # type: ignore[arg-type]
        nested["structure_compatible"] = False
        payload["classification"] = nested
        service = self.service(replace(attachment, classification_payload=payload))
        with self.assertRaises(ValueError):
            service._resolve_choice(
                actor=ACTOR,
                attachment=service._attachments.attachment,  # type: ignore[attr-defined]
                target_kind="knowledge_source",
                target_project_id="project-1",
                prompt_kind=None,
                request_idempotency_key="preview-1",
                source_revision=2,
            )

    def test_choice_resolution_can_replay_the_same_enqueue_idempotency(self) -> None:
        service = self.service(_attachment(choice=True))
        jobs: list[AttachmentJob] = []

        class Jobs:
            def enqueue(_self, **kwargs: object) -> AttachmentJob:
                if jobs:
                    return jobs[0]
                job = AttachmentJob(
                    job_id="job-preview",
                    organization_id=ACTOR.organization_id,
                    requested_by_user_id=ACTOR.user_id,
                    project_id="project-1",
                    attachment_id="attachment-1",
                    proposal_id=None,
                    operation="preview_import_proposal",
                    idempotency_key=str(kwargs["idempotency_key"]),
                    expected_attachment_revision=int(
                        kwargs["expected_attachment_revision"]
                    ),
                    expected_proposal_revision=None,
                    request_payload=kwargs["request_payload"],  # type: ignore[arg-type]
                )
                jobs.append(job)
                return job

        service._job_repository = lambda _organization_id: Jobs()  # type: ignore[method-assign]
        service.wake = lambda: None  # type: ignore[method-assign]
        first = service.enqueue_proposal_preview(
            actor=ACTOR,
            attachment=service._attachments.attachment,  # type: ignore[attr-defined]
            target_kind="knowledge_source",
            target_project_id="project-1",
            plan_id=None,
            idempotency_key="preview-1",
            expected_attachment_revision=2,
        )
        second = service.enqueue_proposal_preview(
            actor=ACTOR,
            attachment=service._attachments.attachment,  # type: ignore[attr-defined]
            target_kind="knowledge_source",
            target_project_id="project-1",
            plan_id=None,
            idempotency_key="preview-1",
            expected_attachment_revision=2,
        )
        self.assertEqual(first, second)
        self.assertEqual(3, first.expected_attachment_revision)

    def test_preview_handler_builds_diff_before_creating_proposal(self) -> None:
        attachment = _attachment()
        service = self.service(attachment)
        calls: list[str] = []
        service._targets = type(  # type: ignore[attr-defined]
            "Targets",
            (),
            {"load": lambda _self, **_kwargs: calls.append("snapshot") or object()},
        )()
        service._preview = type(  # type: ignore[attr-defined]
            "Preview",
            (),
            {"build": lambda _self, **_kwargs: calls.append("build") or {"create": []}},
        )()
        proposal = ImportProposal(
            proposal_id="proposal-1",
            organization_id=ACTOR.organization_id,
            attachment_id=attachment.attachment_id,
            creator_user_id=ACTOR.user_id,
            target_project_id="project-1",
            plan_id=None,
            target_kind="knowledge_source",
            idempotency_key="preview-job:job-1",
            normalized_diff={"create": []},
            revision=0,
            status="awaiting_confirmation",
            confirmed_by=None,
            confirmed_at=None,
            resulting_entity_refs=(),
            standardized_error_code=None,
            created_at=NOW,
            updated_at=NOW,
        )
        service._proposal_service = type(  # type: ignore[attr-defined]
            "Proposals",
            (),
            {"create": lambda _self, **_kwargs: calls.append("create") or proposal},
        )()
        job = AttachmentJob(
            job_id="job-1",
            organization_id=ACTOR.organization_id,
            requested_by_user_id=ACTOR.user_id,
            project_id="project-1",
            attachment_id=attachment.attachment_id,
            proposal_id=None,
            operation="preview_import_proposal",
            idempotency_key="request-1",
            expected_attachment_revision=attachment.revision,
            expected_proposal_revision=None,
            request_payload={
                "target_kind": "knowledge_source",
                "target_project_id": "project-1",
            },
        )
        result = service._handle_preview(job, lambda: False, lambda: None)
        self.assertEqual(["snapshot", "build", "create"], calls)
        self.assertEqual("proposal-1", result.result_payload["proposal_id"])
        self.assertEqual(attachment.revision, result.attachment_revision)


if __name__ == "__main__":
    unittest.main()
