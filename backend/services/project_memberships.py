from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from server_schema import (
    project_memberships,
    workspace_users,
)
from services.access_control import (
    ActorIdentity,
    PostgresProjectAccessRepository,
    ProjectAccessDenied,
    ProjectRole,
    decide_project_permission,
)
from services.audit_log import AuditEvent, PostgresAuditEventWriter


class ProjectMembershipError(RuntimeError):
    """Base error for audited ProjectMembership mutations."""


class ProjectMembershipTargetUnavailable(ProjectMembershipError):
    """Raised without disclosing cross-organization user state."""


class ProjectMembershipConflict(ProjectMembershipError):
    """Raised when a stable audit event identity is reused."""


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


class PostgresProjectMembershipService:
    """Mutate ProjectMembership and its AuditEvent atomically."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._access = PostgresProjectAccessRepository(engine)
        self._audit = PostgresAuditEventWriter()

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

        facts = self._access.resolve_project_access_in_connection(
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
        self._audit.append(
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

        facts = self._access.resolve_project_access_in_connection(
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
        self._audit.append(
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


__all__ = [
    "PostgresProjectMembershipService",
    "ProjectMembershipConflict",
    "ProjectMembershipError",
    "ProjectMembershipRecord",
    "ProjectMembershipTargetUnavailable",
]
