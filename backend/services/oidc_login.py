from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit

import httpx

from services.external_identity import (
    ExternalActorSessionService,
    ExternalIdentityRepository,
)
from services.oidc_identity import (
    OidcIdTokenVerifier,
    OidcProviderClient,
    OidcProviderSettings,
)
from services.server_auth import ServerActorSessionCodec


OIDC_STATE_COOKIE_NAME = "article_agent_oidc_login_state"
_STATE_VERSION = 1


class OidcLoginStateError(PermissionError):
    """The signed login transaction state is missing or invalid."""


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _base64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.b64decode(
            value + padding,
            altchars=b"-_",
            validate=True,
        )
    except (TypeError, ValueError) as exc:
        raise OidcLoginStateError("OIDC login state is invalid") from exc


@dataclass(frozen=True, slots=True)
class OidcLoginAttempt:
    authorization_url: str
    state_cookie: str
    max_age: int


@dataclass(frozen=True, slots=True)
class OidcLoginResult:
    actor_session: str
    redirect_path: str


@dataclass(frozen=True, slots=True)
class _LoginState:
    state: str
    nonce: str
    code_verifier: str
    redirect_path: str


def _local_redirect_path(value: str) -> str:
    normalized = value.strip()
    parsed = urlsplit(normalized)
    if (
        not normalized.startswith("/")
        or normalized.startswith("//")
        or parsed.scheme
        or parsed.netloc
        or parsed.fragment
        or len(normalized) > 2048
    ):
        raise OidcLoginStateError("OIDC login state is invalid")
    return normalized


class OidcLoginStateCodec:
    """Sign short-lived State/Nonce/PKCE data without a server-side cache."""

    def __init__(
        self,
        secret: bytes,
        *,
        lifetime_seconds: int,
    ) -> None:
        source = bytes(secret)
        if len(source) < 32 or not 1 <= lifetime_seconds <= 30 * 60:
            raise OidcLoginStateError("OIDC login state is invalid")
        self._secret = hmac.new(
            source,
            b"article-agent-oidc-login-state-v1",
            hashlib.sha256,
        ).digest()
        self.lifetime_seconds = int(lifetime_seconds)

    def create(
        self,
        *,
        redirect_path: str,
        now: int | None = None,
    ) -> tuple[_LoginState, str, str]:
        issued_at = int(time.time() if now is None else now)
        normalized_redirect = _local_redirect_path(redirect_path)
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        code_verifier = secrets.token_urlsafe(48)
        payload = {
            "v": _STATE_VERSION,
            "state": state,
            "nonce": nonce,
            "verifier": code_verifier,
            "redirect": normalized_redirect,
            "iat": issued_at,
            "exp": issued_at + self.lifetime_seconds,
        }
        encoded = _base64url_encode(
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )
        signature = _base64url_encode(
            hmac.new(
                self._secret,
                encoded.encode("ascii"),
                hashlib.sha256,
            ).digest()
        )
        challenge = _base64url_encode(
            hashlib.sha256(code_verifier.encode("ascii")).digest()
        )
        return (
            _LoginState(
                state,
                nonce,
                code_verifier,
                normalized_redirect,
            ),
            f"{encoded}.{signature}",
            challenge,
        )

    def parse(
        self,
        token: str,
        *,
        supplied_state: str,
        now: int | None = None,
    ) -> _LoginState:
        try:
            encoded, supplied_signature = token.split(".", 1)
            expected_signature = _base64url_encode(
                hmac.new(
                    self._secret,
                    encoded.encode("ascii"),
                    hashlib.sha256,
                ).digest()
            )
            if not hmac.compare_digest(
                supplied_signature,
                expected_signature,
            ):
                raise TypeError
            payload = json.loads(_base64url_decode(encoded))
            if (
                not isinstance(payload, dict)
                or set(payload)
                != {
                    "v",
                    "state",
                    "nonce",
                    "verifier",
                    "redirect",
                    "iat",
                    "exp",
                }
            ):
                raise TypeError
            version = int(payload["v"])
            issued_at = int(payload["iat"])
            expires_at = int(payload["exp"])
            state = str(payload["state"])
            nonce = str(payload["nonce"])
            verifier = str(payload["verifier"])
            redirect_path = _local_redirect_path(
                str(payload["redirect"])
            )
            current = int(time.time() if now is None else now)
            if (
                version != _STATE_VERSION
                or not hmac.compare_digest(state, supplied_state)
                or issued_at > current + 60
                or expires_at <= current
                or expires_at <= issued_at
                or expires_at - issued_at > self.lifetime_seconds
                or not 32 <= len(state) <= 512
                or not 32 <= len(nonce) <= 512
                or not 43 <= len(verifier) <= 128
            ):
                raise TypeError
            return _LoginState(
                state,
                nonce,
                verifier,
                redirect_path,
            )
        except (
            AttributeError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            raise OidcLoginStateError(
                "OIDC login state is invalid"
            ) from exc


class OidcLoginService:
    """Authorization Code + PKCE flow ending in a minimal Actor session."""

    def __init__(
        self,
        *,
        settings: OidcProviderSettings,
        provider: OidcProviderClient,
        verifier: OidcIdTokenVerifier,
        sessions: ExternalActorSessionService,
        state_codec: OidcLoginStateCodec,
    ) -> None:
        self.settings = settings
        self._provider = provider
        self._verifier = verifier
        self._sessions = sessions
        self._state_codec = state_codec

    @classmethod
    def create(
        cls,
        *,
        settings: OidcProviderSettings,
        identities: ExternalIdentityRepository,
        codec: ServerActorSessionCodec,
        client: httpx.Client | None = None,
    ) -> OidcLoginService:
        provider = OidcProviderClient(settings, client=client)
        return cls(
            settings=settings,
            provider=provider,
            verifier=OidcIdTokenVerifier(settings, provider),
            sessions=ExternalActorSessionService(
                identities=identities,
                codec=codec,
            ),
            state_codec=OidcLoginStateCodec(
                codec.secret,
                lifetime_seconds=settings.state_seconds,
            ),
        )

    def begin(
        self,
        *,
        redirect_path: str | None = None,
    ) -> OidcLoginAttempt:
        state, cookie, challenge = self._state_codec.create(
            redirect_path=(
                self.settings.post_login_path
                if redirect_path is None
                else redirect_path
            )
        )
        document = self._provider.discovery()
        query = urlencode(
            {
                "response_type": "code",
                "scope": "openid",
                "client_id": self.settings.client_id,
                "redirect_uri": self.settings.redirect_uri,
                "state": state.state,
                "nonce": state.nonce,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        separator = (
            "&" if "?" in document.authorization_endpoint else "?"
        )
        return OidcLoginAttempt(
            authorization_url=(
                f"{document.authorization_endpoint}{separator}{query}"
            ),
            state_cookie=cookie,
            max_age=self.settings.state_seconds,
        )

    def complete(
        self,
        *,
        code: str,
        state: str,
        state_cookie: str,
    ) -> OidcLoginResult:
        login_state = self._state_codec.parse(
            state_cookie,
            supplied_state=state,
        )
        id_token = self._provider.exchange_code(
            code=code,
            code_verifier=login_state.code_verifier,
        )
        identity = self._verifier.verify(
            id_token,
            expected_nonce=login_state.nonce,
        )
        return OidcLoginResult(
            actor_session=self._sessions.create_session(
                identity,
                max_age=self.settings.session_seconds,
            ),
            redirect_path=login_state.redirect_path,
        )

    def close(self) -> None:
        self._provider.close()


__all__ = [
    "OIDC_STATE_COOKIE_NAME",
    "OidcLoginAttempt",
    "OidcLoginResult",
    "OidcLoginService",
    "OidcLoginStateCodec",
    "OidcLoginStateError",
]
