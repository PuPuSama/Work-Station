from __future__ import annotations

from dataclasses import dataclass

from models import (
    ArticleVersion,
    SeoReviewPreview,
    SeoReviewRun,
    SourceLink,
    TaskRecord,
)
from services.article_validation import (
    ArticleStructureError,
    extract_link_inventory,
    has_intro_transition,
    validate_article_layout,
    visible_word_count,
)
from services.generator import (
    ArticleGenerationError,
    PromptTemplateError,
    ensure_article_hyperlinks,
    validate_minimum_h3_per_h2,
)
from services.seo_review import (
    SeoReviewError,
    build_review_candidate,
    update_review_change,
)
from storage import content_hash, now_iso
from workflow.state_machine import invalidate_downstream


class ServerSeoReviewNotFound(KeyError):
    """The requested Review Run or Change does not belong to this Task."""


class ServerSeoReviewConflict(RuntimeError):
    """The Review command is stale or conflicts with its current state."""


class ServerSeoReviewValidationError(ValueError):
    """The requested Review decision cannot produce a valid article."""


@dataclass(frozen=True, slots=True)
class SeoReviewDecisionSummary:
    decision: str
    risk_count: int
    risk_confirmed: bool


@dataclass(frozen=True, slots=True)
class SeoReviewFinalizeSummary:
    accepted_count: int
    pending_count: int
    rejected_count: int
    invalid_count: int


def _review_entry(
    task: TaskRecord,
    review_id: str,
) -> tuple[int, SeoReviewRun]:
    for index, review in enumerate(task.seo_reviews):
        if review.id == review_id:
            return index, review
    raise ServerSeoReviewNotFound(review_id)


def _editable_review(
    task: TaskRecord,
    review_id: str,
) -> tuple[int, SeoReviewRun]:
    index, review = _review_entry(task, review_id)
    if review.status != "open":
        raise ServerSeoReviewConflict("SEO review is already finalized")
    article = task.initial_article.strip()
    if (
        not article
        or content_hash(article) != review.source_article_hash
        or content_hash(review.source_article.strip())
        != review.source_article_hash
    ):
        raise ServerSeoReviewConflict("SEO review source article changed")
    return index, review


def _summary(review: SeoReviewRun) -> SeoReviewFinalizeSummary:
    return SeoReviewFinalizeSummary(
        accepted_count=sum(
            change.decision == "accepted" for change in review.changes
        ),
        pending_count=sum(
            change.decision == "pending" for change in review.changes
        ),
        rejected_count=sum(
            change.decision == "rejected" for change in review.changes
        ),
        invalid_count=sum(
            not change.applicable for change in review.changes
        ),
    )


def update_server_seo_review_change(
    task: TaskRecord,
    *,
    review_id: str,
    change_id: str,
    decision: str,
    reviewed_text: str,
    confirm_risks: bool,
    actor_user_id: str,
) -> SeoReviewDecisionSummary:
    review_index, review = _editable_review(task, review_id)
    change_index = next(
        (
            index
            for index, change in enumerate(review.changes)
            if change.id == change_id
        ),
        -1,
    )
    if change_index < 0:
        raise ServerSeoReviewNotFound(change_id)
    timestamp = now_iso()
    try:
        updated = update_review_change(
            review.changes[change_index],
            reviewed_text=reviewed_text,
            decision=decision,
            brand_name=task.brand_name,
            product_names=[
                product.name for product in task.products if product.name
            ],
            confirm_risks=confirm_risks,
            decided_at=timestamp,
            decided_by=actor_user_id,
        )
    except SeoReviewError as exc:
        raise ServerSeoReviewValidationError(str(exc)) from exc
    review.changes[change_index] = updated
    task.seo_reviews[review_index] = review
    return SeoReviewDecisionSummary(
        decision=updated.decision,
        risk_count=len(updated.risks),
        risk_confirmed=updated.risk_confirmed,
    )


def build_server_seo_review_preview(
    task: TaskRecord,
    *,
    review_id: str,
) -> SeoReviewPreview:
    _index, review = _editable_review(task, review_id)
    try:
        candidate, change_ids = build_review_candidate(review)
        candidate = ensure_article_hyperlinks(candidate, task)
        validate_article_layout(candidate)
        validate_minimum_h3_per_h2(candidate)
        if not has_intro_transition(candidate):
            raise ArticleStructureError(
                "Article must include a transition paragraph "
                "between its H1 and first H2."
            )
    except (
        ArticleGenerationError,
        ArticleStructureError,
        PromptTemplateError,
        SeoReviewError,
    ) as exc:
        raise ServerSeoReviewValidationError(str(exc)) from exc
    return SeoReviewPreview(
        review_id=review.id,
        article=candidate,
        article_hash=content_hash(candidate),
        accepted_change_ids=change_ids,
        pending_count=sum(
            change.decision == "pending" for change in review.changes
        ),
        rejected_count=sum(
            change.decision == "rejected" for change in review.changes
        ),
        invalid_count=sum(
            not change.applicable for change in review.changes
        ),
        structure_valid=True,
    )


def apply_server_seo_review(
    task: TaskRecord,
    *,
    review_id: str,
    preview_hash: str,
    confirm_pending: bool,
    actor_user_id: str,
) -> SeoReviewFinalizeSummary:
    review_index, review = _editable_review(task, review_id)
    summary = _summary(review)
    if not summary.accepted_count:
        raise ServerSeoReviewValidationError(
            "SEO review has no accepted changes"
        )
    if summary.pending_count and not confirm_pending:
        raise ServerSeoReviewConflict(
            "SEO review still has pending changes"
        )
    preview = build_server_seo_review_preview(
        task,
        review_id=review_id,
    )
    if preview_hash != preview.article_hash:
        raise ServerSeoReviewConflict("SEO review preview changed")

    timestamp = now_iso()
    review.status = "applied"
    review.finalized_at = timestamp
    review.finalized_by = actor_user_id
    review.applied_article_hash = preview.article_hash
    review.applied_revision = task.revision + 1
    task.seo_reviews[review_index] = review
    task.initial_article = preview.article
    task.initial_article_word_count = visible_word_count(preview.article)
    task.initial_article_hash = preview.article_hash
    task.article = preview.article
    invalidate_downstream(task, "initial_article")
    task.source_links = [
        SourceLink.model_validate(item)
        for item in extract_link_inventory(preview.article)
    ]
    task.transition_added = has_intro_transition(preview.article)
    task.article_versions.append(
        ArticleVersion(
            kind="initial",
            content=preview.article,
            word_count=visible_word_count(preview.article),
            content_hash=preview.article_hash,
            created_at=timestamp,
            source_kind=f"seo_review:{review_id}",
        )
    )
    return summary


def complete_server_seo_review(
    task: TaskRecord,
    *,
    review_id: str,
    confirm_pending: bool,
    actor_user_id: str,
) -> SeoReviewFinalizeSummary:
    review_index, review = _editable_review(task, review_id)
    summary = _summary(review)
    if summary.accepted_count:
        raise ServerSeoReviewValidationError(
            "SEO review still has accepted changes"
        )
    if summary.pending_count and not confirm_pending:
        raise ServerSeoReviewConflict(
            "SEO review still has pending changes"
        )
    review.status = "completed"
    review.finalized_at = now_iso()
    review.finalized_by = actor_user_id
    task.seo_reviews[review_index] = review
    return summary


__all__ = [
    "SeoReviewDecisionSummary",
    "SeoReviewFinalizeSummary",
    "ServerSeoReviewConflict",
    "ServerSeoReviewNotFound",
    "ServerSeoReviewValidationError",
    "apply_server_seo_review",
    "build_server_seo_review_preview",
    "complete_server_seo_review",
    "update_server_seo_review_change",
]
