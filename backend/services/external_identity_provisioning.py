from __future__ import annotations

import hashlib

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from server_schema import (
    external_identities,
    organizations,
    workspace_users,
)
from services.access_control import ActorIdentity
from services.audit_log import (
    AuditEvent,
    AuditEventWriter,
    PostgresAuditEventWriter,
)
from services.external_identity import VerifiedExternalIdentity


class ExternalIdentityProvisioningDenied(PermissionError):
    """Generic denial for unauthorized or invalid identity provisioning."""


def _mapping_target(identity: VerifiedExternalIdentity) -> str:
    return hashlib.sha256(
        f"{identity.issuer}\n{identity.subject}".encode("utf-8")
    ).hexdigest()


class PostgresExternalIdentityProvisioningService:
    """Link/revoke external identities with org-admin and audit atomicity."""

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
        allowed = connection.execute(
            sa.select(workspace_users.c.user_id)
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
                workspace_users.c.organization_role == "org_admin",
                organizations.c.status == "active",
            )
        ).scalar_one_or_none()
        if allowed is None:
            raise ExternalIdentityProvisioningDenied(
                "external identity provisioning denied"
            )

    @staticmethod
    def _require_active_target(
        connection: Connection,
        actor: ActorIdentity,
        user_id: str,
    ) -> None:
        target = connection.execute(
            sa.select(workspace_users.c.user_id).where(
                workspace_users.c.organization_id
                == actor.organization_id,
                workspace_users.c.user_id == user_id,
                workspace_users.c.status == "active",
            )
        ).scalar_one_or_none()
        if target is None:
            raise ExternalIdentityProvisioningDenied(
                "external identity provisioning denied"
            )

    def link(
        self,
        *,
        actor: ActorIdentity,
        identity: VerifiedExternalIdentity,
        user_id: str,
        event_id: str,
    ) -> None:
        with self._engine.begin() as connection:
            self.link_in_transaction(
                connection,
                actor=actor,
                identity=identity,
                user_id=user_id,
                event_id=event_id,
            )

    def link_in_transaction(
        self,
        connection: Connection,
        *,
        actor: ActorIdentity,
        identity: VerifiedExternalIdentity,
        user_id: str,
        event_id: str,
    ) -> None:
        normalized_user_id = user_id.strip()
        if not normalized_user_id:
            raise ValueError("user_id is required")
        self._require_org_admin(connection, actor)
        self._require_active_target(connection, actor, normalized_user_id)
        statement = insert(external_identities).values(
            issuer=identity.issuer,
            subject=identity.subject,
            organization_id=actor.organization_id,
            user_id=normalized_user_id,
            status="active",
        )
        try:
            result = connection.execute(
                statement.on_conflict_do_update(
                    index_elements=[
                        external_identities.c.issuer,
                        external_identities.c.subject,
                    ],
                    set_={
                        "status": "active",
                        "updated_at": sa.func.now(),
                    },
                    where=sa.and_(
                        external_identities.c.organization_id
                        == actor.organization_id,
                        external_identities.c.user_id
                        == normalized_user_id,
                    ),
                )
            )
        except IntegrityError as exc:
            raise ExternalIdentityProvisioningDenied(
                "external identity provisioning denied"
            ) from exc
        if not result.rowcount:
            raise ExternalIdentityProvisioningDenied(
                "external identity provisioning denied"
            )
        self._audit.append(
            connection,
            AuditEvent(
                organization_id=actor.organization_id,
                event_id=event_id,
                actor_user_id=actor.user_id,
                action="external_identity.link",
                target_type="external_identity",
                target_id=_mapping_target(identity),
                details={
                    "issuer": identity.issuer,
                    "user_id": normalized_user_id,
                },
            ),
        )

    def revoke(
        self,
        *,
        actor: ActorIdentity,
        identity: VerifiedExternalIdentity,
        event_id: str,
    ) -> None:
        with self._engine.begin() as connection:
            self.revoke_in_transaction(
                connection,
                actor=actor,
                identity=identity,
                event_id=event_id,
            )

    def revoke_in_transaction(
        self,
        connection: Connection,
        *,
        actor: ActorIdentity,
        identity: VerifiedExternalIdentity,
        event_id: str,
    ) -> None:
        self._require_org_admin(connection, actor)
        result = connection.execute(
            external_identities.update()
            .where(
                external_identities.c.issuer == identity.issuer,
                external_identities.c.subject == identity.subject,
                external_identities.c.organization_id
                == actor.organization_id,
                external_identities.c.status == "active",
            )
            .values(status="revoked", updated_at=sa.func.now())
        )
        if result.rowcount != 1:
            raise ExternalIdentityProvisioningDenied(
                "external identity provisioning denied"
            )
        self._audit.append(
            connection,
            AuditEvent(
                organization_id=actor.organization_id,
                event_id=event_id,
                actor_user_id=actor.user_id,
                action="external_identity.revoke",
                target_type="external_identity",
                target_id=_mapping_target(identity),
                details={"issuer": identity.issuer},
            ),
        )


__all__ = [
    "ExternalIdentityProvisioningDenied",
    "PostgresExternalIdentityProvisioningService",
]
