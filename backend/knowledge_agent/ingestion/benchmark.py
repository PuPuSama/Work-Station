from __future__ import annotations

import statistics
import time
import tracemalloc
from dataclasses import asdict, dataclass
from typing import Mapping, Protocol, Sequence

from .contracts import DocumentInput, ParsedDocument


class BenchmarkParser(Protocol):
    def parse(self, document_input: DocumentInput) -> ParsedDocument: ...


@dataclass(frozen=True, slots=True)
class ParserQualityExpectation:
    """Human-labelled minimums used to compare parsers on the same document."""

    required_text: tuple[str, ...] = ()
    minimum_blocks: int = 1
    minimum_headings: int = 0
    minimum_table_rows: int = 0
    minimum_assets: int = 0

    def __post_init__(self) -> None:
        for field_name in (
            "minimum_blocks",
            "minimum_headings",
            "minimum_table_rows",
            "minimum_assets",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field_name} must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class ParserBenchmarkObservation:
    parser_name: str
    parser_version: str
    duration_ms_median: float
    peak_python_bytes_max: int
    block_count: int
    text_character_count: int
    heading_count: int
    table_row_count: int
    asset_count: int
    located_page_count: int
    bbox_block_count: int
    required_text_recall: float
    minimums_passed: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ParserBenchmarkReport:
    filename: str
    content_hash: str
    repeat_count: int
    observations: tuple[ParserBenchmarkObservation, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "filename": self.filename,
            "content_hash": self.content_hash,
            "repeat_count": self.repeat_count,
            "observations": [item.to_dict() for item in self.observations],
        }


def _observe(
    *,
    parser: BenchmarkParser,
    document_input: DocumentInput,
    expectation: ParserQualityExpectation,
    repeat_count: int,
) -> ParserBenchmarkObservation:
    durations: list[float] = []
    peaks: list[int] = []
    parsed: ParsedDocument | None = None
    for _index in range(repeat_count):
        tracemalloc.start()
        started = time.perf_counter()
        try:
            current = parser.parse(document_input)
        finally:
            duration = (time.perf_counter() - started) * 1000
            _current_bytes, peak_bytes = tracemalloc.get_traced_memory()
            tracemalloc.stop()
        durations.append(duration)
        peaks.append(peak_bytes)
        if parsed is not None and current != parsed:
            raise ValueError("parser output changed between benchmark repetitions")
        parsed = current
    assert parsed is not None
    text = parsed.text.casefold()
    required = tuple(
        value.strip().casefold()
        for value in expectation.required_text
        if value.strip()
    )
    matched = sum(1 for value in required if value in text)
    recall = 1.0 if not required else matched / len(required)
    headings = sum(block.kind == "heading" for block in parsed.blocks)
    table_rows = sum(block.kind == "table_row" for block in parsed.blocks)
    pages = {
        int(block.locator["page_number"])
        for block in parsed.blocks
        if isinstance(block.locator.get("page_number"), int)
    }
    bbox_count = sum(
        "bbox_0_1000" in block.locator for block in parsed.blocks
    )
    minimums_passed = (
        len(parsed.blocks) >= expectation.minimum_blocks
        and headings >= expectation.minimum_headings
        and table_rows >= expectation.minimum_table_rows
        and len(parsed.assets) >= expectation.minimum_assets
        and recall == 1.0
    )
    return ParserBenchmarkObservation(
        parser_name=parsed.parser_name,
        parser_version=parsed.parser_version,
        duration_ms_median=round(statistics.median(durations), 3),
        peak_python_bytes_max=max(peaks),
        block_count=len(parsed.blocks),
        text_character_count=len(parsed.text),
        heading_count=headings,
        table_row_count=table_rows,
        asset_count=len(parsed.assets),
        located_page_count=len(pages),
        bbox_block_count=bbox_count,
        required_text_recall=round(recall, 4),
        minimums_passed=minimums_passed,
    )


def compare_parsers(
    *,
    document_input: DocumentInput,
    parsers: Mapping[str, BenchmarkParser],
    expectation: ParserQualityExpectation | None = None,
    repeat_count: int = 1,
) -> ParserBenchmarkReport:
    """Run deterministic parsers on the exact same bytes and record comparable facts."""

    if not parsers:
        raise ValueError("parsers must not be empty")
    if repeat_count <= 0:
        raise ValueError("repeat_count must be positive")
    expected = expectation or ParserQualityExpectation()
    observations = tuple(
        _observe(
            parser=parser,
            document_input=document_input,
            expectation=expected,
            repeat_count=repeat_count,
        )
        for _name, parser in sorted(parsers.items())
    )
    return ParserBenchmarkReport(
        filename=document_input.filename,
        content_hash=document_input.content_hash,
        repeat_count=repeat_count,
        observations=observations,
    )


__all__ = [
    "BenchmarkParser",
    "ParserBenchmarkObservation",
    "ParserBenchmarkReport",
    "ParserQualityExpectation",
    "compare_parsers",
]
