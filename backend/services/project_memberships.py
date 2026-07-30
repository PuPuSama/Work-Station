from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from server_schema import (
    project_memberships,
    project_ownership,
    team_memberships,
    teams,
    workspace_users,
)
from services.access_control import (
    ActorIdentity,
    PostgresProjectAccessRepository,
    ProjectAccessDenied,
    ProjectRole,
    decide_project_permission,
)
from services.audit_log import (
    AuditEvent,
    AuditEventWriter,
    PostgresAuditEventWriter,
)


class ProjectMembershipError(RuntimeError):
    """Base error for audited ProjectMembership mutations."""


class ProjectMembershipTargetUnavailable(ProjectMembershipError):
    """Raised without disclosing cross-organization user state."""


class ProjectMembershipConflict(ProjectMembershipError):
    """Raised when a stable audit event identity is reused."""


class ProjectMembershipUnavailable(ProjectMembershipError):
    """A dependency failed without exposing its private error."""


def _required_text(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized


def _project_role(value: str) -> ProjectRole:
    normalized = _required_text(value, "role")
    if normalized not in {"editor", "reviewer", "viewer"}:
        raise ValueError("role must be editor, reviewer, or viewer")
    return cast(ProjectRole, normalized)


@dataclass(frozen=True)
class ProjectMembershipRecord:
    organization_id: str
    project_id: str
    user_id: str
    role: ProjectRole
    granted_by_user_id: str


@dataclass(frozen=True)
class ProjectMembershipListItem:
    """One explicit membership; inherited access is intentionally separate."""

    user_id: str
    display_name: str
    status: Literal["active", "disabled"]
    role: ProjectRole


@dataclass(frozen=True)
class ProjectMembershipPage:
    items: tuple[ProjectMembershipListItem, ...]
    next_after_user_id: str | None


@dataclass(frozen=True)
class ProjectMembershipCandidate:
    """An active Organization member who lacks effective Project access."""

    user_id: str
    display_name: str


@dataclass(frozen=True)
class ProjectMembershipCandidatePage:
    items: tuple[ProjectMembershipCandidate, ...]
    next_after_user_id: str | None


class PostgresProjectMembershipService:
    """Mutate ProjectMembership and its AuditEvent atomically."""

    def __init__(
        self,
        engine: Engine,
        *,
        audit: AuditEventWriter | None = None,
    ) -> None:
        self._engine = engine
        self._access = PostgresProjectAccessRepository(engine)
        self._audit = audit or PostgresAuditEventWriter()

    def list_members(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        limit: int = 50,
        after_user_id: str | None = None,
    ) -> ProjectMembershipPage:
        """Return a bounded, deterministic explicit-membership read model."""

        normalized_project_id = _required_text(project_id, "project_id")
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
                facts = self._access.lock_project_access_in_connection(
                    connection,
                    actor,
                    normalized_project_id,
                )
                decision = decide_project_permission(
                    facts,
                    "project.members.manage",
                )
                if not decision.allowed:
                    raise ProjectAccessDenied("project access denied")

                statement = (
                    sa.select(
                        project_memberships.c.user_id,
                        workspace_users.c.display_name,
                        workspace_users.c.status,
                        project_memberships.c.role,
                    )
                    .select_from(
                        project_memberships.join(
                            workspace_users,
                            sa.and_(
                                workspace_users.c.organization_id
                                == project_memberships.c.organization_id,
                                workspace_users.c.user_id
                                == project_memberships.c.user_id,
                            ),
                        )
                    )
                    .where(
                        project_memberships.c.organization_id
                        == actor.organization_id,
                        project_memberships.c.project_id
                        == normalized_project_id,
                    )
                    .order_by(project_memberships.c.user_id)
                    .limit(normalized_limit + 1)
                )
                if normalized_after is not None:
                    statement = statement.where(
                        project_memberships.c.user_id > normalized_after
                    )
                rows = connection.execute(statement).mappings().all()
        except ProjectAccessDenied:
            raise
        except SQLAlchemyError as exc:
            raise ProjectMembershipUnavailable(
                "project membership list is unavailable"
            ) from exc

        has_more = len(rows) > normalized_limit
        visible_rows = rows[:normalized_limit]
        items = tuple(
            ProjectMembershipListItem(
                user_id=str(row["user_id"]),
                display_name=str(row["display_name"]),
                status=cast(
                    Literal["active", "disabled"],
                    row["status"],
                ),
                role=cast(ProjectRole, row["role"]),
            )
            for row in visible_rows
        )
        return ProjectMembershipPage(
            items=items,
            next_after_user_id=items[-1].user_id if has_more else None,
        )

    def list_candidates(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        limit: int = 50,
        after_user_id: str | None = None,
    ) -> ProjectMembershipCandidatePage:
        """Return active users who do not already have effective access."""

        normalized_project_id = _required_text(project_id, "project_id")
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
                facts = self._access.lock_project_access_in_connection(
                    connection,
                    actor,
                    normalized_project_id,
                )
                decision = decide_project_permission(
                    facts,
                    "project.members.manage",
                )
                if not decision.allowed:
                    raise ProjectAccessDenied("project access denied")

                owning_team_id = connection.execute(
                    sa.select(project_ownership.c.owning_team_id).where(
                        project_ownership.c.organization_id
                        == actor.organization_id,
                        project_ownership.c.project_id
                        == normalized_project_id,
                    )
                ).scalar_one()
                explicit_membership_exists = sa.exists(
                    sa.select(project_memberships.c.user_id).where(
                        project_memberships.c.organization_id
                        == workspace_users.c.organization_id,
                        project_memberships.c.project_id
                        == normalized_project_id,
                        project_memberships.c.user_id
                        == workspace_users.c.user_id,
                    )
                )
                inherited_team_lead_exists = (
                    sa.false()
                    if owning_team_id is None
                    else sa.exists(
                        sa.select(team_memberships.c.user_id)
                        .select_from(
                            team_memberships.join(
                                teams,
                                sa.and_(
                                    teams.c.organization_id
                                    == team_memberships.c.organization_id,
                                    teams.c.team_id
                                    == team_memberships.c.team_id,
                                ),
                            )
                        )
                        .where(
                            team_memberships.c.organization_id
                            == workspace_users.c.organization_id,
                            team_memberships.c.team_id == owning_team_id,
                            team_memberships.c.user_id
                            == workspace_users.c.user_id,
                            team_memberships.c.role == "team_lead",
                            teams.c.status == "active",
                        )
                    )
                )
                statement = (
                    sa.select(
                        workspace_users.c.user_id,
                        workspace_users.c.display_name,
                    )
                    .where(
                        workspace_users.c.organization_id
                        == actor.organization_id,
                        workspace_users.c.status == "active",
                        workspace_users.c.organization_role == "member",
                        ~explicit_membership_exists,
                        ~inherited_team_lead_exists,
                    )
                    .order_by(workspace_users.c.user_id)
                    .limit(normalized_limit + 1)
                )
                if normalized_after is not None:
                    statement = statement.where(
                        workspace_users.c.user_id > normalized_after
                    )
                rows = connection.execute(statement).mappings().all()
        except ProjectAccessDenied:
            raise
        except SQLAlchemyError as exc:
            raise ProjectMembershipUnavailable(
                "project membership candidate list is unavailable"
            ) from exc

        has_more = len(rows) > normalized_limit
        visible_rows = rows[:normalized_limit]
        items = tuple(
            ProjectMembershipCandidate(
                user_id=str(row["user_id"]),
                display_name=str(row["display_name"]),
            )
            for row in visible_rows
        )
        return ProjectMembershipCandidatePage(
            items=items,
            next_after_user_id=items[-1].user_id if has_more else None,
        )

    def grant(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        target_user_id: str,
        role: str,
        event_id: str,
    ) -> ProjectMembershipRecord:
        normalized_project_id = _required_text(project_id, "project_id")
        normalized_target = _required_text(target_user_id, "target_user_id")
        normalized_event_id = _required_text(event_id, "event_id")
        normalized_role = _project_role(role)

        try:
            with self._engine.begin() as connection:
                return self.grant_in_transaction(
                    connection,
                    actor=actor,
                    project_id=normalized_project_id,
                    target_user_id=normalized_target,
                    role=normalized_role,
                    event_id=normalized_event_id,
                )
        except IntegrityError as exc:
            raise ProjectMembershipConflict(
                "project membership change conflicted"
            ) from exc

    def grant_in_transaction(
        self,
        connection: Connection,
        *,
        actor: ActorIdentity,
        project_id: str,
        target_user_id: str,
        role: str,
        event_id: str,
    ) -> ProjectMembershipRecord:
        if not connection.in_transaction():
            raise ValueError(
                "project membership changes require a business transaction"
            )
        normalized_project_id = _required_text(project_id, "project_id")
        normalized_target = _required_text(target_user_id, "target_user_id")
        normalized_event_id = _required_text(event_id, "event_id")
        normalized_role = _project_role(role)

        facts = self._access.lock_project_access_in_connection(
            connection,
            actor,
            normalized_project_id,
        )
        decision = decide_project_permission(
            facts,
            "project.members.manage",
        )
        if not decision.allowed:
            raise ProjectAccessDenied("project access denied")
        target_exists = connection.execute(
            sa.select(workspace_users.c.user_id).where(
                workspace_users.c.organization_id == actor.organization_id,
                workspace_users.c.user_id == normalized_target,
                workspace_users.c.status == "active",
            )
            .with_for_update()
        ).scalar_one_or_none()
        if target_exists is None:
            raise ProjectMembershipTargetUnavailable(
                "project member target unavailable"
            )

        statement = insert(project_memberships).values(
            organization_id=actor.organization_id,
            project_id=normalized_project_id,
            user_id=normalized_target,
            role=normalized_role,
            granted_by_user_id=actor.user_id,
        )
        statement = statement.on_conflict_do_update(
            index_elements=[
                project_memberships.c.organization_id,
                project_memberships.c.project_id,
                project_memberships.c.user_id,
            ],
            set_={
                "role": statement.excluded.role,
                "granted_by_user_id": statement.excluded.granted_by_user_id,
                "granted_at": sa.func.now(),
                "updated_at": sa.func.now(),
            },
        )
        connection.execute(statement)
        self._append_audit(
            connection,
            AuditEvent(
                organization_id=actor.organization_id,
                event_id=normalized_event_id,
                actor_user_id=actor.user_id,
                project_id=normalized_project_id,
                action="project.membership.granted",
                target_type="project_membership",
                target_id=normalized_target,
                details={"role": normalized_role},
            ),
        )
        return ProjectMembershipRecord(
            organization_id=actor.organization_id,
            project_id=normalized_project_id,
            user_id=normalized_target,
            role=normalized_role,
            granted_by_user_id=actor.user_id,
        )

    def revoke(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        target_user_id: str,
        event_id: str,
    ) -> bool:
        normalized_project_id = _required_text(project_id, "project_id")
        normalized_target = _required_text(target_user_id, "target_user_id")
        normalized_event_id = _required_text(event_id, "event_id")

        try:
            with self._engine.begin() as connection:
                return self.revoke_in_transaction(
                    connection,
                    actor=actor,
                    project_id=normalized_project_id,
                    target_user_id=normalized_target,
                    event_id=normalized_event_id,
                )
        except IntegrityError as exc:
            raise ProjectMembershipConflict(
                "project membership change conflicted"
            ) from exc

    def revoke_in_transaction(
        self,
        connection: Connection,
        *,
        actor: ActorIdentity,
        project_id: str,
        target_user_id: str,
        event_id: str,
    ) -> bool:
        if not connection.in_transaction():
            raise ValueError(
                "project membership changes require a business transaction"
            )
        normalized_project_id = _required_text(project_id, "project_id")
        normalized_target = _required_text(target_user_id, "target_user_id")
        normalized_event_id = _required_text(event_id, "event_id")

        facts = self._access.lock_project_access_in_connection(
            connection,
            actor,
            normalized_project_id,
        )
        decision = decide_project_permission(
            facts,
            "project.members.manage",
        )
        if not decision.allowed:
            raise ProjectAccessDenied("project access denied")
        existing_role = connection.execute(
            sa.select(project_memberships.c.role).where(
                project_memberships.c.organization_id
                == actor.organization_id,
                project_memberships.c.project_id == normalized_project_id,
                project_memberships.c.user_id == normalized_target,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if existing_role is None:
            return False
        connection.execute(
            project_memberships.delete().where(
                project_memberships.c.organization_id == actor.organization_id,
                project_memberships.c.project_id == normalized_project_id,
                project_memberships.c.user_id == normalized_target,
            )
        )
        self._append_audit(
            connection,
            AuditEvent(
                organization_id=actor.organization_id,
                event_id=normalized_event_id,
                actor_user_id=actor.user_id,
                project_id=normalized_project_id,
                action="project.membership.revoked",
                target_type="project_membership",
                target_id=normalized_target,
                details={"previous_role": str(existing_role)},
            ),
        )
        return True

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
            raise ProjectMembershipUnavailable(
                "project membership change is unavailable"
            ) from exc


__all__ = [
    "PostgresProjectMembershipService",
    "ProjectMembershipConflict",
    "ProjectMembershipCandidate",
    "ProjectMembershipCandidatePage",
    "ProjectMembershipError",
    "ProjectMembershipListItem",
    "ProjectMembershipPage",
    "ProjectMembershipRecord",
    "ProjectMembershipTargetUnavailable",
    "ProjectMembershipUnavailable",
]
