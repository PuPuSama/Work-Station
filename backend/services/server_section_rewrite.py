from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from models import ArticleVersion, SourceLink, TaskRecord
from services.article_validation import (
    ArticleStructureError,
    extract_link_inventory,
    has_intro_transition,
    validate_article_layout,
    visible_word_count,
)
from services.generator import (
    ArticleGenerationError,
    PromptTemplateError,
    ensure_article_hyperlinks,
    validate_minimum_h3_per_h2,
)
from storage import content_hash, now_iso
from workflow.state_machine import invalidate_downstream


_HEADING = re.compile(
    r"^(?P<indent>[ \t]{0,3})(?P<marks>#{1,6})[ \t]+"
    r"(?P<title>.*?)(?:[ \t]+#+)?[ \t]*(?:\r?\n)?$"
)
_FENCE = re.compile(
    r"^[ \t]{0,3}(?P<marks>`{3,}|~{3,})"
)


class SectionRewriteError(ValueError):
    """A section command would exceed its target or break the article."""


@dataclass(frozen=True, slots=True)
class _HeadingRecord:
    line_index: int
    level: int
    path: tuple[str, ...]


def _normalized_heading(value: str) -> str:
    return " ".join(str(value).split()).casefold()


def _required_path(values: Sequence[str]) -> tuple[str, ...]:
    path = tuple(_normalized_heading(value) for value in values)
    if not path or any(not value for value in path):
        raise SectionRewriteError("heading_path must not be empty")
    return path


def _headings(lines: Sequence[str]) -> tuple[_HeadingRecord, ...]:
    result: list[_HeadingRecord] = []
    stack: list[tuple[int, str]] = []
    for line_index, level, title in _heading_tokens(lines):
        if level == 1:
            stack.clear()
            continue
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        result.append(
            _HeadingRecord(
                line_index=line_index,
                level=level,
                path=tuple(item[1] for item in stack),
            )
        )
    return tuple(result)


def _heading_tokens(
    lines: Sequence[str],
) -> tuple[tuple[int, int, str], ...]:
    result: list[tuple[int, int, str]] = []
    fence_character = ""
    fence_length = 0
    for line_index, line in enumerate(lines):
        fence = _FENCE.match(line)
        if fence is not None:
            marks = fence.group("marks")
            if not fence_character:
                fence_character = marks[0]
                fence_length = len(marks)
            elif (
                marks[0] == fence_character
                and len(marks) >= fence_length
            ):
                fence_character = ""
                fence_length = 0
            continue
        if fence_character:
            continue
        match = _HEADING.match(line)
        if match is None:
            continue
        level = len(match.group("marks"))
        title = _normalized_heading(match.group("title"))
        if not title:
            continue
        result.append((line_index, level, title))
    return tuple(result)


def _validate_replacement_body(body: str, target_level: int) -> str:
    normalized = body.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        raise SectionRewriteError("replacement_body must not be empty")
    for _, level, _ in _heading_tokens(
        normalized.splitlines(keepends=True)
    ):
        if level <= target_level:
            raise SectionRewriteError(
                "replacement_body cannot introduce a heading at or above "
                "the target section level"
            )
    return normalized


def replace_markdown_section(
    markdown: str,
    *,
    heading_path: Sequence[str],
    replacement_body: str,
) -> str:
    """Replace one section body while preserving its heading and all siblings."""

    if not markdown.strip():
        raise SectionRewriteError("initial article is empty")
    path = _required_path(heading_path)
    lines = markdown.splitlines(keepends=True)
    headings = _headings(lines)
    matches = [heading for heading in headings if heading.path == path]
    if not matches:
        raise SectionRewriteError(
            "target section was not found in the current article"
        )
    if len(matches) != 1:
        raise SectionRewriteError(
            "target section is ambiguous in the current article"
        )
    target = matches[0]
    if target.level < 2:
        raise SectionRewriteError("the article title cannot be rewritten")
    body = _validate_replacement_body(replacement_body, target.level)
    end_index = len(lines)
    for line_index, level, _ in _heading_tokens(lines):
        if (
            line_index > target.line_index
            and level <= target.level
        ):
            end_index = line_index
            break

    prefix = "".join(lines[: target.line_index + 1])
    if prefix and not prefix.endswith(("\n", "\r")):
        prefix += "\n"
    suffix = "".join(lines[end_index:])
    return f"{prefix}\n{body}\n\n{suffix}"


def _version(
    content: str,
    *,
    source_kind: str,
) -> ArticleVersion:
    return ArticleVersion(
        kind="initial",
        content=content,
        word_count=visible_word_count(content),
        content_hash=content_hash(content),
        created_at=now_iso(),
        source_kind=source_kind,
    )


def _source_links(article: str) -> list[SourceLink]:
    return [
        SourceLink.model_validate(item)
        for item in extract_link_inventory(article)
    ]


def rewrite_initial_article_section(
    task: TaskRecord,
    *,
    heading_path: Sequence[str],
    replacement_body: str,
) -> TaskRecord:
    """Snapshot, validate, and apply one deterministic initial-article edit."""

    original = task.initial_article
    candidate = replace_markdown_section(
        original,
        heading_path=heading_path,
        replacement_body=replacement_body,
    )
    try:
        linked = ensure_article_hyperlinks(candidate, task)
        if linked != candidate:
            raise SectionRewriteError(
                "section rewrite would require changes outside the target"
            )
        validate_article_layout(candidate)
        validate_minimum_h3_per_h2(candidate)
        if not has_intro_transition(candidate):
            raise SectionRewriteError(
                "article must keep a transition paragraph between H1 and H2"
            )
    except (
        ArticleStructureError,
        ArticleGenerationError,
        PromptTemplateError,
    ) as exc:
        raise SectionRewriteError(str(exc)) from exc

    before = _version(
        original,
        source_kind="before_section_rewrite",
    )
    after = _version(
        candidate,
        source_kind="section_rewrite",
    )
    task.initial_article = candidate
    task.initial_article_word_count = after.word_count
    task.initial_article_hash = after.content_hash
    task.article = candidate
    invalidate_downstream(task, "initial_article")
    task.source_links = _source_links(candidate)
    task.transition_added = True
    task.article_versions.extend((before, after))
    return task


__all__ = [
    "SectionRewriteError",
    "replace_markdown_section",
    "rewrite_initial_article_section",
]
