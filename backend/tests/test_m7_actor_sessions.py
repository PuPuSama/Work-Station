from __future__ import annotations

import os
import sys
import unittest
import uuid
from pathlib import Path

import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from knowledge_agent.database import create_knowledge_engine  # noqa: E402
from server_schema import (  # noqa: E402
    audit_events,
    organizations,
    workspace_users,
)
from services.access_control import ActorIdentity  # noqa: E402
from services.actor_sessions import (  # noqa: E402
    ActorSessionRevocationDenied,
    ActorSessionRevocationError,
    PostgresActorSessionRepository,
    PostgresActorSessionRevocationService,
)
from services.server_auth import (  # noqa: E402
    SERVER_AUTH_COOKIE_NAME,
    ServerActorSessionCodec,
)
from services.server_request_security import (  # noqa: E402
    ServerRequestSecurity,
)


SECRET_ERROR = "provider-secret-session-revocation-body"


class FailingAuditWriter:
    def append(self, connection, event) -> None:
        raise RuntimeError(SECRET_ERROR)


class RecordingAuditWriter:
    def __init__(self) -> None:
        self.events = []

    def append(self, connection, event) -> None:
        self.events.append(event)


@unittest.skipUnless(
    os.environ.get("ARTICLE_AGENT_DATABASE_URL"),
    "ARTICLE_AGENT_DATABASE_URL is required for PostgreSQL integration tests",
)
class ActorSessionPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = create_knowledge_engine(
            os.environ["ARTICLE_AGENT_DATABASE_URL"]
        )
        cls.codec = ServerActorSessionCodec(b"s" * 32)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    def setUp(self) -> None:
        prefix = f"m7-session-{uuid.uuid4().hex}"
        self.org_a = f"{prefix}-org-a"
        self.org_b = f"{prefix}-org-b"
        self.admin_a = f"{prefix}-admin-a"
        self.member_a = f"{prefix}-member-a"
        self.member_b = f"{prefix}-member-b"
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
                        "user_id": self.admin_a,
                        "display_name": "Admin A",
                        "organization_role": "org_admin",
                    },
                    {
                        "organization_id": self.org_a,
                        "user_id": self.member_a,
                        "display_name": "Member A",
                        "organization_role": "member",
                    },
                    {
                        "organization_id": self.org_b,
                        "user_id": self.member_b,
                        "display_name": "Member B",
                        "organization_role": "member",
                    },
                ),
            )

    def tearDown(self) -> None:
        with self.engine.begin() as connection:
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

    def _version(self, organization_id: str, user_id: str) -> int:
        with self.engine.connect() as connection:
            return int(
                connection.execute(
                    sa.select(workspace_users.c.session_version).where(
                        workspace_users.c.organization_id == organization_id,
                        workspace_users.c.user_id == user_id,
                    )
                ).scalar_one()
            )

    def test_revoke_all_invalidates_old_cookie_and_audits_atomically(
        self,
    ) -> None:
        actor = ActorIdentity(self.org_a, self.member_a)
        sessions = PostgresActorSessionRepository(self.engine)
        old_session = self.codec.parse_session(
            self.codec.create(actor, session_version=1)
        )
        self.assertTrue(sessions.is_current(old_session))

        audit = RecordingAuditWriter()
        next_version = PostgresActorSessionRevocationService(
            self.engine,
            audit=audit,
        ).revoke_all(
            actor=ActorIdentity(self.org_a, self.admin_a),
            user_id=self.member_a,
            event_id=f"event-{uuid.uuid4().hex}",
        )

        self.assertEqual(next_version, 2)
        self.assertFalse(sessions.is_current(old_session))
        fresh_session = self.codec.parse_session(
            self.codec.create(actor, session_version=next_version)
        )
        self.assertTrue(sessions.is_current(fresh_session))
        self.assertEqual(len(audit.events), 1)
        event = audit.events[0]
        self.assertEqual(event.actor_user_id, self.admin_a)
        self.assertEqual(event.target_id, self.member_a)
        self.assertEqual(event.action, "workspace_user.sessions.revoked")
        self.assertEqual(event.details, {"session_version": 2})

    def test_audit_failure_rolls_back_version_without_leaking_secret(
        self,
    ) -> None:
        service = PostgresActorSessionRevocationService(
            self.engine,
            audit=FailingAuditWriter(),
        )
        with self.assertRaisesRegex(
            ActorSessionRevocationError,
            "^actor sessions could not be revoked$",
        ) as captured:
            service.revoke_all(
                actor=ActorIdentity(self.org_a, self.admin_a),
                user_id=self.member_a,
                event_id=f"event-{uuid.uuid4().hex}",
            )

        self.assertNotIn(SECRET_ERROR, str(captured.exception))
        self.assertEqual(self._version(self.org_a, self.member_a), 1)

    def test_postgres_audit_and_version_are_visible_in_same_transaction(
        self,
    ) -> None:
        event_id = f"event-{uuid.uuid4().hex}"
        connection = self.engine.connect()
        transaction = connection.begin()
        try:
            next_version = PostgresActorSessionRevocationService(
                self.engine
            ).revoke_all_in_transaction(
                connection,
                actor=ActorIdentity(self.org_a, self.admin_a),
                user_id=self.member_a,
                event_id=event_id,
            )
            stored_version = connection.execute(
                sa.select(workspace_users.c.session_version).where(
                    workspace_users.c.organization_id == self.org_a,
                    workspace_users.c.user_id == self.member_a,
                )
            ).scalar_one()
            event = connection.execute(
                sa.select(audit_events).where(
                    audit_events.c.organization_id == self.org_a,
                    audit_events.c.event_id == event_id,
                )
            ).mappings().one()

            self.assertEqual(next_version, 2)
            self.assertEqual(int(stored_version), 2)
            self.assertEqual(
                event["action"],
                "workspace_user.sessions.revoked",
            )
        finally:
            transaction.rollback()
            connection.close()

        self.assertEqual(self._version(self.org_a, self.member_a), 1)

    def test_cross_org_and_non_admin_revocation_are_generic(self) -> None:
        service = PostgresActorSessionRevocationService(self.engine)
        for actor, target in (
            (ActorIdentity(self.org_a, self.admin_a), self.member_b),
            (ActorIdentity(self.org_a, self.member_a), self.member_a),
        ):
            with self.subTest(actor=actor.user_id, target=target):
                with self.assertRaisesRegex(
                    ActorSessionRevocationDenied,
                    "^actor session revocation denied$",
                ):
                    service.revoke_all(
                        actor=actor,
                        user_id=target,
                        event_id=f"event-{uuid.uuid4().hex}",
                    )

        self.assertEqual(self._version(self.org_a, self.member_a), 1)
        self.assertEqual(self._version(self.org_b, self.member_b), 1)

    def test_database_rejects_non_positive_session_version(self) -> None:
        with self.assertRaises(IntegrityError):
            with self.engine.begin() as connection:
                connection.execute(
                    workspace_users.update()
                    .where(
                        workspace_users.c.organization_id == self.org_a,
                        workspace_users.c.user_id == self.member_a,
                    )
                    .values(session_version=0)
                )

    def test_org_admin_http_revokes_without_accepting_version_input(
        self,
    ) -> None:
        import app as app_module

        old_member_token = self.codec.create(
            ActorIdentity(self.org_a, self.member_a),
            session_version=1,
        )
        audit = RecordingAuditWriter()
        previous_mode = getattr(
            app_module.app.state,
            "server_mode_enabled",
            None,
        )
        previous_security = getattr(
            app_module.app.state,
            "server_request_security",
            None,
        )
        previous_revocation = getattr(
            app_module.app.state,
            "server_actor_session_revocation",
            None,
        )
        app_module.app.state.server_mode_enabled = True
        app_module.app.state.server_request_security = ServerRequestSecurity(
            codec=self.codec,
            access=object(),  # type: ignore[arg-type]
            sessions=PostgresActorSessionRepository(self.engine),
        )
        app_module.app.state.server_actor_session_revocation = (
            PostgresActorSessionRevocationService(
                self.engine,
                audit=audit,
            )
        )
        client = TestClient(app_module.app)
        try:
            client.cookies.set(
                SERVER_AUTH_COOKIE_NAME,
                self.codec.create(
                    ActorIdentity(self.org_a, self.admin_a),
                    session_version=1,
                ),
            )
            path = (
                f"/api/organizations/{self.org_a}/users/"
                f"{self.member_a}/sessions/revoke"
            )
            rejected = client.post(
                path,
                json={"session_version": 99},
            )
            self.assertEqual(rejected.status_code, 422)
            self.assertEqual(self._version(self.org_a, self.member_a), 1)

            response = client.post(path, json={})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(
                response.json(),
                {"user_id": self.member_a, "revoked": True},
            )
            self.assertEqual(self._version(self.org_a, self.member_a), 2)
            self.assertEqual(len(audit.events), 1)

            cross_org = client.post(
                (
                    f"/api/organizations/{self.org_b}/users/"
                    f"{self.member_b}/sessions/revoke"
                ),
                json={},
            )
            self.assertEqual(cross_org.status_code, 403)
            self.assertEqual(self._version(self.org_b, self.member_b), 1)

            client.cookies.set(
                SERVER_AUTH_COOKIE_NAME,
                old_member_token,
            )
            self.assertEqual(client.get("/api/projects").status_code, 401)

            client.cookies.set(
                SERVER_AUTH_COOKIE_NAME,
                self.codec.create(
                    ActorIdentity(self.org_a, self.member_a),
                    session_version=2,
                ),
            )
            denied = client.post(
                (
                    f"/api/organizations/{self.org_a}/users/"
                    f"{self.admin_a}/sessions/revoke"
                ),
                json={},
            )
            self.assertEqual(denied.status_code, 403)
        finally:
            client.close()
            app_module.app.state.server_mode_enabled = previous_mode
            app_module.app.state.server_request_security = previous_security
            app_module.app.state.server_actor_session_revocation = (
                previous_revocation
            )

    def test_http_audit_failure_rolls_back_and_redacts_error(self) -> None:
        import app as app_module

        previous_mode = getattr(
            app_module.app.state,
            "server_mode_enabled",
            None,
        )
        previous_security = getattr(
            app_module.app.state,
            "server_request_security",
            None,
        )
        previous_revocation = getattr(
            app_module.app.state,
            "server_actor_session_revocation",
            None,
        )
        app_module.app.state.server_mode_enabled = True
        app_module.app.state.server_request_security = ServerRequestSecurity(
            codec=self.codec,
            access=object(),  # type: ignore[arg-type]
            sessions=PostgresActorSessionRepository(self.engine),
        )
        app_module.app.state.server_actor_session_revocation = (
            PostgresActorSessionRevocationService(
                self.engine,
                audit=FailingAuditWriter(),
            )
        )
        client = TestClient(app_module.app)
        try:
            client.cookies.set(
                SERVER_AUTH_COOKIE_NAME,
                self.codec.create(
                    ActorIdentity(self.org_a, self.admin_a),
                    session_version=1,
                ),
            )
            response = client.post(
                (
                    f"/api/organizations/{self.org_a}/users/"
                    f"{self.member_a}/sessions/revoke"
                ),
                json={},
            )
            self.assertEqual(response.status_code, 503)
            self.assertNotIn(SECRET_ERROR, response.text)
            self.assertEqual(self._version(self.org_a, self.member_a), 1)
        finally:
            client.close()
            app_module.app.state.server_mode_enabled = previous_mode
            app_module.app.state.server_request_security = previous_security
            app_module.app.state.server_actor_session_revocation = (
                previous_revocation
            )


if __name__ == "__main__":
    unittest.main()
