from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable, Mapping
from typing import Any

from services.access_control import ActorIdentity, ProjectAccessService

from .context import AssistantTaskContext, AssistantWorkspaceContext
from .contracts import ActionKind, PlanDraft, PlanStep


ALLOWED_ACTION_KINDS: frozenset[ActionKind] = frozenset(
    {
        "list_projects",
        "list_tasks",
        "read_project_context",
        "evidence_query",
        "read_plan_status",
        "update_project_notes",
        "create_task",
        "generate_titles",
        "select_title",
        "generate_products",
        "confirm_products",
        "generate_outline",
        "start_research",
        "generate_article",
        "humanize",
        "review",
        "restore_links",
        "prepare_images",
        "export_docx",
        "generate_tdk",
        "package_delivery",
    }
)

# Natural-language conversations are intentionally narrower than the closed
# registry used by the deterministic batch-writing lane.  Article generation
# belongs to /batch-writing; the assistant may only answer questions or stage
# a project-configuration change for confirmation.
ASSISTANT_ALLOWED_ACTION_KINDS: frozenset[ActionKind] = frozenset(
    {
        "list_projects",
        "list_tasks",
        "read_project_context",
        "evidence_query",
        "read_plan_status",
        "update_project_notes",
    }
)

WRITE_ACTION_KINDS: frozenset[ActionKind] = frozenset(
    ALLOWED_ACTION_KINDS
    - {
        "list_projects",
        "list_tasks",
        "read_project_context",
        "evidence_query",
        "read_plan_status",
    }
)

TASK_BOUND_ACTION_KINDS: frozenset[ActionKind] = frozenset(
    {
        "generate_titles",
        "select_title",
        "generate_products",
        "confirm_products",
        "generate_outline",
        "start_research",
        "generate_article",
        "humanize",
        "review",
        "restore_links",
        "prepare_images",
        "export_docx",
        "generate_tdk",
        "package_delivery",
    }
)

_COMPLETED_TASK_STATUSES = frozenset(
    {"docx_exported", "completed", "delivered"}
)

_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_PRIVATE_SUMMARY_KEYS = {
    "prompt",
    "system_prompt",
    "secret",
    "token",
    "api_key",
    "apikey",
    "credential",
    "password",
    "thought",
    "reasoning",
    "chain_of_thought",
}


class AssistantPolicyError(ValueError):
    """A plan or message violates a Workflow Assistant safety boundary."""


def _task_is_completed(task: AssistantTaskContext) -> bool:
    return bool(
        task.manual_completed
        or task.status.strip().casefold() in _COMPLETED_TASK_STATUSES
    )


def _article_chain_id(step: PlanStep) -> str:
    return str(
        step.article_task_id
        or step.input_summary.get("create_task_step_id")
        or ""
    ).strip()


def _validate_article_evidence_chains(plan: PlanDraft) -> None:
    """Require research/Evidence Pack before every active article generation."""

    researched_chains: set[tuple[str, str]] = set()
    for step in sorted(plan.steps, key=lambda item: item.sequence):
        if step.action_kind not in {"start_research", "generate_article"}:
            continue
        chain_id = _article_chain_id(step)
        if not chain_id:
            # Task binding has a more specific fail-closed error elsewhere.
            continue
        chain_key = (step.project_id, chain_id)
        if step.action_kind == "start_research":
            if step.status != "skipped":
                researched_chains.add(chain_key)
            continue
        if step.status in {"succeeded", "skipped"}:
            # Immutable history predating this validation is not rewritten.
            continue
        if step.input_summary.get("use_evidence_pack") is not True:
            raise AssistantPolicyError(
                "generate_article must use the research Evidence Pack"
            )
        if chain_key not in researched_chains:
            raise AssistantPolicyError(
                "generate_article requires earlier research and an Evidence Pack"
            )


def sanitize_message(content: str, *, max_length: int = 20_000) -> str:
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    normalized = _CONTROL_CHARACTERS.sub("", normalized).strip()
    if not normalized:
        raise AssistantPolicyError("message content is required")
    if len(normalized) > max_length:
        raise AssistantPolicyError("message content is too long")
    return normalized


def _safe_summary(
    value: Any,
    *,
    depth: int = 0,
    string_max_length: int = 1_000,
    allow_empty_strings: bool = False,
) -> Any:
    if depth > 4:
        raise AssistantPolicyError("plan input summary is too deeply nested")
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key).strip()
            normalized_key = re.sub(r"[^a-z0-9_]+", "_", key.casefold()).strip("_")
            key_parts = set(normalized_key.split("_"))
            private_key = normalized_key in _PRIVATE_SUMMARY_KEYS or bool(
                key_parts
                & {
                    "prompt",
                    "secret",
                    "token",
                    "credential",
                    "password",
                    "thought",
                    "reasoning",
                    "key",
                    "apikey",
                }
            )
            if not key or private_key:
                raise AssistantPolicyError("plan input summary contains private data")
            result[key[:80]] = _safe_summary(
                raw_value,
                depth=depth + 1,
                string_max_length=string_max_length,
                allow_empty_strings=allow_empty_strings,
            )
        return result
    if isinstance(value, (list, tuple)):
        if len(value) > 50:
            raise AssistantPolicyError("plan input summary contains too many items")
        return [
            _safe_summary(
                item,
                depth=depth + 1,
                string_max_length=string_max_length,
                allow_empty_strings=allow_empty_strings,
            )
            for item in value
        ]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if not value and allow_empty_strings:
            return ""
        return sanitize_message(value, max_length=string_max_length)
    raise AssistantPolicyError("plan input summary contains an unsupported value")


def sanitize_public_summary(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a tool result before it becomes a durable public projection.

    Tool output may contain longer evidence answers than plan input, but it
    must obey the same private-key and JSON-shape boundary.  Keeping this
    check in the closed registry prevents a newly added Server adapter from
    accidentally persisting credentials, prompt bodies, or model traces.
    """

    result = _safe_summary(
        value,
        string_max_length=12_000,
        allow_empty_strings=True,
    )
    if not isinstance(result, dict) or len(canonical_json(result)) > 32_000:
        raise AssistantPolicyError("workflow tool output is too large")
    return result


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def canonical_plan_hash(plan: PlanDraft | Mapping[str, Any]) -> str:
    payload = (
        plan.normalized_payload()
        if isinstance(plan, PlanDraft)
        else dict(plan)
    )
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _project_notes_change(
    current_notes: str,
    summary: Mapping[str, Any],
) -> tuple[str, str, list[str]]:
    """Apply a model-proposed notes delta to the complete Server value.

    The planner sees only a bounded project summary.  Applying additions and
    exact removals to the full Server value prevents a long, truncated notes
    field from being overwritten by a model-generated partial replacement.
    """

    raw_addition = summary.get("notes_to_add", "")
    if raw_addition is None:
        raw_addition = ""
    if not isinstance(raw_addition, str):
        raise AssistantPolicyError("project notes addition must be text")
    addition = (
        sanitize_message(raw_addition, max_length=10_000)
        if raw_addition.strip()
        else ""
    )
    raw_removals = summary.get("notes_to_remove", [])
    if raw_removals is None:
        raw_removals = []
    if not isinstance(raw_removals, list) or len(raw_removals) > 50:
        raise AssistantPolicyError("project notes removals must be a bounded list")
    removals: list[str] = []
    for raw_removal in raw_removals:
        if not isinstance(raw_removal, str):
            raise AssistantPolicyError("project notes removal must be text")
        removal = sanitize_message(raw_removal, max_length=3_000)
        if removal not in removals:
            removals.append(removal)
    if not addition and not removals:
        raise AssistantPolicyError("project notes change is empty")

    updated = str(current_notes or "").replace("\r\n", "\n").replace("\r", "\n")
    for removal in removals:
        updated = updated.replace(removal, "")
    updated = updated.strip()
    if addition and addition not in updated:
        updated = f"{updated}\n{addition}".strip() if updated else addition
    if len(updated) > 30_000:
        raise AssistantPolicyError("project notes change exceeds the project limit")
    if updated == str(current_notes or "").replace("\r\n", "\n").replace("\r", "\n").strip():
        raise AssistantPolicyError("project notes change has no effect")
    return updated, addition, removals


def validate_plan_scope(
    plan: PlanDraft,
    *,
    actor: ActorIdentity,
    access: ProjectAccessService,
    accessible_project_ids: Iterable[str] | None = None,
    allowed_action_kinds: Iterable[ActionKind] | None = None,
) -> None:
    """Require every plan project and every step project to be authorized.

    The planner output is not trusted merely because it passed Pydantic.  The
    same project permission check is repeated when a plan is created and again
    by execution code before a queued Job is submitted.
    """

    allowed = {
        project_id.strip()
        for project_id in (accessible_project_ids or ())
        if project_id.strip()
    }
    plan_projects = set(plan.project_ids)
    if not plan_projects:
        raise AssistantPolicyError("plan must contain at least one project")
    if not plan_projects.issubset(allowed) and accessible_project_ids is not None:
        raise AssistantPolicyError("plan contains an inaccessible project")
    step_projects = {step.project_id.strip() for step in plan.steps}
    if not step_projects.issubset(plan_projects):
        raise AssistantPolicyError("every step must belong to a plan project")
    for project_id in sorted(plan_projects):
        try:
            access.require(actor, project_id, "project.view")
        except Exception as exc:
            raise AssistantPolicyError("plan contains an inaccessible project") from exc
    allowed_actions = set(allowed_action_kinds or ALLOWED_ACTION_KINDS)
    for step in plan.steps:
        if step.action_kind not in allowed_actions:
            raise AssistantPolicyError("plan contains an unsupported action")


def bind_plan_context(
    plan: PlanDraft,
    *,
    context: AssistantWorkspaceContext,
    selected_task_ids: Iterable[str] | None = None,
) -> PlanDraft:
    """Replace model-supplied pins with the resolved Server snapshots.

    Prompt versions, published knowledge snapshots, task revisions, and
    project revisions are persistence facts.  The planner may request a
    project/task, but it cannot choose a different revision or smuggle one
    project's evidence into another project's step.
    """

    by_project = {project.project_id: project for project in context.projects}
    if not set(plan.project_ids).issubset(by_project):
        raise AssistantPolicyError("plan contains an inaccessible project")
    project_note_steps = [
        step for step in plan.steps if step.action_kind == "update_project_notes"
    ]
    if project_note_steps and len(project_note_steps) != len(plan.steps):
        raise AssistantPolicyError(
            "project notes changes cannot be combined with article workflow steps"
        )
    project_note_ids = [step.project_id for step in project_note_steps]
    if len(project_note_ids) != len(set(project_note_ids)):
        raise AssistantPolicyError(
            "a project notes plan may update each project only once"
        )
    selected_tasks = (
        {
            task_id.strip()
            for task_id in selected_task_ids or ()
            if task_id.strip()
        }
        if selected_task_ids is not None
        else None
    )
    continuation_task_ids = {
        str(step.article_task_id).strip()
        for step in plan.steps
        if (
            step.article_task_id
            and step.action_kind in TASK_BOUND_ACTION_KINDS
            and step.status in {"succeeded", "skipped"}
        )
    }
    # A confirmed plan may create a new Task before running its article
    # chain. The model cannot know the server-allocated Task ID in advance,
    # so it explicitly lists the following step IDs in the create step's
    # public summary. Bind those steps to the create step without accepting
    # an arbitrary model-supplied Task identity.
    steps_by_id = {step.step_id: step for step in plan.steps}
    declared_dynamic_sources: dict[str, str] = {}
    for create_step in plan.steps:
        if create_step.action_kind != "create_task":
            continue
        if create_step.status in {"succeeded", "skipped"}:
            continue
        raw_targets = create_step.input_summary.get("bind_step_ids", [])
        if raw_targets in (None, ""):
            raw_targets = []
        if not isinstance(raw_targets, list) or len(raw_targets) > 100:
            raise AssistantPolicyError(
                "create_task bind_step_ids must be a list"
            )
        for raw_target in raw_targets:
            target_id = str(raw_target).strip()
            target = steps_by_id.get(target_id)
            if not target_id or target is None:
                raise AssistantPolicyError(
                    "create_task references an unavailable workflow step"
                )
            if target.sequence <= create_step.sequence:
                raise AssistantPolicyError(
                    "created Task must bind only later workflow steps"
                )
            if target.project_id != create_step.project_id:
                raise AssistantPolicyError(
                    "created Task cannot bind a different project"
                )
            if target.action_kind not in TASK_BOUND_ACTION_KINDS:
                raise AssistantPolicyError(
                    "created Task can bind only article workflow steps"
                )
            if target.article_task_id:
                raise AssistantPolicyError(
                    "created Task binding cannot replace an explicit article task"
                )
            declared_dynamic_sources[target_id] = create_step.step_id
            existing_source = target.input_summary.get("create_task_step_id")
            if existing_source and str(existing_source).strip() != create_step.step_id:
                raise AssistantPolicyError(
                    "article workflow step has multiple Task creation sources"
                )
            steps_by_id[target_id] = target.model_copy(
                update={
                    "input_summary": {
                        **target.input_summary,
                        "create_task_step_id": create_step.step_id,
                    }
                }
            )

    bound_steps = []
    for step in (steps_by_id[item.step_id] for item in plan.steps):
        project = by_project.get(step.project_id)
        if project is None:
            raise AssistantPolicyError("every step must belong to a resolved project")
        if step.status in {"succeeded", "skipped"}:
            # Completed history is immutable in an explicit revision. This
            # includes create_task rows whose former dynamic targets now have
            # concrete server-allocated Task IDs.
            bound_steps.append(step)
            continue
        if selected_tasks is not None and step.action_kind == "create_task":
            raise AssistantPolicyError(
                "create_task is outside the selected task range"
            )
        if step.action_kind == "create_task":
            published_topic = _published_topic_for_step(project, step)
            source_input = dict(step.input_summary)
            source_input["topic"] = published_topic.topic
            source_input["published_topic_id"] = published_topic.topic_id
            for key, value in (
                ("primary_keyword", published_topic.primary_keyword),
                ("competitor_keyword", published_topic.competitor_keyword),
            ):
                if value:
                    source_input[key] = value
                else:
                    source_input.pop(key, None)
            step = step.model_copy(update={"input_summary": source_input})
        dynamic_task_source = None
        if step.action_kind in TASK_BOUND_ACTION_KINDS and not step.article_task_id:
            source_id = str(step.input_summary.get("create_task_step_id") or "").strip()
            source = steps_by_id.get(source_id)
            if source_id and declared_dynamic_sources.get(step.step_id) != source_id:
                raise AssistantPolicyError(
                    "every article workflow step must be explicitly bound by a create_task step"
                )
            if (
                not source_id
                or source is None
                or source.action_kind != "create_task"
                or source.project_id != step.project_id
                or source.sequence >= step.sequence
            ):
                raise AssistantPolicyError(
                    "every article workflow step must bind an article task"
                )
            if selected_task_ids is not None:
                raise AssistantPolicyError(
                    "created article tasks are outside the selected task range"
                )
            dynamic_task_source = source_id
        if (
            selected_tasks is not None
            and step.article_task_id
            and step.action_kind in TASK_BOUND_ACTION_KINDS
            and step.article_task_id not in selected_tasks
        ):
            raise AssistantPolicyError(
                "article workflow step is outside the selected task range"
            )
        task = None
        if step.article_task_id:
            task = next(
                (item for item in project.tasks if item.task_id == step.article_task_id),
                None,
            )
            if task is None:
                raise AssistantPolicyError("plan references an unavailable article task")
            if (
                task.blocking_failure_code
                and (
                    selected_tasks is None
                    or step.article_task_id not in selected_tasks
                )
            ):
                raise AssistantPolicyError(
                    "article task has a blocking failure and must be explicitly selected"
                )
            if (
                _task_is_completed(task)
                and step.article_task_id not in continuation_task_ids
                and (
                    selected_tasks is None
                    or step.article_task_id not in selected_tasks
                )
            ):
                raise AssistantPolicyError(
                    "completed article task must be explicitly selected"
                )
            if step.status in {"succeeded", "skipped"}:
                # Explicit revisions must carry immutable completed steps so
                # the repository can compare them byte-for-byte. Their
                # historical expected revision and pinned snapshots are
                # intentionally stale after later steps advance the Task;
                # rebinding them would both fail valid revisions and rewrite
                # the approved audit history.
                bound_steps.append(step)
                continue
            if (
                step.expected_task_revision is not None
                and step.expected_task_revision != task.revision
            ):
                raise AssistantPolicyError("article task revision changed while planning")
        source_input_summary = dict(step.input_summary)
        if step.action_kind == "generate_article":
            if source_input_summary.get("use_evidence_pack", True) is not True:
                raise AssistantPolicyError(
                    "generate_article cannot disable the research Evidence Pack"
                )
            source_input_summary["use_evidence_pack"] = True
        if step.action_kind == "update_project_notes":
            updated_notes, addition, removals = _project_notes_change(
                project.project_notes,
                source_input_summary,
            )
            source_input_summary = {
                "previous_project_notes": project.project_notes,
                "project_notes": updated_notes,
                "notes_to_add": addition,
                "notes_to_remove": removals,
                "expected_project_revision": project.revision,
                "change_summary": source_input_summary.get(
                    "change_summary",
                    "Update project notes",
                ),
            }
            safe_input_summary = _safe_summary(
                source_input_summary,
                string_max_length=30_000,
                allow_empty_strings=True,
            )
        else:
            safe_input_summary = _safe_summary(source_input_summary)
        if dynamic_task_source is not None:
            safe_input_summary["create_task_step_id"] = dynamic_task_source
        summary_limit = 70_000 if step.action_kind == "update_project_notes" else 8_000
        if len(canonical_json(safe_input_summary)) > summary_limit:
            raise AssistantPolicyError("plan input summary is too large")
        bound_steps.append(
            step.model_copy(
                update={
                    "expected_task_revision": task.revision if task else None,
                    "hard_gate": step.hard_gate or requires_human_gate(step.action_kind),
                    "input_summary": safe_input_summary,
                    "pinned_prompt_version": {
                        "project_revision": project.revision,
                        "prompts": [
                            {
                                "kind": prompt.kind,
                                "prompt_id": prompt.prompt_id,
                                "version": prompt.version,
                            }
                            for prompt in project.prompts
                        ],
                    },
                    "pinned_knowledge_snapshot": {
                        "project_revision": project.revision,
                        "sources": [
                            {
                                "source_id": source.source_id,
                                "snapshot_id": source.snapshot_id,
                                "trust_tier": source.trust_tier,
                            }
                            for source in project.knowledge
                        ],
                        "products": project.public_summary().get(
                            "confirmed_products",
                            [],
                        ),
                    },
                    "status": "pending",
                    "background_job_id": None,
                    "retry_count": 0,
                    "output_summary": {},
                    "standardized_error_code": None,
                    "human_gate_confirmed": False,
                }
            )
        )
    bound_plan = plan.model_copy(update={"steps": bound_steps})
    validate_task_selection(bound_plan, context=context)
    return bound_plan


def _selection_key(value: str) -> str:
    """Normalize obvious duplicate topic/keyword/search-intent variants."""

    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = "".join(
        character if character.isalnum() else " "
        for character in normalized
    )
    return " ".join(normalized.split())


def _published_topic_for_step(
    project: AssistantProjectContext,
    step: PlanStep,
) -> Any:
    """Resolve and canonicalize the only allowed source for a new Task."""

    topic_id = str(step.input_summary.get("published_topic_id") or "").strip()
    if not topic_id:
        raise AssistantPolicyError(
            "create_task requires a published topic source"
        )
    source = next(
        (item for item in project.published_topics if item.topic_id == topic_id),
        None,
    )
    if source is None:
        raise AssistantPolicyError(
            "create_task references an unavailable published topic"
        )
    requested_topic = _selection_key(str(step.input_summary.get("topic") or ""))
    if requested_topic != _selection_key(source.topic):
        raise AssistantPolicyError(
            "create_task topic must match the published topic"
        )
    return source


def validate_task_selection(
    plan: PlanDraft,
    *,
    context: AssistantWorkspaceContext,
) -> None:
    """Reject duplicate article intent within one project's planned set.

    A single task can have many sequential workflow steps, so the check is
    performed once per distinct ``(project_id, article_task_id)`` chain. The
    existing Task snapshot is authoritative; the model cannot hide duplicate
    topics or keywords inside a later action summary.
    """

    by_project = {project.project_id: project for project in context.projects}
    chains: dict[tuple[str, str], AssistantTaskContext] = {}
    planned_actions: set[tuple[str, str, str]] = set()
    create_topics: dict[tuple[str, str], str] = {}
    create_keywords: dict[tuple[str, str], str] = {}
    for step in plan.steps:
        if step.status in {"succeeded", "skipped"}:
            continue
        if step.action_kind == "create_task":
            project = by_project.get(step.project_id)
            if project is None:
                raise AssistantPolicyError(
                    "every step must belong to a resolved project"
                )
            source = _published_topic_for_step(project, step)
            topic = _selection_key(source.topic)
            if not topic:
                raise AssistantPolicyError(
                    "create_task requires a public topic"
                )
            topic_key = (step.project_id, topic)
            if topic_key in create_topics:
                raise AssistantPolicyError(
                    "planned Task creations contain duplicate topics"
                )
            create_topics[topic_key] = step.step_id
            keyword = _selection_key(
                str(step.input_summary.get("primary_keyword") or "")
            )
            if keyword:
                keyword_key = (step.project_id, keyword)
                if keyword_key in create_keywords:
                    raise AssistantPolicyError(
                        "planned Task creations contain duplicate primary keywords"
                    )
                create_keywords[keyword_key] = step.step_id
            for existing_task in project.tasks:
                if _selection_key(existing_task.topic) == topic:
                    raise AssistantPolicyError(
                        "create_task duplicates an existing project topic"
                    )
                if keyword and _selection_key(existing_task.primary_keyword) == keyword:
                    raise AssistantPolicyError(
                        "create_task duplicates an existing primary keyword"
                    )
            continue
        if step.action_kind not in TASK_BOUND_ACTION_KINDS:
            continue
        chain_id = str(step.article_task_id or "").strip()
        if not chain_id:
            chain_id = str(
                step.input_summary.get("create_task_step_id") or ""
            ).strip()
        action_key = (step.project_id, chain_id, step.action_kind)
        if (
            chain_id
            and step.status not in {"succeeded", "skipped"}
            and action_key in planned_actions
        ):
            raise AssistantPolicyError(
                "an article workflow chain contains a duplicate action"
            )
        if chain_id and step.status not in {"succeeded", "skipped"}:
            planned_actions.add(action_key)
        if not step.article_task_id:
            source_id = str(step.input_summary.get("create_task_step_id") or "").strip()
            source = next(
                (candidate for candidate in plan.steps if candidate.step_id == source_id),
                None,
            )
            if source is None or source.action_kind != "create_task":
                raise AssistantPolicyError(
                    "every article workflow step must bind an article task"
                )
            # The concrete Task identity is filled transactionally after the
            # preceding create_task step succeeds.
            continue
        project = by_project.get(step.project_id)
        if project is None:
            raise AssistantPolicyError("every step must belong to a resolved project")
        task = next(
            (item for item in project.tasks if item.task_id == step.article_task_id),
            None,
        )
        if task is None:
            raise AssistantPolicyError("plan references an unavailable article task")
        chains.setdefault((step.project_id, step.article_task_id), task)

    seen_topics: dict[tuple[str, str], str] = {}
    seen_keywords: dict[tuple[str, str], str] = {}
    seen_intents: dict[tuple[str, str], str] = {}
    for (project_id, task_id), task in chains.items():
        topic = _selection_key(task.topic)
        keyword = _selection_key(task.primary_keyword)
        intent = _selection_key(
            " ".join(value for value in (task.topic, task.primary_keyword) if value)
        )
        if topic:
            key = (project_id, topic)
            previous = seen_topics.get(key)
            if previous is not None and previous != task_id:
                raise AssistantPolicyError(
                    "planned tasks contain duplicate topics in one project"
                )
            seen_topics[key] = task_id
        if keyword:
            key = (project_id, keyword)
            previous = seen_keywords.get(key)
            if previous is not None and previous != task_id:
                raise AssistantPolicyError(
                    "planned tasks contain duplicate primary keywords in one project"
                )
            seen_keywords[key] = task_id
        if intent:
            key = (project_id, intent)
            previous = seen_intents.get(key)
            if previous is not None and previous != task_id:
                raise AssistantPolicyError(
                    "planned tasks contain duplicate search intent in one project"
                )
            seen_intents[key] = task_id
    _validate_article_evidence_chains(plan)


def requires_confirmation(plan: PlanDraft) -> bool:
    return any(step.action_kind in WRITE_ACTION_KINDS for step in plan.steps)


def requires_human_gate(action_kind: ActionKind) -> bool:
    """Return actions that need a second confirmation after plan approval.

    Title/product/outline selection, research over already-published sources,
    generation, humanization, review, and document rendering are explicitly
    delegable in M1. Publishing a newly discovered source remains governed by
    the Knowledge Agent's own review boundary; the assistant must not turn
    every research run into a pre-emptive approval. Packaging is the formal
    final delivery boundary and therefore always stops for another decision.
    """

    return action_kind == "package_delivery"


__all__ = [
    "ALLOWED_ACTION_KINDS",
    "ASSISTANT_ALLOWED_ACTION_KINDS",
    "AssistantPolicyError",
    "TASK_BOUND_ACTION_KINDS",
    "WRITE_ACTION_KINDS",
    "canonical_json",
    "canonical_plan_hash",
    "bind_plan_context",
    "requires_confirmation",
    "requires_human_gate",
    "sanitize_message",
    "sanitize_public_summary",
    "validate_task_selection",
    "validate_plan_scope",
]
