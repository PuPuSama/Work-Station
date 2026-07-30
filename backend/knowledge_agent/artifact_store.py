from __future__ import annotations

import os
import re
from hashlib import sha256
from pathlib import Path, PurePath
from typing import Protocol, runtime_checkable


class ArtifactStoreError(RuntimeError):
    """Raised when immutable knowledge artifacts cannot be persisted safely."""


def _safe_segment(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} is required")
    normalized = value.strip()
    if (
        not normalized
        or normalized in {".", ".."}
        or not re.fullmatch(r"[A-Za-z0-9._-]+", normalized)
    ):
        raise ValueError(
            f"{field_name} must contain only letters, numbers, dot, dash, or underscore"
        )
    return normalized


def _safe_filename(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("filename is required")
    normalized = value.strip()
    if (
        not normalized
        or PurePath(normalized).name != normalized
        or "/" in normalized
        or "\\" in normalized
    ):
        raise ValueError("filename must not contain a directory path")
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "-", normalized).strip("-.")
    if not sanitized:
        raise ValueError("filename must contain at least one safe character")
    return sanitized


@runtime_checkable
class ArtifactStore(Protocol):
    """Content-addressed persistence boundary for raw, normalized, and image files."""

    def put(
        self,
        *,
        project_id: str,
        namespace: str,
        content_hash: str,
        filename: str,
        content: bytes,
    ) -> str: ...


class LocalKnowledgeArtifactStore:
    """Project-scoped immutable filesystem store used until the M7 S3 adapter."""

    def __init__(self, root: Path) -> None:
        self._root = root.expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    def put(
        self,
        *,
        project_id: str,
        namespace: str,
        content_hash: str,
        filename: str,
        content: bytes,
    ) -> str:
        project_segment = _safe_segment(project_id, "project_id")
        namespace_segment = _safe_segment(namespace, "namespace")
        hash_segment = _safe_segment(content_hash.lower(), "content_hash")
        if len(hash_segment) != 64 or any(
            character not in "0123456789abcdef" for character in hash_segment
        ):
            raise ValueError("content_hash must be a 64-character SHA-256 hex digest")
        filename_segment = _safe_filename(filename)
        if not isinstance(content, bytes) or not content:
            raise ValueError("content must be non-empty bytes")
        if sha256(content).hexdigest() != hash_segment:
            raise ValueError("content_hash does not match content bytes")

        project_root = (self._root / project_segment).resolve()
        destination = (
            project_root
            / namespace_segment
            / hash_segment[:2]
            / hash_segment
            / filename_segment
        ).resolve()
        try:
            destination.relative_to(project_root)
        except ValueError as exc:
            raise ArtifactStoreError("artifact path escaped the project root") from exc
        destination.parent.mkdir(parents=True, exist_ok=True)

        if destination.exists():
            try:
                existing = destination.read_bytes()
            except OSError as exc:
                raise ArtifactStoreError("existing artifact could not be read") from exc
            if existing != content:
                raise ArtifactStoreError(
                    "content-addressed artifact path contains different bytes"
                )
            return destination.as_uri()

        temporary = destination.with_name(
            f".{destination.name}.{os.getpid()}.tmp"
        )
        try:
            with temporary.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                temporary.replace(destination)
            except FileExistsError:
                existing = destination.read_bytes()
                if existing != content:
                    raise ArtifactStoreError(
                        "concurrent artifact write produced different bytes"
                    )
        except FileExistsError:
            if not destination.exists() or destination.read_bytes() != content:
                raise ArtifactStoreError("temporary artifact path is already in use")
        except OSError as exc:
            raise ArtifactStoreError("artifact could not be persisted") from exc
        finally:
            if temporary.exists():
                temporary.unlink()
        return destination.as_uri()
