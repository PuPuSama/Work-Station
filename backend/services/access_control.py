from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, TypeAlias, cast

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from knowledge_agent.schema import projects
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
    owner_user_id: str | None = None
    is_project_owner: bool = False
    # This is populated by the Server resolver for a new project whose owner
    # is intentionally pending.  Keep the default false for legacy policy
    # fixtures that do not carry the ownership assignment state.
    is_project_pending: bool = False


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
    if facts.is_project_owner:
        return "editor"
    if facts.project_role is not None:
        return facts.project_role
    return None


def decide_project_permission(
    facts: ProjectAccessFacts | None,
    permission: ProjectPermission,
) -> AuthorizationDecision:
    role = effective_role_for(facts) if facts is not None else None
    allowed = role is not None and permission in ROLE_PERMISSIONS[role]
    if facts is not None and role == "team_lead":
        # New projects have one explicit owner. A Team Lead can supervise,
        # assign, and delete that project's record, but cannot edit or run
        # the article workflow unless they are the owner. Legacy projects
        # without owner_user_id retain the pre-existing inherited matrix.
        if (
            facts.owner_user_id is not None and not facts.is_project_owner
        ) or facts.is_project_pending:
            allowed = permission in {
                "project.view",
                "project.members.manage",
                "project.delete",
            }
        elif facts.is_project_owner and permission == "project.delete":
            allowed = True
    elif facts is not None and facts.is_project_owner:
        # A member-created project is owned by one editor. Ownership also
        # permits deleting that project, but never grants membership admin.
        if permission == "project.delete":
            allowed = True
    return AuthorizationDecision(
        allowed=allowed,
        permission=permission,
        effective_role=role,
    )


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
        return decide_project_permission(facts, permission)

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
        with self._engine.connect() as connection:
            return self.resolve_project_access_in_connection(
                connection,
                actor,
                normalized_project_id,
            )

    def resolve_project_access_in_connection(
        self,
        connection: Connection,
        actor: ActorIdentity,
        project_id: str,
    ) -> ProjectAccessFacts | None:
        normalized_project_id = _required_text(project_id, "project_id")
        active_team = teams.alias("active_owning_team")
        owning_team_membership = team_memberships.alias(
            "owning_team_membership"
        )
        statement = (
            sa.select(
                workspace_users.c.organization_role,
                owning_team_membership.c.role.label("team_role"),
                sa.null().label("project_role"),
                project_ownership.c.owner_user_id,
                (
                    project_ownership.c.owner_user_id == actor.user_id
                ).label("is_project_owner"),
                project_ownership.c.owner_user_id.is_(None).label(
                    "is_project_pending"
                ),
            )
            .select_from(
                project_ownership.join(
                    projects,
                    projects.c.project_id
                    == project_ownership.c.project_id,
                )
                .join(
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
            )
            .where(
                project_ownership.c.project_id == normalized_project_id,
                project_ownership.c.organization_id == actor.organization_id,
                projects.c.status == "active",
                organizations.c.status == "active",
                workspace_users.c.status == "active",
            )
        )

        row = connection.execute(statement).mappings().one_or_none()
        if row is None:
            return None
        return ProjectAccessFacts(
            organization_role=cast(OrganizationRole, row["organization_role"]),
            team_role=cast(TeamRole | None, row["team_role"]),
            project_role=cast(ProjectRole | None, row["project_role"]),
            owner_user_id=(
                str(row["owner_user_id"])
                if row["owner_user_id"] is not None
                else None
            ),
            is_project_owner=bool(row["is_project_owner"]),
            is_project_pending=bool(row["is_project_pending"]),
        )

    def lock_project_access_in_connection(
        self,
        connection: Connection,
        actor: ActorIdentity,
        project_id: str,
    ) -> ProjectAccessFacts | None:
        """Lock every existing row that can revoke this access decision."""

        if not connection.in_transaction():
            raise ValueError(
                "project access locking requires a business transaction"
            )
        normalized_project_id = _required_text(project_id, "project_id")
        base = connection.execute(
            sa.select(
                project_ownership.c.owning_team_id,
                project_ownership.c.owner_user_id,
            )
            .select_from(
                project_ownership.join(
                    projects,
                    projects.c.project_id
                    == project_ownership.c.project_id,
                )
                .join(
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
            )
            .where(
                project_ownership.c.project_id == normalized_project_id,
                project_ownership.c.organization_id
                == actor.organization_id,
            )
            .with_for_update(
                read=True,
                of=(
                    project_ownership,
                    projects,
                    organizations,
                    workspace_users,
                ),
            )
        ).one_or_none()
        if base is None:
            return None

        connection.execute(
            sa.select(project_memberships.c.user_id)
            .where(
                project_memberships.c.organization_id
                == actor.organization_id,
                project_memberships.c.project_id
                == normalized_project_id,
                project_memberships.c.user_id == actor.user_id,
            )
            .with_for_update(read=True)
        ).all()
        owning_team_id = base.owning_team_id
        if owning_team_id is not None:
            connection.execute(
                sa.select(teams.c.team_id)
                .where(
                    teams.c.organization_id
                    == actor.organization_id,
                    teams.c.team_id == owning_team_id,
                )
                .with_for_update(read=True)
            ).all()
        if base.owner_user_id is not None:
            connection.execute(
                sa.select(workspace_users.c.user_id)
                .where(
                    workspace_users.c.organization_id
                    == actor.organization_id,
                    workspace_users.c.user_id == base.owner_user_id,
                )
                .with_for_update(read=True)
            ).all()
        if owning_team_id is not None:
            connection.execute(
                sa.select(team_memberships.c.user_id)
                .where(
                    team_memberships.c.organization_id
                    == actor.organization_id,
                    team_memberships.c.team_id == owning_team_id,
                    team_memberships.c.user_id == actor.user_id,
                )
                .with_for_update(read=True)
            ).all()
        return self.resolve_project_access_in_connection(
            connection,
            actor,
            normalized_project_id,
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
    "decide_project_permission",
    "effective_role_for",
]
