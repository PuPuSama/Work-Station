from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.access_control import ActorIdentity  # noqa: E402
from services.server_auth import SERVER_AUTH_COOKIE_NAME  # noqa: E402
from workflow_assistant.attachment_jobs import AttachmentJob  # noqa: E402
from workflow_assistant.attachment_review_http import (  # noqa: E402
    CancelImportProposalRequest,
    ClassifyAttachmentRequest,
    ConfirmImportProposalRequest,
    CreateImportProposalRequest,
    ReviseImportProposalRequest,
    cancel_import_proposal,
    classify_attachment,
    confirm_import_proposal,
    create_import_proposal,
    get_attachment_job,
    get_import_proposal,
    revise_import_proposal,
)
from workflow_assistant.attachments import (  # noqa: E402
    AttachmentDownload,
    AttachmentNotFound,
    AssistantAttachment,
)
from workflow_assistant.import_proposals import ImportProposal  # noqa: E402
from workflow_assistant.repository import WorkflowAssistantNotFound  # noqa: E402


UTC = timezone.utc
NOW = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)
ACTOR = ActorIdentity("org-a", "user-a")


def attachment(*, revision: int = 2, project: str | None = "project-a") -> AssistantAttachment:
    return AssistantAttachment(
        attachment_id="asa-a",
        organization_id=ACTOR.organization_id,
        creator_user_id=ACTOR.user_id,
        conversation_id="conv-a",
        proposed_project_id=project,
        plan_id=None,
        idempotency_key="upload-a",
        object_key="organizations/org-a/workflow-assistant/temporary/asa-a/hash",
        original_filename="facts.md",
        mime_type="text/markdown",
        byte_size=20,
        sha256="a" * 64,
        classification="knowledge_source",
        classification_payload={"classification": "knowledge_source"},
        revision=revision,
        status="proposal_ready",
        expires_at=NOW + timedelta(days=7),
        created_at=NOW,
        updated_at=NOW,
    )


def proposal(*, status: str = "awaiting_confirmation", revision: int = 3) -> ImportProposal:
    return ImportProposal(
        proposal_id="aip-a",
        organization_id=ACTOR.organization_id,
        attachment_id="asa-a",
        creator_user_id=ACTOR.user_id,
        target_project_id="project-a",
        plan_id=None,
        target_kind="knowledge_source",
        idempotency_key="proposal-a",
        normalized_diff={"documents": [{"title": "Facts"}]},
        revision=revision,
        status=status,  # type: ignore[arg-type]
        confirmed_by=ACTOR.user_id if status == "confirmed" else None,
        confirmed_at=NOW if status == "confirmed" else None,
        resulting_entity_refs=(),
        standardized_error_code=None,
        created_at=NOW,
        updated_at=NOW,
    )


def job(operation: str, *, proposal_id: str | None = None) -> AttachmentJob:
    project_id = "project-a" if operation != "classify_attachment" else None
    expected_proposal_revision = (
        3 if operation == "execute_import_proposal" else None
    )
    return AttachmentJob(
        job_id=f"job-{operation}",
        organization_id=ACTOR.organization_id,
        requested_by_user_id=ACTOR.user_id,
        project_id=project_id,
        attachment_id="asa-a",
        proposal_id=proposal_id,
        operation=operation,  # type: ignore[arg-type]
        idempotency_key=f"idem-{operation}",
        expected_attachment_revision=2,
        expected_proposal_revision=expected_proposal_revision,
    )


class AttachmentService:
    def __init__(self, record: AssistantAttachment | None = None) -> None:
        self.record = record or attachment()

    def upload(self, **_kwargs):
        return self.record

    def get(self, **kwargs):
        if kwargs["attachment_id"] != self.record.attachment_id:
            raise AttachmentNotFound("missing")
        return self.record

    def list(self, **_kwargs):
        return (self.record,)

    def create_download(self, **_kwargs):
        return AttachmentDownload(self.record, "https://objects.test/file", 300)

    def reject(self, **_kwargs):
        return self.record


class ConversationRepository:
    def __init__(self, visible: bool = True) -> None:
        self.visible = visible

    def get_conversation(self, *, actor, conversation_id, include_messages=False):
        del actor, include_messages
        if not self.visible or conversation_id != "conv-a":
            raise WorkflowAssistantNotFound("missing")
        return object()


class Security:
    def __init__(self, *, allowed: bool = True) -> None:
        self.allowed = allowed
        self.calls: list[tuple[str, str, str]] = []

    def authorize_project(self, *, token, project, permission):
        self.calls.append((token, project, permission))
        if not self.allowed:
            from services.server_request_security import ServerRequestForbidden

            raise ServerRequestForbidden("denied")
        return SimpleNamespace(actor=ACTOR, project_id=project)


class Workflow:
    def __init__(self) -> None:
        self.current = proposal()
        self.calls: list[tuple[str, dict[str, object]]] = []

    def enqueue_classification(self, **kwargs):
        self.calls.append(("classify", kwargs))
        return job("classify_attachment")

    def enqueue_proposal_preview(self, **kwargs):
        self.calls.append(("create_preview", kwargs))
        return job("preview_import_proposal")

    def get_job(self, **kwargs):
        self.calls.append(("get_job", kwargs))
        return replace(
            job("preview_import_proposal"),
            status="succeeded",
            result_payload={"proposal_id": "aip-a", "proposal_revision": 3},
            result_attachment_revision=2,
            result_proposal_revision=3,
        )

    def get_proposal(self, **kwargs):
        self.calls.append(("get", kwargs))
        return self.current

    def revise_proposal(self, **kwargs):
        self.calls.append(("revise", kwargs))
        self.current = replace(
            self.current,
            normalized_diff=dict(kwargs["normalized_diff"]),
            revision=int(kwargs["expected_revision"]) + 1,
        )
        return self.current

    def confirm_proposal(self, **kwargs):
        self.calls.append(("confirm", kwargs))
        self.current = replace(
            self.current,
            status="confirmed",
            revision=int(kwargs["expected_revision"]) + 1,
            confirmed_by=ACTOR.user_id,
            confirmed_at=NOW,
        )
        return self.current

    def cancel_proposal(self, **kwargs):
        self.calls.append(("cancel", kwargs))
        self.current = replace(self.current, status="cancelled")
        return self.current

    def enqueue_import_proposal(self, **kwargs):
        self.calls.append(("enqueue_import", kwargs))
        return job("execute_import_proposal", proposal_id="aip-a")


def request(
    *,
    workflow: object | None = None,
    service: AttachmentService | None = None,
    repository: ConversationRepository | None = None,
    security: Security | None = None,
    master: bool = True,
    attachments: bool = True,
):
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                article_agent_config=SimpleNamespace(
                    workflow_assistant_enabled=master,
                    workflow_assistant_attachments_enabled=attachments,
                ),
                workflow_assistant_attachment_service=service or AttachmentService(),
                workflow_assistant_repository=repository or ConversationRepository(),
                workflow_assistant_attachment_review_workflow=(
                    workflow if workflow is not None else Workflow()
                ),
                server_request_security=security or Security(),
            )
        ),
        cookies={SERVER_AUTH_COOKIE_NAME: "session-a"},
    )


class AttachmentReviewHttpTests(unittest.TestCase):
    def test_classify_only_enqueues_and_reauthorizes_preselected_project(self) -> None:
        workflow = Workflow()
        security = Security()
        result = classify_attachment(
            "asa-a",
            ClassifyAttachmentRequest(
                conversation_id="conv-a",
                expected_attachment_revision=2,
                idempotency_key="classify-a",
            ),
            request(workflow=workflow, security=security),  # type: ignore[arg-type]
            actor=ACTOR,
        )
        self.assertEqual([name for name, _ in workflow.calls], ["classify"])
        self.assertEqual(result.job.operation, "classify_attachment")  # type: ignore[union-attr]
        self.assertEqual(result.attachment_stage, "classified")
        self.assertEqual(result.import_stage, "not_imported")
        self.assertEqual(result.publication_stage, "not_published")
        self.assertEqual(security.calls, [("session-a", "project-a", "project.view")])

    def test_create_only_queues_preview_and_does_not_accept_client_diff(self) -> None:
        workflow = Workflow()
        result = create_import_proposal(
            "asa-a",
            CreateImportProposalRequest(
                conversation_id="conv-a",
                expected_attachment_revision=2,
                idempotency_key="proposal-a",
                target_kind="knowledge_source",
                target_project_id="project-a",
            ),
            request(workflow=workflow),  # type: ignore[arg-type]
            actor=ACTOR,
        )
        self.assertEqual([name for name, _ in workflow.calls], ["create_preview"])
        call = workflow.calls[0][1]
        self.assertNotIn("normalized_diff", call)
        self.assertIsNone(result.proposal)
        self.assertEqual(result.proposal_stage, "preview_queued")
        self.assertEqual(result.job.operation, "preview_import_proposal")  # type: ignore[union-attr]

    def test_get_requires_attachment_conversation_ownership(self) -> None:
        with self.assertRaises(HTTPException) as captured:
            get_import_proposal(
                "aip-a",
                request=request(repository=ConversationRepository(False)),  # type: ignore[arg-type]
                conversation_id="conv-a",
                actor=ACTOR,
            )
        self.assertEqual(captured.exception.status_code, 404)

    def test_get_job_exposes_completed_preview_proposal_without_import_claim(self) -> None:
        workflow = Workflow()
        result = get_attachment_job(
            "job-preview_import_proposal",
            request=request(workflow=workflow),  # type: ignore[arg-type]
            conversation_id="conv-a",
            actor=ACTOR,
        )
        self.assertEqual("succeeded", result.job.status)  # type: ignore[union-attr]
        self.assertEqual("aip-a", result.job.result_payload["proposal_id"])  # type: ignore[union-attr]
        self.assertEqual("aip-a", result.proposal.proposal_id)  # type: ignore[union-attr]
        self.assertEqual("not_imported", result.import_stage)

    def test_revise_passes_both_revisions_and_reauthorizes_target(self) -> None:
        workflow = Workflow()
        security = Security()
        result = revise_import_proposal(
            "aip-a",
            ReviseImportProposalRequest(
                conversation_id="conv-a",
                expected_revision=3,
                expected_attachment_revision=2,
                target_kind="knowledge_source",
                target_project_id="project-a",
                normalized_diff={"documents": [{"title": "Revised"}]},
            ),
            request(workflow=workflow, security=security),  # type: ignore[arg-type]
            actor=ACTOR,
        )
        revise_call = next(payload for name, payload in workflow.calls if name == "revise")
        self.assertEqual(revise_call["expected_revision"], 3)
        self.assertEqual(revise_call["expected_attachment_revision"], 2)
        self.assertEqual(result.proposal.revision, 4)  # type: ignore[union-attr]
        self.assertEqual(security.calls, [("session-a", "project-a", "project.view")])

    def test_confirm_releases_and_enqueues_execution(self) -> None:
        workflow = Workflow()
        result = confirm_import_proposal(
            "aip-a",
            ConfirmImportProposalRequest(
                conversation_id="conv-a",
                target_project_id="project-a",
                expected_revision=3,
                expected_attachment_revision=2,
            ),
            request(workflow=workflow),  # type: ignore[arg-type]
            actor=ACTOR,
        )
        self.assertEqual(
            [name for name, _ in workflow.calls],
            ["get", "confirm", "enqueue_import"],
        )
        self.assertEqual(result.proposal_stage, "confirmed")
        self.assertEqual(result.import_stage, "not_imported")
        self.assertEqual(result.publication_stage, "not_published")
        self.assertIsNotNone(result.job)
        self.assertEqual(result.job.operation, "execute_import_proposal")  # type: ignore[union-attr]

    def test_cancel_reauthorizes_existing_target(self) -> None:
        workflow = Workflow()
        security = Security()
        result = cancel_import_proposal(
            "aip-a",
            CancelImportProposalRequest(conversation_id="conv-a", expected_revision=3),
            request(workflow=workflow, security=security),  # type: ignore[arg-type]
            actor=ACTOR,
        )
        self.assertEqual(result.proposal.status, "cancelled")  # type: ignore[union-attr]
        self.assertEqual(security.calls, [("session-a", "project-a", "project.view")])

    def test_revision_conflict_is_explicit_and_never_enqueues(self) -> None:
        workflow = Workflow()
        with self.assertRaises(HTTPException) as captured:
            classify_attachment(
                "asa-a",
                ClassifyAttachmentRequest(
                    conversation_id="conv-a",
                    expected_attachment_revision=1,
                    idempotency_key="classify-a",
                ),
                request(workflow=workflow),  # type: ignore[arg-type]
                actor=ACTOR,
            )
        self.assertEqual(captured.exception.status_code, 409)
        self.assertEqual(workflow.calls, [])

    def test_feature_gates_and_atomic_workflow_availability_fail_closed(self) -> None:
        with self.assertRaises(HTTPException) as disabled:
            classify_attachment(
                "asa-a",
                ClassifyAttachmentRequest(
                    conversation_id="conv-a",
                    expected_attachment_revision=2,
                    idempotency_key="classify-a",
                ),
                request(master=False),  # type: ignore[arg-type]
                actor=ACTOR,
            )
        self.assertEqual(disabled.exception.status_code, 404)

        with self.assertRaises(HTTPException) as unavailable:
            classify_attachment(
                "asa-a",
                ClassifyAttachmentRequest(
                    conversation_id="conv-a",
                    expected_attachment_revision=2,
                    idempotency_key="classify-a",
                ),
                request(workflow=object()),  # type: ignore[arg-type]
                actor=ACTOR,
            )
        self.assertEqual(unavailable.exception.status_code, 503)


if __name__ == "__main__":
    unittest.main()
