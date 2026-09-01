from __future__ import annotations

from collections.abc import Sequence

from .context import (
    AssistantPublishedTopicContext,
    AssistantTaskContext,
    AssistantWorkspaceContext,
)
from .contracts import ActionKind, PlanDraft, PlanStep
from .planner import _requested_article_counts, request_skips_review
from .policy import AssistantPolicyError, _selection_key, sanitize_message


# The assistant message itself may be longer, but the per-step public summary
# is deliberately bounded so a fixed-mode plan remains cheap to persist and
# render. The complete user message stays in the private conversation history.
MAX_FIXED_WRITING_INSTRUCTION_LENGTH = 7_000

_STATUS_RANK = {
    "new": 0,
    "titles_ready": 1,
    "title_selected": 2,
    "outline_ready": 3,
    "outline_confirmed": 4,
    "draft_ready": 5,
    "initial_ai_checked": 6,
    "humanized_ready": 7,
    "final_ai_checked": 8,
    "links_verified": 9,
    "images_ready": 10,
    "docx_exported": 11,
    "completed": 11,
    "delivered": 11,
}
_COMPLETED_STATUSES = frozenset({"docx_exported", "completed", "delivered"})
_DELIVERY_ACTIONS = (
    "humanize",
    "restore_links",
    "prepare_images",
    "export_docx",
    "generate_tdk",
    "package_delivery",
)
_ARTICLE_ACTIONS = (
    "generate_titles",
    "select_title",
    "generate_products",
    "confirm_products",
    "generate_outline",
    "start_research",
    "generate_article",
    "review",
    *_DELIVERY_ACTIONS,
)


def _status_rank(task: AssistantTaskContext) -> int:
    status = task.status.strip().casefold()
    try:
        return _STATUS_RANK[status]
    except KeyError as exc:
        raise AssistantPolicyError(
            f"固定写作模式无法识别文章状态：{task.status or 'unknown'}"
        ) from exc


def _selected_tasks(
    context: AssistantWorkspaceContext,
    *,
    request: str,
    selected_task_ids: Sequence[str],
    selection_locked: bool,
) -> tuple[tuple[str, AssistantTaskContext], ...]:
    by_id: dict[str, list[tuple[str, AssistantTaskContext]]] = {}
    by_project: dict[str, tuple[AssistantTaskContext, ...]] = {}
    for project in context.projects:
        by_project[project.project_id] = project.tasks
        for task in project.tasks:
            by_id.setdefault(task.task_id, []).append((project.project_id, task))

    if selection_locked:
        selected: list[tuple[str, AssistantTaskContext]] = []
        for task_id in selected_task_ids:
            matches = by_id.get(task_id, [])
            if len(matches) != 1:
                raise AssistantPolicyError(
                    "固定写作模式选择的文章不在当前项目范围内"
                )
            selected.append(matches[0])
        return tuple(selected)

    # With no explicit checkboxes, keep the default predictable: one new
    # article per selected project, unless the user gives an unambiguous
    # quantity (for example, "写 2 篇").
    counts = _requested_article_counts(request, context.project_ids)
    selected: list[tuple[str, AssistantTaskContext]] = []
    for project_id in context.project_ids:
        candidates = sorted(
            (
                task
                for task in by_project.get(project_id, ())
                if not task.manual_completed
                and task.status.strip().casefold() not in _COMPLETED_STATUSES
            ),
            key=lambda task: task.task_id,
        )
        count = counts.get(project_id, 1)
        selected.extend((project_id, task) for task in candidates[:count])
    return tuple(selected)


def _selected_topics(
    context: AssistantWorkspaceContext,
    *,
    request: str,
    selected_tasks: Sequence[tuple[str, AssistantTaskContext]],
    selection_locked: bool,
) -> tuple[tuple[str, AssistantPublishedTopicContext], ...]:
    if selection_locked:
        return ()

    counts = _requested_article_counts(request, context.project_ids)
    selected_counts: dict[str, int] = {}
    for project_id, _task in selected_tasks:
        selected_counts[project_id] = selected_counts.get(project_id, 0) + 1

    topics: list[tuple[str, AssistantPublishedTopicContext]] = []
    for project in context.projects:
        needed = max(counts.get(project.project_id, 1) - selected_counts.get(project.project_id, 0), 0)
        if not needed:
            continue
        existing_topics = {_selection_key(task.topic) for task in project.tasks}
        existing_keywords = {
            _selection_key(task.primary_keyword)
            for task in project.tasks
            if task.primary_keyword.strip()
        }
        candidates: list[AssistantPublishedTopicContext] = []
        for topic in sorted(
            project.published_topics,
            key=lambda item: (item.topic_id, item.topic),
        ):
            topic_key = _selection_key(topic.topic)
            keyword_key = _selection_key(topic.primary_keyword)
            if not topic_key or topic_key in existing_topics:
                continue
            if keyword_key and keyword_key in existing_keywords:
                continue
            candidates.append(topic)
            existing_topics.add(topic_key)
            if keyword_key:
                existing_keywords.add(keyword_key)
            if len(candidates) == needed:
                break
        if len(candidates) < needed:
            raise AssistantPolicyError(
                f"项目 {project.project_id} 没有足够的可用已发布话题，请先发布更多话题或选择现有文章"
            )
        topics.extend((project.project_id, topic) for topic in candidates)
    return tuple(topics)


def _step(
    *,
    sequence: int,
    chain_index: int,
    project_id: str,
    task: AssistantTaskContext | None,
    action: ActionKind,
    writing_instruction: str = "",
    create_task_step_id: str = "",
) -> PlanStep:
    input_summary: dict[str, object] = {}
    if action == "generate_article":
        input_summary.update(
            {
                "operation": "article",
                "use_evidence_pack": True,
            }
        )
    if writing_instruction and action in {"generate_outline", "generate_article"}:
        # This is an operator-owned, per-run supplement. It is intentionally
        # not called "prompt" so it cannot be confused with a server prompt
        # snapshot or a provider secret by the policy boundary.
        input_summary["writing_instruction"] = writing_instruction
    if create_task_step_id:
        input_summary["create_task_step_id"] = create_task_step_id
    return PlanStep(
        step_id=f"fixed-{chain_index}-{action}",
        sequence=sequence,
        action_kind=action,
        project_id=project_id,
        article_task_id=task.task_id if task is not None else None,
        input_summary=input_summary,
    )


def _append_topic_chain(
    steps: list[PlanStep],
    *,
    chain_index: int,
    project_id: str,
    topic: AssistantPublishedTopicContext,
    writing_instruction: str,
    skip_review: bool,
) -> None:
    create_step_id = f"fixed-{chain_index}-create-task"
    actions = tuple(
        action for action in _ARTICLE_ACTIONS
        if not (skip_review and action == "review")
    )
    first_sequence = len(steps) + 1
    article_steps = [
        _step(
            sequence=first_sequence + offset + 1,
            chain_index=chain_index,
            project_id=project_id,
            task=None,
            action=action,
            writing_instruction=writing_instruction,
            create_task_step_id=create_step_id,
        )
        for offset, action in enumerate(actions)
    ]
    create_summary: dict[str, object] = {
        "published_topic_id": topic.topic_id,
        "topic": topic.topic,
        "bind_step_ids": [step.step_id for step in article_steps],
    }
    if topic.primary_keyword:
        create_summary["primary_keyword"] = topic.primary_keyword
    if topic.competitor_keyword:
        create_summary["competitor_keyword"] = topic.competitor_keyword
    steps.append(
        PlanStep(
            step_id=create_step_id,
            sequence=first_sequence,
            action_kind="create_task",
            project_id=project_id,
            input_summary=create_summary,
        )
    )
    steps.extend(article_steps)


def build_fixed_article_plan(
    request: str,
    context: AssistantWorkspaceContext,
    *,
    selected_task_ids: Sequence[str] = (),
    selection_locked: bool = False,
    concurrency_limit: int = 5,
) -> PlanDraft:
    """Build the deterministic article lane without a planner-model call.

    The builder chooses an existing Server Task first. If a project needs more
    articles and has no suitable Task, it deterministically chooses published
    topics and lets the existing create-task binding allocate the Task during
    execution. It never invents a topic or silently reuses a completed Task.
    Normal plan confirmation, project authorization, prompt/knowledge pinning,
    and worker CAS checks still happen after this draft is built.
    """

    normalized_request = sanitize_message(request)
    writing_instruction = sanitize_message(
        request,
        max_length=MAX_FIXED_WRITING_INSTRUCTION_LENGTH,
    )
    tasks = _selected_tasks(
        context,
        request=normalized_request,
        selected_task_ids=selected_task_ids,
        selection_locked=selection_locked,
    )
    topics = _selected_topics(
        context,
        request=normalized_request,
        selected_tasks=tasks,
        selection_locked=selection_locked,
    )
    if not tasks and not topics:
        raise AssistantPolicyError(
            "固定写作模式没有可继续的文章任务，也没有可用的已发布话题，请先发布话题或选择现有文章"
        )

    skip_review = request_skips_review(normalized_request)
    steps: list[PlanStep] = []
    participating_projects: list[str] = []
    active_task_count = 0
    for chain_index, (project_id, task) in enumerate(tasks, start=1):
        normalized_status = task.status.strip().casefold()
        if task.manual_completed and normalized_status != "docx_exported":
            raise AssistantPolicyError(
                f"文章 {task.task_id} 已人工完成，固定写作模式不会覆盖它"
            )
        rank = _status_rank(task)
        if normalized_status in {"completed", "delivered"}:
            raise AssistantPolicyError(
                f"文章 {task.task_id} 已完成交付，固定写作模式不会重复生成"
            )

        before = len(steps)
        if rank < _STATUS_RANK["title_selected"] or not task.selected_title:
            if task.title_candidate_count == 0:
                steps.append(
                    _step(
                        sequence=len(steps) + 1,
                        chain_index=chain_index,
                        project_id=project_id,
                        task=task,
                        action="generate_titles",
                    )
                )
            steps.append(
                _step(
                    sequence=len(steps) + 1,
                    chain_index=chain_index,
                    project_id=project_id,
                    task=task,
                    action="select_title",
                )
            )

        if task.confirmed_product_count == 0:
            if task.product_candidate_count == 0:
                steps.append(
                    _step(
                        sequence=len(steps) + 1,
                        chain_index=chain_index,
                        project_id=project_id,
                        task=task,
                        action="generate_products",
                    )
                )
            steps.append(
                _step(
                    sequence=len(steps) + 1,
                    chain_index=chain_index,
                    project_id=project_id,
                    task=task,
                    action="confirm_products",
                )
            )

        if rank < _STATUS_RANK["outline_confirmed"]:
            steps.append(
                _step(
                    sequence=len(steps) + 1,
                    chain_index=chain_index,
                    project_id=project_id,
                    task=task,
                    action="generate_outline",
                    writing_instruction=writing_instruction,
                )
            )

        article_will_generate = rank < _STATUS_RANK["draft_ready"]
        if article_will_generate:
            steps.append(
                _step(
                    sequence=len(steps) + 1,
                    chain_index=chain_index,
                    project_id=project_id,
                    task=task,
                    action="start_research",
                )
            )
            steps.append(
                _step(
                    sequence=len(steps) + 1,
                    chain_index=chain_index,
                    project_id=project_id,
                    task=task,
                    action="generate_article",
                    writing_instruction=writing_instruction,
                )
            )
        article_needs_review = article_will_generate or (
            rank == _STATUS_RANK["draft_ready"]
        )

        if article_needs_review and not skip_review and not any(
            step.article_task_id == task.task_id
            and step.action_kind == "review"
            for step in steps[before:]
        ):
            steps.append(
                _step(
                    sequence=len(steps) + 1,
                    chain_index=chain_index,
                    project_id=project_id,
                    task=task,
                    action="review",
                )
            )

        # A fixed writing request means a complete deliverable, not only an
        # initial article draft. Keep the suffix state-aware so an existing
        # task resumes at its first unfinished stage while a new task gets
        # the canonical 14-step chain.
        if rank < _STATUS_RANK["humanized_ready"]:
            steps.append(
                _step(
                    sequence=len(steps) + 1,
                    chain_index=chain_index,
                    project_id=project_id,
                    task=task,
                    action="humanize",
                )
            )
        if rank < _STATUS_RANK["links_verified"]:
            steps.append(
                _step(
                    sequence=len(steps) + 1,
                    chain_index=chain_index,
                    project_id=project_id,
                    task=task,
                    action="restore_links",
                )
            )
        if rank < _STATUS_RANK["images_ready"]:
            steps.append(
                _step(
                    sequence=len(steps) + 1,
                    chain_index=chain_index,
                    project_id=project_id,
                    task=task,
                    action="prepare_images",
                )
            )
        if rank < _STATUS_RANK["docx_exported"]:
            steps.append(
                _step(
                    sequence=len(steps) + 1,
                    chain_index=chain_index,
                    project_id=project_id,
                    task=task,
                    action="export_docx",
                )
            )
        if normalized_status == "docx_exported":
            # The Server marks a Word export as manually complete, but TDK and
            # delivery packaging are still legitimate downstream actions.
            next_sequence = len(steps) + 1
            for offset, action in enumerate(_DELIVERY_ACTIONS[-2:], start=0):
                steps.append(
                    _step(
                        sequence=next_sequence + offset,
                        chain_index=chain_index,
                        project_id=project_id,
                        task=task,
                        action=action,
                    )
                )
        elif rank < _STATUS_RANK["docx_exported"]:
            next_sequence = len(steps) + 1
            for offset, action in enumerate(_DELIVERY_ACTIONS[4:], start=0):
                steps.append(
                    _step(
                        sequence=next_sequence + offset,
                        chain_index=chain_index,
                        project_id=project_id,
                        task=task,
                        action=action,
                    )
                )

        if len(steps) > before and project_id not in participating_projects:
            participating_projects.append(project_id)
        if len(steps) > before:
            active_task_count += 1
        elif selection_locked:
            raise AssistantPolicyError(
                f"文章 {task.task_id} 当前没有可执行的固定写作步骤"
            )

    for topic_index, (project_id, topic) in enumerate(
        topics,
        start=len(tasks) + 1,
    ):
        before = len(steps)
        _append_topic_chain(
            steps,
            chain_index=topic_index,
            project_id=project_id,
            topic=topic,
            writing_instruction=writing_instruction,
            skip_review=skip_review,
        )
        if len(steps) > before and project_id not in participating_projects:
            participating_projects.append(project_id)
        if len(steps) > before:
            active_task_count += 1

    if not steps:
        if skip_review:
            detail = "；本次请求已明确跳过复检"
        else:
            detail = "；可能已完成正文或正在等待后续人工环节"
        raise AssistantPolicyError(f"选中的文章没有可执行的固定写作步骤{detail}")

    return PlanDraft(
        title=f"固定写作 · {active_task_count} 篇",
        natural_language_request=normalized_request,
        project_ids=participating_projects,
        steps=steps,
        concurrency_limit=max(1, min(32, int(concurrency_limit))),
        attention_state="user_confirmation",
    )


__all__ = [
    "MAX_FIXED_WRITING_INSTRUCTION_LENGTH",
    "build_fixed_article_plan",
]
