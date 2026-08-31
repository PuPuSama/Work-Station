from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

from models import TaskRecord
from services.article_validation import (
    extract_link_inventory,
    visible_markdown_text,
    visible_word_count,
)
from services.tdk import article_title
from storage import now_iso


DELIVERY_METADATA_FILENAME = "metadata.json"
DELIVERY_METADATA_SCHEMA_VERSION = 1


def _unique_keywords(values: Iterable[object]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        keyword = str(value or "").strip()
        key = keyword.casefold()
        if not keyword or key in seen:
            continue
        seen.add(key)
        result.append(keyword)
    return result


def _keyword_pattern(keyword: str) -> re.Pattern[str]:
    normalized = re.sub(r"\s+", " ", keyword.strip())
    parts = [part for part in normalized.split(" ") if part]
    phrase = r"\s+".join(re.escape(part) for part in parts)
    return re.compile(
        rf"(?<![A-Za-z0-9]){phrase}(?![A-Za-z0-9])",
        flags=re.IGNORECASE,
    )


def _keyword_density(article: str, keywords: list[str]) -> tuple[int, list[dict[str, Any]]]:
    visible_article = visible_markdown_text(article)
    body_word_count = visible_word_count(article)
    items: list[dict[str, Any]] = []
    for keyword in keywords:
        occurrences = len(_keyword_pattern(keyword).findall(visible_article))
        density_percent = (
            round(occurrences / body_word_count * 100, 4)
            if body_word_count
            else 0.0
        )
        items.append(
            {
                "keyword": keyword,
                "occurrences": occurrences,
                "density_percent": density_percent,
            }
        )
    return body_word_count, items


def _completion_timestamp(task: TaskRecord) -> str:
    return (
        str(task.manual_completed_at or "").strip()
        or str(task.updated_at or "").strip()
        or now_iso()
    )


def build_delivery_metadata(
    task: TaskRecord,
    *,
    article: str,
    project_id: str,
    delivery_filename: str,
) -> bytes:
    """Build the UTF-8 metadata record shipped with every article package."""

    keywords = _unique_keywords(
        task.tdk.keywords
        or (
            task.seo_primary_keyword,
            *task.seo_long_tail_keywords,
            task.primary_keyword,
        )
    )
    body_word_count, keyword_density = _keyword_density(article, keywords)
    completion_timestamp = _completion_timestamp(task)
    coverage = task.knowledge_coverage
    knowledge_rate = (
        round(coverage.sentence_coverage * 100, 2)
        if coverage.status not in {"not_checked", "unavailable"}
        else None
    )
    anchors = [
        {
            "anchor_text": str(item.get("anchor") or ""),
            "url": str(item.get("url") or ""),
            "occurrences": int(item.get("count") or 0),
            "heading": str(item.get("heading") or ""),
        }
        for item in extract_link_inventory(article)
    ]
    payload: dict[str, Any] = {
        "schema_version": DELIVERY_METADATA_SCHEMA_VERSION,
        "task_id": task.id,
        "project_id": project_id,
        "topic_index": task.topic_index,
        "topic": task.topic,
        "title": article_title(task, article),
        "delivery_filename": delivery_filename,
        "completed_at": completion_timestamp,
        "completion_date": completion_timestamp[:10],
        "body_word_count": body_word_count,
        "keywords": keywords,
        "keyword_density": keyword_density,
        "ai_rate_percent": task.final_ai_check.score,
        "ai_rate_provider": task.final_ai_check.provider,
        "ai_rate_checked_at": task.final_ai_check.checked_at,
        "ai_rate_confirmed": task.final_ai_check.confirmed,
        "knowledge_base_citation_rate_percent": knowledge_rate,
        "knowledge_base_citation_status": coverage.status,
        "knowledge_base_supported_sentences": coverage.supported_sentences,
        "knowledge_base_eligible_sentences": coverage.eligible_sentences,
        "knowledge_base_evidence_link_count": coverage.evidence_link_count,
        "knowledge_base_checked_at": coverage.checked_at,
        "anchor_text": [item["anchor_text"] for item in anchors],
        "anchors": anchors,
    }
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


__all__ = [
    "DELIVERY_METADATA_FILENAME",
    "DELIVERY_METADATA_SCHEMA_VERSION",
    "build_delivery_metadata",
]
