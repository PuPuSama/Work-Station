from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from server_schema import (
    external_identities,
    organizations,
    workspace_users,
)
from services.access_control import ActorIdentity
from services.server_auth import ServerActorSessionCodec


def _issuer(value: str) -> str:
    normalized = value.strip().rstrip("/")
    parsed = urlsplit(normalized)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("issuer must be an absolute HTTPS URL") from exc
    loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if (
        parsed.scheme not in ({"https"} if not loopback else {"http", "https"})
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port is not None
        and not 0 < port < 65536
    ):
        raise ValueError("issuer must be an absolute HTTPS URL")
    return normalized


def _subject(value: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 512:
        raise ValueError("subject must contain between 1 and 512 characters")
    return normalized


@dataclass(frozen=True, slots=True)
class VerifiedExternalIdentity:
    """Issuer/subject pair produced only after upstream token verification."""

    issuer: str
    subject: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "issuer", _issuer(self.issuer))
        object.__setattr__(self, "subject", _subject(self.subject))


@dataclass(frozen=True, slots=True)
class ResolvedExternalActor:
    """Active local Actor plus the session epoch bound into a new cookie."""

    actor: ActorIdentity
    session_version: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.session_version, bool)
            or not isinstance(self.session_version, int)
            or self.session_version <= 0
        ):
            raise ValueError("session_version must be greater than zero")


class ExternalIdentityRepository(Protocol):
    def resolve(
        self,
        identity: VerifiedExternalIdentity,
    ) -> ResolvedExternalActor | None: ...


class PostgresExternalIdentityRepository:
    """Resolve active mappings without accepting role claims from an IdP."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def resolve(
        self,
        identity: VerifiedExternalIdentity,
    ) -> ResolvedExternalActor | None:
        with self._engine.connect() as connection:
            row = connection.execute(
                sa.select(
                    external_identities.c.organization_id,
                    external_identities.c.user_id,
                    workspace_users.c.session_version,
                )
                .select_from(
                    external_identities.join(
                        workspace_users,
                        sa.and_(
                            workspace_users.c.organization_id
                            == external_identities.c.organization_id,
                            workspace_users.c.user_id
                            == external_identities.c.user_id,
                        ),
                    ).join(
                        organizations,
                        organizations.c.organization_id
                        == external_identities.c.organization_id,
                    )
                )
                .where(
                    external_identities.c.issuer == identity.issuer,
                    external_identities.c.subject == identity.subject,
                    external_identities.c.status == "active",
                    workspace_users.c.status == "active",
                    organizations.c.status == "active",
                )
            ).mappings().one_or_none()
        if row is None:
            return None
        return ResolvedExternalActor(
            actor=ActorIdentity(
                organization_id=str(row["organization_id"]),
                user_id=str(row["user_id"]),
            ),
            session_version=int(row["session_version"]),
        )


class ExternalIdentityNotAuthorized(PermissionError):
    """A verified identity has no active local workspace mapping."""


class ExternalActorSessionService:
    """Exchange a verified external identity for the minimal Actor cookie."""

    def __init__(
        self,
        *,
        identities: ExternalIdentityRepository,
        codec: ServerActorSessionCodec,
    ) -> None:
        self._identities = identities
        self._codec = codec

    def create_session(
        self,
        identity: VerifiedExternalIdentity,
        *,
        max_age: int,
    ) -> str:
        resolved = self._identities.resolve(identity)
        if resolved is None:
            raise ExternalIdentityNotAuthorized(
                "external identity is not authorized"
            )
        return self._codec.create(
            resolved.actor,
            session_version=resolved.session_version,
            max_age=max_age,
        )


__all__ = [
    "ExternalActorSessionService",
    "ExternalIdentityNotAuthorized",
    "ExternalIdentityRepository",
    "PostgresExternalIdentityRepository",
    "ResolvedExternalActor",
    "VerifiedExternalIdentity",
]
