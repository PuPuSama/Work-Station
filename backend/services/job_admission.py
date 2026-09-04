from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from threading import Condition, Lock


JobScope = tuple[str, str]


def _scope_value(value: object, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized


@dataclass(frozen=True, slots=True)
class JobAdmissionSnapshot:
    """Process-local execution capacity, safe to expose in diagnostics."""

    global_limit: int
    active_jobs: int
    project_limits: dict[JobScope, int]
    project_active_jobs: dict[JobScope, int]


class JobAdmissionController:
    """Bound all Server Job runners in one process.

    PostgreSQL still owns Job state and leases. This controller only decides
    whether a local runner may claim another durable Job, so waiting work stays
    queued instead of occupying a thread while it waits for capacity.
    """

    def __init__(self, *, global_limit: int, project_limit: int) -> None:
        if not 1 <= int(global_limit) <= 128:
            raise ValueError("global_limit must be between 1 and 128")
        if not 1 <= int(project_limit) <= 32:
            raise ValueError("project_limit must be between 1 and 32")
        self.global_limit = int(global_limit)
        self.project_limit = int(project_limit)
        self._lock = Condition()
        self._active_jobs = 0
        self._project_active_jobs: defaultdict[JobScope, int] = defaultdict(int)

    def reserve(
        self,
        organization_id: object,
        project_id: object,
        requested: int,
    ) -> int:
        """Atomically reserve up to ``requested`` execution slots."""

        if requested <= 0:
            return 0
        scope = (
            _scope_value(organization_id, "organization_id"),
            _scope_value(project_id, "project_id"),
        )
        with self._lock:
            available = min(
                int(requested),
                self.global_limit - self._active_jobs,
                self.project_limit - self._project_active_jobs[scope],
            )
            if available <= 0:
                return 0
            self._active_jobs += available
            self._project_active_jobs[scope] += available
            return available

    def release(
        self,
        organization_id: object,
        project_id: object,
        count: int = 1,
    ) -> None:
        """Release slots after a claimed Job reaches a durable boundary."""

        if count <= 0:
            return
        scope = (
            _scope_value(organization_id, "organization_id"),
            _scope_value(project_id, "project_id"),
        )
        with self._lock:
            project_active = self._project_active_jobs.get(scope, 0)
            if count > project_active or count > self._active_jobs:
                raise RuntimeError("job admission release exceeds active slots")
            remaining = project_active - count
            if remaining:
                self._project_active_jobs[scope] = remaining
            else:
                self._project_active_jobs.pop(scope, None)
            self._active_jobs -= count
            self._lock.notify_all()

    def wait_for_capacity(self, timeout_seconds: float = 2.0) -> None:
        """Sleep until a slot is released without polling PostgreSQL."""

        if timeout_seconds <= 0:
            return
        with self._lock:
            self._lock.wait(timeout_seconds)

    def snapshot(self) -> JobAdmissionSnapshot:
        with self._lock:
            project_active = dict(self._project_active_jobs)
        return JobAdmissionSnapshot(
            global_limit=self.global_limit,
            active_jobs=sum(project_active.values()),
            project_limits={
                scope: self.project_limit
                for scope in project_active
            },
            project_active_jobs=project_active,
        )


_PROCESS_CONTROLLER: JobAdmissionController | None = None
_PROCESS_CONTROLLER_LOCK = Lock()


def configure_process_job_admission(
    *,
    global_limit: int,
    project_limit: int,
) -> JobAdmissionController:
    """Install the controller shared by all authorized runners in this process."""

    global _PROCESS_CONTROLLER
    controller = JobAdmissionController(
        global_limit=global_limit,
        project_limit=project_limit,
    )
    with _PROCESS_CONTROLLER_LOCK:
        _PROCESS_CONTROLLER = controller
    return controller


def process_job_admission() -> JobAdmissionController | None:
    with _PROCESS_CONTROLLER_LOCK:
        return _PROCESS_CONTROLLER


__all__ = [
    "JobAdmissionController",
    "JobAdmissionSnapshot",
    "JobScope",
    "configure_process_job_admission",
    "process_job_admission",
]
