from __future__ import annotations

import hashlib
import io
import re
import unicodedata
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal, Protocol
from urllib.parse import quote
from xml.etree import ElementTree

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from services.access_control import ActorIdentity
from services.object_store import ObjectStore, ObjectStoreError


AttachmentStatus = Literal[
    "uploading",
    "uploaded",
    "classifying",
    "needs_user_choice",
    "proposal_ready",
    "importing",
    "imported",
    "rejected",
    "expired",
    "failed",
    "rejecting",
    "expiring",
]

ATTACHMENT_RETENTION = timedelta(days=7)
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
MAX_PDF_PAGES = 5_000
DOWNLOAD_URL_TTL_SECONDS = 300
MAX_OOXML_ENTRIES = 2_048
MAX_OOXML_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_OOXML_COMPRESSION_RATIO = 100
MAX_OOXML_METADATA_BYTES = 1024 * 1024

_SCOPE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_UNSAFE_FILENAME = re.compile(r"[<>:\"/\\|?*\x00-\x1f\x7f]")
_WINDOWS_DEVICE_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}

_PDF = "application/pdf"
_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_XLSM = "application/vnd.ms-excel.sheet.macroenabled.12"
_XLSM_MAIN_CONTENT_TYPE = (
    "application/vnd.ms-excel.sheet.macroenabled.main+xml"
)
_ALLOWED_TYPES: dict[str, frozenset[str]] = {
    ".pdf": frozenset({_PDF}),
    ".docx": frozenset({_DOCX}),
    ".xlsx": frozenset({_XLSX}),
    ".xlsm": frozenset({_XLSM}),
    ".txt": frozenset({"text/plain"}),
    ".md": frozenset({"text/markdown", "text/plain"}),
}
_TEXT_SUFFIXES = frozenset({".txt", ".md"})
_ACTIVE_OOXML_PATH_PARTS = frozenset({"activex", "embeddings", "externallinks"})


class AttachmentError(RuntimeError):
    """Stable base error for the temporary attachment boundary."""


class AttachmentValidationError(AttachmentError, ValueError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class AttachmentNotFound(AttachmentError):
    """The attachment is absent, expired, or outside the actor's scope."""


class AttachmentConflict(AttachmentError):
    """The attachment cannot make the requested lifecycle transition."""


class AttachmentStorageError(AttachmentError):
    """Private object storage did not complete the requested operation."""


@dataclass(frozen=True, slots=True)
class AssistantAttachment:
    attachment_id: str
    organization_id: str
    creator_user_id: str
    conversation_id: str
    proposed_project_id: str | None
    plan_id: str | None
    idempotency_key: str
    object_key: str
    original_filename: str
    mime_type: str
    byte_size: int
    sha256: str
    classification: str | None
    classification_payload: dict[str, object]
    revision: int
    status: AttachmentStatus
    expires_at: datetime
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class AttachmentDownload:
    attachment: AssistantAttachment
    download_url: str
    url_expires_seconds: int


@dataclass(frozen=True, slots=True)
class AttachmentReservation:
    attachment: AssistantAttachment
    should_write_object: bool


class AttachmentRepository(Protocol):
    """PostgreSQL persistence seam; implementations must scope every actor read."""

    def reserve_upload(self, attachment: AssistantAttachment) -> AttachmentReservation: ...

    def finalize_upload(self, *, attachment: AssistantAttachment, now: datetime) -> AssistantAttachment: ...

    def mark_upload_failed(self, *, attachment: AssistantAttachment, now: datetime) -> None: ...

    def get_for_actor(
        self,
        *,
        organization_id: str,
        creator_user_id: str,
        conversation_id: str,
        attachment_id: str,
    ) -> AssistantAttachment | None: ...

    def get_by_idempotency_for_actor(
        self,
        *,
        organization_id: str,
        creator_user_id: str,
        conversation_id: str,
        idempotency_key: str,
    ) -> AssistantAttachment | None: ...

    def list_for_actor(
        self,
        *,
        organization_id: str,
        creator_user_id: str,
        conversation_id: str,
        limit: int,
    ) -> tuple[AssistantAttachment, ...]: ...

    def claim_rejection(
        self,
        *,
        organization_id: str,
        creator_user_id: str,
        conversation_id: str,
        attachment_id: str,
        now: datetime,
    ) -> AssistantAttachment: ...

    def finalize_rejection(
        self, *, attachment: AssistantAttachment, now: datetime
    ) -> AssistantAttachment: ...

    def claim_expired(
        self,
        *,
        before: datetime,
        limit: int,
        exclude_attachment_ids: tuple[str, ...],
    ) -> tuple[AssistantAttachment, ...]: ...

    def finalize_expiry(
        self, *, attachment: AssistantAttachment, now: datetime
    ) -> AssistantAttachment | None: ...


def _aware_utc(value: datetime | None) -> datetime:
    result = value or datetime.now(timezone.utc)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return result.astimezone(timezone.utc)


def _scope_id(value: str, field: str) -> str:
    normalized = str(value or "").strip()
    if (
        not _SCOPE_ID.fullmatch(normalized)
        or normalized in {".", ".."}
        or "/" in normalized
        or "\\" in normalized
    ):
        raise AttachmentValidationError(
            f"{field} contains unsupported characters",
            code="invalid_attachment_scope",
        )
    return normalized


def _filename(value: str) -> tuple[str, str]:
    filename = str(value or "")
    if (
        not filename
        or filename != filename.strip()
        or len(filename) > 255
        or filename in {".", ".."}
        or unicodedata.normalize("NFC", filename) != filename
        or _UNSAFE_FILENAME.search(filename)
        or filename.endswith((".", " "))
    ):
        raise AttachmentValidationError(
            "original_filename is unsafe",
            code="invalid_attachment_filename",
        )
    stem, separator, extension = filename.rpartition(".")
    suffix = f".{extension.casefold()}" if separator else ""
    if not stem or suffix not in _ALLOWED_TYPES:
        raise AttachmentValidationError(
            "attachment file type is not allowed",
            code="unsupported_attachment_type",
        )
    if filename.split(".", 1)[0].casefold() in _WINDOWS_DEVICE_NAMES:
        raise AttachmentValidationError(
            "original_filename is unsafe",
            code="invalid_attachment_filename",
        )
    return filename, suffix


def _validated_mime(value: str, suffix: str) -> str:
    mime_type = str(value or "").strip().casefold()
    if mime_type not in _ALLOWED_TYPES[suffix]:
        raise AttachmentValidationError(
            "attachment MIME type does not match its filename",
            code="attachment_mime_mismatch",
        )
    return mime_type


def _idempotency_key(value: str) -> str:
    normalized = str(value or "").strip()
    if (
        not normalized
        or len(normalized) > 128
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
    ):
        raise AttachmentValidationError(
            "idempotency_key is invalid",
            code="invalid_attachment_idempotency_key",
        )
    return normalized


def _safe_ooxml_entry(info: zipfile.ZipInfo) -> str:
    name = info.filename
    safe_name = name.rstrip("/") if info.is_dir() else name
    segments = safe_name.split("/")
    if (
        not safe_name
        or name.startswith(("/", "\\"))
        or "\\" in name
        or ":" in segments[0]
        or any(segment in {"", ".", ".."} for segment in segments)
    ):
        raise AttachmentValidationError(
            "Office package contains an unsafe path",
            code="unsafe_attachment_package",
        )
    # ZIP external attributes can encode Unix symlinks. OOXML has no reason
    # to contain one and consumers must never follow it.
    if ((info.external_attr >> 16) & 0o170000) == 0o120000:
        raise AttachmentValidationError(
            "Office package contains an unsafe link",
            code="unsafe_attachment_package",
        )
    if info.flag_bits & 0x1:
        raise AttachmentValidationError(
            "encrypted Office packages are not allowed",
            code="unsafe_attachment_package",
        )
    if info.file_size < 0 or info.compress_size < 0:
        raise AttachmentValidationError(
            "Office package size metadata is invalid",
            code="unsafe_attachment_package",
        )
    if info.file_size and info.file_size / max(info.compress_size, 1) > MAX_OOXML_COMPRESSION_RATIO:
        raise AttachmentValidationError(
            "Office package compression ratio is unsafe",
            code="unsafe_attachment_package",
        )
    return safe_name


def _read_ooxml_metadata(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
) -> bytes:
    if info.file_size > MAX_OOXML_METADATA_BYTES:
        raise AttachmentValidationError(
            "Office package metadata is too large",
            code="unsafe_attachment_package",
        )
    try:
        content = archive.read(info)
    except (RuntimeError, OSError, ValueError, zipfile.BadZipFile) as exc:
        raise AttachmentValidationError(
            "Office package metadata could not be read safely",
            code="unsafe_attachment_package",
        ) from exc
    if len(content) != info.file_size:
        raise AttachmentValidationError(
            "Office package metadata size is inconsistent",
            code="unsafe_attachment_package",
        )
    return content


def _reject_external_relationships(
    archive: zipfile.ZipFile,
    entries: dict[str, zipfile.ZipInfo],
) -> None:
    for name, info in entries.items():
        if not name.casefold().endswith(".rels"):
            continue
        try:
            root = ElementTree.fromstring(_read_ooxml_metadata(archive, info))
        except ElementTree.ParseError as exc:
            raise AttachmentValidationError(
                "Office relationships are malformed",
                code="unsafe_attachment_package",
            ) from exc
        for relationship in root.iter():
            if relationship.tag.rsplit("}", 1)[-1] != "Relationship":
                continue
            if str(relationship.attrib.get("TargetMode") or "").casefold() == "external":
                raise AttachmentValidationError(
                    "external Office relationships are not allowed",
                    code="active_attachment_content",
                )


def _validate_zip_signature(content: bytes, suffix: str) -> None:
    if not content.startswith(b"PK\x03\x04"):
        raise AttachmentValidationError(
            "attachment signature does not match its type",
            code="attachment_signature_mismatch",
        )
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_OOXML_ENTRIES:
                raise AttachmentValidationError(
                    "Office package contains too many entries",
                    code="unsafe_attachment_package",
                )
            entries: dict[str, zipfile.ZipInfo] = {}
            total_size = 0
            total_compressed_size = 0
            for info in infos:
                name = _safe_ooxml_entry(info)
                if name in entries:
                    raise AttachmentValidationError(
                        "Office package contains duplicate entries",
                        code="unsafe_attachment_package",
                    )
                entries[name] = info
                total_size += info.file_size
                total_compressed_size += info.compress_size
                if total_size > MAX_OOXML_UNCOMPRESSED_BYTES:
                    raise AttachmentValidationError(
                        "Office package expands beyond its safe limit",
                        code="unsafe_attachment_package",
                    )
            if (
                total_size
                and total_size / max(total_compressed_size, 1)
                > MAX_OOXML_COMPRESSION_RATIO
            ):
                raise AttachmentValidationError(
                    "Office package compression ratio is unsafe",
                    code="unsafe_attachment_package",
                )
            required = (
                "word/document.xml" if suffix == ".docx" else "xl/workbook.xml"
            )
            required_entries = {"[Content_Types].xml", "_rels/.rels", required}
            if not required_entries.issubset(entries):
                raise AttachmentValidationError(
                    "Office package does not match its declared type",
                    code="attachment_signature_mismatch",
                )
            lowered_parts = {
                part.casefold()
                for name in entries
                for part in name.rstrip("/").split("/")
            }
            if lowered_parts & _ACTIVE_OOXML_PATH_PARTS:
                raise AttachmentValidationError(
                    "embedded or active Office objects are not allowed",
                    code="active_attachment_content",
                )
            has_vba = any(
                name.casefold().endswith("vbaproject.bin") for name in entries
            )
            if suffix != ".xlsm" and has_vba:
                raise AttachmentValidationError(
                    "macro-enabled attachments are not allowed",
                    code="active_attachment_content",
                )
            content_types = _read_ooxml_metadata(
                archive, entries["[Content_Types].xml"]
            ).decode("utf-8-sig", errors="strict")
            declares_xlsm = (
                _XLSM_MAIN_CONTENT_TYPE.casefold() in content_types.casefold()
            )
            if suffix == ".xlsm" and not declares_xlsm:
                raise AttachmentValidationError(
                    "macro workbook content type is missing",
                    code="attachment_signature_mismatch",
                )
            if suffix != ".xlsm" and declares_xlsm:
                raise AttachmentValidationError(
                    "Office package content type does not match its filename",
                    code="attachment_signature_mismatch",
                )
            _reject_external_relationships(archive, entries)
    except AttachmentValidationError:
        raise
    except (UnicodeDecodeError, zipfile.BadZipFile, OSError, ValueError) as exc:
        raise AttachmentValidationError(
            "Office attachment is not a valid package",
            code="attachment_signature_mismatch",
        ) from exc


def _validate_content(content: bytes, suffix: str) -> bytes:
    body = bytes(content)
    if not body:
        raise AttachmentValidationError(
            "attachment must not be empty",
            code="empty_attachment",
        )
    if len(body) > MAX_ATTACHMENT_BYTES:
        raise AttachmentValidationError(
            "attachment exceeds its size limit",
            code="attachment_too_large",
        )
    if suffix == ".pdf":
        if not body.startswith(b"%PDF-"):
            raise AttachmentValidationError(
                "attachment signature does not match its type",
                code="attachment_signature_mismatch",
            )
        try:
            reader = PdfReader(io.BytesIO(body), strict=True)
            if reader.is_encrypted:
                raise AttachmentValidationError(
                    "encrypted PDF attachments are not allowed",
                    code="active_attachment_content",
                )
            # Force the page tree to be read rather than accepting the header alone.
            if len(reader.pages) > MAX_PDF_PAGES:
                raise AttachmentValidationError(
                    "PDF attachment has too many pages",
                    code="attachment_too_complex",
                )
        except AttachmentValidationError:
            raise
        except (PdfReadError, ValueError, TypeError, OSError) as exc:
            raise AttachmentValidationError(
                "PDF attachment is not safely parseable",
                code="attachment_signature_mismatch",
            ) from exc
    elif suffix in {".docx", ".xlsx", ".xlsm"}:
        _validate_zip_signature(body, suffix)
    else:
        if b"\x00" in body:
            raise AttachmentValidationError(
                "text attachment contains binary data",
                code="attachment_signature_mismatch",
            )
        try:
            text = body.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise AttachmentValidationError(
                "text attachment must be UTF-8",
                code="attachment_signature_mismatch",
            ) from exc
        if any(
            (ord(character) < 32 and character not in {"\t", "\n", "\r"})
            or ord(character) == 127
            for character in text
        ):
            raise AttachmentValidationError(
                "text attachment contains binary control characters",
                code="attachment_signature_mismatch",
            )
    return body


def _object_key(
    *,
    organization_id: str,
    creator_user_id: str,
    conversation_id: str,
    attachment_id: str,
    digest: str,
) -> str:
    return (
        f"organizations/{organization_id}/workflow-assistant/"
        f"temporary/users/{creator_user_id}/conversations/{conversation_id}/"
        f"attachments/{attachment_id}/{digest}"
    )


def _content_disposition(filename: str) -> str:
    ascii_name = "".join(
        character if 32 <= ord(character) < 127 and character not in {'"', '\\'} else "_"
        for character in filename
    )
    return (
        f'attachment; filename="{ascii_name}"; '
        f"filename*=UTF-8''{quote(filename, safe='')}"
    )


class AttachmentService:
    """Temporary upload lifecycle; this service never parses or imports content."""

    def __init__(self, *, repository: AttachmentRepository, store: ObjectStore) -> None:
        self._repository = repository
        self._store = store

    def upload(
        self,
        *,
        actor: ActorIdentity,
        conversation_id: str,
        original_filename: str,
        mime_type: str,
        content: bytes,
        idempotency_key: str,
        proposed_project_id: str | None = None,
        now: datetime | None = None,
        attachment_id: str | None = None,
    ) -> AssistantAttachment:
        organization_id = _scope_id(actor.organization_id, "organization_id")
        creator_user_id = _scope_id(actor.user_id, "creator_user_id")
        conversation_id = _scope_id(conversation_id, "conversation_id")
        project_id = (
            _scope_id(proposed_project_id, "proposed_project_id")
            if proposed_project_id is not None
            else None
        )
        normalized_idempotency_key = _idempotency_key(idempotency_key)
        filename, suffix = _filename(original_filename)
        normalized_mime = _validated_mime(mime_type, suffix)
        body = _validate_content(content, suffix)
        created_at = _aware_utc(now)
        digest = hashlib.sha256(body).hexdigest()
        identity = _scope_id(
            attachment_id or f"asa_{uuid.uuid4().hex}", "attachment_id"
        )
        key = _object_key(
            organization_id=organization_id,
            creator_user_id=creator_user_id,
            conversation_id=conversation_id,
            attachment_id=identity,
            digest=digest,
        )
        attachment = AssistantAttachment(
            attachment_id=identity,
            organization_id=organization_id,
            creator_user_id=creator_user_id,
            conversation_id=conversation_id,
            proposed_project_id=project_id,
            plan_id=None,
            idempotency_key=normalized_idempotency_key,
            object_key=key,
            original_filename=filename,
            mime_type=normalized_mime,
            byte_size=len(body),
            sha256=digest,
            classification=None,
            classification_payload={},
            revision=0,
            status="uploading",
            expires_at=created_at + ATTACHMENT_RETENTION,
            created_at=created_at,
            updated_at=created_at,
        )
        reservation = self._repository.reserve_upload(attachment)
        attachment = reservation.attachment
        self._assert_actor_scope(attachment, actor, conversation_id)
        if not reservation.should_write_object:
            return attachment
        try:
            stored = self._store.put(
                key=attachment.object_key,
                data=body,
                content_type=normalized_mime,
                metadata={"attachment-id": attachment.attachment_id, "sha256": digest},
            )
            if (
                stored.key != attachment.object_key
                or stored.content_hash != digest
                or stored.byte_size != len(body)
                or stored.content_type.casefold() != normalized_mime
            ):
                raise AttachmentStorageError("attachment object verification failed")
        except ObjectStoreError as exc:
            self._repository.mark_upload_failed(attachment=attachment, now=created_at)
            raise AttachmentStorageError("attachment upload failed") from exc
        except Exception:
            # The DB reservation remains the source of truth even when best-effort
            # compensation fails; the fixed temporary prefix is bucket-lifecycle safe.
            try:
                self._store.delete(attachment.object_key)
            except ObjectStoreError:
                pass
            self._repository.mark_upload_failed(attachment=attachment, now=created_at)
            raise
        # Once the object is verified, never compensate a metadata-finalize error:
        # the reservation makes this object discoverable and safely retryable.
        return self._repository.finalize_upload(
            attachment=attachment,
            now=created_at,
        )

    def get(
        self,
        *,
        actor: ActorIdentity,
        conversation_id: str,
        attachment_id: str,
        now: datetime | None = None,
    ) -> AssistantAttachment:
        record = self._repository.get_for_actor(
            organization_id=actor.organization_id,
            creator_user_id=actor.user_id,
            conversation_id=conversation_id,
            attachment_id=attachment_id,
        )
        if record is None:
            raise AttachmentNotFound("attachment not found")
        self._assert_actor_scope(record, actor, conversation_id)
        if record.status in {"rejected", "expired"} or record.expires_at <= _aware_utc(now):
            raise AttachmentNotFound("attachment not found")
        return record

    def list(
        self,
        *,
        actor: ActorIdentity,
        conversation_id: str,
        limit: int = 100,
        now: datetime | None = None,
    ) -> tuple[AssistantAttachment, ...]:
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        cutoff = _aware_utc(now)
        records = self._repository.list_for_actor(
            organization_id=actor.organization_id,
            creator_user_id=actor.user_id,
            conversation_id=conversation_id,
            limit=limit,
        )
        for record in records:
            self._assert_actor_scope(record, actor, conversation_id)
        return tuple(
            record
            for record in records
            if record.status not in {"rejected", "expired"} and record.expires_at > cutoff
        )

    def create_download(
        self,
        *,
        actor: ActorIdentity,
        conversation_id: str,
        attachment_id: str,
        now: datetime | None = None,
        expires_seconds: int = DOWNLOAD_URL_TTL_SECONDS,
    ) -> AttachmentDownload:
        if not 1 <= expires_seconds <= 3600:
            raise ValueError("expires_seconds must be between 1 and 3600")
        attachment = self.get(
            actor=actor,
            conversation_id=conversation_id,
            attachment_id=attachment_id,
            now=now,
        )
        if attachment.status not in {
            "uploaded",
            "classifying",
            "needs_user_choice",
            "proposal_ready",
            "importing",
            "imported",
        }:
            raise AttachmentConflict("attachment object is not available for download")
        try:
            url = self._store.create_download_url(
                attachment.object_key,
                expires_seconds=expires_seconds,
                response_content_type=attachment.mime_type,
                response_content_disposition=_content_disposition(
                    attachment.original_filename
                ),
            )
        except ObjectStoreError as exc:
            raise AttachmentStorageError("attachment download signing failed") from exc
        return AttachmentDownload(attachment, url, expires_seconds)

    def reject(
        self,
        *,
        actor: ActorIdentity,
        conversation_id: str,
        attachment_id: str,
        now: datetime | None = None,
    ) -> AssistantAttachment:
        changed_at = _aware_utc(now)
        record = self._repository.claim_rejection(
            organization_id=actor.organization_id,
            creator_user_id=actor.user_id,
            conversation_id=conversation_id,
            attachment_id=attachment_id,
            now=changed_at,
        )
        self._assert_actor_scope(record, actor, conversation_id)
        try:
            self._store.delete(record.object_key)
        except ObjectStoreError as exc:
            raise AttachmentStorageError("attachment rejection cleanup failed") from exc
        return self._repository.finalize_rejection(
            attachment=record,
            now=changed_at,
        )

    def cleanup_expired(
        self,
        *,
        before: datetime | None = None,
        limit: int = 200,
    ) -> int:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        cutoff = _aware_utc(before)
        attempted: set[str] = set()
        cleaned = 0
        while True:
            records = self._repository.claim_expired(
                before=cutoff,
                limit=limit,
                exclude_attachment_ids=tuple(attempted),
            )
            if not records:
                break
            for record in records:
                attempted.add(record.attachment_id)
                try:
                    self._store.delete(record.object_key)
                except ObjectStoreError:
                    # The expiring claim is deliberately retained for a later run.
                    continue
                terminal = (
                    self._repository.finalize_rejection(attachment=record, now=cutoff)
                    if record.status == "rejecting"
                    else self._repository.finalize_expiry(attachment=record, now=cutoff)
                )
                if terminal is not None:
                    cleaned += 1
        return cleaned

    @staticmethod
    def _assert_actor_scope(
        record: AssistantAttachment,
        actor: ActorIdentity,
        conversation_id: str,
    ) -> None:
        expected_prefix = (
            f"organizations/{actor.organization_id}/workflow-assistant/temporary/"
            f"users/{actor.user_id}/conversations/{conversation_id}/attachments/"
        )
        if (
            record.organization_id != actor.organization_id
            or record.creator_user_id != actor.user_id
            or record.conversation_id != conversation_id
            or not record.object_key.startswith(expected_prefix)
        ):
            raise AttachmentNotFound("attachment not found")


__all__ = [
    "ATTACHMENT_RETENTION",
    "DOWNLOAD_URL_TTL_SECONDS",
    "MAX_ATTACHMENT_BYTES",
    "AssistantAttachment",
    "AttachmentReservation",
    "AttachmentConflict",
    "AttachmentDownload",
    "AttachmentError",
    "AttachmentNotFound",
    "AttachmentRepository",
    "AttachmentService",
    "AttachmentStatus",
    "AttachmentStorageError",
    "AttachmentValidationError",
]
