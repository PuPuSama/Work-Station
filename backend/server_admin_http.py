from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from services.access_control import ActorIdentity
from services.actor_sessions import (
    ActorSessionRevocationDenied,
    ActorSessionRevocationError,
    PostgresActorSessionRevocationService,
)
from server_project_http import require_server_actor


class RevokeActorSessionsRequest(BaseModel):
    """An intentionally empty body; versions and roles are server facts."""

    model_config = ConfigDict(extra="forbid")


class RevokeActorSessionsResponse(BaseModel):
    user_id: str
    revoked: bool


def _revocation_service(
    request: Request,
) -> PostgresActorSessionRevocationService:
    service = getattr(
        request.app.state,
        "server_actor_session_revocation",
        None,
    )
    if not isinstance(service, PostgresActorSessionRevocationService):
        raise HTTPException(
            status_code=503,
            detail="Actor session management is not available.",
        )
    return service


router = APIRouter(
    prefix="/api/organizations",
    tags=["server-administration"],
)


@router.post(
    "/{organization_id}/users/{user_id}/sessions/revoke",
    response_model=RevokeActorSessionsResponse,
)
def revoke_actor_sessions(
    organization_id: str,
    user_id: str,
    payload: RevokeActorSessionsRequest,
    request: Request,
    actor: ActorIdentity = Depends(require_server_actor),
) -> RevokeActorSessionsResponse:
    del payload
    if organization_id.strip() != actor.organization_id:
        raise HTTPException(
            status_code=403,
            detail="actor session revocation denied",
        )
    try:
        _revocation_service(request).revoke_all(
            actor=actor,
            user_id=user_id,
            event_id=f"session_revoke_{uuid.uuid4().hex}",
        )
    except ActorSessionRevocationDenied as exc:
        raise HTTPException(
            status_code=403,
            detail="actor session revocation denied",
        ) from exc
    except ActorSessionRevocationError as exc:
        raise HTTPException(
            status_code=503,
            detail="Actor sessions could not be revoked.",
        ) from exc
    return RevokeActorSessionsResponse(
        user_id=user_id.strip(),
        revoked=True,
    )


__all__ = [
    "RevokeActorSessionsRequest",
    "RevokeActorSessionsResponse",
    "router",
]
