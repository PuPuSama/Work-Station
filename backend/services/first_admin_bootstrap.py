from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import re
import secrets
from typing import Callable, Literal
import uuid

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from knowledge_agent.schema import projects
from server_schema import (
    external_identities,
    organizations,
    project_ownership,
    team_memberships,
    teams,
    workspace_invitations,
    workspace_users,
)
from services.audit_log import (
    AuditEvent,
    AuditEventWriter,
    PostgresAuditEventWriter,
)
from services.external_identity import normalize_external_issuer


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")


class FirstAdminBootstrapError(RuntimeError):
    """Base error for the controlled first-administrator bootstrap."""


class FirstAdminBootstrapConflict(FirstAdminBootstrapError):
    """Existing state does not exactly match the requested bootstrap."""


class FirstAdminBootstrapUnavailable(FirstAdminBootstrapError):
    """The database or audit dependency failed without exposing details."""


def _identifier(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not _IDENTIFIER.fullmatch(normalized):
        raise ValueError(
            f"{field_name} must contain 1-200 letters, numbers, dots, "
            "underscores, or hyphens"
        )
    return normalized


def _display_text(value: str, field_name: str) -> str:
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 200
        or any(ord(character) < 32 for character in normalized)
    ):
        raise ValueError(f"{field_name} must contain 1-200 safe characters")
    return normalized


def _invitation_token(value: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 512:
        raise ValueError("invitation_token must contain 1-512 characters")
    return normalized


def _token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class FirstAdminBootstrapRequest:
    organization_id: str
    organization_name: str
    user_id: str
    display_name: str
    team_id: str
    team_name: str
    project_id: str
    issuer: str
    invitation_token: str = field(repr=False)
    expires_in_hours: int = 24

    def __post_init__(self) -> None:
        for field_name in (
            "organization_id",
            "user_id",
            "team_id",
            "project_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _identifier(str(getattr(self, field_name)), field_name),
            )
        for field_name in (
            "organization_name",
            "display_name",
            "team_name",
        ):
            object.__setattr__(
                self,
                field_name,
                _display_text(str(getattr(self, field_name)), field_name),
            )
        object.__setattr__(
            self,
            "issuer",
            normalize_external_issuer(self.issuer),
        )
        object.__setattr__(
            self,
            "invitation_token",
            _invitation_token(self.invitation_token),
        )
        if not 1 <= int(self.expires_in_hours) <= 168:
            raise ValueError("expires_in_hours must be between 1 and 168")
        object.__setattr__(self, "expires_in_hours", int(self.expires_in_hours))


BootstrapState = Literal["created", "pending", "accepted"]


@dataclass(frozen=True, slots=True)
class FirstAdminBootstrapResult:
    organization_id: str
    user_id: str
    team_id: str
    project_id: str
    invitation_id: str
    expires_at: datetime
    state: BootstrapState


class FirstAdminBootstrapService:
    """Create the first verified-admin invitation behind strict gates."""

    def __init__(
        self,
        engine: Engine,
        *,
        audit: AuditEventWriter | None = None,
        token_factory: Callable[[], str] | None = None,
        invitation_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._engine = engine
        self._audit = audit or PostgresAuditEventWriter()
        self._token_factory = token_factory or (
            lambda: secrets.token_urlsafe(32)
        )
        self._invitation_id_factory = invitation_id_factory or (
            lambda: f"inv_{uuid.uuid4().hex}"
        )

    def create_token(self) -> str:
        return _invitation_token(self._token_factory())

    def bootstrap(
        self,
        request: FirstAdminBootstrapRequest,
        *,
        now: datetime | None = None,
    ) -> FirstAdminBootstrapResult:
        observed_at = now or datetime.now(timezone.utc)
        if observed_at.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        normalized_now = observed_at.astimezone(timezone.utc)
        try:
            with self._engine.begin() as connection:
                self._lock(connection, request.organization_id)
                existing = self._existing_result(
                    connection,
                    request,
                    now=normalized_now,
                )
                if existing is not None:
                    return existing
                self._require_empty_boundary(connection, request)
                invitation_id = _identifier(
                    self._invitation_id_factory(),
                    "invitation_id",
                )
                expires_at = normalized_now + timedelta(
                    hours=request.expires_in_hours
                )
                connection.execute(
                    organizations.insert().values(
                        organization_id=request.organization_id,
                        name=request.organization_name,
                        status="active",
                    )
                )
                connection.execute(
                    workspace_users.insert().values(
                        organization_id=request.organization_id,
                        user_id=request.user_id,
                        display_name=request.display_name,
                        status="active",
                        organization_role="org_admin",
                    )
                )
                connection.execute(
                    teams.insert().values(
                        organization_id=request.organization_id,
                        team_id=request.team_id,
                        name=request.team_name,
                        manager_user_id=request.user_id,
                        status="active",
                    )
                )
                connection.execute(
                    team_memberships.insert().values(
                        organization_id=request.organization_id,
                        team_id=request.team_id,
                        user_id=request.user_id,
                        role="team_lead",
                        granted_by_user_id=request.user_id,
                    )
                )
                connection.execute(
                    project_ownership.insert().values(
                        project_id=request.project_id,
                        organization_id=request.organization_id,
                        owning_team_id=request.team_id,
                    )
                )
                connection.execute(
                    workspace_invitations.insert().values(
                        organization_id=request.organization_id,
                        invitation_id=invitation_id,
                        user_id=request.user_id,
                        issuer=request.issuer,
                        token_hash=_token_hash(request.invitation_token),
                        expires_at=expires_at,
                        created_by_user_id=request.user_id,
                    )
                )
                self._append_audit(
                    connection,
                    AuditEvent(
                        organization_id=request.organization_id,
                        event_id=f"bootstrap_{uuid.uuid4().hex}",
                        actor_user_id=request.user_id,
                        project_id=request.project_id,
                        action="organization.first_admin_bootstrapped",
                        target_type="organization",
                        target_id=request.organization_id,
                        details={
                            "project_assigned": True,
                            "team_created": True,
                            "invitation_issued": True,
                        },
                    ),
                )
                self._append_audit(
                    connection,
                    AuditEvent(
                        organization_id=request.organization_id,
                        event_id=f"bootstrap_invitation_{uuid.uuid4().hex}",
                        actor_user_id=request.user_id,
                        action="workspace_invitation.issued",
                        target_type="workspace_invitation",
                        target_id=invitation_id,
                        details={
                            "issuer": request.issuer,
                            "user_id": request.user_id,
                            "expires_in_hours": request.expires_in_hours,
                        },
                    ),
                )
                return FirstAdminBootstrapResult(
                    organization_id=request.organization_id,
                    user_id=request.user_id,
                    team_id=request.team_id,
                    project_id=request.project_id,
                    invitation_id=invitation_id,
                    expires_at=expires_at,
                    state="created",
                )
        except (FirstAdminBootstrapConflict, ValueError):
            raise
        except IntegrityError as exc:
            raise FirstAdminBootstrapConflict(
                "first administrator bootstrap conflicts with existing state"
            ) from exc
        except FirstAdminBootstrapUnavailable:
            raise
        except SQLAlchemyError as exc:
            raise FirstAdminBootstrapUnavailable(
                "first administrator bootstrap is unavailable"
            ) from exc

    @staticmethod
    def _lock(connection: Connection, organization_id: str) -> None:
        connection.execute(
            sa.select(
                sa.func.pg_advisory_xact_lock(
                    sa.func.hashtextextended(
                        f"first-admin-bootstrap:{organization_id}",
                        0,
                    )
                )
            )
        ).scalar_one()

    def _existing_result(
        self,
        connection: Connection,
        request: FirstAdminBootstrapRequest,
        *,
        now: datetime,
    ) -> FirstAdminBootstrapResult | None:
        organization = connection.execute(
            sa.select(
                organizations.c.name,
                organizations.c.status,
            ).where(
                organizations.c.organization_id == request.organization_id
            )
        ).mappings().one_or_none()
        if organization is None:
            return None
        user = connection.execute(
            sa.select(
                workspace_users.c.display_name,
                workspace_users.c.status,
                workspace_users.c.organization_role,
            ).where(
                workspace_users.c.organization_id == request.organization_id,
                workspace_users.c.user_id == request.user_id,
            )
        ).mappings().one_or_none()
        team = connection.execute(
            sa.select(
                teams.c.name,
                teams.c.manager_user_id,
                teams.c.status,
            ).where(
                teams.c.organization_id == request.organization_id,
                teams.c.team_id == request.team_id,
            )
        ).mappings().one_or_none()
        membership = connection.execute(
            sa.select(team_memberships.c.role).where(
                team_memberships.c.organization_id == request.organization_id,
                team_memberships.c.team_id == request.team_id,
                team_memberships.c.user_id == request.user_id,
            )
        ).scalar_one_or_none()
        ownership = connection.execute(
            sa.select(
                project_ownership.c.organization_id,
                project_ownership.c.owning_team_id,
            ).where(project_ownership.c.project_id == request.project_id)
        ).mappings().one_or_none()
        expected = (
            organization["name"] == request.organization_name
            and organization["status"] == "active"
            and user is not None
            and user["display_name"] == request.display_name
            and user["status"] == "active"
            and user["organization_role"] == "org_admin"
            and team is not None
            and team["name"] == request.team_name
            and team["manager_user_id"] == request.user_id
            and team["status"] == "active"
            and membership == "team_lead"
            and ownership is not None
            and ownership["organization_id"] == request.organization_id
            and ownership["owning_team_id"] == request.team_id
        )
        if not expected:
            raise FirstAdminBootstrapConflict(
                "existing bootstrap scope does not match the request"
            )
        identity_exists = connection.execute(
            sa.select(sa.func.count())
            .select_from(external_identities)
            .where(
                external_identities.c.issuer == request.issuer,
                external_identities.c.organization_id
                == request.organization_id,
                external_identities.c.user_id == request.user_id,
                external_identities.c.status == "active",
            )
        ).scalar_one()
        invitation = connection.execute(
            sa.select(
                workspace_invitations.c.invitation_id,
                workspace_invitations.c.status,
                workspace_invitations.c.token_hash,
                workspace_invitations.c.expires_at,
            )
            .where(
                workspace_invitations.c.organization_id
                == request.organization_id,
                workspace_invitations.c.user_id == request.user_id,
                workspace_invitations.c.issuer == request.issuer,
            )
            .order_by(workspace_invitations.c.created_at.desc())
            .limit(1)
        ).mappings().one_or_none()
        if identity_exists:
            if invitation is None:
                raise FirstAdminBootstrapConflict(
                    "bootstrap identity exists without its invitation record"
                )
            return FirstAdminBootstrapResult(
                organization_id=request.organization_id,
                user_id=request.user_id,
                team_id=request.team_id,
                project_id=request.project_id,
                invitation_id=str(invitation["invitation_id"]),
                expires_at=invitation["expires_at"],
                state="accepted",
            )
        if (
            invitation is None
            or invitation["status"] != "pending"
            or invitation["expires_at"] <= now
            or invitation["token_hash"]
            != _token_hash(request.invitation_token)
        ):
            raise FirstAdminBootstrapConflict(
                "bootstrap invitation is unavailable or does not match"
            )
        return FirstAdminBootstrapResult(
            organization_id=request.organization_id,
            user_id=request.user_id,
            team_id=request.team_id,
            project_id=request.project_id,
            invitation_id=str(invitation["invitation_id"]),
            expires_at=invitation["expires_at"],
            state="pending",
        )

    @staticmethod
    def _require_empty_boundary(
        connection: Connection,
        request: FirstAdminBootstrapRequest,
    ) -> None:
        project = connection.execute(
            sa.select(projects.c.status).where(
                projects.c.project_id == request.project_id
            )
        ).scalar_one_or_none()
        if project != "active":
            raise FirstAdminBootstrapConflict(
                "bootstrap project must exist and be active"
            )
        owner = connection.execute(
            sa.select(project_ownership.c.organization_id).where(
                project_ownership.c.project_id == request.project_id
            )
        ).scalar_one_or_none()
        if owner is not None:
            raise FirstAdminBootstrapConflict(
                "bootstrap project already has an owner"
            )
        issuer_identity_count = connection.execute(
            sa.select(sa.func.count())
            .select_from(external_identities)
            .where(
                external_identities.c.issuer == request.issuer,
                external_identities.c.status == "active",
            )
        ).scalar_one()
        issuer_invitation_count = connection.execute(
            sa.select(sa.func.count())
            .select_from(workspace_invitations)
            .where(
                workspace_invitations.c.issuer == request.issuer,
                workspace_invitations.c.status == "pending",
                workspace_invitations.c.expires_at > sa.func.now(),
            )
        ).scalar_one()
        if issuer_identity_count or issuer_invitation_count:
            raise FirstAdminBootstrapConflict(
                "configured issuer is not eligible for first bootstrap"
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
            raise FirstAdminBootstrapUnavailable(
                "first administrator bootstrap audit is unavailable"
            ) from exc


__all__ = [
    "FirstAdminBootstrapConflict",
    "FirstAdminBootstrapError",
    "FirstAdminBootstrapRequest",
    "FirstAdminBootstrapResult",
    "FirstAdminBootstrapService",
    "FirstAdminBootstrapUnavailable",
]
