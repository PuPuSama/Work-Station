from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from services.access_control import (
    ActorIdentity,
    ProjectAccessDenied,
    ProjectAccessService,
    ProjectPermission,
)
from services.job_queue import BatchJobRunner, JobConflict
from services.postgres_job_queue import PostgresJobQueue


JobHandler = Callable[[dict[str, Any], Callable[[], bool]], int]


WORKER_OPERATION_PERMISSIONS: dict[str, ProjectPermission] = {
    "article": "article.edit",
    "export_docx": "article.deliver",
    "generate_tdk": "article.deliver",
    "humanize": "article.edit",
    "knowledge_research": "knowledge.publish",
    "outline": "article.edit",
    "package_delivery": "article.deliver",
    "prepare_images": "article.edit",
    "product_rediscovery": "knowledge.edit",
    "products": "article.edit",
    "restore_links": "article.edit",
    "rewrite_article": "article.edit",
    "seo_review": "article.review",
    "titles": "article.edit",
}


def worker_permission_for(operation: str) -> ProjectPermission:
    """Map known queued work to its explicit M7 permission."""

    normalized = operation.strip()
    try:
        return WORKER_OPERATION_PERMISSIONS[normalized]
    except KeyError as exc:
        raise ValueError("unsupported worker operation") from exc


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


def authorized_batch_runner(
    queue: PostgresJobQueue,
    handler: JobHandler,
    *,
    access: ProjectAccessService,
    operations: Iterable[str],
) -> BatchJobRunner:
    """Build the only allowed Server runner with both authorization checks."""

    normalized = tuple(operation.strip() for operation in operations)
    if not normalized or len(set(normalized)) != len(normalized):
        raise ValueError("worker operations are invalid")
    for operation in normalized:
        worker_permission_for(operation)
    return BatchJobRunner(
        AuthorizedPostgresJobQueue(queue, access=access),
        ReauthorizingJobHandler(handler, access=access),
        concurrency=1,
        operations=normalized,
    )


__all__ = [
    "AuthorizedPostgresJobQueue",
    "ReauthorizingJobHandler",
    "WORKER_OPERATION_PERMISSIONS",
    "authorized_batch_runner",
    "worker_permission_for",
]
