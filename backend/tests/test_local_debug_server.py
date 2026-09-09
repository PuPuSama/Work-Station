from __future__ import annotations

import importlib.util
import tempfile
import unittest
import asyncio
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "local_debug_server",
    ROOT / "scripts" / "local_debug_server.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class LocalDebugServerTests(unittest.TestCase):
    def environment(self, env_file: Path) -> dict[str, str]:
        return {
            "ARTICLE_AGENT_DATABASE_URL": MODULE.EXPECTED_DATABASE_URL,
            "ARTICLE_AGENT_OBJECT_STORE_ENDPOINT": MODULE.EXPECTED_OBJECT_ENDPOINT,
            "ARTICLE_AGENT_OBJECT_STORE_BUCKET": MODULE.EXPECTED_OBJECT_BUCKET,
            "ARTICLE_AGENT_CONFIG": str(ROOT / MODULE.EXPECTED_CONFIG),
            "ARTICLE_AGENT_ROOT": str(ROOT),
            "ARTICLE_AGENT_LOCAL_DEBUG": "1",
            "ARTICLE_AGENT_ENV_FILE": str(env_file),
        }

    def test_valid_environment_is_accepted_without_database_connection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "empty.env"
            env_file.touch()
            MODULE.validate_local_debug_environment(self.environment(env_file))

    def test_disabled_or_production_values_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "empty.env"
            env_file.touch()
            environment = self.environment(env_file)
            for key, value in (
                ("ARTICLE_AGENT_LOCAL_DEBUG", "0"),
                ("ARTICLE_AGENT_CONFIG", "config.yaml"),
                ("ARTICLE_AGENT_OBJECT_STORE_BUCKET", "article-agent"),
                ("ARTICLE_AGENT_OBJECT_STORE_INTERNAL_ENDPOINT", "http://remote.invalid:9000"),
                ("ARTICLE_AGENT_CONFIG", str(Path(directory) / "config.local-debug.yaml")),
                ("ARTICLE_AGENT_ROOT", directory),
            ):
                with self.subTest(key=key):
                    candidate = dict(environment, **{key: value})
                    with self.assertRaises(MODULE.LocalDebugConfigurationError):
                        MODULE.validate_local_debug_environment(candidate)
            env_file.write_text("ARTICLE_AGENT_DATABASE_URL=unexpected\n", encoding="utf-8")
            with self.assertRaises(MODULE.LocalDebugConfigurationError):
                MODULE.validate_local_debug_environment(environment)

    def test_other_database_host_or_query_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "empty.env"
            env_file.touch()
            environment = self.environment(env_file)
            for database in (
                MODULE.EXPECTED_DATABASE_URL.replace("127.0.0.1", "localhost"),
                MODULE.EXPECTED_DATABASE_URL + "?sslmode=require",
                MODULE.EXPECTED_DATABASE_URL.replace("55438", "5432"),
            ):
                with self.subTest(database=database[-20:]):
                    with self.assertRaises(MODULE.LocalDebugConfigurationError):
                        MODULE.validate_local_debug_environment(
                            dict(environment, ARTICLE_AGENT_DATABASE_URL=database)
                        )

    def test_request_boundary_accepts_only_loopback_local_hosts(self) -> None:
        def scope(client, host="127.0.0.1:8108", origin=None, forwarded=None):
            headers = [(b"host", host.encode())]
            if origin:
                headers.append((b"origin", origin.encode()))
            if forwarded:
                headers.append((b"x-forwarded-host", forwarded.encode()))
            return {"client": (client, 1234), "headers": headers}

        self.assertTrue(MODULE.request_boundary_allowed(scope("127.0.0.1")))
        self.assertTrue(
            MODULE.request_boundary_allowed(
                scope("127.0.0.1", forwarded="127.0.0.1:3108")
            )
        )
        self.assertTrue(
            MODULE.request_boundary_allowed(
                scope("::1", origin="http://localhost:3108")
            )
        )
        self.assertFalse(MODULE.request_boundary_allowed(scope("192.0.2.1")))
        self.assertFalse(MODULE.request_boundary_allowed(scope("127.0.0.1", host="evil.test:8108")))
        self.assertFalse(
            MODULE.request_boundary_allowed(
                scope("127.0.0.1", forwarded="evil.test:8108")
            )
        )
        self.assertFalse(
            MODULE.request_boundary_allowed(
                scope("127.0.0.1", origin="https://localhost:3108")
            )
        )
        self.assertFalse(
            MODULE.request_boundary_allowed(
                scope("127.0.0.1", host="user@localhost:8108")
            )
        )
        self.assertFalse(
            MODULE.request_boundary_allowed(
                scope("127.0.0.1", forwarded="127.0.0.1:3108,evil.test:8108")
            )
        )
        self.assertFalse(
            MODULE.request_boundary_allowed(
                scope("127.0.0.1", origin="http://localhost:not-a-port")
            )
        )

    def test_anonymous_api_is_not_auto_authenticated(self) -> None:
        class Security:
            debug_actor = SimpleNamespace(organization_id="local-debug-org", user_id="local-debug-user")

            def __init__(self):
                self.calls = []

            def authenticate(self, token):
                self.calls.append(token)
                if token == "invalid":
                    raise MODULE.ServerRequestUnauthenticated("invalid")
                return self.debug_actor

        class Codec:
            def create(self, *args, **kwargs):
                raise AssertionError("invalid cookie must not trigger auto-issue")

        security = Security()
        async def call_next(request):
            return MODULE.Response("unauthenticated", status_code=401)

        middleware = MODULE.LocalDebugAuthMiddleware(
            lambda scope, receive, send: None,
            codec=Codec(),
            security=security,
        )
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/projects",
            "raw_path": b"/api/projects",
            "query_string": b"",
            "scheme": "http",
            "server": ("127.0.0.1", 8108),
            "client": ("127.0.0.1", 1234),
            "headers": [(b"host", b"127.0.0.1:8108")],
        }
        response = asyncio.run(
            middleware.dispatch(
                MODULE.Request(scope),
                call_next,
            )
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(security.calls, [])

    def test_invalid_debug_cookie_is_not_replaced(self) -> None:
        class Security:
            debug_actor = SimpleNamespace(organization_id="local-debug-org", user_id="local-debug-user")

            def __init__(self):
                self.calls = []

            def authenticate(self, token):
                self.calls.append(token)
                raise MODULE.ServerRequestUnauthenticated("invalid")

        class Codec:
            def create(self, *args, **kwargs):
                raise AssertionError("invalid cookie must not trigger auto-issue")

        async def call_next(request):
            return MODULE.Response("unauthenticated", status_code=401)

        security = Security()
        middleware = MODULE.LocalDebugAuthMiddleware(
            lambda scope, receive, send: None,
            codec=Codec(),
            security=security,
        )
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/auth/status",
            "raw_path": b"/api/auth/status",
            "query_string": b"",
            "scheme": "http",
            "server": ("127.0.0.1", 8108),
            "client": ("127.0.0.1", 1234),
            "headers": [
                (b"host", b"127.0.0.1:8108"),
                (
                    b"cookie",
                    b"article_agent_local_debug=invalid; article_agent_actor_session=old",
                ),
            ],
        }
        response = asyncio.run(
            middleware.dispatch(MODULE.Request(scope), call_next)
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(security.calls, ["invalid"])
        self.assertFalse(
            any(
                name.lower() == b"set-cookie"
                and b"article_agent_local_debug=" in value
                for name, value in response.raw_headers
            )
        )

    def test_internal_cookie_mapping_and_empty_cookie_behavior(self) -> None:
        class Security:
            debug_actor = SimpleNamespace(
                organization_id="local-debug-org", user_id="local-debug-user"
            )

            def __init__(self):
                self.calls = []

            def authenticate(self, token):
                self.calls.append(token)
                if token == "valid":
                    return self.debug_actor
                raise MODULE.ServerRequestUnauthenticated("invalid")

        class Codec:
            def create(self, *args, **kwargs):
                return "issued"

        captured = []

        async def capture(request):
            captured.append(request.headers.get("cookie", ""))
            return MODULE.Response("ok", status_code=200)

        def make_scope(cookie):
            return {
                "type": "http",
                "method": "GET",
                "path": "/api/projects",
                "raw_path": b"/api/projects",
                "query_string": b"",
                "scheme": "http",
                "server": ("127.0.0.1", 8108),
                "client": ("127.0.0.1", 1234),
                "headers": [
                    (b"host", b"127.0.0.1:8108"),
                    (b"cookie", cookie.encode()),
                ],
            }

        security = Security()
        middleware = MODULE.LocalDebugAuthMiddleware(
            lambda scope, receive, send: None,
            codec=Codec(),
            security=security,
        )
        response = asyncio.run(
            middleware.dispatch(
                MODULE.Request(
                    make_scope(
                        "article_agent_local_debug=valid; article_agent_actor_session=old"
                    )
                ),
                capture,
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("article_agent_actor_session=valid", captured[-1])
        self.assertNotIn("old", captured[-1])

        captured.clear()
        response = asyncio.run(
            middleware.dispatch(
                MODULE.Request(make_scope("article_agent_local_debug=")),
                capture,
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured, ["article_agent_local_debug="])
        self.assertNotIn("issued", security.calls)


if __name__ == "__main__":
    unittest.main()
