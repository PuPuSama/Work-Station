from __future__ import annotations

import uuid
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from knowledge_agent.contracts import KnowledgeProject
from knowledge_agent.schema import projects
from server_schema import (
    organizations,
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
    owning_team_id: str | None = None
    owner_user_id: str | None = None


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
            owning_team_id=(
                str(row["owning_team_id"])
                if row.get("owning_team_id") is not None
                else None
            ),
            owner_user_id=(
                str(row["owner_user_id"])
                if row.get("owner_user_id") is not None
                else None
            ),
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
                        project_ownership.c.owning_team_id,
                        project_ownership.c.owner_user_id,
                    )
                    .select_from(
                        projects.join(
                            project_ownership,
                            project_ownership.c.project_id
                            == projects.c.project_id,
                        )
                    )
                    .where(
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
        owner_user_id: str | None = None,
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
            owning_team_id=None,
            owner_user_id=None,
        )
        normalized_team_id = (
            owning_team_id.strip() if owning_team_id is not None else None
        )
        if owning_team_id is not None and not normalized_team_id:
            raise ValueError("owning_team_id must not be blank")
        normalized_owner_user_id = (
            owner_user_id.strip() if owner_user_id is not None else None
        )
        if owner_user_id is not None and not normalized_owner_user_id:
            raise ValueError("owner_user_id must not be blank")
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
                actor_row = connection.execute(
                    sa.select(workspace_users.c.user_id)
                    .where(
                        workspace_users.c.organization_id
                        == actor.organization_id,
                        workspace_users.c.user_id == actor.user_id,
                        workspace_users.c.status == "active",
                    )
                    .with_for_update(read=True)
                ).scalar_one_or_none()
                actor_role = connection.execute(
                    sa.select(workspace_users.c.organization_role)
                    .where(
                        workspace_users.c.organization_id
                        == actor.organization_id,
                        workspace_users.c.user_id == actor.user_id,
                        workspace_users.c.status == "active",
                    )
                    .with_for_update(read=True)
                ).scalar_one_or_none()
                if organization is None or actor_row is None:
                    raise ProjectAccessDenied("project creation denied")
                is_admin = actor_role == "org_admin"
                actor_membership = connection.execute(
                    sa.select(
                        team_memberships.c.team_id,
                        team_memberships.c.role,
                    )
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
                        team_memberships.c.organization_id
                        == actor.organization_id,
                        team_memberships.c.user_id == actor.user_id,
                        teams.c.status == "active",
                    )
                    .with_for_update(read=True)
                ).mappings().one_or_none()
                if normalized_team_id is None and not is_admin:
                    if actor_membership is None:
                        raise ProjectAccessDenied("project creation denied")
                    normalized_team_id = str(actor_membership["team_id"])
                if normalized_team_id is None:
                    raise ValueError("owning_team_id is required")
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
                if not is_admin:
                    if (
                        actor_membership is None
                        or actor_membership["team_id"] != normalized_team_id
                    ):
                        raise ProjectAccessDenied("project creation denied")
                actor_is_lead = (
                    actor_membership is not None
                    and actor_membership["team_id"] == normalized_team_id
                    and actor_membership["role"] == "team_lead"
                )
                if normalized_owner_user_id is None and not is_admin:
                    if not actor_is_lead:
                        normalized_owner_user_id = actor.user_id
                if normalized_owner_user_id is not None:
                    target_membership = connection.execute(
                        sa.select(team_memberships.c.role)
                        .where(
                            team_memberships.c.organization_id
                            == actor.organization_id,
                            team_memberships.c.team_id == normalized_team_id,
                            team_memberships.c.user_id
                            == normalized_owner_user_id,
                            team_memberships.c.role.in_(("member", "team_lead")),
                        )
                        .with_for_update(read=True)
                    ).scalar_one_or_none()
                    if target_membership is None:
                        raise ValueError(
                            "owner_user_id must belong to the owning team"
                        )
                    if not is_admin and not actor_is_lead:
                        if normalized_owner_user_id != actor.user_id:
                            raise ProjectAccessDenied(
                                "project creation denied"
                            )
                requested = ServerProjectMetadata(
                    project_id=requested.project_id,
                    customer_name=requested.customer_name,
                    official_domain=requested.official_domain,
                    project_notes=requested.project_notes,
                    revision=requested.revision,
                    owning_team_id=normalized_team_id,
                    owner_user_id=normalized_owner_user_id,
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
                        owner_user_id=normalized_owner_user_id,
                    )
                )
                if normalized_owner_user_id is not None:
                    connection.execute(
                        project_memberships.insert().values(
                            organization_id=actor.organization_id,
                            project_id=requested.project_id,
                            user_id=normalized_owner_user_id,
                            role="editor",
                            granted_by_user_id=actor.user_id,
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
                            "owner_user_id": normalized_owner_user_id or "",
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
                    "article.edit",
                ).allowed:
                    raise ProjectAccessDenied("project access denied")
                row = connection.execute(
                    sa.select(
                        projects.c.project_id,
                        projects.c.customer_name,
                        projects.c.official_domain,
                        projects.c.project_notes,
                        projects.c.revision,
                        project_ownership.c.owning_team_id,
                        project_ownership.c.owner_user_id,
                    )
                    .select_from(
                        projects.join(
                            project_ownership,
                            project_ownership.c.project_id
                            == projects.c.project_id,
                        )
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
                    owning_team_id=current.owning_team_id,
                    owner_user_id=current.owner_user_id,
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
