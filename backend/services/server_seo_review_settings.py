from __future__ import annotations

import re
from collections.abc import Iterable

from models import PromptSnapshot, TaskRecord
from services.seo_review import normalized_keywords


class ServerSeoReviewSettingsError(ValueError):
    """SEO Review settings are not safe to persist."""


def apply_server_seo_review_settings(
    task: TaskRecord,
    *,
    primary_keyword: str,
    long_tail_keywords: Iterable[str],
    prompt_selection: str,
    resolved_prompt: PromptSnapshot,
) -> tuple[int, str, int]:
    """Apply bounded settings after the caller resolved a Project Prompt."""

    selection = prompt_selection.strip() or "project_default"
    if resolved_prompt.kind != "review":
        raise ServerSeoReviewSettingsError(
            "SEO review prompt kind is invalid"
        )
    primary = re.sub(r"\s+", " ", primary_keyword).strip()
    if len(primary) > 240:
        raise ServerSeoReviewSettingsError(
            "primary keyword exceeds 240 characters"
        )
    keywords = normalized_keywords(long_tail_keywords)
    if any(len(keyword) > 240 for keyword in keywords):
        raise ServerSeoReviewSettingsError(
            "long-tail keyword exceeds 240 characters"
        )
    task.seo_primary_keyword = primary
    task.seo_long_tail_keywords = keywords
    task.seo_review_prompt_selection = selection
    return len(keywords), resolved_prompt.source, resolved_prompt.version


__all__ = [
    "ServerSeoReviewSettingsError",
    "apply_server_seo_review_settings",
]
