from __future__ import annotations

from dataclasses import dataclass

from services.access_control import (
    ActorIdentity,
    ProjectAccessDenied,
    ProjectAccessService,
    ProjectPermission,
)
from services.actor_sessions import ActorSessionVersionReader
from services.server_auth import (
    ServerActorSessionCodec,
    ServerActorSessionError,
)
from services.task_identity import normalized_customer


class ServerRequestUnauthenticated(PermissionError):
    """A request did not contain a valid server Actor session."""


class ServerRequestForbidden(PermissionError):
    """A valid Actor cannot perform the requested project operation."""


def knowledge_permission_for(
    method: str,
    route_path: str,
) -> ProjectPermission:
    """Map Knowledge API semantics to the conservative M7 permission matrix."""

    normalized_method = method.strip().upper()
    normalized_path = route_path.rstrip("/")
    if normalized_method == "GET":
        return "project.view"
    if (
        normalized_path.endswith("/research-assistant/messages")
        or normalized_path.endswith("/evidence-packs")
        or normalized_path.endswith("/knowledge-coverage")
    ):
        return "project.view"
    if (
        normalized_path.endswith("/publish")
        or normalized_path.endswith("/confirm")
    ):
        return "knowledge.publish"
    if normalized_method == "DELETE":
        return "knowledge.delete"
    return "knowledge.edit"


def server_knowledge_route_ready(method: str, route_path: str) -> bool:
    """Return whether the route has every server-side storage dependency."""

    normalized_method = method.strip().upper()
    normalized_path = route_path.rstrip("/")
    if "/wordpress/" in normalized_path:
        return False
    if normalized_path.endswith("/sources/upload"):
        return False
    if normalized_path.endswith("/raw"):
        return False
    if normalized_path.endswith("/retrieval-plan"):
        # This compatibility route still reads the legacy global TaskStore.
        # Keep it closed until it is backed by the authorized project-scoped
        # PostgreSQL Task repository.
        return False
    if (
        normalized_method == "POST"
        and (
            normalized_path.endswith("/research-runs")
            or normalized_path.endswith("/resume")
        )
    ):
        return False
    return True


def server_http_route_available(method: str, path: str) -> bool:
    """Fail closed around legacy routes until their project scope is wired."""

    normalized_method = method.strip().upper()
    if normalized_method == "OPTIONS":
        return True
    normalized_path = "/" + path.strip().lstrip("/")
    if normalized_method == "GET" and normalized_path in {
        "/api/health",
        "/api/auth/status",
        "/api/auth/oidc/start",
        "/api/auth/oidc/callback",
    }:
        return True
    if normalized_method == "POST" and normalized_path in {
        "/api/auth/login",
        "/api/auth/logout",
        "/api/auth/invitations/prepare",
    }:
        return True
    if normalized_method == "GET" and normalized_path.rstrip("/") == (
        "/api/projects"
    ):
        return True
    parts = normalized_path.rstrip("/").split("/")
    if (
        normalized_method == "GET"
        and len(parts) in {5, 6}
        and parts[1:3] == ["api", "projects"]
        and bool(parts[3])
        and parts[4] == "members"
        and (len(parts) == 5 or parts[5] == "candidates")
    ):
        return True
    if (
        normalized_method in {"PUT", "DELETE"}
        and len(parts) == 6
        and parts[1:3] == ["api", "projects"]
        and bool(parts[3])
        and parts[4] == "members"
        and bool(parts[5])
    ):
        return True
    if (
        normalized_method in {"GET", "POST"}
        and len(parts) == 5
        and parts[1:3] == ["api", "organizations"]
        and bool(parts[3])
        and parts[4] == "users"
    ):
        return True
    if (
        normalized_method in {"GET", "POST"}
        and len(parts) == 5
        and parts[1:3] == ["api", "organizations"]
        and bool(parts[3])
        and parts[4] == "external-identities"
    ):
        return True
    if (
        normalized_method == "DELETE"
        and len(parts) == 6
        and parts[1:3] == ["api", "organizations"]
        and bool(parts[3])
        and parts[4] == "external-identities"
        and bool(parts[5])
    ):
        return True
    if (
        normalized_method in {"GET", "POST"}
        and len(parts) == 5
        and parts[1:3] == ["api", "organizations"]
        and bool(parts[3])
        and parts[4] == "invitations"
    ):
        return True
    if (
        normalized_method == "DELETE"
        and len(parts) == 6
        and parts[1:3] == ["api", "organizations"]
        and bool(parts[3])
        and parts[4] == "invitations"
        and bool(parts[5])
    ):
        return True
    if (
        normalized_method == "PATCH"
        and len(parts) == 6
        and parts[1:3] == ["api", "organizations"]
        and bool(parts[3])
        and parts[4] == "users"
        and bool(parts[5])
    ):
        return True
    if (
        normalized_method == "POST"
        and len(parts) == 8
        and parts[1:3] == ["api", "organizations"]
        and bool(parts[3])
        and parts[4] == "users"
        and bool(parts[5])
        and parts[6:8] == ["sessions", "revoke"]
    ):
        return True
    if (
        normalized_method in {"GET", "POST"}
        and len(parts) == 5
        and parts[1:3] == ["api", "organizations"]
        and bool(parts[3])
        and parts[4] == "teams"
    ):
        return True
    if (
        normalized_method == "PATCH"
        and len(parts) == 6
        and parts[1:3] == ["api", "organizations"]
        and bool(parts[3])
        and parts[4] == "teams"
        and bool(parts[5])
    ):
        return True
    if (
        normalized_method == "GET"
        and len(parts) == 7
        and parts[1:3] == ["api", "organizations"]
        and bool(parts[3])
        and parts[4] == "teams"
        and bool(parts[5])
        and parts[6] == "members"
    ):
        return True
    if (
        normalized_method in {"PUT", "DELETE"}
        and len(parts) == 8
        and parts[1:3] == ["api", "organizations"]
        and bool(parts[3])
        and parts[4] == "teams"
        and bool(parts[5])
        and parts[6] == "members"
        and bool(parts[7])
    ):
        return True
    if (
        normalized_method == "GET"
        and len(parts) in {5, 6}
        and parts[1:3] == ["api", "projects"]
        and bool(parts[3])
        and parts[4] == "batches"
        and (len(parts) == 5 or bool(parts[5]))
    ):
        return True
    if (
        normalized_method == "POST"
        and len(parts) == 7
        and parts[1:3] == ["api", "projects"]
        and bool(parts[3])
        and parts[4] == "batches"
        and bool(parts[5])
        and parts[6] == "cancel"
    ):
        return True
    if (
        normalized_method == "POST"
        and len(parts) == 7
        and parts[1:3] == ["api", "projects"]
        and bool(parts[3])
        and parts[4] == "jobs"
        and bool(parts[5])
        and parts[6] in {"cancel", "retry"}
    ):
        return True
    if (
        normalized_method == "GET"
        and len(parts) in {5, 6}
        and parts[1:3] == ["api", "projects"]
        and bool(parts[3])
        and parts[4] == "tasks"
        and (len(parts) == 5 or bool(parts[5]))
    ):
        return True
    if (
        normalized_method == "POST"
        and len(parts) == 7
        and parts[1:3] == ["api", "projects"]
        and bool(parts[3])
        and parts[4] == "tasks"
        and bool(parts[5])
        and parts[6] == "rewrite-from-scratch"
    ):
        return True
    if (
        normalized_method == "PUT"
        and len(parts) == 7
        and parts[1:3] == ["api", "projects"]
        and bool(parts[3])
        and parts[4] == "tasks"
        and bool(parts[5])
        and parts[6] in {"products", "selected-title"}
    ):
        return True
    if (
        normalized_method == "POST"
        and len(parts) == 7
        and parts[1:3] == ["api", "projects"]
        and bool(parts[3])
        and parts[4] == "tasks"
        and bool(parts[5])
        and parts[6] == "product-rediscovery"
    ):
        return True
    if (
        normalized_method == "POST"
        and len(parts) == 7
        and parts[1:3] == ["api", "projects"]
        and bool(parts[3])
        and parts[4] == "tasks"
        and bool(parts[5])
        and parts[6] == "prepare-images"
    ):
        return True
    if (
        normalized_method == "POST"
        and len(parts) == 9
        and parts[1:3] == ["api", "projects"]
        and bool(parts[3])
        and parts[4] == "tasks"
        and bool(parts[5])
        and parts[6:9] == ["checks", "final-ai", "screenshot"]
    ):
        return True
    if (
        normalized_method == "PUT"
        and len(parts) == 8
        and parts[1:3] == ["api", "projects"]
        and bool(parts[3])
        and parts[4] == "tasks"
        and bool(parts[5])
        and parts[6:8] == ["checks", "final-ai"]
    ):
        return True
    if (
        normalized_method == "GET"
        and len(parts) == 10
        and parts[1:3] == ["api", "projects"]
        and bool(parts[3])
        and parts[4] == "tasks"
        and bool(parts[5])
        and parts[6:10]
        == ["checks", "final-ai", "screenshot", "download"]
    ):
        return True
    if (
        normalized_method == "POST"
        and len(parts) == 7
        and parts[1:3] == ["api", "projects"]
        and bool(parts[3])
        and parts[4] == "tasks"
        and bool(parts[5])
        and parts[6] == "export-docx"
    ):
        return True
    if (
        normalized_method == "GET"
        and len(parts) == 8
        and parts[1:3] == ["api", "projects"]
        and bool(parts[3])
        and parts[4] == "tasks"
        and bool(parts[5])
        and parts[6:8] == ["docx", "download"]
    ):
        return True
    if (
        normalized_method == "POST"
        and len(parts) == 7
        and parts[1:3] == ["api", "projects"]
        and bool(parts[3])
        and parts[4] == "tasks"
        and bool(parts[5])
        and parts[6] == "generate-tdk"
    ):
        return True
    if (
        normalized_method == "POST"
        and len(parts) == 7
        and parts[1:3] == ["api", "projects"]
        and bool(parts[3])
        and parts[4] == "tasks"
        and bool(parts[5])
        and parts[6] == "package-delivery"
    ):
        return True
    if (
        normalized_method == "GET"
        and len(parts) == 8
        and parts[1:3] == ["api", "projects"]
        and bool(parts[3])
        and parts[4] == "tasks"
        and bool(parts[5])
        and parts[6:8] == ["delivery-package", "download"]
    ):
        return True
    if (
        normalized_method == "GET"
        and len(parts) == 8
        and parts[1:3] == ["api", "projects"]
        and bool(parts[3])
        and parts[4] == "tasks"
        and bool(parts[5])
        and parts[6:8] == ["tdk", "download"]
    ):
        return True
    if (
        normalized_method == "GET"
        and len(parts) == 9
        and parts[1:3] == ["api", "projects"]
        and bool(parts[3])
        and parts[4] == "tasks"
        and bool(parts[5])
        and parts[6:8] == ["product-rediscovery", "jobs"]
        and bool(parts[8])
    ):
        return True
    if (
        normalized_method == "PUT"
        and len(parts) == 8
        and parts[1:3] == ["api", "projects"]
        and bool(parts[3])
        and parts[4] == "tasks"
        and bool(parts[5])
        and parts[6:8] == ["article", "sections"]
    ):
        return True
    if (
        normalized_method == "GET"
        and len(parts) == 7
        and parts[1:3] == ["api", "projects"]
        and bool(parts[3])
        and parts[4] == "assets"
        and bool(parts[5])
        and parts[6] == "download"
    ):
        return True
    return normalized_path.startswith("/api/knowledge/")


@dataclass(frozen=True)
class AuthorizedProjectRequest:
    actor: ActorIdentity
    project_id: str
    permission: ProjectPermission


class ServerRequestSecurity:
    """Authenticate a signed Actor and authorize one normalized project."""

    def __init__(
        self,
        *,
        codec: ServerActorSessionCodec,
        access: ProjectAccessService,
        sessions: ActorSessionVersionReader,
    ) -> None:
        self._codec = codec
        self._access = access
        self._sessions = sessions

    def authenticate(self, token: str) -> ActorIdentity:
        try:
            session = self._codec.parse_session(token)
        except ServerActorSessionError as exc:
            raise ServerRequestUnauthenticated(
                "authentication required"
            ) from exc
        try:
            current = self._sessions.is_current(session)
        except Exception as exc:
            raise ServerRequestUnauthenticated(
                "authentication required"
            ) from exc
        if not current:
            raise ServerRequestUnauthenticated("authentication required")
        return session.actor

    def authorize_project(
        self,
        *,
        token: str,
        project: str,
        permission: ProjectPermission,
    ) -> AuthorizedProjectRequest:
        actor = self.authenticate(token)
        project_id = normalized_customer(project)
        if not project_id:
            raise ServerRequestForbidden("project access denied")
        try:
            self._access.require(actor, project_id, permission)
        except ProjectAccessDenied as exc:
            raise ServerRequestForbidden("project access denied") from exc
        return AuthorizedProjectRequest(
            actor=actor,
            project_id=project_id,
            permission=permission,
        )


__all__ = [
    "AuthorizedProjectRequest",
    "ServerRequestForbidden",
    "ServerRequestSecurity",
    "ServerRequestUnauthenticated",
    "knowledge_permission_for",
    "server_http_route_available",
    "server_knowledge_route_ready",
]
