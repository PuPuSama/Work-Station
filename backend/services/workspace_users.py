from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine, RowMapping
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from server_schema import (
    external_identities,
    organizations,
    project_memberships,
    team_memberships,
    workspace_users,
)
from services.access_control import ActorIdentity
from services.audit_log import (
    AuditEvent,
    AuditEventWriter,
    PostgresAuditEventWriter,
)


WorkspaceUserStatus = Literal["active", "disabled"]
WorkspaceOrganizationRole = Literal["org_admin", "member"]


class WorkspaceUserError(RuntimeError):
    """Base error for Organization-scoped user administration."""


class WorkspaceUserDenied(WorkspaceUserError):
    """The Actor is not an active administrator of this Organization."""


class WorkspaceUserNotFound(WorkspaceUserError):
    """The target is unavailable without disclosing another Organization."""


class WorkspaceUserConflict(WorkspaceUserError):
    """A user or stable audit identity conflicts with committed state."""


class WorkspaceUserLastAdmin(WorkspaceUserError):
    """The change would leave no active Organization administrator."""


class WorkspaceUserUnavailable(WorkspaceUserError):
    """A private dependency failed and must not leak through HTTP."""


def _required_text(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized


def _status(value: str) -> WorkspaceUserStatus:
    normalized = _required_text(value, "status")
    if normalized not in {"active", "disabled"}:
        raise ValueError("status must be active or disabled")
    return cast(WorkspaceUserStatus, normalized)


def _organization_role(value: str) -> WorkspaceOrganizationRole:
    normalized = _required_text(value, "organization_role")
    if normalized not in {"org_admin", "member"}:
        raise ValueError("organization_role must be org_admin or member")
    return cast(WorkspaceOrganizationRole, normalized)


@dataclass(frozen=True)
class WorkspaceUserRecord:
    user_id: str
    display_name: str
    status: WorkspaceUserStatus
    organization_role: WorkspaceOrganizationRole
    team_membership_count: int
    project_membership_count: int
    login_linked: bool


@dataclass(frozen=True)
class WorkspaceUserPage:
    items: tuple[WorkspaceUserRecord, ...]
    next_after_user_id: str | None


class PostgresWorkspaceUserService:
    """Organization user read/write model with audit and session revocation."""

    def __init__(
        self,
        engine: Engine,
        *,
        audit: AuditEventWriter | None = None,
    ) -> None:
        self._engine = engine
        self._audit = audit or PostgresAuditEventWriter()

    def list_users(
        self,
        *,
        actor: ActorIdentity,
        organization_id: str,
        limit: int = 50,
        after_user_id: str | None = None,
    ) -> WorkspaceUserPage:
        normalized_organization_id = _required_text(
            organization_id,
            "organization_id",
        )
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
                    write=False,
                )
                rows = self._select_users(
                    connection,
                    organization_id=normalized_organization_id,
                    limit=normalized_limit + 1,
                    after_user_id=normalized_after,
                )
        except WorkspaceUserDenied:
            raise
        except SQLAlchemyError as exc:
            raise WorkspaceUserUnavailable(
                "workspace user directory is unavailable"
            ) from exc

        has_more = len(rows) > normalized_limit
        items = tuple(
            self._record_from_row(row)
            for row in rows[:normalized_limit]
        )
        return WorkspaceUserPage(
            items=items,
            next_after_user_id=items[-1].user_id if has_more else None,
        )

    def create_user(
        self,
        *,
        actor: ActorIdentity,
        organization_id: str,
        user_id: str,
        display_name: str,
        organization_role: str,
        event_id: str,
    ) -> WorkspaceUserRecord:
        try:
            with self._engine.begin() as connection:
                return self.create_user_in_transaction(
                    connection,
                    actor=actor,
                    organization_id=organization_id,
                    user_id=user_id,
                    display_name=display_name,
                    organization_role=organization_role,
                    event_id=event_id,
                )
        except WorkspaceUserDenied:
            raise
        except IntegrityError as exc:
            raise WorkspaceUserConflict(
                "workspace user change conflicted"
            ) from exc
        except WorkspaceUserUnavailable:
            raise
        except SQLAlchemyError as exc:
            raise WorkspaceUserUnavailable(
                "workspace user change is unavailable"
            ) from exc

    def create_user_in_transaction(
        self,
        connection: Connection,
        *,
        actor: ActorIdentity,
        organization_id: str,
        user_id: str,
        display_name: str,
        organization_role: str,
        event_id: str,
    ) -> WorkspaceUserRecord:
        if not connection.in_transaction():
            raise ValueError(
                "workspace user changes require a business transaction"
            )
        normalized_organization_id = _required_text(
            organization_id,
            "organization_id",
        )
        normalized_user_id = _required_text(user_id, "user_id")
        normalized_display_name = _required_text(
            display_name,
            "display_name",
        )
        normalized_role = _organization_role(organization_role)
        normalized_event_id = _required_text(event_id, "event_id")

        self._lock_active_admin(
            connection,
            actor=actor,
            organization_id=normalized_organization_id,
            write=True,
        )
        connection.execute(
            workspace_users.insert().values(
                organization_id=normalized_organization_id,
                user_id=normalized_user_id,
                display_name=normalized_display_name,
                status="active",
                organization_role=normalized_role,
            )
        )
        self._append_audit(
            connection,
            AuditEvent(
                organization_id=normalized_organization_id,
                event_id=normalized_event_id,
                actor_user_id=actor.user_id,
                action="workspace_user.created",
                target_type="workspace_user",
                target_id=normalized_user_id,
                details={
                    "status": "active",
                    "organization_role": normalized_role,
                },
            ),
        )
        return WorkspaceUserRecord(
            user_id=normalized_user_id,
            display_name=normalized_display_name,
            status="active",
            organization_role=normalized_role,
            team_membership_count=0,
            project_membership_count=0,
            login_linked=False,
        )

    def update_user(
        self,
        *,
        actor: ActorIdentity,
        organization_id: str,
        user_id: str,
        event_id: str,
        display_name: str | None = None,
        status: str | None = None,
        organization_role: str | None = None,
    ) -> WorkspaceUserRecord:
        try:
            with self._engine.begin() as connection:
                return self.update_user_in_transaction(
                    connection,
                    actor=actor,
                    organization_id=organization_id,
                    user_id=user_id,
                    event_id=event_id,
                    display_name=display_name,
                    status=status,
                    organization_role=organization_role,
                )
        except (
            WorkspaceUserDenied,
            WorkspaceUserLastAdmin,
            WorkspaceUserNotFound,
        ):
            raise
        except IntegrityError as exc:
            raise WorkspaceUserConflict(
                "workspace user change conflicted"
            ) from exc
        except WorkspaceUserUnavailable:
            raise
        except SQLAlchemyError as exc:
            raise WorkspaceUserUnavailable(
                "workspace user change is unavailable"
            ) from exc

    def update_user_in_transaction(
        self,
        connection: Connection,
        *,
        actor: ActorIdentity,
        organization_id: str,
        user_id: str,
        event_id: str,
        display_name: str | None = None,
        status: str | None = None,
        organization_role: str | None = None,
    ) -> WorkspaceUserRecord:
        if not connection.in_transaction():
            raise ValueError(
                "workspace user changes require a business transaction"
            )
        normalized_organization_id = _required_text(
            organization_id,
            "organization_id",
        )
        normalized_user_id = _required_text(user_id, "user_id")
        normalized_event_id = _required_text(event_id, "event_id")
        normalized_display_name = (
            _required_text(display_name, "display_name")
            if display_name is not None
            else None
        )
        normalized_status = _status(status) if status is not None else None
        normalized_role = (
            _organization_role(organization_role)
            if organization_role is not None
            else None
        )
        if (
            normalized_display_name is None
            and normalized_status is None
            and normalized_role is None
        ):
            raise ValueError("at least one workspace user field is required")

        self._lock_active_admin(
            connection,
            actor=actor,
            organization_id=normalized_organization_id,
            write=True,
        )
        current = connection.execute(
            sa.select(
                workspace_users.c.display_name,
                workspace_users.c.status,
                workspace_users.c.organization_role,
                workspace_users.c.session_version,
            )
            .where(
                workspace_users.c.organization_id
                == normalized_organization_id,
                workspace_users.c.user_id == normalized_user_id,
            )
            .with_for_update()
        ).mappings().one_or_none()
        if current is None:
            raise WorkspaceUserNotFound("workspace user is unavailable")

        next_display_name = (
            normalized_display_name
            if normalized_display_name is not None
            else str(current["display_name"])
        )
        next_status = (
            normalized_status
            if normalized_status is not None
            else cast(WorkspaceUserStatus, current["status"])
        )
        next_role = (
            normalized_role
            if normalized_role is not None
            else cast(
                WorkspaceOrganizationRole,
                current["organization_role"],
            )
        )
        current_status = cast(WorkspaceUserStatus, current["status"])
        current_role = cast(
            WorkspaceOrganizationRole,
            current["organization_role"],
        )
        changed = {
            "display_name": next_display_name != current["display_name"],
            "status": next_status != current_status,
            "organization_role": next_role != current_role,
        }
        if not any(changed.values()):
            return self._read_user(
                connection,
                organization_id=normalized_organization_id,
                user_id=normalized_user_id,
            )

        if (
            current_status == "active"
            and current_role == "org_admin"
            and not (next_status == "active" and next_role == "org_admin")
        ):
            active_admin_ids = connection.execute(
                sa.select(workspace_users.c.user_id)
                .where(
                    workspace_users.c.organization_id
                    == normalized_organization_id,
                    workspace_users.c.status == "active",
                    workspace_users.c.organization_role == "org_admin",
                )
                .order_by(workspace_users.c.user_id)
                .with_for_update()
            ).scalars().all()
            if len(active_admin_ids) <= 1:
                raise WorkspaceUserLastAdmin(
                    "the last active organization administrator is protected"
                )

        values: dict[str, object] = {
            "display_name": next_display_name,
            "status": next_status,
            "organization_role": next_role,
            "updated_at": sa.func.now(),
        }
        if changed["status"]:
            values["session_version"] = int(current["session_version"]) + 1
        connection.execute(
            workspace_users.update()
            .where(
                workspace_users.c.organization_id
                == normalized_organization_id,
                workspace_users.c.user_id == normalized_user_id,
            )
            .values(**values)
        )
        details: dict[str, object] = {
            "display_name_changed": changed["display_name"],
        }
        if changed["status"]:
            details.update(
                {
                    "previous_status": current_status,
                    "status": next_status,
                    "sessions_revoked": True,
                }
            )
        if changed["organization_role"]:
            details.update(
                {
                    "previous_organization_role": current_role,
                    "organization_role": next_role,
                }
            )
        self._append_audit(
            connection,
            AuditEvent(
                organization_id=normalized_organization_id,
                event_id=normalized_event_id,
                actor_user_id=actor.user_id,
                action="workspace_user.updated",
                target_type="workspace_user",
                target_id=normalized_user_id,
                details=details,
            ),
        )
        return self._read_user(
            connection,
            organization_id=normalized_organization_id,
            user_id=normalized_user_id,
        )

    def _lock_active_admin(
        self,
        connection: Connection,
        *,
        actor: ActorIdentity,
        organization_id: str,
        write: bool,
    ) -> None:
        if actor.organization_id != organization_id:
            raise WorkspaceUserDenied("workspace user administration denied")
        organization_exists = connection.execute(
            sa.select(organizations.c.organization_id)
            .where(
                organizations.c.organization_id == organization_id,
                organizations.c.status == "active",
            )
            .with_for_update(read=not write)
        ).scalar_one_or_none()
        if organization_exists is None:
            raise WorkspaceUserDenied("workspace user administration denied")
        actor_exists = connection.execute(
            sa.select(workspace_users.c.user_id)
            .where(
                workspace_users.c.organization_id == organization_id,
                workspace_users.c.user_id == actor.user_id,
                workspace_users.c.status == "active",
                workspace_users.c.organization_role == "org_admin",
            )
            .with_for_update(read=True)
        ).scalar_one_or_none()
        if actor_exists is None:
            raise WorkspaceUserDenied("workspace user administration denied")

    def _select_users(
        self,
        connection: Connection,
        *,
        organization_id: str,
        limit: int,
        after_user_id: str | None,
    ) -> list[RowMapping]:
        statement = self._user_select(organization_id=organization_id)
        if after_user_id is not None:
            statement = statement.where(
                workspace_users.c.user_id > after_user_id
            )
        return list(
            connection.execute(
                statement.order_by(workspace_users.c.user_id).limit(limit)
            )
            .mappings()
            .all()
        )

    @staticmethod
    def _user_select(*, organization_id: str) -> sa.Select:
        team_counts = (
            sa.select(
                team_memberships.c.organization_id,
                team_memberships.c.user_id,
                sa.func.count().label("team_membership_count"),
            )
            .where(
                team_memberships.c.organization_id == organization_id
            )
            .group_by(
                team_memberships.c.organization_id,
                team_memberships.c.user_id,
            )
            .subquery()
        )
        project_counts = (
            sa.select(
                project_memberships.c.organization_id,
                project_memberships.c.user_id,
                sa.func.count().label("project_membership_count"),
            )
            .where(
                project_memberships.c.organization_id == organization_id
            )
            .group_by(
                project_memberships.c.organization_id,
                project_memberships.c.user_id,
            )
            .subquery()
        )
        linked_identities = (
            sa.select(
                external_identities.c.organization_id,
                external_identities.c.user_id,
                sa.func.count().label("active_identity_count"),
            )
            .where(
                external_identities.c.organization_id == organization_id,
                external_identities.c.status == "active",
            )
            .group_by(
                external_identities.c.organization_id,
                external_identities.c.user_id,
            )
            .subquery()
        )
        statement = (
            sa.select(
                workspace_users.c.user_id,
                workspace_users.c.display_name,
                workspace_users.c.status,
                workspace_users.c.organization_role,
                sa.func.coalesce(
                    team_counts.c.team_membership_count,
                    0,
                ).label("team_membership_count"),
                sa.func.coalesce(
                    project_counts.c.project_membership_count,
                    0,
                ).label("project_membership_count"),
                (
                    sa.func.coalesce(
                        linked_identities.c.active_identity_count,
                        0,
                    )
                    > 0
                ).label("login_linked"),
            )
            .select_from(
                workspace_users.outerjoin(
                    team_counts,
                    sa.and_(
                        team_counts.c.organization_id
                        == workspace_users.c.organization_id,
                        team_counts.c.user_id == workspace_users.c.user_id,
                    ),
                )
                .outerjoin(
                    project_counts,
                    sa.and_(
                        project_counts.c.organization_id
                        == workspace_users.c.organization_id,
                        project_counts.c.user_id
                        == workspace_users.c.user_id,
                    ),
                )
                .outerjoin(
                    linked_identities,
                    sa.and_(
                        linked_identities.c.organization_id
                        == workspace_users.c.organization_id,
                        linked_identities.c.user_id
                        == workspace_users.c.user_id,
                    ),
                )
            )
            .where(
                workspace_users.c.organization_id == organization_id
            )
        )
        return statement

    def _read_user(
        self,
        connection: Connection,
        *,
        organization_id: str,
        user_id: str,
    ) -> WorkspaceUserRecord:
        row = connection.execute(
            self._user_select(organization_id=organization_id).where(
                workspace_users.c.user_id == user_id
            )
        ).mappings().one_or_none()
        if row is None:
            raise WorkspaceUserNotFound("workspace user is unavailable")
        return self._record_from_row(row)

    @staticmethod
    def _record_from_row(row: RowMapping) -> WorkspaceUserRecord:
        return WorkspaceUserRecord(
            user_id=str(row["user_id"]),
            display_name=str(row["display_name"]),
            status=cast(WorkspaceUserStatus, row["status"]),
            organization_role=cast(
                WorkspaceOrganizationRole,
                row["organization_role"],
            ),
            team_membership_count=int(row["team_membership_count"]),
            project_membership_count=int(
                row["project_membership_count"]
            ),
            login_linked=bool(row["login_linked"]),
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
            raise WorkspaceUserUnavailable(
                "workspace user change is unavailable"
            ) from exc


__all__ = [
    "PostgresWorkspaceUserService",
    "WorkspaceOrganizationRole",
    "WorkspaceUserConflict",
    "WorkspaceUserDenied",
    "WorkspaceUserError",
    "WorkspaceUserLastAdmin",
    "WorkspaceUserNotFound",
    "WorkspaceUserPage",
    "WorkspaceUserRecord",
    "WorkspaceUserStatus",
    "WorkspaceUserUnavailable",
]
