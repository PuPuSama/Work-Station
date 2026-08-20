from __future__ import annotations

import copy
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Literal

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine, RowMapping
from sqlalchemy.exc import IntegrityError

from server_schema import (
    article_tasks,
    assistant_attachments,
    assistant_conversations,
    assistant_messages,
    assistant_usage_events,
    background_jobs,
    workflow_plan_events,
    workflow_plan_projects,
    workflow_plan_steps,
    workflow_plans,
)
from services.access_control import ActorIdentity

from .contracts import PlanDraft, PlanStatus, PlanStep, StepStatus
from .policy import (
    canonical_json,
    canonical_plan_hash,
    requires_confirmation,
    sanitize_message,
    sanitize_public_summary,
)


class WorkflowAssistantRepositoryError(RuntimeError):
    """Base error for assistant persistence failures."""


class WorkflowAssistantNotFound(WorkflowAssistantRepositoryError):
    """The requested private assistant resource does not exist."""


class WorkflowAssistantConflict(WorkflowAssistantRepositoryError):
    """A revision or idempotency identity is stale or already conflicting."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "workflow_conflict",
        current_revision: int | None = None,
        current_plan_hash: str | None = None,
        current_steps: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        super().__init__(message)
        self.code = str(code).strip() or "workflow_conflict"
        self.current_revision = current_revision
        self.current_plan_hash = current_plan_hash
        # Keep the conflict projection deliberately smaller than a plan
        # response. It contains only step identity/status fields; prompt
        # snapshots, input summaries, outputs, and provider data never cross
        # the 409 boundary.
        self.current_steps = tuple(
            {
                str(key): value
                for key, value in dict(step).items()
                if str(key)
                in {
                    "step_id",
                    "sequence",
                    "action_kind",
                    "project_id",
                    "article_task_id",
                    "status",
                    "retry_count",
                    "standardized_error_code",
                }
            }
            for step in current_steps
        )


class WorkflowAssistantForbidden(WorkflowAssistantRepositoryError):
    """The current actor cannot access the private assistant resource."""


@dataclass(frozen=True, slots=True)
class AssistantMessage:
    message_id: str
    sequence: int
    role: Literal["user", "assistant", "system"]
    content: str
    request_id: str
    idempotency_key: str
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class AssistantConversation:
    organization_id: str
    conversation_id: str
    creator_user_id: str
    title: str
    project_ids: tuple[str, ...]
    created_at: datetime | None = None
    updated_at: datetime | None = None
    expires_at: datetime | None = None
    messages: tuple[AssistantMessage, ...] = ()


@dataclass(frozen=True, slots=True)
class WorkflowPlanStep:
    step_id: str
    sequence: int
    action_kind: str
    project_id: str
    article_task_id: str | None
    expected_task_revision: int | None
    pinned_prompt_version: dict[str, Any]
    pinned_knowledge_snapshot: dict[str, Any]
    status: StepStatus
    background_job_id: str | None
    retry_count: int
    hard_gate: bool
    human_gate_confirmed: bool
    input_summary: dict[str, Any]
    output_summary: dict[str, Any]
    standardized_error_code: str | None


@dataclass(frozen=True, slots=True)
class WorkflowPlan:
    organization_id: str
    plan_id: str
    creator_user_id: str
    conversation_id: str
    title: str
    natural_language_request: str
    normalized_plan: dict[str, Any]
    plan_hash: str
    revision: int
    status: PlanStatus
    project_ids: tuple[str, ...]
    paused_project_ids: tuple[str, ...]
    steps: tuple[WorkflowPlanStep, ...]
    concurrency_limit: int
    budget_warning: bool
    attention_state: str
    approved_by: str | None
    approved_at: datetime | None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    source_idempotency_key: str | None = None


@dataclass(frozen=True, slots=True)
class WorkflowPlanEvent:
    plan_id: str
    sequence: int
    event_kind: str
    public_payload: dict[str, Any]
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class AssistantUsageEvent:
    usage_event_id: str
    user_id: str
    project_id: str | None
    plan_id: str | None
    provider: str
    model: str
    operation_kind: str
    input_tokens: int
    output_tokens: int
    estimated_cost: Decimal | None
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class WorkflowExecutionCandidate:
    organization_id: str
    creator_user_id: str
    plan_id: str
    status: PlanStatus


_SERVER_JOB_OPERATIONS = {
    "generate_titles": "titles",
    "generate_products": "products",
    "generate_outline": "outline",
    "start_research": "knowledge_research",
    "humanize": "humanize",
    "review": "seo_review",
    "restore_links": "restore_links",
}
_SERVER_JOB_RECOVERY_WINDOW = timedelta(minutes=5)
_SERVER_JOB_CLOCK_SKEW = timedelta(minutes=1)
_EVENT_STEP_ID_LIMIT = 50


def _server_job_operation(
    action_kind: str,
    input_summary: Mapping[str, Any],
) -> str | None:
    if action_kind == "generate_article":
        requested = str(input_summary.get("operation") or "article").strip()
        return requested if requested in {"article", "rewrite_article"} else None
    return _SERVER_JOB_OPERATIONS.get(action_kind)


def _signed_advisory_key(value: bytes) -> int:
    return int.from_bytes(value, byteorder="big", signed=True)


def _required(value: str, field: str, *, max_length: int = 255) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field} is required")
    if len(normalized) > max_length:
        raise ValueError(f"{field} is too long")
    return normalized


def _aware(value: datetime | None, *, days: int = 30) -> datetime:
    if value is None:
        return datetime.now(timezone.utc) + timedelta(days=days)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value


def _json_dict(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return copy.deepcopy(dict(value))


def _json_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        return ()
    return tuple(
        str(item).strip()
        for item in value
        if str(item).strip()
    )


def _bounded_step_id_event_projection(
    prefix: str,
    step_ids: Sequence[str],
) -> dict[str, Any]:
    """Keep revision audit payloads within the public-summary list bound."""

    normalized = sorted({str(step_id).strip() for step_id in step_ids if str(step_id).strip()})
    return {
        f"{prefix}_step_count": len(normalized),
        f"{prefix}_step_ids": normalized[:_EVENT_STEP_ID_LIMIT],
        f"{prefix}_step_ids_truncated": len(normalized) > _EVENT_STEP_ID_LIMIT,
    }


def _current_plan_steps(
    connection: Connection,
    *,
    organization_id: str,
    plan_id: str,
) -> tuple[dict[str, Any], ...]:
    """Return a bounded, non-sensitive step projection for a CAS conflict."""

    rows = connection.execute(
        sa.select(
            workflow_plan_steps.c.step_id,
            workflow_plan_steps.c.sequence,
            workflow_plan_steps.c.action_kind,
            workflow_plan_steps.c.project_id,
            workflow_plan_steps.c.article_task_id,
            workflow_plan_steps.c.status,
            workflow_plan_steps.c.retry_count,
            workflow_plan_steps.c.standardized_error_code,
        )
        .where(
            workflow_plan_steps.c.organization_id == organization_id,
            workflow_plan_steps.c.plan_id == plan_id,
        )
        .order_by(workflow_plan_steps.c.sequence)
        .limit(100)
    ).mappings().all()
    return tuple(
        {
            "step_id": str(row["step_id"]),
            "sequence": int(row["sequence"]),
            "action_kind": str(row["action_kind"]),
            "project_id": str(row["project_id"]),
            "article_task_id": (
                str(row["article_task_id"])
                if row["article_task_id"] is not None
                else None
            ),
            "status": str(row["status"]),
            "retry_count": int(row["retry_count"]),
            "standardized_error_code": (
                str(row["standardized_error_code"])
                if row["standardized_error_code"] is not None
                else None
            ),
        }
        for row in rows
    )


class PostgresWorkflowAssistantRepository:
    """PostgreSQL source of truth for private conversations and plans."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def create_conversation(
        self,
        *,
        actor: ActorIdentity,
        title: str,
        project_ids: Sequence[str] = (),
        conversation_id: str | None = None,
        expires_at: datetime | None = None,
    ) -> AssistantConversation:
        conversation_id = _required(
            conversation_id or f"asc_{uuid.uuid4().hex}",
            "conversation_id",
            max_length=128,
        )
        title = _required(title, "title", max_length=160)
        normalized_projects = tuple(
            dict.fromkeys(_required(item, "project_id") for item in project_ids)
        )
        expires_at = _aware(expires_at)
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    assistant_conversations.insert().values(
                        organization_id=actor.organization_id,
                        conversation_id=conversation_id,
                        creator_user_id=actor.user_id,
                        title=title,
                        last_project_ids=list(normalized_projects),
                        expires_at=expires_at,
                    )
                )
                result = self._get_conversation_in_connection(
                    connection,
                    actor,
                    conversation_id,
                )
                if result is None:
                    raise WorkflowAssistantRepositoryError(
                        "conversation disappeared during creation"
                    )
                return result
        except IntegrityError as exc:
            raise WorkflowAssistantConflict(
                "conversation identity already exists"
            ) from exc

    def list_conversations(
        self,
        *,
        actor: ActorIdentity,
        limit: int = 50,
    ) -> tuple[AssistantConversation, ...]:
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(assistant_conversations)
                .where(
                    assistant_conversations.c.organization_id == actor.organization_id,
                    assistant_conversations.c.creator_user_id == actor.user_id,
                    assistant_conversations.c.expires_at > sa.func.now(),
                )
                .order_by(
                    assistant_conversations.c.updated_at.desc(),
                    assistant_conversations.c.conversation_id,
                )
                .limit(limit)
            ).mappings().all()
            return tuple(self._conversation_from_row(row) for row in rows)

    def get_conversation(
        self,
        *,
        actor: ActorIdentity,
        conversation_id: str,
        include_messages: bool = True,
    ) -> AssistantConversation:
        conversation_id = _required(conversation_id, "conversation_id")
        with self._engine.connect() as connection:
            result = self._get_conversation_in_connection(
                connection,
                actor,
                conversation_id,
                include_messages=include_messages,
            )
            if result is None:
                raise WorkflowAssistantNotFound("conversation not found")
            return result

    def update_conversation_scope(
        self,
        *,
        actor: ActorIdentity,
        conversation_id: str,
        project_ids: Sequence[str],
    ) -> AssistantConversation:
        """Persist the latest project scope selected in the workspace UI."""

        conversation_id = _required(conversation_id, "conversation_id")
        normalized_projects = tuple(
            dict.fromkeys(_required(item, "project_id") for item in project_ids)
        )
        with self._engine.begin() as connection:
            if self._lock_conversation(connection, actor, conversation_id) is None:
                raise WorkflowAssistantNotFound("conversation not found")
            connection.execute(
                assistant_conversations.update()
                .where(
                    assistant_conversations.c.organization_id == actor.organization_id,
                    assistant_conversations.c.conversation_id == conversation_id,
                )
                .values(last_project_ids=list(normalized_projects), updated_at=sa.func.now())
            )
            result = self._get_conversation_in_connection(
                connection,
                actor,
                conversation_id,
                include_messages=False,
            )
            if result is None:
                raise WorkflowAssistantRepositoryError("conversation disappeared during scope update")
            return result

    def append_message(
        self,
        *,
        actor: ActorIdentity,
        conversation_id: str,
        role: Literal["user", "assistant", "system"],
        content: str,
        request_id: str,
        idempotency_key: str,
    ) -> AssistantMessage:
        conversation_id = _required(conversation_id, "conversation_id")
        request_id = _required(request_id, "request_id", max_length=128)
        idempotency_key = _required(idempotency_key, "idempotency_key", max_length=128)
        content = sanitize_message(content)
        if role not in {"user", "assistant", "system"}:
            raise ValueError("assistant message role is unsupported")
        with self._engine.begin() as connection:
            conversation = self._lock_conversation(
                connection,
                actor,
                conversation_id,
            )
            if conversation is None:
                raise WorkflowAssistantNotFound("conversation not found")
            existing = connection.execute(
                sa.select(assistant_messages)
                .where(
                    assistant_messages.c.organization_id == actor.organization_id,
                    assistant_messages.c.conversation_id == conversation_id,
                    assistant_messages.c.idempotency_key == idempotency_key,
                )
                .with_for_update()
            ).mappings().one_or_none()
            if existing is not None:
                if (
                    str(existing["role"]) != role
                    or str(existing["sanitized_content"]) != content
                    or str(existing["request_id"]) != request_id
                ):
                    raise WorkflowAssistantConflict(
                        "message idempotency key already has different content"
                    )
                return self._message_from_row(existing)
            next_sequence = int(
                connection.execute(
                    sa.select(
                        sa.func.coalesce(
                            sa.func.max(assistant_messages.c.sequence),
                            0,
                        )
                    ).where(
                        assistant_messages.c.organization_id == actor.organization_id,
                        assistant_messages.c.conversation_id == conversation_id,
                    )
                ).scalar_one()
            ) + 1
            message_id = f"asm_{uuid.uuid4().hex}"
            connection.execute(
                assistant_messages.insert().values(
                    organization_id=actor.organization_id,
                    conversation_id=conversation_id,
                    message_id=message_id,
                    sequence=next_sequence,
                    role=role,
                    sanitized_content=content,
                    request_id=request_id,
                    idempotency_key=idempotency_key,
                )
            )
            connection.execute(
                assistant_conversations.update()
                .where(
                    assistant_conversations.c.organization_id == actor.organization_id,
                    assistant_conversations.c.conversation_id == conversation_id,
                )
                .values(updated_at=sa.func.now())
            )
            row = connection.execute(
                sa.select(assistant_messages).where(
                    assistant_messages.c.organization_id == actor.organization_id,
                    assistant_messages.c.conversation_id == conversation_id,
                    assistant_messages.c.message_id == message_id,
                )
            ).mappings().one()
            return self._message_from_row(row)

    def get_message_by_idempotency(
        self,
        *,
        actor: ActorIdentity,
        conversation_id: str,
        idempotency_key: str,
    ) -> AssistantMessage | None:
        """Read one private message identity before replaying a request."""

        conversation_id = _required(conversation_id, "conversation_id")
        idempotency_key = _required(idempotency_key, "idempotency_key", max_length=128)
        with self._engine.connect() as connection:
            if self._get_conversation_in_connection(
                connection,
                actor,
                conversation_id,
                include_messages=False,
            ) is None:
                raise WorkflowAssistantNotFound("conversation not found")
            row = connection.execute(
                sa.select(assistant_messages).where(
                    assistant_messages.c.organization_id == actor.organization_id,
                    assistant_messages.c.conversation_id == conversation_id,
                    assistant_messages.c.idempotency_key == idempotency_key,
                )
            ).mappings().one_or_none()
            return self._message_from_row(row) if row is not None else None

    def create_plan(
        self,
        *,
        actor: ActorIdentity,
        conversation_id: str,
        plan: PlanDraft,
        authorization_snapshot: Mapping[str, Mapping[str, Any]] | None = None,
        plan_id: str | None = None,
        source_idempotency_key: str | None = None,
    ) -> WorkflowPlan:
        conversation_id = _required(conversation_id, "conversation_id")
        plan_id = _required(plan_id or f"wfp_{uuid.uuid4().hex}", "plan_id")
        source_idempotency_key = (
            _required(source_idempotency_key, "source_idempotency_key", max_length=128)
            if source_idempotency_key is not None
            else None
        )
        normalized = plan.normalized_payload()
        plan_hash = canonical_plan_hash(plan)
        needs_confirmation = requires_confirmation(plan)
        initial_status: PlanStatus = (
            "awaiting_confirmation" if needs_confirmation else "draft"
        )
        snapshot = authorization_snapshot or {}
        with self._engine.begin() as connection:
            conversation = self._lock_conversation(connection, actor, conversation_id)
            if conversation is None:
                raise WorkflowAssistantNotFound("conversation not found")
            if source_idempotency_key is not None:
                existing_plan_id = connection.execute(
                    sa.select(workflow_plans.c.plan_id)
                    .where(
                        workflow_plans.c.organization_id == actor.organization_id,
                        workflow_plans.c.creator_user_id == actor.user_id,
                        workflow_plans.c.conversation_id == conversation_id,
                        workflow_plans.c.source_idempotency_key
                        == source_idempotency_key,
                    )
                    .limit(1)
                ).scalar_one_or_none()
                if existing_plan_id is not None:
                    existing_plan = self._get_plan_in_connection(
                        connection,
                        actor,
                        str(existing_plan_id),
                    )
                    if existing_plan is None:
                        raise WorkflowAssistantRepositoryError(
                            "idempotent workflow plan disappeared"
                        )
                    return existing_plan
            connection.execute(
                workflow_plans.insert().values(
                    organization_id=actor.organization_id,
                    plan_id=plan_id,
                    creator_user_id=actor.user_id,
                    conversation_id=conversation_id,
                    source_idempotency_key=source_idempotency_key,
                    natural_language_request=plan.natural_language_request,
                    normalized_plan=normalized,
                    plan_hash=plan_hash,
                    revision=0,
                    status=initial_status,
                    concurrency_limit=plan.concurrency_limit,
                    budget_warning=plan.budget_warning,
                    # Attention is a server-owned projection.  A planner or
                    # client cannot suppress the write-plan confirmation
                    # marker by supplying ``attention_state=none``.
                    attention_state=(
                        "user_confirmation" if needs_confirmation else "none"
                    ),
                )
            )
            self._insert_plan_children(
                connection,
                actor=actor,
                plan_id=plan_id,
                plan=plan,
                authorization_snapshot=snapshot,
            )
            self._insert_event(
                connection,
                actor=actor,
                plan_id=plan_id,
                event_kind="plan_created",
                public_payload={
                    "revision": 0,
                    "status": initial_status,
                    "requires_confirmation": needs_confirmation,
                },
            )
            result = self._get_plan_in_connection(connection, actor, plan_id)
            if result is None:
                raise WorkflowAssistantRepositoryError("plan disappeared during creation")
            return result

    def get_plan_by_idempotency(
        self,
        *,
        actor: ActorIdentity,
        conversation_id: str,
        source_idempotency_key: str,
    ) -> WorkflowPlan | None:
        """Find the durable plan created for one assistant message retry."""

        conversation_id = _required(conversation_id, "conversation_id")
        source_idempotency_key = _required(
            source_idempotency_key,
            "source_idempotency_key",
            max_length=128,
        )
        with self._engine.connect() as connection:
            plan_id = connection.execute(
                sa.select(workflow_plans.c.plan_id)
                .where(
                    workflow_plans.c.organization_id == actor.organization_id,
                    workflow_plans.c.creator_user_id == actor.user_id,
                    workflow_plans.c.conversation_id == conversation_id,
                    workflow_plans.c.source_idempotency_key
                    == source_idempotency_key,
                )
                .limit(1)
            ).scalar_one_or_none()
            if plan_id is None:
                return None
            return self._get_plan_in_connection(actor=actor, connection=connection, plan_id=str(plan_id))

    def get_plan(self, *, actor: ActorIdentity, plan_id: str) -> WorkflowPlan:
        plan_id = _required(plan_id, "plan_id")
        with self._engine.connect() as connection:
            result = self._get_plan_in_connection(connection, actor, plan_id)
            if result is None:
                raise WorkflowAssistantNotFound("plan not found")
            return result

    def get_latest_plan_for_conversation(
        self,
        *,
        actor: ActorIdentity,
        conversation_id: str,
    ) -> WorkflowPlan | None:
        conversation_id = _required(conversation_id, "conversation_id")
        with self._engine.connect() as connection:
            row = connection.execute(
                sa.select(workflow_plans.c.plan_id)
                .where(
                    workflow_plans.c.organization_id == actor.organization_id,
                    workflow_plans.c.creator_user_id == actor.user_id,
                    workflow_plans.c.conversation_id == conversation_id,
                )
                .order_by(
                    workflow_plans.c.updated_at.desc(),
                    workflow_plans.c.plan_id.desc(),
                )
                .limit(1)
            ).scalar_one_or_none()
            if row is None:
                return None
            return self._get_plan_in_connection(connection, actor, str(row))

    @contextmanager
    def plan_execution_lock(
        self,
        *,
        actor: ActorIdentity,
        plan_id: str,
    ) -> Iterator[bool]:
        """Try to own one plan's execution across all application processes.

        The session-scoped advisory lock stays held while the coordinator may
        call several existing Server services. A losing runner does no
        recovery or dispatch work and simply rediscovers the durable plan on
        its next poll.
        """

        plan_id = _required(plan_id, "plan_id")
        identity = uuid.uuid5(
            uuid.NAMESPACE_URL,
            "\x1f".join(
                (
                    "workflow-assistant-plan-execution",
                    actor.organization_id,
                    actor.user_id,
                    plan_id,
                )
            ),
        ).bytes
        key_a = _signed_advisory_key(identity[:4])
        key_b = _signed_advisory_key(identity[4:8])
        with self._engine.connect() as connection:
            acquired = bool(
                connection.execute(
                    sa.select(sa.func.pg_try_advisory_lock(key_a, key_b))
                ).scalar_one()
            )
            # Advisory locks are session-scoped, so close the implicit
            # transaction before potentially long-running tool execution.
            connection.commit()
            try:
                yield acquired
            finally:
                if acquired:
                    connection.execute(
                        sa.select(sa.func.pg_advisory_unlock(key_a, key_b))
                    )
                    connection.commit()

    def claim_step(
        self,
        *,
        actor: ActorIdentity,
        plan_id: str,
        step_id: str,
        allow_unconfirmed_read_only: bool = False,
    ) -> bool:
        """Claim one pending step exactly once for a worker attempt.

        The HTTP read-only path runs before plan confirmation and opts into
        the two draft states explicitly.  Durable write workers keep the
        default queued/running boundary, so an unconfirmed write plan cannot
        be dispatched by accidentally reusing this repository primitive.
        """

        plan_id = _required(plan_id, "plan_id")
        step_id = _required(step_id, "step_id")
        with self._engine.begin() as connection:
            plan = self._lock_plan(connection, actor, plan_id)
            if plan is None:
                raise WorkflowAssistantNotFound("plan not found")
            # The plan row is locked before the step update.  This makes a
            # pause/cancel command a real dispatch boundary instead of a UI
            # hint: once the command commits, no new pending step can be
            # claimed by a worker that was racing the command.
            allowed_statuses = {"queued", "running"}
            if allow_unconfirmed_read_only:
                allowed_statuses.update({"draft", "awaiting_confirmation"})
            if str(plan["status"]) not in allowed_statuses:
                return False
            result = connection.execute(
                workflow_plan_steps.update()
                .where(
                    workflow_plan_steps.c.organization_id == actor.organization_id,
                    workflow_plan_steps.c.plan_id == plan_id,
                    workflow_plan_steps.c.step_id == step_id,
                    workflow_plan_steps.c.status == "pending",
                )
                .values(status="running", updated_at=sa.func.now())
            )
            return bool(result.rowcount)

    def finish_step(
        self,
        *,
        actor: ActorIdentity,
        plan_id: str,
        step_id: str,
        status: StepStatus,
        output_summary: Mapping[str, Any] | None = None,
        standardized_error_code: str | None = None,
        background_job_id: str | None = None,
        retry_count: int | None = None,
    ) -> bool:
        """Commit a step result only from the claimed running revision."""

        if status not in {"succeeded", "failed", "waiting_job", "waiting_review", "cancelled", "skipped"}:
            raise ValueError("step result status is unsupported")
        if retry_count is not None and (
            isinstance(retry_count, bool)
            or not isinstance(retry_count, int)
            or retry_count < 0
        ):
            raise ValueError("retry_count must not be negative")
        safe_output = sanitize_public_summary(output_summary or {})
        values: dict[str, Any] = {
            "status": status,
            "output_summary": safe_output,
            "standardized_error_code": standardized_error_code,
            "background_job_id": background_job_id,
            "updated_at": sa.func.now(),
        }
        if retry_count is not None:
            values["retry_count"] = retry_count
        with self._engine.begin() as connection:
            plan = self._lock_plan(connection, actor, plan_id)
            if plan is None:
                raise WorkflowAssistantNotFound("plan not found")
            result = connection.execute(
                workflow_plan_steps.update()
                .where(
                    workflow_plan_steps.c.organization_id == actor.organization_id,
                    workflow_plan_steps.c.plan_id == plan_id,
                    workflow_plan_steps.c.step_id == step_id,
                    workflow_plan_steps.c.status.in_(("running", "waiting_job")),
                )
                .values(**values)
            )
            return bool(result.rowcount)

    @staticmethod
    def _recoverable_server_job_ids(
        connection: Connection,
        *,
        actor: ActorIdentity,
        plan_id: str,
        step: RowMapping,
        before: datetime | None,
    ) -> tuple[str, ...]:
        action_kind = str(step["action_kind"])
        operation = _server_job_operation(
            action_kind,
            _json_dict(step["input_summary"]),
        )
        task_id = str(step["article_task_id"] or "").strip()
        expected_revision = step["expected_task_revision"]
        claimed_at = step["updated_at"]
        if (
            operation is None
            or not task_id
            or expected_revision is None
            or not isinstance(claimed_at, datetime)
        ):
            return ()
        revisions = {int(expected_revision)}
        # These adapters may persist the explicitly deferred AI-check state
        # immediately before creating the Server Job. That one CAS increment
        # is deterministic and is the only alternate source revision allowed.
        if action_kind in {"humanize", "restore_links"}:
            revisions.add(int(expected_revision) + 1)
        latest_created_at = claimed_at + _SERVER_JOB_RECOVERY_WINDOW
        if before is not None:
            latest_created_at = min(latest_created_at, before)
        conditions = [
            background_jobs.c.organization_id == actor.organization_id,
            background_jobs.c.project_id == str(step["project_id"]),
            background_jobs.c.task_id == task_id,
            background_jobs.c.requested_by_user_id == actor.user_id,
            background_jobs.c.operation == operation,
            background_jobs.c.source_revision.in_(tuple(sorted(revisions))),
            # Step claims use PostgreSQL ``now()`` while Server Jobs are
            # timestamped by the application process. Allow a small clock
            # skew; a second match still fails closed instead of guessing.
            background_jobs.c.created_at >= claimed_at - _SERVER_JOB_CLOCK_SKEW,
            background_jobs.c.created_at <= latest_created_at,
        ]
        if action_kind == "start_research":
            # Research already owns a stable idempotency identity in its
            # private Job request. Prefer it over the generic compound match.
            conditions.append(
                background_jobs.c.request["request_id"].astext
                == f"assistant-{plan_id}-{str(step['step_id'])}"
            )
        rows = connection.execute(
            sa.select(background_jobs.c.job_id)
            .where(*conditions)
            .order_by(background_jobs.c.created_at, background_jobs.c.job_id)
            .limit(2)
        ).scalars().all()
        return tuple(str(job_id) for job_id in rows)

    def recover_interrupted_steps(
        self,
        *,
        actor: ActorIdentity,
        plan_id: str,
        before: datetime | None = None,
    ) -> WorkflowPlan:
        """Close stale worker claims at a process-restart boundary.

        A queued Server Job can be reconciled from its durable Job identity;
        a synchronous step without one cannot be safely replayed without a
        business-specific idempotency contract, so it fails visibly instead
        of being executed twice.
        """

        with self._engine.begin() as connection:
            plan = self._lock_plan(connection, actor, plan_id)
            if plan is None:
                raise WorkflowAssistantNotFound("plan not found")
            conditions = [
                workflow_plan_steps.c.organization_id == actor.organization_id,
                workflow_plan_steps.c.plan_id == plan_id,
                workflow_plan_steps.c.status == "running",
            ]
            if before is not None:
                before = _aware(before, days=0)
                conditions.append(workflow_plan_steps.c.updated_at < before)
            rows = connection.execute(
                sa.select(
                    workflow_plan_steps.c.step_id,
                    workflow_plan_steps.c.action_kind,
                    workflow_plan_steps.c.project_id,
                    workflow_plan_steps.c.article_task_id,
                    workflow_plan_steps.c.expected_task_revision,
                    workflow_plan_steps.c.input_summary,
                    workflow_plan_steps.c.background_job_id,
                    workflow_plan_steps.c.updated_at,
                ).where(*conditions)
            ).mappings().all()
            for row in rows:
                recovered_job_id = str(row["background_job_id"] or "").strip()
                matches: tuple[str, ...] = ()
                if not recovered_job_id:
                    matches = self._recoverable_server_job_ids(
                        connection,
                        actor=actor,
                        plan_id=plan_id,
                        step=row,
                        before=before,
                    )
                    if len(matches) == 1:
                        recovered_job_id = matches[0]
                has_job = bool(recovered_job_id)
                status = "waiting_job" if has_job else "failed"
                error_code = (
                    None
                    if has_job
                    else (
                        "worker_interrupted_ambiguous_job"
                        if len(matches) > 1
                        else "worker_interrupted"
                    )
                )
                connection.execute(
                    workflow_plan_steps.update()
                    .where(
                        workflow_plan_steps.c.organization_id
                        == actor.organization_id,
                        workflow_plan_steps.c.plan_id == plan_id,
                        workflow_plan_steps.c.step_id == str(row["step_id"]),
                        workflow_plan_steps.c.status == "running",
                    )
                    .values(
                        status=status,
                        background_job_id=(
                            recovered_job_id if has_job else None
                        ),
                        standardized_error_code=error_code,
                        updated_at=sa.func.now(),
                    )
                )
                self._insert_event(
                    connection,
                    actor=actor,
                    plan_id=plan_id,
                    event_kind=(
                        "step_recovered" if has_job else "step_failed"
                    ),
                    public_payload={
                        "step_id": str(row["step_id"]),
                        "status": status,
                        **(
                            {"error_code": error_code}
                            if error_code
                            else {}
                        ),
                        **(
                            {"background_job_id": recovered_job_id}
                            if has_job
                            else {}
                        ),
                    },
                )
            result = self._get_plan_in_connection(connection, actor, plan_id)
            if result is None:
                raise WorkflowAssistantRepositoryError(
                    "plan disappeared during interrupted-step recovery"
                )
            return result

    def advance_task_chain_revision(
        self,
        *,
        actor: ActorIdentity,
        plan_id: str,
        project_id: str,
        article_task_id: str,
        after_sequence: int,
        from_revision: int,
        to_revision: int,
    ) -> int:
        """Carry an internal Task CAS revision to later pending steps.

        A queued Server Job is itself the authoritative writer.  Once it
        reports its committed revision, later steps in the same article chain
        must use that revision; otherwise a valid multi-step plan would fail
        against its own previous write.  The update is narrow and only moves
        still-pending steps which still carry the predecessor revision, so an
        external edit or another worker cannot be silently overwritten.
        """

        if from_revision < 0 or to_revision < 0 or to_revision < from_revision:
            raise ValueError("task chain revisions are invalid")
        with self._engine.begin() as connection:
            if self._lock_plan(connection, actor, plan_id) is None:
                raise WorkflowAssistantNotFound("plan not found")
            result = connection.execute(
                workflow_plan_steps.update()
                .where(
                    workflow_plan_steps.c.organization_id == actor.organization_id,
                    workflow_plan_steps.c.plan_id == plan_id,
                    workflow_plan_steps.c.project_id == project_id,
                    workflow_plan_steps.c.article_task_id == article_task_id,
                    workflow_plan_steps.c.sequence > after_sequence,
                    workflow_plan_steps.c.status == "pending",
                    workflow_plan_steps.c.expected_task_revision == from_revision,
                )
                .values(
                    expected_task_revision=to_revision,
                    updated_at=sa.func.now(),
                )
            )
            return int(result.rowcount or 0)

    def bind_created_task_steps(
        self,
        *,
        actor: ActorIdentity,
        plan_id: str,
        create_step_id: str,
        assignments: Sequence[tuple[str, str, int]],
    ) -> WorkflowPlan:
        """Bind server-allocated Task IDs to a pending article suffix.

        Task creation is a confirmed write whose identity is allocated by the
        existing intake service. This transaction closes the identity gap
        between that service and later plan steps without accepting a model
        supplied cross-project Task ID.
        """

        plan_id = _required(plan_id, "plan_id")
        create_step_id = _required(create_step_id, "create_step_id")
        if not assignments:
            return self.get_plan(actor=actor, plan_id=plan_id)
        with self._engine.begin() as connection:
            plan_row = self._lock_plan(connection, actor, plan_id)
            if plan_row is None:
                raise WorkflowAssistantNotFound("plan not found")
            create_row = connection.execute(
                sa.select(workflow_plan_steps)
                .where(
                    workflow_plan_steps.c.organization_id == actor.organization_id,
                    workflow_plan_steps.c.plan_id == plan_id,
                    workflow_plan_steps.c.step_id == create_step_id,
                )
            ).mappings().one_or_none()
            if create_row is None or str(create_row["action_kind"]) != "create_task":
                raise WorkflowAssistantConflict(
                    "created Task binding source is invalid"
                )
            if str(create_row["status"]) not in {"running", "succeeded"}:
                raise WorkflowAssistantConflict(
                    "created Task binding source is no longer active"
                )
            for step_id, task_id, revision in assignments:
                step_id = _required(step_id, "step_id")
                task_id = _required(task_id, "task_id")
                if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
                    raise ValueError("created Task revision is invalid")
                step_row = connection.execute(
                    sa.select(
                        workflow_plan_steps.c.project_id,
                        workflow_plan_steps.c.status,
                        workflow_plan_steps.c.article_task_id,
                        workflow_plan_steps.c.input_summary,
                    )
                    .where(
                        workflow_plan_steps.c.organization_id == actor.organization_id,
                        workflow_plan_steps.c.plan_id == plan_id,
                        workflow_plan_steps.c.step_id == step_id,
                    )
                ).mappings().one_or_none()
                if step_row is None:
                    raise WorkflowAssistantConflict(
                        "created Task binding target is unavailable"
                    )
                if str(step_row["status"]) != "pending" or step_row["article_task_id"]:
                    raise WorkflowAssistantConflict(
                        "created Task binding target is no longer pending"
                    )
                if str(_json_dict(step_row["input_summary"]).get("create_task_step_id") or "") != create_step_id:
                    raise WorkflowAssistantConflict(
                        "created Task binding target has a different source"
                    )
                project_id = str(step_row["project_id"])
                task_exists = connection.execute(
                    sa.select(article_tasks.c.task_id)
                    .where(
                        article_tasks.c.organization_id == actor.organization_id,
                        article_tasks.c.project_id == project_id,
                        article_tasks.c.task_id == task_id,
                    )
                ).scalar_one_or_none()
                if task_exists is None:
                    raise WorkflowAssistantConflict(
                        "created Task is outside the step project"
                    )
                result = connection.execute(
                    workflow_plan_steps.update()
                    .where(
                        workflow_plan_steps.c.organization_id == actor.organization_id,
                        workflow_plan_steps.c.plan_id == plan_id,
                        workflow_plan_steps.c.step_id == step_id,
                        workflow_plan_steps.c.status == "pending",
                        workflow_plan_steps.c.article_task_id.is_(None),
                    )
                    .values(
                        article_task_id=task_id,
                        expected_task_revision=revision,
                        updated_at=sa.func.now(),
                    )
                )
                if not result.rowcount:
                    raise WorkflowAssistantConflict(
                        "created Task binding was lost"
                    )
            result = self._get_plan_in_connection(connection, actor, plan_id)
            if result is None:
                raise WorkflowAssistantRepositoryError(
                    "plan disappeared during created Task binding"
                )
            return result

    def hold_step_for_review(
        self,
        *,
        actor: ActorIdentity,
        plan_id: str,
        step_id: str,
        standardized_error_code: str = "human_confirmation_required",
    ) -> bool:
        """Move one pending hard-gate step into a visible review state."""

        with self._engine.begin() as connection:
            if self._lock_plan(connection, actor, plan_id) is None:
                raise WorkflowAssistantNotFound("plan not found")
            result = connection.execute(
                workflow_plan_steps.update()
                .where(
                    workflow_plan_steps.c.organization_id == actor.organization_id,
                    workflow_plan_steps.c.plan_id == plan_id,
                    workflow_plan_steps.c.step_id == step_id,
                    workflow_plan_steps.c.status == "pending",
                    workflow_plan_steps.c.hard_gate.is_(True),
                )
                .values(
                    status="waiting_review",
                    standardized_error_code=standardized_error_code,
                    updated_at=sa.func.now(),
                )
            )
            return bool(result.rowcount)

    def complete_read_only_plan(
        self,
        *,
        actor: ActorIdentity,
        plan_id: str,
        outputs: Mapping[str, Mapping[str, Any]],
    ) -> WorkflowPlan:
        """Commit a read-only plan without creating a write approval."""

        plan_id = _required(plan_id, "plan_id")
        with self._engine.begin() as connection:
            existing = self._lock_plan(connection, actor, plan_id)
            if existing is None:
                raise WorkflowAssistantNotFound("plan not found")
            if str(existing["status"]) not in {"draft", "awaiting_confirmation"}:
                raise WorkflowAssistantConflict("read-only plan is no longer pending")
            step_rows = connection.execute(
                sa.select(workflow_plan_steps.c.step_id)
                .where(
                    workflow_plan_steps.c.organization_id == actor.organization_id,
                    workflow_plan_steps.c.plan_id == plan_id,
                )
            ).scalars().all()
            if set(outputs) != {str(step_id) for step_id in step_rows}:
                raise WorkflowAssistantConflict("read-only plan outputs are incomplete")
            for step_id, output in outputs.items():
                safe_output = sanitize_public_summary(output)
                connection.execute(
                    workflow_plan_steps.update()
                    .where(
                        workflow_plan_steps.c.organization_id == actor.organization_id,
                        workflow_plan_steps.c.plan_id == plan_id,
                        workflow_plan_steps.c.step_id == step_id,
                    )
                    .values(
                        status="succeeded",
                        output_summary=safe_output,
                        updated_at=sa.func.now(),
                    )
                )
            new_revision = int(existing["revision"]) + 1
            connection.execute(
                workflow_plans.update()
                .where(
                    workflow_plans.c.organization_id == actor.organization_id,
                    workflow_plans.c.plan_id == plan_id,
                    workflow_plans.c.revision == int(existing["revision"]),
                )
                .values(
                    status="completed",
                    revision=new_revision,
                    attention_state="none",
                    updated_at=sa.func.now(),
                )
            )
            self._insert_event(
                connection,
                actor=actor,
                plan_id=plan_id,
                event_kind="plan_completed",
                public_payload={"revision": new_revision, "read_only": True},
            )
            result = self._get_plan_in_connection(connection, actor, plan_id)
            if result is None:
                raise WorkflowAssistantRepositoryError("plan disappeared during completion")
            return result

    def confirm_plan(
        self,
        *,
        actor: ActorIdentity,
        plan_id: str,
        expected_revision: int,
        expected_plan_hash: str,
    ) -> WorkflowPlan:
        return self._transition_plan(
            actor=actor,
            plan_id=plan_id,
            expected_revision=expected_revision,
            expected_statuses={"draft", "awaiting_confirmation", "paused", "waiting_review"},
            new_status="queued",
            event_kind="plan_confirmed",
            event_payload={"plan_hash": expected_plan_hash},
            approval=True,
            expected_plan_hash=expected_plan_hash,
        )

    def set_plan_status(
        self,
        *,
        actor: ActorIdentity,
        plan_id: str,
        expected_revision: int,
        new_status: PlanStatus,
    ) -> WorkflowPlan:
        allowed: dict[PlanStatus, set[PlanStatus]] = {
            "paused": {"queued", "running", "waiting_review"},
            # A plain resume from waiting_review is deliberately distinct
            # from confirm_plan: it lets the runner reconcile other durable
            # Jobs while leaving every held hard gate unapproved.
            "running": {"queued", "paused", "waiting_review"},
            "waiting_review": {"queued", "running"},
            "completed": {"queued", "running", "waiting_review"},
            "failed": {"queued", "running", "waiting_review"},
            "cancelled": {"draft", "awaiting_confirmation", "queued", "running", "paused", "waiting_review", "failed"},
        }
        if new_status not in allowed:
            raise ValueError("unsupported plan status transition")
        return self._transition_plan(
            actor=actor,
            plan_id=plan_id,
            expected_revision=expected_revision,
            expected_statuses=allowed[new_status],
            new_status=new_status,
            event_kind=f"plan_{new_status}",
            event_payload={},
        )

    def set_projects_paused(
        self,
        *,
        actor: ActorIdentity,
        plan_id: str,
        expected_revision: int,
        project_ids: Sequence[str],
        paused: bool,
    ) -> WorkflowPlan:
        """Pause selected project lanes without changing the approved plan."""

        plan_id = _required(plan_id, "plan_id")
        normalized = tuple(
            dict.fromkeys(_required(value, "project_id") for value in project_ids)
        )
        if not normalized:
            raise ValueError("at least one project_id is required")
        with self._engine.begin() as connection:
            existing = self._lock_plan(connection, actor, plan_id)
            if existing is None:
                raise WorkflowAssistantNotFound("plan not found")
            current_revision = int(existing["revision"])
            if current_revision != expected_revision:
                raise WorkflowAssistantConflict(
                    "plan revision conflict",
                    code="plan_revision_conflict",
                    current_revision=current_revision,
                    current_plan_hash=str(existing["plan_hash"]),
                    current_steps=_current_plan_steps(
                        connection,
                        organization_id=actor.organization_id,
                        plan_id=plan_id,
                    ),
                )
            if str(existing["status"]) not in {
                "queued",
                "running",
                "waiting_review",
            }:
                raise WorkflowAssistantConflict(
                    "project lanes cannot be changed in the current plan state"
                )
            known = set(
                connection.execute(
                    sa.select(workflow_plan_projects.c.project_id).where(
                        workflow_plan_projects.c.organization_id
                        == actor.organization_id,
                        workflow_plan_projects.c.plan_id == plan_id,
                    )
                ).scalars()
            )
            unknown = set(normalized) - {str(value) for value in known}
            if unknown:
                raise WorkflowAssistantConflict(
                    "project lane is outside the confirmed plan"
                )
            connection.execute(
                workflow_plan_projects.update()
                .where(
                    workflow_plan_projects.c.organization_id
                    == actor.organization_id,
                    workflow_plan_projects.c.plan_id == plan_id,
                    workflow_plan_projects.c.project_id.in_(normalized),
                )
                .values(paused=paused)
            )
            new_revision = current_revision + 1
            connection.execute(
                workflow_plans.update()
                .where(
                    workflow_plans.c.organization_id == actor.organization_id,
                    workflow_plans.c.plan_id == plan_id,
                    workflow_plans.c.revision == current_revision,
                )
                .values(revision=new_revision, updated_at=sa.func.now())
            )
            self._insert_event(
                connection,
                actor=actor,
                plan_id=plan_id,
                event_kind=("project_paused" if paused else "project_resumed"),
                public_payload={
                    "project_ids": list(normalized),
                    "revision": new_revision,
                },
            )
            result = self._get_plan_in_connection(
                connection,
                actor,
                plan_id,
            )
            if result is None:
                raise WorkflowAssistantRepositoryError(
                    "plan disappeared while changing project lanes"
                )
            return result

    def revise_plan(
        self,
        *,
        actor: ActorIdentity,
        plan_id: str,
        expected_revision: int,
        expected_plan_hash: str | None = None,
        plan: PlanDraft,
        authorization_snapshot: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> WorkflowPlan:
        plan_id = _required(plan_id, "plan_id")
        with self._engine.begin() as connection:
            existing = self._lock_plan(connection, actor, plan_id)
            if existing is None:
                raise WorkflowAssistantNotFound("plan not found")
            if int(existing["revision"]) != expected_revision:
                raise WorkflowAssistantConflict(
                    "plan revision conflict: "
                    f"current_revision={int(existing['revision'])} "
                    f"current_plan_hash={str(existing['plan_hash'])}",
                    code="plan_revision_conflict",
                    current_revision=int(existing["revision"]),
                    current_plan_hash=str(existing["plan_hash"]),
                    current_steps=_current_plan_steps(
                        connection,
                        organization_id=actor.organization_id,
                        plan_id=plan_id,
                    ),
                )
            if (
                expected_plan_hash is not None
                and str(existing["plan_hash"]) != expected_plan_hash
            ):
                raise WorkflowAssistantConflict(
                    "plan hash conflict: "
                    f"current_revision={int(existing['revision'])} "
                    f"current_plan_hash={str(existing['plan_hash'])}",
                    code="plan_hash_conflict",
                    current_revision=int(existing["revision"]),
                    current_plan_hash=str(existing["plan_hash"]),
                    current_steps=_current_plan_steps(
                        connection,
                        organization_id=actor.organization_id,
                        plan_id=plan_id,
                    ),
                )
            if str(existing["status"]) in {"completed", "cancelled"}:
                raise WorkflowAssistantConflict("completed plan cannot be revised")
            old_step_rows = connection.execute(
                sa.select(workflow_plan_steps)
                .where(
                    workflow_plan_steps.c.organization_id == actor.organization_id,
                    workflow_plan_steps.c.plan_id == plan_id,
                )
                .order_by(workflow_plan_steps.c.sequence)
            ).mappings().all()
            active_step_statuses = {"running", "waiting_job"}
            if any(str(row["status"]) in active_step_statuses for row in old_step_rows):
                raise WorkflowAssistantConflict(
                    "pause and reconcile active Jobs before revising the plan"
                )
            new_steps_by_id = {step.step_id: step for step in plan.steps}
            new_project_ids = set(plan.project_ids)
            immutable_step_ids: set[str] = set()
            effective_steps: list[PlanStep] = []
            for row in old_step_rows:
                status = str(row["status"])
                if status not in {"succeeded", "skipped"}:
                    continue
                step_id = str(row["step_id"])
                candidate = new_steps_by_id.get(step_id)
                if candidate is None:
                    raise WorkflowAssistantConflict(
                        "completed steps must remain in a revised plan"
                    )
                if str(row["project_id"]) not in new_project_ids:
                    raise WorkflowAssistantConflict(
                        "completed step projects must remain in a revised plan"
                    )
                if (
                    candidate.sequence != int(row["sequence"])
                    or candidate.action_kind != str(row["action_kind"])
                    or candidate.project_id != str(row["project_id"])
                    or candidate.article_task_id
                    != (str(row["article_task_id"]) if row["article_task_id"] else None)
                    or candidate.hard_gate != bool(row["hard_gate"])
                ):
                    raise WorkflowAssistantConflict(
                        "completed steps are immutable in a revised plan"
                    )
                immutable_step_ids.add(step_id)
            for step in plan.steps:
                if step.step_id not in immutable_step_ids:
                    effective_steps.append(step)
                    continue
                old = next(row for row in old_step_rows if str(row["step_id"]) == step.step_id)
                # Keep the persisted completed step's CAS/pins in the
                # normalized plan too; only unfinished steps receive the new
                # context from the revision request.
                effective_steps.append(
                    step.model_copy(
                        update={
                            "expected_task_revision": (
                                int(old["expected_task_revision"])
                                if old["expected_task_revision"] is not None
                                else None
                            ),
                            "pinned_prompt_version": _json_dict(old["pinned_prompt_version"]),
                            "pinned_knowledge_snapshot": _json_dict(
                                old["pinned_knowledge_snapshot"]
                            ),
                            "input_summary": _json_dict(old["input_summary"]),
                        }
                    )
                )
            effective_plan = plan.model_copy(update={"steps": effective_steps})
            old_by_id = {str(row["step_id"]): row for row in old_step_rows}
            new_by_id = {step.step_id: step for step in effective_plan.steps}
            added_step_ids = sorted(set(new_by_id) - set(old_by_id))
            removed_step_ids = sorted(set(old_by_id) - set(new_by_id))
            changed_step_ids = sorted(
                step_id
                for step_id in set(old_by_id) & set(new_by_id)
                if (
                    int(old_by_id[step_id]["sequence"])
                    != int(new_by_id[step_id].sequence)
                    or str(old_by_id[step_id]["action_kind"])
                    != str(new_by_id[step_id].action_kind)
                    or str(old_by_id[step_id]["project_id"])
                    != str(new_by_id[step_id].project_id)
                    or (
                        str(old_by_id[step_id]["article_task_id"] or "")
                        != str(new_by_id[step_id].article_task_id or "")
                    )
                    or bool(old_by_id[step_id]["hard_gate"])
                    != bool(new_by_id[step_id].hard_gate)
                    or canonical_json(_json_dict(old_by_id[step_id]["input_summary"]))
                    != canonical_json(new_by_id[step_id].input_summary)
                )
            )
            new_revision = expected_revision + 1
            connection.execute(
                workflow_plans.update()
                .where(
                    workflow_plans.c.organization_id == actor.organization_id,
                    workflow_plans.c.plan_id == plan_id,
                    workflow_plans.c.revision == expected_revision,
                )
                .values(
                    natural_language_request=plan.natural_language_request,
                    normalized_plan=effective_plan.normalized_payload(),
                    plan_hash=canonical_plan_hash(effective_plan),
                    revision=new_revision,
                    status="awaiting_confirmation",
                    concurrency_limit=plan.concurrency_limit,
                    budget_warning=plan.budget_warning,
                    approved_by=None,
                    approved_at=None,
                    attention_state="user_confirmation",
                    updated_at=sa.func.now(),
                )
            )
            connection.execute(
                workflow_plan_steps.delete().where(
                    workflow_plan_steps.c.organization_id == actor.organization_id,
                    workflow_plan_steps.c.plan_id == plan_id,
                    workflow_plan_steps.c.step_id.not_in(immutable_step_ids),
                )
            )
            connection.execute(
                workflow_plan_projects.delete().where(
                    workflow_plan_projects.c.organization_id == actor.organization_id,
                    workflow_plan_projects.c.plan_id == plan_id,
                    workflow_plan_projects.c.project_id.not_in(plan.project_ids),
                )
            )
            existing_project_ids = {
                str(project_id)
                for project_id in connection.execute(
                    sa.select(workflow_plan_projects.c.project_id).where(
                        workflow_plan_projects.c.organization_id == actor.organization_id,
                        workflow_plan_projects.c.plan_id == plan_id,
                    )
                ).scalars().all()
            }
            self._insert_plan_children(
                connection,
                actor=actor,
                plan_id=plan_id,
                plan=effective_plan,
                authorization_snapshot=authorization_snapshot or {},
                preserve_step_ids=immutable_step_ids,
                existing_project_ids=existing_project_ids,
            )
            self._insert_event(
                connection,
                actor=actor,
                plan_id=plan_id,
                event_kind="plan_revised",
                public_payload={
                    "revision": new_revision,
                    "plan_hash": canonical_plan_hash(effective_plan),
                    **_bounded_step_id_event_projection(
                        "preserved",
                        tuple(immutable_step_ids),
                    ),
                    **_bounded_step_id_event_projection("added", added_step_ids),
                    **_bounded_step_id_event_projection("removed", removed_step_ids),
                    **_bounded_step_id_event_projection("changed", changed_step_ids),
                },
            )
            result = self._get_plan_in_connection(connection, actor, plan_id)
            if result is None:
                raise WorkflowAssistantRepositoryError("plan disappeared during revision")
            return result

    def list_events(
        self,
        *,
        actor: ActorIdentity,
        plan_id: str,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> tuple[WorkflowPlanEvent, ...]:
        plan_id = _required(plan_id, "plan_id")
        if after_sequence < 0 or not 1 <= limit <= 500:
            raise ValueError("event cursor or limit is invalid")
        with self._engine.connect() as connection:
            if self._lock_plan(connection, actor, plan_id) is None:
                raise WorkflowAssistantNotFound("plan not found")
            rows = connection.execute(
                sa.select(workflow_plan_events)
                .where(
                    workflow_plan_events.c.organization_id == actor.organization_id,
                    workflow_plan_events.c.plan_id == plan_id,
                    workflow_plan_events.c.sequence > after_sequence,
                )
                .order_by(workflow_plan_events.c.sequence)
                .limit(limit)
            ).mappings().all()
            return tuple(self._event_from_row(row) for row in rows)

    def append_event(
        self,
        *,
        actor: ActorIdentity,
        plan_id: str,
        event_kind: str,
        public_payload: Mapping[str, Any] | None = None,
    ) -> WorkflowPlanEvent:
        """Append one public execution event under the plan row lock."""

        plan_id = _required(plan_id, "plan_id")
        with self._engine.begin() as connection:
            if self._lock_plan(connection, actor, plan_id) is None:
                raise WorkflowAssistantNotFound("plan not found")
            return self._insert_event(
                connection,
                actor=actor,
                plan_id=plan_id,
                event_kind=event_kind,
                public_payload=sanitize_public_summary(public_payload or {}),
            )

    def record_usage(
        self,
        *,
        actor: ActorIdentity,
        provider: str,
        model: str,
        operation_kind: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        estimated_cost: Decimal | None = None,
        project_id: str | None = None,
        plan_id: str | None = None,
        usage_event_id: str | None = None,
    ) -> AssistantUsageEvent:
        if input_tokens < 0 or output_tokens < 0:
            raise ValueError("token counts must not be negative")
        values = {
            "organization_id": actor.organization_id,
            "usage_event_id": _required(usage_event_id or f"asu_{uuid.uuid4().hex}", "usage_event_id"),
            "user_id": actor.user_id,
            "provider": _required(provider, "provider"),
            "model": _required(model, "model"),
            "operation_kind": _required(operation_kind, "operation_kind"),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "estimated_cost": estimated_cost,
            "project_id": _required(project_id, "project_id") if project_id else None,
            "plan_id": _required(plan_id, "plan_id") if plan_id else None,
        }
        with self._engine.begin() as connection:
            connection.execute(assistant_usage_events.insert().values(**values))
            row = connection.execute(
                sa.select(assistant_usage_events).where(
                    assistant_usage_events.c.organization_id == actor.organization_id,
                    assistant_usage_events.c.usage_event_id == values["usage_event_id"],
                )
            ).mappings().one()
            return self._usage_from_row(row)

    def attention_count(
        self,
        *,
        actor: ActorIdentity,
        accessible_project_ids: Sequence[str] | None = None,
    ) -> int:
        """Count only attention plans whose complete project scope is visible.

        Assistant conversations are private to their creator, but a plan can
        outlive a project assignment.  The optional live project set lets the
        HTTP layer keep the inbox badge behind the same authorization boundary
        as plan reads instead of leaking the existence of a revoked plan.
        """

        conditions = [
            workflow_plans.c.organization_id == actor.organization_id,
            workflow_plans.c.creator_user_id == actor.user_id,
            workflow_plans.c.attention_state != "none",
        ]
        if accessible_project_ids is not None:
            normalized_projects = tuple(
                dict.fromkeys(
                    project_id.strip()
                    for project_id in accessible_project_ids
                    if project_id.strip()
                )
            )
            if not normalized_projects:
                return 0
            inaccessible_scope = sa.exists(
                sa.select(1)
                .select_from(workflow_plan_projects)
                .where(
                    workflow_plan_projects.c.organization_id
                    == workflow_plans.c.organization_id,
                    workflow_plan_projects.c.plan_id
                    == workflow_plans.c.plan_id,
                    workflow_plan_projects.c.project_id.not_in(normalized_projects),
                )
            )
            conditions.append(~inaccessible_scope)
        with self._engine.connect() as connection:
            return int(
                connection.execute(
                    sa.select(sa.func.count())
                    .select_from(workflow_plans)
                    .where(*conditions)
                ).scalar_one()
            )

    def list_attention_plans(
        self,
        *,
        actor: ActorIdentity,
        accessible_project_ids: Sequence[str] | None = None,
        limit: int = 50,
    ) -> tuple[WorkflowPlan, ...]:
        """List visible plans which still need the creator's attention.

        The inbox is deliberately derived from the durable plan projection,
        rather than from messages or process-local runner state.  A plan is
        returned only when its complete project scope is still visible to the
        actor; this keeps revoked project assignments from leaking plan
        existence through either the badge or the list.
        """

        if not 1 <= limit <= 200:
            raise ValueError("attention limit is invalid")
        conditions = [
            workflow_plans.c.organization_id == actor.organization_id,
            workflow_plans.c.creator_user_id == actor.user_id,
            workflow_plans.c.attention_state != "none",
        ]
        if accessible_project_ids is not None:
            normalized_projects = tuple(
                dict.fromkeys(
                    project_id.strip()
                    for project_id in accessible_project_ids
                    if project_id.strip()
                )
            )
            if not normalized_projects:
                return ()
            inaccessible_scope = sa.exists(
                sa.select(1)
                .select_from(workflow_plan_projects)
                .where(
                    workflow_plan_projects.c.organization_id
                    == workflow_plans.c.organization_id,
                    workflow_plan_projects.c.plan_id
                    == workflow_plans.c.plan_id,
                    workflow_plan_projects.c.project_id.not_in(normalized_projects),
                )
            )
            conditions.append(~inaccessible_scope)
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(workflow_plans.c.plan_id)
                .where(*conditions)
                .order_by(
                    workflow_plans.c.updated_at.desc(),
                    workflow_plans.c.plan_id.desc(),
                )
                .limit(limit)
            ).scalars().all()
            return tuple(
                plan
                for plan_id in rows
                if (plan := self._get_plan_in_connection(
                    connection,
                    actor,
                    str(plan_id),
                ))
                is not None
            )

    def mark_plan_seen(
        self,
        *,
        actor: ActorIdentity,
        plan_id: str,
    ) -> WorkflowPlan:
        """Clear an unread completion marker after the creator opens it."""

        plan_id = _required(plan_id, "plan_id")
        with self._engine.begin() as connection:
            existing = self._lock_plan(connection, actor, plan_id)
            if existing is None:
                raise WorkflowAssistantNotFound("plan not found")
            if str(existing["attention_state"]) == "unread":
                connection.execute(
                    workflow_plans.update()
                    .where(
                        workflow_plans.c.organization_id == actor.organization_id,
                        workflow_plans.c.plan_id == plan_id,
                        workflow_plans.c.attention_state == "unread",
                    )
                    .values(
                        attention_state="none",
                        updated_at=sa.func.now(),
                    )
                )
            result = self._get_plan_in_connection(connection, actor, plan_id)
            if result is None:
                raise WorkflowAssistantRepositoryError(
                    "plan disappeared while marking it seen"
                )
            return result

    def list_execution_candidates(
        self,
        *,
        limit: int = 100,
    ) -> tuple[WorkflowExecutionCandidate, ...]:
        """List durable plans for the internal runner to re-authorize.

        The runner receives only organization/user/plan identities. It must
        load the full plan through the normal creator-scoped repository path
        before invoking any typed tool.
        """

        if not 1 <= limit <= 500:
            raise ValueError("execution candidate limit is invalid")
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(
                    workflow_plans.c.organization_id,
                    workflow_plans.c.creator_user_id,
                    workflow_plans.c.plan_id,
                    workflow_plans.c.status,
                )
                .where(workflow_plans.c.status.in_(("queued", "running")))
                .order_by(workflow_plans.c.updated_at, workflow_plans.c.plan_id)
                .limit(limit)
            ).mappings().all()
            return tuple(
                WorkflowExecutionCandidate(
                    organization_id=str(row["organization_id"]),
                    creator_user_id=str(row["creator_user_id"]),
                    plan_id=str(row["plan_id"]),
                    status=str(row["status"]),
                )
                for row in rows
            )

    def prune_expired(self, *, before: datetime) -> int:
        before = _aware(before, days=0)
        with self._engine.begin() as connection:
            expired = sa.select(assistant_conversations.c.conversation_id).where(
                assistant_conversations.c.expires_at < before
            )
            has_plan = sa.exists(
                sa.select(1)
                .select_from(workflow_plans)
                .where(
                    workflow_plans.c.organization_id
                    == assistant_conversations.c.organization_id,
                    workflow_plans.c.conversation_id
                    == assistant_conversations.c.conversation_id,
                )
            )
            has_attachment = sa.exists(
                sa.select(1)
                .select_from(assistant_attachments)
                .where(
                    assistant_attachments.c.organization_id
                    == assistant_conversations.c.organization_id,
                    assistant_attachments.c.conversation_id
                    == assistant_conversations.c.conversation_id,
                )
            )
            # Private messages expire after 30 days. Confirmed plans remain
            # durable, so retain their parent conversation row as an opaque
            # anchor while removing the message bodies.
            connection.execute(
                assistant_messages.delete().where(
                    assistant_messages.c.conversation_id.in_(
                        expired
                    ),
                )
            )
            result = connection.execute(
                assistant_conversations.delete().where(
                    assistant_conversations.c.expires_at < before,
                    ~has_plan,
                    ~has_attachment,
                )
            )
            return int(result.rowcount or 0)

    def _lock_conversation(
        self,
        connection: Connection,
        actor: ActorIdentity,
        conversation_id: str,
    ) -> RowMapping | None:
        row = connection.execute(
            sa.select(assistant_conversations)
            .where(
                assistant_conversations.c.organization_id == actor.organization_id,
                assistant_conversations.c.creator_user_id == actor.user_id,
                assistant_conversations.c.conversation_id == conversation_id,
                assistant_conversations.c.expires_at > sa.func.now(),
            )
            .with_for_update()
        ).mappings().one_or_none()
        return row

    def _get_conversation_in_connection(
        self,
        connection: Connection,
        actor: ActorIdentity,
        conversation_id: str,
        *,
        include_messages: bool = True,
    ) -> AssistantConversation | None:
        row = connection.execute(
            sa.select(assistant_conversations).where(
                assistant_conversations.c.organization_id == actor.organization_id,
                assistant_conversations.c.creator_user_id == actor.user_id,
                assistant_conversations.c.conversation_id == conversation_id,
                assistant_conversations.c.expires_at > sa.func.now(),
            )
        ).mappings().one_or_none()
        if row is None:
            return None
        messages: tuple[AssistantMessage, ...] = ()
        if include_messages:
            message_rows = connection.execute(
                sa.select(assistant_messages)
                .where(
                    assistant_messages.c.organization_id == actor.organization_id,
                    assistant_messages.c.conversation_id == conversation_id,
                )
                .order_by(assistant_messages.c.sequence)
            ).mappings().all()
            messages = tuple(self._message_from_row(item) for item in message_rows)
        return self._conversation_from_row(row, messages=messages)

    def _lock_plan(
        self,
        connection: Connection,
        actor: ActorIdentity,
        plan_id: str,
    ) -> RowMapping | None:
        return connection.execute(
            sa.select(workflow_plans)
            .where(
                workflow_plans.c.organization_id == actor.organization_id,
                workflow_plans.c.creator_user_id == actor.user_id,
                workflow_plans.c.plan_id == plan_id,
            )
            .with_for_update()
        ).mappings().one_or_none()

    def _insert_plan_children(
        self,
        connection: Connection,
        *,
        actor: ActorIdentity,
        plan_id: str,
        plan: PlanDraft,
        authorization_snapshot: Mapping[str, Mapping[str, Any]],
        preserve_step_ids: set[str] | None = None,
        existing_project_ids: set[str] | None = None,
    ) -> None:
        existing_project_ids = existing_project_ids or set()
        project_values = [
            {
                "organization_id": actor.organization_id,
                "plan_id": plan_id,
                "project_id": project_id,
                "authorization_snapshot": copy.deepcopy(
                    dict(authorization_snapshot.get(project_id, {}))
                ),
            }
            for project_id in plan.project_ids
            if project_id not in existing_project_ids
        ]
        if project_values:
            connection.execute(workflow_plan_projects.insert(), project_values)
        preserve_step_ids = preserve_step_ids or set()
        step_values = [
            {
                "organization_id": actor.organization_id,
                "plan_id": plan_id,
                "step_id": step.step_id,
                "sequence": step.sequence,
                "action_kind": step.action_kind,
                "project_id": step.project_id,
                "article_task_id": step.article_task_id,
                "expected_task_revision": step.expected_task_revision,
                "pinned_prompt_version": copy.deepcopy(step.pinned_prompt_version),
                "pinned_knowledge_snapshot": copy.deepcopy(step.pinned_knowledge_snapshot),
                "status": "pending",
                "retry_count": 0,
                "hard_gate": step.hard_gate,
                "human_gate_confirmed": False,
                "input_summary": copy.deepcopy(step.input_summary),
                "output_summary": {},
            }
            for step in plan.steps
            if step.step_id not in preserve_step_ids
        ]
        if step_values:
            connection.execute(workflow_plan_steps.insert(), step_values)

    def _transition_plan(
        self,
        *,
        actor: ActorIdentity,
        plan_id: str,
        expected_revision: int,
        expected_statuses: set[str],
        new_status: PlanStatus,
        event_kind: str,
        event_payload: Mapping[str, Any],
        approval: bool = False,
        expected_plan_hash: str | None = None,
    ) -> WorkflowPlan:
        plan_id = _required(plan_id, "plan_id")
        with self._engine.begin() as connection:
            existing = self._lock_plan(connection, actor, plan_id)
            if existing is None:
                raise WorkflowAssistantNotFound("plan not found")
            if int(existing["revision"]) != expected_revision:
                raise WorkflowAssistantConflict(
                    "plan revision conflict: "
                    f"current_revision={int(existing['revision'])} "
                    f"current_plan_hash={str(existing['plan_hash'])}",
                    code="plan_revision_conflict",
                    current_revision=int(existing["revision"]),
                    current_plan_hash=str(existing["plan_hash"]),
                    current_steps=_current_plan_steps(
                        connection,
                        organization_id=actor.organization_id,
                        plan_id=plan_id,
                    ),
                )
            if str(existing["status"]) not in expected_statuses:
                raise WorkflowAssistantConflict("plan status cannot be changed")
            if expected_plan_hash is not None and str(existing["plan_hash"]) != expected_plan_hash:
                raise WorkflowAssistantConflict(
                    "plan hash conflict: "
                    f"current_revision={int(existing['revision'])} "
                    f"current_plan_hash={str(existing['plan_hash'])}",
                    code="plan_hash_conflict",
                    current_revision=int(existing["revision"]),
                    current_plan_hash=str(existing["plan_hash"]),
                    current_steps=_current_plan_steps(
                        connection,
                        organization_id=actor.organization_id,
                        plan_id=plan_id,
                    ),
                )
            values: dict[str, Any] = {
                "status": new_status,
                "revision": expected_revision + 1,
                "updated_at": sa.func.now(),
            }
            if approval:
                values.update(
                    approved_by=actor.user_id,
                    approved_at=sa.func.now(),
                    attention_state="none",
                )
            elif new_status == "waiting_review":
                values["attention_state"] = "user_confirmation"
            elif new_status == "failed":
                values["attention_state"] = "error"
            elif new_status == "completed":
                # A completed write plan remains visible in the private
                # inbox until its creator opens the result.  Read-only plans
                # use ``complete_read_only_plan`` and intentionally clear
                # attention immediately because their response is already in
                # the same request.
                values["attention_state"] = "unread"
            elif new_status == "cancelled":
                values["attention_state"] = "none"
            connection.execute(
                workflow_plans.update()
                .where(
                    workflow_plans.c.organization_id == actor.organization_id,
                    workflow_plans.c.plan_id == plan_id,
                    workflow_plans.c.revision == expected_revision,
                )
                .values(**values)
            )
            if approval:
                # A confirmation releases only steps explicitly held at the
                # human gate.  This also covers a plan paused while it was in
                # review; completed steps remain immutable and are never
                # replayed.
                connection.execute(
                    workflow_plan_steps.update()
                    .where(
                        workflow_plan_steps.c.organization_id == actor.organization_id,
                        workflow_plan_steps.c.plan_id == plan_id,
                        workflow_plan_steps.c.status == "waiting_review",
                    )
                    .values(
                        status="pending",
                        human_gate_confirmed=True,
                        standardized_error_code=None,
                        updated_at=sa.func.now(),
                    )
                )
            elif new_status == "cancelled":
                # Cancellation is durable and immediate for the assistant
                # state machine.  Completed steps stay immutable; any step
                # that has not reached a terminal result is closed so an
                # in-flight worker cannot commit a late result into a
                # cancelled plan or be rediscovered after a restart.
                connection.execute(
                    workflow_plan_steps.update()
                    .where(
                        workflow_plan_steps.c.organization_id == actor.organization_id,
                        workflow_plan_steps.c.plan_id == plan_id,
                        workflow_plan_steps.c.status.in_(
                            ("pending", "running", "waiting_job", "waiting_review")
                        ),
                    )
                    .values(
                        status="cancelled",
                        standardized_error_code="plan_cancelled",
                        updated_at=sa.func.now(),
                    )
                )
            elif new_status == "failed":
                # A plan-level failure is a stop boundary. Close every
                # unfinished step before the transaction commits so a late
                # worker result cannot be written into a failed plan or be
                # rediscovered after a process restart.
                connection.execute(
                    workflow_plan_steps.update()
                    .where(
                        workflow_plan_steps.c.organization_id == actor.organization_id,
                        workflow_plan_steps.c.plan_id == plan_id,
                        workflow_plan_steps.c.status.in_(
                            ("pending", "running", "waiting_job", "waiting_review")
                        ),
                    )
                    .values(
                        status="failed",
                        standardized_error_code="plan_failed",
                        updated_at=sa.func.now(),
                    )
                )
            self._insert_event(
                connection,
                actor=actor,
                plan_id=plan_id,
                event_kind=event_kind,
                public_payload={
                    **dict(event_payload),
                    "revision": expected_revision + 1,
                    "status": new_status,
                },
            )
            result = self._get_plan_in_connection(connection, actor, plan_id)
            if result is None:
                raise WorkflowAssistantRepositoryError("plan disappeared during transition")
            return result

    def _insert_event(
        self,
        connection: Connection,
        *,
        actor: ActorIdentity,
        plan_id: str,
        event_kind: str,
        public_payload: Mapping[str, Any],
    ) -> WorkflowPlanEvent:
        safe_payload = sanitize_public_summary(public_payload)
        next_sequence = int(
            connection.execute(
                sa.select(
                    sa.func.coalesce(sa.func.max(workflow_plan_events.c.sequence), 0)
                ).where(
                    workflow_plan_events.c.organization_id == actor.organization_id,
                    workflow_plan_events.c.plan_id == plan_id,
                )
            ).scalar_one()
        ) + 1
        connection.execute(
            workflow_plan_events.insert().values(
                organization_id=actor.organization_id,
                plan_id=plan_id,
                sequence=next_sequence,
                event_kind=_required(event_kind, "event_kind"),
                public_payload=safe_payload,
            )
        )
        return WorkflowPlanEvent(
            plan_id=plan_id,
            sequence=next_sequence,
            event_kind=event_kind,
            public_payload=copy.deepcopy(safe_payload),
        )

    def _get_plan_in_connection(
        self,
        connection: Connection,
        actor: ActorIdentity,
        plan_id: str,
    ) -> WorkflowPlan | None:
        row = connection.execute(
            sa.select(workflow_plans).where(
                workflow_plans.c.organization_id == actor.organization_id,
                workflow_plans.c.creator_user_id == actor.user_id,
                workflow_plans.c.plan_id == plan_id,
            )
        ).mappings().one_or_none()
        if row is None:
            return None
        project_rows = connection.execute(
            sa.select(
                workflow_plan_projects.c.project_id,
                workflow_plan_projects.c.paused,
            )
            .where(
                workflow_plan_projects.c.organization_id == actor.organization_id,
                workflow_plan_projects.c.plan_id == plan_id,
            )
            .order_by(workflow_plan_projects.c.project_id)
        ).mappings().all()
        step_rows = connection.execute(
            sa.select(workflow_plan_steps)
            .where(
                workflow_plan_steps.c.organization_id == actor.organization_id,
                workflow_plan_steps.c.plan_id == plan_id,
            )
            .order_by(workflow_plan_steps.c.sequence)
        ).mappings().all()
        return WorkflowPlan(
            organization_id=str(row["organization_id"]),
            plan_id=str(row["plan_id"]),
            creator_user_id=str(row["creator_user_id"]),
            conversation_id=str(row["conversation_id"]),
            source_idempotency_key=(
                str(row["source_idempotency_key"])
                if row["source_idempotency_key"]
                else None
            ),
            title=str(_json_dict(row["normalized_plan"]).get("title") or "Workflow plan"),
            natural_language_request=str(row["natural_language_request"]),
            normalized_plan=_json_dict(row["normalized_plan"]),
            plan_hash=str(row["plan_hash"]),
            revision=int(row["revision"]),
            status=str(row["status"]),
            project_ids=tuple(str(value["project_id"]) for value in project_rows),
            paused_project_ids=tuple(
                str(value["project_id"])
                for value in project_rows
                if bool(value["paused"])
            ),
            steps=tuple(self._step_from_row(item) for item in step_rows),
            concurrency_limit=int(row["concurrency_limit"]),
            budget_warning=bool(row["budget_warning"]),
            attention_state=str(row["attention_state"]),
            approved_by=(str(row["approved_by"]) if row["approved_by"] else None),
            approved_at=row["approved_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _conversation_from_row(
        row: RowMapping,
        *,
        messages: tuple[AssistantMessage, ...] = (),
    ) -> AssistantConversation:
        return AssistantConversation(
            organization_id=str(row["organization_id"]),
            conversation_id=str(row["conversation_id"]),
            creator_user_id=str(row["creator_user_id"]),
            title=str(row["title"]),
            project_ids=_json_list(row["last_project_ids"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            expires_at=row["expires_at"],
            messages=messages,
        )

    @staticmethod
    def _message_from_row(row: RowMapping) -> AssistantMessage:
        return AssistantMessage(
            message_id=str(row["message_id"]),
            sequence=int(row["sequence"]),
            role=str(row["role"]),
            content=str(row["sanitized_content"]),
            request_id=str(row["request_id"]),
            idempotency_key=str(row["idempotency_key"]),
            created_at=row["created_at"],
        )

    @staticmethod
    def _step_from_row(row: RowMapping) -> WorkflowPlanStep:
        return WorkflowPlanStep(
            step_id=str(row["step_id"]),
            sequence=int(row["sequence"]),
            action_kind=str(row["action_kind"]),
            project_id=str(row["project_id"]),
            article_task_id=(str(row["article_task_id"]) if row["article_task_id"] else None),
            expected_task_revision=(int(row["expected_task_revision"]) if row["expected_task_revision"] is not None else None),
            pinned_prompt_version=_json_dict(row["pinned_prompt_version"]),
            pinned_knowledge_snapshot=_json_dict(row["pinned_knowledge_snapshot"]),
            status=str(row["status"]),
            background_job_id=(str(row["background_job_id"]) if row["background_job_id"] else None),
            retry_count=int(row["retry_count"]),
            hard_gate=bool(row["hard_gate"]),
            human_gate_confirmed=bool(row["human_gate_confirmed"]),
            input_summary=_json_dict(row["input_summary"]),
            output_summary=_json_dict(row["output_summary"]),
            standardized_error_code=(str(row["standardized_error_code"]) if row["standardized_error_code"] else None),
        )

    @staticmethod
    def _event_from_row(row: RowMapping) -> WorkflowPlanEvent:
        return WorkflowPlanEvent(
            plan_id=str(row["plan_id"]),
            sequence=int(row["sequence"]),
            event_kind=str(row["event_kind"]),
            public_payload=_json_dict(row["public_payload"]),
            created_at=row["created_at"],
        )

    @staticmethod
    def _usage_from_row(row: RowMapping) -> AssistantUsageEvent:
        return AssistantUsageEvent(
            usage_event_id=str(row["usage_event_id"]),
            user_id=str(row["user_id"]),
            project_id=(str(row["project_id"]) if row["project_id"] else None),
            plan_id=(str(row["plan_id"]) if row["plan_id"] else None),
            provider=str(row["provider"]),
            model=str(row["model"]),
            operation_kind=str(row["operation_kind"]),
            input_tokens=int(row["input_tokens"]),
            output_tokens=int(row["output_tokens"]),
            estimated_cost=row["estimated_cost"],
            created_at=row["created_at"],
        )


__all__ = [
    "AssistantConversation",
    "AssistantMessage",
    "AssistantUsageEvent",
    "PostgresWorkflowAssistantRepository",
    "WorkflowAssistantConflict",
    "WorkflowAssistantForbidden",
    "WorkflowAssistantNotFound",
    "WorkflowAssistantRepositoryError",
    "WorkflowPlan",
    "WorkflowPlanEvent",
    "WorkflowExecutionCandidate",
    "WorkflowPlanStep",
]
