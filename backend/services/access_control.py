from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, TypeAlias, cast

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from server_schema import (
    organizations,
    project_memberships,
    project_ownership,
    team_memberships,
    teams,
    workspace_users,
)


OrganizationRole: TypeAlias = Literal["org_admin", "member"]
TeamRole: TypeAlias = Literal["team_lead", "member"]
ProjectRole: TypeAlias = Literal["editor", "reviewer", "viewer"]
EffectiveRole: TypeAlias = Literal[
    "org_admin",
    "team_lead",
    "editor",
    "reviewer",
    "viewer",
]
ProjectPermission: TypeAlias = Literal[
    "project.view",
    "article.edit",
    "article.review",
    "article.deliver",
    "knowledge.edit",
    "knowledge.publish",
    "project.members.manage",
    "knowledge.delete",
    "project.delete",
]


ALL_PROJECT_PERMISSIONS: frozenset[ProjectPermission] = frozenset(
    {
        "project.view",
        "article.edit",
        "article.review",
        "article.deliver",
        "knowledge.edit",
        "knowledge.publish",
        "project.members.manage",
        "knowledge.delete",
        "project.delete",
    }
)

ROLE_PERMISSIONS: dict[EffectiveRole, frozenset[ProjectPermission]] = {
    "org_admin": ALL_PROJECT_PERMISSIONS,
    "team_lead": frozenset(
        {
            "project.view",
            "article.edit",
            "article.review",
            "article.deliver",
            "knowledge.edit",
            "knowledge.publish",
            "project.members.manage",
        }
    ),
    "editor": frozenset(
        {
            "project.view",
            "article.edit",
            "article.review",
            "article.deliver",
            "knowledge.edit",
            "knowledge.publish",
        }
    ),
    "reviewer": frozenset(
        {
            "project.view",
            "article.review",
        }
    ),
    "viewer": frozenset({"project.view"}),
}


def _required_text(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized


@dataclass(frozen=True)
class ActorIdentity:
    """Trusted server-side identity used for project authorization."""

    organization_id: str
    user_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "organization_id",
            _required_text(self.organization_id, "organization_id"),
        )
        object.__setattr__(
            self,
            "user_id",
            _required_text(self.user_id, "user_id"),
        )


@dataclass(frozen=True)
class ProjectAccessFacts:
    """Database facts for one active actor and one organization-bound project."""

    organization_role: OrganizationRole
    team_role: TeamRole | None = None
    project_role: ProjectRole | None = None


@dataclass(frozen=True)
class AuthorizationDecision:
    allowed: bool
    permission: ProjectPermission
    effective_role: EffectiveRole | None


class ProjectAccessRepository(Protocol):
    def resolve_project_access(
        self,
        actor: ActorIdentity,
        project_id: str,
    ) -> ProjectAccessFacts | None:
        """Return trusted access facts or None without revealing denial details."""


class ProjectAccessDenied(PermissionError):
    """Generic project denial that does not reveal cross-organization state."""


def effective_role_for(facts: ProjectAccessFacts) -> EffectiveRole | None:
    if facts.organization_role == "org_admin":
        return "org_admin"
    if facts.team_role == "team_lead":
        return "team_lead"
    if facts.project_role is not None:
        return facts.project_role
    return None


class ProjectAccessService:
    """Apply the shared ADR-0003 permission matrix to repository facts."""

    def __init__(self, repository: ProjectAccessRepository) -> None:
        self._repository = repository

    def decide(
        self,
        actor: ActorIdentity,
        project_id: str,
        permission: ProjectPermission,
    ) -> AuthorizationDecision:
        normalized_project_id = _required_text(project_id, "project_id")
        facts = self._repository.resolve_project_access(actor, normalized_project_id)
        role = effective_role_for(facts) if facts is not None else None
        return AuthorizationDecision(
            allowed=role is not None and permission in ROLE_PERMISSIONS[role],
            permission=permission,
            effective_role=role,
        )

    def require(
        self,
        actor: ActorIdentity,
        project_id: str,
        permission: ProjectPermission,
    ) -> AuthorizationDecision:
        decision = self.decide(actor, project_id, permission)
        if not decision.allowed:
            raise ProjectAccessDenied("project access denied")
        return decision


class PostgresProjectAccessRepository:
    """Resolve project access from PostgreSQL without trusting client roles."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def resolve_project_access(
        self,
        actor: ActorIdentity,
        project_id: str,
    ) -> ProjectAccessFacts | None:
        normalized_project_id = _required_text(project_id, "project_id")

        active_team = teams.alias("active_owning_team")
        owning_team_membership = team_memberships.alias(
            "owning_team_membership"
        )
        explicit_project_membership = project_memberships.alias(
            "explicit_project_membership"
        )

        statement = (
            sa.select(
                workspace_users.c.organization_role,
                owning_team_membership.c.role.label("team_role"),
                explicit_project_membership.c.role.label("project_role"),
            )
            .select_from(
                project_ownership.join(
                    organizations,
                    organizations.c.organization_id
                    == project_ownership.c.organization_id,
                )
                .join(
                    workspace_users,
                    sa.and_(
                        workspace_users.c.organization_id
                        == project_ownership.c.organization_id,
                        workspace_users.c.user_id == actor.user_id,
                    ),
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
                        owning_team_membership.c.user_id == actor.user_id,
                    ),
                )
                .outerjoin(
                    explicit_project_membership,
                    sa.and_(
                        explicit_project_membership.c.organization_id
                        == project_ownership.c.organization_id,
                        explicit_project_membership.c.project_id
                        == project_ownership.c.project_id,
                        explicit_project_membership.c.user_id == actor.user_id,
                    ),
                )
            )
            .where(
                project_ownership.c.project_id == normalized_project_id,
                project_ownership.c.organization_id == actor.organization_id,
                organizations.c.status == "active",
                workspace_users.c.status == "active",
            )
        )

        with self._engine.connect() as connection:
            row = connection.execute(statement).mappings().one_or_none()
        if row is None:
            return None
        return ProjectAccessFacts(
            organization_role=cast(OrganizationRole, row["organization_role"]),
            team_role=cast(TeamRole | None, row["team_role"]),
            project_role=cast(ProjectRole | None, row["project_role"]),
        )


__all__ = [
    "ALL_PROJECT_PERMISSIONS",
    "ActorIdentity",
    "AuthorizationDecision",
    "EffectiveRole",
    "OrganizationRole",
    "PostgresProjectAccessRepository",
    "ProjectAccessDenied",
    "ProjectAccessFacts",
    "ProjectAccessRepository",
    "ProjectAccessService",
    "ProjectPermission",
    "ProjectRole",
    "ROLE_PERMISSIONS",
    "TeamRole",
    "effective_role_for",
]
