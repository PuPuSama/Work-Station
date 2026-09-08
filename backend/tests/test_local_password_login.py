from __future__ import annotations

import sys
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services.access_control import ActorIdentity  # noqa: E402
from services.local_password_login import (  # noqa: E402
    LocalPasswordLoginFailed,
    LocalPasswordLoginService,
    LocalPasswordLoginSettings,
    LocalPasswordLoginUnavailable,
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
