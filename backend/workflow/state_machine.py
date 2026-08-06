from __future__ import annotations

from typing import Iterable

try:
    from models import (
        STATUS_DOCX_EXPORTED,
        STATUS_DRAFT_READY,
        STATUS_FINAL_AI_CHECKED,
        STATUS_HUMANIZED_READY,
        STATUS_IMAGES_READY,
        STATUS_INITIAL_AI_CHECKED,
        STATUS_LINKS_VERIFIED,
        STATUS_NEW,
        STATUS_OUTLINE_CONFIRMED,
        STATUS_OUTLINE_READY,
        STATUS_TITLE_SELECTED,
        STATUS_TITLES_READY,
        AICheck,
        LinkValidation,
        TaskRecord,
        TdkMetadata,
        WorkflowError,
    )
except ImportError:  # pragma: no cover - supports `import backend.workflow`
    from ..models import (
        STATUS_DOCX_EXPORTED,
        STATUS_DRAFT_READY,
        STATUS_FINAL_AI_CHECKED,
        STATUS_HUMANIZED_READY,
        STATUS_IMAGES_READY,
        STATUS_INITIAL_AI_CHECKED,
        STATUS_LINKS_VERIFIED,
        STATUS_NEW,
        STATUS_OUTLINE_CONFIRMED,
        STATUS_OUTLINE_READY,
        STATUS_TITLE_SELECTED,
        STATUS_TITLES_READY,
        AICheck,
        LinkValidation,
        TaskRecord,
        TdkMetadata,
        WorkflowError,
    )


ACTION_GENERATE_TITLES = "generate_titles"
ACTION_SELECT_TITLE = "select_title"
ACTION_GENERATE_PRODUCTS = "generate_products"
ACTION_UPDATE_PRODUCTS = "update_products"
ACTION_GENERATE_OUTLINE = "generate_outline"
ACTION_UPDATE_OUTLINE = "update_outline"
ACTION_GENERATE_ARTICLE = "generate_article"
ACTION_UPDATE_ARTICLE = "update_article"
ACTION_CONFIRM_INITIAL_AI = "confirm_initial_ai_check"
ACTION_HUMANIZE_ARTICLE = "humanize_article"
ACTION_UPDATE_HUMANIZED = "update_humanized_article"
ACTION_CONFIRM_FINAL_AI = "confirm_final_ai_check"
ACTION_RESTORE_LINKS = "restore_links"
ACTION_VERIFY_LINKS = "verify_links"
ACTION_UPDATE_IMAGES = "update_images"
ACTION_PREPARE_IMAGES = "prepare_images"
ACTION_EXPORT_DOCX = "export_docx"
ACTION_DOWNLOAD_DOCX = "download_docx"
ACTION_GENERATE_TDK = "generate_tdk"
ACTION_PACKAGE_DELIVERY = "package_delivery"
ACTION_RETRY_COMPRESSION = "retry_compression"
ACTION_CLEAR_WORKFLOW_ERROR = "clear_workflow_error"
ACTION_REWRITE_FROM_SCRATCH = "rewrite_from_scratch"


LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    STATUS_NEW: frozenset({STATUS_TITLES_READY}),
    STATUS_TITLES_READY: frozenset({STATUS_TITLE_SELECTED}),
    STATUS_TITLE_SELECTED: frozenset({STATUS_OUTLINE_READY}),
    STATUS_OUTLINE_READY: frozenset({STATUS_OUTLINE_CONFIRMED}),
    STATUS_OUTLINE_CONFIRMED: frozenset({STATUS_DRAFT_READY}),
    STATUS_DRAFT_READY: frozenset({STATUS_INITIAL_AI_CHECKED}),
    STATUS_INITIAL_AI_CHECKED: frozenset({STATUS_HUMANIZED_READY}),
    STATUS_HUMANIZED_READY: frozenset({STATUS_FINAL_AI_CHECKED}),
    STATUS_FINAL_AI_CHECKED: frozenset({STATUS_LINKS_VERIFIED}),
    STATUS_LINKS_VERIFIED: frozenset({STATUS_IMAGES_READY}),
    STATUS_IMAGES_READY: frozenset({STATUS_DOCX_EXPORTED}),
    STATUS_DOCX_EXPORTED: frozenset(),
}


_ACTIONS_BY_STATUS: dict[str, tuple[str, ...]] = {
    STATUS_NEW: (ACTION_GENERATE_TITLES,),
    STATUS_TITLES_READY: (ACTION_GENERATE_TITLES, ACTION_SELECT_TITLE),
    STATUS_TITLE_SELECTED: (
        ACTION_SELECT_TITLE,
        ACTION_GENERATE_PRODUCTS,
        ACTION_UPDATE_PRODUCTS,
        ACTION_GENERATE_OUTLINE,
        ACTION_UPDATE_OUTLINE,
        ACTION_UPDATE_ARTICLE,
    ),
    STATUS_OUTLINE_READY: (
        ACTION_GENERATE_PRODUCTS,
        ACTION_UPDATE_PRODUCTS,
        ACTION_GENERATE_OUTLINE,
        ACTION_UPDATE_OUTLINE,
        ACTION_UPDATE_ARTICLE,
    ),
    STATUS_OUTLINE_CONFIRMED: (
        ACTION_GENERATE_PRODUCTS,
        ACTION_UPDATE_PRODUCTS,
        ACTION_GENERATE_OUTLINE,
        ACTION_UPDATE_OUTLINE,
        ACTION_GENERATE_ARTICLE,
        ACTION_UPDATE_ARTICLE,
    ),
    STATUS_DRAFT_READY: (
        ACTION_GENERATE_PRODUCTS,
        ACTION_UPDATE_PRODUCTS,
        ACTION_GENERATE_OUTLINE,
        ACTION_UPDATE_OUTLINE,
        ACTION_GENERATE_ARTICLE,
        ACTION_UPDATE_ARTICLE,
        ACTION_CONFIRM_INITIAL_AI,
        ACTION_UPDATE_HUMANIZED,
    ),
    STATUS_INITIAL_AI_CHECKED: (
        ACTION_GENERATE_PRODUCTS,
        ACTION_UPDATE_PRODUCTS,
        ACTION_GENERATE_OUTLINE,
        ACTION_UPDATE_OUTLINE,
        ACTION_GENERATE_ARTICLE,
        ACTION_UPDATE_ARTICLE,
        ACTION_HUMANIZE_ARTICLE,
        ACTION_UPDATE_HUMANIZED,
    ),
    STATUS_HUMANIZED_READY: (
        ACTION_GENERATE_PRODUCTS,
        ACTION_UPDATE_PRODUCTS,
        ACTION_GENERATE_OUTLINE,
        ACTION_UPDATE_OUTLINE,
        ACTION_GENERATE_ARTICLE,
        ACTION_UPDATE_ARTICLE,
        ACTION_HUMANIZE_ARTICLE,
        ACTION_UPDATE_HUMANIZED,
        ACTION_CONFIRM_FINAL_AI,
    ),
    STATUS_FINAL_AI_CHECKED: (
        ACTION_GENERATE_PRODUCTS,
        ACTION_UPDATE_PRODUCTS,
        ACTION_GENERATE_OUTLINE,
        ACTION_UPDATE_OUTLINE,
        ACTION_GENERATE_ARTICLE,
        ACTION_UPDATE_ARTICLE,
        ACTION_HUMANIZE_ARTICLE,
        ACTION_UPDATE_HUMANIZED,
        ACTION_RESTORE_LINKS,
        ACTION_VERIFY_LINKS,
    ),
    STATUS_LINKS_VERIFIED: (
        ACTION_GENERATE_PRODUCTS,
        ACTION_UPDATE_PRODUCTS,
        ACTION_GENERATE_OUTLINE,
        ACTION_UPDATE_OUTLINE,
        ACTION_GENERATE_ARTICLE,
        ACTION_UPDATE_ARTICLE,
        ACTION_UPDATE_HUMANIZED,
        ACTION_RESTORE_LINKS,
        ACTION_UPDATE_IMAGES,
        ACTION_PREPARE_IMAGES,
    ),
    STATUS_IMAGES_READY: (
        ACTION_GENERATE_PRODUCTS,
        ACTION_UPDATE_PRODUCTS,
        ACTION_GENERATE_OUTLINE,
        ACTION_UPDATE_OUTLINE,
        ACTION_GENERATE_ARTICLE,
        ACTION_UPDATE_ARTICLE,
        ACTION_UPDATE_HUMANIZED,
        ACTION_UPDATE_IMAGES,
        ACTION_PREPARE_IMAGES,
        ACTION_EXPORT_DOCX,
    ),
    STATUS_DOCX_EXPORTED: (
        ACTION_GENERATE_PRODUCTS,
        ACTION_UPDATE_PRODUCTS,
        ACTION_GENERATE_OUTLINE,
        ACTION_UPDATE_OUTLINE,
        ACTION_GENERATE_ARTICLE,
        ACTION_UPDATE_ARTICLE,
        ACTION_UPDATE_HUMANIZED,
        ACTION_UPDATE_IMAGES,
        ACTION_PREPARE_IMAGES,
        ACTION_DOWNLOAD_DOCX,
        ACTION_EXPORT_DOCX,
        ACTION_GENERATE_TDK,
        ACTION_PACKAGE_DELIVERY,
    ),
}


class InvalidWorkflowTransition(ValueError):
    def __init__(self, current: str, target: str):
        self.current = current
        self.target = target
        super().__init__(f"Cannot transition article workflow from {current!r} to {target!r}.")


class WorkflowActionNotAllowed(ValueError):
    def __init__(self, status: str, action: str, allowed: Iterable[str]):
        self.status = status
        self.action = action
        self.allowed = tuple(allowed)
        super().__init__(
            f"Action {action!r} is not allowed while task status is {status!r}. "
            f"Allowed actions: {', '.join(self.allowed) or 'none'}."
        )


def can_transition(current: str, target: str) -> bool:
    return current == target or target in LEGAL_TRANSITIONS.get(current, frozenset())


def ensure_transition(current: str, target: str) -> None:
    if not can_transition(current, target):
        raise InvalidWorkflowTransition(current, target)


def transition_task(task: TaskRecord, target: str) -> TaskRecord:
    ensure_transition(task.status, target)
    task.status = target
    task.workflow_error = None
    return task


def allowed_actions(task_or_status: TaskRecord | str) -> list[str]:
    task = task_or_status if isinstance(task_or_status, TaskRecord) else None
    status = task.status if task is not None else str(task_or_status)
    actions = list(_ACTIONS_BY_STATUS.get(status, ()))

    if task is None or task.workflow_error is None:
        return _with_rewrite_action(actions)

    error = task.workflow_error
    if error.code == "compression_failed":
        return _with_rewrite_action(actions)
    if not error.blocking:
        if ACTION_CLEAR_WORKFLOW_ERROR not in actions:
            actions.append(ACTION_CLEAR_WORKFLOW_ERROR)
        return _with_rewrite_action(actions)

    recovery_actions: list[str] = []
    if error.recoverable:
        if error.code == "compression_failed":
            recovery_actions.append(ACTION_RETRY_COMPRESSION)
        retry_candidates: tuple[str, ...] = ()
        if error.code in {"humanize_failed", "humanization_failed"} or error.stage in {
            "humanize",
            "humanized_article",
        }:
            retry_candidates = (ACTION_HUMANIZE_ARTICLE,)
        elif error.code in {"link_restore_failed", "link_validation_failed"} or error.stage in {
            "links",
            "link_restore",
        }:
            retry_candidates = (ACTION_RESTORE_LINKS, ACTION_VERIFY_LINKS)
        elif error.code in {"image_failed", "image_prepare_failed"} or error.stage in {
            "images",
            "prepare_images",
        }:
            retry_candidates = (ACTION_UPDATE_IMAGES, ACTION_PREPARE_IMAGES)
        elif error.code == "export_failed" or error.stage == "export_docx":
            retry_candidates = (ACTION_EXPORT_DOCX,)
        recovery_actions.extend(
            action for action in retry_candidates if action in actions
        )
        for action in (
            ACTION_GENERATE_ARTICLE,
            ACTION_UPDATE_ARTICLE,
            ACTION_UPDATE_HUMANIZED,
        ):
            if action in actions:
                recovery_actions.append(action)
        recovery_actions.append(ACTION_CLEAR_WORKFLOW_ERROR)
    return _with_rewrite_action(recovery_actions)


def _with_rewrite_action(actions: Iterable[str]) -> list[str]:
    return list(dict.fromkeys([*actions, ACTION_REWRITE_FROM_SCRATCH]))


def ensure_action_allowed(task: TaskRecord, action: str) -> None:
    actions = allowed_actions(task)
    if action not in actions:
        raise WorkflowActionNotAllowed(task.status, action, actions)


def _keep_versions(task: TaskRecord, maximum_phase: int) -> None:
    """Keep append-only history while runtime fields are invalidated separately."""

    del task, maximum_phase


def _clear_tdk(task: TaskRecord) -> None:
    task.tdk = TdkMetadata()
    task.tdk_path = ""
    task.tdk_asset_id = ""
    task.tdk_content_hash = ""
    task.tdk_filename = ""
    task.delivery_package_path = ""
    task.delivery_package_asset_id = ""
    task.delivery_package_content_hash = ""
    task.delivery_package_filename = ""


def _clear_export(task: TaskRecord) -> None:
    task.docx_path = ""
    task.docx_asset_id = ""
    task.docx_content_hash = ""
    task.docx_filename = ""
    task.legacy_export = False
    _clear_tdk(task)


def _clear_images(task: TaskRecord) -> None:
    task.images = []
    task.final_article = ""
    task.final_article_word_count = 0
    task.final_article_hash = ""
    _clear_export(task)


def _clear_links_and_images(task: TaskRecord) -> None:
    task.linked_article = ""
    task.linked_article_word_count = 0
    task.linked_article_hash = ""
    task.link_validation = LinkValidation()
    _clear_images(task)


def _clear_humanized_and_after(task: TaskRecord) -> None:
    task.humanized_article = ""
    task.humanization_skipped = False
    task.humanized_article_word_count = 0
    task.humanized_article_hash = ""
    task.final_ai_check = AICheck()
    _clear_links_and_images(task)


def _clear_initial_and_after(task: TaskRecord) -> None:
    task.initial_article = ""
    task.initial_article_word_count = 0
    task.initial_article_hash = ""
    task.initial_ai_check = AICheck()
    task.source_links = []
    task.transition_added = False
    _clear_humanized_and_after(task)


def _clear_raw_and_after(task: TaskRecord) -> None:
    task.raw_draft_article = ""
    task.raw_draft_word_count = 0
    task.raw_draft_hash = ""
    _clear_initial_and_after(task)


def reset_for_full_rewrite(task: TaskRecord) -> TaskRecord:
    """Return a task to its source-only state so the article can be rebuilt."""

    task.title_candidates = []
    task.selected_title = ""
    task.product_candidate_ids = []
    task.products = []
    task.outline = ""
    task.outline_draft = ""
    task.hero_image = ""
    _clear_raw_and_after(task)
    task.article = ""
    task.article_versions = []
    task.zero_gpt_report = ""
    task.status = STATUS_NEW
    task.workflow_error = None

    # These are legacy/derived extension fields rather than source task data.
    if task.__pydantic_extra__:
        for field in (
            "allowed_actions",
            "compression",
            "initial_article_ready",
            "initial_article_issues",
        ):
            task.__pydantic_extra__.pop(field, None)
    return task


def invalidate_downstream(task: TaskRecord, changed_stage: str) -> TaskRecord:
    """Invalidate every artifact/check which depends on an edited upstream stage.

    The caller should first place the newly edited value on ``task`` and then
    call this function with its field/stage name. The function intentionally
    does not advance the workflow; normal forward progress must still use
    ``transition_task``.
    """

    stage = changed_stage.strip().lower()
    task.workflow_error = None

    if stage in {"title_candidates", "titles"}:
        task.selected_title = ""
        task.product_candidate_ids = []
        task.outline = ""
        task.outline_draft = ""
        _clear_raw_and_after(task)
        task.article = ""
        _keep_versions(task, -1)
        task.status = STATUS_TITLES_READY
    elif stage in {"selected_title", "title"}:
        task.product_candidate_ids = []
        task.outline = ""
        task.outline_draft = ""
        _clear_raw_and_after(task)
        task.article = ""
        _keep_versions(task, -1)
        task.status = STATUS_TITLE_SELECTED
    elif stage in {"products", "product"}:
        task.product_candidate_ids = []
        task.outline = ""
        task.outline_draft = ""
        _clear_raw_and_after(task)
        task.article = ""
        _keep_versions(task, -1)
        if task.selected_title:
            task.status = STATUS_TITLE_SELECTED
        elif task.title_candidates:
            task.status = STATUS_TITLES_READY
        else:
            task.status = STATUS_NEW
    elif stage == "outline":
        _clear_raw_and_after(task)
        task.article = ""
        _keep_versions(task, -1)
        task.status = STATUS_OUTLINE_READY
    elif stage in {"raw", "raw_draft", "raw_draft_article"}:
        _clear_initial_and_after(task)
        task.article = task.raw_draft_article
        _keep_versions(task, 0)
        task.status = STATUS_OUTLINE_CONFIRMED
    elif stage in {"initial", "initial_article", "article"}:
        if stage == "article":
            task.initial_article = task.article
        task.initial_ai_check = AICheck()
        task.source_links = []
        task.transition_added = False
        _clear_humanized_and_after(task)
        task.article = task.initial_article or task.article
        _keep_versions(task, 1)
        task.status = STATUS_DRAFT_READY
    elif stage in {"initial_ai_check", "initial_check"}:
        _clear_humanized_and_after(task)
        task.article = task.initial_article
        _keep_versions(task, 1)
        task.status = (
            STATUS_INITIAL_AI_CHECKED
            if task.initial_ai_check.confirmed
            else STATUS_DRAFT_READY
        )
    elif stage in {"humanized", "humanized_article"}:
        task.humanization_skipped = False
        task.final_ai_check = AICheck()
        _clear_links_and_images(task)
        task.article = task.humanized_article
        _keep_versions(task, 2)
        task.status = STATUS_HUMANIZED_READY
    elif stage in {"final_ai_check", "final_check"}:
        _clear_links_and_images(task)
        task.article = task.humanized_article
        _keep_versions(task, 2)
        task.status = (
            STATUS_FINAL_AI_CHECKED
            if task.final_ai_check.confirmed
            else STATUS_HUMANIZED_READY
        )
    elif stage in {"links", "linked", "linked_article", "source_links", "link_validation"}:
        task.link_validation = LinkValidation()
        _clear_images(task)
        task.article = task.linked_article or task.humanized_article
        _keep_versions(task, 3)
        task.status = STATUS_FINAL_AI_CHECKED
    elif stage in {"images", "hero_image"}:
        for image in task.images:
            image.status = "pending"
            image.error = ""
            image.prepared_path = ""
            image.prepared_asset_id = ""
            image.prepared_content_hash = ""
            image.width = None
            image.height = None
            image.filename = ""
            image.marker = ""
        task.final_article = ""
        task.final_article_word_count = 0
        task.final_article_hash = ""
        _clear_export(task)
        task.status = STATUS_LINKS_VERIFIED
    elif stage == "final_article":
        task.article = task.final_article
        _clear_export(task)
        _keep_versions(task, 4)
        task.status = STATUS_IMAGES_READY
    else:
        raise ValueError(f"Unknown workflow invalidation stage: {changed_stage!r}")

    return task


def set_workflow_error(
    task: TaskRecord,
    *,
    code: str,
    message: str,
    stage: str = "",
    occurred_at: str = "",
    recoverable: bool = True,
    blocking: bool = True,
) -> TaskRecord:
    task.workflow_error = WorkflowError(
        code=code,
        message=message,
        stage=stage,
        occurred_at=occurred_at,
        recoverable=recoverable,
        blocking=blocking,
    )
    return task
