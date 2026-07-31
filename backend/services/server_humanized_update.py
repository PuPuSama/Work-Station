from __future__ import annotations

from models import ArticleVersion, TaskRecord
from services.article_validation import (
    ArticleStructureError,
    validate_humanized_article,
    visible_word_count,
)
from storage import content_hash, now_iso
from workflow.state_machine import invalidate_downstream


class ServerHumanizedArticleError(ValueError):
    """The reviewed humanized article cannot safely replace the draft."""


def apply_reviewed_humanized_article(
    task: TaskRecord,
    *,
    article: str,
) -> str:
    """Validate and store external humanized copy without local artifacts."""

    source = (task.initial_article or task.article).strip()
    if not source:
        raise ServerHumanizedArticleError(
            "the initial article is empty"
        )
    candidate = article.strip()
    if not candidate:
        raise ServerHumanizedArticleError(
            "the humanized article cannot be empty"
        )
    required_phrases = [task.competitor_keyword or task.topic]
    required_phrases.extend(
        product.name for product in task.products if product.name
    )
    try:
        validate_humanized_article(
            source,
            candidate,
            required_phrases=required_phrases,
        )
    except ArticleStructureError as exc:
        raise ServerHumanizedArticleError(str(exc)) from exc

    task.humanized_article = candidate
    task.humanization_skipped = False
    task.humanized_article_word_count = visible_word_count(candidate)
    task.humanized_article_hash = content_hash(candidate)
    task.article = candidate
    invalidate_downstream(task, "humanized_article")
    task.article_versions.append(
        ArticleVersion(
            kind="humanized",
            content=candidate,
            word_count=task.humanized_article_word_count,
            content_hash=task.humanized_article_hash,
            created_at=now_iso(),
            source_kind="external_manual",
        )
    )
    return candidate


__all__ = [
    "ServerHumanizedArticleError",
    "apply_reviewed_humanized_article",
]
