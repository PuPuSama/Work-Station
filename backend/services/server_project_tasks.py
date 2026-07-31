from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.engine import Engine

from config import AppConfig
from services.audit_log import AuditEventWriter
from services.postgres_task_repository import PostgresTaskRepository
from services.server_request_security import AuthorizedProjectRequest
from services.server_task_intake import PostgresServerTaskIntakeService
from services.server_task_commands import PostgresAuditedTaskWriter
from storage import TaskStore


@dataclass(frozen=True, slots=True)
class ServerProjectTaskScope:
    """Authorized TaskStore scope derived from a server request."""

    organization_id: str
    project_id: str


@dataclass(frozen=True, slots=True)
class ServerProjectTaskRuntime:
    """Compatibility runtime fixed to one authorized organization/project."""

    scope: ServerProjectTaskScope
    store: TaskStore
    intake: PostgresServerTaskIntakeService
    audited_writer: PostgresAuditedTaskWriter


class ServerProjectTaskStoreFactory:
    """Build project-scoped PostgreSQL TaskStore compatibility adapters.

    The factory deliberately receives an ``AuthorizedProjectRequest`` rather
    than raw organization/project request fields. It never enables legacy JSON
    or SQLite import and therefore cannot fall back to local application data.
    """

    def __init__(
        self,
        engine: Engine,
        config: AppConfig,
        *,
        audit: AuditEventWriter | None = None,
    ) -> None:
        self._engine = engine
        self._config = config
        self._audit = audit

    @property
    def config(self) -> AppConfig:
        """Formatting/runtime config shared by project-scoped commands."""

        return self._config

    def create(
        self,
        authorized: AuthorizedProjectRequest,
    ) -> ServerProjectTaskRuntime:
        scope = ServerProjectTaskScope(
            organization_id=authorized.actor.organization_id,
            project_id=authorized.project_id,
        )
        repository = PostgresTaskRepository(
            self._engine,
            organization_id=scope.organization_id,
            project_id=scope.project_id,
        )
        return ServerProjectTaskRuntime(
            scope=scope,
            store=TaskStore(
                self._config,
                repository=repository,
                legacy_import_enabled=False,
            ),
            intake=PostgresServerTaskIntakeService(
                self._engine,
                organization_id=scope.organization_id,
                project_id=scope.project_id,
                audit=self._audit,
            ),
            audited_writer=PostgresAuditedTaskWriter(
                self._engine,
                organization_id=scope.organization_id,
                project_id=scope.project_id,
                audit=self._audit,
            ),
        )


__all__ = [
    "ServerProjectTaskScope",
    "ServerProjectTaskRuntime",
    "ServerProjectTaskStoreFactory",
]
