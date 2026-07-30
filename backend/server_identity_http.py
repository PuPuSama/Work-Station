from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from server_project_http import require_server_actor
from services.access_control import ActorIdentity
from services.external_identity import VerifiedExternalIdentity
from services.external_identity_provisioning import (
    ExternalIdentityMappingNotFound,
    ExternalIdentityMappingRecord,
    ExternalIdentityProvisioningDenied,
    ExternalIdentityProvisioningUnavailable,
    PostgresExternalIdentityProvisioningService,
)


class ExternalIdentityLinkRequest(BaseModel):
    """Accept a raw Subject only on the write boundary; never echo it."""

    model_config = ConfigDict(extra="forbid")

    issuer: str = Field(min_length=1, max_length=2048)
    subject: str = Field(min_length=1, max_length=512)
    user_id: str = Field(min_length=1, max_length=200)

    @field_validator("issuer", "subject", "user_id")
    @classmethod
    def validate_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized


class ExternalIdentityMappingResponse(BaseModel):
    mapping_id: str
    issuer: str
    user_id: str
    user_display_name: str
    user_status: Literal["active", "disabled"]
    status: Literal["active", "revoked"]


class ExternalIdentityMappingListResponse(BaseModel):
    items: list[ExternalIdentityMappingResponse]
    next_after_mapping_id: str | None = None


def _service(
    request: Request,
) -> PostgresExternalIdentityProvisioningService:
    service = getattr(
        request.app.state,
        "server_external_identity_provisioning",
        None,
    )
    if not isinstance(
        service,
        PostgresExternalIdentityProvisioningService,
    ):
        raise HTTPException(
            status_code=503,
            detail="External identity management is not available.",
        )
    return service


def _response(
    record: ExternalIdentityMappingRecord,
) -> ExternalIdentityMappingResponse:
    return ExternalIdentityMappingResponse(
        mapping_id=record.mapping_id,
        issuer=record.issuer,
        user_id=record.user_id,
        user_display_name=record.user_display_name,
        user_status=record.user_status,
        status=record.status,
    )


def _raise_http_error(exc: Exception) -> None:
    if isinstance(exc, ExternalIdentityProvisioningDenied):
        raise HTTPException(
            status_code=403,
            detail="external identity provisioning denied",
        ) from exc
    if isinstance(exc, ExternalIdentityMappingNotFound):
        raise HTTPException(
            status_code=404,
            detail="external identity mapping is unavailable",
        ) from exc
    if isinstance(exc, ExternalIdentityProvisioningUnavailable):
        raise HTTPException(
            status_code=503,
            detail="External identity management is unavailable.",
        ) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    raise exc


_IDENTITY_ERRORS = (
    ExternalIdentityMappingNotFound,
    ExternalIdentityProvisioningDenied,
    ExternalIdentityProvisioningUnavailable,
    ValueError,
)


router = APIRouter(
    prefix="/api/organizations",
    tags=["server-identity-administration"],
)


@router.get(
    "/{organization_id}/external-identities",
    response_model=ExternalIdentityMappingListResponse,
)
def list_external_identity_mappings(
    organization_id: str,
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    after_mapping_id: str | None = Query(default=None, min_length=1),
    actor: ActorIdentity = Depends(require_server_actor),
) -> ExternalIdentityMappingListResponse:
    try:
        page = _service(request).list_mappings(
            actor=actor,
            organization_id=organization_id,
            limit=limit,
            after_mapping_id=after_mapping_id,
        )
    except _IDENTITY_ERRORS as exc:
        _raise_http_error(exc)
        raise AssertionError("identity error mapping returned")
    return ExternalIdentityMappingListResponse(
        items=[_response(item) for item in page.items],
        next_after_mapping_id=page.next_after_mapping_id,
    )


@router.post(
    "/{organization_id}/external-identities",
    response_model=ExternalIdentityMappingResponse,
    status_code=201,
)
def link_external_identity(
    organization_id: str,
    payload: ExternalIdentityLinkRequest,
    request: Request,
    actor: ActorIdentity = Depends(require_server_actor),
) -> ExternalIdentityMappingResponse:
    if organization_id.strip() != actor.organization_id:
        raise HTTPException(
            status_code=403,
            detail="external identity provisioning denied",
        )
    try:
        identity = VerifiedExternalIdentity(
            issuer=payload.issuer,
            subject=payload.subject,
        )
        record = _service(request).link(
            actor=actor,
            identity=identity,
            user_id=payload.user_id,
            event_id=f"external_identity_link_{uuid.uuid4().hex}",
        )
    except _IDENTITY_ERRORS as exc:
        _raise_http_error(exc)
        raise AssertionError("identity error mapping returned")
    return _response(record)


@router.delete(
    "/{organization_id}/external-identities/{mapping_id}",
    response_model=ExternalIdentityMappingResponse,
)
def revoke_external_identity(
    organization_id: str,
    mapping_id: str,
    request: Request,
    actor: ActorIdentity = Depends(require_server_actor),
) -> ExternalIdentityMappingResponse:
    try:
        record = _service(request).revoke_mapping(
            actor=actor,
            organization_id=organization_id,
            mapping_id=mapping_id,
            event_id=f"external_identity_revoke_{uuid.uuid4().hex}",
        )
    except _IDENTITY_ERRORS as exc:
        _raise_http_error(exc)
        raise AssertionError("identity error mapping returned")
    return _response(record)


__all__ = [
    "ExternalIdentityLinkRequest",
    "ExternalIdentityMappingListResponse",
    "ExternalIdentityMappingResponse",
    "router",
]
