from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from typing import Mapping

from services.access_control import ActorIdentity


SERVER_AUTH_COOKIE_NAME = "article_agent_actor_session"
DEFAULT_SERVER_SESSION_SECONDS = 12 * 60 * 60
MAX_SERVER_SESSION_SECONDS = 24 * 60 * 60
_TOKEN_VERSION = 2


class ServerActorSessionError(ValueError):
    """Raised for invalid configuration or an invalid actor session."""


def server_mode_enabled(environment: Mapping[str, str] | None = None) -> bool:
    source = os.environ if environment is None else environment
    raw = source.get("ARTICLE_AGENT_SERVER_MODE", "").strip().lower()
    if not raw:
        return False
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ServerActorSessionError(
        "ARTICLE_AGENT_SERVER_MODE must be a boolean value"
    )


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
    except (ValueError, TypeError) as exc:
        raise ServerActorSessionError("invalid actor session") from exc


@dataclass(frozen=True, slots=True)
class ServerActorSession:
    """Verified cookie claims before the database version check."""

    actor: ActorIdentity
    session_version: int
    issued_at: int
    expires_at: int


@dataclass(frozen=True)
class ServerActorSessionCodec:
    """Sign minimal Actor identity; roles are deliberately absent."""

    secret: bytes
    max_session_seconds: int = MAX_SERVER_SESSION_SECONDS

    def __post_init__(self) -> None:
        normalized = bytes(self.secret)
        if len(normalized) < 32:
            raise ServerActorSessionError(
                "server session secret must contain at least 32 bytes"
            )
        if self.max_session_seconds <= 0:
            raise ServerActorSessionError(
                "max_session_seconds must be greater than zero"
            )
        object.__setattr__(self, "secret", normalized)

    def create(
        self,
        actor: ActorIdentity,
        *,
        session_version: int = 1,
        now: int | None = None,
        max_age: int = DEFAULT_SERVER_SESSION_SECONDS,
    ) -> str:
        if max_age <= 0 or max_age > self.max_session_seconds:
            raise ServerActorSessionError("actor session lifetime is invalid")
        if (
            isinstance(session_version, bool)
            or not isinstance(session_version, int)
            or session_version <= 0
        ):
            raise ServerActorSessionError("actor session version is invalid")
        issued_at = int(time.time() if now is None else now)
        payload = {
            "v": _TOKEN_VERSION,
            "sv": session_version,
            "org": actor.organization_id,
            "sub": actor.user_id,
            "iat": issued_at,
            "exp": issued_at + max_age,
        }
        encoded_payload = _base64url_encode(
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )
        signature = _base64url_encode(
            hmac.new(
                self.secret,
                encoded_payload.encode("ascii"),
                hashlib.sha256,
            ).digest()
        )
        return f"{encoded_payload}.{signature}"

    def parse(
        self,
        token: str,
        *,
        now: int | None = None,
    ) -> ActorIdentity:
        return self.parse_session(token, now=now).actor

    def parse_session(
        self,
        token: str,
        *,
        now: int | None = None,
    ) -> ServerActorSession:
        try:
            encoded_payload, supplied_signature = token.split(".", 1)
        except (AttributeError, ValueError) as exc:
            raise ServerActorSessionError("invalid actor session") from exc

        expected_signature = _base64url_encode(
            hmac.new(
                self.secret,
                encoded_payload.encode("ascii"),
                hashlib.sha256,
            ).digest()
        )
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise ServerActorSessionError("invalid actor session")

        try:
            payload = json.loads(_base64url_decode(encoded_payload))
            if not isinstance(payload, dict):
                raise TypeError
            if set(payload) != {"v", "sv", "org", "sub", "iat", "exp"}:
                raise TypeError
            if type(payload["sv"]) is not int:
                raise TypeError
            version = int(payload["v"])
            session_version = int(payload["sv"])
            issued_at = int(payload["iat"])
            expires_at = int(payload["exp"])
            actor = ActorIdentity(
                organization_id=str(payload["org"]),
                user_id=str(payload["sub"]),
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ServerActorSessionError("invalid actor session") from exc

        current_time = int(time.time() if now is None else now)
        if (
            version != _TOKEN_VERSION
            or session_version <= 0
            or issued_at > current_time + 60
            or expires_at <= current_time
            or expires_at <= issued_at
            or expires_at - issued_at > self.max_session_seconds
        ):
            raise ServerActorSessionError("invalid actor session")
        return ServerActorSession(
            actor=actor,
            session_version=session_version,
            issued_at=issued_at,
            expires_at=expires_at,
        )


def load_server_actor_session_codec(
    environment: Mapping[str, str] | None = None,
) -> ServerActorSessionCodec:
    source = os.environ if environment is None else environment
    secret = source.get("ARTICLE_AGENT_SERVER_SESSION_SECRET", "")
    if not secret:
        raise ServerActorSessionError(
            "ARTICLE_AGENT_SERVER_SESSION_SECRET is required"
        )
    return ServerActorSessionCodec(secret.encode("utf-8"))


__all__ = [
    "DEFAULT_SERVER_SESSION_SECONDS",
    "MAX_SERVER_SESSION_SECONDS",
    "SERVER_AUTH_COOKIE_NAME",
    "ServerActorSession",
    "ServerActorSessionCodec",
    "ServerActorSessionError",
    "load_server_actor_session_codec",
    "server_mode_enabled",
]
