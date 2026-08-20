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
from server_schema import (  # noqa: E402
    assistant_attachments,
    assistant_conversations,
    audit_events,
    organizations,
    workspace_users,
)
from workflow_assistant.attachment_repository import (  # noqa: E402
    PostgresAttachmentRepository,
)
from workflow_assistant.repository import (  # noqa: E402
    PostgresWorkflowAssistantRepository,
)
from workflow_assistant.attachments import (  # noqa: E402
    AssistantAttachment,
    AttachmentConflict,
    AttachmentNotFound,
)


@unittest.skipUnless(
    os.environ.get("ARTICLE_AGENT_DATABASE_URL"),
    "ARTICLE_AGENT_DATABASE_URL is required for PostgreSQL integration tests",
)
class AttachmentRepositoryPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = create_knowledge_engine(os.environ["ARTICLE_AGENT_DATABASE_URL"])

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    def setUp(self) -> None:
        prefix = f"attachment-repository-{uuid.uuid4().hex}"
        self.organization_id = f"{prefix}-org"
        self.owner_user_id = f"{prefix}-owner"
        self.other_user_id = f"{prefix}-other"
        self.conversation_id = f"{prefix}-conversation"
        self.other_conversation_id = f"{prefix}-other-conversation"
        self.repository = PostgresAttachmentRepository(self.engine)
        self.now = datetime.now(timezone.utc).replace(microsecond=123456)
        with self.engine.begin() as connection:
            connection.execute(
                organizations.insert().values(
                    organization_id=self.organization_id,
                    name="Attachment Repository Test",
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
                assistant_conversations.insert(),
                (
                    {
                        "organization_id": self.organization_id,
                        "conversation_id": self.conversation_id,
                        "creator_user_id": self.owner_user_id,
                        "title": "Owner attachments",
                        "expires_at": self.now + timedelta(days=30),
                    },
                    {
                        "organization_id": self.organization_id,
                        "conversation_id": self.other_conversation_id,
                        "creator_user_id": self.other_user_id,
                        "title": "Other attachments",
                        "expires_at": self.now + timedelta(days=30),
                    },
                ),
            )

    def tearDown(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                assistant_attachments.delete().where(
                    assistant_attachments.c.organization_id == self.organization_id
                )
            )
            connection.execute(
                assistant_conversations.delete().where(
                    assistant_conversations.c.organization_id == self.organization_id
                )
            )
            has_audit = connection.execute(
                sa.select(sa.func.count()).select_from(audit_events).where(
                    audit_events.c.organization_id == self.organization_id
                )
            ).scalar_one() > 0
            if has_audit:
                # Audit rows are intentionally append-only. Dedicated integration
                # databases are disposable; retain their required actor parents.
                return
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

    def attachment(
        self,
        identity: str,
        *,
        creator_user_id: str | None = None,
        conversation_id: str | None = None,
        idempotency_key: str | None = None,
        status: str = "uploaded",
        created_at: datetime | None = None,
        expires_at: datetime | None = None,
    ) -> AssistantAttachment:
        creator = creator_user_id or self.owner_user_id
        conversation = conversation_id or self.conversation_id
        created = created_at or self.now
        expiry = expires_at or created + timedelta(days=7)
        return AssistantAttachment(
            attachment_id=identity,
            organization_id=self.organization_id,
            creator_user_id=creator,
            conversation_id=conversation,
            proposed_project_id=None,
            plan_id=None,
            idempotency_key=idempotency_key or f"idem-{identity}",
            object_key=(
                f"organizations/{self.organization_id}/workflow-assistant/"
                f"users/{creator}/conversations/{conversation}/attachments/"
                f"{identity}/{'a' * 64}"
            ),
            original_filename="资料.md",
            mime_type="text/markdown",
            byte_size=17,
            sha256="a" * 64,
            classification="knowledge_source",
            classification_payload={
                "reason": "产品参数",
                "scores": [0.9, 0.1],
                "nested": {"safe": True},
            },
            revision=0,
            status=status,  # type: ignore[arg-type]
            expires_at=expiry,
            created_at=created,
            updated_at=created,
        )

    def persist(self, attachment: AssistantAttachment) -> AssistantAttachment:
        desired_status = attachment.status
        uploading = replace(attachment, status="uploading")
        reservation = self.repository.reserve_upload(uploading)
        if reservation.attachment.status == "uploaded":
            return reservation.attachment
        uploaded = self.repository.finalize_upload(
            attachment=reservation.attachment,
            now=attachment.updated_at,
        )
        if desired_status == "uploaded":
            return uploaded
        with self.engine.begin() as connection:
            row = connection.execute(
                assistant_attachments.update()
                .where(
                    assistant_attachments.c.organization_id
                    == uploaded.organization_id,
                    assistant_attachments.c.attachment_id
                    == uploaded.attachment_id,
                )
                .values(status=desired_status)
                .returning(*assistant_attachments.c)
            ).mappings().one()
        return replace(uploaded, status=row["status"])

    def test_create_replays_exact_idempotency_and_rejects_conflict(self) -> None:
        candidate = self.attachment("attachment-create", idempotency_key="upload-1")

        reservation = self.repository.reserve_upload(replace(candidate, status="uploading"))
        self.assertTrue(reservation.should_write_object)
        created = self.repository.finalize_upload(
            attachment=reservation.attachment,
            now=candidate.updated_at,
        )
        replay = self.repository.reserve_upload(
            replace(candidate, attachment_id="loser-id", status="uploading")
        ).attachment

        self.assertEqual(replay, created)
        self.assertEqual(created.classification_payload, candidate.classification_payload)
        self.assertEqual(created.created_at.tzinfo, timezone.utc)
        with self.assertRaises(AttachmentConflict):
            self.repository.reserve_upload(
                replace(candidate, original_filename="other.md", status="uploading")
            )
        with self.engine.connect() as connection:
            count = connection.execute(
                sa.select(sa.func.count()).select_from(assistant_attachments).where(
                    assistant_attachments.c.organization_id == self.organization_id
                )
            ).scalar_one()
            audit_rows = connection.execute(
                sa.select(audit_events).where(
                    audit_events.c.organization_id == self.organization_id
                )
            ).mappings().all()
        self.assertEqual(count, 1)
        self.assertEqual(len(audit_rows), 1)
        self.assertEqual(audit_rows[0]["action"], "assistant.attachment.uploaded")
        self.assertEqual(audit_rows[0]["actor_user_id"], self.owner_user_id)
        self.assertEqual(
            dict(audit_rows[0]["details"]),
            {
                "conversation_id": self.conversation_id,
                "sha256": "a" * 64,
                "status": "uploaded",
                "byte_size": 17,
                "mime_type": "text/markdown",
            },
        )
        self.assertNotIn("filename", str(audit_rows[0]["details"]).casefold())

    def test_actor_scoped_get_idempotency_and_list(self) -> None:
        owner = self.persist(self.attachment("attachment-owner"))
        other = self.persist(
            self.attachment(
                "attachment-other",
                creator_user_id=self.other_user_id,
                conversation_id=self.other_conversation_id,
            )
        )

        self.assertEqual(
            self.repository.get_for_actor(
                organization_id=self.organization_id,
                creator_user_id=self.owner_user_id,
                conversation_id=self.conversation_id,
                attachment_id=owner.attachment_id,
            ),
            owner,
        )
        self.assertIsNone(
            self.repository.get_for_actor(
                organization_id=self.organization_id,
                creator_user_id=self.other_user_id,
                conversation_id=self.other_conversation_id,
                attachment_id=owner.attachment_id,
            )
        )
        self.assertEqual(
            self.repository.get_by_idempotency_for_actor(
                organization_id=self.organization_id,
                creator_user_id=self.other_user_id,
                conversation_id=self.other_conversation_id,
                idempotency_key=other.idempotency_key,
            ),
            other,
        )
        self.assertEqual(
            self.repository.list_for_actor(
                organization_id=self.organization_id,
                creator_user_id=self.owner_user_id,
                conversation_id=self.conversation_id,
                limit=10,
            ),
            (owner,),
        )

    def test_mark_rejected_is_actor_scoped_and_revisioned(self) -> None:
        attachment = self.persist(self.attachment("attachment-reject"))

        with self.assertRaises(AttachmentNotFound):
            self.repository.claim_rejection(
                organization_id=self.organization_id,
                creator_user_id=self.other_user_id,
                conversation_id=self.other_conversation_id,
                attachment_id=attachment.attachment_id,
                now=self.now + timedelta(minutes=1),
            )
        claimed = self.repository.claim_rejection(
            organization_id=self.organization_id,
            creator_user_id=self.owner_user_id,
            conversation_id=self.conversation_id,
            attachment_id=attachment.attachment_id,
            now=self.now + timedelta(minutes=1),
        )

        rejected = self.repository.finalize_rejection(
            attachment=claimed,
            now=self.now + timedelta(minutes=1),
        )
        self.assertEqual(rejected.status, "rejected")
        self.assertEqual(rejected.revision, 3)
        self.assertIsNone(self.repository.get_for_actor(
            organization_id=self.organization_id,
            creator_user_id=self.owner_user_id,
            conversation_id=self.conversation_id,
            attachment_id=attachment.attachment_id,
        ))
        with self.engine.connect() as connection:
            actions = tuple(
                connection.execute(
                    sa.select(audit_events.c.action)
                    .where(audit_events.c.organization_id == self.organization_id)
                    .order_by(audit_events.c.created_at, audit_events.c.event_id)
                ).scalars()
            )
        self.assertEqual(
            actions,
            (
                "assistant.attachment.uploaded",
                "assistant.attachment.rejected",
            ),
        )

    def test_expiry_selection_and_object_key_cas(self) -> None:
        created = self.now - timedelta(days=8)
        expired = self.persist(
            self.attachment(
                "attachment-expired",
                created_at=created,
                expires_at=self.now - timedelta(seconds=1),
            )
        )
        self.persist(
            self.attachment(
                "attachment-importing",
                status="importing",
                created_at=created,
                expires_at=self.now - timedelta(seconds=1),
            )
        )
        self.persist(self.attachment("attachment-live"))

        self.assertEqual(
            tuple(item.attachment_id for item in self.repository.claim_expired(
                before=self.now,
                limit=10,
                exclude_attachment_ids=(),
            )),
            (expired.attachment_id,),
        )
        claimed = self.repository.claim_expired(
            before=self.now, limit=10, exclude_attachment_ids=()
        )[0]
        unchanged = self.repository.get_for_actor(
            organization_id=self.organization_id,
            creator_user_id=self.owner_user_id,
            conversation_id=self.conversation_id,
            attachment_id=expired.attachment_id,
        )
        self.assertIsNotNone(unchanged)
        self.assertEqual(unchanged.status, "expiring")  # type: ignore[union-attr]

        terminal = self.repository.finalize_expiry(attachment=claimed, now=self.now)
        self.assertIsNotNone(terminal)
        changed = self.repository.get_for_actor(
            organization_id=self.organization_id,
            creator_user_id=self.owner_user_id,
            conversation_id=self.conversation_id,
            attachment_id=expired.attachment_id,
        )
        self.assertIsNone(changed)
        self.assertEqual(
            self.repository.claim_expired(
                before=self.now, limit=10, exclude_attachment_ids=()
            ),
            (),
        )
        with self.engine.connect() as connection:
            expiry_events = connection.execute(
                sa.select(audit_events).where(
                    audit_events.c.organization_id == self.organization_id,
                    audit_events.c.action == "assistant.attachment.expired",
                )
            ).mappings().all()
        self.assertEqual(len(expiry_events), 1)
        self.assertIsNone(expiry_events[0]["actor_user_id"])
        self.assertEqual(expiry_events[0]["target_id"], expired.attachment_id)
        self.assertEqual(expiry_events[0]["details"]["sha256"], "a" * 64)

    def test_rejects_naive_datetimes_and_non_json_payloads(self) -> None:
        naive = replace(
            self.attachment("attachment-naive"),
            created_at=datetime(2026, 8, 20),
        )
        with self.assertRaises(ValueError):
            self.repository.reserve_upload(replace(naive, status="uploading"))

        unsafe = replace(
            self.attachment("attachment-json"),
            classification_payload={"score": float("nan")},
        )
        with self.assertRaises(ValueError):
            self.repository.reserve_upload(replace(unsafe, status="uploading"))

    def test_conversation_prune_waits_for_temporary_attachment_cleanup(self) -> None:
        attachment = self.persist(self.attachment("attachment-prune-anchor"))
        with self.engine.begin() as connection:
            connection.execute(
                assistant_conversations.update()
                .where(
                    assistant_conversations.c.organization_id
                    == self.organization_id,
                    assistant_conversations.c.conversation_id
                    == self.conversation_id,
                )
                .values(expires_at=self.now - timedelta(minutes=1))
            )
        conversations = PostgresWorkflowAssistantRepository(self.engine)
        self.assertEqual(0, conversations.prune_expired(before=self.now))

        claimed = self.repository.claim_expired(
            before=attachment.expires_at + timedelta(seconds=1),
            limit=10,
            exclude_attachment_ids=(),
        )[0]
        self.assertIsNotNone(
            self.repository.finalize_expiry(
                attachment=claimed,
                now=attachment.expires_at + timedelta(seconds=1),
            )
        )
        self.assertEqual(1, conversations.prune_expired(before=self.now))


if __name__ == "__main__":
    unittest.main()
