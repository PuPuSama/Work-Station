from __future__ import annotations

from dataclasses import dataclass

from ..contracts import KnowledgeChunk
from .contracts import ParsedBlock, ParsedDocument


def _split_text(text: str, max_characters: int) -> tuple[str, ...]:
    remaining = text.strip()
    parts: list[str] = []
    while len(remaining) > max_characters:
        minimum_break = max_characters * 3 // 5
        break_at = remaining.rfind("\n", minimum_break, max_characters + 1)
        if break_at < minimum_break:
            break_at = remaining.rfind(" ", minimum_break, max_characters + 1)
        if break_at < minimum_break:
            break_at = max_characters
        parts.append(remaining[:break_at].strip())
        remaining = remaining[break_at:].strip()
    if remaining:
        parts.append(remaining)
    return tuple(parts)


@dataclass(frozen=True, slots=True)
class ParsedDocumentChunker:
    """Deterministically map parsed blocks to M1 chunks without provider logic."""

    max_characters: int = 1800

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_characters, bool)
            or not isinstance(self.max_characters, int)
            or self.max_characters < 256
        ):
            raise ValueError("max_characters must be an integer of at least 256")

    def chunk(
        self,
        *,
        project_id: str,
        source_id: str,
        snapshot_id: str,
        document: ParsedDocument,
    ) -> tuple[KnowledgeChunk, ...]:
        chunks: list[KnowledgeChunk] = []
        for block in document.blocks:
            parts = _split_text(block.text, self.max_characters)
            for part_index, text in enumerate(parts):
                locator = dict(block.locator)
                locator.update(
                    {
                        "block_ordinal": block.ordinal,
                        "part_index": part_index,
                        "part_count": len(parts),
                    }
                )
                chunks.append(
                    KnowledgeChunk(
                        project_id=project_id,
                        chunk_id=f"{snapshot_id}:{len(chunks):06d}",
                        source_id=source_id,
                        snapshot_id=snapshot_id,
                        text=text,
                        ordinal=len(chunks),
                        heading_path=block.heading_path,
                        locator=locator,
                        metadata={
                            "parsed_block_kind": block.kind,
                            **dict(block.metadata),
                        },
                    )
                )
        if not chunks:
            raise ValueError("parsed document must contain text before it can be ingested")
        return tuple(chunks)


def block_identity(block: ParsedBlock) -> tuple[object, ...]:
    """Expose the stable fields used by chunking tests and future refactors."""

    return (
        block.kind,
        block.ordinal,
        block.text,
        block.heading_path,
        tuple(sorted(dict(block.locator).items())),
    )
