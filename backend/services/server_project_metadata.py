from __future__ import annotations

import uuid
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from knowledge_agent.contracts import KnowledgeProject
from knowledge_agent.schema import projects
from server_schema import organizations, project_ownership, teams, workspace_users
from services.access_control import (
    ActorIdentity,
    PostgresProjectAccessRepository,
    ProjectAccessDenied,
    ProjectAccessService,
    decide_project_permission,
)
from services.audit_log import (
    AuditEvent,
    AuditEventWriter,
    PostgresAuditEventWriter,
)
from services.task_identity import normalized_customer


class ServerProjectMetadataConflict(RuntimeError):
    """A Project metadata command used a stale Revision."""


class ServerProjectMetadataUnavailable(RuntimeError):
    """Project metadata could not be read or committed safely."""


class ServerProjectCreationConflict(RuntimeError):
    """A new Project identity already exists or changed concurrently."""


@dataclass(frozen=True, slots=True)
class ServerProjectMetadata:
    """Project-scoped settings with optimistic Revision."""

    project_id: str
    customer_name: str
    official_domain: str
    project_notes: str
    revision: int


def _normalized_customer_name(value: str) -> str:
    normalized = " ".join(value.split())
    if not normalized:
        raise ValueError("customer_name is required")
    if len(normalized) > 120:
        raise ValueError("customer_name is too long")
    if any(character in normalized for character in "[]"):
        raise ValueError(
            "customer_name cannot contain Markdown square brackets"
        )
    return normalized


def _validated_metadata(
    *,
    project_id: str,
    customer_name: str,
    official_domain: str,
    project_notes: str,
    revision: int,
) -> ServerProjectMetadata:
    if isinstance(revision, bool) or not isinstance(revision, int):
        raise ValueError("revision must be a non-negative integer")
    if revision < 0:
        raise ValueError("revision must be a non-negative integer")
    normalized_name = _normalized_customer_name(customer_name)
    validated = KnowledgeProject(
        project_id=project_id,
        customer_name=normalized_name,
        official_domain=official_domain,
    )
    normalized_notes = str(project_notes or "").replace("\r\n", "\n").strip()
    if len(normalized_notes) > 30000:
        raise ValueError("project_notes is too long")
    return ServerProjectMetadata(
        project_id=validated.project_id,
        customer_name=validated.customer_name,
        official_domain=validated.official_domain,
        project_notes=normalized_notes,
        revision=revision,
    )


class PostgresServerProjectMetadata:
    """Read and update shared Project settings without renaming its ID.

    Project notes are operator guidance, not authoritative business evidence.
    New Tasks capture them during intake; existing Tasks retain their snapshot.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        audit: AuditEventWriter | None = None,
    ) -> None:
        self._engine = engine
        self._access_repository = PostgresProjectAccessRepository(engine)
        self._access = ProjectAccessService(self._access_repository)
        self._audit = audit or PostgresAuditEventWriter()

    @staticmethod
    def _from_row(row: sa.RowMapping) -> ServerProjectMetadata:
        return ServerProjectMetadata(
            project_id=str(row["project_id"]),
            customer_name=str(row["customer_name"]),
            official_domain=str(row["official_domain"]),
            project_notes=str(row["project_notes"] or ""),
            revision=int(row["revision"]),
        )

    def get(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
    ) -> ServerProjectMetadata:
        try:
            self._access.require(actor, project_id, "project.view")
            with self._engine.connect() as connection:
                row = connection.execute(
                    sa.select(
                        projects.c.project_id,
                        projects.c.customer_name,
                        projects.c.official_domain,
                        projects.c.project_notes,
                        projects.c.revision,
                    ).where(
                        projects.c.project_id == project_id,
                        projects.c.status == "active",
                    )
                ).mappings().one_or_none()
        except ProjectAccessDenied:
            raise
        except (RuntimeError, SQLAlchemyError) as exc:
            raise ServerProjectMetadataUnavailable(
                "project metadata is unavailable"
            ) from exc
        if row is None:
            raise ProjectAccessDenied("project access denied")
        return self._from_row(row)

    def create(
        self,
        *,
        actor: ActorIdentity,
        customer_name: str,
        official_domain: str,
        owning_team_id: str | None,
        event_id: str,
    ) -> ServerProjectMetadata:
        """Provision one active Project and its Organization ownership atomically."""

        identity = KnowledgeProject(
            project_id=official_domain,
            customer_name=_normalized_customer_name(customer_name),
            official_domain=official_domain,
        )
        requested = ServerProjectMetadata(
            project_id=normalized_customer(identity.official_domain),
            customer_name=identity.customer_name,
            official_domain=identity.official_domain,
            project_notes="",
            revision=0,
        )
        normalized_team_id = (
            owning_team_id.strip() if owning_team_id is not None else None
        )
        if owning_team_id is not None and not normalized_team_id:
            raise ValueError("owning_team_id must not be blank")
        normalized_event_id = event_id.strip()
        if not normalized_event_id:
            raise ValueError("event_id is required")
        try:
            with self._engine.begin() as connection:
                organization = connection.execute(
                    sa.select(organizations.c.organization_id)
                    .where(
                        organizations.c.organization_id
                        == actor.organization_id,
                        organizations.c.status == "active",
                    )
                    .with_for_update()
                ).scalar_one_or_none()
                administrator = connection.execute(
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
                if organization is None or administrator is None:
                    raise ProjectAccessDenied("project creation denied")
                if normalized_team_id is not None:
                    active_team = connection.execute(
                        sa.select(teams.c.team_id)
                        .where(
                            teams.c.organization_id == actor.organization_id,
                            teams.c.team_id == normalized_team_id,
                            teams.c.status == "active",
                        )
                        .with_for_update(read=True)
                    ).scalar_one_or_none()
                    if active_team is None:
                        raise ValueError(
                            "owning_team_id must reference an active team"
                        )
                connection.execute(
                    projects.insert().values(
                        project_id=requested.project_id,
                        customer_name=requested.customer_name,
                        official_domain=requested.official_domain,
                        status="active",
                        revision=0,
                    )
                )
                connection.execute(
                    project_ownership.insert().values(
                        project_id=requested.project_id,
                        organization_id=actor.organization_id,
                        owning_team_id=normalized_team_id,
                    )
                )
                self._audit.append(
                    connection,
                    AuditEvent(
                        organization_id=actor.organization_id,
                        event_id=normalized_event_id,
                        actor_user_id=actor.user_id,
                        project_id=requested.project_id,
                        action="project.created",
                        target_type="project",
                        target_id=requested.project_id,
                        details={
                            "owning_team_id": normalized_team_id or "",
                            "status": "active",
                        },
                    ),
                )
        except (ProjectAccessDenied, ValueError):
            raise
        except IntegrityError as exc:
            raise ServerProjectCreationConflict(
                "project already exists"
            ) from exc
        except SQLAlchemyError as exc:
            raise ServerProjectMetadataUnavailable(
                "project creation is unavailable"
            ) from exc
        return requested

    def update(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        expected_revision: int,
        customer_name: str,
        official_domain: str,
        project_notes: str,
    ) -> ServerProjectMetadata:
        requested = _validated_metadata(
            project_id=project_id,
            customer_name=customer_name,
            official_domain=official_domain,
            project_notes=project_notes,
            revision=expected_revision,
        )
        try:
            with self._engine.begin() as connection:
                facts = (
                    self._access_repository.lock_project_access_in_connection(
                        connection,
                        actor,
                        project_id,
                    )
                )
                if not decide_project_permission(
                    facts,
                    "project.members.manage",
                ).allowed:
                    raise ProjectAccessDenied("project access denied")
                row = connection.execute(
                    sa.select(
                        projects.c.project_id,
                        projects.c.customer_name,
                        projects.c.official_domain,
                        projects.c.project_notes,
                        projects.c.revision,
                    )
                    .where(
                        projects.c.project_id == project_id,
                        projects.c.status == "active",
                    )
                    .with_for_update()
                ).mappings().one_or_none()
                if row is None:
                    raise ProjectAccessDenied("project access denied")
                current = self._from_row(row)
                if current.revision != expected_revision:
                    raise ServerProjectMetadataConflict(
                        "project metadata revision changed"
                    )
                customer_name_changed = (
                    current.customer_name != requested.customer_name
                )
                official_domain_changed = (
                    current.official_domain != requested.official_domain
                )
                project_notes_changed = (
                    current.project_notes != requested.project_notes
                )
                if not any(
                    (
                        customer_name_changed,
                        official_domain_changed,
                        project_notes_changed,
                    )
                ):
                    return current

                next_revision = expected_revision + 1
                result = connection.execute(
                    projects.update()
                    .where(
                        projects.c.project_id == project_id,
                        projects.c.revision == expected_revision,
                    )
                    .values(
                        customer_name=requested.customer_name,
                        official_domain=requested.official_domain,
                        project_notes=requested.project_notes,
                        revision=next_revision,
                        updated_at=sa.func.now(),
                    )
                )
                if result.rowcount != 1:
                    raise ServerProjectMetadataConflict(
                        "project metadata revision changed"
                    )
                self._audit.append(
                    connection,
                    AuditEvent(
                        organization_id=actor.organization_id,
                        event_id=(
                            "project_metadata_"
                            + uuid.uuid5(
                                uuid.NAMESPACE_URL,
                                "\x1f".join(
                                    (
                                        actor.organization_id,
                                        project_id,
                                        str(next_revision),
                                    )
                                ),
                            ).hex
                        ),
                        actor_user_id=actor.user_id,
                        project_id=project_id,
                        action="project.metadata.updated",
                        target_type="project",
                        target_id=project_id,
                        details={
                            "from_revision": expected_revision,
                            "to_revision": next_revision,
                            "customer_name_changed": customer_name_changed,
                            "official_domain_changed": (
                                official_domain_changed
                            ),
                            "project_notes_changed": project_notes_changed,
                        },
                    ),
                )
                return ServerProjectMetadata(
                    project_id=project_id,
                    customer_name=requested.customer_name,
                    official_domain=requested.official_domain,
                    project_notes=requested.project_notes,
                    revision=next_revision,
                )
        except (
            ProjectAccessDenied,
            ServerProjectMetadataConflict,
            ValueError,
        ):
            raise
        except (RuntimeError, SQLAlchemyError) as exc:
            raise ServerProjectMetadataUnavailable(
                "project metadata could not be committed"
            ) from exc


__all__ = [
    "ServerProjectCreationConflict",
    "PostgresServerProjectMetadata",
    "ServerProjectMetadata",
    "ServerProjectMetadataConflict",
    "ServerProjectMetadataUnavailable",
]
