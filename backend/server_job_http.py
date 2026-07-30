from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict

from server_project_http import require_server_actor
from services.access_control import ActorIdentity, ProjectAccessDenied
from services.server_job_control import (
    PostgresServerJobControlService,
    ServerBatchPage,
    ServerBatchSummary,
    ServerJobControlConflict,
    ServerJobControlUnavailable,
    ServerJobSummary,
)


router = APIRouter(
    prefix="/api/projects",
    tags=["server-project-jobs"],
)


class ServerJobControlCommandRequest(BaseModel):
    """An explicit empty command body prevents private Job overrides."""

    model_config = ConfigDict(extra="forbid")


def _job_control(request: Request) -> PostgresServerJobControlService:
    service = getattr(
        request.app.state,
        "server_job_control",
        None,
    )
    if not isinstance(service, PostgresServerJobControlService):
        raise HTTPException(
            status_code=503,
            detail="Server Job control is not available.",
        )
    return service


def _denied(exc: ProjectAccessDenied) -> HTTPException:
    return HTTPException(
        status_code=403,
        detail="project access denied",
    )


def _unavailable(exc: ServerJobControlUnavailable) -> HTTPException:
    return HTTPException(
        status_code=503,
        detail="Server Job control is temporarily unavailable.",
    )


@router.get(
    "/{project}/batches",
    response_model=ServerBatchPage,
)
def list_project_batches(
    project: str,
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    after_batch_id: str | None = Query(
        default=None,
        min_length=1,
        max_length=128,
    ),
    actor: ActorIdentity = Depends(require_server_actor),
) -> ServerBatchPage:
    try:
        return _job_control(request).list_batches(
            actor=actor,
            project_id=project,
            limit=limit,
            after_batch_id=after_batch_id,
        )
    except ProjectAccessDenied as exc:
        raise _denied(exc) from exc
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail="Batch cursor not found.",
        ) from exc
    except ServerJobControlUnavailable as exc:
        raise _unavailable(exc) from exc


@router.get(
    "/{project}/batches/{batch_id}",
    response_model=ServerBatchSummary,
)
def read_project_batch(
    project: str,
    batch_id: str,
    request: Request,
    actor: ActorIdentity = Depends(require_server_actor),
) -> ServerBatchSummary:
    try:
        return _job_control(request).get_batch(
            actor=actor,
            project_id=project,
            batch_id=batch_id,
        )
    except ProjectAccessDenied as exc:
        raise _denied(exc) from exc
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail="Batch not found.",
        ) from exc
    except ServerJobControlUnavailable as exc:
        raise _unavailable(exc) from exc


@router.post(
    "/{project}/batches/{batch_id}/cancel",
    response_model=ServerBatchSummary,
)
def cancel_project_batch(
    project: str,
    batch_id: str,
    request: Request,
    command: ServerJobControlCommandRequest | None = Body(default=None),
    actor: ActorIdentity = Depends(require_server_actor),
) -> ServerBatchSummary:
    del command
    try:
        return _job_control(request).cancel_batch(
            actor=actor,
            project_id=project,
            batch_id=batch_id,
        )
    except ProjectAccessDenied as exc:
        raise _denied(exc) from exc
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail="Batch not found.",
        ) from exc
    except ServerJobControlUnavailable as exc:
        raise _unavailable(exc) from exc


@router.post(
    "/{project}/jobs/{job_id}/cancel",
    response_model=ServerJobSummary,
)
def cancel_project_job(
    project: str,
    job_id: str,
    request: Request,
    command: ServerJobControlCommandRequest | None = Body(default=None),
    actor: ActorIdentity = Depends(require_server_actor),
) -> ServerJobSummary:
    del command
    try:
        return _job_control(request).cancel_job(
            actor=actor,
            project_id=project,
            job_id=job_id,
        )
    except ProjectAccessDenied as exc:
        raise _denied(exc) from exc
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail="Job not found.",
        ) from exc
    except ServerJobControlUnavailable as exc:
        raise _unavailable(exc) from exc


@router.post(
    "/{project}/jobs/{job_id}/retry",
    response_model=ServerJobSummary,
)
def retry_project_job(
    project: str,
    job_id: str,
    request: Request,
    command: ServerJobControlCommandRequest | None = Body(default=None),
    actor: ActorIdentity = Depends(require_server_actor),
) -> ServerJobSummary:
    del command
    try:
        return _job_control(request).retry_job(
            actor=actor,
            project_id=project,
            job_id=job_id,
        )
    except ProjectAccessDenied as exc:
        raise _denied(exc) from exc
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail="Job not found.",
        ) from exc
    except ServerJobControlConflict as exc:
        raise HTTPException(
            status_code=409,
            detail="Job cannot be retried in its current state.",
        ) from exc
    except ServerJobControlUnavailable as exc:
        raise _unavailable(exc) from exc


__all__ = ["router"]
