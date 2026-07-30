from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import secrets
from typing import Literal, Protocol, cast
import uuid

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection, Engine, RowMapping
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from server_schema import (
    external_identities,
    organizations,
    workspace_invitations,
    workspace_users,
)
from services.access_control import ActorIdentity
from services.audit_log import (
    AuditEvent,
    AuditEventWriter,
    PostgresAuditEventWriter,
)
from services.external_identity import (
    ResolvedExternalActor,
    VerifiedExternalIdentity,
    normalize_external_issuer,
)


InvitationStatus = Literal["pending", "expired", "accepted", "revoked"]


class WorkspaceInvitationDenied(PermissionError):
    """A generic invitation denial that does not reveal tenant facts."""


class WorkspaceInvitationConflict(RuntimeError):
    """A safe conflict such as an existing pending invitation."""


class WorkspaceInvitationNotFound(LookupError):
    """An invitation is unavailable in the authorized Organization."""


class WorkspaceInvitationUnavailable(RuntimeError):
    """A private database or audit dependency failed."""


@dataclass(frozen=True)
class WorkspaceInvitationRecord:
    organization_id: str
    invitation_id: str
    user_id: str
    user_display_name: str
    issuer: str
    status: InvitationStatus
    expires_at: datetime
    created_by_user_id: str
    created_at: datetime


@dataclass(frozen=True)
class WorkspaceInvitationPage:
    items: tuple[WorkspaceInvitationRecord, ...]
    next_after_invitation_id: str | None


@dataclass(frozen=True)
class IssuedWorkspaceInvitation:
    invitation: WorkspaceInvitationRecord
    invitation_token: str


class WorkspaceInvitationRedeemer(Protocol):
    def redeem(
        self,
        *,
        invitation_token: str,
        identity: VerifiedExternalIdentity,
        event_id: str,
    ) -> ResolvedExternalActor: ...


def _required_text(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized


def _token_hash(token: str) -> str:
    normalized = _required_text(token, "invitation_token")
    if len(normalized) > 512:
        raise WorkspaceInvitationDenied("workspace invitation denied")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class PostgresWorkspaceInvitationService:
    """Issue, revoke, and redeem one-time external identity invitations."""

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
        admin = connection.execute(
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
        if organization is None or admin is None:
            raise WorkspaceInvitationDenied(
                "workspace invitation denied"
            )

    @staticmethod
    def _require_active_target(
        connection: Connection,
        *,
        organization_id: str,
        user_id: str,
    ) -> RowMapping:
        target = connection.execute(
            sa.select(
                workspace_users.c.user_id,
                workspace_users.c.display_name,
                workspace_users.c.session_version,
            )
            .where(
                workspace_users.c.organization_id == organization_id,
                workspace_users.c.user_id == user_id,
                workspace_users.c.status == "active",
            )
            .with_for_update(read=True)
        ).mappings().one_or_none()
        if target is None:
            raise WorkspaceInvitationDenied(
                "workspace invitation denied"
            )
        return target

    def list_invitations(
        self,
        *,
        actor: ActorIdentity,
        organization_id: str,
        limit: int = 50,
        after_invitation_id: str | None = None,
    ) -> WorkspaceInvitationPage:
        normalized_organization_id = _required_text(
            organization_id,
            "organization_id",
        )
        if normalized_organization_id != actor.organization_id:
            raise WorkspaceInvitationDenied(
                "workspace invitation denied"
            )
        normalized_limit = int(limit)
        if not 1 <= normalized_limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        normalized_after = (
            _required_text(after_invitation_id, "after_invitation_id")
            if after_invitation_id is not None
            else None
        )
        try:
            with self._engine.begin() as connection:
                self._require_org_admin(connection, actor, write=False)
                statement = self._directory_select(
                    normalized_organization_id
                )
                if normalized_after is not None:
                    statement = statement.where(
                        workspace_invitations.c.invitation_id
                        > normalized_after
                    )
                rows = (
                    connection.execute(
                        statement.order_by(
                            workspace_invitations.c.invitation_id
                        ).limit(normalized_limit + 1)
                    )
                    .mappings()
                    .all()
                )
        except WorkspaceInvitationDenied:
            raise
        except SQLAlchemyError as exc:
            raise WorkspaceInvitationUnavailable(
                "workspace invitation directory is unavailable"
            ) from exc
        has_more = len(rows) > normalized_limit
        items = tuple(
            self._record(row) for row in rows[:normalized_limit]
        )
        return WorkspaceInvitationPage(
            items=items,
            next_after_invitation_id=(
                items[-1].invitation_id if has_more else None
            ),
        )

    def issue(
        self,
        *,
        actor: ActorIdentity,
        organization_id: str,
        user_id: str,
        issuer: str,
        expires_in_hours: int,
        event_id: str,
    ) -> IssuedWorkspaceInvitation:
        normalized_organization_id = _required_text(
            organization_id,
            "organization_id",
        )
        if normalized_organization_id != actor.organization_id:
            raise WorkspaceInvitationDenied(
                "workspace invitation denied"
            )
        normalized_user_id = _required_text(user_id, "user_id")
        normalized_issuer = normalize_external_issuer(issuer)
        normalized_hours = int(expires_in_hours)
        if not 1 <= normalized_hours <= 168:
            raise ValueError("expires_in_hours must be between 1 and 168")
        normalized_event_id = _required_text(event_id, "event_id")
        invitation_id = f"inv_{uuid.uuid4().hex}"
        raw_token = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + timedelta(
            hours=normalized_hours
        )
        try:
            with self._engine.begin() as connection:
                self._require_org_admin(connection, actor, write=True)
                self._require_active_target(
                    connection,
                    organization_id=normalized_organization_id,
                    user_id=normalized_user_id,
                )
                try:
                    connection.execute(
                        workspace_invitations.insert().values(
                            organization_id=normalized_organization_id,
                            invitation_id=invitation_id,
                            user_id=normalized_user_id,
                            issuer=normalized_issuer,
                            token_hash=_token_hash(raw_token),
                            expires_at=expires_at,
                            created_by_user_id=actor.user_id,
                        )
                    )
                except IntegrityError as exc:
                    raise WorkspaceInvitationConflict(
                        "a pending invitation already exists"
                    ) from exc
                self._append_audit(
                    connection,
                    AuditEvent(
                        organization_id=normalized_organization_id,
                        event_id=normalized_event_id,
                        actor_user_id=actor.user_id,
                        action="workspace_invitation.issued",
                        target_type="workspace_invitation",
                        target_id=invitation_id,
                        details={
                            "issuer": normalized_issuer,
                            "user_id": normalized_user_id,
                            "expires_in_hours": normalized_hours,
                        },
                    ),
                )
                row = connection.execute(
                    self._directory_select(
                        normalized_organization_id
                    ).where(
                        workspace_invitations.c.invitation_id
                        == invitation_id
                    )
                ).mappings().one()
        except (
            WorkspaceInvitationConflict,
            WorkspaceInvitationDenied,
        ):
            raise
        except WorkspaceInvitationUnavailable:
            raise
        except SQLAlchemyError as exc:
            raise WorkspaceInvitationUnavailable(
                "workspace invitation change is unavailable"
            ) from exc
        return IssuedWorkspaceInvitation(
            invitation=self._record(row),
            invitation_token=raw_token,
        )

    def revoke(
        self,
        *,
        actor: ActorIdentity,
        organization_id: str,
        invitation_id: str,
        event_id: str,
    ) -> WorkspaceInvitationRecord:
        normalized_organization_id = _required_text(
            organization_id,
            "organization_id",
        )
        normalized_invitation_id = _required_text(
            invitation_id,
            "invitation_id",
        )
        normalized_event_id = _required_text(event_id, "event_id")
        if normalized_organization_id != actor.organization_id:
            raise WorkspaceInvitationDenied(
                "workspace invitation denied"
            )
        try:
            with self._engine.begin() as connection:
                self._require_org_admin(connection, actor, write=True)
                result = connection.execute(
                    workspace_invitations.update()
                    .where(
                        workspace_invitations.c.organization_id
                        == normalized_organization_id,
                        workspace_invitations.c.invitation_id
                        == normalized_invitation_id,
                        workspace_invitations.c.status == "pending",
                    )
                    .values(
                        status="revoked",
                        updated_at=sa.func.now(),
                    )
                )
                if result.rowcount != 1:
                    raise WorkspaceInvitationNotFound(
                        "workspace invitation is unavailable"
                    )
                self._append_audit(
                    connection,
                    AuditEvent(
                        organization_id=normalized_organization_id,
                        event_id=normalized_event_id,
                        actor_user_id=actor.user_id,
                        action="workspace_invitation.revoked",
                        target_type="workspace_invitation",
                        target_id=normalized_invitation_id,
                        details={},
                    ),
                )
                row = connection.execute(
                    self._directory_select(
                        normalized_organization_id
                    ).where(
                        workspace_invitations.c.invitation_id
                        == normalized_invitation_id
                    )
                ).mappings().one()
        except (
            WorkspaceInvitationDenied,
            WorkspaceInvitationNotFound,
        ):
            raise
        except WorkspaceInvitationUnavailable:
            raise
        except SQLAlchemyError as exc:
            raise WorkspaceInvitationUnavailable(
                "workspace invitation change is unavailable"
            ) from exc
        return self._record(row)

    def redeem(
        self,
        *,
        invitation_token: str,
        identity: VerifiedExternalIdentity,
        event_id: str,
    ) -> ResolvedExternalActor:
        token_hash = _token_hash(invitation_token)
        normalized_event_id = _required_text(event_id, "event_id")
        try:
            with self._engine.begin() as connection:
                invitation = connection.execute(
                    sa.select(
                        workspace_invitations.c.organization_id,
                        workspace_invitations.c.invitation_id,
                        workspace_invitations.c.user_id,
                        workspace_invitations.c.issuer,
                    )
                    .select_from(
                        workspace_invitations.join(
                            organizations,
                            organizations.c.organization_id
                            == workspace_invitations.c.organization_id,
                        )
                    )
                    .where(
                        workspace_invitations.c.token_hash == token_hash,
                        workspace_invitations.c.status == "pending",
                        workspace_invitations.c.expires_at
                        > sa.func.now(),
                        workspace_invitations.c.issuer
                        == identity.issuer,
                        organizations.c.status == "active",
                    )
                    .with_for_update()
                ).mappings().one_or_none()
                if invitation is None:
                    raise WorkspaceInvitationDenied(
                        "workspace invitation denied"
                    )
                organization_id = str(
                    invitation["organization_id"]
                )
                user_id = str(invitation["user_id"])
                target = self._require_active_target(
                    connection,
                    organization_id=organization_id,
                    user_id=user_id,
                )
                self._link_redeemed_identity(
                    connection,
                    organization_id=organization_id,
                    user_id=user_id,
                    identity=identity,
                )
                result = connection.execute(
                    workspace_invitations.update()
                    .where(
                        workspace_invitations.c.organization_id
                        == organization_id,
                        workspace_invitations.c.invitation_id
                        == invitation["invitation_id"],
                        workspace_invitations.c.status == "pending",
                    )
                    .values(
                        status="accepted",
                        accepted_at=sa.func.now(),
                        updated_at=sa.func.now(),
                    )
                )
                if result.rowcount != 1:
                    raise WorkspaceInvitationDenied(
                        "workspace invitation denied"
                    )
                self._append_audit(
                    connection,
                    AuditEvent(
                        organization_id=organization_id,
                        event_id=normalized_event_id,
                        actor_user_id=user_id,
                        action="workspace_invitation.accepted",
                        target_type="workspace_invitation",
                        target_id=str(invitation["invitation_id"]),
                        details={
                            "issuer": identity.issuer,
                            "mapping_id": hashlib.sha256(
                                (
                                    f"{identity.issuer}\n"
                                    f"{identity.subject}"
                                ).encode("utf-8")
                            ).hexdigest(),
                        },
                    ),
                )
                return ResolvedExternalActor(
                    actor=ActorIdentity(organization_id, user_id),
                    session_version=int(target["session_version"]),
                )
        except WorkspaceInvitationDenied:
            raise
        except WorkspaceInvitationUnavailable:
            raise
        except SQLAlchemyError as exc:
            raise WorkspaceInvitationUnavailable(
                "workspace invitation redemption is unavailable"
            ) from exc

    @staticmethod
    def _link_redeemed_identity(
        connection: Connection,
        *,
        organization_id: str,
        user_id: str,
        identity: VerifiedExternalIdentity,
    ) -> None:
        existing = connection.execute(
            sa.select(
                external_identities.c.organization_id,
                external_identities.c.user_id,
            )
            .where(
                external_identities.c.issuer == identity.issuer,
                external_identities.c.subject == identity.subject,
            )
            .with_for_update()
        ).mappings().one_or_none()
        if existing is not None and (
            existing["organization_id"] != organization_id
            or existing["user_id"] != user_id
        ):
            raise WorkspaceInvitationDenied(
                "workspace invitation denied"
            )
        statement = insert(external_identities).values(
            issuer=identity.issuer,
            subject=identity.subject,
            organization_id=organization_id,
            user_id=user_id,
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
                        == organization_id,
                        external_identities.c.user_id == user_id,
                    ),
                )
            )
        except IntegrityError as exc:
            raise WorkspaceInvitationDenied(
                "workspace invitation denied"
            ) from exc
        if not result.rowcount:
            raise WorkspaceInvitationDenied(
                "workspace invitation denied"
            )

    @staticmethod
    def _directory_select(
        organization_id: str,
    ) -> sa.Select:
        effective_status = sa.case(
            (
                sa.and_(
                    workspace_invitations.c.status == "pending",
                    workspace_invitations.c.expires_at
                    <= sa.func.now(),
                ),
                "expired",
            ),
            else_=workspace_invitations.c.status,
        )
        return (
            sa.select(
                workspace_invitations.c.organization_id,
                workspace_invitations.c.invitation_id,
                workspace_invitations.c.user_id,
                workspace_users.c.display_name.label(
                    "user_display_name"
                ),
                workspace_invitations.c.issuer,
                effective_status.label("effective_status"),
                workspace_invitations.c.expires_at,
                workspace_invitations.c.created_by_user_id,
                workspace_invitations.c.created_at,
            )
            .select_from(
                workspace_invitations.join(
                    workspace_users,
                    sa.and_(
                        workspace_users.c.organization_id
                        == workspace_invitations.c.organization_id,
                        workspace_users.c.user_id
                        == workspace_invitations.c.user_id,
                    ),
                )
            )
            .where(
                workspace_invitations.c.organization_id
                == organization_id
            )
        )

    @staticmethod
    def _record(row: RowMapping) -> WorkspaceInvitationRecord:
        return WorkspaceInvitationRecord(
            organization_id=str(row["organization_id"]),
            invitation_id=str(row["invitation_id"]),
            user_id=str(row["user_id"]),
            user_display_name=str(row["user_display_name"]),
            issuer=str(row["issuer"]),
            status=cast(
                InvitationStatus,
                row["effective_status"],
            ),
            expires_at=row["expires_at"],
            created_by_user_id=str(row["created_by_user_id"]),
            created_at=row["created_at"],
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
            raise WorkspaceInvitationUnavailable(
                "workspace invitation audit is unavailable"
            ) from exc


__all__ = [
    "IssuedWorkspaceInvitation",
    "PostgresWorkspaceInvitationService",
    "WorkspaceInvitationConflict",
    "WorkspaceInvitationDenied",
    "WorkspaceInvitationNotFound",
    "WorkspaceInvitationPage",
    "WorkspaceInvitationRecord",
    "WorkspaceInvitationRedeemer",
    "WorkspaceInvitationUnavailable",
]
