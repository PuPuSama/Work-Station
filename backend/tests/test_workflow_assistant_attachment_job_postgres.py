from __future__ import annotations

import os
import sys
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sqlalchemy as sa


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from knowledge_agent.database import create_knowledge_engine  # noqa: E402
from knowledge_agent.schema import projects  # noqa: E402
from server_schema import (  # noqa: E402
    assistant_attachment_jobs,
    assistant_attachments,
    assistant_conversations,
    assistant_import_proposals,
    audit_events,
    organizations,
    project_ownership,
    workspace_users,
)
from workflow_assistant.attachment_job_repository import (  # noqa: E402
    PostgresAttachmentJobOrganizationDiscovery,
    PostgresAttachmentJobRepository,
)
from workflow_assistant.attachment_jobs import (  # noqa: E402
    AttachmentJobConflict,
    AttachmentJobResult,
)


@unittest.skipUnless(
    os.environ.get("ARTICLE_AGENT_DATABASE_URL"),
    "ARTICLE_AGENT_DATABASE_URL is required for PostgreSQL integration tests",
)
class AttachmentJobPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = create_knowledge_engine(os.environ["ARTICLE_AGENT_DATABASE_URL"])

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    def setUp(self) -> None:
        prefix = f"attachment-job-{uuid.uuid4().hex}"
        self.organization_id = f"{prefix}-org"
        self.user_id = f"{prefix}-user"
        self.other_user_id = f"{prefix}-other"
        self.project_id = f"{prefix}-project"
        self.conversation_id = f"{prefix}-conversation"
        self.attachment_id = f"{prefix}-attachment"
        self.proposal_id = f"{prefix}-proposal"
        self.now = datetime.now(timezone.utc)
        self.repository = PostgresAttachmentJobRepository(
            self.engine,
            organization_id=self.organization_id,
            worker_id=f"{prefix}-worker",
            lease_seconds=60,
        )
        with self.engine.begin() as connection:
            connection.execute(
                organizations.insert().values(
                    organization_id=self.organization_id,
                    name="Attachment Job Test",
                )
            )
            connection.execute(
                workspace_users.insert(),
                (
                    {
                        "organization_id": self.organization_id,
                        "user_id": self.user_id,
                        "display_name": "Owner",
                    },
                    {
                        "organization_id": self.organization_id,
                        "user_id": self.other_user_id,
                        "display_name": "Other",
                    },
                ),
            )
            connection.execute(
                projects.insert().values(
                    project_id=self.project_id,
                    customer_name="Attachment Job Project",
                    official_domain="attachment-job.example.test",
                )
            )
            connection.execute(
                project_ownership.insert().values(
                    project_id=self.project_id,
                    organization_id=self.organization_id,
                    owner_user_id=self.user_id,
                )
            )
            connection.execute(
                assistant_conversations.insert().values(
                    organization_id=self.organization_id,
                    conversation_id=self.conversation_id,
                    creator_user_id=self.user_id,
                    title="Attachment jobs",
                    expires_at=self.now + timedelta(days=7),
                )
            )
            connection.execute(
                assistant_attachments.insert().values(
                    organization_id=self.organization_id,
                    attachment_id=self.attachment_id,
                    creator_user_id=self.user_id,
                    conversation_id=self.conversation_id,
                    proposed_project_id=self.project_id,
                    idempotency_key="upload-1",
                    object_key=(
                        f"organizations/{self.organization_id}/workflow-assistant/"
                        f"temporary/{self.attachment_id}"
                    ),
                    original_filename="notes.md",
                    mime_type="text/markdown",
                    byte_size=5,
                    sha256="a" * 64,
                    revision=0,
                    status="uploaded",
                    expires_at=self.now + timedelta(days=7),
                )
            )
            connection.execute(
                assistant_import_proposals.insert().values(
                    organization_id=self.organization_id,
                    proposal_id=self.proposal_id,
                    attachment_id=self.attachment_id,
                    creator_user_id=self.user_id,
                    target_project_id=self.project_id,
                    target_kind="project_notes",
                    idempotency_key="proposal-1",
                    revision=0,
                    status="awaiting_confirmation",
                )
            )

    def tearDown(self) -> None:
        """Remove durable job fixtures while retaining append-only audit parents."""

        with self.engine.begin() as connection:
            connection.execute(
                assistant_attachment_jobs.delete().where(
                    assistant_attachment_jobs.c.organization_id
                    == self.organization_id
                )
            )
            connection.execute(
                assistant_import_proposals.delete().where(
                    assistant_import_proposals.c.organization_id
                    == self.organization_id
                )
            )
            connection.execute(
                assistant_attachments.delete().where(
                    assistant_attachments.c.organization_id == self.organization_id
                )
            )
            connection.execute(
                assistant_conversations.delete().where(
                    assistant_conversations.c.organization_id
                    == self.organization_id
                )
            )
            has_audit = (
                connection.execute(
                    sa.select(sa.func.count())
                    .select_from(audit_events)
                    .where(
                        audit_events.c.organization_id == self.organization_id
                    )
                ).scalar_one()
                > 0
            )
            if has_audit:
                # Audit is immutable. Keep the parent identities required by
                # those rows, while allowing the next test to start clean.
                return
            connection.execute(
                project_ownership.delete().where(
                    project_ownership.c.organization_id == self.organization_id
                )
            )
            connection.execute(
                projects.delete().where(projects.c.project_id == self.project_id)
            )
            connection.execute(
                workspace_users.delete().where(
                    workspace_users.c.organization_id == self.organization_id
                )
            )
            connection.execute(
                organizations.delete().where(
                    organizations.c.organization_id == self.organization_id
                )
            )

    def test_idempotency_active_exclusion_claim_retry_cancel_and_cas(self) -> None:
        queued = self.repository.enqueue(
            requested_by_user_id=self.user_id,
            attachment_id=self.attachment_id,
            operation="classify_attachment",
            idempotency_key="classify-1",
            expected_attachment_revision=0,
            request_payload={"language": "en"},
        )
        replay = self.repository.enqueue(
            requested_by_user_id=self.user_id,
            attachment_id=self.attachment_id,
            operation="classify_attachment",
            idempotency_key="classify-1",
            expected_attachment_revision=0,
            request_payload={"language": "en"},
        )
        self.assertEqual(replay.job_id, queued.job_id)
        with self.assertRaises(AttachmentJobConflict) as idempotency_conflict:
            self.repository.enqueue(
                requested_by_user_id=self.user_id,
                attachment_id=self.attachment_id,
                operation="classify_attachment",
                idempotency_key="classify-1",
                expected_attachment_revision=0,
                request_payload={"language": "zh"},
            )
        self.assertEqual(idempotency_conflict.exception.code, "idempotency_conflict")
        self.assertIn(
            self.organization_id,
            PostgresAttachmentJobOrganizationDiscovery(
                self.engine
            ).list_pending_organization_ids(limit=10),
        )
        with self.assertRaises(AttachmentJobConflict):
            self.repository.enqueue(
                requested_by_user_id=self.user_id,
                attachment_id=self.attachment_id,
                operation="preview_import_proposal",
                idempotency_key="preview-active",
                expected_attachment_revision=0,
                project_id=self.project_id,
            )
        with self.assertRaises(AttachmentJobConflict):
            self.repository.enqueue(
                requested_by_user_id=self.other_user_id,
                attachment_id=self.attachment_id,
                operation="classify_attachment",
                idempotency_key="cross-user",
                expected_attachment_revision=0,
            )

        candidates = self.repository.list_claim_candidates(limit=1)
        self.assertEqual(candidates[0].job_id, queued.job_id)
        claimed = self.repository.claim_authorized((queued.job_id,), limit=1)[0]
        self.assertEqual(claimed.attempts, 1)
        self.assertTrue(self.repository.renew_lease(claimed.job_id))
        self.assertEqual(
            self.repository.mark_failed(
                claimed,
                error_code="transient_failure",
                retryable=True,
            ),
            "retry_wait",
        )
        with self.engine.begin() as connection:
            connection.execute(
                assistant_attachment_jobs.update()
                .where(
                    assistant_attachment_jobs.c.organization_id == self.organization_id,
                    assistant_attachment_jobs.c.job_id == queued.job_id,
                )
                .values(available_at=datetime.now(timezone.utc))
            )
        claimed = self.repository.claim_authorized((queued.job_id,), limit=1)[0]
        self.assertEqual(claimed.attempts, 2)
        cancelling = self.repository.request_cancel(
            user_id=self.user_id, job_id=queued.job_id
        )
        self.assertEqual(cancelling.status, "running")
        self.assertTrue(cancelling.cancel_requested)
        self.repository.mark_cancelled(claimed)
        cancelled = self.repository.get_for_actor(
            user_id=self.user_id, job_id=queued.job_id
        )
        self.assertEqual(cancelled.status, "cancelled")

        retried = self.repository.retry(
            user_id=self.user_id,
            job_id=queued.job_id,
            expected_attachment_revision=0,
            expected_proposal_revision=None,
        )
        self.assertEqual(retried.status, "queued")
        self.assertEqual(retried.attempts, 0)

    def test_retryable_failure_stops_when_handler_advanced_revision(self) -> None:
        queued = self.repository.enqueue(
            requested_by_user_id=self.user_id,
            attachment_id=self.attachment_id,
            operation="classify_attachment",
            idempotency_key="handler-failed-revision",
            expected_attachment_revision=0,
        )
        claimed = self.repository.claim_authorized((queued.job_id,), limit=1)[0]
        with self.engine.begin() as connection:
            connection.execute(
                assistant_attachments.update()
                .where(
                    assistant_attachments.c.organization_id == self.organization_id,
                    assistant_attachments.c.attachment_id == self.attachment_id,
                )
                .values(revision=1, status="failed")
            )

        status = self.repository.mark_failed(
            claimed,
            error_code="transient_failure",
            retryable=True,
        )

        self.assertEqual(status, "conflict")
        failed = self.repository.get_for_actor(
            user_id=self.user_id, job_id=queued.job_id
        )
        self.assertEqual(failed.standardized_error_code, "attachment_revision_conflict")

    def test_classification_job_replays_after_result_commit(self) -> None:
        queued = self.repository.enqueue(
            requested_by_user_id=self.user_id,
            attachment_id=self.attachment_id,
            operation="classify_attachment",
            idempotency_key="classification-result-committed",
            expected_attachment_revision=0,
        )
        with self.engine.begin() as connection:
            connection.execute(
                assistant_attachments.update()
                .where(
                    assistant_attachments.c.organization_id == self.organization_id,
                    assistant_attachments.c.attachment_id == self.attachment_id,
                )
                .values(
                    revision=1,
                    status="proposal_ready",
                    classification_payload={
                        "classification_job_idempotency_key": (
                            "classification-result-committed"
                        ),
                        "classification": {
                            "document_kind": "project_notes",
                            "confidence": 0.98,
                            "needs_user_choice": False,
                        },
                    },
                )
            )

        claimed = self.repository.claim_authorized((queued.job_id,), limit=1)

        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0].job_id, queued.job_id)
        self.assertEqual(claimed[0].attempts, 1)

    def test_preview_creates_and_returns_proposal_only_after_build(self) -> None:
        built_proposal_id = f"{self.proposal_id}-built"
        queued = self.repository.enqueue(
            requested_by_user_id=self.user_id,
            attachment_id=self.attachment_id,
            operation="preview_import_proposal",
            idempotency_key="preview-build-first",
            expected_attachment_revision=0,
            project_id=self.project_id,
        )
        self.assertIsNone(queued.proposal_id)
        self.assertIsNone(queued.expected_proposal_revision)
        claimed = self.repository.claim_authorized((queued.job_id,), limit=1)[0]

        # This insert represents the handler's final durable step after the
        # normalized diff has already been built successfully.
        with self.engine.begin() as connection:
            connection.execute(
                assistant_import_proposals.insert().values(
                    organization_id=self.organization_id,
                    proposal_id=built_proposal_id,
                    attachment_id=self.attachment_id,
                    creator_user_id=self.user_id,
                    target_project_id=self.project_id,
                    target_kind="project_notes",
                    idempotency_key="proposal-built-after-preview",
                    normalized_diff={"create": [{"note": "safe"}]},
                    revision=0,
                    status="awaiting_confirmation",
                )
            )
        self.repository.mark_succeeded(
            claimed,
            AttachmentJobResult(
                result_payload={
                    "proposal_id": built_proposal_id,
                    "proposal_revision": 0,
                },
                attachment_revision=0,
                proposal_revision=0,
            ),
        )

        completed = self.repository.get_for_actor(
            user_id=self.user_id, job_id=queued.job_id
        )
        self.assertEqual(completed.status, "succeeded")
        self.assertEqual(completed.result_payload["proposal_id"], built_proposal_id)
        self.assertEqual(completed.result_proposal_revision, 0)

    def test_execute_job_replays_after_import_claim_advanced_revisions(self) -> None:
        with self.engine.begin() as connection:
            now = datetime.now(timezone.utc)
            connection.execute(
                assistant_attachments.update()
                .where(
                    assistant_attachments.c.organization_id == self.organization_id,
                    assistant_attachments.c.attachment_id == self.attachment_id,
                )
                .values(status="proposal_ready")
            )
            connection.execute(
                assistant_import_proposals.update()
                .where(
                    assistant_import_proposals.c.organization_id == self.organization_id,
                    assistant_import_proposals.c.proposal_id == self.proposal_id,
                )
                .values(
                    revision=1,
                    status="confirmed",
                    confirmed_by=self.user_id,
                    confirmed_at=now,
                )
            )
        queued = self.repository.enqueue(
            requested_by_user_id=self.user_id,
            attachment_id=self.attachment_id,
            operation="execute_import_proposal",
            idempotency_key="execute-replay-after-claim",
            expected_attachment_revision=0,
            project_id=self.project_id,
            proposal_id=self.proposal_id,
            expected_proposal_revision=1,
        )
        claimed = self.repository.claim_authorized((queued.job_id,), limit=1)[0]
        self.assertEqual(claimed.attempts, 1)

        with self.engine.begin() as connection:
            connection.execute(
                assistant_attachments.update()
                .where(
                    assistant_attachments.c.organization_id == self.organization_id,
                    assistant_attachments.c.attachment_id == self.attachment_id,
                )
                .values(status="importing", revision=1)
            )
            connection.execute(
                assistant_import_proposals.update()
                .where(
                    assistant_import_proposals.c.organization_id == self.organization_id,
                    assistant_import_proposals.c.proposal_id == self.proposal_id,
                )
                .values(
                    status="running",
                    revision=2,
                    execution_idempotency_key=queued.idempotency_key,
                )
            )
            connection.execute(
                assistant_attachment_jobs.update()
                .where(
                    assistant_attachment_jobs.c.organization_id == self.organization_id,
                    assistant_attachment_jobs.c.job_id == queued.job_id,
                )
                .values(lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
            )
        self.assertEqual(self.repository.recover_interrupted(), 1)
        replayed = self.repository.claim_authorized((queued.job_id,), limit=1)[0]
        self.assertEqual(replayed.job_id, queued.job_id)
        self.assertEqual(replayed.attempts, 2)

    def test_stale_revision_conflicts_and_expired_lease_recovers(self) -> None:
        first = self.repository.enqueue(
            requested_by_user_id=self.user_id,
            attachment_id=self.attachment_id,
            operation="classify_attachment",
            idempotency_key="stale-1",
            expected_attachment_revision=0,
        )
        with self.engine.begin() as connection:
            connection.execute(
                assistant_attachments.update()
                .where(
                    assistant_attachments.c.organization_id == self.organization_id,
                    assistant_attachments.c.attachment_id == self.attachment_id,
                )
                .values(revision=1)
            )
        self.assertEqual(
            self.repository.claim_authorized((first.job_id,), limit=1), ()
        )
        stale = self.repository.get_for_actor(user_id=self.user_id, job_id=first.job_id)
        self.assertEqual(stale.status, "conflict")
        self.assertEqual(stale.standardized_error_code, "attachment_revision_conflict")

        second = self.repository.enqueue(
            requested_by_user_id=self.user_id,
            attachment_id=self.attachment_id,
            operation="preview_import_proposal",
            idempotency_key="recover-1",
            expected_attachment_revision=1,
            project_id=self.project_id,
        )
        claimed = self.repository.claim_authorized((second.job_id,), limit=1)[0]
        with self.engine.begin() as connection:
            connection.execute(
                assistant_attachment_jobs.update()
                .where(
                    assistant_attachment_jobs.c.organization_id == self.organization_id,
                    assistant_attachment_jobs.c.job_id == claimed.job_id,
                )
                .values(lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
            )
        recovery_worker = PostgresAttachmentJobRepository(
            self.engine,
            organization_id=self.organization_id,
            worker_id="recovery-worker",
        )
        self.assertEqual(recovery_worker.recover_interrupted(), 1)
        reclaimed = recovery_worker.claim_authorized((second.job_id,), limit=1)[0]
        self.assertEqual(reclaimed.attempts, 2)


if __name__ == "__main__":
    unittest.main()
