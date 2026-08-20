from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Literal, Mapping, Protocol, cast, get_args

from models import PromptKind
from services.access_control import ActorIdentity
from workflow_assistant.attachments import AssistantAttachment
from workflow_assistant.classification import AttachmentClassification


ImportTargetKind = Literal[
    "knowledge_source",
    "prompt_asset",
    "task_workbook",
    "project_notes",
    "topic_library",
    "needs_user_choice",
]
ImportProposalStatus = Literal[
    "draft",
    "awaiting_confirmation",
    "confirmed",
    "running",
    "waiting_publication",
    "completed",
    "failed",
    "cancelled",
]

MAX_NORMALIZED_DIFF_BYTES = 1024 * 1024


class ImportProposalError(RuntimeError):
    """Stable base error for the proposal-only import boundary."""


class ImportProposalNotFound(ImportProposalError):
    """The proposal or attachment is absent from the actor's private scope."""


class ImportProposalConflict(ImportProposalError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "import_proposal_conflict",
        current_revision: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.current_revision = current_revision


class ImportProposalValidationError(ImportProposalError, ValueError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def _required(value: str, field_name: str, *, max_length: int = 255) -> str:
    normalized = value.strip()
    if not normalized:
        raise ImportProposalValidationError(
            f"{field_name} is required", code="invalid_import_proposal"
        )
    if len(normalized) > max_length:
        raise ImportProposalValidationError(
            f"{field_name} is too long", code="invalid_import_proposal"
        )
    return normalized


def _optional(value: str | None, field_name: str) -> str | None:
    return _required(value, field_name) if value is not None else None


def _aware_utc(value: datetime | None) -> datetime:
    moment = value or datetime.now(timezone.utc)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ImportProposalValidationError(
            "now must be timezone-aware", code="invalid_import_proposal"
        )
    return moment.astimezone(timezone.utc)


def normalized_json_object(value: Mapping[str, object]) -> dict[str, object]:
    """Return a detached, finite JSON object with a bounded encoded size."""

    if not isinstance(value, Mapping):
        raise ImportProposalValidationError(
            "normalized_diff must be a JSON object", code="invalid_normalized_diff"
        )
    try:
        encoded = json.dumps(
            dict(value),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        decoded = json.loads(encoded)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ImportProposalValidationError(
            "normalized_diff must be JSON-safe", code="invalid_normalized_diff"
        ) from exc
    if len(encoded) > MAX_NORMALIZED_DIFF_BYTES:
        raise ImportProposalValidationError(
            "normalized_diff exceeds its size limit",
            code="normalized_diff_too_large",
        )
    if not isinstance(decoded, dict):
        raise ImportProposalValidationError(
            "normalized_diff must be a JSON object", code="invalid_normalized_diff"
        )
    return cast(dict[str, object], decoded)


def proposal_review_status(
    *,
    target_kind: ImportTargetKind,
    target_project_id: str | None,
    normalized_diff: Mapping[str, object],
) -> ImportProposalStatus:
    """Keep incomplete or lossy previews out of the confirmation state."""

    if target_kind == "needs_user_choice" or target_project_id is None:
        return "draft"
    workbook = normalized_diff.get("workbook")
    if isinstance(workbook, Mapping) and bool(workbook.get("truncated")):
        return "draft"
    for field_name in ("conflicts", "invalid"):
        values = normalized_diff.get(field_name)
        if isinstance(values, list) and values:
            return "draft"
    return "awaiting_confirmation"


@dataclass(frozen=True, slots=True)
class ImportProposal:
    proposal_id: str
    organization_id: str
    attachment_id: str
    creator_user_id: str
    target_project_id: str | None
    plan_id: str | None
    target_kind: ImportTargetKind
    idempotency_key: str
    normalized_diff: dict[str, object]
    revision: int
    status: ImportProposalStatus
    confirmed_by: str | None
    confirmed_at: datetime | None
    resulting_entity_refs: tuple[dict[str, object], ...]
    standardized_error_code: str | None
    created_at: datetime
    updated_at: datetime
    execution_idempotency_key: str | None = None


@dataclass(frozen=True, slots=True)
class ImportExecutionState:
    """Durable execution state returned together with source revisions."""

    proposal: ImportProposal
    attachment_revision: int


class ImportProposalRepository(Protocol):
    def create(self, proposal: ImportProposal) -> ImportProposal: ...

    def get_for_actor(
        self, *, actor: ActorIdentity, proposal_id: str
    ) -> ImportProposal | None: ...

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
    ) -> ImportProposal: ...

    def confirm(
        self,
        *,
        actor: ActorIdentity,
        proposal_id: str,
        expected_revision: int,
        target_project_id: str,
        authorize_target: Callable[[], None],
        now: datetime,
    ) -> ImportProposal: ...

    def cancel(
        self,
        *,
        actor: ActorIdentity,
        proposal_id: str,
        expected_revision: int,
        now: datetime,
    ) -> ImportProposal: ...


AttachmentLoader = Callable[[ActorIdentity, str], AssistantAttachment | None]
AuthorizationStage = Literal["preview", "confirm"]
ProjectAuthorization = Callable[
    [ActorIdentity, str, ImportTargetKind, AuthorizationStage], None
]


def _classification(attachment: AssistantAttachment) -> AttachmentClassification:
    if attachment.classification is None:
        raise ImportProposalConflict(
            "attachment has not been classified", code="attachment_not_classified"
        )
    stored_payload = dict(attachment.classification_payload)
    nested = stored_payload.get("classification")
    # The classifier persists a private envelope containing source/model
    # metadata and the strict classification object.  Only that nested object
    # is an authorization input; envelope fields must never be accepted as
    # model contract extensions.  Flat payloads remain readable for direct
    # domain fixtures and pre-envelope records.
    payload = dict(nested) if isinstance(nested, Mapping) else stored_payload
    payload.setdefault("classification", attachment.classification)
    try:
        result = AttachmentClassification.model_validate(payload)
    except Exception as exc:
        raise ImportProposalConflict(
            "attachment classification is invalid", code="invalid_attachment_classification"
        ) from exc
    if result.classification != attachment.classification:
        raise ImportProposalConflict(
            "attachment classification changed", code="attachment_classification_conflict"
        )
    return result


def validate_import_target(
    *,
    attachment: AssistantAttachment,
    target_project_id: str | None,
    target_kind: ImportTargetKind,
    normalized_diff: Mapping[str, object],
    now: datetime,
) -> tuple[str | None, ImportTargetKind, dict[str, object]]:
    if target_kind not in get_args(ImportTargetKind):
        raise ImportProposalValidationError(
            "target_kind is not supported", code="unsupported_import_target"
        )
    if attachment.expires_at <= now or attachment.status in {
        "rejected",
        "expired",
        "rejecting",
        "expiring",
        "failed",
        "imported",
    }:
        raise ImportProposalConflict(
            "attachment is not available for a proposal",
            code="attachment_not_available",
        )
    if attachment.status not in {"proposal_ready", "needs_user_choice"}:
        raise ImportProposalConflict(
            "attachment classification is not ready",
            code="attachment_classification_not_ready",
        )

    classification = _classification(attachment)
    if classification.classification == "unsupported":
        raise ImportProposalValidationError(
            "unsupported attachments cannot create import proposals",
            code="unsupported_import_target",
        )
    if classification.classification == "needs_user_choice":
        candidates = set(classification.candidate_classifications)
        if target_kind == "needs_user_choice":
            if attachment.status != "needs_user_choice":
                raise ImportProposalConflict(
                    "resolved classifications cannot return to user choice",
                    code="attachment_classification_conflict",
                )
        elif target_kind not in candidates:
            raise ImportProposalValidationError(
                "target_kind is outside the classified candidates",
                code="target_kind_classification_mismatch",
            )
    elif target_kind != classification.classification:
        raise ImportProposalValidationError(
            "target_kind does not match the attachment classification",
            code="target_kind_classification_mismatch",
        )

    project_id = _optional(target_project_id, "target_project_id")
    classified_project = classification.target_project_id
    if classified_project is not None and project_id not in {None, classified_project}:
        raise ImportProposalValidationError(
            "target project does not match the attachment classification",
            code="target_project_classification_mismatch",
        )
    project_id = project_id or classified_project
    if target_kind != "needs_user_choice" and project_id is None:
        raise ImportProposalValidationError(
            "a resolved proposal requires target_project_id",
            code="target_project_required",
        )

    safe_diff = normalized_json_object(normalized_diff)
    if target_kind == "prompt_asset":
        prompt_kind = safe_diff.get("prompt_kind")
        if prompt_kind not in get_args(PromptKind):
            raise ImportProposalValidationError(
                "prompt_asset requires an explicit existing prompt_kind",
                code="prompt_kind_required",
            )
        if (
            classification.prompt_kind is not None
            and prompt_kind != classification.prompt_kind
        ):
            raise ImportProposalValidationError(
                "prompt_kind does not match the attachment classification",
                code="prompt_kind_classification_mismatch",
            )
    return project_id, target_kind, safe_diff


class ImportProposalService:
    """Review boundary: confirmation only releases a proposal for later import."""

    def __init__(
        self,
        *,
        repository: ImportProposalRepository,
        attachment_loader: AttachmentLoader,
        authorize_project: ProjectAuthorization,
    ) -> None:
        self._repository = repository
        self._attachment_loader = attachment_loader
        self._authorize_project = authorize_project

    def create(
        self,
        *,
        actor: ActorIdentity,
        attachment_id: str,
        target_kind: ImportTargetKind,
        normalized_diff: Mapping[str, object],
        idempotency_key: str,
        target_project_id: str | None = None,
        plan_id: str | None = None,
        proposal_id: str | None = None,
        now: datetime | None = None,
    ) -> ImportProposal:
        changed_at = _aware_utc(now)
        attachment_id = _required(attachment_id, "attachment_id")
        attachment = self._attachment_loader(actor, attachment_id)
        if attachment is None:
            raise ImportProposalNotFound("attachment not found")
        self._assert_attachment_actor(actor, attachment)
        project_id, target_kind, safe_diff = validate_import_target(
            attachment=attachment,
            target_project_id=target_project_id,
            target_kind=target_kind,
            normalized_diff=normalized_diff,
            now=changed_at,
        )
        if project_id is not None:
            self._authorize_project(actor, project_id, target_kind, "preview")
        status = proposal_review_status(
            target_kind=target_kind,
            target_project_id=project_id,
            normalized_diff=safe_diff,
        )
        proposal = ImportProposal(
            proposal_id=_required(
                proposal_id or f"aip_{uuid.uuid4().hex}", "proposal_id"
            ),
            organization_id=actor.organization_id,
            attachment_id=attachment_id,
            creator_user_id=actor.user_id,
            target_project_id=project_id,
            plan_id=_optional(plan_id, "plan_id"),
            target_kind=target_kind,
            idempotency_key=_required(
                idempotency_key, "idempotency_key", max_length=512
            ),
            normalized_diff=safe_diff,
            revision=0,
            status=status,
            confirmed_by=None,
            confirmed_at=None,
            resulting_entity_refs=(),
            standardized_error_code=None,
            created_at=changed_at,
            updated_at=changed_at,
        )
        return self._repository.create(proposal)

    def get(self, *, actor: ActorIdentity, proposal_id: str) -> ImportProposal:
        proposal = self._repository.get_for_actor(
            actor=actor, proposal_id=_required(proposal_id, "proposal_id")
        )
        if proposal is None:
            raise ImportProposalNotFound("import proposal not found")
        return proposal

    def revise(
        self,
        *,
        actor: ActorIdentity,
        proposal_id: str,
        expected_revision: int,
        target_kind: ImportTargetKind,
        normalized_diff: Mapping[str, object],
        target_project_id: str | None,
        now: datetime | None = None,
    ) -> ImportProposal:
        current = self.get(actor=actor, proposal_id=proposal_id)
        attachment = self._attachment_loader(actor, current.attachment_id)
        if attachment is None:
            raise ImportProposalNotFound("attachment not found")
        self._assert_attachment_actor(actor, attachment)
        changed_at = _aware_utc(now)
        project_id, target_kind, safe_diff = validate_import_target(
            attachment=attachment,
            target_project_id=target_project_id,
            target_kind=target_kind,
            normalized_diff=normalized_diff,
            now=changed_at,
        )
        if project_id is not None:
            self._authorize_project(actor, project_id, target_kind, "preview")
        return self._repository.revise(
            actor=actor,
            proposal_id=current.proposal_id,
            expected_revision=expected_revision,
            target_project_id=project_id,
            target_kind=target_kind,
            normalized_diff=safe_diff,
            now=changed_at,
        )

    def confirm(
        self,
        *,
        actor: ActorIdentity,
        proposal_id: str,
        expected_revision: int,
        target_project_id: str,
        now: datetime | None = None,
    ) -> ImportProposal:
        changed_at = _aware_utc(now)
        current = self.get(actor=actor, proposal_id=proposal_id)
        project_id = _required(target_project_id, "target_project_id")
        if current.target_kind == "needs_user_choice":
            raise ImportProposalConflict(
                "choose a concrete target kind before confirmation",
                code="target_kind_required",
            )
        if current.target_project_id not in {None, project_id}:
            raise ImportProposalConflict(
                "confirmation target does not match the proposal",
                code="target_project_conflict",
            )
        attachment = self._attachment_loader(actor, current.attachment_id)
        if attachment is None:
            raise ImportProposalNotFound("attachment not found")
        self._assert_attachment_actor(actor, attachment)
        validated_project, validated_kind, _ = validate_import_target(
            attachment=attachment,
            target_project_id=project_id,
            target_kind=current.target_kind,
            normalized_diff=current.normalized_diff,
            now=changed_at,
        )
        if validated_project != project_id or validated_kind != current.target_kind:
            raise ImportProposalConflict(
                "proposal no longer matches the attachment classification",
                code="attachment_classification_conflict",
            )
        return self._repository.confirm(
            actor=actor,
            proposal_id=current.proposal_id,
            expected_revision=expected_revision,
            target_project_id=project_id,
            authorize_target=lambda: self._authorize_project(
                actor, project_id, current.target_kind, "confirm"
            ),
            now=changed_at,
        )

    def cancel(
        self,
        *,
        actor: ActorIdentity,
        proposal_id: str,
        expected_revision: int,
        now: datetime | None = None,
    ) -> ImportProposal:
        return self._repository.cancel(
            actor=actor,
            proposal_id=_required(proposal_id, "proposal_id"),
            expected_revision=expected_revision,
            now=_aware_utc(now),
        )

    @staticmethod
    def _assert_attachment_actor(
        actor: ActorIdentity, attachment: AssistantAttachment
    ) -> None:
        if (
            attachment.organization_id != actor.organization_id
            or attachment.creator_user_id != actor.user_id
        ):
            raise ImportProposalNotFound("attachment not found")


__all__ = [
    "ImportProposal",
    "ImportExecutionState",
    "ImportProposalConflict",
    "ImportProposalError",
    "ImportProposalNotFound",
    "ImportProposalRepository",
    "ImportProposalService",
    "ImportProposalStatus",
    "ImportProposalValidationError",
    "ImportTargetKind",
    "AuthorizationStage",
    "MAX_NORMALIZED_DIFF_BYTES",
    "normalized_json_object",
    "proposal_review_status",
    "validate_import_target",
]
