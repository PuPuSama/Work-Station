from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import PurePath
from typing import Mapping

from knowledge_agent.ingestion.contracts import DocumentInput, ParsedDocument
from knowledge_agent.ingestion.parsers import (
    DocumentParseError,
    DocumentParserError,
    DocumentParserRouter,
)
from models import PromptKind
from services.access_control import ActorIdentity
from services.object_store import ObjectStore, ObjectStoreError
from services.server_task_intake import ServerTaskIntakeRow
from services.server_task_workbook import (
    IMPORT_FIELDS,
    ServerTaskWorkbookError,
    preview_task_workbook,
)
from workflow_assistant.attachments import MAX_ATTACHMENT_BYTES, AssistantAttachment
from workflow_assistant.classification import AttachmentClassification
from workflow_assistant.import_proposals import normalized_json_object


MAX_PROMPT_CHARACTERS = 40_000
MAX_PROJECT_NOTES_CHARACTERS = 30_000
_TEXT_SUFFIXES = frozenset({".txt", ".md"})
_DOCUMENT_SUFFIXES = frozenset({".pdf", ".docx", ".xlsx", ".xlsm"})
_WORKBOOK_TARGETS = frozenset({"task_workbook", "topic_library"})


class ProposalPreviewError(RuntimeError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ExistingPrompt:
    prompt_id: str
    name: str
    kind: PromptKind
    content: str = field(repr=False)
    version: int = 1


@dataclass(frozen=True, slots=True)
class ExistingTabularItem:
    item_id: str
    topic: str
    primary_keyword: str = ""
    competitor_keyword: str = ""
    competitor_blog: str = ""


@dataclass(frozen=True, slots=True)
class ProposalTargetSnapshot:
    """Bounded, already-authorized current Project state used for comparison."""

    project_id: str
    knowledge_content_hashes: frozenset[str] = frozenset()
    prompts: tuple[ExistingPrompt, ...] = ()
    project_notes: str = ""
    project_notes_revision: int = 0
    task_rows: tuple[ExistingTabularItem, ...] = ()
    topics: tuple[ExistingTabularItem, ...] = ()


def _normalized_text(content: bytes) -> str:
    try:
        text = content.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise ProposalPreviewError(
            "text attachment must be valid UTF-8",
            code="proposal_preview_parse_failed",
        ) from exc
    if "\x00" in text:
        raise ProposalPreviewError(
            "text attachment contains unsupported control data",
            code="proposal_preview_parse_failed",
        )
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        raise ProposalPreviewError(
            "attachment contains no readable text",
            code="proposal_preview_parse_failed",
        )
    return normalized


def _document_text(parsed: ParsedDocument) -> str:
    text = parsed.text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        raise ProposalPreviewError(
            "attachment contains no readable text",
            code="proposal_preview_parse_failed",
        )
    return text


def _clean_cell(value: str) -> str:
    return " ".join(str(value or "").split())


def _row_values(
    row: tuple[str, ...], mapping: Mapping[str, int | None]
) -> dict[str, str]:
    values: dict[str, str] = {}
    for field_name in IMPORT_FIELDS:
        column = mapping.get(field_name)
        value = row[column] if column is not None and column < len(row) else ""
        values[field_name] = _clean_cell(value)
    return values


def _identity(value: str) -> str:
    return " ".join(value.casefold().split())


def _tabular_diff(
    *,
    filename: str,
    content: bytes,
    existing: tuple[ExistingTabularItem, ...],
    include_competitor_blog: bool,
) -> dict[str, object]:
    try:
        preview = preview_task_workbook(filename=filename, content=content)
    except ServerTaskWorkbookError as exc:
        raise ProposalPreviewError(
            "workbook does not satisfy the existing preview contract",
            code="proposal_preview_parse_failed",
        ) from exc
    if preview.mapping.get("topic") is None:
        raise ProposalPreviewError(
            "workbook requires an explicit topic column",
            code="proposal_preview_needs_user_choice",
        )

    existing_by_topic = {_identity(item.topic): item for item in existing}
    seen: dict[str, dict[str, str]] = {}
    create: list[dict[str, object]] = []
    skip: list[dict[str, object]] = []
    conflicts: list[dict[str, object]] = []
    invalid: list[dict[str, object]] = []
    fields = ["topic", "primary_keyword", "competitor_keyword"]
    if include_competitor_blog:
        fields.append("competitor_blog")

    for offset, raw_row in enumerate(preview.rows, start=2):
        values = _row_values(raw_row, preview.mapping)
        values = {name: values[name] for name in fields}
        topic_key = _identity(values["topic"])
        invalid_fields: list[str] = []
        if include_competitor_blog and topic_key:
            try:
                normalized = ServerTaskIntakeRow(**values).normalized()
                values = {
                    "topic": normalized.topic,
                    "primary_keyword": normalized.primary_keyword,
                    "competitor_keyword": normalized.competitor_keyword,
                    "competitor_blog": normalized.competitor_blog,
                }
            except ValueError:
                invalid_fields = ["task_row"]
        elif topic_key:
            invalid_fields = [
                name for name, value in values.items() if len(value) > 500
            ]
        if not topic_key or invalid_fields:
            invalid.append(
                {
                    "source_row": offset,
                    "reason": "missing_topic" if not topic_key else "invalid_values",
                    "fields": invalid_fields,
                }
            )
            continue
        duplicate_in_file = seen.get(topic_key)
        if duplicate_in_file is not None:
            bucket = skip if duplicate_in_file == values else conflicts
            bucket.append(
                {
                    "source_row": offset,
                    "reason": (
                        "duplicate_in_attachment"
                        if duplicate_in_file == values
                        else "conflicting_rows_in_attachment"
                    ),
                    "incoming": values,
                }
            )
            continue
        seen[topic_key] = values

        current = existing_by_topic.get(topic_key)
        if current is None:
            create.append({"source_row": offset, **values})
            continue
        current_values = {
            "topic": _clean_cell(current.topic),
            "primary_keyword": _clean_cell(current.primary_keyword),
            "competitor_keyword": _clean_cell(current.competitor_keyword),
        }
        if include_competitor_blog:
            current_values["competitor_blog"] = _clean_cell(
                current.competitor_blog
            )
        if current_values == values:
            skip.append(
                {
                    "source_row": offset,
                    "reason": "already_exists",
                    "existing_id": current.item_id,
                    "incoming": values,
                }
            )
        else:
            conflicts.append(
                {
                    "source_row": offset,
                    "reason": "same_topic_different_values",
                    "existing_id": current.item_id,
                    "before": current_values,
                    "after": values,
                }
            )

    return {
        "workbook": {
            "sheet_name": preview.sheet_name,
            "headers": list(preview.headers),
            "mapping": dict(preview.mapping),
            "truncated": preview.truncated,
        },
        "create": create,
        "update": [],
        "skip": skip,
        "conflicts": conflicts,
        "invalid": invalid,
    }


class ProposalPreviewBuilder:
    """Build a review-only diff from an untrusted temporary attachment.

    Callers provide an already-authorized current-state snapshot. This service
    reads bytes and compares data only; it has no import, publication, or tool
    execution dependency.
    """

    def __init__(
        self,
        object_store: ObjectStore,
        *,
        parser_router: DocumentParserRouter | None = None,
    ) -> None:
        self._object_store = object_store
        self._parser_router = parser_router or DocumentParserRouter()

    def build(
        self,
        *,
        actor: ActorIdentity,
        attachment: AssistantAttachment,
        classification: AttachmentClassification,
        target: ProposalTargetSnapshot,
        now: datetime | None = None,
    ) -> dict[str, object]:
        self._validate_scope(
            actor,
            attachment,
            classification,
            target,
            now=now or datetime.now(timezone.utc),
        )
        try:
            content = self._object_store.get(
                attachment.object_key, max_bytes=MAX_ATTACHMENT_BYTES
            )
        except ObjectStoreError as exc:
            raise ProposalPreviewError(
                "attachment object is unavailable",
                code="proposal_preview_storage_unavailable",
            ) from exc
        if len(content) != attachment.byte_size or hashlib.sha256(content).hexdigest() != attachment.sha256:
            raise ProposalPreviewError(
                "attachment object does not match its immutable metadata",
                code="proposal_preview_integrity_failed",
            )

        target_kind = classification.classification
        source = {
            "attachment_id": attachment.attachment_id,
            "filename": attachment.original_filename,
            "mime_type": attachment.mime_type,
            "byte_size": attachment.byte_size,
            "sha256": attachment.sha256,
        }
        if target_kind == "knowledge_source":
            body = self._knowledge_diff(attachment, content, target)
        elif target_kind == "prompt_asset":
            body = self._prompt_diff(attachment, content, classification, target)
        elif target_kind == "project_notes":
            body = self._notes_diff(attachment, content, target)
        elif target_kind in _WORKBOOK_TARGETS:
            body = _tabular_diff(
                filename=attachment.original_filename,
                content=content,
                existing=(target.task_rows if target_kind == "task_workbook" else target.topics),
                include_competitor_blog=target_kind == "task_workbook",
            )
        else:
            raise ProposalPreviewError(
                "classification requires an explicit supported choice",
                code="proposal_preview_needs_user_choice",
            )

        result = {
            "schema_version": 1,
            "target_project_id": target.project_id,
            "target_kind": target_kind,
            "source": source,
            "requires_publication_review": target_kind == "knowledge_source",
            **body,
        }
        return normalized_json_object(result)

    @staticmethod
    def _validate_scope(
        actor: ActorIdentity,
        attachment: AssistantAttachment,
        classification: AttachmentClassification,
        target: ProposalTargetSnapshot,
        *,
        now: datetime,
    ) -> None:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ProposalPreviewError(
                "preview time must be timezone-aware",
                code="proposal_preview_invalid_request",
            )
        if (
            actor.organization_id != attachment.organization_id
            or actor.user_id != attachment.creator_user_id
        ):
            raise ProposalPreviewError(
                "attachment is outside the actor scope",
                code="proposal_preview_not_found",
            )
        if attachment.expires_at <= now.astimezone(timezone.utc):
            raise ProposalPreviewError(
                "attachment is no longer available",
                code="proposal_preview_not_found",
            )
        if attachment.status != "proposal_ready":
            raise ProposalPreviewError(
                "attachment is not ready for a resolved proposal",
                code="proposal_preview_needs_user_choice",
            )
        if attachment.classification != classification.classification:
            raise ProposalPreviewError(
                "classification does not match the attachment state",
                code="proposal_preview_classification_conflict",
            )
        stored_payload = dict(attachment.classification_payload)
        nested = stored_payload.get("classification")
        persisted_payload = (
            dict(nested) if isinstance(nested, Mapping) else stored_payload
        )
        persisted_payload.setdefault("classification", attachment.classification)
        try:
            persisted = AttachmentClassification.model_validate(persisted_payload)
        except Exception as exc:
            raise ProposalPreviewError(
                "stored attachment classification is invalid",
                code="proposal_preview_classification_conflict",
            ) from exc
        if persisted != classification:
            raise ProposalPreviewError(
                "classification does not match the stored attachment result",
                code="proposal_preview_classification_conflict",
            )
        if classification.classification in {"unsupported", "needs_user_choice"}:
            raise ProposalPreviewError(
                "classification requires an explicit supported choice",
                code="proposal_preview_needs_user_choice",
            )
        if classification.target_project_id != target.project_id:
            raise ProposalPreviewError(
                "target project does not match the classification",
                code="proposal_preview_project_conflict",
            )
        # proposed_project_id is upload-time context, not an import grant.
        # A later explicit choice may target another freshly authorized
        # project; classification.target_project_id is the reviewed source of
        # truth at this boundary.

    def _parse(self, attachment: AssistantAttachment, content: bytes) -> ParsedDocument:
        try:
            return self._parser_router.parse(
                DocumentInput(
                    filename=attachment.original_filename,
                    content=content,
                    content_type=attachment.mime_type,
                )
            )
        except (DocumentParserError, DocumentParseError, ValueError) as exc:
            raise ProposalPreviewError(
                "attachment could not be parsed safely",
                code="proposal_preview_parse_failed",
            ) from exc

    def _text_content(self, attachment: AssistantAttachment, content: bytes) -> str:
        suffix = PurePath(attachment.original_filename).suffix.casefold()
        if suffix in _TEXT_SUFFIXES:
            return _normalized_text(content)
        if suffix not in _DOCUMENT_SUFFIXES or suffix in {".xlsx", ".xlsm"}:
            raise ProposalPreviewError(
                "attachment structure is incompatible with this target",
                code="proposal_preview_needs_user_choice",
            )
        return _document_text(self._parse(attachment, content))

    def _knowledge_diff(
        self,
        attachment: AssistantAttachment,
        content: bytes,
        target: ProposalTargetSnapshot,
    ) -> dict[str, object]:
        suffix = PurePath(attachment.original_filename).suffix.casefold()
        if suffix in _TEXT_SUFFIXES:
            text = _normalized_text(content)
            parser_name = "utf8-text"
            parser_version = "1.0"
            block_count = len(text.splitlines())
            asset_count = 0
            title = PurePath(attachment.original_filename).stem
        else:
            parsed = self._parse(attachment, content)
            text = _document_text(parsed)
            parser_name = parsed.parser_name
            parser_version = parsed.parser_version
            block_count = len(parsed.blocks)
            asset_count = len(parsed.assets)
            title = parsed.title or PurePath(attachment.original_filename).stem
        candidate = {
            "filename": attachment.original_filename,
            "title": title,
            "content_hash": attachment.sha256,
            "parser_name": parser_name,
            "parser_version": parser_version,
            "character_count": len(text),
            "block_count": block_count,
            "asset_count": asset_count,
            "publication_status": "candidate",
        }
        if attachment.sha256 in target.knowledge_content_hashes:
            return {
                "create": [],
                "update": [],
                "skip": [{"reason": "content_hash_already_exists", **candidate}],
                "conflicts": [],
                "invalid": [],
            }
        return {
            "create": [candidate],
            "update": [],
            "skip": [],
            "conflicts": [],
            "invalid": [],
        }

    def _prompt_diff(
        self,
        attachment: AssistantAttachment,
        content: bytes,
        classification: AttachmentClassification,
        target: ProposalTargetSnapshot,
    ) -> dict[str, object]:
        kind = classification.prompt_kind
        if kind is None:
            raise ProposalPreviewError(
                "prompt kind must be selected explicitly",
                code="proposal_preview_needs_user_choice",
            )
        text = self._text_content(attachment, content)
        if len(text) > MAX_PROMPT_CHARACTERS:
            raise ProposalPreviewError(
                "prompt content exceeds the existing prompt contract",
                code="proposal_preview_invalid_content",
            )
        if kind == "humanize" and text.count("{{ARTICLE}}") != 1:
            raise ProposalPreviewError(
                "humanize prompt must contain exactly one {{ARTICLE}} placeholder",
                code="proposal_preview_invalid_content",
            )
        same_kind = [prompt for prompt in target.prompts if prompt.kind == kind]
        exact = next((prompt for prompt in same_kind if prompt.content.strip() == text), None)
        item = {
            "name": PurePath(attachment.original_filename).stem[:120],
            "prompt_kind": kind,
            "content": text,
            "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }
        if exact is not None:
            return {
                "prompt_kind": kind,
                "create": [],
                "update": [],
                "skip": [{"reason": "identical_prompt_exists", "existing_id": exact.prompt_id, **item}],
                "conflicts": [],
                "invalid": [],
            }
        if same_kind:
            return {
                "prompt_kind": kind,
                "create": [],
                "update": [],
                "skip": [],
                "conflicts": [
                    {
                        "reason": "prompt_destination_requires_user_choice",
                        "existing": [
                            {"prompt_id": prompt.prompt_id, "name": prompt.name, "version": prompt.version}
                            for prompt in same_kind
                        ],
                        "incoming": item,
                    }
                ],
                "invalid": [],
            }
        return {
            "prompt_kind": kind,
            "create": [item],
            "update": [],
            "skip": [],
            "conflicts": [],
            "invalid": [],
        }

    def _notes_diff(
        self,
        attachment: AssistantAttachment,
        content: bytes,
        target: ProposalTargetSnapshot,
    ) -> dict[str, object]:
        text = self._text_content(attachment, content)
        if len(text) > MAX_PROJECT_NOTES_CHARACTERS:
            raise ProposalPreviewError(
                "project notes exceed the existing metadata contract",
                code="proposal_preview_invalid_content",
            )
        before = target.project_notes.replace("\r\n", "\n").replace("\r", "\n").strip()
        if before == text:
            return {
                "create": [],
                "update": [],
                "skip": [{"reason": "project_notes_unchanged", "revision": target.project_notes_revision}],
                "conflicts": [],
                "invalid": [],
            }
        return {
            "create": [],
            "update": [
                {
                    "field": "project_notes",
                    "expected_revision": target.project_notes_revision,
                    "before": before,
                    "after": text,
                }
            ],
            "skip": [],
            "conflicts": [],
            "invalid": [],
        }


__all__ = [
    "ExistingPrompt",
    "ExistingTabularItem",
    "ProposalPreviewBuilder",
    "ProposalPreviewError",
    "ProposalTargetSnapshot",
]
