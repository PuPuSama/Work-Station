from __future__ import annotations

import os
import sys
import unittest
import uuid
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from knowledge_agent.database import create_knowledge_engine  # noqa: E402
from server_schema import (  # noqa: E402
    audit_events,
    external_identities,
    organizations,
    workspace_users,
)
from services.access_control import ActorIdentity  # noqa: E402
from services.external_identity import (  # noqa: E402
    ExternalActorSessionService,
    ExternalIdentityNotAuthorized,
    PostgresExternalIdentityRepository,
    VerifiedExternalIdentity,
)
from services.external_identity_provisioning import (  # noqa: E402
    ExternalIdentityProvisioningDenied,
    PostgresExternalIdentityProvisioningService,
)
from services.server_auth import ServerActorSessionCodec  # noqa: E402


class FakeIdentityRepository:
    def __init__(self, actor):
        self.actor = actor

    def resolve(self, identity):
        return self.actor


class ExternalIdentityUnitTests(unittest.TestCase):
    def test_issuer_validation_allows_https_and_loopback_only(self) -> None:
        identity = VerifiedExternalIdentity(
            "https://id.example.test/tenant/",
            "subject-a",
        )
        self.assertEqual(
            identity.issuer,
            "https://id.example.test/tenant",
        )
        self.assertEqual(
            VerifiedExternalIdentity(
                "http://127.0.0.1:8080",
                "local",
            ).issuer,
            "http://127.0.0.1:8080",
        )
        for issuer in (
            "http://id.example.test",
            "https://user:secret@id.example.test",
            "https://id.example.test?tenant=a",
        ):
            with self.subTest(issuer=issuer):
                with self.assertRaisesRegex(ValueError, "absolute HTTPS"):
                    VerifiedExternalIdentity(issuer, "subject")

    def test_session_exchange_contains_actor_but_no_external_claims(self) -> None:
        codec = ServerActorSessionCodec(b"s" * 32)
        identity = VerifiedExternalIdentity(
            "https://id.example.test",
            "external-subject",
        )
        service = ExternalActorSessionService(
            identities=FakeIdentityRepository(
                ActorIdentity("org-a", "user-a")
            ),
            codec=codec,
        )
        token = service.create_session(identity, max_age=300)

        self.assertEqual(
            codec.parse(token),
            ActorIdentity("org-a", "user-a"),
        )
        self.assertNotIn("external-subject", token)
        self.assertNotIn("role", token)

        denied = ExternalActorSessionService(
            identities=FakeIdentityRepository(None),
            codec=codec,
        )
        with self.assertRaisesRegex(
            ExternalIdentityNotAuthorized,
            "^external identity is not authorized$",
        ):
            denied.create_session(identity, max_age=300)


@unittest.skipUnless(
    os.environ.get("ARTICLE_AGENT_DATABASE_URL"),
    "ARTICLE_AGENT_DATABASE_URL is required for PostgreSQL integration tests",
)
class ExternalIdentityPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = create_knowledge_engine(
            os.environ["ARTICLE_AGENT_DATABASE_URL"]
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    def setUp(self) -> None:
        prefix = f"m7-identity-{uuid.uuid4().hex}"
        self.org_a = f"{prefix}-org-a"
        self.org_b = f"{prefix}-org-b"
        self.user_a = f"{prefix}-user-a"
        self.user_b = f"{prefix}-user-b"
        self.admin_a = f"{prefix}-admin-a"
        self.issuer = f"https://identity.example.test/{prefix}"
        self.subject = f"{prefix}-subject"
        with self.engine.begin() as connection:
            connection.execute(
                organizations.insert(),
                (
                    {"organization_id": self.org_a, "name": "Org A"},
                    {"organization_id": self.org_b, "name": "Org B"},
                ),
            )
            connection.execute(
                workspace_users.insert(),
                (
                    {
                        "organization_id": self.org_a,
                        "user_id": self.user_a,
                        "display_name": "User A",
                        "organization_role": "member",
                    },
                    {
                        "organization_id": self.org_b,
                        "user_id": self.user_b,
                        "display_name": "User B",
                        "organization_role": "member",
                    },
                    {
                        "organization_id": self.org_a,
                        "user_id": self.admin_a,
                        "display_name": "Admin A",
                        "organization_role": "org_admin",
                    },
                ),
            )
            connection.execute(
                external_identities.insert().values(
                    issuer=self.issuer,
                    subject=self.subject,
                    organization_id=self.org_a,
                    user_id=self.user_a,
                )
            )

    def tearDown(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                external_identities.delete().where(
                    external_identities.c.issuer.like(f"{self.issuer}%")
                )
            )
            connection.execute(
                workspace_users.delete().where(
                    workspace_users.c.organization_id.in_(
                        (self.org_a, self.org_b)
                    )
                )
            )
            connection.execute(
                organizations.delete().where(
                    organizations.c.organization_id.in_(
                        (self.org_a, self.org_b)
                    )
                )
            )

    def test_resolve_requires_active_mapping_user_and_organization(self) -> None:
        repository = PostgresExternalIdentityRepository(self.engine)
        identity = VerifiedExternalIdentity(self.issuer, self.subject)

        self.assertEqual(
            repository.resolve(identity),
            ActorIdentity(self.org_a, self.user_a),
        )
        self.assertIsNone(
            repository.resolve(
                VerifiedExternalIdentity(self.issuer, "unknown")
            )
        )
        with self.engine.begin() as connection:
            connection.execute(
                external_identities.update()
                .where(
                    external_identities.c.issuer == self.issuer,
                    external_identities.c.subject == self.subject,
                )
                .values(status="revoked")
            )
        self.assertIsNone(repository.resolve(identity))

    def test_disabled_user_or_suspended_org_cannot_resolve(self) -> None:
        repository = PostgresExternalIdentityRepository(self.engine)
        identity = VerifiedExternalIdentity(self.issuer, self.subject)
        with self.engine.begin() as connection:
            connection.execute(
                workspace_users.update()
                .where(
                    workspace_users.c.organization_id == self.org_a,
                    workspace_users.c.user_id == self.user_a,
                )
                .values(status="disabled")
            )
        self.assertIsNone(repository.resolve(identity))

        with self.engine.begin() as connection:
            connection.execute(
                workspace_users.update()
                .where(
                    workspace_users.c.organization_id == self.org_a,
                    workspace_users.c.user_id == self.user_a,
                )
                .values(status="active")
            )
            connection.execute(
                organizations.update()
                .where(organizations.c.organization_id == self.org_a)
                .values(status="suspended")
            )
        self.assertIsNone(repository.resolve(identity))

    def test_database_prevents_cross_org_or_duplicate_identity_mapping(
        self,
    ) -> None:
        with self.assertRaises(IntegrityError):
            with self.engine.begin() as connection:
                connection.execute(
                    external_identities.insert().values(
                        issuer=f"{self.issuer}/cross",
                        subject="cross",
                        organization_id=self.org_a,
                        user_id=self.user_b,
                    )
                )
        with self.assertRaises(IntegrityError):
            with self.engine.begin() as connection:
                connection.execute(
                    external_identities.insert().values(
                        issuer=self.issuer,
                        subject=self.subject,
                        organization_id=self.org_b,
                        user_id=self.user_b,
                    )
                )

    def test_schema_constraints_and_index_exist(self) -> None:
        inspector = sa.inspect(self.engine)
        self.assertIn("external_identities", inspector.get_table_names())
        constraints = {
            item["name"]
            for item in inspector.get_unique_constraints(
                "external_identities"
            )
        }
        self.assertIn(
            "uq_external_identities_user_issuer",
            constraints,
        )
        indexes = {
            item["name"]
            for item in inspector.get_indexes("external_identities")
        }
        self.assertIn(
            "ix_external_identities_workspace_user",
            indexes,
        )

    def test_org_admin_link_and_revoke_are_audited_atomically(self) -> None:
        service = PostgresExternalIdentityProvisioningService(self.engine)
        actor = ActorIdentity(self.org_a, self.admin_a)
        identity = VerifiedExternalIdentity(
            f"{self.issuer}/managed",
            f"{self.subject}-managed",
        )
        link_event = f"{self.subject}-link"
        revoke_event = f"{self.subject}-revoke"

        connection = self.engine.connect()
        transaction = connection.begin()
        try:
            service.link_in_transaction(
                connection,
                actor=actor,
                identity=identity,
                user_id=self.user_a,
                event_id=link_event,
            )
            linked = connection.execute(
                sa.select(
                    external_identities.c.organization_id,
                    external_identities.c.user_id,
                    external_identities.c.status,
                ).where(
                    external_identities.c.issuer == identity.issuer,
                    external_identities.c.subject == identity.subject,
                )
            ).mappings().one()
            self.assertEqual(linked["organization_id"], self.org_a)
            self.assertEqual(linked["user_id"], self.user_a)
            self.assertEqual(linked["status"], "active")

            service.revoke_in_transaction(
                connection,
                actor=actor,
                identity=identity,
                event_id=revoke_event,
            )
            self.assertEqual(
                connection.execute(
                    sa.select(external_identities.c.status).where(
                        external_identities.c.issuer == identity.issuer,
                        external_identities.c.subject == identity.subject,
                    )
                ).scalar_one(),
                "revoked",
            )
            rows = connection.execute(
                sa.select(audit_events).where(
                    audit_events.c.organization_id == self.org_a,
                    audit_events.c.event_id.in_(
                        (link_event, revoke_event)
                    ),
                )
            ).mappings().all()
            self.assertEqual(len(rows), 2)
            self.assertEqual(
                {str(row["action"]) for row in rows},
                {
                    "external_identity.link",
                    "external_identity.revoke",
                },
            )
            self.assertNotIn(
                identity.subject,
                str([dict(row) for row in rows]),
            )
        finally:
            transaction.rollback()
            connection.close()

    def test_non_admin_and_cross_org_mapping_are_generically_denied(
        self,
    ) -> None:
        service = PostgresExternalIdentityProvisioningService(self.engine)
        identity = VerifiedExternalIdentity(
            self.issuer,
            f"{self.subject}-denied",
        )
        for actor, target in (
            (ActorIdentity(self.org_a, self.user_a), self.user_a),
            (ActorIdentity(self.org_b, self.user_b), self.user_a),
        ):
            with self.subTest(actor=actor):
                with self.assertRaisesRegex(
                    ExternalIdentityProvisioningDenied,
                    "^external identity provisioning denied$",
                ):
                    service.link(
                        actor=actor,
                        identity=identity,
                        user_id=target,
                        event_id=f"{self.subject}-{actor.user_id}",
                    )
        with self.engine.connect() as connection:
            count = connection.execute(
                sa.select(sa.func.count())
                .select_from(external_identities)
                .where(
                    external_identities.c.issuer == identity.issuer,
                    external_identities.c.subject == identity.subject,
                )
            ).scalar_one()
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
