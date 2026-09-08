from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from typing import Mapping

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from server_schema import organizations, workspace_users
from services.access_control import ActorIdentity
from services.audit_log import AuditEvent, AuditEventWriter, PostgresAuditEventWriter
from services.server_auth import (
    MAX_SERVER_SESSION_SECONDS,
    ServerActorSessionCodec,
)


PASSWORD_HASH_ALGORITHM = "pbkdf2_sha256"
PASSWORD_HASH_ITERATIONS = 240_000
PASSWORD_HASH_SALT_BYTES = 16
PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 256


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


def _password_text(value: str, field_name: str) -> str:
    normalized = str(value or "")
    if not normalized:
        raise ValueError(f"{field_name} must not be blank")
    if len(normalized) < PASSWORD_MIN_LENGTH:
        raise ValueError(
            f"{field_name} must be at least {PASSWORD_MIN_LENGTH} characters"
        )
    if len(normalized) > PASSWORD_MAX_LENGTH:
        raise ValueError(
            f"{field_name} must be at most {PASSWORD_MAX_LENGTH} characters"
        )
    return normalized


def hash_local_password(password: str) -> str:
    """Create a versioned salted password hash for workspace users."""

    normalized = _password_text(password, "password")
    salt = secrets.token_bytes(PASSWORD_HASH_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        normalized.encode("utf-8"),
        salt,
        PASSWORD_HASH_ITERATIONS,
    )
    encode = lambda value: base64.urlsafe_b64encode(value).decode("ascii")
    return (
        f"{PASSWORD_HASH_ALGORITHM}${PASSWORD_HASH_ITERATIONS}$"
        f"{encode(salt)}${encode(digest)}"
    )


def verify_local_password(password: str, encoded_hash: str) -> bool:
    """Verify a password hash without exposing malformed hash details."""

    try:
        algorithm, raw_iterations, encoded_salt, encoded_digest = (
            str(encoded_hash or "").split("$", 3)
        )
        if algorithm != PASSWORD_HASH_ALGORITHM:
            return False
        iterations = int(raw_iterations)
        if not 10_000 <= iterations <= 2_000_000:
            return False
        salt = base64.urlsafe_b64decode(encoded_salt.encode("ascii"))
        expected = base64.urlsafe_b64decode(encoded_digest.encode("ascii"))
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            str(password or "").encode("utf-8"),
            salt,
            iterations,
        )
    except (ValueError, TypeError, UnicodeError):
        return False
    return hmac.compare_digest(actual, expected)


@dataclass(frozen=True, slots=True)
class LocalPasswordLoginSettings:
    """Deployment-scoped login credentials and their workspace Actor."""

    username: str
    password: str
    organization_id: str = ""
    user_id: str = ""
    session_seconds: int = 12 * 60 * 60

    def __post_init__(self) -> None:
        username_configured = bool(str(self.username or "").strip())
        password_configured = bool(str(self.password or "").strip())
        if username_configured != password_configured:
            raise ValueError(
                "username and password must both be configured or both be blank"
            )
        for name in ("organization_id", "user_id"):
            if getattr(self, name, "") is None:
                raise ValueError(f"{name} must not be null")
        if not 1 <= self.session_seconds <= MAX_SERVER_SESSION_SECONDS:
            raise ValueError("session_seconds is outside the allowed range")

    @classmethod
    def database_only(cls) -> "LocalPasswordLoginSettings":
        """Use only database password hashes after bootstrap is complete."""

        return cls(username="", password="")

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
    """Authenticate workspace users and issue the normal Actor cookie.

    Environment credentials are retained as a one-time bootstrap path for
    legacy deployments. Once a user has a database password hash, that hash
    is authoritative and the environment password is ignored for login.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        codec: ServerActorSessionCodec,
        settings: LocalPasswordLoginSettings,
        audit: AuditEventWriter | None = None,
    ) -> None:
        self._engine = engine
        self._codec = codec
        self.settings = settings
        self._audit = audit or PostgresAuditEventWriter()

    def has_available_accounts(self) -> bool:
        """Return whether login has a configured bootstrap or DB account."""

        if self.settings.username.strip() and self.settings.password:
            return True
        try:
            with self._engine.connect() as connection:
                result = connection.execute(
                    sa.select(workspace_users.c.user_id)
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
                        workspace_users.c.password_hash.is_not(None),
                    )
                    .limit(1)
                )
                return result.first() is not None
        except SQLAlchemyError:
            return False

    def _candidate_user_id(self, username: str) -> str:
        configured_username = self.settings.username.strip()
        if configured_username and hmac.compare_digest(
            username,
            configured_username,
        ):
            return self.settings.user_id.strip() or username
        return username

    def _select_actor(self, *, username: str, connection):
        statement = (
            sa.select(
                workspace_users.c.organization_id,
                workspace_users.c.user_id,
                workspace_users.c.session_version,
                workspace_users.c.password_hash,
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
                workspace_users.c.user_id == self._candidate_user_id(username),
            )
        )
        if self.settings.organization_id.strip():
            statement = statement.where(
                workspace_users.c.organization_id
                == self.settings.organization_id.strip(),
            )
        return connection.execute(statement).mappings().all()

    def _bootstrap_password_matches(
        self,
        *,
        username: str,
        password: str,
        actor: ActorIdentity,
    ) -> bool:
        configured_username = self.settings.username.strip()
        configured_user_id = self.settings.user_id.strip() or configured_username
        configured_organization = self.settings.organization_id.strip()
        return bool(
            configured_username
            and hmac.compare_digest(username, configured_username)
            and hmac.compare_digest(password, self.settings.password)
            and actor.user_id == configured_user_id
            and (
                not configured_organization
                or actor.organization_id == configured_organization
            )
        )

    def _create_session(
        self,
        *,
        organization_id: str,
        user_id: str,
        session_version: int,
    ) -> LocalPasswordLoginResult:
        actor = ActorIdentity(
            organization_id=str(organization_id),
            user_id=str(user_id),
        )
        return LocalPasswordLoginResult(
            actor=actor,
            actor_session=self._codec.create(
                actor,
                session_version=int(session_version),
                max_age=self.settings.session_seconds,
            ),
        )

    def login(
        self,
        username: str,
        password: str,
    ) -> LocalPasswordLoginResult:
        normalized_username = str(username or "").strip()
        if not normalized_username:
            raise LocalPasswordLoginFailed("invalid username or password")
        try:
            with self._engine.connect() as connection:
                rows = self._select_actor(
                    username=normalized_username,
                    connection=connection,
                )
        except SQLAlchemyError as exc:
            raise LocalPasswordLoginUnavailable(
                "workspace login is unavailable"
            ) from exc
        if not rows:
            if hmac.compare_digest(
                normalized_username,
                self.settings.username.strip(),
            ):
                raise LocalPasswordLoginUnavailable(
                    "configured workspace actor is unavailable"
                )
            raise LocalPasswordLoginFailed("invalid username or password")
        if len(rows) != 1:
            raise LocalPasswordLoginUnavailable(
                "workspace login account is ambiguous"
            )

        row = rows[0]
        actor = ActorIdentity(
            organization_id=str(row["organization_id"]),
            user_id=str(row["user_id"]),
        )
        stored_hash = str(row.get("password_hash") or "").strip()
        if stored_hash:
            if not verify_local_password(password, stored_hash):
                raise LocalPasswordLoginFailed("invalid username or password")
        elif not self._bootstrap_password_matches(
            username=normalized_username,
            password=str(password or ""),
            actor=actor,
        ):
            raise LocalPasswordLoginFailed("invalid username or password")
        elif "password_hash" in row:
            # Bootstrap legacy deployments on the first successful login. The
            # compare is repeated under a row lock so concurrent requests do
            # not replace a password that another request has already set.
            try:
                with self._engine.begin() as connection:
                    locked = connection.execute(
                        sa.select(workspace_users.c.password_hash)
                        .where(
                            workspace_users.c.organization_id
                            == actor.organization_id,
                            workspace_users.c.user_id == actor.user_id,
                            workspace_users.c.status == "active",
                        )
                        .with_for_update()
                    ).mappings().one_or_none()
                    if locked is None:
                        raise LocalPasswordLoginUnavailable(
                            "workspace login account is unavailable"
                        )
                    if not str(locked.get("password_hash") or "").strip():
                        connection.execute(
                            workspace_users.update()
                            .where(
                                workspace_users.c.organization_id
                                == actor.organization_id,
                                workspace_users.c.user_id == actor.user_id,
                                workspace_users.c.password_hash.is_(None),
                            )
                            .values(
                                password_hash=hash_local_password(password),
                                updated_at=sa.func.now(),
                            )
                        )
            except LocalPasswordLoginUnavailable:
                raise
            except SQLAlchemyError as exc:
                raise LocalPasswordLoginUnavailable(
                    "workspace login is unavailable"
                ) from exc
        return self._create_session(
            organization_id=actor.organization_id,
            user_id=actor.user_id,
            session_version=int(row["session_version"]),
        )

    def change_password(
        self,
        *,
        actor: ActorIdentity,
        current_password: str,
        new_password: str,
        event_id: str,
    ) -> LocalPasswordLoginResult:
        """Change the current user's password and rotate its sessions."""

        normalized_new_password = _password_text(new_password, "new_password")
        normalized_event_id = str(event_id or "").strip()
        if not normalized_event_id:
            raise ValueError("event_id must not be blank")
        try:
            with self._engine.begin() as connection:
                row = connection.execute(
                    sa.select(
                        workspace_users.c.password_hash,
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
                        workspace_users.c.organization_id
                        == actor.organization_id,
                        workspace_users.c.user_id == actor.user_id,
                        workspace_users.c.status == "active",
                        organizations.c.status == "active",
                    )
                    .with_for_update()
                ).mappings().one_or_none()
                if row is None:
                    raise LocalPasswordLoginUnavailable(
                        "workspace user is unavailable"
                    )
                stored_hash = str(row.get("password_hash") or "").strip()
                if stored_hash:
                    current_matches = verify_local_password(
                        current_password,
                        stored_hash,
                    )
                else:
                    current_matches = self._bootstrap_password_matches(
                        username=actor.user_id,
                        password=str(current_password or ""),
                        actor=actor,
                    )
                if not current_matches:
                    raise LocalPasswordLoginFailed("current password is invalid")
                next_version = int(row["session_version"]) + 1
                connection.execute(
                    workspace_users.update()
                    .where(
                        workspace_users.c.organization_id
                        == actor.organization_id,
                        workspace_users.c.user_id == actor.user_id,
                        workspace_users.c.session_version
                        == int(row["session_version"]),
                    )
                    .values(
                        password_hash=hash_local_password(normalized_new_password),
                        session_version=next_version,
                        updated_at=sa.func.now(),
                    )
                )
                self._audit.append(
                    connection,
                    AuditEvent(
                        organization_id=actor.organization_id,
                        event_id=normalized_event_id,
                        actor_user_id=actor.user_id,
                        action="workspace_user.password_changed",
                        target_type="workspace_user",
                        target_id=actor.user_id,
                        details={"session_version": next_version},
                    ),
                )
        except (LocalPasswordLoginFailed, LocalPasswordLoginUnavailable, ValueError):
            raise
        except SQLAlchemyError as exc:
            raise LocalPasswordLoginUnavailable(
                "workspace password change is unavailable"
            ) from exc
        return self._create_session(
            organization_id=actor.organization_id,
            user_id=actor.user_id,
            session_version=next_version,
        )


__all__ = [
    "LocalPasswordLoginError",
    "LocalPasswordLoginFailed",
    "LocalPasswordLoginResult",
    "LocalPasswordLoginService",
    "LocalPasswordLoginSettings",
    "LocalPasswordLoginUnavailable",
    "PASSWORD_MAX_LENGTH",
    "PASSWORD_MIN_LENGTH",
    "hash_local_password",
    "verify_local_password",
]
