from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping
from sqlalchemy.exc import IntegrityError

from server_schema import (
    assistant_attachment_jobs,
    assistant_attachments,
    assistant_import_proposals,
)
from services.audit_log import AuditEvent, AuditEventWriter, PostgresAuditEventWriter
from workflow_assistant.attachment_jobs import (
    ACTIVE_ATTACHMENT_JOB_STATUSES,
    ATTACHMENT_JOB_OPERATIONS,
    AttachmentJob,
    AttachmentJobConflict,
    AttachmentJobNotFound,
    AttachmentJobOperation,
    AttachmentJobResult,
    AttachmentJobStatus,
    PendingAttachmentJobAuthorization,
    validate_attachment_job_target,
)


RETRY_DELAYS_SECONDS = (5, 15, 45)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _required_text(value: str, field_name: str) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized


class PostgresAttachmentJobRepository:
    """Durable queue independent of article_tasks and their Job foreign key."""

    def __init__(
        self,
        engine: Engine,
        *,
        organization_id: str,
        worker_id: str | None = None,
        lease_seconds: int = 15 * 60,
        audit: AuditEventWriter | None = None,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self._engine = engine
        self.organization_id = _required_text(organization_id, "organization_id")
        self.worker_id = _required_text(worker_id or uuid4().hex, "worker_id")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be greater than zero")
        self.lease_seconds = int(lease_seconds)
        self._audit = audit or PostgresAuditEventWriter()
        self._clock = clock

    def enqueue(
        self,
        *,
        requested_by_user_id: str,
        attachment_id: str,
        operation: AttachmentJobOperation,
        idempotency_key: str,
        expected_attachment_revision: int,
        project_id: str | None = None,
        proposal_id: str | None = None,
        expected_proposal_revision: int | None = None,
        request_payload: Mapping[str, Any] | None = None,
        max_attempts: int = 4,
    ) -> AttachmentJob:
        requester = _required_text(requested_by_user_id, "requested_by_user_id")
        attachment = _required_text(attachment_id, "attachment_id")
        idem = _required_text(idempotency_key, "idempotency_key")
        if operation not in ATTACHMENT_JOB_OPERATIONS:
            raise ValueError("unsupported attachment job operation")
        project = _required_text(project_id, "project_id") if project_id else None
        proposal = _required_text(proposal_id, "proposal_id") if proposal_id else None
        self._validate_shape(
            operation=operation,
            project_id=project,
            proposal_id=proposal,
            expected_attachment_revision=expected_attachment_revision,
            expected_proposal_revision=expected_proposal_revision,
            max_attempts=max_attempts,
        )
        request = dict(request_payload or {})
        current = self._clock()
        values = {
            "organization_id": self.organization_id,
            "job_id": uuid4().hex,
            "requested_by_user_id": requester,
            "project_id": project,
            "attachment_id": attachment,
            "proposal_id": proposal,
            "operation": operation,
            "idempotency_key": idem,
            "expected_attachment_revision": expected_attachment_revision,
            "expected_proposal_revision": expected_proposal_revision,
            "request_payload": request,
            "status": "queued",
            "attempts": 0,
            "max_attempts": max_attempts,
            "cancel_requested": False,
            "available_at": current,
            "created_at": current,
            "updated_at": current,
        }
        with self._engine.begin() as connection:
            existing = self._find_idempotent(
                connection,
                requester=requester,
                operation=operation,
                idempotency_key=idem,
            )
            if existing is not None:
                self._require_exact_replay(existing, values)
                return self._job(existing)
            self._require_source_revisions(
                connection,
                requester=requester,
                attachment_id=attachment,
                attachment_revision=expected_attachment_revision,
                proposal_id=proposal,
                proposal_revision=expected_proposal_revision,
                project_id=project,
            )
            try:
                with connection.begin_nested():
                    row = connection.execute(
                        assistant_attachment_jobs.insert()
                        .values(**values)
                        .returning(*assistant_attachment_jobs.c)
                    ).mappings().one()
            except IntegrityError as exc:
                replay = self._find_idempotent(
                    connection,
                    requester=requester,
                    operation=operation,
                    idempotency_key=idem,
                )
                if replay is not None:
                    self._require_exact_replay(replay, values)
                    return self._job(replay)
                raise AttachmentJobConflict("attachment or proposal has an active job") from exc
            self._append_audit(connection, row, action="enqueued")
            return self._job(row)

    def recover_interrupted(self) -> int:
        current = self._clock()
        with self._engine.begin() as connection:
            rows = connection.execute(
                sa.select(assistant_attachment_jobs)
                .where(
                    assistant_attachment_jobs.c.organization_id == self.organization_id,
                    assistant_attachment_jobs.c.status == "running",
                    assistant_attachment_jobs.c.lease_expires_at <= current,
                )
                .with_for_update(skip_locked=True)
            ).mappings().all()
            for row in rows:
                if row["cancel_requested"]:
                    status, code, finished_at = "cancelled", "job_cancelled", current
                elif int(row["attempts"]) >= int(row["max_attempts"]):
                    status, code, finished_at = "failed", "retry_exhausted", current
                else:
                    status, code, finished_at = "queued", None, None
                updated = connection.execute(
                    assistant_attachment_jobs.update()
                    .where(
                        assistant_attachment_jobs.c.organization_id == self.organization_id,
                        assistant_attachment_jobs.c.job_id == row["job_id"],
                        assistant_attachment_jobs.c.status == "running",
                    )
                    .values(
                        status=status,
                        standardized_error_code=code,
                        available_at=current,
                        finished_at=finished_at,
                        worker_id=None,
                        lease_expires_at=None,
                        updated_at=current,
                    )
                    .returning(*assistant_attachment_jobs.c)
                ).mappings().one_or_none()
                if updated is not None and status in {"cancelled", "failed"}:
                    self._append_audit(connection, updated, action=status)
            return len(rows)

    def list_claim_candidates(
        self, *, limit: int
    ) -> tuple[PendingAttachmentJobAuthorization, ...]:
        if limit <= 0:
            return ()
        current = self._clock()
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(
                    assistant_attachment_jobs.c.job_id,
                    assistant_attachment_jobs.c.organization_id,
                    assistant_attachment_jobs.c.requested_by_user_id,
                    assistant_attachment_jobs.c.project_id,
                    assistant_attachment_jobs.c.attachment_id,
                    assistant_attachment_jobs.c.proposal_id,
                    assistant_attachment_jobs.c.operation,
                )
                .where(
                    assistant_attachment_jobs.c.organization_id == self.organization_id,
                    assistant_attachment_jobs.c.status.in_(("queued", "retry_wait")),
                    assistant_attachment_jobs.c.available_at <= current,
                    assistant_attachment_jobs.c.cancel_requested.is_(False),
                )
                .order_by(
                    assistant_attachment_jobs.c.available_at,
                    assistant_attachment_jobs.c.created_at,
                    assistant_attachment_jobs.c.job_id,
                )
                .limit(limit)
            ).mappings().all()
        return tuple(self._candidate(row) for row in rows)

    def claim_authorized(
        self, job_ids: tuple[str, ...], *, limit: int
    ) -> tuple[AttachmentJob, ...]:
        ids = tuple(dict.fromkeys(_required_text(item, "job_id") for item in job_ids))
        if limit <= 0 or not ids:
            return ()
        current = self._clock()
        claimed: list[AttachmentJob] = []
        with self._engine.begin() as connection:
            rows = connection.execute(
                sa.select(assistant_attachment_jobs)
                .where(
                    assistant_attachment_jobs.c.organization_id == self.organization_id,
                    assistant_attachment_jobs.c.job_id.in_(ids),
                    assistant_attachment_jobs.c.status.in_(("queued", "retry_wait")),
                    assistant_attachment_jobs.c.available_at <= current,
                    assistant_attachment_jobs.c.cancel_requested.is_(False),
                )
                .order_by(
                    assistant_attachment_jobs.c.available_at,
                    assistant_attachment_jobs.c.created_at,
                    assistant_attachment_jobs.c.job_id,
                )
                .limit(limit)
                .with_for_update(skip_locked=True)
            ).mappings().all()
            for row in rows:
                try:
                    if not self._classification_replay_ready(connection, row):
                        self._require_source_revisions(
                            connection,
                            requester=str(row["requested_by_user_id"]),
                            attachment_id=str(row["attachment_id"]),
                            attachment_revision=int(row["expected_attachment_revision"]),
                            proposal_id=(str(row["proposal_id"]) if row["proposal_id"] else None),
                            proposal_revision=(
                                int(row["expected_proposal_revision"])
                                if row["expected_proposal_revision"] is not None
                                else None
                            ),
                            project_id=(str(row["project_id"]) if row["project_id"] else None),
                        )
                except AttachmentJobConflict as exc:
                    failed = connection.execute(
                        assistant_attachment_jobs.update()
                        .where(
                            assistant_attachment_jobs.c.organization_id == self.organization_id,
                            assistant_attachment_jobs.c.job_id == row["job_id"],
                            assistant_attachment_jobs.c.status.in_(("queued", "retry_wait")),
                        )
                        .values(
                            status="conflict",
                            standardized_error_code=exc.code,
                            finished_at=current,
                            updated_at=current,
                        )
                        .returning(*assistant_attachment_jobs.c)
                    ).mappings().one_or_none()
                    if failed is not None:
                        self._append_audit(connection, failed, action="conflict")
                    continue
                updated = connection.execute(
                    assistant_attachment_jobs.update()
                    .where(
                        assistant_attachment_jobs.c.organization_id == self.organization_id,
                        assistant_attachment_jobs.c.job_id == row["job_id"],
                        assistant_attachment_jobs.c.status.in_(("queued", "retry_wait")),
                    )
                    .values(
                        status="running",
                        attempts=assistant_attachment_jobs.c.attempts + 1,
                        started_at=current,
                        worker_id=self.worker_id,
                        lease_expires_at=current + timedelta(seconds=self.lease_seconds),
                        standardized_error_code=None,
                        updated_at=current,
                    )
                    .returning(*assistant_attachment_jobs.c)
                ).mappings().one_or_none()
                if updated is not None:
                    claimed.append(self._job(updated))
        return tuple(claimed)

    def reject_authorization(self, job_id: str) -> bool:
        return self._finish_pending(
            job_id, status="conflict", error_code="authorization_changed"
        )

    def is_cancel_requested(self, job_id: str) -> bool:
        with self._engine.connect() as connection:
            value = connection.execute(
                sa.select(assistant_attachment_jobs.c.cancel_requested).where(
                    assistant_attachment_jobs.c.organization_id == self.organization_id,
                    assistant_attachment_jobs.c.job_id == job_id,
                )
            ).scalar_one_or_none()
        return bool(value)

    def renew_lease(self, job_id: str) -> bool:
        current = self._clock()
        with self._engine.begin() as connection:
            row = connection.execute(
                assistant_attachment_jobs.update()
                .where(
                    assistant_attachment_jobs.c.organization_id == self.organization_id,
                    assistant_attachment_jobs.c.job_id == job_id,
                    assistant_attachment_jobs.c.status == "running",
                    assistant_attachment_jobs.c.worker_id == self.worker_id,
                )
                .values(
                    lease_expires_at=current + timedelta(seconds=self.lease_seconds),
                    updated_at=current,
                )
                .returning(assistant_attachment_jobs.c.job_id)
            ).scalar_one_or_none()
        return row is not None

    def mark_succeeded(self, job: AttachmentJob, result: AttachmentJobResult) -> None:
        current = self._clock()
        with self._engine.begin() as connection:
            self._require_running_owner(connection, job.job_id)
            proposal_id = job.proposal_id
            if job.operation == "preview_import_proposal":
                proposal_id = _required_text(
                    str(result.result_payload.get("proposal_id") or ""),
                    "result_payload.proposal_id",
                )
                if result.proposal_revision is None:
                    raise AttachmentJobConflict(
                        "preview result proposal revision is required"
                    )
                payload_revision = result.result_payload.get("proposal_revision")
                if (
                    isinstance(payload_revision, bool)
                    or not isinstance(payload_revision, int)
                    or payload_revision != result.proposal_revision
                ):
                    raise AttachmentJobConflict(
                        "preview result proposal revision does not match"
                    )
            self._require_source_revisions(
                connection,
                requester=job.requested_by_user_id,
                attachment_id=job.attachment_id,
                attachment_revision=result.attachment_revision,
                proposal_id=proposal_id,
                proposal_revision=result.proposal_revision,
                project_id=job.project_id,
            )
            row = connection.execute(
                assistant_attachment_jobs.update()
                .where(
                    assistant_attachment_jobs.c.organization_id == self.organization_id,
                    assistant_attachment_jobs.c.job_id == job.job_id,
                    assistant_attachment_jobs.c.status == "running",
                    assistant_attachment_jobs.c.worker_id == self.worker_id,
                )
                .values(
                    status="succeeded",
                    result_payload=dict(result.result_payload),
                    result_attachment_revision=result.attachment_revision,
                    result_proposal_revision=result.proposal_revision,
                    standardized_error_code=None,
                    finished_at=current,
                    worker_id=None,
                    lease_expires_at=None,
                    updated_at=current,
                )
                .returning(*assistant_attachment_jobs.c)
            ).mappings().one_or_none()
            if row is None:
                raise AttachmentJobConflict("attachment job claim changed")
            self._append_audit(connection, row, action="succeeded")

    def mark_failed(
        self, job: AttachmentJob, *, error_code: str, retryable: bool
    ) -> AttachmentJobStatus:
        code = _required_text(error_code, "error_code")
        # A preview handler creates the proposal only after building the diff.
        # If it fails after that durable write, the Job cannot safely discover
        # whether a retry would create a second proposal. Require explicit retry.
        retryable = retryable and job.operation != "preview_import_proposal"
        current = self._clock()
        with self._engine.begin() as connection:
            row = self._require_running_owner(connection, job.job_id)
            attempts = int(row["attempts"])
            max_attempts = int(row["max_attempts"])
            revision_conflict: AttachmentJobConflict | None = None
            if retryable:
                try:
                    self._require_source_revisions(
                        connection,
                        requester=job.requested_by_user_id,
                        attachment_id=job.attachment_id,
                        attachment_revision=job.expected_attachment_revision,
                        proposal_id=job.proposal_id,
                        proposal_revision=job.expected_proposal_revision,
                        project_id=job.project_id,
                    )
                except AttachmentJobConflict as exc:
                    revision_conflict = exc
            if revision_conflict is not None:
                status = "conflict"
                code = revision_conflict.code
                finished_at = current
                available_at = current
            elif retryable and attempts < max_attempts and not row["cancel_requested"]:
                delay_index = min(max(attempts - 1, 0), len(RETRY_DELAYS_SECONDS) - 1)
                status: AttachmentJobStatus = "retry_wait"
                finished_at = None
                available_at = current + timedelta(seconds=RETRY_DELAYS_SECONDS[delay_index])
            else:
                status = "cancelled" if row["cancel_requested"] else "failed"
                if retryable and attempts >= max_attempts:
                    code = "retry_exhausted"
                elif status == "cancelled":
                    code = "job_cancelled"
                finished_at = current
                available_at = current
            updated = connection.execute(
                assistant_attachment_jobs.update()
                .where(
                    assistant_attachment_jobs.c.organization_id == self.organization_id,
                    assistant_attachment_jobs.c.job_id == job.job_id,
                    assistant_attachment_jobs.c.status == "running",
                    assistant_attachment_jobs.c.worker_id == self.worker_id,
                )
                .values(
                    status=status,
                    standardized_error_code=code,
                    available_at=available_at,
                    finished_at=finished_at,
                    worker_id=None,
                    lease_expires_at=None,
                    updated_at=current,
                )
                .returning(*assistant_attachment_jobs.c)
            ).mappings().one()
            if status in {"failed", "cancelled", "conflict"}:
                self._append_audit(connection, updated, action=status)
            return status

    def mark_cancelled(self, job: AttachmentJob) -> None:
        self._finish_running(job, status="cancelled", error_code="job_cancelled")

    def mark_interrupted(self, job: AttachmentJob) -> None:
        current = self._clock()
        with self._engine.begin() as connection:
            row = self._require_running_owner(connection, job.job_id)
            if row["cancel_requested"]:
                status, code, finished_at = "cancelled", "job_cancelled", current
            elif int(row["attempts"]) >= int(row["max_attempts"]):
                status, code, finished_at = "failed", "retry_exhausted", current
            else:
                status, code, finished_at = "queued", None, None
            updated = connection.execute(
                assistant_attachment_jobs.update()
                .where(
                    assistant_attachment_jobs.c.organization_id == self.organization_id,
                    assistant_attachment_jobs.c.job_id == job.job_id,
                    assistant_attachment_jobs.c.status == "running",
                    assistant_attachment_jobs.c.worker_id == self.worker_id,
                )
                .values(
                    status=status,
                    standardized_error_code=code,
                    available_at=current,
                    finished_at=finished_at,
                    worker_id=None,
                    lease_expires_at=None,
                    updated_at=current,
                )
                .returning(*assistant_attachment_jobs.c)
            ).mappings().one()
            if status in {"failed", "cancelled"}:
                self._append_audit(connection, updated, action=status)

    def request_cancel(self, *, user_id: str, job_id: str) -> AttachmentJob:
        current = self._clock()
        with self._engine.begin() as connection:
            row = connection.execute(
                sa.select(assistant_attachment_jobs)
                .where(
                    assistant_attachment_jobs.c.organization_id == self.organization_id,
                    assistant_attachment_jobs.c.job_id == job_id,
                    assistant_attachment_jobs.c.requested_by_user_id == user_id,
                )
                .with_for_update()
            ).mappings().one_or_none()
            if row is None:
                raise AttachmentJobNotFound("attachment job was not found")
            if row["status"] in {"queued", "retry_wait"}:
                values = {
                    "status": "cancelled",
                    "cancel_requested": True,
                    "standardized_error_code": "job_cancelled",
                    "finished_at": current,
                    "updated_at": current,
                }
            elif row["status"] == "running":
                values = {"cancel_requested": True, "updated_at": current}
            else:
                return self._job(row)
            updated = connection.execute(
                assistant_attachment_jobs.update()
                .where(
                    assistant_attachment_jobs.c.organization_id == self.organization_id,
                    assistant_attachment_jobs.c.job_id == job_id,
                )
                .values(**values)
                .returning(*assistant_attachment_jobs.c)
            ).mappings().one()
            if updated["status"] == "cancelled":
                self._append_audit(connection, updated, action="cancelled")
            return self._job(updated)

    def retry(
        self,
        *,
        user_id: str,
        job_id: str,
        expected_attachment_revision: int,
        expected_proposal_revision: int | None,
        request_payload: Mapping[str, Any] | None = None,
    ) -> AttachmentJob:
        current = self._clock()
        with self._engine.begin() as connection:
            row = connection.execute(
                sa.select(assistant_attachment_jobs)
                .where(
                    assistant_attachment_jobs.c.organization_id == self.organization_id,
                    assistant_attachment_jobs.c.job_id == job_id,
                    assistant_attachment_jobs.c.requested_by_user_id == user_id,
                )
                .with_for_update()
            ).mappings().one_or_none()
            if row is None:
                raise AttachmentJobNotFound("attachment job was not found")
            if row["status"] not in {"failed", "cancelled", "conflict"}:
                raise AttachmentJobConflict("only terminal attachment jobs can be retried")
            self._require_source_revisions(
                connection,
                requester=str(row["requested_by_user_id"]),
                attachment_id=str(row["attachment_id"]),
                attachment_revision=expected_attachment_revision,
                proposal_id=str(row["proposal_id"]) if row["proposal_id"] else None,
                proposal_revision=expected_proposal_revision,
                project_id=str(row["project_id"]) if row["project_id"] else None,
            )
            updated = connection.execute(
                assistant_attachment_jobs.update()
                .where(
                    assistant_attachment_jobs.c.organization_id == self.organization_id,
                    assistant_attachment_jobs.c.job_id == job_id,
                    assistant_attachment_jobs.c.status.in_(("failed", "cancelled", "conflict")),
                )
                .values(
                    status="queued",
                    expected_attachment_revision=expected_attachment_revision,
                    expected_proposal_revision=expected_proposal_revision,
                    request_payload=(
                        dict(request_payload)
                        if request_payload is not None
                        else row["request_payload"]
                    ),
                    result_payload={},
                    result_attachment_revision=None,
                    result_proposal_revision=None,
                    attempts=0,
                    cancel_requested=False,
                    standardized_error_code=None,
                    available_at=current,
                    started_at=None,
                    finished_at=None,
                    worker_id=None,
                    lease_expires_at=None,
                    updated_at=current,
                )
                .returning(*assistant_attachment_jobs.c)
            ).mappings().one()
            self._append_audit(connection, updated, action="retried")
            return self._job(updated)

    def get_for_actor(self, *, user_id: str, job_id: str) -> AttachmentJob:
        with self._engine.connect() as connection:
            row = connection.execute(
                sa.select(assistant_attachment_jobs).where(
                    assistant_attachment_jobs.c.organization_id == self.organization_id,
                    assistant_attachment_jobs.c.requested_by_user_id == user_id,
                    assistant_attachment_jobs.c.job_id == job_id,
                )
            ).mappings().one_or_none()
        if row is None:
            raise AttachmentJobNotFound("attachment job was not found")
        return self._job(row)

    def _finish_pending(
        self, job_id: str, *, status: AttachmentJobStatus, error_code: str
    ) -> bool:
        current = self._clock()
        with self._engine.begin() as connection:
            row = connection.execute(
                assistant_attachment_jobs.update()
                .where(
                    assistant_attachment_jobs.c.organization_id == self.organization_id,
                    assistant_attachment_jobs.c.job_id == job_id,
                    assistant_attachment_jobs.c.status.in_(("queued", "retry_wait")),
                )
                .values(
                    status=status,
                    standardized_error_code=error_code,
                    finished_at=current,
                    updated_at=current,
                )
                .returning(*assistant_attachment_jobs.c)
            ).mappings().one_or_none()
            if row is not None:
                self._append_audit(connection, row, action=status)
        return row is not None

    def _finish_running(
        self, job: AttachmentJob, *, status: AttachmentJobStatus, error_code: str
    ) -> None:
        current = self._clock()
        with self._engine.begin() as connection:
            self._require_running_owner(connection, job.job_id)
            row = connection.execute(
                assistant_attachment_jobs.update()
                .where(
                    assistant_attachment_jobs.c.organization_id == self.organization_id,
                    assistant_attachment_jobs.c.job_id == job.job_id,
                    assistant_attachment_jobs.c.status == "running",
                    assistant_attachment_jobs.c.worker_id == self.worker_id,
                )
                .values(
                    status=status,
                    cancel_requested=(status == "cancelled"),
                    standardized_error_code=error_code,
                    finished_at=current,
                    worker_id=None,
                    lease_expires_at=None,
                    updated_at=current,
                )
                .returning(*assistant_attachment_jobs.c)
            ).mappings().one()
            self._append_audit(connection, row, action=status)

    def _require_running_owner(self, connection: Any, job_id: str) -> RowMapping:
        row = connection.execute(
            sa.select(assistant_attachment_jobs)
            .where(
                assistant_attachment_jobs.c.organization_id == self.organization_id,
                assistant_attachment_jobs.c.job_id == job_id,
            )
            .with_for_update()
        ).mappings().one_or_none()
        if row is None:
            raise AttachmentJobNotFound("attachment job was not found")
        if row["status"] != "running" or row["worker_id"] != self.worker_id:
            raise AttachmentJobConflict("attachment job claim changed")
        return row

    def _require_source_revisions(
        self,
        connection: Any,
        *,
        requester: str,
        attachment_id: str,
        attachment_revision: int,
        proposal_id: str | None,
        proposal_revision: int | None,
        project_id: str | None,
    ) -> None:
        attachment = connection.execute(
            sa.select(
                assistant_attachments.c.revision,
                assistant_attachments.c.status,
            )
            .where(
                assistant_attachments.c.organization_id == self.organization_id,
                assistant_attachments.c.attachment_id == attachment_id,
                assistant_attachments.c.creator_user_id == requester,
            )
            .with_for_update()
        ).mappings().one_or_none()
        if attachment is None:
            raise AttachmentJobConflict("attachment scope changed")
        if int(attachment["revision"]) != attachment_revision:
            conflict = AttachmentJobConflict("attachment revision changed")
            conflict.code = "attachment_revision_conflict"
            raise conflict
        if proposal_id is None:
            return
        proposal = connection.execute(
            sa.select(
                assistant_import_proposals.c.attachment_id,
                assistant_import_proposals.c.creator_user_id,
                assistant_import_proposals.c.target_project_id,
                assistant_import_proposals.c.revision,
            )
            .where(
                assistant_import_proposals.c.organization_id == self.organization_id,
                assistant_import_proposals.c.proposal_id == proposal_id,
            )
            .with_for_update()
        ).mappings().one_or_none()
        if (
            proposal is None
            or proposal["attachment_id"] != attachment_id
            or proposal["creator_user_id"] != requester
            or proposal["target_project_id"] != project_id
        ):
            raise AttachmentJobConflict("proposal scope changed")
        if int(proposal["revision"]) != proposal_revision:
            conflict = AttachmentJobConflict("proposal revision changed")
            conflict.code = "proposal_revision_conflict"
            raise conflict

    def _classification_replay_ready(
        self,
        connection: Any,
        job: RowMapping,
    ) -> bool:
        if str(job["operation"]) != "classify_attachment":
            return False
        row = connection.execute(
            sa.select(
                assistant_attachments.c.status,
                assistant_attachments.c.classification_payload,
            ).where(
                assistant_attachments.c.organization_id == self.organization_id,
                assistant_attachments.c.attachment_id == job["attachment_id"],
                assistant_attachments.c.creator_user_id
                == job["requested_by_user_id"],
            )
        ).mappings().one_or_none()
        if row is None or str(row["status"]) not in {
            "proposal_ready",
            "needs_user_choice",
        }:
            return False
        payload = dict(row["classification_payload"] or {})
        return payload.get("classification_job_idempotency_key") == str(
            job["idempotency_key"]
        )

    @staticmethod
    def _validate_shape(
        *,
        operation: AttachmentJobOperation,
        project_id: str | None,
        proposal_id: str | None,
        expected_attachment_revision: int,
        expected_proposal_revision: int | None,
        max_attempts: int,
    ) -> None:
        if expected_attachment_revision < 0 or max_attempts <= 0:
            raise ValueError("attachment job revision or attempts are invalid")
        validate_attachment_job_target(
            operation=operation,
            project_id=project_id,
            proposal_id=proposal_id,
            expected_proposal_revision=expected_proposal_revision,
        )

    @staticmethod
    def _require_exact_replay(row: RowMapping, values: Mapping[str, Any]) -> None:
        fields = (
            "requested_by_user_id",
            "project_id",
            "attachment_id",
            "proposal_id",
            "operation",
            "idempotency_key",
            "expected_attachment_revision",
            "expected_proposal_revision",
            "max_attempts",
        )
        exact = all(row[field] == values[field] for field in fields) and dict(
            row["request_payload"] or {}
        ) == dict(values["request_payload"] or {})
        if not exact:
            conflict = AttachmentJobConflict("idempotency key payload changed")
            conflict.code = "idempotency_conflict"
            raise conflict

    def _find_idempotent(
        self,
        connection: Any,
        *,
        requester: str,
        operation: AttachmentJobOperation,
        idempotency_key: str,
    ) -> RowMapping | None:
        return connection.execute(
            sa.select(assistant_attachment_jobs).where(
                assistant_attachment_jobs.c.organization_id == self.organization_id,
                assistant_attachment_jobs.c.requested_by_user_id == requester,
                assistant_attachment_jobs.c.operation == operation,
                assistant_attachment_jobs.c.idempotency_key == idempotency_key,
            )
        ).mappings().one_or_none()

    def _append_audit(self, connection: Any, row: RowMapping, *, action: str) -> None:
        event_id = uuid5(
            NAMESPACE_URL,
            f"workflow-assistant-attachment-job:{self.organization_id}:"
            f"{row['job_id']}:{action}:{row['attempts']}",
        ).hex
        self._audit.append(
            connection,
            AuditEvent(
                organization_id=self.organization_id,
                event_id=event_id,
                actor_user_id=str(row["requested_by_user_id"]),
                project_id=str(row["project_id"]) if row["project_id"] else None,
                action=f"workflow_assistant.attachment_job.{action}",
                target_type="assistant_attachment_job",
                target_id=str(row["job_id"]),
                details={
                    "operation": str(row["operation"]),
                    "attachment_id": str(row["attachment_id"]),
                    "proposal_id": str(row["proposal_id"]) if row["proposal_id"] else None,
                    "status": str(row["status"]),
                    "standardized_error_code": row["standardized_error_code"],
                },
            ),
        )

    @staticmethod
    def _candidate(row: RowMapping) -> PendingAttachmentJobAuthorization:
        return PendingAttachmentJobAuthorization(
            job_id=str(row["job_id"]),
            organization_id=str(row["organization_id"]),
            requested_by_user_id=str(row["requested_by_user_id"]),
            project_id=str(row["project_id"]) if row["project_id"] else None,
            attachment_id=str(row["attachment_id"]),
            proposal_id=str(row["proposal_id"]) if row["proposal_id"] else None,
            operation=str(row["operation"]),  # type: ignore[arg-type]
        )

    @staticmethod
    def _job(row: RowMapping) -> AttachmentJob:
        return AttachmentJob(
            job_id=str(row["job_id"]),
            organization_id=str(row["organization_id"]),
            requested_by_user_id=str(row["requested_by_user_id"]),
            project_id=str(row["project_id"]) if row["project_id"] else None,
            attachment_id=str(row["attachment_id"]),
            proposal_id=str(row["proposal_id"]) if row["proposal_id"] else None,
            operation=str(row["operation"]),  # type: ignore[arg-type]
            idempotency_key=str(row["idempotency_key"]),
            expected_attachment_revision=int(row["expected_attachment_revision"]),
            expected_proposal_revision=(
                int(row["expected_proposal_revision"])
                if row["expected_proposal_revision"] is not None
                else None
            ),
            request_payload=dict(row["request_payload"] or {}),
            result_payload=dict(row["result_payload"] or {}),
            result_attachment_revision=(
                int(row["result_attachment_revision"])
                if row["result_attachment_revision"] is not None
                else None
            ),
            result_proposal_revision=(
                int(row["result_proposal_revision"])
                if row["result_proposal_revision"] is not None
                else None
            ),
            status=str(row["status"]),  # type: ignore[arg-type]
            attempts=int(row["attempts"]),
            max_attempts=int(row["max_attempts"]),
            cancel_requested=bool(row["cancel_requested"]),
            standardized_error_code=(
                str(row["standardized_error_code"])
                if row["standardized_error_code"]
                else None
            ),
        )


class PostgresAttachmentJobOrganizationDiscovery:
    """Read only organization identities needed to construct scoped workers."""

    def __init__(
        self,
        engine: Engine,
        *,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self._engine = engine
        self._clock = clock

    def list_pending_organization_ids(self, *, limit: int) -> tuple[str, ...]:
        if limit <= 0:
            return ()
        current = self._clock()
        pending = sa.or_(
            sa.and_(
                assistant_attachment_jobs.c.status.in_(("queued", "retry_wait")),
                assistant_attachment_jobs.c.available_at <= current,
                assistant_attachment_jobs.c.cancel_requested.is_(False),
            ),
            sa.and_(
                assistant_attachment_jobs.c.status == "running",
                assistant_attachment_jobs.c.lease_expires_at <= current,
            ),
        )
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(assistant_attachment_jobs.c.organization_id)
                .where(pending)
                .group_by(assistant_attachment_jobs.c.organization_id)
                .order_by(sa.func.min(assistant_attachment_jobs.c.available_at))
                .limit(limit)
            ).scalars().all()
        return tuple(str(row) for row in rows)


__all__ = [
    "PostgresAttachmentJobOrganizationDiscovery",
    "PostgresAttachmentJobRepository",
    "RETRY_DELAYS_SECONDS",
]
