from __future__ import annotations

from typing import Protocol

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from server_schema import organizations, workspace_users
from services.access_control import ActorIdentity
from services.audit_log import (
    AuditEvent,
    AuditEventWriter,
    PostgresAuditEventWriter,
)
from services.server_auth import ServerActorSession


class ActorSessionVersionReader(Protocol):
    """Validate signed claims against the current database session epoch."""

    def is_current(self, session: ServerActorSession) -> bool: ...


class PostgresActorSessionRepository:
    """Treat active Organization/User rows as the session validity source."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def is_current(self, session: ServerActorSession) -> bool:
        with self._engine.connect() as connection:
            version = connection.execute(
                sa.select(workspace_users.c.session_version)
                .select_from(
                    workspace_users.join(
                        organizations,
                        organizations.c.organization_id
                        == workspace_users.c.organization_id,
                    )
                )
                .where(
                    workspace_users.c.organization_id
                    == session.actor.organization_id,
                    workspace_users.c.user_id == session.actor.user_id,
                    workspace_users.c.status == "active",
                    organizations.c.status == "active",
                )
            ).scalar_one_or_none()
        return version is not None and int(version) == session.session_version


class ActorSessionRevocationDenied(PermissionError):
    """Generic denial for cross-organization or unauthorized revocation."""


class ActorSessionRevocationError(RuntimeError):
    """A revocation could not be committed without exposing dependencies."""


class PostgresActorSessionRevocationService:
    """Increment a user's session epoch with org-admin Audit atomicity."""

    def __init__(
        self,
        engine: Engine,
        *,
        audit: AuditEventWriter | None = None,
    ) -> None:
        self._engine = engine
        self._audit = audit or PostgresAuditEventWriter()

    @staticmethod
    def _require_org_admin(
        connection: Connection,
        actor: ActorIdentity,
    ) -> None:
        actor_user = workspace_users.alias("session_revocation_actor")
        allowed = connection.execute(
            sa.select(actor_user.c.user_id)
            .select_from(
                actor_user.join(
                    organizations,
                    organizations.c.organization_id
                    == actor_user.c.organization_id,
                )
            )
            .where(
                actor_user.c.organization_id == actor.organization_id,
                actor_user.c.user_id == actor.user_id,
                actor_user.c.status == "active",
                actor_user.c.organization_role == "org_admin",
                organizations.c.status == "active",
            )
            .with_for_update(of=(actor_user, organizations))
        ).scalar_one_or_none()
        if allowed is None:
            raise ActorSessionRevocationDenied(
                "actor session revocation denied"
            )

    def revoke_all(
        self,
        *,
        actor: ActorIdentity,
        user_id: str,
        event_id: str,
    ) -> int:
        with self._engine.begin() as connection:
            return self.revoke_all_in_transaction(
                connection,
                actor=actor,
                user_id=user_id,
                event_id=event_id,
            )

    def revoke_all_in_transaction(
        self,
        connection: Connection,
        *,
        actor: ActorIdentity,
        user_id: str,
        event_id: str,
    ) -> int:
        if not connection.in_transaction():
            raise ValueError(
                "actor session revocation requires a business transaction"
            )
        normalized_user_id = user_id.strip()
        if not normalized_user_id:
            raise ValueError("user_id is required")
        normalized_event_id = event_id.strip()
        if not normalized_event_id:
            raise ValueError("event_id is required")

        self._require_org_admin(connection, actor)
        current_version = connection.execute(
            sa.select(workspace_users.c.session_version)
            .where(
                workspace_users.c.organization_id
                == actor.organization_id,
                workspace_users.c.user_id == normalized_user_id,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if current_version is None:
            raise ActorSessionRevocationDenied(
                "actor session revocation denied"
            )

        next_version = int(current_version) + 1
        connection.execute(
            workspace_users.update()
            .where(
                workspace_users.c.organization_id
                == actor.organization_id,
                workspace_users.c.user_id == normalized_user_id,
                workspace_users.c.session_version == int(current_version),
            )
            .values(
                session_version=next_version,
                updated_at=sa.func.now(),
            )
        )
        try:
            self._audit.append(
                connection,
                AuditEvent(
                    organization_id=actor.organization_id,
                    event_id=normalized_event_id,
                    actor_user_id=actor.user_id,
                    action="workspace_user.sessions.revoked",
                    target_type="workspace_user",
                    target_id=normalized_user_id,
                    details={"session_version": next_version},
                ),
            )
        except Exception as exc:
            raise ActorSessionRevocationError(
                "actor sessions could not be revoked"
            ) from exc
        return next_version


__all__ = [
    "ActorSessionRevocationDenied",
    "ActorSessionRevocationError",
    "ActorSessionVersionReader",
    "PostgresActorSessionRepository",
    "PostgresActorSessionRevocationService",
]
