from __future__ import annotations

import uuid
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from knowledge_agent.contracts import KnowledgeProject
from knowledge_agent.schema import projects
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


class ServerProjectMetadataConflict(RuntimeError):
    """A Project metadata command used a stale Revision."""


class ServerProjectMetadataUnavailable(RuntimeError):
    """Project metadata could not be read or committed safely."""


@dataclass(frozen=True, slots=True)
class ServerProjectMetadata:
    """Public, project-scoped identity metadata with optimistic Revision."""

    project_id: str
    customer_name: str
    official_domain: str
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
    return ServerProjectMetadata(
        project_id=validated.project_id,
        customer_name=validated.customer_name,
        official_domain=validated.official_domain,
        revision=revision,
    )


class PostgresServerProjectMetadata:
    """Read and update the shared Project identity without renaming its ID.

    Free-form business facts are deliberately excluded: authoritative facts
    belong in Published Knowledge and writing rules belong in immutable Prompt
    Snapshots. Existing Tasks retain their captured brand/domain identity;
    updates apply to future Task Intake and official-site operations.
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

    def update(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        expected_revision: int,
        customer_name: str,
        official_domain: str,
    ) -> ServerProjectMetadata:
        requested = _validated_metadata(
            project_id=project_id,
            customer_name=customer_name,
            official_domain=official_domain,
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
                if not customer_name_changed and not official_domain_changed:
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
                        },
                    ),
                )
                return ServerProjectMetadata(
                    project_id=project_id,
                    customer_name=requested.customer_name,
                    official_domain=requested.official_domain,
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
    "PostgresServerProjectMetadata",
    "ServerProjectMetadata",
    "ServerProjectMetadataConflict",
    "ServerProjectMetadataUnavailable",
]
