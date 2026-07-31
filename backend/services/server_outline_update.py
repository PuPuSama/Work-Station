from __future__ import annotations

from typing import Literal

from models import (
    STATUS_OUTLINE_CONFIRMED,
    ArticleVersion,
    TaskRecord,
)
from services.article_validation import visible_word_count
from storage import content_hash, now_iso
from workflow.state_machine import invalidate_downstream, transition_task


class ServerOutlineUpdateError(ValueError):
    """The reviewed outline cannot be applied to the Task."""


class ServerOutlineVersionNotFound(LookupError):
    """The requested version index does not exist on the scoped Task."""


def _append_outline_version(
    task: TaskRecord,
    *,
    kind: Literal["outline", "outline_draft"],
    content: str,
    source_kind: Literal[
        "manual_confirmed",
        "manual_draft",
        "restored",
    ],
) -> None:
    """Append a reviewed snapshot without duplicating the latest version."""

    record = ArticleVersion(
        kind=kind,
        content=content,
        word_count=visible_word_count(content),
        content_hash=content_hash(content),
        created_at=now_iso(),
        source_kind=source_kind,
    )
    if task.article_versions:
        latest = task.article_versions[-1]
        if (
            latest.kind == record.kind
            and latest.content_hash == record.content_hash
            and latest.source_kind == record.source_kind
        ):
            return
    task.article_versions.append(record)


def apply_reviewed_outline(
    task: TaskRecord,
    *,
    outline: str,
    confirmed: bool,
) -> str:
    """Apply a reviewed draft or confirmation to an authorized Task."""

    normalized = outline.strip()
    if not normalized:
        raise ServerOutlineUpdateError("outline cannot be empty")
    task.outline_draft = normalized
    version_kind: Literal["outline", "outline_draft"] = (
        "outline" if confirmed else "outline_draft"
    )
    source_kind: Literal["manual_confirmed", "manual_draft"] = (
        "manual_confirmed" if confirmed else "manual_draft"
    )
    _append_outline_version(
        task,
        kind=version_kind,
        content=normalized,
        source_kind=source_kind,
    )
    if confirmed:
        task.outline = normalized
        invalidate_downstream(task, "outline")
        transition_task(task, STATUS_OUTLINE_CONFIRMED)
    return normalized


def restore_reviewed_outline_version(
    task: TaskRecord,
    *,
    version_index: int,
) -> Literal["outline", "outline_draft"]:
    """Restore one server-owned outline snapshot into the editable draft."""

    if version_index >= len(task.article_versions):
        raise ServerOutlineVersionNotFound(
            "outline version was not found"
        )
    version = task.article_versions[version_index]
    if version.kind not in {"outline", "outline_draft"}:
        raise ServerOutlineUpdateError(
            "selected version is not an outline"
        )
    content = version.content.strip()
    if not content:
        raise ServerOutlineUpdateError(
            "selected outline version is empty"
        )
    task.outline_draft = content
    _append_outline_version(
        task,
        kind="outline_draft",
        content=content,
        source_kind="restored",
    )
    return version.kind
