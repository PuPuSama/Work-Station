from __future__ import annotations

from fastapi import HTTPException, Request

from services.server_auth import (
    SERVER_AUTH_COOKIE_NAME,
    server_mode_enabled,
)
from services.server_request_security import (
    ServerRequestForbidden,
    ServerRequestSecurity,
    ServerRequestUnauthenticated,
    knowledge_permission_for,
    server_knowledge_route_ready,
)


def require_knowledge_project_access(request: Request) -> None:
    """FastAPI dependency for every project-scoped Knowledge API route."""

    configured_mode = getattr(
        request.app.state,
        "server_mode_enabled",
        None,
    )
    enabled = (
        server_mode_enabled()
        if configured_mode is None
        else bool(configured_mode)
    )
    if not enabled:
        return

    security = getattr(
        request.app.state,
        "server_request_security",
        None,
    )
    if not isinstance(security, ServerRequestSecurity):
        raise HTTPException(
            status_code=503,
            detail="Server security is not available.",
        )
    project = str(request.path_params.get("project") or "")
    route = request.scope.get("route")
    route_path = str(getattr(route, "path", request.url.path))
    permission = knowledge_permission_for(request.method, route_path)
    try:
        authorized = security.authorize_project(
            token=request.cookies.get(SERVER_AUTH_COOKIE_NAME, ""),
            project=project,
            permission=permission,
        )
    except ServerRequestUnauthenticated as exc:
        raise HTTPException(
            status_code=401,
            detail="Authentication required.",
        ) from exc
    except ServerRequestForbidden as exc:
        raise HTTPException(
            status_code=403,
            detail="project access denied",
        ) from exc
    request.state.actor_identity = authorized.actor
    request.state.project_id = authorized.project_id
    request.state.project_permission = authorized.permission
    if not server_knowledge_route_ready(request.method, route_path):
        raise HTTPException(
            status_code=503,
            detail="Route is not available in server mode yet.",
        )


__all__ = ["require_knowledge_project_access"]
