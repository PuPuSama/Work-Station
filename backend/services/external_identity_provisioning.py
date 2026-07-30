from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Literal, cast

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection, Engine, RowMapping
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

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
    """Generic denial for unauthorized or cross-Organization provisioning."""


class ExternalIdentityMappingNotFound(LookupError):
    """A mapping is unavailable inside the authorized Organization."""


class ExternalIdentityProvisioningUnavailable(RuntimeError):
    """A private database or audit dependency failed."""


def _mapping_target(identity: VerifiedExternalIdentity) -> str:
    return hashlib.sha256(
        f"{identity.issuer}\n{identity.subject}".encode("utf-8")
    ).hexdigest()


def _required_text(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized


def _mapping_id(value: str, field_name: str = "mapping_id") -> str:
    normalized = _required_text(value, field_name)
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 value")
    return normalized


def _mapping_id_expression() -> sa.ColumnElement[str]:
    raw = external_identities.c.issuer + sa.literal(
        "\n"
    ) + external_identities.c.subject
    return sa.func.encode(
        sa.func.sha256(sa.func.convert_to(raw, "UTF8")),
        "hex",
        type_=sa.Text(),
    )


@dataclass(frozen=True)
class ExternalIdentityMappingRecord:
    """Public admin read model that intentionally omits the raw Subject."""

    mapping_id: str
    issuer: str
    user_id: str
    user_display_name: str
    user_status: Literal["active", "disabled"]
    status: Literal["active", "revoked"]


@dataclass(frozen=True)
class ExternalIdentityMappingPage:
    items: tuple[ExternalIdentityMappingRecord, ...]
    next_after_mapping_id: str | None


class PostgresExternalIdentityProvisioningService:
    """List/link/revoke mappings with org-admin and audit atomicity."""

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
        *,
        write: bool,
    ) -> None:
        organization = connection.execute(
            sa.select(organizations.c.organization_id)
            .where(
                organizations.c.organization_id == actor.organization_id,
                organizations.c.status == "active",
            )
            .with_for_update(read=not write)
        ).scalar_one_or_none()
        if organization is None:
            raise ExternalIdentityProvisioningDenied(
                "external identity provisioning denied"
            )
        allowed = connection.execute(
            sa.select(workspace_users.c.user_id)
            .where(
                workspace_users.c.organization_id
                == actor.organization_id,
                workspace_users.c.user_id == actor.user_id,
                workspace_users.c.status == "active",
                workspace_users.c.organization_role == "org_admin",
            )
            .with_for_update(read=True)
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
            sa.select(workspace_users.c.user_id)
            .where(
                workspace_users.c.organization_id
                == actor.organization_id,
                workspace_users.c.user_id == user_id,
                workspace_users.c.status == "active",
            )
            .with_for_update(read=True)
        ).scalar_one_or_none()
        if target is None:
            raise ExternalIdentityProvisioningDenied(
                "external identity provisioning denied"
            )

    def list_mappings(
        self,
        *,
        actor: ActorIdentity,
        organization_id: str,
        limit: int = 50,
        after_mapping_id: str | None = None,
    ) -> ExternalIdentityMappingPage:
        normalized_organization_id = _required_text(
            organization_id,
            "organization_id",
        )
        if normalized_organization_id != actor.organization_id:
            raise ExternalIdentityProvisioningDenied(
                "external identity provisioning denied"
            )
        normalized_limit = int(limit)
        if normalized_limit < 1 or normalized_limit > 100:
            raise ValueError("limit must be between 1 and 100")
        normalized_after = (
            _mapping_id(after_mapping_id, "after_mapping_id")
            if after_mapping_id is not None
            else None
        )
        mapping_id = _mapping_id_expression()
        try:
            with self._engine.begin() as connection:
                self._require_org_admin(connection, actor, write=False)
                statement = self._mapping_select(
                    organization_id=normalized_organization_id
                )
                if normalized_after is not None:
                    statement = statement.where(
                        mapping_id > normalized_after
                    )
                rows = (
                    connection.execute(
                        statement.order_by(mapping_id).limit(
                            normalized_limit + 1
                        )
                    )
                    .mappings()
                    .all()
                )
        except ExternalIdentityProvisioningDenied:
            raise
        except SQLAlchemyError as exc:
            raise ExternalIdentityProvisioningUnavailable(
                "external identity directory is unavailable"
            ) from exc
        has_more = len(rows) > normalized_limit
        items = tuple(
            self._record(row) for row in rows[:normalized_limit]
        )
        return ExternalIdentityMappingPage(
            items=items,
            next_after_mapping_id=(
                items[-1].mapping_id if has_more else None
            ),
        )

    def link(
        self,
        *,
        actor: ActorIdentity,
        identity: VerifiedExternalIdentity,
        user_id: str,
        event_id: str,
    ) -> ExternalIdentityMappingRecord:
        try:
            with self._engine.begin() as connection:
                return self.link_in_transaction(
                    connection,
                    actor=actor,
                    identity=identity,
                    user_id=user_id,
                    event_id=event_id,
                )
        except ExternalIdentityProvisioningDenied:
            raise
        except ExternalIdentityProvisioningUnavailable:
            raise
        except SQLAlchemyError as exc:
            raise ExternalIdentityProvisioningUnavailable(
                "external identity change is unavailable"
            ) from exc

    def link_in_transaction(
        self,
        connection: Connection,
        *,
        actor: ActorIdentity,
        identity: VerifiedExternalIdentity,
        user_id: str,
        event_id: str,
    ) -> ExternalIdentityMappingRecord:
        normalized_user_id = _required_text(user_id, "user_id")
        normalized_event_id = _required_text(event_id, "event_id")
        self._require_org_admin(connection, actor, write=True)
        self._require_active_target(connection, actor, normalized_user_id)
        existing = connection.execute(
            sa.select(
                external_identities.c.organization_id,
                external_identities.c.user_id,
                external_identities.c.status,
            )
            .where(
                external_identities.c.issuer == identity.issuer,
                external_identities.c.subject == identity.subject,
            )
            .with_for_update()
        ).mappings().one_or_none()
        if existing is not None:
            if (
                existing["organization_id"] != actor.organization_id
                or existing["user_id"] != normalized_user_id
            ):
                raise ExternalIdentityProvisioningDenied(
                    "external identity provisioning denied"
                )
            if existing["status"] == "active":
                return self._read_mapping(
                    connection,
                    organization_id=actor.organization_id,
                    mapping_id=_mapping_target(identity),
                )
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
        self._append_audit(
            connection,
            AuditEvent(
                organization_id=actor.organization_id,
                event_id=normalized_event_id,
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
        return self._read_mapping(
            connection,
            organization_id=actor.organization_id,
            mapping_id=_mapping_target(identity),
        )

    def revoke(
        self,
        *,
        actor: ActorIdentity,
        identity: VerifiedExternalIdentity,
        event_id: str,
    ) -> None:
        try:
            with self._engine.begin() as connection:
                self.revoke_in_transaction(
                    connection,
                    actor=actor,
                    identity=identity,
                    event_id=event_id,
                )
        except ExternalIdentityProvisioningDenied:
            raise
        except ExternalIdentityProvisioningUnavailable:
            raise
        except SQLAlchemyError as exc:
            raise ExternalIdentityProvisioningUnavailable(
                "external identity change is unavailable"
            ) from exc

    def revoke_mapping(
        self,
        *,
        actor: ActorIdentity,
        organization_id: str,
        mapping_id: str,
        event_id: str,
    ) -> ExternalIdentityMappingRecord:
        normalized_organization_id = _required_text(
            organization_id,
            "organization_id",
        )
        normalized_mapping_id = _mapping_id(mapping_id)
        if normalized_organization_id != actor.organization_id:
            raise ExternalIdentityProvisioningDenied(
                "external identity provisioning denied"
            )
        try:
            with self._engine.begin() as connection:
                self._require_org_admin(connection, actor, write=True)
                mapping_id_expression = _mapping_id_expression()
                row = connection.execute(
                    sa.select(
                        external_identities.c.issuer,
                        external_identities.c.subject,
                    )
                    .where(
                        external_identities.c.organization_id
                        == normalized_organization_id,
                        mapping_id_expression == normalized_mapping_id,
                        external_identities.c.status == "active",
                    )
                    .with_for_update()
                ).mappings().one_or_none()
                if row is None:
                    raise ExternalIdentityMappingNotFound(
                        "external identity mapping is unavailable"
                    )
                identity = VerifiedExternalIdentity(
                    issuer=str(row["issuer"]),
                    subject=str(row["subject"]),
                )
                self.revoke_in_transaction(
                    connection,
                    actor=actor,
                    identity=identity,
                    event_id=event_id,
                    authorization_checked=True,
                )
                return self._read_mapping(
                    connection,
                    organization_id=normalized_organization_id,
                    mapping_id=normalized_mapping_id,
                )
        except (
            ExternalIdentityMappingNotFound,
            ExternalIdentityProvisioningDenied,
        ):
            raise
        except ExternalIdentityProvisioningUnavailable:
            raise
        except SQLAlchemyError as exc:
            raise ExternalIdentityProvisioningUnavailable(
                "external identity change is unavailable"
            ) from exc

    def revoke_in_transaction(
        self,
        connection: Connection,
        *,
        actor: ActorIdentity,
        identity: VerifiedExternalIdentity,
        event_id: str,
        authorization_checked: bool = False,
    ) -> None:
        normalized_event_id = _required_text(event_id, "event_id")
        if not authorization_checked:
            self._require_org_admin(connection, actor, write=True)
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
        self._append_audit(
            connection,
            AuditEvent(
                organization_id=actor.organization_id,
                event_id=normalized_event_id,
                actor_user_id=actor.user_id,
                action="external_identity.revoke",
                target_type="external_identity",
                target_id=_mapping_target(identity),
                details={"issuer": identity.issuer},
            ),
        )

    @staticmethod
    def _mapping_select(
        *,
        organization_id: str,
    ) -> sa.Select:
        mapping_id = _mapping_id_expression()
        return (
            sa.select(
                mapping_id.label("mapping_id"),
                external_identities.c.issuer,
                external_identities.c.user_id,
                workspace_users.c.display_name.label(
                    "user_display_name"
                ),
                workspace_users.c.status.label("user_status"),
                external_identities.c.status,
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
                )
            )
            .where(
                external_identities.c.organization_id == organization_id
            )
        )

    def _read_mapping(
        self,
        connection: Connection,
        *,
        organization_id: str,
        mapping_id: str,
    ) -> ExternalIdentityMappingRecord:
        row = connection.execute(
            self._mapping_select(
                organization_id=organization_id
            ).where(_mapping_id_expression() == mapping_id)
        ).mappings().one_or_none()
        if row is None:
            raise ExternalIdentityMappingNotFound(
                "external identity mapping is unavailable"
            )
        return self._record(row)

    @staticmethod
    def _record(row: RowMapping) -> ExternalIdentityMappingRecord:
        return ExternalIdentityMappingRecord(
            mapping_id=str(row["mapping_id"]),
            issuer=str(row["issuer"]),
            user_id=str(row["user_id"]),
            user_display_name=str(row["user_display_name"]),
            user_status=cast(
                Literal["active", "disabled"],
                row["user_status"],
            ),
            status=cast(
                Literal["active", "revoked"],
                row["status"],
            ),
        )

    def _append_audit(
        self,
        connection: Connection,
        event: AuditEvent,
    ) -> None:
        try:
            self._audit.append(connection, event)
        except IntegrityError:
            raise
        except Exception as exc:
            raise ExternalIdentityProvisioningUnavailable(
                "external identity change is unavailable"
            ) from exc


__all__ = [
    "ExternalIdentityMappingNotFound",
    "ExternalIdentityMappingPage",
    "ExternalIdentityMappingRecord",
    "ExternalIdentityProvisioningDenied",
    "ExternalIdentityProvisioningUnavailable",
    "PostgresExternalIdentityProvisioningService",
]
