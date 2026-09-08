from __future__ import annotations

import hmac
import os
from dataclasses import dataclass
from typing import Mapping

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from server_schema import organizations, workspace_users
from services.access_control import ActorIdentity
from services.server_auth import (
    MAX_SERVER_SESSION_SECONDS,
    ServerActorSessionCodec,
)


class LocalPasswordLoginError(RuntimeError):
    """Base error for local password authentication."""


class LocalPasswordLoginUnavailable(LocalPasswordLoginError):
    """The local login account is not configured or no longer valid."""


class LocalPasswordLoginFailed(LocalPasswordLoginError):
    """Credentials or the configured workspace actor are invalid."""


def _configured_value(
    source: Mapping[str, str],
    *names: str,
) -> str:
    for name in names:
        value = str(source.get(name, "") or "").strip()
        if value:
            return value
    return ""


@dataclass(frozen=True, slots=True)
class LocalPasswordLoginSettings:
    """Deployment-scoped login credentials and their workspace Actor."""

    username: str
    password: str
    organization_id: str = ""
    user_id: str = ""
    session_seconds: int = 12 * 60 * 60

    def __post_init__(self) -> None:
        for name in ("username", "password"):
            if not str(getattr(self, name) or "").strip():
                raise ValueError(f"{name} must not be blank")
        for name in ("organization_id", "user_id"):
            if getattr(self, name, "") is None:
                raise ValueError(f"{name} must not be null")
        if not 1 <= self.session_seconds <= MAX_SERVER_SESSION_SECONDS:
            raise ValueError("session_seconds is outside the allowed range")

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> "LocalPasswordLoginSettings | None":
        source = os.environ if environment is None else environment
        username = _configured_value(
            source,
            "ARTICLE_AGENT_LOGIN_USERNAME",
            "APP_USERNAME",
        )
        password = _configured_value(
            source,
            "ARTICLE_AGENT_LOGIN_PASSWORD",
            "APP_PASSWORD",
        )
        organization_id = _configured_value(
            source,
            "ARTICLE_AGENT_LOGIN_ORGANIZATION_ID",
        )
        user_id = _configured_value(
            source,
            "ARTICLE_AGENT_LOGIN_USER_ID",
        )
        if not all((username, password)):
            return None
        raw_session_seconds = _configured_value(
            source,
            "ARTICLE_AGENT_LOGIN_SESSION_SECONDS",
        )
        session_seconds = 12 * 60 * 60
        if raw_session_seconds:
            try:
                session_seconds = int(raw_session_seconds)
            except ValueError as exc:
                raise ValueError(
                    "ARTICLE_AGENT_LOGIN_SESSION_SECONDS must be an integer"
                ) from exc
        return cls(
            username=username,
            password=password,
            organization_id=organization_id,
            user_id=user_id,
            session_seconds=session_seconds,
        )


@dataclass(frozen=True, slots=True)
class LocalPasswordLoginResult:
    actor: ActorIdentity
    actor_session: str


class LocalPasswordLoginService:
    """Authenticate one deployment account and issue the normal Actor cookie."""

    def __init__(
        self,
        engine: Engine,
        *,
        codec: ServerActorSessionCodec,
        settings: LocalPasswordLoginSettings,
    ) -> None:
        self._engine = engine
        self._codec = codec
        self.settings = settings

    def login(
        self,
        username: str,
        password: str,
    ) -> LocalPasswordLoginResult:
        username_match = hmac.compare_digest(
            str(username or "").strip(),
            self.settings.username,
        )
        password_match = hmac.compare_digest(
            str(password or ""),
            self.settings.password,
        )
        if not username_match or not password_match:
            raise LocalPasswordLoginFailed("invalid username or password")

        with self._engine.connect() as connection:
            statement = (
                sa.select(
                    workspace_users.c.organization_id,
                    workspace_users.c.user_id,
                    workspace_users.c.session_version,
                )
                .select_from(
                    workspace_users.join(
                        organizations,
                        organizations.c.organization_id
                        == workspace_users.c.organization_id,
                    )
                )
                .where(
                    workspace_users.c.status == "active",
                    organizations.c.status == "active",
                )
            )
            configured_user_id = (
                self.settings.user_id.strip() or self.settings.username
            )
            statement = statement.where(
                workspace_users.c.user_id == configured_user_id,
            )
            if self.settings.organization_id.strip():
                statement = statement.where(
                    workspace_users.c.organization_id
                    == self.settings.organization_id.strip(),
                )
            rows = connection.execute(statement).mappings().all()
        if len(rows) != 1:
            raise LocalPasswordLoginUnavailable(
                "configured workspace actor is unavailable or ambiguous"
            )
        row = rows[0]
        actor = ActorIdentity(
            organization_id=str(row["organization_id"]),
            user_id=str(row["user_id"]),
        )
        return LocalPasswordLoginResult(
            actor=actor,
            actor_session=self._codec.create(
                actor,
                session_version=int(row["session_version"]),
                max_age=self.settings.session_seconds,
            ),
        )


__all__ = [
    "LocalPasswordLoginError",
    "LocalPasswordLoginFailed",
    "LocalPasswordLoginResult",
    "LocalPasswordLoginService",
    "LocalPasswordLoginSettings",
    "LocalPasswordLoginUnavailable",
]
