from __future__ import annotations

import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import app as app_module  # noqa: E402
from config import load_config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from services.auth import (  # noqa: E402
    AUTH_COOKIE_NAME,
    create_session_token,
    valid_session_token,
)


class AuthenticationTests(unittest.TestCase):
    def test_password_login_protects_api_and_logout_clears_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = replace(
                load_config(),
                data_file=root / "tasks.json",
                output_root=root / "projects",
                topic_library=root / "topics",
                knowledge_base=root / "knowledge",
            )
            environment = {
                "APP_PASSWORD": "correct horse battery staple",
                "APP_SESSION_SECRET": "test-session-secret",
                "APP_COOKIE_SECURE": "false",
            }
            with (
                patch.dict(os.environ, environment, clear=False),
                patch.object(app_module, "config", return_value=cfg),
            ):
                client = TestClient(app_module.app)
                public_health = client.get("/api/health")
                unauthenticated = client.get("/api/tasks")
                wrong = client.post(
                    "/api/auth/login",
                    json={"password": "wrong"},
                )
                login = client.post(
                    "/api/auth/login",
                    json={"password": environment["APP_PASSWORD"]},
                )
                authenticated = client.get("/api/tasks")
                logout = client.post("/api/auth/logout")
                signed_out = client.get("/api/tasks")

            self.assertEqual(public_health.status_code, 200)
            self.assertEqual(unauthenticated.status_code, 401)
            self.assertEqual(wrong.status_code, 401)
            self.assertEqual(login.status_code, 200, login.text)
            self.assertIn(AUTH_COOKIE_NAME, login.cookies)
            self.assertEqual(authenticated.status_code, 200, authenticated.text)
            self.assertEqual(logout.status_code, 200)
            self.assertEqual(signed_out.status_code, 401)

    def test_session_signature_and_expiry_are_verified(self) -> None:
        with patch.dict(
            os.environ,
            {
                "APP_PASSWORD": "password",
                "APP_SESSION_SECRET": "secret",
            },
            clear=False,
        ):
            token = create_session_token(now=1_000, max_age=60)
            self.assertTrue(valid_session_token(token, now=1_059))
            self.assertFalse(valid_session_token(token, now=1_060))
            self.assertFalse(valid_session_token(f"{token}tampered", now=1_001))

    def test_authentication_is_disabled_without_password(self) -> None:
        with patch.dict(
            os.environ,
            {"APP_PASSWORD": "", "APP_SESSION_SECRET": ""},
            clear=False,
        ):
            response = TestClient(app_module.app).get("/api/auth/status")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["data"]["enabled"])
        self.assertTrue(response.json()["data"]["authenticated"])


if __name__ == "__main__":
    unittest.main()
