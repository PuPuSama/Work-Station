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
from services.external_identity import (  # noqa: E402
    ExternalIdentityNotAuthorized,
    ResolvedExternalActor,
)
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
    OidcLoginService,
    OidcLoginStateCodec,
    OidcLoginStateError,
)
from services.server_auth import ServerActorSessionCodec  # noqa: E402
from services.workspace_invitations import (  # noqa: E402
    WorkspaceInvitationDenied,
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


class FakeInvitationRedeemer:
    def __init__(
        self,
        actor: ResolvedExternalActor,
        error: Exception | None = None,
    ) -> None:
        self.actor = actor
        self.error = error
        self.calls = []

    def redeem(self, *, invitation_token, identity, event_id):
        self.calls.append((invitation_token, identity, event_id))
        if self.error is not None:
            raise self.error
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
        for origin in (
            "http://app.example.test",
            "https://app.example.test/path",
            "https://user@app.example.test",
        ):
            with self.subTest(origin=origin):
                with self.assertRaisesRegex(
                    OidcConfigurationError,
                    "safe origin",
                ):
                    OidcProviderSettings(
                        issuer=ISSUER,
                        client_id=CLIENT_ID,
                        client_secret=CLIENT_SECRET,
                        redirect_uri=REDIRECT_URI,
                        post_login_origin=origin,
                    )

    def test_loopback_post_login_origin_builds_absolute_safe_url(self) -> None:
        configured = replace(
            settings(),
            post_login_origin="http://127.0.0.1:3000/",
        )

        self.assertEqual(
            configured.post_login_url("/projects/example?tab=knowledge"),
            "http://127.0.0.1:3000/projects/example?tab=knowledge",
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

    def test_invitation_cookie_cannot_be_swapped_after_state_start(
        self,
    ) -> None:
        fake = FakeOidcProvider()
        _, public_jwk = make_key("unused-key")
        fake.keys = [public_jwk]
        client = httpx.Client(transport=fake.transport())
        service = OidcLoginService.create(
            settings=settings(),
            identities=FakeIdentityRepository(None),
            codec=ServerActorSessionCodec(b"w" * 32),
            invitations=FakeInvitationRedeemer(
                ResolvedExternalActor(
                    ActorIdentity("org-a", "user-a"),
                    session_version=1,
                )
            ),
            client=client,
        )
        try:
            attempt = service.begin(invitation_token="token-a")
            state = parse_qs(
                urlsplit(attempt.authorization_url).query
            )["state"][0]
            with self.assertRaises(OidcLoginStateError):
                service.complete(
                    code="must-not-be-exchanged",
                    state=state,
                    state_cookie=attempt.state_cookie,
                    invitation_token="token-b",
                )
            self.assertEqual(fake.token_calls, [])
        finally:
            service.close()
            client.close()


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
    def test_denied_invitation_is_returned_as_auth_failure(self) -> None:
        fake = FakeOidcProvider()
        private_key, public_jwk = make_key("denied-invitation-key")
        fake.keys = [public_jwk]
        http_client = httpx.Client(transport=fake.transport())
        redeemer = FakeInvitationRedeemer(
            ResolvedExternalActor(
                ActorIdentity("org-denied", "user-denied"),
                session_version=1,
            ),
            error=WorkspaceInvitationDenied(
                "invitation does not authorize this identity"
            ),
        )
        service = OidcLoginService.create(
            settings=settings(),
            identities=FakeIdentityRepository(None),
            codec=ServerActorSessionCodec(b"d" * 32),
            invitations=redeemer,
            client=http_client,
        )
        try:
            attempt = service.begin(invitation_token="denied-token")
            query = parse_qs(urlsplit(attempt.authorization_url).query)
            fake.id_token = encode_id_token(
                private_key,
                key_id="denied-invitation-key",
                nonce=query["nonce"][0],
            )
            with self.assertRaisesRegex(
                ExternalIdentityNotAuthorized,
                "external identity is not authorized",
            ):
                service.complete(
                    code="denied-code",
                    state=query["state"][0],
                    state_cookie=attempt.state_cookie,
                    invitation_token="denied-token",
                )
        finally:
            service.close()
            http_client.close()

    def test_invitation_token_is_http_only_state_bound_and_redeemed(
        self,
    ) -> None:
        import app as app_module
        client = TestClient(app_module.app, follow_redirects=False)
        try:
            prepared = client.post(
                "/api/auth/invitations/prepare",
                json={"invitation_token": "invite-token"},
            )
            self.assertEqual(prepared.status_code, 404, prepared.text)
            started = client.get("/api/auth/oidc/start")
            self.assertEqual(started.status_code, 404, started.text)
            completed = client.get(
                "/api/auth/oidc/callback",
                params={"code": "invitation-code", "state": "state"},
            )
            self.assertEqual(completed.status_code, 404, completed.text)
        finally:
            client.close()

    def test_authorization_code_pkce_flow_sets_minimal_actor_cookie(
        self,
    ) -> None:
        import app as app_module
        client = TestClient(
            app_module.app,
            follow_redirects=False,
        )
        try:
            started = client.get(
                "/api/auth/oidc/start",
                params={"next": "/projects/example?tab=knowledge"},
            )
            self.assertEqual(started.status_code, 404, started.text)
            status = client.get("/api/auth/status")
            self.assertFalse(status.json()["data"]["login_available"])
            self.assertNotIn("issuer", status.json()["data"])
            completed = client.get(
                "/api/auth/oidc/callback",
                params={"code": "one-time-code", "state": "state"},
            )
            self.assertEqual(completed.status_code, 404, completed.text)
        finally:
            client.close()

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
    def test_server_lifespan_does_not_wire_external_login(
        self,
    ) -> None:
        import app as app_module

        base_config = app_module.config()
        with tempfile.TemporaryDirectory() as directory:
            isolated = replace(
                base_config,
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
                        # Legacy OIDC settings must not re-enable an external
                        # login path after the local-password migration.
                        "ARTICLE_AGENT_ENABLE_OIDC": "true",
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
                self.assertIsNone(
                    getattr(app_module.app.state, "server_oidc_login", None)
                )
                status = client.get("/api/auth/status")
                self.assertEqual(status.status_code, 200)
                self.assertFalse(status.json()["data"]["login_available"])
                self.assertNotIn("issuer", status.json()["data"])


if __name__ == "__main__":
    unittest.main()
