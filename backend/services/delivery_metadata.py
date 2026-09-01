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
DELIVERY_METADATA_SCHEMA_VERSION = 2
PRIMARY_KEYWORD_MIN_EXACT_OCCURRENCES = 3
_APPROXIMATE_MAX_GAP = 3
_APPROXIMATE_WORD_PATTERN = re.compile(
    r"[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)*",
    flags=re.IGNORECASE,
)
_APPROXIMATE_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "can",
        "does",
        "do",
        "for",
        "from",
        "how",
        "in",
        "into",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "their",
        "these",
        "this",
        "those",
        "to",
        "we",
        "when",
        "where",
        "which",
        "why",
        "with",
        "what",
        "you",
        "your",
    }
)
_APPROXIMATE_CANONICAL_TOKENS = {
    "bottle": "bottle",
    "bottles": "bottle",
    "blow": "blow",
    "blown": "blow",
    "blowing": "blow",
    "blows": "blow",
    "cavity": "cavity",
    "cavities": "cavity",
    "cooled": "cool",
    "cooling": "cool",
    "cools": "cool",
    "cool": "cool",
    "component": "component",
    "components": "component",
    "injected": "inject",
    "injecting": "inject",
    "injection": "inject",
    "inject": "inject",
    "manufacture": "manufacture",
    "manufactured": "manufacture",
    "manufactures": "manufacture",
    "manufacturing": "manufacture",
    "produce": "manufacture",
    "produced": "manufacture",
    "producing": "manufacture",
    "production": "manufacture",
    "mold": "mold",
    "molded": "mold",
    "molding": "mold",
    "molds": "mold",
    "mould": "mold",
    "moulded": "mold",
    "moulding": "mold",
    "moulds": "mold",
    "preform": "preform",
    "preforms": "preform",
    "process": "process",
    "processed": "process",
    "processes": "process",
    "processing": "process",
    "system": "system",
    "systems": "system",
}


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


def _canonical_approximate_token(value: str) -> str:
    token = value.casefold().strip("-'\"")
    if not token:
        return ""
    mapped = _APPROXIMATE_CANONICAL_TOKENS.get(token)
    if mapped:
        return mapped
    if len(token) > 5 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 5 and token.endswith("ing"):
        base = token[:-3]
        if len(base) > 2 and base[-1] == base[-2]:
            base = base[:-1]
        return base
    if len(token) > 4 and token.endswith("ed"):
        return token[:-2]
    if len(token) > 4 and token.endswith("es"):
        return token[:-2]
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _approximate_tokens(value: str, *, drop_stopwords: bool) -> list[str]:
    tokens: list[str] = []
    for raw_token in _APPROXIMATE_WORD_PATTERN.findall(value or ""):
        parts = re.split(r"[-']", raw_token)
        for part in parts:
            token = _canonical_approximate_token(part)
            if not token or (drop_stopwords and token in _APPROXIMATE_STOPWORDS):
                continue
            tokens.append(token)
    return tokens


def _approximate_keyword_occurrences(article: str, keyword: str) -> int:
    target = _approximate_tokens(keyword, drop_stopwords=True)
    if not target:
        return 0
    article_tokens = _approximate_tokens(article, drop_stopwords=False)
    if not article_tokens:
        return 0

    occurrences = 0
    for start, token in enumerate(article_tokens):
        if token != target[0]:
            continue
        cursor = start
        matched = True
        for expected in target[1:]:
            end = min(
                len(article_tokens),
                cursor + _APPROXIMATE_MAX_GAP + 2,
            )
            next_index = next(
                (
                    index
                    for index in range(cursor + 1, end)
                    if article_tokens[index] == expected
                ),
                None,
            )
            if next_index is None:
                matched = False
                break
            cursor = next_index
        if matched:
            occurrences += 1
    return occurrences


def _keyword_density(
    article: str,
    keywords: list[str],
    *,
    primary_keyword: str = "",
) -> tuple[int, list[dict[str, Any]]]:
    visible_article = visible_markdown_text(article)
    body_word_count = visible_word_count(article)
    primary_key = (primary_keyword or (keywords[0] if keywords else "")).casefold()
    items: list[dict[str, Any]] = []
    for keyword in keywords:
        exact_occurrences = len(_keyword_pattern(keyword).findall(visible_article))
        approximate_occurrences = 0
        match_mode = "exact"
        if (
            keyword.casefold() == primary_key
            and exact_occurrences < PRIMARY_KEYWORD_MIN_EXACT_OCCURRENCES
        ):
            approximate_occurrences = _approximate_keyword_occurrences(
                visible_article,
                keyword,
            )
            match_mode = "approximate"
        occurrences = max(exact_occurrences, approximate_occurrences)
        density_percent = (
            round(occurrences / body_word_count * 100, 4)
            if body_word_count
            else 0.0
        )
        items.append(
            {
                "keyword": keyword,
                "occurrences": occurrences,
                "exact_occurrences": exact_occurrences,
                "approximate_occurrences": approximate_occurrences,
                "match_mode": match_mode,
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
        "primary_keyword": keywords[0] if keywords else "",
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
    "PRIMARY_KEYWORD_MIN_EXACT_OCCURRENCES",
    "build_delivery_metadata",
]
