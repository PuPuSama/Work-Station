from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services.access_control import ActorIdentity  # noqa: E402
from services.external_identity import ResolvedExternalActor  # noqa: E402
from services.oidc_identity import (  # noqa: E402
    OidcConfigurationError,
    OidcDiscoveryDocument,
    OidcIdTokenVerifier,
    OidcProviderClient,
    OidcProviderSettings,
    OidcProviderUnavailable,
    OidcVerificationError,
)
from services.oidc_login import (  # noqa: E402
    OIDC_STATE_COOKIE_NAME,
    OidcLoginService,
    OidcLoginStateCodec,
    OidcLoginStateError,
)
from services.server_auth import (  # noqa: E402
    SERVER_AUTH_COOKIE_NAME,
    ServerActorSessionCodec,
)
from services.server_request_security import (  # noqa: E402
    ServerRequestSecurity,
)


ISSUER = "https://id.example.test/tenant"
CLIENT_ID = "article-agent-client"
CLIENT_SECRET = "-".join(("oidc", "client", "test", "value"))
REDIRECT_URI = "https://app.example.test/api/auth/oidc/callback"
AUTHORIZATION_ENDPOINT = f"{ISSUER}/authorize"
TOKEN_ENDPOINT = f"{ISSUER}/token"
JWKS_URI = f"{ISSUER}/jwks"


def settings() -> OidcProviderSettings:
    return OidcProviderSettings(
        issuer=ISSUER,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
        post_login_path="/projects",
    )


def make_key(key_id: str):
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )
    public_jwk = RSAAlgorithm.to_jwk(
        private_key.public_key(),
        as_dict=True,
    )
    public_jwk.update(
        {"kid": key_id, "use": "sig", "alg": "RS256"}
    )
    return private_key, public_jwk


def encode_id_token(
    private_key,
    *,
    key_id: str,
    nonce: str,
    overrides: dict[str, object] | None = None,
) -> str:
    current = int(time.time())
    claims: dict[str, object] = {
        "iss": ISSUER,
        "sub": "external-subject",
        "aud": CLIENT_ID,
        "iat": current,
        "exp": current + 300,
        "nonce": nonce,
        "role": "org_admin",
        "groups": ["must-not-be-trusted"],
    }
    claims.update(overrides or {})
    return jwt.encode(
        claims,
        private_key,
        algorithm="RS256",
        headers={"kid": key_id},
    )


class FakeOidcProvider:
    def __init__(self) -> None:
        self.keys: list[dict[str, object]] = []
        self.id_token = ""
        self.discovery_calls = 0
        self.jwks_calls = 0
        self.token_calls: list[dict[str, object]] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        if (
            request.method == "GET"
            and str(request.url)
            == f"{ISSUER}/.well-known/openid-configuration"
        ):
            self.discovery_calls += 1
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": AUTHORIZATION_ENDPOINT,
                    "token_endpoint": TOKEN_ENDPOINT,
                    "jwks_uri": JWKS_URI,
                    "token_endpoint_auth_methods_supported": [
                        "client_secret_basic"
                    ],
                    "id_token_signing_alg_values_supported": [
                        "RS256"
                    ],
                },
            )
        if request.method == "GET" and str(request.url) == JWKS_URI:
            self.jwks_calls += 1
            return httpx.Response(200, json={"keys": self.keys})
        if request.method == "POST" and str(request.url) == TOKEN_ENDPOINT:
            form = {
                key: values[0]
                for key, values in parse_qs(
                    request.content.decode("utf-8")
                ).items()
            }
            self.token_calls.append(
                {
                    "form": form,
                    "authorization": request.headers.get(
                        "authorization",
                        "",
                    ),
                }
            )
            return httpx.Response(
                200,
                json={"id_token": self.id_token},
            )
        return httpx.Response(404, json={"error": "not found"})


class FakeIdentityRepository:
    def __init__(self, actor: ResolvedExternalActor | None) -> None:
        self.actor = actor
        self.identities = []

    def resolve(self, identity):
        self.identities.append(identity)
        return self.actor


class OidcConfigurationTests(unittest.TestCase):
    def test_environment_is_all_or_nothing_and_secret_is_redacted(
        self,
    ) -> None:
        self.assertIsNone(OidcProviderSettings.from_environment({}))
        with self.assertRaisesRegex(
            OidcConfigurationError,
            "configuration is incomplete",
        ) as raised:
            OidcProviderSettings.from_environment(
                {
                    "ARTICLE_AGENT_OIDC_ISSUER": ISSUER,
                    "ARTICLE_AGENT_OIDC_CLIENT_SECRET": (
                        CLIENT_SECRET
                    ),
                }
            )
        self.assertNotIn(CLIENT_SECRET, str(raised.exception))
        configured = settings()
        self.assertNotIn(CLIENT_SECRET, repr(configured))

    def test_remote_http_and_external_post_login_redirect_are_rejected(
        self,
    ) -> None:
        with self.assertRaisesRegex(
            OidcConfigurationError,
            "absolute HTTPS",
        ):
            OidcProviderSettings(
                issuer="http://id.example.test",
                client_id=CLIENT_ID,
                client_secret=CLIENT_SECRET,
                redirect_uri=REDIRECT_URI,
            )
        with self.assertRaisesRegex(
            OidcConfigurationError,
            "local path",
        ):
            OidcProviderSettings(
                issuer=ISSUER,
                client_id=CLIENT_ID,
                client_secret=CLIENT_SECRET,
                redirect_uri=REDIRECT_URI,
                post_login_path="https://evil.example.test/",
            )

    def test_discovery_issuer_match_is_exact_including_trailing_slash(
        self,
    ) -> None:
        configured = OidcProviderSettings(
            issuer=f"{ISSUER}/",
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            redirect_uri=REDIRECT_URI,
        )
        self.assertEqual(
            configured.discovery_url,
            f"{ISSUER}/.well-known/openid-configuration",
        )
        with self.assertRaisesRegex(
            OidcProviderUnavailable,
            "metadata is invalid",
        ):
            OidcDiscoveryDocument.from_mapping(
                {
                    "issuer": ISSUER,
                    "authorization_endpoint": AUTHORIZATION_ENDPOINT,
                    "token_endpoint": TOKEN_ENDPOINT,
                    "jwks_uri": JWKS_URI,
                    "token_endpoint_auth_methods_supported": [
                        "client_secret_basic"
                    ],
                    "id_token_signing_alg_values_supported": [
                        "RS256"
                    ],
                },
                expected_issuer=configured.issuer,
            )


class OidcStateTests(unittest.TestCase):
    def test_state_binds_nonce_pkce_state_and_expiry(self) -> None:
        codec = OidcLoginStateCodec(
            b"s" * 32,
            lifetime_seconds=600,
        )
        created, token, challenge = codec.create(
            redirect_path="/projects/example",
            now=1000,
        )
        parsed = codec.parse(
            token,
            supplied_state=created.state,
            now=1001,
        )

        self.assertEqual(parsed, created)
        self.assertEqual(
            challenge,
            base64.urlsafe_b64encode(
                hashlib.sha256(
                    created.code_verifier.encode("ascii")
                ).digest()
            )
            .decode("ascii")
            .rstrip("="),
        )
        for supplied_token, supplied_state, now in (
            (token + "x", created.state, 1001),
            (token, "wrong-state", 1001),
            (token, created.state, 1600),
        ):
            with self.subTest(
                supplied_state=supplied_state,
                now=now,
            ):
                with self.assertRaisesRegex(
                    OidcLoginStateError,
                    "^OIDC login state is invalid$",
                ):
                    codec.parse(
                        supplied_token,
                        supplied_state=supplied_state,
                        now=now,
                    )


class OidcTokenVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeOidcProvider()
        self.private_key, public_jwk = make_key("key-1")
        self.fake.keys = [public_jwk]
        self.client = httpx.Client(transport=self.fake.transport())
        self.provider = OidcProviderClient(
            settings(),
            client=self.client,
        )
        self.verifier = OidcIdTokenVerifier(
            settings(),
            self.provider,
        )

    def tearDown(self) -> None:
        self.client.close()

    def test_valid_token_returns_only_verified_issuer_and_subject(
        self,
    ) -> None:
        token = encode_id_token(
            self.private_key,
            key_id="key-1",
            nonce="nonce-a",
        )

        identity = self.verifier.verify(
            token,
            expected_nonce="nonce-a",
        )

        self.assertEqual(identity.issuer, ISSUER)
        self.assertEqual(identity.subject, "external-subject")
        self.assertFalse(hasattr(identity, "role"))
        self.assertFalse(hasattr(identity, "groups"))

    def test_claim_and_algorithm_failures_are_generic(self) -> None:
        current = int(time.time())
        cases = (
            (
                "issuer",
                encode_id_token(
                    self.private_key,
                    key_id="key-1",
                    nonce="nonce-a",
                    overrides={"iss": "https://other.example.test"},
                ),
                "nonce-a",
            ),
            (
                "audience",
                encode_id_token(
                    self.private_key,
                    key_id="key-1",
                    nonce="nonce-a",
                    overrides={"aud": "other-client"},
                ),
                "nonce-a",
            ),
            (
                "nonce",
                encode_id_token(
                    self.private_key,
                    key_id="key-1",
                    nonce="nonce-a",
                ),
                "nonce-b",
            ),
            (
                "expired",
                encode_id_token(
                    self.private_key,
                    key_id="key-1",
                    nonce="nonce-a",
                    overrides={
                        "iat": current - 600,
                        "exp": current - 120,
                    },
                ),
                "nonce-a",
            ),
            (
                "multiple-audience-without-azp",
                encode_id_token(
                    self.private_key,
                    key_id="key-1",
                    nonce="nonce-a",
                    overrides={"aud": [CLIENT_ID, "other-client"]},
                ),
                "nonce-a",
            ),
            (
                "untrusted-algorithm",
                jwt.encode(
                    {
                        "iss": ISSUER,
                        "sub": "external-subject",
                        "aud": CLIENT_ID,
                        "iat": current,
                        "exp": current + 300,
                        "nonce": "nonce-a",
                    },
                    "h" * 32,
                    algorithm="HS256",
                    headers={"kid": "key-1"},
                ),
                "nonce-a",
            ),
        )
        for name, token, nonce in cases:
            with self.subTest(name=name):
                with self.assertRaisesRegex(
                    OidcVerificationError,
                    "^OIDC identity verification failed$",
                ) as raised:
                    self.verifier.verify(
                        token,
                        expected_nonce=nonce,
                    )
                message = str(raised.exception)
                self.assertNotIn(token, message)
                self.assertNotIn(CLIENT_SECRET, message)

    def test_unknown_kid_forces_one_jwks_refresh_for_rotation(
        self,
    ) -> None:
        first = encode_id_token(
            self.private_key,
            key_id="key-1",
            nonce="nonce-a",
        )
        self.verifier.verify(first, expected_nonce="nonce-a")
        rotated_private, rotated_jwk = make_key("key-2")
        self.fake.keys = [rotated_jwk]
        rotated = encode_id_token(
            rotated_private,
            key_id="key-2",
            nonce="nonce-b",
        )

        identity = self.verifier.verify(
            rotated,
            expected_nonce="nonce-b",
        )

        self.assertEqual(identity.subject, "external-subject")
        self.assertEqual(self.fake.jwks_calls, 2)

    def test_provider_error_does_not_leak_body_or_client_secret(
        self,
    ) -> None:
        def failed_token_endpoint(
            request: httpx.Request,
        ) -> httpx.Response:
            if str(request.url) == (
                f"{ISSUER}/.well-known/openid-configuration"
            ):
                return self.fake.handle(request)
            if str(request.url) == TOKEN_ENDPOINT:
                return httpx.Response(
                    500,
                    json={
                        "error_description": (
                            f"provider echoed {CLIENT_SECRET}"
                        )
                    },
                )
            return self.fake.handle(request)

        client = httpx.Client(
            transport=httpx.MockTransport(failed_token_endpoint)
        )
        provider = OidcProviderClient(settings(), client=client)
        try:
            with self.assertRaisesRegex(
                OidcProviderUnavailable,
                "^identity provider request failed$",
            ) as raised:
                provider.exchange_code(
                    code="code-a",
                    code_verifier="v" * 64,
                )
            self.assertNotIn(
                CLIENT_SECRET,
                str(raised.exception),
            )
        finally:
            client.close()


class OidcLoginHttpTests(unittest.TestCase):
    def test_authorization_code_pkce_flow_sets_minimal_actor_cookie(
        self,
    ) -> None:
        import app as app_module

        fake = FakeOidcProvider()
        private_key, public_jwk = make_key("login-key")
        fake.keys = [public_jwk]
        http_client = httpx.Client(transport=fake.transport())
        codec = ServerActorSessionCodec(b"c" * 32)
        identities = FakeIdentityRepository(
            ResolvedExternalActor(
                ActorIdentity("org-a", "user-a"),
                session_version=1,
            )
        )
        service = OidcLoginService.create(
            settings=settings(),
            identities=identities,
            codec=codec,
            client=http_client,
        )
        previous_mode = getattr(
            app_module.app.state,
            "server_mode_enabled",
            None,
        )
        previous_login = getattr(
            app_module.app.state,
            "server_oidc_login",
            None,
        )
        previous_security = getattr(
            app_module.app.state,
            "server_request_security",
            None,
        )
        app_module.app.state.server_mode_enabled = True
        app_module.app.state.server_oidc_login = service
        app_module.app.state.server_request_security = (
            ServerRequestSecurity(
                codec=codec,
                access=object(),  # type: ignore[arg-type]
                sessions=type(
                    "AlwaysCurrentSessions",
                    (),
                    {"is_current": lambda self, session: True},
                )(),
            )
        )
        client = TestClient(
            app_module.app,
            follow_redirects=False,
        )
        try:
            started = client.get(
                "/api/auth/oidc/start",
                params={"next": "/projects/example?tab=knowledge"},
            )
            self.assertEqual(started.status_code, 307, started.text)
            authorization = urlsplit(started.headers["location"])
            query = parse_qs(authorization.query)
            self.assertEqual(
                f"{authorization.scheme}://{authorization.netloc}"
                f"{authorization.path}",
                AUTHORIZATION_ENDPOINT,
            )
            self.assertEqual(query["response_type"], ["code"])
            self.assertEqual(query["scope"], ["openid"])
            self.assertEqual(
                query["code_challenge_method"],
                ["S256"],
            )
            self.assertIn(OIDC_STATE_COOKIE_NAME, client.cookies)
            fake.id_token = encode_id_token(
                private_key,
                key_id="login-key",
                nonce=query["nonce"][0],
            )

            completed = client.get(
                "/api/auth/oidc/callback",
                params={
                    "code": "one-time-code",
                    "state": query["state"][0],
                },
            )

            self.assertEqual(completed.status_code, 303)
            self.assertEqual(
                completed.headers["location"],
                "/projects/example?tab=knowledge",
            )
            actor_token = client.cookies.get(
                SERVER_AUTH_COOKIE_NAME
            )
            self.assertIsNotNone(actor_token)
            self.assertEqual(
                codec.parse(str(actor_token)),
                ActorIdentity("org-a", "user-a"),
            )
            self.assertEqual(len(identities.identities), 1)
            self.assertEqual(len(fake.token_calls), 1)
            form = fake.token_calls[0]["form"]
            self.assertEqual(form["code"], "one-time-code")
            self.assertEqual(
                form["redirect_uri"],
                REDIRECT_URI,
            )
            verifier = str(form["code_verifier"])
            expected_challenge = base64.urlsafe_b64encode(
                hashlib.sha256(verifier.encode("ascii")).digest()
            ).decode("ascii").rstrip("=")
            self.assertEqual(
                expected_challenge,
                query["code_challenge"][0],
            )
            basic = str(fake.token_calls[0]["authorization"])
            self.assertTrue(basic.startswith("Basic "))
            decoded_basic = base64.b64decode(
                basic.removeprefix("Basic ")
            ).decode("utf-8")
            self.assertEqual(
                decoded_basic,
                f"{CLIENT_ID}:{CLIENT_SECRET}",
            )
            self.assertNotIn(CLIENT_SECRET, completed.text)
            status = client.get("/api/auth/status")
            self.assertTrue(status.json()["data"]["authenticated"])
            self.assertTrue(
                status.json()["data"]["login_available"]
            )
            invalid_destination = client.get(
                "/api/auth/oidc/start",
                params={"next": "https://evil.example.test/"},
            )
            self.assertEqual(
                invalid_destination.status_code,
                400,
            )
            replayed = client.get(
                "/api/auth/oidc/callback",
                params={
                    "code": "replayed-code",
                    "state": query["state"][0],
                },
            )
            self.assertEqual(replayed.status_code, 401)
            self.assertEqual(
                replayed.json()["detail"],
                "OIDC login failed.",
            )
            self.assertNotIn(CLIENT_SECRET, replayed.text)

            denied = client.get(
                "/api/auth/oidc/callback",
                params={
                    "error": "access_denied",
                    "error_description": (
                        f"provider echoed {CLIENT_SECRET}"
                    ),
                    "state": "provider-state",
                },
            )
            self.assertEqual(denied.status_code, 401)
            self.assertEqual(
                denied.json()["detail"],
                "OIDC login failed.",
            )
            self.assertNotIn(CLIENT_SECRET, denied.text)
            self.assertNotIn(
                OIDC_STATE_COOKIE_NAME,
                client.cookies,
            )
        finally:
            client.close()
            service.close()
            http_client.close()
            app_module.app.state.server_mode_enabled = previous_mode
            app_module.app.state.server_oidc_login = previous_login
            app_module.app.state.server_request_security = (
                previous_security
            )

    def test_local_mode_and_invalid_state_fail_closed(self) -> None:
        import app as app_module

        previous_mode = getattr(
            app_module.app.state,
            "server_mode_enabled",
            None,
        )
        try:
            app_module.app.state.server_mode_enabled = False
            client = TestClient(app_module.app)
            try:
                self.assertEqual(
                    client.get("/api/auth/oidc/start").status_code,
                    404,
                )
            finally:
                client.close()
        finally:
            app_module.app.state.server_mode_enabled = previous_mode

    @unittest.skipUnless(
        os.environ.get("ARTICLE_AGENT_DATABASE_URL"),
        "ARTICLE_AGENT_DATABASE_URL is required for server OIDC wiring",
    )
    def test_server_lifespan_wires_configured_oidc_lazily(
        self,
    ) -> None:
        import app as app_module

        base_config = app_module.config()
        with tempfile.TemporaryDirectory() as directory:
            isolated = replace(
                base_config,
                data_file=Path(directory) / "tasks.json",
                knowledge_agent_enabled=False,
            )
            with (
                patch.object(
                    app_module,
                    "config",
                    return_value=isolated,
                ),
                patch.dict(
                    os.environ,
                    {
                        "ARTICLE_AGENT_SERVER_MODE": "true",
                        "ARTICLE_AGENT_SERVER_SESSION_SECRET": "s" * 32,
                        "ARTICLE_AGENT_OIDC_ISSUER": ISSUER,
                        "ARTICLE_AGENT_OIDC_CLIENT_ID": CLIENT_ID,
                        "ARTICLE_AGENT_OIDC_CLIENT_SECRET": (
                            CLIENT_SECRET
                        ),
                        "ARTICLE_AGENT_OIDC_REDIRECT_URI": (
                            REDIRECT_URI
                        ),
                        "ARTICLE_AGENT_OIDC_POST_LOGIN_PATH": (
                            "/projects"
                        ),
                    },
                    clear=False,
                ),
                TestClient(app_module.app) as client,
            ):
                self.assertIsInstance(
                    app_module.app.state.server_oidc_login,
                    OidcLoginService,
                )
                status = client.get("/api/auth/status")
                self.assertEqual(status.status_code, 200)
                self.assertTrue(
                    status.json()["data"]["login_available"]
                )
                # Discovery/JWKS are deliberately lazy; startup and status
                # must not depend on a live external provider.


if __name__ == "__main__":
    unittest.main()
