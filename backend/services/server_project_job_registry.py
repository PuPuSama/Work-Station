from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from server_schema import background_jobs
from services.access_control import (
    ActorIdentity,
    ProjectAccessService,
)
from services.audit_log import AuditEventWriter
from services.authorized_job_queue import (
    DEFAULT_PROJECT_JOB_CONCURRENCY,
    authorized_batch_runner,
)
from services.job_queue import (
    ACTIVE_JOB_STATUSES,
    BatchJobRunner,
)
from services.postgres_job_queue import PostgresJobQueue


ProjectJobHandler = Callable[
    [dict[str, Any], Callable[[], bool]],
    int,
]


@dataclass(frozen=True, slots=True)
class ServerProjectJobStopReport:
    project_runner_count: int
    dispatcher_stopped: bool
    remaining_jobs: int

    @property
    def drained(self) -> bool:
        return self.dispatcher_stopped and self.remaining_jobs == 0


@dataclass(slots=True)
class _ProjectRunner:
    queue: PostgresJobQueue
    runner: BatchJobRunner | None


class ServerProjectJobRegistry:
    """Shared runner lifecycle; business enqueue contracts stay operation-specific."""

    def __init__(
        self,
        engine: Engine,
        *,
        operation: str,
        access: ProjectAccessService,
        handler: ProjectJobHandler | None,
        error_type: type[RuntimeError],
        terminal_audit: AuditEventWriter,
        project_job_concurrency: int = DEFAULT_PROJECT_JOB_CONCURRENCY,
    ) -> None:
        self._engine = engine
        self._operation = operation
        self._access = access
        self._handler = handler
        self._error_type = error_type
        self._terminal_audit = terminal_audit
        self._project_job_concurrency = project_job_concurrency
        self._lock = threading.Lock()
        self._closed = False
        self._projects: dict[tuple[str, str], _ProjectRunner] = {}
        self._stop_report: ServerProjectJobStopReport | None = None

    def project(
        self,
        organization_id: str,
        project_id: str,
        *,
        start_runner: bool,
    ) -> _ProjectRunner:
        scope = (organization_id, project_id)
        with self._lock:
            if self._closed:
                raise self._error_type(
                    f"{self._operation} runner is stopped"
                )
            current = self._projects.get(scope)
            if current is not None and (
                not start_runner or current.runner is not None
            ):
                return current
            if current is None:
                current = _ProjectRunner(
                    queue=PostgresJobQueue(
                        self._engine,
                        organization_id=organization_id,
                        project_id=project_id,
                        terminal_audit=self._terminal_audit,
                    ),
                    runner=None,
                )
                self._projects[scope] = current
            if not start_runner:
                return current
            if self._handler is None:
                raise self._error_type(
                    f"{self._operation} runner is not configured"
                )
            runner = authorized_batch_runner(
                current.queue,
                self._handler,
                access=self._access,
                operations=(self._operation,),
                concurrency=self._project_job_concurrency,
            )
            current.runner = runner
            try:
                runner.start()
            except Exception:
                current.runner = None
                runner.stop()
                raise
            return current

    def start_existing(self) -> None:
        if self._handler is None:
            return
        with self._engine.connect() as connection:
            scopes = connection.execute(
                sa.select(
                    background_jobs.c.organization_id,
                    background_jobs.c.project_id,
                )
                .where(
                    background_jobs.c.operation == self._operation,
                    background_jobs.c.status.in_(ACTIVE_JOB_STATUSES),
                )
                .distinct()
            ).all()
        for organization_id, project_id in scopes:
            project = self.project(
                str(organization_id),
                str(project_id),
                start_runner=True,
            )
            if project.runner is None:
                raise self._error_type(
                    f"{self._operation} runner did not start"
                )
            project.runner.wake()

    def get_job(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        task_id: str,
        job_id: str,
    ) -> dict[str, object]:
        self._access.require(actor, project_id, "project.view")
        project = self.project(
            actor.organization_id,
            project_id,
            start_runner=False,
        )
        job = project.queue.get_job(job_id)
        if (
            str(job["task_id"]) != task_id
            or str(job["operation"]) != self._operation
        ):
            raise KeyError(job_id)
        return public_job(job)

    def stop(
        self,
        *,
        timeout_seconds: float = 10.0,
    ) -> ServerProjectJobStopReport:
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative")
        with self._lock:
            if self._closed:
                return self._stop_report or ServerProjectJobStopReport(
                    project_runner_count=0,
                    dispatcher_stopped=True,
                    remaining_jobs=0,
                )
            self._closed = True
            runners = [
                project.runner
                for project in self._projects.values()
                if project.runner is not None
            ]
            self._projects.clear()
        deadline = time.monotonic() + timeout_seconds
        dispatcher_stopped = True
        remaining_jobs = 0
        for runner in runners:
            report = runner.stop(
                timeout_seconds=max(
                    0.0,
                    deadline - time.monotonic(),
                )
            )
            dispatcher_stopped = (
                dispatcher_stopped and report.dispatcher_stopped
            )
            remaining_jobs += report.remaining_jobs
        result = ServerProjectJobStopReport(
            project_runner_count=len(runners),
            dispatcher_stopped=dispatcher_stopped,
            remaining_jobs=remaining_jobs,
        )
        with self._lock:
            self._stop_report = result
        return result


def public_job(job: Mapping[str, object]) -> dict[str, object]:
    """Project Job projection that omits request, requester, and raw error."""

    def optional_text(value: object) -> str | None:
        normalized = "" if value is None else str(value).strip()
        return normalized or None

    return {
        "job_id": str(job["id"]),
        "batch_id": str(job["batch_id"]),
        "task_id": str(job["task_id"]),
        "operation": str(job["operation"]),
        "status": str(job["status"]),
        "source_revision": int(job["source_revision"]),
        "result_revision": (
            None
            if job.get("result_revision") is None
            else int(job["result_revision"])
        ),
        "attempts": int(job["attempts"]),
        "created_at": str(job["created_at"]),
        "started_at": optional_text(job.get("started_at")),
        "finished_at": optional_text(job.get("finished_at")),
        "updated_at": str(job["updated_at"]),
        "has_error": bool(str(job.get("error") or "")),
    }


__all__ = [
    "ProjectJobHandler",
    "ServerProjectJobRegistry",
    "ServerProjectJobStopReport",
    "public_job",
]
