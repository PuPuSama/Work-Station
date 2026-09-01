from __future__ import annotations

from collections.abc import Sequence

from .context import AssistantTaskContext, AssistantWorkspaceContext
from .contracts import ActionKind, PlanDraft, PlanStep
from .planner import _requested_article_counts, request_skips_review
from .policy import AssistantPolicyError, sanitize_message


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


def _step(
    *,
    sequence: int,
    chain_index: int,
    project_id: str,
    task: AssistantTaskContext,
    action: ActionKind,
    writing_instruction: str = "",
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
    return PlanStep(
        step_id=f"fixed-{chain_index}-{action}",
        sequence=sequence,
        action_kind=action,
        project_id=project_id,
        article_task_id=task.task_id,
        input_summary=input_summary,
    )


def build_fixed_article_plan(
    request: str,
    context: AssistantWorkspaceContext,
    *,
    selected_task_ids: Sequence[str] = (),
    selection_locked: bool = False,
    concurrency_limit: int = 5,
) -> PlanDraft:
    """Build the deterministic article lane without a planner-model call.

    The builder only chooses already-existing Server Tasks. It never creates a
    topic or silently reuses a completed Task. Normal plan confirmation,
    project authorization, prompt/knowledge pinning, and worker CAS checks
    still happen after this draft is built.
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
    if not tasks:
        raise AssistantPolicyError(
            "固定写作模式没有找到可继续的文章任务，请先在“设置范围”中选择文章"
        )

    skip_review = request_skips_review(normalized_request)
    steps: list[PlanStep] = []
    participating_projects: list[str] = []
    active_task_count = 0
    for chain_index, (project_id, task) in enumerate(tasks, start=1):
        if task.manual_completed:
            raise AssistantPolicyError(
                f"文章 {task.task_id} 已人工完成，固定写作模式不会覆盖它"
            )
        rank = _status_rank(task)
        if task.status.strip().casefold() in _COMPLETED_STATUSES:
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
        elif rank == _STATUS_RANK["draft_ready"] and not skip_review:
            article_will_generate = True

        if article_will_generate and not skip_review and not any(
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

        if len(steps) > before and project_id not in participating_projects:
            participating_projects.append(project_id)
        if len(steps) > before:
            active_task_count += 1
        elif selection_locked:
            raise AssistantPolicyError(
                f"文章 {task.task_id} 当前没有可执行的固定写作步骤"
            )

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
