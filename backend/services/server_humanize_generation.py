from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any, Protocol

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from config import AppConfig
from models import (
    STATUS_HUMANIZED_READY,
    STATUS_FINAL_AI_CHECKED,
    AICheck,
    ArticleVersion,
    PromptSnapshot,
    TaskRecord,
)
from server_schema import article_tasks
from services.access_control import (
    ActorIdentity,
    PostgresProjectAccessRepository,
    ProjectAccessDenied,
    ProjectAccessService,
    decide_project_permission,
)
from services.article_validation import (
    ArticleStructureError,
    validate_humanized_article,
    visible_word_count,
)
from services.audit_log import (
    AuditEvent,
    AuditEventWriter,
    PostgresAuditEventWriter,
)
from services.generator import (
    PromptTemplateError,
    article_output_token_limit,
)
from services.job_queue import (
    ActiveJobError,
    JobCancelled,
    JobConflict,
)
from services.llm import LLMClient
from services.postgres_task_repository import PostgresTaskRepository
from services.server_outline_generation import (
    ProjectPromptReference,
    load_pinned_project_prompt,
)
from services.server_project_job_registry import (
    ProjectJobHandler,
    ServerProjectJobRegistry,
    ServerProjectJobStopReport,
    public_job,
)
from services.server_project_prompts import PostgresProjectPromptService
from services.server_task_commands import (
    PostgresAuditedTaskWriter,
    ServerTaskCommandUnavailable,
)
from services.server_llm_settings import ServerLlmClientFactory
from services.zerogpt import ZeroGPTDetectionResult
from storage import RevisionConflictError, content_hash, now_iso
from workflow.state_machine import (
    ACTION_HUMANIZE_ARTICLE,
    WorkflowActionNotAllowed,
    ensure_action_allowed,
    invalidate_downstream,
    transition_task,
)


HUMANIZE_OPERATION = "humanize"


class HumanizeGenerationUnavailable(RuntimeError):
    """The scoped Humanize runner cannot safely complete work."""


class HumanizeLlmClient(Protocol):
    @property
    def ready(self) -> bool: ...

    def chat(
        self,
        messages: list[dict[str, Any]],
        temperature: float = 0.7,
        max_tokens: int = 1800,
    ) -> str: ...


class HumanizeGenerationProvider(Protocol):
    def generate(
        self,
        task: TaskRecord,
        *,
        source_article: str,
        prompt_snapshot: PromptSnapshot,
    ) -> str: ...


class HumanizeAiRateDetector(Protocol):
    @property
    def ready(self) -> bool: ...

    def detect(self, text: str) -> ZeroGPTDetectionResult: ...


def _source_article(task: TaskRecord) -> tuple[str, bool]:
    rehumanizing = (
        task.status in {STATUS_HUMANIZED_READY, STATUS_FINAL_AI_CHECKED}
        and bool(task.humanized_article.strip())
    )
    source = (
        task.humanized_article if rehumanizing else task.initial_article
    ).strip()
    if not source:
        raise JobConflict("humanize source article is unavailable")
    stored_hash = (
        task.humanized_article_hash
        if rehumanizing
        else task.initial_article_hash
    ).strip()
    if stored_hash != content_hash(source):
        raise JobConflict("humanize source article identity is invalid")
    return source, rehumanizing


def _required_phrases(task: TaskRecord) -> list[str]:
    return [
        task.competitor_keyword or task.topic,
        *(product.name for product in task.products if product.name),
    ]


def _validate_prompt(snapshot: PromptSnapshot) -> None:
    if (
        snapshot.kind != "humanize"
        or snapshot.source == "system"
        or snapshot.content.count("{{ARTICLE}}") != 1
    ):
        raise JobConflict("project humanize prompt is not configured")


class LlmServerHumanizeProvider:
    """Humanize from a pinned Project Prompt without reading local files."""

    def __init__(
        self,
        config: AppConfig,
        *,
        llm: HumanizeLlmClient | None = None,
        llm_factory: ServerLlmClientFactory | None = None,
    ) -> None:
        self._llm_factory = llm_factory
        self._llm = llm or LLMClient(config)

    @property
    def ready(self) -> bool:
        return self._llm.ready

    def _client_for(self, organization_id: str) -> HumanizeLlmClient:
        if self._llm_factory is not None:
            return self._llm_factory.client(organization_id)
        return self._llm

    def generate_for_organization(
        self,
        task: TaskRecord,
        *,
        organization_id: str,
        source_article: str,
        prompt_snapshot: PromptSnapshot,
    ) -> str:
        return self.generate(
            task,
            source_article=source_article,
            prompt_snapshot=prompt_snapshot,
            organization_id=organization_id,
        )

    def generate(
        self,
        task: TaskRecord,
        *,
        source_article: str,
        prompt_snapshot: PromptSnapshot,
        organization_id: str = "",
    ) -> str:
        client = self._client_for(organization_id)
        if not client.ready:
            raise HumanizeGenerationUnavailable(
                "humanize provider is not configured"
            )
        _validate_prompt(prompt_snapshot)
        try:
            result = client.chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "You are a careful B2B editor. Preserve every "
                            "fact, link, heading, product name, and required "
                            "phrase. State supported facts directly. Never "
                            "expose source, website, supplier, manufacturer, "
                            "product-page, retrieval, "
                            "or writing-workflow narration in reader-facing "
                            "copy. Preserve supplied img index-tag blocks "
                            "exactly. Return only Markdown."
                        ),
                    },
                    {
                        "role": "user",
                        "content": prompt_snapshot.content.replace(
                            "{{ARTICLE}}",
                            source_article,
                        ),
                    },
                ],
                temperature=0.5,
                max_tokens=article_output_token_limit(
                    max(visible_word_count(source_article), 1500)
                ),
            )
            candidate = str(result or "").strip()
            if not candidate:
                raise ArticleStructureError(
                    "humanize provider returned no article"
                )
            validate_humanized_article(
                source_article,
                candidate,
                required_phrases=_required_phrases(task),
            )
            return candidate
        except HumanizeGenerationUnavailable:
            raise
        except (
            ArticleStructureError,
            PromptTemplateError,
            RuntimeError,
        ) as exc:
            raise HumanizeGenerationUnavailable(
                "humanize provider returned an invalid result"
            ) from exc


def apply_generated_humanized_article(
    task: TaskRecord,
    *,
    source_revision: int,
    source_article: str,
    candidate: str,
) -> bool:
    if task.revision != source_revision:
        raise JobConflict("source task revision changed")
    current_source, rehumanizing = _source_article(task)
    if (
        content_hash(current_source) != content_hash(source_article)
        or not candidate.strip()
    ):
        raise JobConflict("humanize source article changed")
    try:
        ensure_action_allowed(task, ACTION_HUMANIZE_ARTICLE)
    except WorkflowActionNotAllowed as exc:
        raise JobConflict("task cannot be humanized") from exc
    try:
        validate_humanized_article(
            current_source,
            candidate,
            required_phrases=_required_phrases(task),
        )
    except ArticleStructureError as exc:
        raise JobConflict("humanize result is invalid") from exc
    timestamp = now_iso()
    humanized = candidate.strip()
    task.humanized_article = humanized
    task.humanization_skipped = False
    task.humanized_article_word_count = visible_word_count(humanized)
    task.humanized_article_hash = content_hash(humanized)
    task.article = humanized
    task.article_versions.append(
        ArticleVersion(
            kind="humanized",
            content=humanized,
            word_count=visible_word_count(humanized),
            content_hash=content_hash(humanized),
            created_at=timestamp,
            source_kind=(
                "rehumanized" if rehumanizing else "initial"
            ),
        )
    )
    task.workflow_error = None
    if task.status == "initial_ai_checked":
        transition_task(task, STATUS_HUMANIZED_READY)
    else:
        invalidate_downstream(task, "humanized_article")
    return rehumanizing


class ServerHumanizeGenerationHandler:
    """Humanize one pinned source with one immutable Project Prompt."""

    def __init__(
        self,
        engine: Engine,
        *,
        provider: HumanizeGenerationProvider,
        ai_rate: HumanizeAiRateDetector | None = None,
        audit: AuditEventWriter | None = None,
    ) -> None:
        self._engine = engine
        self._provider = provider
        self._ai_rate = ai_rate
        self._audit = audit

    def __call__(
        self,
        job: dict[str, Any],
        cancelled: Callable[[], bool],
    ) -> int:
        if str(job.get("operation") or "") != HUMANIZE_OPERATION:
            raise JobConflict("unsupported server job operation")
        organization_id = str(job.get("organization_id") or "").strip()
        project_id = str(job.get("project_id") or "").strip()
        task_id = str(job.get("task_id") or "").strip()
        requester = str(job.get("requested_by_user_id") or "").strip()
        source_revision = int(job.get("source_revision") or 0)
        request = dict(job.get("request") or {})
        reference = ProjectPromptReference.from_mapping(request)
        source_hash = str(
            request.get("source_article_hash") or ""
        ).strip()
        if len(source_hash) != 64:
            raise JobConflict("humanize article identity is invalid")
        if cancelled():
            raise JobCancelled("humanize cancelled before execution")
        repository = PostgresTaskRepository(
            self._engine,
            organization_id=organization_id,
            project_id=project_id,
        )
        payload = repository.get(task_id)
        if payload is None:
            raise JobConflict("source task is unavailable")
        task = TaskRecord.model_validate(payload)
        if task.revision != source_revision:
            raise JobConflict("source task revision changed")
        source_article, _rehumanizing = _source_article(task)
        if content_hash(source_article) != source_hash:
            raise JobConflict("humanize source article changed")
        try:
            ensure_action_allowed(task, ACTION_HUMANIZE_ARTICLE)
        except WorkflowActionNotAllowed as exc:
            raise JobConflict("task cannot be humanized") from exc
        prompt = load_pinned_project_prompt(
            self._engine,
            organization_id=organization_id,
            project_id=project_id,
            kind="humanize",
            reference=reference,
        )
        _validate_prompt(prompt)
        if cancelled():
            raise JobCancelled("humanize cancelled before provider call")
        generate_for_organization = getattr(
            self._provider,
            "generate_for_organization",
            None,
        )
        if callable(generate_for_organization):
            candidate = generate_for_organization(
                task,
                organization_id=organization_id,
                source_article=source_article,
                prompt_snapshot=prompt,
            )
        else:
            candidate = self._provider.generate(
                task,
                source_article=source_article,
                prompt_snapshot=prompt,
            )
        if cancelled():
            raise JobCancelled("humanize cancelled before result commit")
        rehumanizing = apply_generated_humanized_article(
            task,
            source_revision=source_revision,
            source_article=source_article,
            candidate=candidate,
        )
        if cancelled():
            raise JobCancelled(
                "humanize cancelled before AI-rate detection"
            )
        checked_at = now_iso()
        article_hash = task.humanized_article_hash
        if self._ai_rate is None or not self._ai_rate.ready:
            task.final_ai_check = AICheck(
                confirmed=False,
                report=(
                    "ZeroGPT 自动复检未运行：服务端尚未配置 API Key。"
                ),
                provider="zerogpt",
                checked_at=checked_at,
                article_hash=article_hash,
            )
        else:
            try:
                detection = self._ai_rate.detect(task.humanized_article)
            except Exception:
                task.final_ai_check = AICheck(
                    confirmed=False,
                    report=(
                        "ZeroGPT 自动复检暂时不可用，请稍后重试或保留截图人工确认。"
                    ),
                    provider="zerogpt",
                    checked_at=checked_at,
                    article_hash=article_hash,
                )
            else:
                task.final_ai_check = AICheck(
                    confirmed=False,
                    score=detection.ai_percentage,
                    report=detection.report,
                    provider="zerogpt",
                    checked_at=checked_at,
                    article_hash=article_hash,
                )
        if cancelled():
            raise JobCancelled(
                "humanize cancelled before result commit"
            )
        try:
            saved = PostgresAuditedTaskWriter(
                self._engine,
                organization_id=organization_id,
                project_id=project_id,
                audit=self._audit,
            ).put(
                task,
                expected_revision=source_revision,
                actor=ActorIdentity(organization_id, requester),
                action="article.humanized.generated",
                details={
                    "article_characters": len(candidate),
                    "prompt_source": reference.source,
                    "prompt_version": reference.version,
                    "rehumanizing": rehumanizing,
                },
            )
        except ProjectAccessDenied as exc:
            raise JobConflict("job actor is not authorized") from exc
        except RevisionConflictError as exc:
            raise JobConflict("source task revision changed") from exc
        except ServerTaskCommandUnavailable:
            raise
        return saved.revision


class ServerHumanizeGenerationRegistry:
    """Trusted Humanize Enqueue plus shared Project runner lifecycle."""

    def __init__(
        self,
        engine: Engine,
        *,
        access: ProjectAccessService,
        handler: ProjectJobHandler | None,
        access_repository: PostgresProjectAccessRepository | None = None,
        audit: AuditEventWriter | None = None,
    ) -> None:
        self._engine = engine
        self._access = access
        self._access_repository = (
            access_repository or PostgresProjectAccessRepository(engine)
        )
        self._audit = audit or PostgresAuditEventWriter()
        self._registry = ServerProjectJobRegistry(
            engine,
            operation=HUMANIZE_OPERATION,
            access=access,
            handler=handler,
            error_type=HumanizeGenerationUnavailable,
            terminal_audit=self._audit,
        )

    def start_existing(self) -> None:
        self._registry.start_existing()

    def enqueue(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        task_id: str,
        source_revision: int,
    ) -> dict[str, object]:
        self._access.require(actor, project_id, "article.edit")
        repository = PostgresTaskRepository(
            self._engine,
            organization_id=actor.organization_id,
            project_id=project_id,
        )
        payload = repository.get(task_id)
        if payload is None:
            raise KeyError(task_id)
        task = TaskRecord.model_validate(payload)
        if task.revision != source_revision:
            raise JobConflict("source task revision changed")
        try:
            ensure_action_allowed(task, ACTION_HUMANIZE_ARTICLE)
        except WorkflowActionNotAllowed as exc:
            raise JobConflict("task cannot be humanized") from exc
        source_article, _rehumanizing = _source_article(task)
        try:
            snapshot = PostgresProjectPromptService(
                self._engine,
                organization_id=actor.organization_id,
                project_id=project_id,
            ).resolve(
                actor,
                kind="humanize",
                selection="project_default",
            )
        except ProjectAccessDenied:
            raise
        except (RuntimeError, SQLAlchemyError) as exc:
            raise HumanizeGenerationUnavailable(
                "humanize prompt could not be resolved"
            ) from exc
        _validate_prompt(snapshot)
        reference = ProjectPromptReference.from_snapshot(snapshot)
        project = self._registry.project(
            actor.organization_id,
            project_id,
            start_runner=True,
        )
        try:
            with self._engine.begin() as connection:
                facts = self._access_repository.lock_project_access_in_connection(
                    connection,
                    actor,
                    project_id,
                )
                if not decide_project_permission(
                    facts,
                    "article.edit",
                ).allowed:
                    raise ProjectAccessDenied("project access denied")
                row = connection.execute(
                    sa.select(
                        article_tasks.c.revision,
                        article_tasks.c.topic_index,
                    )
                    .where(
                        article_tasks.c.organization_id
                        == actor.organization_id,
                        article_tasks.c.project_id == project_id,
                        article_tasks.c.task_id == task_id,
                    )
                    .with_for_update()
                ).one_or_none()
                if row is None:
                    raise KeyError(task_id)
                if int(row.revision) != source_revision:
                    raise JobConflict("source task revision changed")
                batch = project.queue.create_batch_in_transaction(
                    connection,
                    HUMANIZE_OPERATION,
                    [
                        {
                            "task_id": task_id,
                            "source_revision": source_revision,
                            "customer": project_id,
                            "topic_index": int(row.topic_index),
                            "request": {
                                **reference.private_values(),
                                "source_article_hash": content_hash(
                                    source_article
                                ),
                            },
                        }
                    ],
                    customer=project_id,
                    requested_by_user_id=actor.user_id,
                )
                job = batch["jobs"][0]
                job_id = str(job["id"])
                identity = "\n".join(
                    (
                        actor.organization_id,
                        project_id,
                        job_id,
                        HUMANIZE_OPERATION,
                    )
                )
                self._audit.append(
                    connection,
                    AuditEvent(
                        organization_id=actor.organization_id,
                        event_id=(
                            "job_"
                            + uuid.uuid5(
                                uuid.NAMESPACE_URL,
                                identity,
                            ).hex
                        ),
                        actor_user_id=actor.user_id,
                        project_id=project_id,
                        action="article.humanize.queued",
                        target_type="background_job",
                        target_id=job_id,
                        details={
                            "operation": HUMANIZE_OPERATION,
                            "prompt_source": reference.source,
                            "prompt_version": reference.version,
                            "source_revision": source_revision,
                        },
                    ),
                )
        except (
            ActiveJobError,
            JobConflict,
            KeyError,
            ProjectAccessDenied,
        ):
            raise
        except (RuntimeError, SQLAlchemyError) as exc:
            raise HumanizeGenerationUnavailable(
                "humanize could not be queued"
            ) from exc
        if project.runner is None:
            raise HumanizeGenerationUnavailable(
                "humanize runner did not start"
            )
        project.runner.wake()
        return public_job(job)

    def get_job(self, **kwargs: Any) -> dict[str, object]:
        return self._registry.get_job(**kwargs)

    def stop(
        self,
        *,
        timeout_seconds: float = 10.0,
    ) -> ServerProjectJobStopReport:
        return self._registry.stop(timeout_seconds=timeout_seconds)


__all__ = [
    "HUMANIZE_OPERATION",
    "HumanizeGenerationUnavailable",
    "LlmServerHumanizeProvider",
    "ServerHumanizeGenerationHandler",
    "ServerHumanizeGenerationRegistry",
    "apply_generated_humanized_article",
]
