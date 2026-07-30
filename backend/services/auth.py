from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time


AUTH_COOKIE_NAME = "article_agent_session"
DEFAULT_SESSION_SECONDS = 12 * 60 * 60


def configured_password() -> str:
    return os.environ.get("APP_PASSWORD", "").strip()


def authentication_enabled() -> bool:
    return bool(configured_password())


def _session_secret() -> bytes:
    secret = os.environ.get("APP_SESSION_SECRET", "").strip() or configured_password()
    return secret.encode("utf-8")


def _signature(expires_at: int) -> str:
    digest = hmac.new(
        _session_secret(),
        str(expires_at).encode("ascii"),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def create_session_token(
    *,
    now: int | None = None,
    max_age: int = DEFAULT_SESSION_SECONDS,
) -> str:
    expires_at = int(time.time() if now is None else now) + max_age
    return f"{expires_at}.{_signature(expires_at)}"


def valid_session_token(token: str, *, now: int | None = None) -> bool:
    if not authentication_enabled() or not token:
        return False
    try:
        expires_raw, signature = token.split(".", 1)
        expires_at = int(expires_raw)
    except (TypeError, ValueError):
        return False
    current_time = int(time.time() if now is None else now)
    if expires_at <= current_time:
        return False
    return hmac.compare_digest(signature, _signature(expires_at))


def password_matches(candidate: str) -> bool:
    password = configured_password()
    return bool(password) and hmac.compare_digest(
        str(candidate or "").encode("utf-8"),
        password.encode("utf-8"),
    )
