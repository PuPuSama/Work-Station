from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine, RowMapping
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from knowledge_agent.schema import projects
from server_schema import (
    organizations,
    project_memberships,
    project_ownership,
    team_memberships,
    teams,
    workspace_invitations,
    workspace_users,
)
from services.access_control import ActorIdentity
from services.audit_log import (
    AuditEvent,
    AuditEventWriter,
    PostgresAuditEventWriter,
)


TeamStatus = Literal["active", "archived"]
TeamMembershipRole = Literal["team_lead", "member"]


class TeamAdministrationError(RuntimeError):
    """Base error for Organization-scoped Team administration."""


class TeamAdministrationDenied(TeamAdministrationError):
    """The Actor is not an active administrator of this Organization."""


class TeamNotFound(TeamAdministrationError):
    """The Team is unavailable within the authorized Organization."""


class TeamUserNotFound(TeamAdministrationError):
    """A target User is unavailable without cross-Organization disclosure."""


class TeamAdministrationConflict(TeamAdministrationError):
    """The requested mutation conflicts with committed Team state."""


class TeamAdministrationUnavailable(TeamAdministrationError):
    """A private dependency failed and must not leak through HTTP."""


def _required_text(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized


def _team_status(value: str) -> TeamStatus:
    normalized = _required_text(value, "status")
    if normalized not in {"active", "archived"}:
        raise ValueError("status must be active or archived")
    return cast(TeamStatus, normalized)


def _membership_role(value: str) -> TeamMembershipRole:
    normalized = _required_text(value, "role")
    if normalized not in {"team_lead", "member"}:
        raise ValueError("role must be team_lead or member")
    return cast(TeamMembershipRole, normalized)


@dataclass(frozen=True)
class TeamRecord:
    team_id: str
    name: str
    manager_user_id: str | None
    status: TeamStatus
    member_count: int
    team_lead_count: int
    project_count: int


@dataclass(frozen=True)
class TeamPage:
    items: tuple[TeamRecord, ...]
    next_after_team_id: str | None


@dataclass(frozen=True)
class TeamMemberRecord:
    user_id: str
    display_name: str
    user_status: Literal["active", "disabled"]
    role: TeamMembershipRole


@dataclass(frozen=True)
class TeamMemberPage:
    items: tuple[TeamMemberRecord, ...]
    next_after_user_id: str | None


class PostgresTeamAdministrationService:
    """Manage Teams without treating manager metadata as an access grant."""

    def __init__(
        self,
        engine: Engine,
        *,
        audit: AuditEventWriter | None = None,
    ) -> None:
        self._engine = engine
        self._audit = audit or PostgresAuditEventWriter()

    def list_teams(
        self,
        *,
        actor: ActorIdentity,
        organization_id: str,
        limit: int = 50,
        after_team_id: str | None = None,
    ) -> TeamPage:
        normalized_organization_id = _required_text(
            organization_id,
            "organization_id",
        )
        normalized_limit = int(limit)
        if normalized_limit < 1 or normalized_limit > 100:
            raise ValueError("limit must be between 1 and 100")
        normalized_after = (
            _required_text(after_team_id, "after_team_id")
            if after_team_id is not None
            else None
        )
        try:
            with self._engine.begin() as connection:
                actor_role = self._lock_active_admin(
                    connection,
                    actor=actor,
                    organization_id=normalized_organization_id,
                    write=False,
                    allow_any_team_lead=True,
                )
                statement = self._team_select(
                    organization_id=normalized_organization_id
                )
                if normalized_after is not None:
                    statement = statement.where(
                        teams.c.team_id > normalized_after
                    )
                if actor_role != "org_admin":
                    statement = statement.where(
                        teams.c.team_id.in_(
                            sa.select(team_memberships.c.team_id).where(
                                team_memberships.c.organization_id
                                == normalized_organization_id,
                                team_memberships.c.user_id == actor.user_id,
                                team_memberships.c.role == "team_lead",
                            )
                        )
                    )
                rows = (
                    connection.execute(
                        statement.order_by(teams.c.team_id).limit(
                            normalized_limit + 1
                        )
                    )
                    .mappings()
                    .all()
                )
        except TeamAdministrationDenied:
            raise
        except SQLAlchemyError as exc:
            raise TeamAdministrationUnavailable(
                "team directory is unavailable"
            ) from exc
        has_more = len(rows) > normalized_limit
        items = tuple(
            self._team_record(row) for row in rows[:normalized_limit]
        )
        return TeamPage(
            items=items,
            next_after_team_id=items[-1].team_id if has_more else None,
        )

    def create_team(
        self,
        *,
        actor: ActorIdentity,
        organization_id: str,
        team_id: str,
        name: str,
        manager_user_id: str | None,
        event_id: str,
    ) -> TeamRecord:
        normalized_organization_id = _required_text(
            organization_id,
            "organization_id",
        )
        normalized_team_id = _required_text(team_id, "team_id")
        normalized_name = _required_text(name, "name")
        normalized_manager = (
            _required_text(manager_user_id, "manager_user_id")
            if manager_user_id is not None
            else None
        )
        if normalized_manager is None:
            raise ValueError("manager_user_id is required")
        normalized_event_id = _required_text(event_id, "event_id")
        try:
            with self._engine.begin() as connection:
                self._lock_active_admin(
                    connection,
                    actor=actor,
                    organization_id=normalized_organization_id,
                    write=True,
                )
                manager = self._require_active_user(
                    connection,
                    organization_id=normalized_organization_id,
                    user_id=normalized_manager,
                )
                if manager is not None and manager["organization_role"] == "org_admin":
                    raise TeamAdministrationConflict(
                        "organization administrators are outside team membership"
                    )
                connection.execute(
                    teams.insert().values(
                        organization_id=normalized_organization_id,
                        team_id=normalized_team_id,
                        name=normalized_name,
                        manager_user_id=normalized_manager,
                        status="active",
                    )
                )
                if normalized_manager is not None:
                    connection.execute(
                        team_memberships.insert().values(
                            organization_id=normalized_organization_id,
                            team_id=normalized_team_id,
                            user_id=normalized_manager,
                            role="team_lead",
                            granted_by_user_id=actor.user_id,
                        )
                    )
                self._append_audit(
                    connection,
                    AuditEvent(
                        organization_id=normalized_organization_id,
                        event_id=normalized_event_id,
                        actor_user_id=actor.user_id,
                        action="team.created",
                        target_type="team",
                        target_id=normalized_team_id,
                        details={
                            "status": "active",
                            "manager_assigned": normalized_manager is not None,
                        },
                    ),
                )
                return self._read_team(
                    connection,
                    organization_id=normalized_organization_id,
                    team_id=normalized_team_id,
                )
        except (TeamAdministrationDenied, TeamUserNotFound):
            raise
        except IntegrityError as exc:
            raise TeamAdministrationConflict(
                "team change conflicted"
            ) from exc
        except TeamAdministrationUnavailable:
            raise
        except SQLAlchemyError as exc:
            raise TeamAdministrationUnavailable(
                "team change is unavailable"
            ) from exc

    def update_team(
        self,
        *,
        actor: ActorIdentity,
        organization_id: str,
        team_id: str,
        event_id: str,
        name: str | None = None,
        status: str | None = None,
        manager_user_id: str | None = None,
        manager_user_id_set: bool = False,
    ) -> TeamRecord:
        normalized_organization_id = _required_text(
            organization_id,
            "organization_id",
        )
        normalized_team_id = _required_text(team_id, "team_id")
        normalized_event_id = _required_text(event_id, "event_id")
        normalized_name = (
            _required_text(name, "name") if name is not None else None
        )
        normalized_status = (
            _team_status(status) if status is not None else None
        )
        normalized_manager = (
            _required_text(manager_user_id, "manager_user_id")
            if manager_user_id is not None
            else None
        )
        if (
            normalized_name is None
            and normalized_status is None
            and not manager_user_id_set
        ):
            raise ValueError("at least one team field is required")
        try:
            with self._engine.begin() as connection:
                self._lock_active_admin(
                    connection,
                    actor=actor,
                    organization_id=normalized_organization_id,
                    write=True,
                )
                current = self._lock_team(
                    connection,
                    organization_id=normalized_organization_id,
                    team_id=normalized_team_id,
                )
                if manager_user_id_set:
                    manager = self._require_active_user(
                        connection,
                        organization_id=normalized_organization_id,
                        user_id=normalized_manager,
                    )
                    if manager is not None and manager["organization_role"] == "org_admin":
                        raise TeamAdministrationConflict(
                            "organization administrators are outside team membership"
                        )
                next_name = (
                    normalized_name
                    if normalized_name is not None
                    else str(current["name"])
                )
                next_status = (
                    normalized_status
                    if normalized_status is not None
                    else cast(TeamStatus, current["status"])
                )
                next_manager = (
                    normalized_manager
                    if manager_user_id_set
                    else (
                        str(current["manager_user_id"])
                        if current["manager_user_id"] is not None
                        else None
                    )
                )
                if next_status == "active" and next_manager is None:
                    raise TeamAdministrationConflict(
                        "an active team must have one lead"
                    )
                membership_changed = False
                if manager_user_id_set:
                    if next_manager is not None:
                        existing_team = connection.execute(
                            sa.select(team_memberships.c.team_id)
                            .where(
                                team_memberships.c.organization_id
                                == normalized_organization_id,
                                team_memberships.c.user_id == next_manager,
                                team_memberships.c.team_id != normalized_team_id,
                            )
                            .with_for_update(read=True)
                        ).scalar_one_or_none()
                        if existing_team is not None:
                            raise TeamAdministrationConflict(
                                "a user can belong to only one team"
                            )
                    connection.execute(
                        team_memberships.update()
                        .where(
                            team_memberships.c.organization_id
                            == normalized_organization_id,
                            team_memberships.c.team_id == normalized_team_id,
                            team_memberships.c.role == "team_lead",
                        )
                        .values(
                            role="member",
                            granted_by_user_id=actor.user_id,
                            updated_at=sa.func.now(),
                        )
                    )
                    if next_manager is not None:
                        current_target = connection.execute(
                            sa.select(team_memberships.c.user_id)
                            .where(
                                team_memberships.c.organization_id
                                == normalized_organization_id,
                                team_memberships.c.team_id == normalized_team_id,
                                team_memberships.c.user_id == next_manager,
                            )
                            .with_for_update()
                        ).scalar_one_or_none()
                        if current_target is None:
                            connection.execute(
                                team_memberships.insert().values(
                                    organization_id=normalized_organization_id,
                                    team_id=normalized_team_id,
                                    user_id=next_manager,
                                    role="team_lead",
                                    granted_by_user_id=actor.user_id,
                                )
                            )
                        else:
                            connection.execute(
                                team_memberships.update()
                                .where(
                                    team_memberships.c.organization_id
                                    == normalized_organization_id,
                                    team_memberships.c.team_id
                                    == normalized_team_id,
                                    team_memberships.c.user_id == next_manager,
                                )
                                .values(
                                    role="team_lead",
                                    granted_by_user_id=actor.user_id,
                                    updated_at=sa.func.now(),
                                )
                            )
                    membership_changed = True
                changed = {
                    "name": next_name != current["name"],
                    "status": next_status != current["status"],
                    "manager": next_manager != current["manager_user_id"],
                }
                if not any(changed.values()) and not membership_changed:
                    return self._read_team(
                        connection,
                        organization_id=normalized_organization_id,
                        team_id=normalized_team_id,
                    )
                revoked_invitation_count = 0
                if changed["manager"] or (
                    changed["status"] and next_status == "archived"
                ):
                    revoked_invitation_count = int(
                        connection.execute(
                            workspace_invitations.update()
                            .where(
                                workspace_invitations.c.organization_id
                                == normalized_organization_id,
                                workspace_invitations.c.team_id
                                == normalized_team_id,
                                workspace_invitations.c.status == "pending",
                            )
                            .values(
                                status="revoked",
                                updated_at=sa.func.now(),
                            )
                        ).rowcount
                        or 0
                    )
                connection.execute(
                    teams.update()
                    .where(
                        teams.c.organization_id
                        == normalized_organization_id,
                        teams.c.team_id == normalized_team_id,
                    )
                    .values(
                        name=next_name,
                        status=next_status,
                        manager_user_id=next_manager,
                        updated_at=sa.func.now(),
                    )
                )
                details: dict[str, object] = {
                    "name_changed": changed["name"],
                    "manager_changed": changed["manager"],
                    "pending_invitations_revoked": revoked_invitation_count,
                }
                if changed["status"]:
                    details.update(
                        {
                            "previous_status": current["status"],
                            "status": next_status,
                        }
                    )
                self._append_audit(
                    connection,
                    AuditEvent(
                        organization_id=normalized_organization_id,
                        event_id=normalized_event_id,
                        actor_user_id=actor.user_id,
                        action="team.updated",
                        target_type="team",
                        target_id=normalized_team_id,
                        details=details,
                    ),
                )
                return self._read_team(
                    connection,
                    organization_id=normalized_organization_id,
                    team_id=normalized_team_id,
                )
        except (
            TeamAdministrationDenied,
            TeamNotFound,
            TeamUserNotFound,
        ):
            raise
        except IntegrityError as exc:
            raise TeamAdministrationConflict(
                "team change conflicted"
            ) from exc
        except TeamAdministrationUnavailable:
            raise
        except SQLAlchemyError as exc:
            raise TeamAdministrationUnavailable(
                "team change is unavailable"
            ) from exc

    def list_members(
        self,
        *,
        actor: ActorIdentity,
        organization_id: str,
        team_id: str,
        limit: int = 50,
        after_user_id: str | None = None,
    ) -> TeamMemberPage:
        normalized_organization_id = _required_text(
            organization_id,
            "organization_id",
        )
        normalized_team_id = _required_text(team_id, "team_id")
        normalized_limit = int(limit)
        if normalized_limit < 1 or normalized_limit > 100:
            raise ValueError("limit must be between 1 and 100")
        normalized_after = (
            _required_text(after_user_id, "after_user_id")
            if after_user_id is not None
            else None
        )
        try:
            with self._engine.begin() as connection:
                self._lock_active_admin(
                    connection,
                    actor=actor,
                    organization_id=normalized_organization_id,
                    team_id=normalized_team_id,
                    write=False,
                )
                self._lock_team(
                    connection,
                    organization_id=normalized_organization_id,
                    team_id=normalized_team_id,
                    read=True,
                )
                statement = (
                    sa.select(
                        team_memberships.c.user_id,
                        workspace_users.c.display_name,
                        workspace_users.c.status.label("user_status"),
                        team_memberships.c.role,
                    )
                    .select_from(
                        team_memberships.join(
                            workspace_users,
                            sa.and_(
                                workspace_users.c.organization_id
                                == team_memberships.c.organization_id,
                                workspace_users.c.user_id
                                == team_memberships.c.user_id,
                            ),
                        )
                    )
                    .where(
                        team_memberships.c.organization_id
                        == normalized_organization_id,
                        team_memberships.c.team_id == normalized_team_id,
                    )
                )
                if normalized_after is not None:
                    statement = statement.where(
                        team_memberships.c.user_id > normalized_after
                    )
                rows = (
                    connection.execute(
                        statement.order_by(
                            team_memberships.c.user_id
                        ).limit(normalized_limit + 1)
                    )
                    .mappings()
                    .all()
                )
        except (TeamAdministrationDenied, TeamNotFound):
            raise
        except SQLAlchemyError as exc:
            raise TeamAdministrationUnavailable(
                "team member directory is unavailable"
            ) from exc
        has_more = len(rows) > normalized_limit
        items = tuple(
            TeamMemberRecord(
                user_id=str(row["user_id"]),
                display_name=str(row["display_name"]),
                user_status=cast(
                    Literal["active", "disabled"],
                    row["user_status"],
                ),
                role=cast(TeamMembershipRole, row["role"]),
            )
            for row in rows[:normalized_limit]
        )
        return TeamMemberPage(
            items=items,
            next_after_user_id=items[-1].user_id if has_more else None,
        )

    def upsert_member(
        self,
        *,
        actor: ActorIdentity,
        organization_id: str,
        team_id: str,
        user_id: str,
        role: str,
        event_id: str,
    ) -> TeamMemberRecord:
        normalized_organization_id = _required_text(
            organization_id,
            "organization_id",
        )
        normalized_team_id = _required_text(team_id, "team_id")
        normalized_user_id = _required_text(user_id, "user_id")
        normalized_role = _membership_role(role)
        normalized_event_id = _required_text(event_id, "event_id")
        try:
            with self._engine.begin() as connection:
                self._lock_active_admin(
                    connection,
                    actor=actor,
                    organization_id=normalized_organization_id,
                    team_id=normalized_team_id,
                    write=True,
                )
                team = self._lock_team(
                    connection,
                    organization_id=normalized_organization_id,
                    team_id=normalized_team_id,
                )
                if team["status"] != "active":
                    raise TeamAdministrationConflict(
                        "archived team membership cannot be changed"
                    )
                user = self._require_active_user(
                    connection,
                    organization_id=normalized_organization_id,
                    user_id=normalized_user_id,
                )
                if user["organization_role"] == "org_admin":
                    raise TeamAdministrationConflict(
                        "organization administrators are outside team membership"
                    )
                current_role = connection.execute(
                    sa.select(team_memberships.c.role)
                    .where(
                        team_memberships.c.organization_id
                        == normalized_organization_id,
                        team_memberships.c.team_id == normalized_team_id,
                        team_memberships.c.user_id == normalized_user_id,
                    )
                    .with_for_update()
                ).scalar_one_or_none()
                if current_role == normalized_role:
                    return TeamMemberRecord(
                        user_id=normalized_user_id,
                        display_name=str(user["display_name"]),
                        user_status="active",
                        role=normalized_role,
                    )
                existing_team = connection.execute(
                    sa.select(team_memberships.c.team_id)
                    .where(
                        team_memberships.c.organization_id
                        == normalized_organization_id,
                        team_memberships.c.user_id == normalized_user_id,
                        team_memberships.c.team_id != normalized_team_id,
                    )
                    .with_for_update(read=True)
                ).scalar_one_or_none()
                if existing_team is not None:
                    raise TeamAdministrationConflict(
                        "a user can belong to only one team"
                    )
                if normalized_role == "team_lead":
                    other_lead = connection.execute(
                        sa.select(team_memberships.c.user_id)
                        .where(
                            team_memberships.c.organization_id
                            == normalized_organization_id,
                            team_memberships.c.team_id == normalized_team_id,
                            team_memberships.c.role == "team_lead",
                            team_memberships.c.user_id
                            != normalized_user_id,
                        )
                        .with_for_update(read=True)
                    ).scalar_one_or_none()
                    if other_lead is not None:
                        raise TeamAdministrationConflict(
                            "a team can have only one active lead"
                        )
                if current_role == "team_lead" and normalized_role != "team_lead":
                    other_lead = connection.execute(
                        sa.select(team_memberships.c.user_id)
                        .where(
                            team_memberships.c.organization_id
                            == normalized_organization_id,
                            team_memberships.c.team_id == normalized_team_id,
                            team_memberships.c.role == "team_lead",
                            team_memberships.c.user_id != normalized_user_id,
                        )
                        .with_for_update(read=True)
                    ).scalar_one_or_none()
                    if other_lead is None:
                        raise TeamAdministrationConflict(
                            "an active team must have one lead"
                        )
                if current_role is None:
                    connection.execute(
                        team_memberships.insert().values(
                            organization_id=normalized_organization_id,
                            team_id=normalized_team_id,
                            user_id=normalized_user_id,
                            role=normalized_role,
                            granted_by_user_id=actor.user_id,
                        )
                    )
                    action = "team.membership.granted"
                    details: dict[str, object] = {
                        "team_id": normalized_team_id,
                        "role": normalized_role,
                    }
                else:
                    connection.execute(
                        team_memberships.update()
                        .where(
                            team_memberships.c.organization_id
                            == normalized_organization_id,
                            team_memberships.c.team_id
                            == normalized_team_id,
                            team_memberships.c.user_id == normalized_user_id,
                        )
                        .values(
                            role=normalized_role,
                            granted_by_user_id=actor.user_id,
                            updated_at=sa.func.now(),
                        )
                    )
                    action = "team.membership.updated"
                    details = {
                        "team_id": normalized_team_id,
                        "previous_role": current_role,
                        "role": normalized_role,
                    }
                if normalized_role == "team_lead":
                    next_manager = normalized_user_id
                elif current_role == "team_lead":
                    next_manager = connection.execute(
                        sa.select(team_memberships.c.user_id)
                        .where(
                            team_memberships.c.organization_id
                            == normalized_organization_id,
                            team_memberships.c.team_id == normalized_team_id,
                            team_memberships.c.role == "team_lead",
                            team_memberships.c.user_id != normalized_user_id,
                        )
                        .with_for_update(read=True)
                    ).scalar_one_or_none()
                else:
                    next_manager = team["manager_user_id"]
                connection.execute(
                    teams.update()
                    .where(
                        teams.c.organization_id == normalized_organization_id,
                        teams.c.team_id == normalized_team_id,
                    )
                    .values(
                        manager_user_id=next_manager,
                        updated_at=sa.func.now(),
                    )
                )
                if current_role == "team_lead" and normalized_role != "team_lead":
                    connection.execute(
                        workspace_invitations.update()
                        .where(
                            workspace_invitations.c.organization_id
                            == normalized_organization_id,
                            workspace_invitations.c.team_id
                            == normalized_team_id,
                            workspace_invitations.c.status == "pending",
                        )
                        .values(
                            status="revoked",
                            updated_at=sa.func.now(),
                        )
                    )
                self._append_audit(
                    connection,
                    AuditEvent(
                        organization_id=normalized_organization_id,
                        event_id=normalized_event_id,
                        actor_user_id=actor.user_id,
                        action=action,
                        target_type="workspace_user",
                        target_id=normalized_user_id,
                        details=details,
                    ),
                )
                return TeamMemberRecord(
                    user_id=normalized_user_id,
                    display_name=str(user["display_name"]),
                    user_status="active",
                    role=normalized_role,
                )
        except (
            TeamAdministrationConflict,
            TeamAdministrationDenied,
            TeamNotFound,
            TeamUserNotFound,
        ):
            raise
        except IntegrityError as exc:
            raise TeamAdministrationConflict(
                "team membership change conflicted"
            ) from exc
        except TeamAdministrationUnavailable:
            raise
        except SQLAlchemyError as exc:
            raise TeamAdministrationUnavailable(
                "team membership change is unavailable"
            ) from exc

    def revoke_member(
        self,
        *,
        actor: ActorIdentity,
        organization_id: str,
        team_id: str,
        user_id: str,
        event_id: str,
    ) -> bool:
        normalized_organization_id = _required_text(
            organization_id,
            "organization_id",
        )
        normalized_team_id = _required_text(team_id, "team_id")
        normalized_user_id = _required_text(user_id, "user_id")
        normalized_event_id = _required_text(event_id, "event_id")
        try:
            with self._engine.begin() as connection:
                self._lock_active_admin(
                    connection,
                    actor=actor,
                    organization_id=normalized_organization_id,
                    team_id=normalized_team_id,
                    write=True,
                )
                team = self._lock_team(
                    connection,
                    organization_id=normalized_organization_id,
                    team_id=normalized_team_id,
                )
                membership = connection.execute(
                    sa.select(team_memberships.c.role)
                    .where(
                        team_memberships.c.organization_id
                        == normalized_organization_id,
                        team_memberships.c.team_id == normalized_team_id,
                        team_memberships.c.user_id == normalized_user_id,
                    )
                    .with_for_update()
                ).scalar_one_or_none()
                if membership is None:
                    return False
                replacement_lead: str | None = None
                if membership == "team_lead" and team["status"] == "active":
                    replacement_lead = connection.execute(
                        sa.select(team_memberships.c.user_id)
                        .where(
                            team_memberships.c.organization_id
                            == normalized_organization_id,
                            team_memberships.c.team_id == normalized_team_id,
                            team_memberships.c.role == "team_lead",
                            team_memberships.c.user_id != normalized_user_id,
                        )
                        .with_for_update(read=True)
                    ).scalar_one_or_none()
                    if replacement_lead is None:
                        raise TeamAdministrationConflict(
                            "an active team must have one lead"
                        )
                connection.execute(
                    project_ownership.update()
                    .where(
                        project_ownership.c.organization_id
                        == normalized_organization_id,
                        project_ownership.c.owner_user_id == normalized_user_id,
                    )
                    .values(owner_user_id=None, updated_at=sa.func.now())
                )
                connection.execute(
                    project_memberships.delete().where(
                        project_memberships.c.organization_id
                        == normalized_organization_id,
                        project_memberships.c.user_id == normalized_user_id,
                    )
                )
                connection.execute(
                    team_memberships.delete().where(
                        team_memberships.c.organization_id
                        == normalized_organization_id,
                        team_memberships.c.team_id == normalized_team_id,
                        team_memberships.c.user_id == normalized_user_id,
                    )
                )
                if membership == "team_lead":
                    connection.execute(
                        teams.update()
                        .where(
                            teams.c.organization_id
                            == normalized_organization_id,
                            teams.c.team_id == normalized_team_id,
                        )
                        .values(
                            manager_user_id=replacement_lead,
                            updated_at=sa.func.now(),
                        )
                    )
                    connection.execute(
                        workspace_invitations.update()
                        .where(
                            workspace_invitations.c.organization_id
                            == normalized_organization_id,
                            workspace_invitations.c.team_id
                            == normalized_team_id,
                            workspace_invitations.c.status == "pending",
                        )
                        .values(
                            status="revoked",
                            updated_at=sa.func.now(),
                        )
                    )
                self._append_audit(
                    connection,
                    AuditEvent(
                        organization_id=normalized_organization_id,
                        event_id=normalized_event_id,
                        actor_user_id=actor.user_id,
                        action="team.membership.revoked",
                        target_type="workspace_user",
                        target_id=normalized_user_id,
                        details={
                            "team_id": normalized_team_id,
                            "previous_role": membership,
                        },
                    ),
                )
                return True
        except (TeamAdministrationDenied, TeamNotFound):
            raise
        except IntegrityError as exc:
            raise TeamAdministrationConflict(
                "team membership change conflicted"
            ) from exc
        except TeamAdministrationUnavailable:
            raise
        except SQLAlchemyError as exc:
            raise TeamAdministrationUnavailable(
                "team membership change is unavailable"
            ) from exc

    def _lock_active_admin(
        self,
        connection: Connection,
        *,
        actor: ActorIdentity,
        organization_id: str,
        team_id: str | None = None,
        write: bool,
        allow_any_team_lead: bool = False,
    ) -> Literal["org_admin", "team_lead"]:
        if actor.organization_id != organization_id:
            raise TeamAdministrationDenied("team administration denied")
        organization_exists = connection.execute(
            sa.select(organizations.c.organization_id)
            .where(
                organizations.c.organization_id == organization_id,
                organizations.c.status == "active",
            )
            .with_for_update(read=not write)
        ).scalar_one_or_none()
        if organization_exists is None:
            raise TeamAdministrationDenied("team administration denied")
        actor_exists = connection.execute(
            sa.select(
                workspace_users.c.user_id,
                workspace_users.c.organization_role,
            )
            .where(
                workspace_users.c.organization_id == organization_id,
                workspace_users.c.user_id == actor.user_id,
                workspace_users.c.status == "active",
            )
            .with_for_update(read=True)
        ).mappings().one_or_none()
        if actor_exists is None:
            raise TeamAdministrationDenied("team administration denied")
        if actor_exists["organization_role"] == "org_admin":
            return "org_admin"
        if team_id is not None:
            lead_exists = connection.execute(
                sa.select(team_memberships.c.user_id)
                .select_from(
                    team_memberships.join(
                        teams,
                        sa.and_(
                            teams.c.organization_id
                            == team_memberships.c.organization_id,
                            teams.c.team_id == team_memberships.c.team_id,
                        ),
                    )
                )
                .where(
                    team_memberships.c.organization_id == organization_id,
                    team_memberships.c.team_id == team_id,
                    team_memberships.c.user_id == actor.user_id,
                    team_memberships.c.role == "team_lead",
                    teams.c.status == "active",
                )
                .with_for_update(read=True)
            ).scalar_one_or_none()
            if lead_exists is not None:
                return "team_lead"
        elif allow_any_team_lead:
            lead_exists = connection.execute(
                sa.select(team_memberships.c.team_id)
                .select_from(
                    team_memberships.join(
                        teams,
                        sa.and_(
                            teams.c.organization_id
                            == team_memberships.c.organization_id,
                            teams.c.team_id == team_memberships.c.team_id,
                        ),
                    )
                )
                .where(
                    team_memberships.c.organization_id == organization_id,
                    team_memberships.c.user_id == actor.user_id,
                    team_memberships.c.role == "team_lead",
                    teams.c.status == "active",
                )
                .limit(1)
                .with_for_update(read=True)
            ).scalar_one_or_none()
            if lead_exists is not None:
                return "team_lead"
        raise TeamAdministrationDenied("team administration denied")

    @staticmethod
    def _lock_team(
        connection: Connection,
        *,
        organization_id: str,
        team_id: str,
        read: bool = False,
    ) -> RowMapping:
        team = connection.execute(
            sa.select(
                teams.c.name,
                teams.c.status,
                teams.c.manager_user_id,
            )
            .where(
                teams.c.organization_id == organization_id,
                teams.c.team_id == team_id,
            )
            .with_for_update(read=read)
        ).mappings().one_or_none()
        if team is None:
            raise TeamNotFound("team is unavailable")
        return team

    @staticmethod
    def _require_active_user(
        connection: Connection,
        *,
        organization_id: str,
        user_id: str | None,
    ) -> RowMapping | None:
        if user_id is None:
            return None
        user = connection.execute(
            sa.select(
                workspace_users.c.user_id,
                workspace_users.c.display_name,
                workspace_users.c.organization_role,
            )
            .where(
                workspace_users.c.organization_id == organization_id,
                workspace_users.c.user_id == user_id,
                workspace_users.c.status == "active",
            )
            .with_for_update(read=True)
        ).mappings().one_or_none()
        if user is None:
            raise TeamUserNotFound("team user is unavailable")
        return user

    @staticmethod
    def _team_select(*, organization_id: str) -> sa.Select:
        member_count = (
            sa.select(sa.func.count())
            .select_from(team_memberships)
            .where(
                team_memberships.c.organization_id
                == teams.c.organization_id,
                team_memberships.c.team_id == teams.c.team_id,
            )
            .correlate(teams)
            .scalar_subquery()
        )
        lead_count = (
            sa.select(sa.func.count())
            .select_from(team_memberships)
            .where(
                team_memberships.c.organization_id
                == teams.c.organization_id,
                team_memberships.c.team_id == teams.c.team_id,
                team_memberships.c.role == "team_lead",
            )
            .correlate(teams)
            .scalar_subquery()
        )
        project_count = (
            sa.select(sa.func.count())
            .select_from(
                project_ownership.join(
                    projects,
                    projects.c.project_id == project_ownership.c.project_id,
                )
            )
            .where(
                project_ownership.c.organization_id
                == teams.c.organization_id,
                project_ownership.c.owning_team_id == teams.c.team_id,
            )
            .correlate(teams)
            .scalar_subquery()
        )
        return sa.select(
            teams.c.team_id,
            teams.c.name,
            teams.c.manager_user_id,
            teams.c.status,
            member_count.label("member_count"),
            lead_count.label("team_lead_count"),
            project_count.label("project_count"),
        ).where(teams.c.organization_id == organization_id)

    def _read_team(
        self,
        connection: Connection,
        *,
        organization_id: str,
        team_id: str,
    ) -> TeamRecord:
        row = connection.execute(
            self._team_select(organization_id=organization_id).where(
                teams.c.team_id == team_id
            )
        ).mappings().one_or_none()
        if row is None:
            raise TeamNotFound("team is unavailable")
        return self._team_record(row)

    @staticmethod
    def _team_record(row: RowMapping) -> TeamRecord:
        return TeamRecord(
            team_id=str(row["team_id"]),
            name=str(row["name"]),
            manager_user_id=(
                str(row["manager_user_id"])
                if row["manager_user_id"] is not None
                else None
            ),
            status=cast(TeamStatus, row["status"]),
            member_count=int(row["member_count"]),
            team_lead_count=int(row["team_lead_count"]),
            project_count=int(row["project_count"]),
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
            raise TeamAdministrationUnavailable(
                "team change is unavailable"
            ) from exc


__all__ = [
    "PostgresTeamAdministrationService",
    "TeamAdministrationConflict",
    "TeamAdministrationDenied",
    "TeamAdministrationError",
    "TeamAdministrationUnavailable",
    "TeamMemberPage",
    "TeamMemberRecord",
    "TeamMembershipRole",
    "TeamNotFound",
    "TeamPage",
    "TeamRecord",
    "TeamStatus",
    "TeamUserNotFound",
]
