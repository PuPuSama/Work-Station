from __future__ import annotations

from dataclasses import dataclass, field
from threading import Event, Thread
from typing import Any, Callable, Literal, Mapping, Protocol


AttachmentJobOperation = Literal[
    "classify_attachment",
    "preview_import_proposal",
    "execute_import_proposal",
]
AttachmentJobStatus = Literal[
    "queued",
    "running",
    "retry_wait",
    "succeeded",
    "failed",
    "cancelled",
    "conflict",
]
AuthorizationPhase = Literal["execute", "commit"]

ATTACHMENT_JOB_OPERATIONS: tuple[AttachmentJobOperation, ...] = (
    "classify_attachment",
    "preview_import_proposal",
    "execute_import_proposal",
)
ACTIVE_ATTACHMENT_JOB_STATUSES = ("queued", "running", "retry_wait")
TERMINAL_ATTACHMENT_JOB_STATUSES = (
    "succeeded",
    "failed",
    "cancelled",
    "conflict",
)


class AttachmentJobError(RuntimeError):
    code = "attachment_job_error"


class AttachmentJobConflict(AttachmentJobError):
    code = "attachment_job_conflict"


class AttachmentJobNotFound(AttachmentJobError):
    code = "attachment_job_not_found"


class AttachmentJobAuthorizationChanged(AttachmentJobError):
    code = "authorization_changed"


class AttachmentJobRetryableError(AttachmentJobError):
    code = "transient_failure"


def validate_attachment_job_target(
    *,
    operation: AttachmentJobOperation,
    project_id: str | None,
    proposal_id: str | None,
    expected_proposal_revision: int | None,
) -> None:
    """Preview creates its proposal only after a successful diff build."""

    if operation not in ATTACHMENT_JOB_OPERATIONS:
        raise ValueError("unsupported attachment job operation")
    if operation == "classify_attachment":
        if proposal_id is not None or expected_proposal_revision is not None:
            raise ValueError("classification jobs cannot target a proposal")
        return
    if operation == "preview_import_proposal":
        if (
            not project_id
            or proposal_id is not None
            or expected_proposal_revision is not None
        ):
            raise ValueError(
                "preview jobs require a project and cannot target a proposal"
            )
        return
    if (
        not project_id
        or not proposal_id
        or expected_proposal_revision is None
        or expected_proposal_revision < 0
    ):
        raise ValueError("execute jobs require project and proposal revisions")


@dataclass(frozen=True, slots=True)
class AttachmentJob:
    job_id: str
    organization_id: str
    requested_by_user_id: str
    project_id: str | None
    attachment_id: str
    proposal_id: str | None
    operation: AttachmentJobOperation
    idempotency_key: str
    expected_attachment_revision: int
    expected_proposal_revision: int | None
    request_payload: Mapping[str, Any] = field(default_factory=dict)
    result_payload: Mapping[str, Any] = field(default_factory=dict)
    result_attachment_revision: int | None = None
    result_proposal_revision: int | None = None
    status: AttachmentJobStatus = "queued"
    attempts: int = 0
    max_attempts: int = 4
    cancel_requested: bool = False
    standardized_error_code: str | None = None

    def __post_init__(self) -> None:
        validate_attachment_job_target(
            operation=self.operation,
            project_id=self.project_id,
            proposal_id=self.proposal_id,
            expected_proposal_revision=self.expected_proposal_revision,
        )
        if self.expected_attachment_revision < 0:
            raise ValueError("expected_attachment_revision must be non-negative")


@dataclass(frozen=True, slots=True)
class PendingAttachmentJobAuthorization:
    """Private request payload is intentionally absent before authorization."""

    job_id: str
    organization_id: str
    requested_by_user_id: str
    project_id: str | None
    attachment_id: str
    proposal_id: str | None
    operation: AttachmentJobOperation


@dataclass(frozen=True, slots=True)
class AttachmentJobResult:
    result_payload: Mapping[str, Any]
    attachment_revision: int
    proposal_revision: int | None = None


class AttachmentJobAuthorization(Protocol):
    def __call__(
        self,
        job: PendingAttachmentJobAuthorization | AttachmentJob,
        phase: AuthorizationPhase,
    ) -> None: ...


AttachmentJobHandler = Callable[
    [AttachmentJob, Callable[[], bool], Callable[[], None]], AttachmentJobResult
]


class AttachmentJobRepository(Protocol):
    def recover_interrupted(self) -> int: ...

    def list_claim_candidates(
        self, *, limit: int
    ) -> tuple[PendingAttachmentJobAuthorization, ...]: ...

    def claim_authorized(
        self, job_ids: tuple[str, ...], *, limit: int
    ) -> tuple[AttachmentJob, ...]: ...

    def reject_authorization(self, job_id: str) -> bool: ...

    def is_cancel_requested(self, job_id: str) -> bool: ...

    def renew_lease(self, job_id: str) -> bool: ...

    def mark_succeeded(self, job: AttachmentJob, result: AttachmentJobResult) -> None: ...

    def mark_failed(
        self, job: AttachmentJob, *, error_code: str, retryable: bool
    ) -> AttachmentJobStatus: ...

    def mark_cancelled(self, job: AttachmentJob) -> None: ...

    def mark_interrupted(self, job: AttachmentJob) -> None: ...


class PendingAttachmentJobOrganizationDiscovery(Protocol):
    def list_pending_organization_ids(self, *, limit: int) -> tuple[str, ...]: ...


class AttachmentJobRunner:
    """Small durable dispatcher with authorization at execute and commit."""

    def __init__(
        self,
        repository: AttachmentJobRepository,
        *,
        authorize: AttachmentJobAuthorization,
        handlers: Mapping[AttachmentJobOperation, AttachmentJobHandler],
    ) -> None:
        unknown = set(handlers).difference(ATTACHMENT_JOB_OPERATIONS)
        if unknown:
            raise ValueError("unsupported attachment job handler")
        self._repository = repository
        self._authorize = authorize
        self._handlers = dict(handlers)

    def recover(self) -> int:
        return self._repository.recover_interrupted()

    def run_once(self, *, limit: int = 1) -> int:
        if limit <= 0:
            return 0
        allowed: list[str] = []
        for candidate in self._repository.list_claim_candidates(limit=limit * 4):
            try:
                self._authorize(candidate, "execute")
            except Exception:
                self._repository.reject_authorization(candidate.job_id)
                continue
            allowed.append(candidate.job_id)
            if len(allowed) >= limit:
                break
        if not allowed:
            return 0
        claimed = self._repository.claim_authorized(tuple(allowed), limit=limit)
        for job in claimed:
            self._run_claimed(job)
        return len(claimed)

    def _run_claimed(self, job: AttachmentJob) -> None:
        cancelled = lambda: self._repository.is_cancel_requested(job.job_id)
        try:
            if cancelled():
                self._repository.mark_cancelled(job)
                return
            handler = self._handlers.get(job.operation)
            if handler is None:
                self._repository.mark_failed(
                    job,
                    error_code="handler_unavailable",
                    retryable=False,
                )
                return
            try:
                self._authorize(job, "execute")
            except Exception as exc:
                raise AttachmentJobAuthorizationChanged(
                    "attachment job authorization changed before execution"
                ) from exc
            result = self._run_with_lease_heartbeat(
                job,
                handler,
                cancelled,
            )
            self._repository.mark_succeeded(job, result)
        except AttachmentJobAuthorizationChanged:
            self._repository.mark_failed(
                job,
                error_code="authorization_changed",
                retryable=False,
            )
        except AttachmentJobRetryableError as exc:
            self._repository.mark_failed(
                job,
                error_code=exc.code,
                retryable=True,
            )
        except AttachmentJobConflict as exc:
            self._repository.mark_failed(
                job,
                error_code=exc.code,
                retryable=False,
            )
        except Exception:
            self._repository.mark_failed(
                job,
                error_code="job_execution_failed",
                retryable=False,
            )

    def _run_with_lease_heartbeat(
        self,
        job: AttachmentJob,
        handler: AttachmentJobHandler,
        cancelled: Callable[[], bool],
    ) -> AttachmentJobResult:
        stop = Event()
        renew = getattr(self._repository, "renew_lease", None)

        def heartbeat() -> None:
            while not stop.wait(30.0):
                try:
                    if not callable(renew) or not renew(job.job_id):
                        return
                except Exception:
                    return

        thread = None
        commit_authorized = False
        if callable(renew):
            thread = Thread(
                target=heartbeat,
                name=f"attachment-job-lease-{job.job_id[:24]}",
                daemon=True,
            )
            thread.start()

        def commit_guard() -> None:
            nonlocal commit_authorized
            if cancelled():
                raise AttachmentJobConflict("attachment job was cancelled")
            try:
                self._authorize(job, "commit")
            except Exception as exc:
                raise AttachmentJobAuthorizationChanged(
                    "attachment job authorization changed before commit"
                ) from exc
            commit_authorized = True

        try:
            result = handler(job, cancelled, commit_guard)
            if not commit_authorized:
                conflict = AttachmentJobConflict(
                    "attachment job handler skipped commit authorization"
                )
                conflict.code = "commit_authorization_missing"
                raise conflict
            return result
        finally:
            stop.set()
            if thread is not None:
                thread.join(1.0)


class AttachmentJobOrganizationDispatcher:
    """Discover durable work across organizations without a process registry."""

    def __init__(
        self,
        discovery: PendingAttachmentJobOrganizationDiscovery,
        *,
        runner_factory: Callable[[str], AttachmentJobRunner],
    ) -> None:
        self._discovery = discovery
        self._runner_factory = runner_factory

    def run_once(
        self,
        *,
        organization_limit: int = 25,
        jobs_per_organization: int = 1,
    ) -> int:
        if organization_limit <= 0 or jobs_per_organization <= 0:
            return 0
        handled = 0
        for organization_id in self._discovery.list_pending_organization_ids(
            limit=organization_limit
        ):
            runner = self._runner_factory(organization_id)
            runner.recover()
            handled += runner.run_once(limit=jobs_per_organization)
        return handled


__all__ = [
    "ACTIVE_ATTACHMENT_JOB_STATUSES",
    "ATTACHMENT_JOB_OPERATIONS",
    "AttachmentJob",
    "AttachmentJobAuthorization",
    "AttachmentJobAuthorizationChanged",
    "AttachmentJobConflict",
    "AttachmentJobError",
    "AttachmentJobHandler",
    "AttachmentJobNotFound",
    "AttachmentJobOperation",
    "AttachmentJobOrganizationDispatcher",
    "AttachmentJobRepository",
    "AttachmentJobResult",
    "AttachmentJobRetryableError",
    "AttachmentJobRunner",
    "AttachmentJobStatus",
    "PendingAttachmentJobAuthorization",
    "PendingAttachmentJobOrganizationDiscovery",
    "TERMINAL_ATTACHMENT_JOB_STATUSES",
    "validate_attachment_job_target",
]
