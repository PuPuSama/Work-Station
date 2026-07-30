from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from services.access_control import (
    ActorIdentity,
    ProjectAccessDenied,
    ProjectAccessService,
    ProjectPermission,
)
from services.job_queue import JobConflict
from services.postgres_job_queue import PostgresJobQueue


JobHandler = Callable[[dict[str, Any], Callable[[], bool]], int]


def worker_permission_for(operation: str) -> ProjectPermission:
    """Map queued work to the least conservative existing M7 permission."""

    normalized = operation.strip()
    if normalized == "seo_review":
        return "article.review"
    if normalized in {
        "export_docx",
        "generate_tdk",
        "package_delivery",
    }:
        return "article.deliver"
    if normalized in {
        "knowledge_research",
        "product_rediscovery",
    }:
        return "knowledge.edit"
    return "article.edit"


class AuthorizedPostgresJobQueue:
    """Authorize minimal Job metadata before returning private Job input."""

    def __init__(
        self,
        queue: PostgresJobQueue,
        *,
        access: ProjectAccessService,
        scan_multiplier: int = 4,
    ) -> None:
        self._queue = queue
        self._access = access
        self.organization_id = queue.organization_id
        self.project_id = queue.project_id
        self.worker_id = queue.worker_id
        self._scan_multiplier = max(1, int(scan_multiplier))

    def recover_interrupted(
        self,
        operations: Iterable[str] | None = None,
    ) -> int:
        return self._queue.recover_interrupted(operations)

    def claim_jobs(
        self,
        limit: int,
        operations: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        claimed: list[dict[str, Any]] = []
        attempted: set[str] = set()
        for _attempt in range(self._scan_multiplier):
            remaining = limit - len(claimed)
            if remaining <= 0:
                break
            scan_limit = min(
                100,
                max(remaining, remaining * self._scan_multiplier),
            )
            candidates = [
                candidate
                for candidate in self._queue.list_claim_candidates(
                    scan_limit,
                    operations,
                )
                if candidate.job_id not in attempted
            ]
            if not candidates:
                break
            allowed_ids: list[str] = []
            for candidate in candidates:
                attempted.add(candidate.job_id)
                requester = candidate.requested_by_user_id
                if requester is None:
                    self._queue.reject_pending_authorization(
                        candidate.job_id
                    )
                    continue
                try:
                    self._access.require(
                        ActorIdentity(
                            self.organization_id,
                            requester,
                        ),
                        self.project_id,
                        worker_permission_for(
                            candidate.operation
                        ),
                    )
                except (ProjectAccessDenied, ValueError):
                    self._queue.reject_pending_authorization(
                        candidate.job_id
                    )
                    continue
                allowed_ids.append(candidate.job_id)
                if len(allowed_ids) >= remaining:
                    break
            newly_claimed = self._queue.claim_jobs_by_id(
                allowed_ids,
                limit=remaining,
            )
            claimed.extend(newly_claimed)
            if not allowed_ids and not newly_claimed:
                continue
        return claimed

    def is_cancel_requested(self, job_id: str) -> bool:
        return self._queue.is_cancel_requested(job_id)

    def mark_succeeded(self, job_id: str, result_revision: int) -> None:
        self._queue.mark_succeeded(job_id, result_revision)

    def mark_cancelled(self, job_id: str) -> None:
        self._queue.mark_cancelled(job_id)

    def mark_interrupted(self, job_id: str) -> None:
        self._queue.mark_interrupted(job_id)

    def mark_conflict(self, job_id: str, error: str) -> None:
        self._queue.mark_conflict(job_id, error)

    def mark_failed(
        self,
        job_id: str,
        error: str,
        *,
        retryable: bool,
    ) -> str:
        return self._queue.mark_failed(
            job_id,
            error,
            retryable=retryable,
        )


class ReauthorizingJobHandler:
    """Recheck the requesting Actor immediately before business execution."""

    def __init__(
        self,
        handler: JobHandler,
        *,
        access: ProjectAccessService,
    ) -> None:
        self._handler = handler
        self._access = access

    def __call__(
        self,
        job: dict[str, Any],
        cancelled: Callable[[], bool],
    ) -> int:
        organization_id = str(
            job.get("organization_id") or ""
        ).strip()
        project_id = str(job.get("project_id") or "").strip()
        requester = str(
            job.get("requested_by_user_id") or ""
        ).strip()
        operation = str(job.get("operation") or "")
        try:
            actor = ActorIdentity(organization_id, requester)
            self._access.require(
                actor,
                project_id,
                worker_permission_for(operation),
            )
        except (ProjectAccessDenied, ValueError) as exc:
            raise JobConflict(
                "job actor is not authorized"
            ) from exc
        return self._handler(job, cancelled)


__all__ = [
    "AuthorizedPostgresJobQueue",
    "ReauthorizingJobHandler",
    "worker_permission_for",
]
