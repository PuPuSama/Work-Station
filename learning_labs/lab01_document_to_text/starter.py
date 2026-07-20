"""Starter code for Lab 01.

Implement only the TODO named in docs/agent-learning-progress.md.
"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ParsedDocument:
    """A tiny normalized document model used only by this lab."""

    source_id: str
    title: str
    text: str
    metadata: dict[str, Any]


def normalize_text(text: str) -> str:
    """Return text with stable line endings, line spacing, and outer whitespace."""

    if not isinstance(text, str):
        raise TypeError("text must be a string")

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines: list[str] = []
    previous_line_was_blank = False

    for line in normalized.split("\n"):
        stripped_line = line.strip(" \t")
        if not stripped_line:
            if lines and not previous_line_was_blank:
                lines.append("")
            previous_line_was_blank = True
            continue

        lines.append(stripped_line)
        previous_line_was_blank = False

    if lines and lines[-1] == "":
        lines.pop()

    return "\n".join(lines)


def extract_title(text: str, source_id: str) -> str:
    """Return the first Markdown H1, or fall back to the source filename."""

    # TODO 2: leave this untouched until TODO 1 passes review.
    raise NotImplementedError("TODO 2: implement extract_title")


def parse_text_document(content: str, source_id: str) -> ParsedDocument:
    """Normalize one text document and attach small, deterministic metadata."""

    # TODO 3: leave this untouched until TODO 2 passes review.
    raise NotImplementedError("TODO 3: implement parse_text_document")
