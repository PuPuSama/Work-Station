#!/usr/bin/env python3
"""Validate Article Agent metadata and emit the seven-cell sheet patch.

This script never opens or edits the spreadsheet. It only reads one JSON file
and writes a deterministic JSON preview to stdout for the browser workflow.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any


class MetadataError(ValueError):
    """Raised when metadata cannot be mapped without guessing."""


def _require_text(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise MetadataError(f"{key} must be a non-empty string")
    return value.strip()


def _require_number(data: dict[str, Any], key: str) -> int | float:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MetadataError(f"{key} must be a number")
    if not 0 <= value <= 100:
        raise MetadataError(f"{key} must be between 0 and 100")
    return value


def _parse_completion_date(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise MetadataError("completion_date must use YYYY-MM-DD") from exc
    return f"{parsed.month}.{parsed.day}"


def _primary_keyword(data: dict[str, Any]) -> str:
    primary = data.get("primary_keyword")
    if isinstance(primary, str) and primary.strip():
        return primary.strip()

    keywords = data.get("keywords")
    if (
        isinstance(keywords, list)
        and len(keywords) == 1
        and isinstance(keywords[0], str)
        and keywords[0].strip()
    ):
        return keywords[0].strip()
    raise MetadataError("primary_keyword is required when keywords contains multiple values")


def _keyword_occurrences(data: dict[str, Any], primary: str) -> int | float:
    entries = data.get("keyword_density")
    if not isinstance(entries, list):
        raise MetadataError("keyword_density must be an array")

    matches = [entry for entry in entries if isinstance(entry, dict) and entry.get("keyword") == primary]
    if len(matches) != 1:
        raise MetadataError("keyword_density must contain exactly one entry for primary_keyword")

    occurrences = matches[0].get("occurrences")
    if isinstance(occurrences, bool) or not isinstance(occurrences, int) or occurrences < 0:
        raise MetadataError("the matching keyword_density entry needs non-negative integer occurrences")
    return occurrences


def _anchor_text(data: dict[str, Any]) -> str:
    values = data.get("anchor_text")
    if not isinstance(values, list):
        raise MetadataError("anchor_text must be an array")
    if any(not isinstance(value, str) for value in values):
        raise MetadataError("anchor_text entries must be strings")
    cleaned = [value.strip() for value in values if isinstance(value, str) and value.strip()]
    if not cleaned:
        raise MetadataError("anchor_text must contain at least one non-empty text")
    return "\n".join(cleaned)


def build_patch(data: dict[str, Any], source_path: Path) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise MetadataError("metadata root must be an object")

    project_id = _require_text(data, "project_id")
    title = _require_text(data, "title")
    completion_date = _require_text(data, "completion_date")
    primary = _primary_keyword(data)
    occurrences = _keyword_occurrences(data, primary)

    if data.get("ai_rate_confirmed") is not True:
        raise MetadataError("ai_rate_confirmed must be true before writing AI%")
    ai_rate = _require_number(data, "ai_rate_percent")

    if data.get("knowledge_base_citation_status") != "available":
        raise MetadataError("knowledge_base_citation_status must be available before writing citation rate")
    citation_rate = _require_number(data, "knowledge_base_citation_rate_percent")

    return {
        "source_path": str(source_path),
        "project_id": project_id,
        "match_title": title,
        "cells": {
            "A": {"header": "撰写日期", "value": _parse_completion_date(completion_date)},
            "D": {"header": "关键词", "value": primary},
            "E": {"header": "关键词密度", "value": occurrences},
            "G": {"header": "标题", "value": title},
            "H": {"header": "AI%", "value": ai_rate},
            "I": {"header": "锚文本", "value": _anchor_text(data)},
            "K": {"header": "知识库引用率", "value": citation_rate},
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("metadata", type=Path, help="path to metadata.json")
    args = parser.parse_args()

    try:
        source_path = args.metadata.resolve(strict=True)
        with source_path.open("r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
        patch = build_patch(data, source_path)
    except (OSError, UnicodeError, json.JSONDecodeError, MetadataError) as exc:
        print(f"metadata_to_patch: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(patch, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
