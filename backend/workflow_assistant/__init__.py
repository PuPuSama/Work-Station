"""Server-only Workflow Assistant M1 primitives.

The package intentionally contains business contracts and PostgreSQL-backed
coordination only.  It does not provide shell, Git, deployment, arbitrary SQL,
or a second local persistence mode.
"""

from .contracts import (
    ActionKind,
    AssistantConversationCreateRequest,
    AssistantMessageRequest,
    PlanDraft,
    PlanStep,
)
from .policy import (
    ALLOWED_ACTION_KINDS,
    canonical_plan_hash,
    sanitize_message,
    validate_plan_scope,
)
from .execution import WorkflowExecutionCoordinator
from .graph import WorkflowAssistantGraph, WorkflowGraphState
from .tools import WorkflowToolInvocation, WorkflowToolRegistry
from .adapters import WorkflowAssistantServiceAdapters
from .runner import WorkflowAssistantRunner

__all__ = [
    "ALLOWED_ACTION_KINDS",
    "ActionKind",
    "AssistantConversationCreateRequest",
    "AssistantMessageRequest",
    "PlanDraft",
    "PlanStep",
    "canonical_plan_hash",
    "sanitize_message",
    "validate_plan_scope",
    "WorkflowExecutionCoordinator",
    "WorkflowAssistantGraph",
    "WorkflowGraphState",
    "WorkflowAssistantRunner",
    "WorkflowAssistantServiceAdapters",
    "WorkflowToolInvocation",
    "WorkflowToolRegistry",
]
