"""Starter code for Lab 02. Keep locked until Lab 01 passes review."""

from dataclasses import dataclass


@dataclass(frozen=True)
class MarkdownSection:
    heading_path: tuple[str, ...]
    body: str


@dataclass(frozen=True)
class TextChunk:
    chunk_id: str
    source_id: str
    heading_path: tuple[str, ...]
    text: str
    position: int


def split_markdown_sections(text: str) -> list[MarkdownSection]:
    """Split normalized Markdown while preserving the active H1/H2/H3 path."""

    # TODO 1: implement after Lab 01 review.
    raise NotImplementedError("TODO 1: implement split_markdown_sections")


def build_chunks(
    sections: list[MarkdownSection],
    source_id: str,
    max_chars: int,
) -> list[TextChunk]:
    """Pack whole paragraphs into deterministic chunks no larger than max_chars."""

    # TODO 2: implement after split_markdown_sections passes.
    raise NotImplementedError("TODO 2: implement build_chunks")

