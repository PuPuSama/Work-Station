from __future__ import annotations

import math

from models import (
    STATUS_DRAFT_READY,
    STATUS_FINAL_AI_CHECKED,
    STATUS_HUMANIZED_READY,
    STATUS_INITIAL_AI_CHECKED,
    AICheck,
    TaskRecord,
)
from services.article_validation import visible_word_count
from storage import content_hash, now_iso
from workflow.state_machine import transition_task


def _initial_article_is_current(task: TaskRecord) -> bool:
    initial = task.initial_article.strip()
    if not initial:
        return False
    current_hash = content_hash(initial)
    if task.initial_article_hash.strip() and task.initial_article_hash != current_hash:
        return False
    return task.initial_ai_check.article_hash.strip() == current_hash


def _below_threshold(task: TaskRecord, threshold: float) -> bool:
    score = task.initial_ai_check.score
    if score is None:
        return False
    try:
        normalized_threshold = float(threshold)
        normalized_score = float(score)
    except (TypeError, ValueError):
        return False
    return (
        math.isfinite(normalized_threshold)
        and math.isfinite(normalized_score)
        and normalized_score < normalized_threshold
    )


def apply_ai_rate_humanization_skip(
    task: TaskRecord,
    *,
    threshold: float,
    automatic: bool = False,
) -> bool:
    """Reuse the initial article when its checked AI rate is below threshold.

    automatic=True is reserved for the Workflow Assistant path. The manual
    HTTP confirmation path still requires an explicit user-confirmed initial
    check. In both cases the exact article hash must match the detector result,
    and the normal Task state machine/CAS writer remains the persistence
    boundary.
    """

    if not _initial_article_is_current(task) or not _below_threshold(task, threshold):
        return False
    if not automatic and not task.initial_ai_check.confirmed:
        return False
    if task.humanization_skipped and task.status in {
        STATUS_HUMANIZED_READY,
        STATUS_FINAL_AI_CHECKED,
    }:
        return True
    if task.status == STATUS_DRAFT_READY:
        task.initial_ai_check = task.initial_ai_check.model_copy(
            update={
                "confirmed": True,
                "deferred": False,
                "confirmed_at": now_iso(),
            }
        )
        transition_task(task, STATUS_INITIAL_AI_CHECKED)
    elif task.status == STATUS_INITIAL_AI_CHECKED:
        if automatic and not task.initial_ai_check.confirmed:
            task.initial_ai_check = task.initial_ai_check.model_copy(
                update={
                    "confirmed": True,
                    "deferred": False,
                    "confirmed_at": now_iso(),
                }
            )
    else:
        return False

    initial = task.initial_article.strip()
    initial_hash = content_hash(initial)
    score = float(task.initial_ai_check.score)
    threshold_value = float(threshold)
    report = (
        f"Initial AI rate {score:g}% was below the {threshold_value:g}% "
        "threshold; humanization and the second AI check were skipped."
    )
    task.humanized_article = initial
    task.humanized_article_word_count = (
        task.initial_article_word_count or visible_word_count(initial)
    )
    task.humanized_article_hash = initial_hash
    task.humanization_skipped = True
    task.article = initial
    task.zero_gpt_report = report
    task.final_ai_check = AICheck(
        confirmed=True,
        deferred=False,
        score=score,
        report=report,
        provider=task.initial_ai_check.provider,
        checked_at=task.initial_ai_check.checked_at,
        confirmed_at=task.initial_ai_check.confirmed_at or now_iso(),
        article_hash=initial_hash,
    )
    transition_task(task, STATUS_HUMANIZED_READY)
    transition_task(task, STATUS_FINAL_AI_CHECKED)
    return True


__all__ = ["apply_ai_rate_humanization_skip"]
