from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from typing import Any, Mapping, cast

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Engine

from server_schema import assistant_attachments
from services.audit_log import AuditEvent, AuditEventWriter, PostgresAuditEventWriter
from workflow_assistant.attachments import (
    AssistantAttachment,
    AttachmentConflict,
    AttachmentNotFound,
    AttachmentReservation,
    AttachmentStatus,
)


_EXPIRABLE_STATUSES = (
    "uploading",
    "uploaded",
    "classifying",
    "needs_user_choice",
    "proposal_ready",
    "failed",
)


def _aware_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _json_object(value: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("classification_payload must be a JSON object")
    try:
        decoded = json.loads(json.dumps(dict(value), ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError("classification_payload must be JSON-safe") from exc
    if not isinstance(decoded, dict):
        raise ValueError("classification_payload must be a JSON object")
    return cast(dict[str, object], decoded)


def _from_row(row: Mapping[str, Any]) -> AssistantAttachment:
    return AssistantAttachment(
        attachment_id=str(row["attachment_id"]),
        organization_id=str(row["organization_id"]),
        creator_user_id=str(row["creator_user_id"]),
        conversation_id=str(row["conversation_id"]),
        proposed_project_id=str(row["proposed_project_id"]) if row["proposed_project_id"] is not None else None,
        plan_id=str(row["plan_id"]) if row["plan_id"] is not None else None,
        idempotency_key=str(row["idempotency_key"]),
        object_key=str(row["object_key"]),
        original_filename=str(row["original_filename"]),
        mime_type=str(row["mime_type"]),
        byte_size=int(row["byte_size"]),
        sha256=str(row["sha256"]),
        classification=str(row["classification"]) if row["classification"] is not None else None,
        classification_payload=copy.deepcopy(dict(row["classification_payload"] or {})),
        revision=int(row["revision"]),
        status=cast(AttachmentStatus, str(row["status"])),
        expires_at=_aware_utc(row["expires_at"], "expires_at"),
        created_at=_aware_utc(row["created_at"], "created_at"),
        updated_at=_aware_utc(row["updated_at"], "updated_at"),
    )


def _values(attachment: AssistantAttachment) -> dict[str, object]:
    return {
        "organization_id": attachment.organization_id,
        "attachment_id": attachment.attachment_id,
        "creator_user_id": attachment.creator_user_id,
        "conversation_id": attachment.conversation_id,
        "proposed_project_id": attachment.proposed_project_id,
        "plan_id": attachment.plan_id,
        "idempotency_key": attachment.idempotency_key,
        "object_key": attachment.object_key,
        "original_filename": attachment.original_filename,
        "mime_type": attachment.mime_type,
        "byte_size": attachment.byte_size,
        "sha256": attachment.sha256,
        "classification": attachment.classification,
        "classification_payload": _json_object(attachment.classification_payload),
        "revision": attachment.revision,
        "status": attachment.status,
        "expires_at": _aware_utc(attachment.expires_at, "expires_at"),
        "created_at": _aware_utc(attachment.created_at, "created_at"),
        "updated_at": _aware_utc(attachment.updated_at, "updated_at"),
    }


def _same_upload(left: AssistantAttachment, right: AssistantAttachment) -> bool:
    return (
        left.organization_id,
        left.creator_user_id,
        left.conversation_id,
        left.idempotency_key,
        left.proposed_project_id,
        left.plan_id,
        left.original_filename,
        left.mime_type,
        left.byte_size,
        left.sha256,
    ) == (
        right.organization_id,
        right.creator_user_id,
        right.conversation_id,
        right.idempotency_key,
        right.proposed_project_id,
        right.plan_id,
        right.original_filename,
        right.mime_type,
        right.byte_size,
        right.sha256,
    )


class PostgresAttachmentRepository:
    def __init__(self, engine: Engine, *, audit: AuditEventWriter | None = None) -> None:
        self._engine = engine
        self._audit = audit or PostgresAuditEventWriter()

    @staticmethod
    def _audit_event(attachment: AssistantAttachment, *, transition: str, actor_user_id: str | None) -> AuditEvent:
        details: dict[str, object] = {
            "conversation_id": attachment.conversation_id,
            "sha256": attachment.sha256,
            "status": attachment.status,
        }
        if transition == "uploaded":
            details.update({"byte_size": attachment.byte_size, "mime_type": attachment.mime_type})
        return AuditEvent(
            organization_id=attachment.organization_id,
            event_id=f"assistant-attachment:{attachment.attachment_id}:{transition}",
            actor_user_id=actor_user_id,
            project_id=attachment.proposed_project_id,
            action=f"assistant.attachment.{transition}",
            target_type="assistant_attachment",
            target_id=attachment.attachment_id,
            details=details,
        )

    def reserve_upload(self, attachment: AssistantAttachment) -> AttachmentReservation:
        if attachment.status != "uploading":
            raise ValueError("upload reservation must start in uploading state")
        with self._engine.begin() as connection:
            inserted = connection.execute(
                insert(assistant_attachments)
                .values(**_values(attachment))
                .on_conflict_do_nothing()
                .returning(*assistant_attachments.c)
            ).mappings().one_or_none()
            if inserted is not None:
                return AttachmentReservation(_from_row(inserted), True)
            row = connection.execute(
                sa.select(assistant_attachments)
                .where(
                    assistant_attachments.c.organization_id == attachment.organization_id,
                    assistant_attachments.c.creator_user_id == attachment.creator_user_id,
                    assistant_attachments.c.conversation_id == attachment.conversation_id,
                    assistant_attachments.c.idempotency_key == attachment.idempotency_key,
                )
                .with_for_update()
            ).mappings().one_or_none()
            if row is None:
                raise AttachmentConflict("attachment identity already exists")
            winner = _from_row(row)
            if not _same_upload(winner, attachment):
                raise AttachmentConflict("attachment idempotency key already has different content")
            if winner.status == "failed":
                row = connection.execute(
                    assistant_attachments.update()
                    .where(
                        assistant_attachments.c.organization_id == winner.organization_id,
                        assistant_attachments.c.attachment_id == winner.attachment_id,
                        assistant_attachments.c.revision == winner.revision,
                        assistant_attachments.c.status == "failed",
                    )
                    .values(status="uploading", revision=assistant_attachments.c.revision + 1, updated_at=attachment.updated_at)
                    .returning(*assistant_attachments.c)
                ).mappings().one()
                return AttachmentReservation(_from_row(row), True)
            return AttachmentReservation(winner, winner.status == "uploading")

    def finalize_upload(self, *, attachment: AssistantAttachment, now: datetime) -> AssistantAttachment:
        changed_at = _aware_utc(now, "now")
        with self._engine.begin() as connection:
            current_row = connection.execute(
                sa.select(assistant_attachments).where(
                    assistant_attachments.c.organization_id == attachment.organization_id,
                    assistant_attachments.c.attachment_id == attachment.attachment_id,
                    assistant_attachments.c.object_key == attachment.object_key,
                ).with_for_update()
            ).mappings().one_or_none()
            if current_row is None:
                raise AttachmentConflict("upload reservation is missing")
            current = _from_row(current_row)
            if not _same_upload(current, attachment):
                raise AttachmentConflict("upload reservation content changed")
            if current.status == "uploaded":
                return current
            if current.status not in {"uploading", "failed"}:
                raise AttachmentConflict("upload reservation changed before finalize")
            row = connection.execute(
                assistant_attachments.update()
                .where(
                    assistant_attachments.c.organization_id == current.organization_id,
                    assistant_attachments.c.attachment_id == current.attachment_id,
                    assistant_attachments.c.revision == current.revision,
                    assistant_attachments.c.status == current.status,
                )
                .values(status="uploaded", revision=assistant_attachments.c.revision + 1, updated_at=changed_at)
                .returning(*assistant_attachments.c)
            ).mappings().one_or_none()
            if row is None:
                raise AttachmentConflict("upload reservation changed before finalize")
            uploaded = _from_row(row)
            self._audit.append(connection, self._audit_event(uploaded, transition="uploaded", actor_user_id=uploaded.creator_user_id))
            return uploaded

    def mark_upload_failed(self, *, attachment: AssistantAttachment, now: datetime) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                assistant_attachments.update()
                .where(
                    assistant_attachments.c.organization_id == attachment.organization_id,
                    assistant_attachments.c.attachment_id == attachment.attachment_id,
                    assistant_attachments.c.object_key == attachment.object_key,
                    assistant_attachments.c.status == "uploading",
                )
                .values(status="failed", revision=assistant_attachments.c.revision + 1, updated_at=_aware_utc(now, "now"))
            )

    def get_for_actor(self, *, organization_id: str, creator_user_id: str, conversation_id: str, attachment_id: str) -> AssistantAttachment | None:
        with self._engine.connect() as connection:
            row = connection.execute(sa.select(assistant_attachments).where(
                assistant_attachments.c.organization_id == organization_id,
                assistant_attachments.c.creator_user_id == creator_user_id,
                assistant_attachments.c.conversation_id == conversation_id,
                assistant_attachments.c.attachment_id == attachment_id,
            )).mappings().one_or_none()
        return _from_row(row) if row is not None else None

    def get_by_idempotency_for_actor(self, *, organization_id: str, creator_user_id: str, conversation_id: str, idempotency_key: str) -> AssistantAttachment | None:
        with self._engine.connect() as connection:
            row = connection.execute(sa.select(assistant_attachments).where(
                assistant_attachments.c.organization_id == organization_id,
                assistant_attachments.c.creator_user_id == creator_user_id,
                assistant_attachments.c.conversation_id == conversation_id,
                assistant_attachments.c.idempotency_key == idempotency_key,
            )).mappings().one_or_none()
        return _from_row(row) if row is not None else None

    def list_for_actor(self, *, organization_id: str, creator_user_id: str, conversation_id: str, limit: int) -> tuple[AssistantAttachment, ...]:
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        with self._engine.connect() as connection:
            rows = connection.execute(sa.select(assistant_attachments).where(
                assistant_attachments.c.organization_id == organization_id,
                assistant_attachments.c.creator_user_id == creator_user_id,
                assistant_attachments.c.conversation_id == conversation_id,
            ).order_by(assistant_attachments.c.updated_at.desc(), assistant_attachments.c.attachment_id).limit(limit)).mappings().all()
        return tuple(_from_row(row) for row in rows)

    def claim_rejection(self, *, organization_id: str, creator_user_id: str, conversation_id: str, attachment_id: str, now: datetime) -> AssistantAttachment:
        changed_at = _aware_utc(now, "now")
        scope = (
            assistant_attachments.c.organization_id == organization_id,
            assistant_attachments.c.creator_user_id == creator_user_id,
            assistant_attachments.c.conversation_id == conversation_id,
            assistant_attachments.c.attachment_id == attachment_id,
        )
        with self._engine.begin() as connection:
            row = connection.execute(assistant_attachments.update().where(
                *scope,
                assistant_attachments.c.status.in_(_EXPIRABLE_STATUSES),
                assistant_attachments.c.expires_at > changed_at,
            ).values(status="rejecting", revision=assistant_attachments.c.revision + 1, updated_at=changed_at).returning(*assistant_attachments.c)).mappings().one_or_none()
            if row is not None:
                return _from_row(row)
            existing = connection.execute(sa.select(assistant_attachments).where(*scope)).mappings().one_or_none()
            if existing is None:
                raise AttachmentNotFound("attachment not found")
            claimed = _from_row(existing)
            if claimed.status == "rejecting":
                return claimed
            raise AttachmentConflict("attachment cannot be rejected in its current state")

    def _finalize_cleanup(self, *, attachment: AssistantAttachment, terminal_status: AttachmentStatus, actor_user_id: str | None, now: datetime) -> AssistantAttachment | None:
        claim_status = "rejecting" if terminal_status == "rejected" else "expiring"
        with self._engine.begin() as connection:
            row = connection.execute(assistant_attachments.update().where(
                assistant_attachments.c.organization_id == attachment.organization_id,
                assistant_attachments.c.attachment_id == attachment.attachment_id,
                assistant_attachments.c.object_key == attachment.object_key,
                assistant_attachments.c.revision == attachment.revision,
                assistant_attachments.c.status == claim_status,
            ).values(status=terminal_status, revision=assistant_attachments.c.revision + 1, updated_at=_aware_utc(now, "now")).returning(*assistant_attachments.c)).mappings().one_or_none()
            if row is None:
                return None
            terminal = _from_row(row)
            self._audit.append(connection, self._audit_event(terminal, transition=terminal_status, actor_user_id=actor_user_id))
            connection.execute(assistant_attachments.delete().where(
                assistant_attachments.c.organization_id == terminal.organization_id,
                assistant_attachments.c.attachment_id == terminal.attachment_id,
                assistant_attachments.c.revision == terminal.revision,
                assistant_attachments.c.status == terminal_status,
            ))
            return terminal

    def finalize_rejection(self, *, attachment: AssistantAttachment, now: datetime) -> AssistantAttachment:
        terminal = self._finalize_cleanup(attachment=attachment, terminal_status="rejected", actor_user_id=attachment.creator_user_id, now=now)
        if terminal is None:
            raise AttachmentConflict("attachment rejection claim changed before finalize")
        return terminal

    def claim_expired(self, *, before: datetime, limit: int, exclude_attachment_ids: tuple[str, ...]) -> tuple[AssistantAttachment, ...]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        cutoff = _aware_utc(before, "before")
        eligible = sa.or_(
            assistant_attachments.c.status == "rejecting",
            assistant_attachments.c.status == "expiring",
            sa.and_(assistant_attachments.c.expires_at <= cutoff, assistant_attachments.c.status.in_(_EXPIRABLE_STATUSES)),
        )
        filters: list[object] = [eligible]
        if exclude_attachment_ids:
            filters.append(assistant_attachments.c.attachment_id.not_in(exclude_attachment_ids))
        with self._engine.begin() as connection:
            candidates = connection.execute(sa.select(
                assistant_attachments.c.organization_id,
                assistant_attachments.c.attachment_id,
            ).where(*filters).order_by(
                assistant_attachments.c.expires_at,
                assistant_attachments.c.organization_id,
                assistant_attachments.c.attachment_id,
            ).limit(limit).with_for_update(skip_locked=True)).all()
            claimed: list[AssistantAttachment] = []
            for organization_id, attachment_id in candidates:
                next_status = sa.case(
                    (assistant_attachments.c.status == "rejecting", "rejecting"),
                    else_="expiring",
                )
                row = connection.execute(assistant_attachments.update().where(
                    assistant_attachments.c.organization_id == organization_id,
                    assistant_attachments.c.attachment_id == attachment_id,
                    eligible,
                ).values(
                    status=next_status,
                    revision=sa.case(
                        (assistant_attachments.c.status.in_(("rejecting", "expiring")), assistant_attachments.c.revision),
                        else_=assistant_attachments.c.revision + 1,
                    ),
                    updated_at=sa.case(
                        (assistant_attachments.c.status.in_(("rejecting", "expiring")), assistant_attachments.c.updated_at),
                        else_=cutoff,
                    ),
                ).returning(*assistant_attachments.c)).mappings().one_or_none()
                if row is not None:
                    claimed.append(_from_row(row))
            return tuple(claimed)

    def finalize_expiry(self, *, attachment: AssistantAttachment, now: datetime) -> AssistantAttachment | None:
        return self._finalize_cleanup(attachment=attachment, terminal_status="expired", actor_user_id=None, now=now)


__all__ = ["PostgresAttachmentRepository"]
