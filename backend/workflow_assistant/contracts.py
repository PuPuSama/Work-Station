from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


ActionKind = Literal[
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
]


ConversationStatus = Literal["active", "expired"]
WorkflowMode = Literal["assistant", "article"]
PlanStatus = Literal[
    "draft",
    "awaiting_confirmation",
    "queued",
    "running",
    "waiting_review",
    "paused",
    "completed",
    "failed",
    "cancelled",
]
StepStatus = Literal[
    "pending",
    "running",
    "waiting_job",
    "waiting_review",
    "succeeded",
    "failed",
    "skipped",
    "cancelled",
]


def _normalized_ids(values: list[str]) -> list[str]:
    normalized = [value.strip() for value in values]
    if any(not value for value in normalized):
        raise ValueError("project ids must not be empty")
    if len(normalized) != len(set(normalized)):
        raise ValueError("project ids must be unique")
    return normalized


class AssistantConversationCreateRequest(BaseModel):
    """Create a private workspace conversation with optional project scope."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )

    title: str = Field(default="New assistant conversation", min_length=1, max_length=160)
    project_ids: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("project_ids")
    @classmethod
    def validate_project_ids(cls, values: list[str]) -> list[str]:
        return _normalized_ids(values)


class AssistantMessageRequest(BaseModel):
    """A user message; request and idempotency identities are client-bound."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )

    content: str = Field(min_length=1, max_length=20_000)
    request_id: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    idempotency_key: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    project_ids: list[str] | None = Field(default=None, max_length=100)
    article_task_ids: list[str] | None = Field(default=None, max_length=500)
    # Keep the legacy discriminator so old clients receive a clear migration
    # error; new assistant clients use the prompt/information lane only.
    workflow_mode: WorkflowMode = "assistant"

    @field_validator("project_ids")
    @classmethod
    def validate_optional_project_ids(cls, values: list[str] | None) -> list[str] | None:
        return _normalized_ids(values) if values is not None else None

    @field_validator("article_task_ids")
    @classmethod
    def validate_article_task_ids(cls, values: list[str] | None) -> list[str] | None:
        return _normalized_ids(values) if values is not None else None


class BatchWritingProjectConfig(BaseModel):
    """One explicit project row in the batch-writing form."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )

    project_id: str = Field(min_length=1, max_length=255)
    article_count: int = Field(ge=1, le=50)


class BatchWritingPlanRequest(BaseModel):
    """Structured input for the deterministic multi-project writing lane."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )

    projects: list[BatchWritingProjectConfig] = Field(
        min_length=1,
        max_length=20,
    )
    writing_instruction: str = Field(default="", max_length=7_000)
    skip_review: bool = False
    concurrency_limit: int = Field(default=5, ge=1, le=32)
    request_id: str = Field(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    idempotency_key: str = Field(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )

    @field_validator("projects")
    @classmethod
    def validate_projects(
        cls,
        values: list[BatchWritingProjectConfig],
    ) -> list[BatchWritingProjectConfig]:
        project_ids = [item.project_id for item in values]
        if len(project_ids) != len(set(project_ids)):
            raise ValueError("batch writing projects must be unique")
        return values

    @model_validator(mode="after")
    def validate_total_articles(self) -> "BatchWritingPlanRequest":
        total = sum(item.article_count for item in self.projects)
        if total > 60:
            raise ValueError("batch writing may contain at most 60 articles")
        return self


class PlanStep(BaseModel):
    """One explicit, project-bound assistant action.

    ``action_kind`` is deliberately a closed discriminator.  A model may not
    invent an operation name or smuggle a tool name through ``input_summary``.
    """

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )

    step_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    sequence: int = Field(ge=1, le=1000)
    action_kind: ActionKind
    project_id: str = Field(min_length=1, max_length=255)
    article_task_id: str | None = Field(default=None, min_length=1, max_length=255)
    expected_task_revision: int | None = Field(default=None, ge=0)
    pinned_prompt_version: dict[str, Any] = Field(default_factory=dict)
    pinned_knowledge_snapshot: dict[str, Any] = Field(default_factory=dict)
    input_summary: dict[str, Any] = Field(default_factory=dict)
    hard_gate: bool = False
    # Execution fields are server-owned.  Planner input uses their defaults;
    # plan responses populate them from PostgreSQL step rows.
    status: StepStatus = "pending"
    background_job_id: str | None = None
    retry_count: int = Field(default=0, ge=0)
    output_summary: dict[str, Any] = Field(default_factory=dict)
    standardized_error_code: str | None = None
    human_gate_confirmed: bool = False
    # Server-owned timestamp used by the plan overview; planner payloads keep
    # the field unset and therefore do not persist it in normalized plans.
    updated_at: str | None = None

    @field_validator("article_task_id", mode="before")
    @classmethod
    def normalize_optional_article_task_id(cls, value: object) -> object:
        """Treat a planner's empty optional Task binding as not-yet-bound.

        The planner contract explicitly tells the model to leave this field
        empty for steps whose Task will be allocated by a preceding
        ``create_task`` step.  Structured-output providers commonly encode
        that instruction as ``""`` instead of JSON ``null``.  Normalizing at
        the contract boundary preserves the later server-side binding and
        scope checks without accepting an arbitrary Task identity.
        """

        if isinstance(value, str) and not value.strip():
            return None
        return value


class PlanDraft(BaseModel):
    """The only planner output accepted by the assistant execution layer."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )

    title: str = Field(min_length=1, max_length=200)
    natural_language_request: str = Field(min_length=1, max_length=20_000)
    project_ids: list[str] = Field(min_length=1, max_length=100)
    steps: list[PlanStep] = Field(min_length=1, max_length=1000)
    concurrency_limit: int = Field(default=5, ge=1, le=32)
    budget_warning: bool = False
    attention_state: Literal["none", "user_confirmation", "error", "unread"] = "user_confirmation"

    @field_validator("project_ids")
    @classmethod
    def validate_plan_project_ids(cls, values: list[str]) -> list[str]:
        return _normalized_ids(values)

    @field_validator("steps")
    @classmethod
    def validate_step_sequence(cls, values: list[PlanStep]) -> list[PlanStep]:
        sequences = [step.sequence for step in values]
        if len(sequences) != len(set(sequences)):
            raise ValueError("plan step sequences must be unique")
        step_ids = [step.step_id for step in values]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("plan step ids must be unique")
        if sorted(sequences) != list(range(1, len(values) + 1)):
            raise ValueError("plan step sequences must start at one and be contiguous")
        return values

    def normalized_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


class AssistantConversationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    conversation_id: str
    title: str
    project_ids: list[str]
    created_at: str
    updated_at: str
    expires_at: str
    messages: list["AssistantMessageResponse"] = Field(default_factory=list)


class AssistantMessageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    message_id: str
    sequence: int
    role: Literal["user", "assistant", "system"]
    content: str
    request_id: str
    created_at: str


class WorkflowPlanResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    plan_id: str
    conversation_id: str
    title: str
    natural_language_request: str
    plan_hash: str
    revision: int
    status: PlanStatus
    project_ids: list[str]
    paused_project_ids: list[str] = Field(default_factory=list)
    steps: list[PlanStep]
    concurrency_limit: int
    budget_warning: bool
    attention_state: str
    approved_by: str | None = None
    approved_at: str | None = None


class PlanCommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    revision: int = Field(ge=0)
    plan_hash: str | None = Field(default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    project_ids: list[str] | None = Field(default=None, max_length=100)

    @field_validator("project_ids")
    @classmethod
    def validate_command_project_ids(
        cls,
        values: list[str] | None,
    ) -> list[str] | None:
        return _normalized_ids(values) if values is not None else None


class PlanRevisionRequest(PlanCommandRequest):
    natural_language_request: str = Field(min_length=1, max_length=20_000)
    plan: PlanDraft | None = None


class AttentionCountResponse(BaseModel):
    count: int = Field(ge=0)


class WorkflowPlanSummary(BaseModel):
    """Lightweight plan summary without full steps and knowledge snapshots."""

    model_config = ConfigDict(from_attributes=True)

    plan_id: str
    conversation_id: str
    title: str
    natural_language_request: str
    plan_hash: str
    revision: int
    status: PlanStatus
    project_ids: list[str]
    paused_project_ids: list[str] = Field(default_factory=list)
    step_count: int = Field(ge=0)
    pending_step_count: int = Field(ge=0)
    concurrency_limit: int
    budget_warning: bool
    attention_state: str
    approved_by: str | None = None
    approved_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


PlanDraft.model_rebuild()
AssistantConversationResponse.model_rebuild()


__all__ = [
    "ActionKind",
    "BatchWritingPlanRequest",
    "BatchWritingProjectConfig",
    "AssistantConversationCreateRequest",
    "AssistantConversationResponse",
    "AssistantMessageRequest",
    "AssistantMessageResponse",
    "AttentionCountResponse",
    "PlanCommandRequest",
    "PlanDraft",
    "PlanRevisionRequest",
    "PlanStep",
    "WorkflowPlanResponse",
    "WorkflowPlanSummary",
    "WorkflowMode",
]
