"""Loopback-only development wrapper for the Server application.

This module is intentionally outside ``backend`` so the production image does
not contain a local identity entry point.  The launcher must provide the
complete, disposable local environment before importing this module.
"""

from __future__ import annotations

import os
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import Response

try:
    from services.server_request_security import ServerRequestUnauthenticated
except ModuleNotFoundError:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
    from services.server_request_security import ServerRequestUnauthenticated


EXPECTED_DATABASE_URL = (
    "postgresql+psycopg://article_debug:article_debug_local@"
    "127.0.0.1:55438/article_agent_debug"
)
EXPECTED_OBJECT_ENDPOINT = "http://127.0.0.1:59018"
EXPECTED_OBJECT_BUCKET = "article-agent-debug"
EXPECTED_CONFIG = "config.local-debug.yaml"
EXPECTED_LOCAL_FLAG = "1"
EXPECTED_INTERNAL_ENDPOINT = EXPECTED_OBJECT_ENDPOINT
DEBUG_COOKIE_NAME = "article_agent_local_debug"
DEBUG_ORG = "local-debug-org"
DEBUG_USER = "local-debug-user"
DEBUG_SESSION_VERSION = 1


class LocalDebugConfigurationError(RuntimeError):
    """The independent local debug environment is not safe to start."""


def _require(environment: Mapping[str, str], name: str, expected: str) -> None:
    if environment.get(name, "").strip() != expected:
        raise LocalDebugConfigurationError(
            f"{name} must be the dedicated local-debug value"
        )


def validate_local_debug_environment(
    environment: Mapping[str, str] | None = None,
) -> None:
    """Fail closed before importing the real application.

    Values are compared without including them in exceptions, so malformed
    database URLs and secrets cannot be echoed by this entry point.
    """

    source = os.environ if environment is None else environment
    _require(source, "ARTICLE_AGENT_DATABASE_URL", EXPECTED_DATABASE_URL)
    try:
        from sqlalchemy.engine import make_url

        database = make_url(source["ARTICLE_AGENT_DATABASE_URL"])
    except Exception as exc:  # pragma: no cover - make_url versions vary
        raise LocalDebugConfigurationError(
            "ARTICLE_AGENT_DATABASE_URL is not a valid local debug URL"
        ) from exc
    if (
        database.drivername != "postgresql+psycopg"
        or database.username != "article_debug"
        or database.password != "article_debug_local"
        or database.host != "127.0.0.1"
        or database.port != 55438
        or database.database != "article_agent_debug"
        or database.query
    ):
        raise LocalDebugConfigurationError(
            "ARTICLE_AGENT_DATABASE_URL is not the dedicated local database"
        )
    _require(source, "ARTICLE_AGENT_OBJECT_STORE_ENDPOINT", EXPECTED_OBJECT_ENDPOINT)
    try:
        object_url = urlsplit(source["ARTICLE_AGENT_OBJECT_STORE_ENDPOINT"])
        object_port = object_url.port
    except ValueError as exc:
        raise LocalDebugConfigurationError(
            "ARTICLE_AGENT_OBJECT_STORE_ENDPOINT is not local debug storage"
        ) from exc
    if (
        object_url.scheme != "http"
        or object_url.hostname != "127.0.0.1"
        or object_port != 59018
        or object_url.path not in ("", "/")
        or object_url.query
        or object_url.fragment
        or object_url.username
        or object_url.password
    ):
        raise LocalDebugConfigurationError(
            "ARTICLE_AGENT_OBJECT_STORE_ENDPOINT is not local debug storage"
        )
    _require(source, "ARTICLE_AGENT_OBJECT_STORE_BUCKET", EXPECTED_OBJECT_BUCKET)
    repository_root = Path(__file__).resolve().parents[1]
    configured_root = source.get("ARTICLE_AGENT_ROOT", "").strip()
    if configured_root and Path(configured_root).expanduser().resolve() != repository_root:
        raise LocalDebugConfigurationError(
            "ARTICLE_AGENT_ROOT must be the repository root"
        )
    config_path = Path(source.get("ARTICLE_AGENT_CONFIG", "").strip()).expanduser()
    try:
        config_path = config_path.resolve(strict=True)
    except OSError as exc:
        raise LocalDebugConfigurationError(
            "ARTICLE_AGENT_CONFIG must select the local-debug configuration"
        ) from exc
    if config_path != (repository_root / EXPECTED_CONFIG).resolve() or not config_path.is_file():
        raise LocalDebugConfigurationError(
            "ARTICLE_AGENT_CONFIG must select the local-debug configuration"
        )
    _require(source, "ARTICLE_AGENT_LOCAL_DEBUG", EXPECTED_LOCAL_FLAG)
    internal_endpoint = source.get("ARTICLE_AGENT_OBJECT_STORE_INTERNAL_ENDPOINT", "").strip()
    if internal_endpoint and internal_endpoint != EXPECTED_INTERNAL_ENDPOINT:
        raise LocalDebugConfigurationError(
            "ARTICLE_AGENT_OBJECT_STORE_INTERNAL_ENDPOINT is not local debug storage"
        )

    env_file = source.get("ARTICLE_AGENT_ENV_FILE", "").strip()
    if not env_file:
        raise LocalDebugConfigurationError(
            "ARTICLE_AGENT_ENV_FILE must point to an empty local file"
        )
    env_path = Path(env_file).expanduser()
    if not env_path.is_file():
        raise LocalDebugConfigurationError(
            "ARTICLE_AGENT_ENV_FILE must point to an empty local file"
        )
    try:
        env_lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise LocalDebugConfigurationError(
            "ARTICLE_AGENT_ENV_FILE must point to an empty local file"
        ) from exc
    if any(line.strip() and not line.lstrip().startswith("#") for line in env_lines):
        raise LocalDebugConfigurationError(
            "ARTICLE_AGENT_ENV_FILE must point to an empty local file"
        )


def validate_environment(environment: Mapping[str, str] | None = None) -> None:
    """Launcher-facing alias; kept separate from the public app entry point."""

    validate_local_debug_environment(environment)


def _loopback(host: str | None) -> bool:
    return host in {"127.0.0.1", "localhost", "::1"}


def _allowed_host(value: str, *, ports: set[int]) -> bool:
    try:
        parsed = urlsplit(f"http://{value}")
        port = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.hostname
        and _loopback(parsed.hostname)
        and port in ports
        and not parsed.username
        and not parsed.password
        and parsed.path in ("", "/")
        and not parsed.query
        and not parsed.fragment
    )


def request_boundary_allowed(scope: Mapping[str, object]) -> bool:
    """Check client, Host, Origin and forwarded Host before touching auth."""

    client = scope.get("client")
    if not isinstance(client, (tuple, list)) or not client or not _loopback(str(client[0])):
        return False
    headers = Headers(raw=scope.get("headers", []))
    host = headers.get("host", "")
    if not _allowed_host(host, ports={8108}):
        return False
    forwarded = headers.get("x-forwarded-host")
    if forwarded and (
        "," in forwarded
        or not _allowed_host(forwarded.strip(), ports={3108, 8108})
    ):
        return False
    origin = headers.get("origin")
    if origin:
        try:
            parsed = urlsplit(origin)
            origin_port = parsed.port
        except ValueError:
            return False
        if (
            parsed.scheme != "http"
            or not _loopback(parsed.hostname)
            or parsed.username is not None
            or parsed.password is not None
            or origin_port not in {3108, 8108}
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            return False
    return True


def _remove_internal_cookie(scope: dict) -> None:
    headers = SimpleCookie()
    raw = Headers(raw=scope.get("headers", [])).get("cookie", "")
    headers.load(raw)
    headers.pop("article_agent_actor_session", None)
    cookie_value = "; ".join(
        f"{key}={morsel.value}" for key, morsel in headers.items()
    ).encode("latin-1")
    raw_headers = [
        (key, value)
        for key, value in scope.get("headers", [])
        if key.lower() != b"cookie"
    ]
    raw_headers.append((b"cookie", cookie_value))
    scope["headers"] = raw_headers


def _replace_internal_cookie(scope: dict, token: str) -> None:
    _remove_internal_cookie(scope)
    headers = Headers(raw=scope.get("headers", []))
    raw = headers.get("cookie", "")
    cookies = SimpleCookie()
    cookies.load(raw)
    cookies["article_agent_actor_session"] = token
    cookie_value = "; ".join(
        f"{key}={morsel.value}" for key, morsel in cookies.items()
    ).encode("latin-1")
    raw_headers = [
        (key, value)
        for key, value in scope.get("headers", [])
        if key.lower() != b"cookie"
    ]
    raw_headers.append((b"cookie", cookie_value))
    scope["headers"] = raw_headers


class LocalDebugAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, codec, security) -> None:
        super().__init__(app)
        self._codec = codec
        self._security = security

    async def dispatch(self, request: Request, call_next):
        if not request_boundary_allowed(request.scope):
            return Response("Local debug server accepts loopback requests only.", status_code=403)
        has_debug_cookie = DEBUG_COOKIE_NAME in request.cookies
        debug_token = request.cookies.get(DEBUG_COOKIE_NAME)
        _remove_internal_cookie(request.scope)
        issued = False
        if debug_token:
            try:
                await run_in_threadpool(self._security.authenticate, debug_token)
            except ServerRequestUnauthenticated:
                debug_token = None
            else:
                _replace_internal_cookie(request.scope, debug_token)
        elif not has_debug_cookie and request.method == "GET" and request.url.path == "/api/auth/status":
            token = self._codec.create(
                self._security.debug_actor,
                session_version=DEBUG_SESSION_VERSION,
            )
            try:
                await run_in_threadpool(self._security.authenticate, token)
            except ServerRequestUnauthenticated:
                return Response("Local debug identity is unavailable.", status_code=401)
            debug_token = token
            issued = True
            _replace_internal_cookie(request.scope, token)

        # Request caches headers/cookies on first access; refresh them after
        # changing the ASGI scope so downstream handlers see the mapped value.
        for attribute in ("_headers", "_cookies"):
            if hasattr(request, attribute):
                delattr(request, attribute)

        response = await call_next(request)
        # Logout in the real app must not clear another localhost service's cookie.
        response.raw_headers = [
            (name, value)
            for name, value in response.raw_headers
            if not (
                name.lower() == b"set-cookie"
                and value.split(b"=", 1)[0].strip()
                in {b"article_agent_actor_session", b"article_agent_session"}
            )
        ]
        if issued and debug_token:
            response.set_cookie(
                DEBUG_COOKIE_NAME,
                debug_token,
                httponly=True,
                samesite="lax",
                secure=False,
                max_age=12 * 60 * 60,
                path="/",
            )
        if request.url.path == "/api/auth/logout":
            response.delete_cookie(DEBUG_COOKIE_NAME, path="/")
        return response


class _DebugSecurityAdapter:
    """Small adapter keeping the fixed actor out of client input."""

    def __init__(self, app, actor) -> None:
        self._app = app
        self.debug_actor = actor

    def authenticate(self, token: str):
        security = getattr(self._app.state, "server_request_security", None)
        if security is None:
            raise LocalDebugConfigurationError(
                "the real application has not initialized server security"
            )
        return security.authenticate(token)


def create_app():
    validate_local_debug_environment()
    import sys

    backend = str(Path(__file__).resolve().parents[1] / "backend")
    if backend not in sys.path:
        sys.path.insert(0, backend)
    from app import app
    from services.access_control import ActorIdentity
    from services.server_auth import load_server_actor_session_codec

    adapter = _DebugSecurityAdapter(
        app,
        ActorIdentity(DEBUG_ORG, DEBUG_USER),
    )
    app.add_middleware(
        LocalDebugAuthMiddleware,
        codec=load_server_actor_session_codec(),
        security=adapter,
    )
    return app


__all__ = [
    "DEBUG_COOKIE_NAME",
    "LocalDebugAuthMiddleware",
    "LocalDebugConfigurationError",
    "create_app",
    "request_boundary_allowed",
    "validate_environment",
    "validate_local_debug_environment",
]
