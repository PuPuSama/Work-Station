from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from services.access_control import (
    ActorIdentity,
    ProjectAccessDenied,
    ProjectAccessService,
)

from .context import AssistantWorkspaceContext
from .contracts import ActionKind
from .policy import (
    ALLOWED_ACTION_KINDS,
    WRITE_ACTION_KINDS,
    requires_human_gate,
    sanitize_public_summary,
)


class WorkflowToolError(RuntimeError):
    """A typed assistant tool could not complete its business operation."""


class WorkflowToolUnavailable(WorkflowToolError):
    """The existing underlying Server service is not available."""


class WorkflowToolAuthorizationError(WorkflowToolError):
    """The Actor lost permission before a tool was invoked."""


class WorkflowToolHumanGateRequired(WorkflowToolError):
    """The operation is explicitly blocked until a human confirms it."""


@dataclass(frozen=True, slots=True)
class WorkflowToolInvocation:
    actor: ActorIdentity
    plan_id: str
    step_id: str
    action_kind: ActionKind
    project_id: str
    article_task_id: str | None
    expected_task_revision: int | None
    input_summary: Mapping[str, Any]
    pinned_prompt_version: Mapping[str, Any]
    pinned_knowledge_snapshot: Mapping[str, Any]
    hard_gate: bool = False
    confirmed: bool = False
    human_gate_confirmed: bool = False


class WorkflowToolHandler(Protocol):
    def __call__(self, invocation: WorkflowToolInvocation) -> Mapping[str, Any]: ...


_ACTION_PERMISSIONS = {
    "list_projects": "project.view",
    "list_tasks": "project.view",
    "read_project_context": "project.view",
    "evidence_query": "project.view",
    "read_plan_status": "project.view",
    "create_task": "article.edit",
    "generate_titles": "article.edit",
    "select_title": "article.edit",
    "generate_products": "article.edit",
    "confirm_products": "article.edit",
    "generate_outline": "article.edit",
    "start_research": "knowledge.publish",
    "generate_article": "article.edit",
    "humanize": "article.edit",
    "review": "article.review",
    "restore_links": "article.edit",
    "prepare_images": "article.edit",
    "export_docx": "article.deliver",
    "generate_tdk": "article.deliver",
    "package_delivery": "article.deliver",
}


class WorkflowToolRegistry:
    """Closed action-to-service registry; no dynamic tool names are accepted."""

    def __init__(
        self,
        *,
        access: ProjectAccessService,
        handlers: Mapping[ActionKind, WorkflowToolHandler] | None = None,
    ) -> None:
        self._access = access
        self._handlers: dict[ActionKind, WorkflowToolHandler] = {}
        for action_kind, handler in (handlers or {}).items():
            if action_kind not in ALLOWED_ACTION_KINDS:
                raise ValueError("unsupported workflow tool action")
            self._handlers[action_kind] = handler

    def register(self, action_kind: ActionKind, handler: WorkflowToolHandler) -> None:
        if action_kind not in ALLOWED_ACTION_KINDS:
            raise ValueError("unsupported workflow tool action")
        self._handlers[action_kind] = handler

    def has_handler(self, action_kind: ActionKind) -> bool:
        return action_kind in self._handlers

    def invoke(self, invocation: WorkflowToolInvocation) -> dict[str, Any]:
        if invocation.action_kind not in ALLOWED_ACTION_KINDS:
            raise WorkflowToolError("unsupported workflow tool action")
        if invocation.action_kind in WRITE_ACTION_KINDS and not invocation.confirmed:
            raise WorkflowToolHumanGateRequired("plan confirmation is required")
        if (
            (requires_human_gate(invocation.action_kind) or invocation.hard_gate)
            and not invocation.human_gate_confirmed
        ):
            raise WorkflowToolHumanGateRequired("human confirmation is required")
        permission = _ACTION_PERMISSIONS[invocation.action_kind]
        try:
            self._access.require(
                invocation.actor,
                invocation.project_id,
                permission,  # type: ignore[arg-type]
            )
        except (ProjectAccessDenied, ValueError) as exc:
            raise WorkflowToolAuthorizationError("project access denied") from exc
        handler = self._handlers.get(invocation.action_kind)
        if handler is None:
            raise WorkflowToolUnavailable(
                f"tool {invocation.action_kind} is not wired to a Server service"
            )
        try:
            result = handler(invocation)
        except WorkflowToolError:
            raise
        except Exception as exc:
            raise WorkflowToolError("workflow tool failed") from exc
        if not isinstance(result, Mapping):
            raise WorkflowToolError("workflow tool returned an invalid result")
        try:
            return sanitize_public_summary(result)
        except Exception as exc:
            raise WorkflowToolError("workflow tool returned an unsafe result") from exc


def build_read_only_tool_handlers(
    *,
    context: AssistantWorkspaceContext,
    plan_status: Callable[[str], Mapping[str, Any]] | None = None,
) -> dict[ActionKind, WorkflowToolHandler]:
    """Build safe, already-resolved read projections for one plan context."""

    by_project = {project.project_id: project for project in context.projects}

    def project_context(invocation: WorkflowToolInvocation) -> Mapping[str, Any]:
        try:
            return by_project[invocation.project_id].public_summary()
        except KeyError as exc:
            raise WorkflowToolAuthorizationError("project is outside the plan context") from exc

    def list_tasks(invocation: WorkflowToolInvocation) -> Mapping[str, Any]:
        summary = project_context(invocation)
        return {"project_id": invocation.project_id, "tasks": summary["tasks"]}

    def evidence_query(invocation: WorkflowToolInvocation) -> Mapping[str, Any]:
        summary = project_context(invocation)
        # Evidence queries intentionally exclude official blogs and other
        # reference-only sources.  Those remain visible under the separate
        # writing_references projection for article context.
        return {
            "project_id": invocation.project_id,
            "evidence": summary["evidence_knowledge"],
        }

    def list_projects(_invocation: WorkflowToolInvocation) -> Mapping[str, Any]:
        return {
            "projects": [
                {
                    "project_id": project.project_id,
                    "customer_name": project.customer_name,
                    "official_domain": project.official_domain,
                }
                for project in context.projects
            ]
        }

    handlers: dict[ActionKind, WorkflowToolHandler] = {
        "list_projects": list_projects,
        "list_tasks": list_tasks,
        "read_project_context": project_context,
        "evidence_query": evidence_query,
    }
    if plan_status is not None:
        handlers["read_plan_status"] = lambda invocation: dict(
            plan_status(invocation.plan_id)
        )
    return handlers


__all__ = [
    "WorkflowToolAuthorizationError",
    "WorkflowToolError",
    "WorkflowToolHandler",
    "WorkflowToolHumanGateRequired",
    "WorkflowToolInvocation",
    "WorkflowToolRegistry",
    "WorkflowToolUnavailable",
    "build_read_only_tool_handlers",
]
