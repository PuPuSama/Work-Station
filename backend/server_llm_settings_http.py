from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from config import load_runtime_config
from models import LlmSettingsUpdateRequest
from services.access_control import ActorIdentity
from services.server_llm_settings import (
    PostgresServerLlmSettings,
    ServerLlmSettings,
    ServerLlmSettingsConflict,
    ServerLlmSettingsDenied,
    ServerLlmSettingsUnavailable,
)
from server_project_http import require_server_actor


class LlmSettingsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    reasoning_effort: str
    available_models: list[str]
    available_reasoning_efforts: list[str]
    revision: int = Field(ge=0)
    updated_at: str | None = None
    can_edit: bool


def _service(request: Request) -> PostgresServerLlmSettings:
    service = getattr(request.app.state, "server_llm_settings", None)
    if not isinstance(service, PostgresServerLlmSettings):
        raise HTTPException(
            status_code=503,
            detail="User model settings are not available.",
        )
    return service


def _with_current(current: str, configured: tuple[str, ...]) -> list[str]:
    values = list(configured)
    if current and current not in values:
        values.insert(0, current)
    return values


def _response(
    settings: ServerLlmSettings,
    *,
    available_models: tuple[str, ...],
    available_reasoning_efforts: tuple[str, ...],
) -> LlmSettingsResponse:
    return LlmSettingsResponse(
        model=settings.model,
        reasoning_effort=settings.reasoning_effort,
        available_models=_with_current(settings.model, available_models),
        available_reasoning_efforts=_with_current(
            settings.reasoning_effort,
            available_reasoning_efforts,
        ),
        revision=settings.revision,
        updated_at=settings.updated_at,
        can_edit=settings.can_edit,
    )


router = APIRouter(
    prefix="/api/settings",
    tags=["server-settings"],
)


@router.get("/llm", response_model=LlmSettingsResponse)
def read_llm_settings(
    request: Request,
    actor: ActorIdentity = Depends(require_server_actor),
) -> LlmSettingsResponse:
    cfg = load_runtime_config()
    try:
        settings = _service(request).get(
            actor=actor,
            fallback_model=cfg.llm_model,
            fallback_reasoning_effort=cfg.llm_reasoning_effort,
        )
    except ServerLlmSettingsDenied as exc:
        raise HTTPException(status_code=403, detail="user model settings read denied") from exc
    except ServerLlmSettingsUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="User model settings are temporarily unavailable.",
        ) from exc
    return _response(
        settings,
        available_models=cfg.llm_available_models,
        available_reasoning_efforts=cfg.llm_available_reasoning_efforts,
    )


@router.put("/llm", response_model=LlmSettingsResponse)
def update_llm_settings(
    payload: LlmSettingsUpdateRequest,
    request: Request,
    actor: ActorIdentity = Depends(require_server_actor),
) -> LlmSettingsResponse:
    cfg = load_runtime_config()
    try:
        settings = _service(request).update(
            actor=actor,
            expected_revision=payload.revision,
            model=payload.model,
            reasoning_effort=payload.reasoning_effort,
            allowed_models=cfg.llm_available_models,
            allowed_reasoning_efforts=cfg.llm_available_reasoning_efforts,
            event_id=f"llm_settings_{uuid.uuid4().hex}",
        )
    except ServerLlmSettingsDenied as exc:
        raise HTTPException(status_code=403, detail="user model settings update denied") from exc
    except ServerLlmSettingsConflict as exc:
        raise HTTPException(
            status_code=409,
            detail="your model settings changed; reload and try again",
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ServerLlmSettingsUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="User model settings could not be updated.",
        ) from exc
    return _response(
        settings,
        available_models=cfg.llm_available_models,
        available_reasoning_efforts=cfg.llm_available_reasoning_efforts,
    )


__all__ = ["LlmSettingsResponse", "router"]
