from __future__ import annotations

import base64
import json
import sys
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services.access_control import ActorIdentity  # noqa: E402
from services.server_auth import (  # noqa: E402
    MAX_SERVER_SESSION_SECONDS,
    ServerActorSessionCodec,
    ServerActorSessionError,
    load_server_actor_session_codec,
    server_mode_enabled,
)


SECRET = b"m7-test-only-secret-with-at-least-32-bytes"


class ServerActorSessionTests(unittest.TestCase):
    def test_server_mode_is_always_enabled(self) -> None:
        self.assertTrue(server_mode_enabled({}))
        self.assertTrue(server_mode_enabled({"ARTICLE_AGENT_SERVER_MODE": "off"}))

    def test_token_roundtrip_contains_identity_but_no_role(self) -> None:
        codec = ServerActorSessionCodec(SECRET)
        actor = ActorIdentity("org-a", "user-a")

        token = codec.create(
            actor,
            session_version=7,
            now=1_000,
            max_age=600,
        )
        parsed = codec.parse(token, now=1_599)
        session = codec.parse_session(token, now=1_599)
        encoded_payload = token.split(".", 1)[0]
        payload = json.loads(
            base64.urlsafe_b64decode(
                encoded_payload + "=" * (-len(encoded_payload) % 4)
            )
        )

        self.assertEqual(parsed, actor)
        self.assertEqual(payload["v"], 2)
        self.assertEqual(payload["org"], "org-a")
        self.assertEqual(payload["sub"], "user-a")
        self.assertEqual(payload["sv"], 7)
        self.assertEqual(session.session_version, 7)
        self.assertNotIn("role", payload)
        self.assertNotIn("permissions", payload)

    def test_tampering_expiry_and_future_issuance_are_rejected(self) -> None:
        codec = ServerActorSessionCodec(SECRET)
        token = codec.create(
            ActorIdentity("org-a", "user-a"),
            now=1_000,
            max_age=60,
        )

        for candidate, now in (
            (f"{token}tampered", 1_001),
            (token, 1_060),
            (token, 900),
            ("malformed", 1_001),
        ):
            with self.subTest(candidate=candidate[-12:], now=now):
                with self.assertRaisesRegex(
                    ServerActorSessionError,
                    "^invalid actor session$",
                ):
                    codec.parse(candidate, now=now)

    def test_secret_and_lifetime_configuration_fail_closed(self) -> None:
        with self.assertRaisesRegex(
            ServerActorSessionError,
            "at least 32 bytes",
        ):
            ServerActorSessionCodec(b"short")
        with self.assertRaisesRegex(
            ServerActorSessionError,
            "lifetime",
        ):
            ServerActorSessionCodec(SECRET).create(
                ActorIdentity("org-a", "user-a"),
                now=1_000,
                max_age=MAX_SERVER_SESSION_SECONDS + 1,
            )
        with self.assertRaisesRegex(
            ServerActorSessionError,
            "version",
        ):
            ServerActorSessionCodec(SECRET).create(
                ActorIdentity("org-a", "user-a"),
                session_version=0,
            )
        with self.assertRaisesRegex(
            ServerActorSessionError,
            "ARTICLE_AGENT_SERVER_SESSION_SECRET",
        ):
            load_server_actor_session_codec(
                {
                    "APP_SESSION_SECRET": SECRET.decode("ascii"),
                    "ARTICLE_AGENT_SERVER_SESSION_SECRET": "",
                }
            )

    def test_errors_do_not_contain_session_secret(self) -> None:
        secret = "unique-server-session-secret-1234567890"
        codec = load_server_actor_session_codec(
            {"ARTICLE_AGENT_SERVER_SESSION_SECRET": secret}
        )
        try:
            codec.parse("invalid")
        except ServerActorSessionError as exc:
            self.assertNotIn(secret, str(exc))
        else:
            self.fail("invalid session must be rejected")


if __name__ == "__main__":
    unittest.main()
