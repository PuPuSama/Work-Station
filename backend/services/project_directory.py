from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import sqlalchemy as sa
from sqlalchemy.engine import Engine

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
    EffectiveRole,
    OrganizationRole,
    ProjectAccessFacts,
    ProjectRole,
    TeamRole,
    effective_role_for,
)


class ProjectDirectoryDenied(PermissionError):
    """The Actor is no longer active in its signed Organization."""


@dataclass(frozen=True, slots=True)
class AccessibleProject:
    project_id: str
    customer_name: str
    official_domain: str
    revision: int
    effective_role: EffectiveRole
    owning_team_id: str | None = None
    owner_user_id: str | None = None
    is_project_owner: bool = False
    assignment_status: str = "assigned"


class PostgresProjectDirectory:
    """List only active projects visible to one active Actor in SQL."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def list_for_actor(
        self,
        actor: ActorIdentity,
    ) -> tuple[AccessibleProject, ...]:
        active_team = teams.alias("directory_active_owning_team")
        owning_team_membership = team_memberships.alias(
            "directory_owning_team_membership"
        )
        explicit_project_membership = project_memberships.alias(
            "directory_explicit_project_membership"
        )
        with self._engine.connect() as connection:
            actor_role = connection.execute(
                sa.select(workspace_users.c.organization_role)
                .select_from(
                    workspace_users.join(
                        organizations,
                        organizations.c.organization_id
                        == workspace_users.c.organization_id,
                    )
                )
                .where(
                    workspace_users.c.organization_id
                    == actor.organization_id,
                    workspace_users.c.user_id == actor.user_id,
                    workspace_users.c.status == "active",
                    organizations.c.status == "active",
                )
            ).scalar_one_or_none()
            if actor_role is None:
                raise ProjectDirectoryDenied("project access denied")

            rows = connection.execute(
                sa.select(
                    projects.c.project_id,
                    projects.c.customer_name,
                    projects.c.official_domain,
                    projects.c.revision,
                    project_ownership.c.owning_team_id,
                    project_ownership.c.owner_user_id,
                    (
                        project_ownership.c.owner_user_id == actor.user_id
                    ).label("is_project_owner"),
                    owning_team_membership.c.role.label("team_role"),
                    explicit_project_membership.c.role.label("project_role"),
                )
                .select_from(
                    project_ownership.join(
                        projects,
                        projects.c.project_id
                        == project_ownership.c.project_id,
                    )
                    .outerjoin(
                        active_team,
                        sa.and_(
                            active_team.c.organization_id
                            == project_ownership.c.organization_id,
                            active_team.c.team_id
                            == project_ownership.c.owning_team_id,
                            active_team.c.status == "active",
                        ),
                    )
                    .outerjoin(
                        owning_team_membership,
                        sa.and_(
                            owning_team_membership.c.organization_id
                            == project_ownership.c.organization_id,
                            owning_team_membership.c.team_id
                            == active_team.c.team_id,
                            owning_team_membership.c.user_id
                            == actor.user_id,
                        ),
                    )
                    .outerjoin(
                        explicit_project_membership,
                        sa.and_(
                            explicit_project_membership.c.organization_id
                            == project_ownership.c.organization_id,
                            explicit_project_membership.c.project_id
                            == project_ownership.c.project_id,
                            explicit_project_membership.c.user_id
                            == actor.user_id,
                            project_ownership.c.owner_user_id.is_(None),
                        ),
                    )
                )
                .where(
                    project_ownership.c.organization_id
                    == actor.organization_id,
                    projects.c.status == "active",
                    sa.or_(
                        actor_role == "org_admin",
                        owning_team_membership.c.role == "team_lead",
                        project_ownership.c.owner_user_id == actor.user_id,
                        explicit_project_membership.c.role.is_not(None),
                    ),
                )
                .order_by(
                    sa.func.lower(projects.c.customer_name),
                    projects.c.project_id,
                )
            ).mappings()
            result: list[AccessibleProject] = []
            for row in rows:
                facts = ProjectAccessFacts(
                    organization_role=cast(
                        OrganizationRole,
                        actor_role,
                    ),
                    team_role=cast(
                        TeamRole | None,
                        row["team_role"],
                    ),
                    project_role=cast(
                        ProjectRole | None,
                        row["project_role"],
                    ),
                    owner_user_id=(
                        str(row["owner_user_id"])
                        if row["owner_user_id"] is not None
                        else None
                    ),
                    is_project_owner=bool(row["is_project_owner"]),
                )
                role = effective_role_for(facts)
                if role is None:
                    # SQL already excludes this state. Keep the Python mapping
                    # defensive if the permission model changes later.
                    continue
                result.append(
                    AccessibleProject(
                        project_id=str(row["project_id"]),
                        customer_name=str(row["customer_name"]),
                        official_domain=str(row["official_domain"]),
                        revision=int(row["revision"]),
                        effective_role=role,
                        owning_team_id=(
                            str(row["owning_team_id"])
                            if row["owning_team_id"] is not None
                            else None
                        ),
                        owner_user_id=(
                            str(row["owner_user_id"])
                            if row["owner_user_id"] is not None
                            else None
                        ),
                        is_project_owner=bool(row["is_project_owner"]),
                        assignment_status=(
                            "assigned"
                            if row["owner_user_id"] is not None
                            else "pending"
                        ),
                    )
                )
        return tuple(result)


__all__ = [
    "AccessibleProject",
    "PostgresProjectDirectory",
    "ProjectDirectoryDenied",
]
