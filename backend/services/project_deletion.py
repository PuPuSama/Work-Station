from __future__ import annotations

from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from knowledge_agent.schema import metadata as knowledge_metadata
from server_schema import (
    audit_events,
    background_jobs,
    metadata as server_metadata,
)
from services.access_control import (
    ActorIdentity,
    PostgresProjectAccessRepository,
    ProjectAccessDenied,
    decide_project_permission,
)
from services.audit_log import AuditEvent, AuditEventWriter, PostgresAuditEventWriter


class ProjectDeletionDenied(PermissionError):
    """The actor cannot delete this active project."""


class ProjectDeletionConflict(RuntimeError):
    """The project could not be removed without violating a committed FK."""


class ProjectDeletionUnavailable(RuntimeError):
    """The project deletion transaction could not be completed."""


@dataclass(frozen=True, slots=True)
class DeletedProject:
    organization_id: str
    project_id: str
    cancelled_job_count: int
    deleted_row_count: int


def _project_tables() -> tuple[sa.Table, ...]:
    """Return every current table carrying project-scoped rows.

    The Server and Knowledge schemas intentionally use separate MetaData
    objects.  Combining them here keeps deletion aligned with migrations as
    new project-scoped tables are added, while the FK ordering below still
    deletes children before project ownership and the project row itself.
    """

    tables: dict[str, sa.Table] = {}
    for table in (*knowledge_metadata.tables.values(), *server_metadata.tables.values()):
        if table.name == audit_events.name:
            continue
        if table.name == "projects" or "project_id" in table.c:
            tables[table.name] = table
    related = tuple(tables.values())
    related_set = set(related)
    depth = {table: 0 for table in related}
    for _ in range(len(related)):
        changed = False
        for table in related:
            parent_depth = max(
                (
                    depth[fk.column.table] + 1
                    for fk in table.foreign_keys
                    if fk.column.table in related_set
                ),
                default=0,
            )
            if parent_depth > depth[table]:
                depth[table] = parent_depth
                changed = True
        if not changed:
            break
    return tuple(sorted(related, key=lambda table: (-depth[table], table.name)))


class PostgresProjectDeletionService:
    """Delete one project after revoking access and cancelling queued work."""

    def __init__(
        self,
        engine: Engine,
        *,
        audit: AuditEventWriter | None = None,
    ) -> None:
        self._engine = engine
        self._access = PostgresProjectAccessRepository(engine)
        self._audit = audit or PostgresAuditEventWriter()

    def delete(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        event_id: str,
    ) -> DeletedProject:
        normalized_project_id = project_id.strip()
        normalized_event_id = event_id.strip()
        if not normalized_project_id:
            raise ValueError("project_id is required")
        if not normalized_event_id:
            raise ValueError("event_id is required")
        try:
            with self._engine.begin() as connection:
                return self.delete_in_transaction(
                    connection,
                    actor=actor,
                    project_id=normalized_project_id,
                    event_id=normalized_event_id,
                )
        except (ProjectDeletionDenied, ProjectDeletionConflict, ValueError):
            raise
        except IntegrityError as exc:
            raise ProjectDeletionConflict(
                "project has dependent records that cannot be removed"
            ) from exc
        except SQLAlchemyError as exc:
            raise ProjectDeletionUnavailable(
                "project deletion is temporarily unavailable"
            ) from exc

    def delete_in_transaction(
        self,
        connection: Connection,
        *,
        actor: ActorIdentity,
        project_id: str,
        event_id: str,
    ) -> DeletedProject:
        if not connection.in_transaction():
            raise ValueError("project deletion requires a business transaction")
        facts = self._access.lock_project_access_in_connection(
            connection,
            actor,
            project_id,
        )
        if not decide_project_permission(facts, "project.delete").allowed:
            raise ProjectDeletionDenied("project deletion denied")

        # A running worker observes cancellation before its row disappears;
        # clearing its lease also satisfies the background_jobs state check.
        cancelled_job_count = int(
            connection.execute(
                background_jobs.update()
                .where(
                    background_jobs.c.organization_id == actor.organization_id,
                    background_jobs.c.project_id == project_id,
                    background_jobs.c.status.in_(("queued", "running", "retry_wait")),
                )
                .values(
                    status="cancelled",
                    cancel_requested=True,
                    worker_id=None,
                    lease_expires_at=None,
                    finished_at=sa.func.now(),
                    updated_at=sa.func.now(),
                )
            ).rowcount
            or 0
        )

        # Receipt rows are append-only during ordinary operation. The audited
        # project deletion transaction is the one explicit lifecycle command
        # allowed to remove them; the migration-gated trigger checks this
        # transaction-local marker for cascaded deletes as well.
        if connection.dialect.name == "postgresql":
            connection.execute(
                sa.text(
                    "SELECT set_config('article_agent.project_deletion', 'on', true)"
                )
            )

        # Audit history survives project deletion, but its nullable FK cannot
        # continue pointing at a removed project ownership row. The same
        # transaction-local marker permits this one controlled pointer update.
        connection.execute(
            audit_events.update()
            .where(
                audit_events.c.organization_id == actor.organization_id,
                audit_events.c.project_id == project_id,
            )
            .values(project_id=None)
        )

        deleted_row_count = 0
        for table in _project_tables():
            project_column = table.c.get("project_id")
            if project_column is None:
                continue
            conditions = [project_column == project_id]
            organization_column = table.c.get("organization_id")
            if organization_column is not None:
                conditions.append(organization_column == actor.organization_id)
            result = connection.execute(table.delete().where(*conditions))
            deleted_row_count += int(result.rowcount or 0)

        self._audit.append(
            connection,
            AuditEvent(
                organization_id=actor.organization_id,
                event_id=event_id,
                actor_user_id=actor.user_id,
                project_id=None,
                action="project.deleted",
                target_type="project",
                target_id=project_id,
                details={
                    "cancelled_job_count": cancelled_job_count,
                    "deleted_row_count": deleted_row_count,
                },
            ),
        )
        return DeletedProject(
            organization_id=actor.organization_id,
            project_id=project_id,
            cancelled_job_count=cancelled_job_count,
            deleted_row_count=deleted_row_count,
        )


__all__ = [
    "DeletedProject",
    "PostgresProjectDeletionService",
    "ProjectDeletionConflict",
    "ProjectDeletionDenied",
    "ProjectDeletionUnavailable",
]
