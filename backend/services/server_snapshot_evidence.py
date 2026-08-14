from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Literal, Protocol, cast
from urllib.parse import unquote, urlsplit

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from knowledge_agent.schema import knowledge_sources, source_snapshots
from services.access_control import (
    ActorIdentity,
    ProjectAccessService,
)
from services.object_store import ObjectStore, ObjectStoreError, ObjectTooLarge


# Normalized MinerU output can be moderately larger than the short text shown
# to reviewers. Read a bounded 2 MiB artifact, then keep the existing 64 KiB
# display truncation so catalog previews remain useful without unbounded reads.
MAX_NORMALIZED_PREVIEW_BYTES = 2 * 1024 * 1024
MAX_PREVIEW_CHARACTERS = 64 * 1024
RAW_DOWNLOAD_EXPIRES_SECONDS = 60
RAW_DOWNLOAD_CONTENT_TYPE = "application/octet-stream"
RAW_DOWNLOAD_FILENAME = "snapshot-evidence.bin"
RAW_DOWNLOAD_CONTENT_DISPOSITION = (
    f'attachment; filename="{RAW_DOWNLOAD_FILENAME}"'
)

_SAFE_CONTENT_TYPE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*/"
    r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*(?:\s*;[^\r\n]*)?$"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class SnapshotEvidenceNotFound(LookupError):
    """The requested current or pending Snapshot evidence does not exist."""


class SnapshotEvidenceUnavailable(RuntimeError):
    """Snapshot evidence cannot be safely read without exposing internals."""


@dataclass(frozen=True, slots=True)
class SnapshotEvidenceManifest:
    project_id: str
    source_id: str
    snapshot_id: str
    slot: Literal["current", "pending"]
    raw_available: bool
    raw_content_type: str | None
    raw_byte_size: int | None
    normalized_available: bool
    normalized_content_type: str | None
    normalized_byte_size: int | None
    preview_supported: bool


@dataclass(frozen=True, slots=True)
class SnapshotEvidencePreview:
    project_id: str
    source_id: str
    snapshot_id: str
    slot: Literal["current", "pending"]
    text: str
    truncated: bool
    block_count: int


@dataclass(frozen=True, slots=True)
class SnapshotEvidenceDownload:
    project_id: str
    source_id: str
    snapshot_id: str
    slot: Literal["current", "pending"]
    download_url: str
    expires_seconds: int
    content_type: str
    content_disposition: str
    filename: str


class _ObjectHead(Protocol):
    content_type: str
    byte_size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class _SnapshotRecord:
    project_id: str
    source_id: str
    snapshot_id: str
    slot: Literal["current", "pending"]
    raw_uri: str | None
    normalized_uri: str | None


@dataclass(frozen=True, slots=True)
class _ArtifactRecord:
    key: str
    content_type: str
    byte_size: int
    sha256: str


def _required_text(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized


def _safe_content_type(value: object) -> str:
    candidate = str(value or "").strip()
    if (
        not candidate
        or len(candidate) > 255
        or not _SAFE_CONTENT_TYPE.fullmatch(candidate)
    ):
        return RAW_DOWNLOAD_CONTENT_TYPE
    return candidate.partition(";")[0].strip().lower()


def _is_json_content_type(value: str) -> bool:
    media_type = value.partition(";")[0].strip().lower()
    return media_type == "application/json" or media_type.endswith("+json")


def _preview_table(
    table_id: str,
    rows: list[tuple[tuple[str, ...], bool]],
    headers: tuple[str, ...] = (),
) -> str:
    if not rows:
        return ""
    width = max(len(cells) for cells, _ in rows)

    def padded(cells: tuple[str, ...]) -> tuple[str, ...]:
        return cells + ("",) * (width - len(cells))

    def line(cells: tuple[str, ...]) -> str:
        escaped = tuple(value.replace("|", "\\|") for value in padded(cells))
        return "| " + " | ".join(escaped) + " |"

    header_rows = [cells for cells, is_header in rows if is_header]
    header = header_rows[-1] if header_rows else headers or rows[0][0]
    if header_rows:
        data_rows = [cells for cells, is_header in rows if not is_header]
    elif headers and tuple(rows[0][0]) != tuple(headers):
        # The inferred headers describe a two-level matrix; retain the
        # original grouping row so the preview still shows the source table.
        data_rows = [cells for cells, _ in rows]
    else:
        data_rows = [cells for cells, _ in rows[1:]]
    rendered = [f"[Table {table_id}]", line(header)]
    rendered.append(line(tuple("---" for _ in range(width))))
    rendered.extend(line(cells) for cells in data_rows)
    return "\n".join(rendered)


def _block_texts(payload: object) -> tuple[str, tuple[str, ...]]:
    if not isinstance(payload, dict):
        raise SnapshotEvidenceUnavailable(
            "Snapshot evidence preview is temporarily unavailable."
        )
    title_value = payload.get("title")
    blocks_value = payload.get("blocks")
    page = payload.get("page")
    if isinstance(page, dict):
        if not isinstance(title_value, str):
            title_value = page.get("title")
        if not isinstance(blocks_value, list):
            blocks_value = page.get("blocks")
    if blocks_value is None:
        blocks_value = []
    if not isinstance(blocks_value, list):
        raise SnapshotEvidenceUnavailable(
            "Snapshot evidence preview is temporarily unavailable."
        )
    title = title_value.strip() if isinstance(title_value, str) else ""
    texts: list[str] = []
    table_id: str | None = None
    table_rows: list[tuple[tuple[str, ...], bool]] = []
    table_headers: tuple[str, ...] = ()

    def flush_table() -> None:
        nonlocal table_id, table_rows, table_headers
        if table_id is not None:
            rendered = _preview_table(table_id, table_rows, table_headers)
            if rendered:
                texts.append(rendered)
        table_id = None
        table_rows = []
        table_headers = ()

    for block in blocks_value:
        if not isinstance(block, dict):
            continue
        metadata = block.get("metadata")
        cells = metadata.get("table_cells") if isinstance(metadata, dict) else None
        if block.get("kind") == "table_row" and isinstance(cells, list):
            current_table_id = (
                str(metadata.get("table_id") or "table")
                if isinstance(metadata, dict)
                else "table"
            )
            if table_id != current_table_id:
                flush_table()
                table_id = current_table_id
            if not table_headers and isinstance(metadata, dict):
                raw_headers = metadata.get("table_headers")
                if isinstance(raw_headers, list):
                    table_headers = tuple(str(cell or "") for cell in raw_headers)
            table_rows.append(
                (
                    tuple(str(cell or "") for cell in cells),
                    bool(
                        metadata.get("table_is_header")
                        if isinstance(metadata, dict)
                        else False
                    ),
                )
            )
            continue
        flush_table()
        text = block.get("text")
        if isinstance(text, str) and text.strip():
            texts.append(text.strip())
    flush_table()
    if not title and not texts:
        raise SnapshotEvidenceUnavailable(
            "Snapshot evidence preview is temporarily unavailable."
        )
    return title, tuple(texts)


class PostgresServerSnapshotEvidenceService:
    """Authorize and expose only exact current/pending Snapshot evidence."""

    def __init__(
        self,
        *,
        engine: Engine,
        store: ObjectStore,
        bucket: str,
        access: ProjectAccessService,
    ) -> None:
        self._engine = engine
        self._store = store
        self._bucket = _required_text(bucket, "bucket")
        self._access = access

    def get_manifest(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        source_id: str,
        snapshot_id: str,
    ) -> SnapshotEvidenceManifest:
        snapshot = self._authorized_snapshot(
            actor=actor,
            project_id=project_id,
            source_id=source_id,
            snapshot_id=snapshot_id,
        )
        raw = self._artifact(actor, snapshot.project_id, snapshot.raw_uri)
        normalized = self._artifact(
            actor,
            snapshot.project_id,
            snapshot.normalized_uri,
        )
        return SnapshotEvidenceManifest(
            project_id=snapshot.project_id,
            source_id=snapshot.source_id,
            snapshot_id=snapshot.snapshot_id,
            slot=snapshot.slot,
            raw_available=raw is not None,
            raw_content_type=None if raw is None else raw.content_type,
            raw_byte_size=None if raw is None else raw.byte_size,
            normalized_available=normalized is not None,
            normalized_content_type=(
                None if normalized is None else normalized.content_type
            ),
            normalized_byte_size=(
                None if normalized is None else normalized.byte_size
            ),
            preview_supported=(
                normalized is not None
                and normalized.byte_size <= MAX_NORMALIZED_PREVIEW_BYTES
                and _is_json_content_type(normalized.content_type)
            ),
        )

    def get_preview(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        source_id: str,
        snapshot_id: str,
    ) -> SnapshotEvidencePreview:
        snapshot = self._authorized_snapshot(
            actor=actor,
            project_id=project_id,
            source_id=source_id,
            snapshot_id=snapshot_id,
        )
        normalized = self._artifact(
            actor,
            snapshot.project_id,
            snapshot.normalized_uri,
        )
        if normalized is None:
            raise SnapshotEvidenceNotFound("Snapshot evidence was not found.")
        if (
            normalized.byte_size > MAX_NORMALIZED_PREVIEW_BYTES
            or not _is_json_content_type(normalized.content_type)
        ):
            raise SnapshotEvidenceUnavailable(
                "Snapshot evidence preview is temporarily unavailable."
            )
        # Reauthorize immediately before returning private object bytes. The
        # route dependency and the earlier SQL lookup are not trusted as a
        # durable permission decision.
        self._access.require(actor, snapshot.project_id, "project.view")
        try:
            body = self._store.get(
                normalized.key,
                max_bytes=MAX_NORMALIZED_PREVIEW_BYTES,
            )
        except (ObjectStoreError, ObjectTooLarge) as exc:
            raise SnapshotEvidenceUnavailable(
                "Snapshot evidence preview is temporarily unavailable."
            ) from exc
        if hashlib.sha256(body).hexdigest() != normalized.sha256:
            raise SnapshotEvidenceUnavailable(
                "Snapshot evidence preview is temporarily unavailable."
            )
        try:
            payload = json.loads(body.decode("utf-8"))
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            RecursionError,
        ) as exc:
            raise SnapshotEvidenceUnavailable(
                "Snapshot evidence preview is temporarily unavailable."
            ) from exc
        title, blocks = _block_texts(payload)
        text = "\n\n".join(part for part in (title, *blocks) if part)
        truncated = len(text) > MAX_PREVIEW_CHARACTERS
        if truncated:
            text = text[:MAX_PREVIEW_CHARACTERS].rstrip()
        return SnapshotEvidencePreview(
            project_id=snapshot.project_id,
            source_id=snapshot.source_id,
            snapshot_id=snapshot.snapshot_id,
            slot=snapshot.slot,
            text=text,
            truncated=truncated,
            block_count=len(blocks),
        )

    def create_raw_download(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        source_id: str,
        snapshot_id: str,
    ) -> SnapshotEvidenceDownload:
        snapshot = self._authorized_snapshot(
            actor=actor,
            project_id=project_id,
            source_id=source_id,
            snapshot_id=snapshot_id,
        )
        raw = self._artifact(actor, snapshot.project_id, snapshot.raw_uri)
        if raw is None:
            raise SnapshotEvidenceNotFound("Snapshot evidence was not found.")
        self._access.require(actor, snapshot.project_id, "project.view")
        try:
            url = self._store.create_download_url(
                raw.key,
                expires_seconds=RAW_DOWNLOAD_EXPIRES_SECONDS,
                response_content_type=RAW_DOWNLOAD_CONTENT_TYPE,
                response_content_disposition=RAW_DOWNLOAD_CONTENT_DISPOSITION,
            )
        except ObjectStoreError as exc:
            raise SnapshotEvidenceUnavailable(
                "Snapshot evidence download is temporarily unavailable."
            ) from exc
        normalized_url = str(url).strip()
        parsed_url = urlsplit(normalized_url)
        if (
            parsed_url.scheme not in {"http", "https"}
            or not parsed_url.hostname
            or parsed_url.username is not None
            or parsed_url.password is not None
            or bool(parsed_url.fragment)
        ):
            raise SnapshotEvidenceUnavailable(
                "Snapshot evidence download is temporarily unavailable."
            )
        return SnapshotEvidenceDownload(
            project_id=snapshot.project_id,
            source_id=snapshot.source_id,
            snapshot_id=snapshot.snapshot_id,
            slot=snapshot.slot,
            download_url=normalized_url,
            expires_seconds=RAW_DOWNLOAD_EXPIRES_SECONDS,
            content_type=RAW_DOWNLOAD_CONTENT_TYPE,
            content_disposition=RAW_DOWNLOAD_CONTENT_DISPOSITION,
            filename=RAW_DOWNLOAD_FILENAME,
        )

    def _authorized_snapshot(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        source_id: str,
        snapshot_id: str,
    ) -> _SnapshotRecord:
        normalized_project_id = _required_text(project_id, "project_id")
        normalized_source_id = _required_text(source_id, "source_id")
        normalized_snapshot_id = _required_text(snapshot_id, "snapshot_id")
        self._access.require(actor, normalized_project_id, "project.view")
        statement = (
            sa.select(
                knowledge_sources.c.status,
                knowledge_sources.c.current_snapshot_id,
                knowledge_sources.c.pending_snapshot_id,
                source_snapshots.c.raw_artifact_uri,
                source_snapshots.c.normalized_artifact_uri,
            )
            .select_from(
                knowledge_sources.join(
                    source_snapshots,
                    sa.and_(
                        source_snapshots.c.project_id
                        == knowledge_sources.c.project_id,
                        source_snapshots.c.source_id
                        == knowledge_sources.c.source_id,
                    ),
                )
            )
            .where(
                knowledge_sources.c.project_id == normalized_project_id,
                knowledge_sources.c.source_id == normalized_source_id,
                source_snapshots.c.snapshot_id == normalized_snapshot_id,
            )
        )
        try:
            with self._engine.connect() as connection:
                row = connection.execute(statement).mappings().one_or_none()
        except SQLAlchemyError as exc:
            raise SnapshotEvidenceUnavailable(
                "Snapshot evidence is temporarily unavailable."
            ) from exc
        if row is None:
            raise SnapshotEvidenceNotFound("Snapshot evidence was not found.")
        if (
            row["status"] != "rejected"
            and row["pending_snapshot_id"] == normalized_snapshot_id
        ):
            slot: Literal["current", "pending"] = "pending"
        elif (
            row["status"] == "published"
            and row["current_snapshot_id"] == normalized_snapshot_id
        ):
            slot = "current"
        else:
            raise SnapshotEvidenceNotFound("Snapshot evidence was not found.")
        return _SnapshotRecord(
            project_id=normalized_project_id,
            source_id=normalized_source_id,
            snapshot_id=normalized_snapshot_id,
            slot=slot,
            raw_uri=(
                None
                if row["raw_artifact_uri"] is None
                else str(row["raw_artifact_uri"])
            ),
            normalized_uri=(
                None
                if row["normalized_artifact_uri"] is None
                else str(row["normalized_artifact_uri"])
            ),
        )

    def _artifact(
        self,
        actor: ActorIdentity,
        project_id: str,
        uri: str | None,
    ) -> _ArtifactRecord | None:
        if uri is None:
            return None
        key = self._scoped_key(actor, project_id, uri)
        try:
            head = cast(_ObjectHead, self._store.head(key))
            byte_size = int(head.byte_size)
            sha256 = str(head.sha256).strip().lower()
        except (ObjectStoreError, TypeError, ValueError, AttributeError) as exc:
            raise SnapshotEvidenceUnavailable(
                "Snapshot evidence is temporarily unavailable."
            ) from exc
        if byte_size < 0 or not _SHA256.fullmatch(sha256):
            raise SnapshotEvidenceUnavailable(
                "Snapshot evidence is temporarily unavailable."
            )
        return _ArtifactRecord(
            key=key,
            content_type=_safe_content_type(head.content_type),
            byte_size=byte_size,
            sha256=sha256,
        )

    def _scoped_key(
        self,
        actor: ActorIdentity,
        project_id: str,
        uri: str,
    ) -> str:
        parsed = urlsplit(uri)
        if (
            parsed.scheme != "s3"
            or parsed.netloc != self._bucket
            or parsed.query
            or parsed.fragment
            or not parsed.path.startswith("/")
        ):
            raise SnapshotEvidenceUnavailable(
                "Snapshot evidence is temporarily unavailable."
            )
        key = unquote(parsed.path[1:])
        segments = key.split("/")
        if (
            not key
            or "\\" in key
            or any(segment in {"", ".", ".."} for segment in segments)
            or any(ord(character) < 32 for character in key)
        ):
            raise SnapshotEvidenceUnavailable(
                "Snapshot evidence is temporarily unavailable."
            )
        expected_prefix = (
            f"organizations/{actor.organization_id}/projects/{project_id}/"
        )
        if not key.startswith(expected_prefix):
            raise SnapshotEvidenceUnavailable(
                "Snapshot evidence is temporarily unavailable."
            )
        return key


__all__ = [
    "MAX_NORMALIZED_PREVIEW_BYTES",
    "MAX_PREVIEW_CHARACTERS",
    "PostgresServerSnapshotEvidenceService",
    "RAW_DOWNLOAD_CONTENT_DISPOSITION",
    "RAW_DOWNLOAD_CONTENT_TYPE",
    "RAW_DOWNLOAD_EXPIRES_SECONDS",
    "RAW_DOWNLOAD_FILENAME",
    "SnapshotEvidenceDownload",
    "SnapshotEvidenceManifest",
    "SnapshotEvidenceNotFound",
    "SnapshotEvidencePreview",
    "SnapshotEvidenceUnavailable",
]
