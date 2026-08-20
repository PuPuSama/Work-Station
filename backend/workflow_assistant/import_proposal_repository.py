from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, cast

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection, Engine

from server_schema import assistant_attachments, assistant_import_proposals
from services.access_control import ActorIdentity
from services.audit_log import AuditEvent, AuditEventWriter, PostgresAuditEventWriter
from workflow_assistant.attachments import AssistantAttachment, AttachmentStatus
from workflow_assistant.import_proposals import (
    ImportProposal,
    ImportProposalConflict,
    ImportProposalNotFound,
    ImportProposalStatus,
    ImportTargetKind,
    normalized_json_object,
    proposal_review_status,
    validate_import_target,
)


def _aware_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _json_refs(value: object) -> tuple[dict[str, object], ...]:
    if not isinstance(value, list):
        raise ValueError("resulting_entity_refs must be a JSON array")
    refs: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("resulting_entity_refs entries must be objects")
        refs.append(normalized_json_object(item))
    return tuple(refs)


def _from_row(row: Mapping[str, Any]) -> ImportProposal:
    return ImportProposal(
        proposal_id=str(row["proposal_id"]),
        organization_id=str(row["organization_id"]),
        attachment_id=str(row["attachment_id"]),
        creator_user_id=str(row["creator_user_id"]),
        target_project_id=(
            str(row["target_project_id"])
            if row["target_project_id"] is not None
            else None
        ),
        plan_id=str(row["plan_id"]) if row["plan_id"] is not None else None,
        target_kind=cast(ImportTargetKind, str(row["target_kind"])),
        idempotency_key=str(row["idempotency_key"]),
        normalized_diff=copy.deepcopy(dict(row["normalized_diff"] or {})),
        revision=int(row["revision"]),
        status=cast(ImportProposalStatus, str(row["status"])),
        confirmed_by=(
            str(row["confirmed_by"]) if row["confirmed_by"] is not None else None
        ),
        confirmed_at=(
            _aware_utc(row["confirmed_at"], "confirmed_at")
            if row["confirmed_at"] is not None
            else None
        ),
        resulting_entity_refs=_json_refs(row["resulting_entity_refs"] or []),
        standardized_error_code=(
            str(row["standardized_error_code"])
            if row["standardized_error_code"] is not None
            else None
        ),
        created_at=_aware_utc(row["created_at"], "created_at"),
        updated_at=_aware_utc(row["updated_at"], "updated_at"),
    )


def _values(proposal: ImportProposal) -> dict[str, object]:
    refs = [normalized_json_object(item) for item in proposal.resulting_entity_refs]
    # Exercise the stdlib encoder as well as SQLAlchemy's JSON adaptation so
    # NaN, bytes, datetime and arbitrary objects fail before a transaction.
    try:
        json.dumps(refs, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("resulting_entity_refs must be JSON-safe") from exc
    return {
        "organization_id": proposal.organization_id,
        "proposal_id": proposal.proposal_id,
        "attachment_id": proposal.attachment_id,
        "creator_user_id": proposal.creator_user_id,
        "target_project_id": proposal.target_project_id,
        "plan_id": proposal.plan_id,
        "target_kind": proposal.target_kind,
        "idempotency_key": proposal.idempotency_key,
        "normalized_diff": normalized_json_object(proposal.normalized_diff),
        "revision": proposal.revision,
        "status": proposal.status,
        "confirmed_by": proposal.confirmed_by,
        "confirmed_at": (
            _aware_utc(proposal.confirmed_at, "confirmed_at")
            if proposal.confirmed_at is not None
            else None
        ),
        "resulting_entity_refs": refs,
        "standardized_error_code": proposal.standardized_error_code,
        "created_at": _aware_utc(proposal.created_at, "created_at"),
        "updated_at": _aware_utc(proposal.updated_at, "updated_at"),
    }


def _attachment_from_row(row: Mapping[str, Any]) -> AssistantAttachment:
    return AssistantAttachment(
        attachment_id=str(row["attachment_id"]),
        organization_id=str(row["organization_id"]),
        creator_user_id=str(row["creator_user_id"]),
        conversation_id=str(row["conversation_id"]),
        proposed_project_id=(
            str(row["proposed_project_id"])
            if row["proposed_project_id"] is not None
            else None
        ),
        plan_id=str(row["plan_id"]) if row["plan_id"] is not None else None,
        idempotency_key=str(row["idempotency_key"]),
        object_key=str(row["object_key"]),
        original_filename=str(row["original_filename"]),
        mime_type=str(row["mime_type"]),
        byte_size=int(row["byte_size"]),
        sha256=str(row["sha256"]),
        classification=(
            str(row["classification"]) if row["classification"] is not None else None
        ),
        classification_payload=copy.deepcopy(dict(row["classification_payload"] or {})),
        revision=int(row["revision"]),
        status=cast(AttachmentStatus, str(row["status"])),
        expires_at=_aware_utc(row["expires_at"], "expires_at"),
        created_at=_aware_utc(row["created_at"], "created_at"),
        updated_at=_aware_utc(row["updated_at"], "updated_at"),
    )


def _same_create(left: ImportProposal, right: ImportProposal) -> bool:
    return (
        left.organization_id,
        left.attachment_id,
        left.creator_user_id,
        left.target_project_id,
        left.plan_id,
        left.target_kind,
        left.idempotency_key,
        left.normalized_diff,
    ) == (
        right.organization_id,
        right.attachment_id,
        right.creator_user_id,
        right.target_project_id,
        right.plan_id,
        right.target_kind,
        right.idempotency_key,
        right.normalized_diff,
    )


class PostgresImportProposalRepository:
    """Actor-scoped proposal persistence with revision CAS and atomic audit."""

    def __init__(
        self, engine: Engine, *, audit: AuditEventWriter | None = None
    ) -> None:
        self._engine = engine
        self._audit = audit or PostgresAuditEventWriter()

    @staticmethod
    def _scope(actor: ActorIdentity, proposal_id: str) -> tuple[object, ...]:
        return (
            assistant_import_proposals.c.organization_id == actor.organization_id,
            assistant_import_proposals.c.creator_user_id == actor.user_id,
            assistant_import_proposals.c.proposal_id == proposal_id,
        )

    def _lock_for_actor(
        self, connection: Connection, actor: ActorIdentity, proposal_id: str
    ) -> Mapping[str, Any] | None:
        return connection.execute(
            sa.select(assistant_import_proposals)
            .where(*self._scope(actor, proposal_id))
            .with_for_update()
        ).mappings().one_or_none()

    @staticmethod
    def _audit_event(proposal: ImportProposal, *, transition: str) -> AuditEvent:
        return AuditEvent(
            organization_id=proposal.organization_id,
            event_id=(
                f"assistant-import-proposal:{proposal.proposal_id}:"
                f"{transition}:{proposal.revision}"
            ),
            actor_user_id=proposal.creator_user_id,
            project_id=proposal.target_project_id,
            action=f"assistant.import_proposal.{transition}",
            target_type="assistant_import_proposal",
            target_id=proposal.proposal_id,
            details={
                "attachment_id": proposal.attachment_id,
                "target_kind": proposal.target_kind,
                "revision": proposal.revision,
                "status": proposal.status,
            },
        )

    @staticmethod
    def _lock_attachment(
        connection: Connection,
        *,
        organization_id: str,
        creator_user_id: str,
        attachment_id: str,
    ) -> Mapping[str, Any] | None:
        return connection.execute(
            sa.select(assistant_attachments)
            .where(
                assistant_attachments.c.organization_id == organization_id,
                assistant_attachments.c.creator_user_id == creator_user_id,
                assistant_attachments.c.attachment_id == attachment_id,
            )
            .with_for_update(read=True)
        ).mappings().one_or_none()

    @staticmethod
    def _assert_attachment_available(
        row: Mapping[str, Any] | None, *, at: datetime
    ) -> None:
        if row is None:
            raise ImportProposalNotFound("attachment not found")
        if str(row["status"]) not in {"proposal_ready", "needs_user_choice"}:
            raise ImportProposalConflict(
                "attachment is not ready for an import proposal",
                code="attachment_classification_not_ready",
            )
        if _aware_utc(row["expires_at"], "expires_at") <= at:
            raise ImportProposalConflict(
                "attachment has expired", code="attachment_not_available"
            )

    def create(self, proposal: ImportProposal) -> ImportProposal:
        expected_status = (
            "draft"
            if proposal.target_kind == "needs_user_choice"
            or proposal.target_project_id is None
            else "awaiting_confirmation"
        )
        if (
            proposal.revision != 0
            or proposal.status != expected_status
            or proposal.confirmed_by is not None
            or proposal.confirmed_at is not None
            or proposal.resulting_entity_refs
            or proposal.standardized_error_code is not None
        ):
            raise ValueError("a new import proposal must start unconfirmed at revision 0")
        values = _values(proposal)
        with self._engine.begin() as connection:
            attachment = self._lock_attachment(
                connection,
                organization_id=proposal.organization_id,
                creator_user_id=proposal.creator_user_id,
                attachment_id=proposal.attachment_id,
            )
            self._assert_attachment_available(attachment, at=proposal.created_at)
            project_id, target_kind, normalized_diff = validate_import_target(
                attachment=_attachment_from_row(attachment),  # type: ignore[arg-type]
                target_project_id=proposal.target_project_id,
                target_kind=proposal.target_kind,
                normalized_diff=proposal.normalized_diff,
                now=proposal.created_at,
            )
            if (
                project_id != proposal.target_project_id
                or target_kind != proposal.target_kind
                or normalized_diff != proposal.normalized_diff
            ):
                raise ImportProposalConflict(
                    "proposal target is not normalized",
                    code="proposal_target_not_normalized",
                )
            inserted = connection.execute(
                insert(assistant_import_proposals)
                .values(**values)
                .on_conflict_do_nothing()
                .returning(*assistant_import_proposals.c)
            ).mappings().one_or_none()
            if inserted is not None:
                created = _from_row(inserted)
                self._audit.append(
                    connection, self._audit_event(created, transition="created")
                )
                return created
            row = connection.execute(
                sa.select(assistant_import_proposals)
                .where(
                    assistant_import_proposals.c.organization_id
                    == proposal.organization_id,
                    assistant_import_proposals.c.creator_user_id
                    == proposal.creator_user_id,
                    assistant_import_proposals.c.attachment_id
                    == proposal.attachment_id,
                    assistant_import_proposals.c.idempotency_key
                    == proposal.idempotency_key,
                )
                .with_for_update()
            ).mappings().one_or_none()
            if row is None:
                raise ImportProposalConflict(
                    "import proposal identity already exists",
                    code="proposal_identity_conflict",
                )
            winner = _from_row(row)
            if not _same_create(winner, proposal):
                raise ImportProposalConflict(
                    "proposal idempotency key already has different content",
                    code="idempotency_conflict",
                )
            return winner

    def get_for_actor(
        self, *, actor: ActorIdentity, proposal_id: str
    ) -> ImportProposal | None:
        with self._engine.connect() as connection:
            row = connection.execute(
                sa.select(assistant_import_proposals).where(
                    *self._scope(actor, proposal_id)
                )
            ).mappings().one_or_none()
        return _from_row(row) if row is not None else None

    def revise(
        self,
        *,
        actor: ActorIdentity,
        proposal_id: str,
        expected_revision: int,
        target_project_id: str | None,
        target_kind: ImportTargetKind,
        normalized_diff: Mapping[str, object],
        now: datetime,
    ) -> ImportProposal:
        changed_at = _aware_utc(now, "now")
        safe_diff = normalized_json_object(normalized_diff)
        next_status = proposal_review_status(
            target_kind=target_kind,
            target_project_id=target_project_id,
            normalized_diff=safe_diff,
        )
        with self._engine.begin() as connection:
            row = self._lock_for_actor(connection, actor, proposal_id)
            if row is None:
                raise ImportProposalNotFound("import proposal not found")
            current = _from_row(row)
            if current.revision != expected_revision:
                raise ImportProposalConflict(
                    "import proposal revision conflict",
                    code="import_proposal_revision_conflict",
                    current_revision=current.revision,
                )
            if current.status not in {"draft", "awaiting_confirmation"}:
                raise ImportProposalConflict(
                    "import proposal cannot be revised in its current state",
                    code="import_proposal_status_conflict",
                    current_revision=current.revision,
                )
            attachment = self._lock_attachment(
                connection,
                organization_id=actor.organization_id,
                creator_user_id=actor.user_id,
                attachment_id=current.attachment_id,
            )
            self._assert_attachment_available(attachment, at=changed_at)
            project_id, normalized_kind, normalized_diff = validate_import_target(
                attachment=_attachment_from_row(attachment),  # type: ignore[arg-type]
                target_project_id=target_project_id,
                target_kind=target_kind,
                normalized_diff=safe_diff,
                now=changed_at,
            )
            if project_id != target_project_id or normalized_kind != target_kind:
                raise ImportProposalConflict(
                    "proposal target is not normalized",
                    code="proposal_target_not_normalized",
                )
            safe_diff = normalized_diff
            updated = connection.execute(
                assistant_import_proposals.update()
                .where(
                    *self._scope(actor, proposal_id),
                    assistant_import_proposals.c.revision == expected_revision,
                )
                .values(
                    target_project_id=target_project_id,
                    target_kind=target_kind,
                    normalized_diff=safe_diff,
                    revision=expected_revision + 1,
                    status=next_status,
                    confirmed_by=None,
                    confirmed_at=None,
                    standardized_error_code=None,
                    updated_at=changed_at,
                )
                .returning(*assistant_import_proposals.c)
            ).mappings().one_or_none()
            if updated is None:
                raise ImportProposalConflict(
                    "import proposal revision conflict",
                    code="import_proposal_revision_conflict",
                )
            revised = _from_row(updated)
            self._audit.append(
                connection, self._audit_event(revised, transition="revised")
            )
            return revised

    def confirm(
        self,
        *,
        actor: ActorIdentity,
        proposal_id: str,
        expected_revision: int,
        target_project_id: str,
        authorize_target: Callable[[], None],
        now: datetime,
    ) -> ImportProposal:
        confirmed_at = _aware_utc(now, "now")
        with self._engine.begin() as connection:
            row = self._lock_for_actor(connection, actor, proposal_id)
            if row is None:
                raise ImportProposalNotFound("import proposal not found")
            current = _from_row(row)
            if current.target_kind == "needs_user_choice":
                raise ImportProposalConflict(
                    "choose a concrete target kind before confirmation",
                    code="target_kind_required",
                    current_revision=current.revision,
                )
            if current.target_project_id not in {None, target_project_id}:
                raise ImportProposalConflict(
                    "confirmation target does not match the proposal",
                    code="target_project_conflict",
                    current_revision=current.revision,
                )
            attachment = self._lock_attachment(
                connection,
                organization_id=actor.organization_id,
                creator_user_id=actor.user_id,
                attachment_id=current.attachment_id,
            )
            self._assert_attachment_available(attachment, at=confirmed_at)
            project_id, target_kind, _ = validate_import_target(
                attachment=_attachment_from_row(attachment),  # type: ignore[arg-type]
                target_project_id=target_project_id,
                target_kind=current.target_kind,
                normalized_diff=current.normalized_diff,
                now=confirmed_at,
            )
            if project_id != target_project_id or target_kind != current.target_kind:
                raise ImportProposalConflict(
                    "proposal no longer matches the attachment classification",
                    code="attachment_classification_conflict",
                )
            # This callback must perform a fresh, server-derived project check.
            # It intentionally runs after the proposal and attachment are locked.
            authorize_target()
            if current.revision != expected_revision:
                if (
                    current.status == "confirmed"
                    and current.revision == expected_revision + 1
                    and current.confirmed_by == actor.user_id
                    and current.target_project_id == target_project_id
                ):
                    return current
                raise ImportProposalConflict(
                    "import proposal revision conflict",
                    code="import_proposal_revision_conflict",
                    current_revision=current.revision,
                )
            if current.status != "awaiting_confirmation":
                raise ImportProposalConflict(
                    "import proposal is not awaiting confirmation",
                    code="import_proposal_status_conflict",
                    current_revision=current.revision,
                )
            updated_row = connection.execute(
                assistant_import_proposals.update()
                .where(
                    *self._scope(actor, proposal_id),
                    assistant_import_proposals.c.revision == expected_revision,
                    assistant_import_proposals.c.status == "awaiting_confirmation",
                )
                .values(
                    target_project_id=target_project_id,
                    status="confirmed",
                    revision=expected_revision + 1,
                    confirmed_by=actor.user_id,
                    confirmed_at=confirmed_at,
                    updated_at=confirmed_at,
                )
                .returning(*assistant_import_proposals.c)
            ).mappings().one_or_none()
            if updated_row is None:
                raise ImportProposalConflict(
                    "import proposal revision conflict",
                    code="import_proposal_revision_conflict",
                )
            confirmed = _from_row(updated_row)
            event = self._audit_event(confirmed, transition="confirmed")
            self._audit.append(
                connection,
                AuditEvent(
                    organization_id=event.organization_id,
                    event_id=event.event_id,
                    actor_user_id=event.actor_user_id,
                    project_id=event.project_id,
                    action=event.action,
                    target_type=event.target_type,
                    target_id=event.target_id,
                    details={
                        **dict(event.details),
                        "imports_executed": False,
                        "knowledge_published": False,
                    },
                ),
            )
            return confirmed

    def cancel(
        self,
        *,
        actor: ActorIdentity,
        proposal_id: str,
        expected_revision: int,
        now: datetime,
    ) -> ImportProposal:
        changed_at = _aware_utc(now, "now")
        with self._engine.begin() as connection:
            row = self._lock_for_actor(connection, actor, proposal_id)
            if row is None:
                raise ImportProposalNotFound("import proposal not found")
            current = _from_row(row)
            if current.revision != expected_revision:
                if (
                    current.status == "cancelled"
                    and current.revision == expected_revision + 1
                ):
                    return current
                raise ImportProposalConflict(
                    "import proposal revision conflict",
                    code="import_proposal_revision_conflict",
                    current_revision=current.revision,
                )
            if current.status not in {
                "draft",
                "awaiting_confirmation",
                "confirmed",
                "failed",
            }:
                raise ImportProposalConflict(
                    "import proposal cannot be cancelled in its current state",
                    code="import_proposal_status_conflict",
                    current_revision=current.revision,
                )
            updated = connection.execute(
                assistant_import_proposals.update()
                .where(
                    *self._scope(actor, proposal_id),
                    assistant_import_proposals.c.revision == expected_revision,
                )
                .values(
                    status="cancelled",
                    revision=expected_revision + 1,
                    updated_at=changed_at,
                )
                .returning(*assistant_import_proposals.c)
            ).mappings().one_or_none()
            if updated is None:
                raise ImportProposalConflict(
                    "import proposal revision conflict",
                    code="import_proposal_revision_conflict",
                )
            cancelled = _from_row(updated)
            self._audit.append(
                connection, self._audit_event(cancelled, transition="cancelled")
            )
            return cancelled


__all__ = ["PostgresImportProposalRepository"]
