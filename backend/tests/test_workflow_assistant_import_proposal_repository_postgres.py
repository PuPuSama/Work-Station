from __future__ import annotations

import os
import sys
import unittest
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sqlalchemy as sa


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from knowledge_agent.database import create_knowledge_engine  # noqa: E402
from knowledge_agent.schema import projects  # noqa: E402
from server_schema import (  # noqa: E402
    assistant_attachments,
    assistant_conversations,
    assistant_import_proposals,
    audit_events,
    organizations,
    project_ownership,
    workspace_users,
)
from services.access_control import ActorIdentity  # noqa: E402
from workflow_assistant.import_proposal_repository import (  # noqa: E402
    PostgresImportProposalRepository,
)
from workflow_assistant.import_proposals import (  # noqa: E402
    ImportProposal,
    ImportProposalConflict,
    ImportProposalNotFound,
)


class FailingAuditWriter:
    def append(self, connection: object, event: object) -> None:
        del connection, event
        raise RuntimeError("audit unavailable")


@unittest.skipUnless(
    os.environ.get("ARTICLE_AGENT_DATABASE_URL"),
    "ARTICLE_AGENT_DATABASE_URL is required for PostgreSQL integration tests",
)
class ImportProposalRepositoryPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = create_knowledge_engine(os.environ["ARTICLE_AGENT_DATABASE_URL"])

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    def setUp(self) -> None:
        prefix = f"import-proposal-{uuid.uuid4().hex}"
        self.organization_id = f"{prefix}-org"
        self.owner_user_id = f"{prefix}-owner"
        self.other_user_id = f"{prefix}-other"
        self.project_id = f"{prefix}.example.test"
        self.conversation_id = f"{prefix}-conversation"
        self.attachment_id = f"{prefix}-attachment"
        self.actor = ActorIdentity(self.organization_id, self.owner_user_id)
        self.other_actor = ActorIdentity(self.organization_id, self.other_user_id)
        self.repository = PostgresImportProposalRepository(self.engine)
        self.now = datetime.now(timezone.utc).replace(microsecond=123456)
        with self.engine.begin() as connection:
            connection.execute(
                organizations.insert().values(
                    organization_id=self.organization_id,
                    name="Import Proposal Test",
                )
            )
            connection.execute(
                workspace_users.insert(),
                (
                    {
                        "organization_id": self.organization_id,
                        "user_id": self.owner_user_id,
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
                    customer_name="Import Proposal Project",
                    official_domain=self.project_id,
                )
            )
            connection.execute(
                project_ownership.insert().values(
                    organization_id=self.organization_id,
                    project_id=self.project_id,
                    owner_user_id=self.owner_user_id,
                )
            )
            connection.execute(
                assistant_conversations.insert().values(
                    organization_id=self.organization_id,
                    conversation_id=self.conversation_id,
                    creator_user_id=self.owner_user_id,
                    title="Import proposal conversation",
                    expires_at=self.now + timedelta(days=30),
                )
            )
            connection.execute(
                assistant_attachments.insert().values(
                    organization_id=self.organization_id,
                    attachment_id=self.attachment_id,
                    creator_user_id=self.owner_user_id,
                    conversation_id=self.conversation_id,
                    proposed_project_id=self.project_id,
                    plan_id=None,
                    idempotency_key="upload-one",
                    object_key=f"private/{self.attachment_id}",
                    original_filename="source.md",
                    mime_type="text/markdown",
                    byte_size=10,
                    sha256="a" * 64,
                    classification="knowledge_source",
                    classification_payload={
                        "schema_version": 1,
                        "classification": {
                            "classification": "knowledge_source",
                            "target_project_id": self.project_id,
                            "reason": "Explicit knowledge source.",
                            "confidence": 0.99,
                        },
                        "source": {"text_preview": "private"},
                        "model_identity": "model-one",
                        "source_sha256": "a" * 64,
                    },
                    revision=2,
                    status="proposal_ready",
                    expires_at=self.now + timedelta(days=7),
                    created_at=self.now,
                    updated_at=self.now,
                )
            )

    def tearDown(self) -> None:
        with self.engine.begin() as connection:
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
                    .where(audit_events.c.organization_id == self.organization_id)
                ).scalar_one()
                > 0
            )
            if has_audit:
                # Audit is immutable. The integration database is disposable;
                # retain the parent identities required by those audit rows.
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

    def proposal(
        self,
        identity: str,
        *,
        idempotency_key: str = "proposal-key",
        normalized_diff: dict[str, object] | None = None,
        creator_user_id: str | None = None,
    ) -> ImportProposal:
        return ImportProposal(
            proposal_id=identity,
            organization_id=self.organization_id,
            attachment_id=self.attachment_id,
            creator_user_id=creator_user_id or self.owner_user_id,
            target_project_id=self.project_id,
            plan_id=None,
            target_kind="knowledge_source",
            idempotency_key=idempotency_key,
            normalized_diff=normalized_diff or {"create": [{"source": "source.md"}]},
            revision=0,
            status="awaiting_confirmation",
            confirmed_by=None,
            confirmed_at=None,
            resulting_entity_refs=(),
            standardized_error_code=None,
            created_at=self.now,
            updated_at=self.now,
        )

    def test_create_is_idempotent_actor_scoped_and_attachment_creator_bound(self) -> None:
        candidate = self.proposal("proposal-one")
        created = self.repository.create(candidate)
        replay = self.repository.create(replace(candidate, proposal_id="proposal-loser"))

        self.assertEqual(replay, created)
        self.assertEqual(
            self.repository.get_for_actor(
                actor=self.actor, proposal_id=created.proposal_id
            ),
            created,
        )
        self.assertIsNone(
            self.repository.get_for_actor(
                actor=self.other_actor, proposal_id=created.proposal_id
            )
        )
        with self.assertRaises(ImportProposalConflict):
            self.repository.create(
                replace(candidate, proposal_id="proposal-conflict", normalized_diff={"skip": [1]})
            )
        with self.assertRaises(ImportProposalNotFound):
            self.repository.create(
                self.proposal(
                    "proposal-cross-creator",
                    idempotency_key="cross-creator",
                    creator_user_id=self.other_user_id,
                )
            )
        with self.engine.connect() as connection:
            count = connection.execute(
                sa.select(sa.func.count())
                .select_from(assistant_import_proposals)
                .where(
                    assistant_import_proposals.c.organization_id
                    == self.organization_id
                )
            ).scalar_one()
        self.assertEqual(count, 1)

    def test_revise_uses_revision_cas_and_revalidates_classification(self) -> None:
        created = self.repository.create(self.proposal("proposal-revise"))
        revised = self.repository.revise(
            actor=self.actor,
            proposal_id=created.proposal_id,
            expected_revision=0,
            target_project_id=self.project_id,
            target_kind="knowledge_source",
            normalized_diff={"create": [], "skip": [{"source": "duplicate"}]},
            now=self.now + timedelta(minutes=1),
        )
        self.assertEqual(revised.revision, 1)
        with self.assertRaises(ImportProposalConflict) as caught:
            self.repository.revise(
                actor=self.actor,
                proposal_id=created.proposal_id,
                expected_revision=0,
                target_project_id=self.project_id,
                target_kind="knowledge_source",
                normalized_diff={},
                now=self.now + timedelta(minutes=2),
            )
        self.assertEqual(caught.exception.current_revision, 1)
        with self.engine.begin() as connection:
            connection.execute(
                assistant_attachments.update()
                .where(
                    assistant_attachments.c.organization_id == self.organization_id,
                    assistant_attachments.c.attachment_id == self.attachment_id,
                )
                .values(
                    classification="topic_library",
                    classification_payload={
                        "schema_version": 1,
                        "classification": {
                            "classification": "topic_library",
                            "target_project_id": self.project_id,
                            "reason": "Reclassified.",
                            "confidence": 1,
                        },
                        "source": {"text_preview": "private"},
                        "model_identity": "model-one",
                        "source_sha256": "a" * 64,
                    },
                )
            )
        with self.assertRaisesRegex(Exception, "does not match"):
            self.repository.revise(
                actor=self.actor,
                proposal_id=created.proposal_id,
                expected_revision=1,
                target_project_id=self.project_id,
                target_kind="knowledge_source",
                normalized_diff={},
                now=self.now + timedelta(minutes=2),
            )

    def test_confirm_reauthorizes_audits_and_does_not_import_or_publish(self) -> None:
        created = self.repository.create(self.proposal("proposal-confirm"))
        authorization_calls = 0

        def authorize() -> None:
            nonlocal authorization_calls
            authorization_calls += 1

        confirmed = self.repository.confirm(
            actor=self.actor,
            proposal_id=created.proposal_id,
            expected_revision=0,
            target_project_id=self.project_id,
            authorize_target=authorize,
            now=self.now + timedelta(minutes=1),
        )
        replay = self.repository.confirm(
            actor=self.actor,
            proposal_id=created.proposal_id,
            expected_revision=0,
            target_project_id=self.project_id,
            authorize_target=authorize,
            now=self.now + timedelta(minutes=2),
        )

        self.assertEqual(replay, confirmed)
        self.assertEqual(authorization_calls, 2)
        self.assertEqual(confirmed.status, "confirmed")
        self.assertEqual(confirmed.revision, 1)
        self.assertEqual(confirmed.resulting_entity_refs, ())
        with self.engine.connect() as connection:
            attachment_status = connection.execute(
                sa.select(assistant_attachments.c.status).where(
                    assistant_attachments.c.organization_id == self.organization_id,
                    assistant_attachments.c.attachment_id == self.attachment_id,
                )
            ).scalar_one()
            events = connection.execute(
                sa.select(audit_events).where(
                    audit_events.c.organization_id == self.organization_id,
                    audit_events.c.target_id == created.proposal_id,
                    audit_events.c.action
                    == "assistant.import_proposal.confirmed",
                )
            ).mappings().all()
        self.assertEqual(attachment_status, "proposal_ready")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["action"], "assistant.import_proposal.confirmed")
        self.assertEqual(events[0]["details"]["imports_executed"], False)
        self.assertEqual(events[0]["details"]["knowledge_published"], False)

    def test_authorization_or_audit_failure_rolls_back_confirmation(self) -> None:
        denied = self.repository.create(self.proposal("proposal-denied", idempotency_key="denied"))

        def deny() -> None:
            raise PermissionError("project access denied")

        with self.assertRaises(PermissionError):
            self.repository.confirm(
                actor=self.actor,
                proposal_id=denied.proposal_id,
                expected_revision=0,
                target_project_id=self.project_id,
                authorize_target=deny,
                now=self.now + timedelta(minutes=1),
            )
        self.assertEqual(
            self.repository.get_for_actor(actor=self.actor, proposal_id=denied.proposal_id).status,  # type: ignore[union-attr]
            "awaiting_confirmation",
        )

        failing = PostgresImportProposalRepository(
            self.engine, audit=FailingAuditWriter()
        )
        audit_failure = self.repository.create(
            self.proposal("proposal-audit-failure", idempotency_key="audit-failure")
        )
        with self.assertRaisesRegex(RuntimeError, "audit unavailable"):
            failing.confirm(
                actor=self.actor,
                proposal_id=audit_failure.proposal_id,
                expected_revision=0,
                target_project_id=self.project_id,
                authorize_target=lambda: None,
                now=self.now + timedelta(minutes=1),
            )
        self.assertEqual(
            self.repository.get_for_actor(
                actor=self.actor, proposal_id=audit_failure.proposal_id
            ).status,  # type: ignore[union-attr]
            "awaiting_confirmation",
        )

    def test_cancel_is_revisioned_and_idempotent(self) -> None:
        created = self.repository.create(self.proposal("proposal-cancel"))
        cancelled = self.repository.cancel(
            actor=self.actor,
            proposal_id=created.proposal_id,
            expected_revision=0,
            now=self.now + timedelta(minutes=1),
        )
        replay = self.repository.cancel(
            actor=self.actor,
            proposal_id=created.proposal_id,
            expected_revision=0,
            now=self.now + timedelta(minutes=2),
        )
        self.assertEqual(replay, cancelled)
        self.assertEqual(cancelled.status, "cancelled")
        self.assertEqual(cancelled.revision, 1)

    def test_execution_claim_and_completion_are_replay_safe(self) -> None:
        created = self.repository.create(self.proposal("proposal-execute"))
        confirmed = self.repository.confirm(
            actor=self.actor,
            proposal_id=created.proposal_id,
            expected_revision=0,
            target_project_id=self.project_id,
            authorize_target=lambda: None,
            now=self.now + timedelta(minutes=1),
        )

        claimed = self.repository.claim_execution(
            actor=self.actor,
            proposal_id=confirmed.proposal_id,
            expected_revision=confirmed.revision,
            execution_idempotency_key="execute-proposal",
            now=self.now + timedelta(minutes=2),
        )
        self.assertEqual(claimed.proposal.status, "running")
        self.assertEqual(claimed.proposal.revision, 2)
        self.assertEqual(claimed.attachment_revision, 3)

        replayed_claim = self.repository.claim_execution(
            actor=self.actor,
            proposal_id=confirmed.proposal_id,
            expected_revision=confirmed.revision,
            execution_idempotency_key="execute-proposal",
            now=self.now + timedelta(minutes=3),
        )
        self.assertEqual(replayed_claim, claimed)

        completed = self.repository.complete_execution(
            actor=self.actor,
            proposal_id=confirmed.proposal_id,
            expected_running_revision=claimed.proposal.revision,
            execution_idempotency_key="execute-proposal",
            resulting_entity_refs=(
                {
                    "entity_type": "knowledge_source",
                    "entity_id": "source-1",
                    "action": "create",
                },
            ),
            waiting_publication=True,
            now=self.now + timedelta(minutes=4),
        )
        self.assertEqual(completed.proposal.status, "waiting_publication")
        self.assertEqual(completed.proposal.revision, 3)
        self.assertEqual(completed.attachment_revision, 4)
        self.assertEqual(completed.proposal.resulting_entity_refs[0]["entity_id"], "source-1")

        replayed_completion = self.repository.complete_execution(
            actor=self.actor,
            proposal_id=confirmed.proposal_id,
            expected_running_revision=claimed.proposal.revision,
            execution_idempotency_key="execute-proposal",
            resulting_entity_refs=(),
            waiting_publication=False,
            now=self.now + timedelta(minutes=5),
        )
        self.assertEqual(replayed_completion, completed)

    def test_execution_failure_reverts_attachment_and_is_idempotent(self) -> None:
        created = self.repository.create(self.proposal("proposal-execute-fail"))
        confirmed = self.repository.confirm(
            actor=self.actor,
            proposal_id=created.proposal_id,
            expected_revision=0,
            target_project_id=self.project_id,
            authorize_target=lambda: None,
            now=self.now + timedelta(minutes=1),
        )
        claimed = self.repository.claim_execution(
            actor=self.actor,
            proposal_id=confirmed.proposal_id,
            expected_revision=confirmed.revision,
            execution_idempotency_key="execute-fail",
            now=self.now + timedelta(minutes=2),
        )

        failed = self.repository.fail_execution(
            actor=self.actor,
            proposal_id=confirmed.proposal_id,
            expected_running_revision=claimed.proposal.revision,
            execution_idempotency_key="execute-fail",
            error_code="target_revision_conflict",
            now=self.now + timedelta(minutes=3),
        )
        self.assertEqual(failed.proposal.status, "failed")
        self.assertEqual(failed.proposal.revision, 3)
        self.assertEqual(failed.proposal.standardized_error_code, "target_revision_conflict")
        self.assertEqual(failed.attachment_revision, 4)

        with self.engine.connect() as connection:
            attachment = connection.execute(
                sa.select(
                    assistant_attachments.c.status,
                    assistant_attachments.c.revision,
                ).where(
                    assistant_attachments.c.organization_id == self.organization_id,
                    assistant_attachments.c.attachment_id == self.attachment_id,
                )
            ).mappings().one()
        self.assertEqual(attachment["status"], "proposal_ready")
        self.assertEqual(attachment["revision"], 4)
        self.assertEqual(
            self.repository.fail_execution(
                actor=self.actor,
                proposal_id=confirmed.proposal_id,
                expected_running_revision=claimed.proposal.revision,
                execution_idempotency_key="execute-fail",
                error_code="different_error",
                now=self.now + timedelta(minutes=4),
            ),
            failed,
        )


if __name__ == "__main__":
    unittest.main()
