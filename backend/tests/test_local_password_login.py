from __future__ import annotations

import sys
import unittest
from pathlib import Path

import sqlalchemy as sa
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services.access_control import ActorIdentity  # noqa: E402
from server_schema import (  # noqa: E402
    organizations,
    workspace_users,
)
from services.local_password_login import (  # noqa: E402
    LocalPasswordLoginFailed,
    LocalPasswordLoginService,
    LocalPasswordLoginSettings,
    LocalPasswordLoginUnavailable,
    hash_local_password,
    verify_local_password,
)
from services.server_auth import ServerActorSessionCodec  # noqa: E402


class _Result:
    def mappings(self):
        return self

    def all(self):
        return [{
            "organization_id": "org-a",
            "user_id": "user-a",
            "session_version": 3,
        }]


class _Connection:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, _statement):
        return _Result()


class _Engine:
    def connect(self):
        return _Connection()


class LocalPasswordLoginTests(unittest.TestCase):
    def test_password_hash_round_trip(self) -> None:
        encoded = hash_local_password("old-password")
        self.assertTrue(verify_local_password("old-password", encoded))
        self.assertFalse(verify_local_password("wrong-password", encoded))

    def test_database_password_login_and_change_rotate_sessions(self) -> None:
        engine = sa.create_engine("sqlite://")
        test_metadata = sa.MetaData()
        sa.Table(
            "organizations",
            test_metadata,
            sa.Column("organization_id", sa.Text(), primary_key=True),
            sa.Column("name", sa.Text(), nullable=False),
            sa.Column("status", sa.Text(), nullable=False),
        )
        sa.Table(
            "workspace_users",
            test_metadata,
            sa.Column("organization_id", sa.Text(), nullable=False),
            sa.Column("user_id", sa.Text(), nullable=False),
            sa.Column("display_name", sa.Text(), nullable=False),
            sa.Column("organization_role", sa.Text(), nullable=False),
            sa.Column("status", sa.Text(), nullable=False),
            sa.Column("session_version", sa.BigInteger(), nullable=False),
            sa.Column("password_hash", sa.Text(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("organization_id", "user_id"),
        )
        test_metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(
                organizations.insert().values(
                    organization_id="org-a",
                    name="Organization A",
                    status="active",
                )
            )
            connection.execute(
                workspace_users.insert().values(
                    organization_id="org-a",
                    user_id="admin",
                    display_name="Administrator",
                    organization_role="org_admin",
                    status="active",
                    session_version=1,
                    password_hash=hash_local_password("old-password"),
                )
            )

        class RecordingAudit:
            def __init__(self) -> None:
                self.events = []

            def append(self, _connection, event) -> None:
                self.events.append(event)

        audit = RecordingAudit()
        service = LocalPasswordLoginService(
            engine,
            codec=ServerActorSessionCodec(b"s" * 32),
            settings=LocalPasswordLoginSettings.database_only(),
            audit=audit,
        )
        actor = ActorIdentity("org-a", "admin")
        logged_in = service.login("admin", "old-password")
        self.assertEqual(logged_in.actor, actor)
        changed = service.change_password(
            actor=actor,
            current_password="old-password",
            new_password="new-password",
            event_id="password-change-1",
        )
        self.assertEqual(changed.actor, actor)
        self.assertEqual([event.action for event in audit.events], [
            "workspace_user.password_changed",
        ])
        with engine.connect() as connection:
            row = connection.execute(
                sa.select(
                    workspace_users.c.password_hash,
                    workspace_users.c.session_version,
                ).where(
                    workspace_users.c.organization_id == "org-a",
                    workspace_users.c.user_id == "admin",
                )
            ).one()
        self.assertEqual(row.session_version, 2)
        self.assertTrue(verify_local_password("new-password", row.password_hash))
        with self.assertRaises(LocalPasswordLoginFailed):
            service.login("admin", "old-password")

    def test_login_route_sets_the_server_actor_cookie(self) -> None:
        import app as app_module

        service = LocalPasswordLoginService(
            _Engine(),
            codec=ServerActorSessionCodec(b"s" * 32),
            settings=LocalPasswordLoginSettings(
                username="admin",
                password="secret",
                organization_id="org-a",
                user_id="user-a",
            ),
        )
        previous = getattr(
            app_module.app.state,
            "server_password_login",
            None,
        )
        app_module.app.state.server_password_login = service
        client = TestClient(app_module.app)
        try:
            response = client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "secret"},
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn("article_agent_actor_session", client.cookies)
            self.assertTrue(response.json()["data"]["authenticated"])

            wrong = client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "wrong"},
            )
            self.assertEqual(wrong.status_code, 401, wrong.text)
        finally:
            client.close()
            app_module.app.state.server_password_login = previous

    def test_settings_require_credentials_and_allow_actor_defaults(self) -> None:
        self.assertIsNone(
            LocalPasswordLoginSettings.from_environment(
                {
                    "ARTICLE_AGENT_LOGIN_USERNAME": "admin",
                    "ARTICLE_AGENT_LOGIN_ORGANIZATION_ID": "org-a",
                }
            )
        )
        settings = LocalPasswordLoginSettings.from_environment(
            {
                "ARTICLE_AGENT_LOGIN_USERNAME": " admin ",
                "ARTICLE_AGENT_LOGIN_PASSWORD": "secret",
                "ARTICLE_AGENT_LOGIN_SESSION_SECONDS": "3600",
            }
        )
        assert settings is not None
        self.assertEqual(settings.username, "admin")
        self.assertEqual(settings.user_id, "")
        self.assertEqual(settings.session_seconds, 3600)

    def test_login_checks_credentials_and_issues_actor_session(self) -> None:
        settings = LocalPasswordLoginSettings(
            username="admin",
            password="secret",
            organization_id="org-a",
            user_id="user-a",
            session_seconds=3600,
        )
        service = LocalPasswordLoginService(
            _Engine(),
            codec=ServerActorSessionCodec(b"s" * 32),
            settings=settings,
        )
        result = service.login(" admin ", "secret")
        self.assertEqual(result.actor, ActorIdentity("org-a", "user-a"))
        self.assertEqual(
            result.actor_session.count("."),
            1,
        )

        with self.assertRaises(LocalPasswordLoginFailed):
            service.login("admin", "wrong")

    def test_login_rejects_a_disabled_or_missing_actor(self) -> None:
        class MissingConnection(_Connection):
            def execute(self, _statement):
                class MissingResult:
                    def mappings(self):
                        return self

                    def all(self):
                        return []

                return MissingResult()

        class MissingEngine(_Engine):
            def connect(self):
                return MissingConnection()

        service = LocalPasswordLoginService(
            MissingEngine(),
            codec=ServerActorSessionCodec(b"s" * 32),
            settings=LocalPasswordLoginSettings(
                username="admin",
                password="secret",
                organization_id="org-a",
                user_id="user-a",
            ),
        )
        with self.assertRaises(LocalPasswordLoginUnavailable):
            service.login("admin", "secret")


if __name__ == "__main__":
    unittest.main()
