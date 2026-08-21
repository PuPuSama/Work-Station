from __future__ import annotations

import logging
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
    IMG_MARKER_PATTERN,
    MARKDOWN_IMAGE_PATTERN,
    ArticleStructureError,
    markdown_link_counter,
    strip_llm_code_fence,
    url_counter,
    validate_humanized_article,
    visible_word_count,
)
from services.audit_log import (
    AuditEvent,
    AuditEventWriter,
    PostgresAuditEventWriter,
)
from services.generator import (
    ARTICLE_TARGET_MAX,
    ARTICLE_TARGET_MIN,
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
from services.server_knowledge_coverage import ServerKnowledgeCoverageService
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
# 1000-1200 remains the writing target, not a destructive hard cutoff.  The
# wider acceptance band keeps a structurally sound edit when semantic
# compression cannot safely remove more prose without changing locked facts,
# links, headings, tables, or image markers.
HUMANIZE_ACCEPT_MIN = ARTICLE_TARGET_MIN - 100
HUMANIZE_ACCEPT_MAX = ARTICLE_TARGET_MAX + 200
_LOGGER = logging.getLogger(__name__)


def _record_humanize_failure(
    category: str,
    *,
    source_words: int,
    candidate_words: int,
) -> None:
    """Log only non-sensitive diagnostics for a rejected provider result."""

    _LOGGER.warning(
        "humanize_failure category=%s source_words=%d candidate_words=%d",
        category,
        source_words,
        candidate_words,
    )


def _validate_locked_humanize_content(
    source: str,
    candidate: str,
    *,
    required_phrases: list[str],
) -> None:
    """Validate every locally provable invariant promised by Humanize."""

    validate_humanized_article(
        source,
        candidate,
        required_phrases=required_phrases,
    )
    if markdown_link_counter(source) != markdown_link_counter(candidate):
        raise ArticleStructureError("Humanization changed Markdown links.")
    if url_counter(source) != url_counter(candidate):
        raise ArticleStructureError("Humanization changed URL occurrences.")
    if MARKDOWN_IMAGE_PATTERN.findall(source) != MARKDOWN_IMAGE_PATTERN.findall(
        candidate
    ):
        raise ArticleStructureError("Humanization changed Markdown images.")
    if IMG_MARKER_PATTERN.findall(source) != IMG_MARKER_PATTERN.findall(candidate):
        raise ArticleStructureError("Humanization changed image markers.")


def _word_range_failure_category(word_count: int) -> str:
    return "word_count_high" if word_count > HUMANIZE_ACCEPT_MAX else "word_count_low"


def _word_range_distance(word_count: int) -> int:
    if word_count < HUMANIZE_ACCEPT_MIN:
        return HUMANIZE_ACCEPT_MIN - word_count
    if word_count > HUMANIZE_ACCEPT_MAX:
        return word_count - HUMANIZE_ACCEPT_MAX
    return 0


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
        if self._llm_factory is not None:
            return self._llm_factory.ready
        return self._llm.ready

    def _client_for(
        self,
        organization_id: str,
        user_id: str,
    ) -> HumanizeLlmClient:
        if self._llm_factory is not None:
            return self._llm_factory.client(organization_id, user_id)
        return self._llm

    def generate_for_organization(
        self,
        task: TaskRecord,
        *,
        organization_id: str,
        user_id: str,
        source_article: str,
        prompt_snapshot: PromptSnapshot,
    ) -> str:
        return self.generate(
            task,
            source_article=source_article,
            prompt_snapshot=prompt_snapshot,
            organization_id=organization_id,
            user_id=user_id,
        )

    def generate(
        self,
        task: TaskRecord,
        *,
        source_article: str,
        prompt_snapshot: PromptSnapshot,
        organization_id: str = "",
        user_id: str = "",
    ) -> str:
        _validate_prompt(prompt_snapshot)
        source_words = visible_word_count(source_article)
        candidate_words = 0
        try:
            client = self._client_for(organization_id, user_id)
            if not client.ready:
                raise HumanizeGenerationUnavailable(
                    "humanize provider is not configured"
                )
            required_phrases = _required_phrases(task)
            messages = [
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
                        "exactly. Keep the finished article between 1000 "
                        "and 1200 visible English words. Return only "
                        "Markdown."
                    ),
                },
                {
                    "role": "user",
                    "content": prompt_snapshot.content.replace(
                        "{{ARTICLE}}",
                        source_article,
                    ),
                },
            ]
            candidate = ""
            for initial_attempt in range(3):
                attempt_messages = messages
                if initial_attempt:
                    attempt_messages = [
                        messages[0],
                        {
                            "role": "user",
                            "content": (
                                messages[1]["content"]
                                + "\n\nThe previous result was empty or changed "
                                "locked content. "
                                "Try again from the supplied article. Preserve "
                                "the exact heading hierarchy and heading text, "
                                "numeric facts and occurrence counts, tables, "
                                "list structure, required phrases, links, and "
                                "image markers. Return only Markdown."
                            ),
                        },
                    ]
                result = client.chat(
                    attempt_messages,
                    temperature=0.5 if initial_attempt == 0 else 0.3,
                    max_tokens=article_output_token_limit(
                        max(visible_word_count(source_article), 1500)
                    ),
                )
                candidate = strip_llm_code_fence(str(result or "")).strip()
                candidate_words = visible_word_count(candidate)
                if not candidate:
                    _record_humanize_failure(
                        "initial_empty",
                        source_words=source_words,
                        candidate_words=0,
                    )
                    continue
                try:
                    _validate_locked_humanize_content(
                        source_article,
                        candidate,
                        required_phrases=required_phrases,
                    )
                except ArticleStructureError:
                    _record_humanize_failure(
                        "initial_locked_content",
                        source_words=source_words,
                        candidate_words=candidate_words,
                    )
                    continue
                break
            else:
                raise ArticleStructureError(
                    "humanize provider repeatedly changed locked content"
                )
            if (
                source_words >= ARTICLE_TARGET_MIN
                and not HUMANIZE_ACCEPT_MIN <= candidate_words <= HUMANIZE_ACCEPT_MAX
            ):
                best_candidate = candidate
                best_words = candidate_words
                for correction_attempt in range(6):
                    direction = (
                        "compress"
                        if best_words > HUMANIZE_ACCEPT_MAX
                        else "expand"
                    )
                    target_low, target_high, target_center = (
                        (1100, 1180, 1140)
                        if direction == "compress"
                        else (1020, 1100, 1060)
                    )
                    semantic_delta = (
                        best_words - target_center
                        if direction == "compress"
                        else target_center - best_words
                    )
                    result = client.chat(
                        [
                            messages[0],
                            {
                                "role": "user",
                                "content": (
                                    f"The valid edit below has {best_words} visible "
                                    f"English words. Semantically {direction} it by "
                                    f"approximately {max(1, semantic_delta)} words, "
                                    f"aiming for {target_low}-{target_high} visible "
                                    "English words inside the preferred 1000-1200 "
                                    "target. Rewrite sentences and paragraphs; "
                                    "do not mechanically truncate, cut off the ending, "
                                    "or delete a whole section. Preserve every heading "
                                    "and its order, "
                                    "all numeric facts and their occurrence counts, "
                                    "tables, list structure, exact required phrases, "
                                    "Markdown links, and img index-tag blocks. "
                                    "When expanding, add only explanatory prose "
                                    "supported by the existing article and introduce "
                                    "no new facts or numbers. "
                                    f"This is correction attempt {correction_attempt + 1}. "
                                    "Return only the revised Markdown.\n\n"
                                    f"{best_candidate}"
                                ),
                            },
                        ],
                        temperature=0.1,
                        max_tokens=article_output_token_limit(
                            ARTICLE_TARGET_MAX
                        ),
                    )
                    corrected = strip_llm_code_fence(str(result or "")).strip()
                    corrected_words = visible_word_count(corrected)
                    candidate_words = corrected_words
                    if not corrected:
                        _record_humanize_failure(
                            "correction_empty",
                            source_words=source_words,
                            candidate_words=0,
                        )
                        continue
                    try:
                        _validate_locked_humanize_content(
                            source_article,
                            corrected,
                            required_phrases=required_phrases,
                        )
                    except ArticleStructureError:
                        _record_humanize_failure(
                            "correction_locked_content",
                            source_words=source_words,
                            candidate_words=corrected_words,
                        )
                        continue
                    if HUMANIZE_ACCEPT_MIN <= corrected_words <= HUMANIZE_ACCEPT_MAX:
                        candidate = corrected
                        candidate_words = corrected_words
                        break
                    _record_humanize_failure(
                        _word_range_failure_category(corrected_words),
                        source_words=source_words,
                        candidate_words=corrected_words,
                    )
                    # Only accumulate a structurally valid result when it is
                    # closer to the hard range. A valid regression must not
                    # replace the best retry source and cause oscillation.
                    if _word_range_distance(corrected_words) < _word_range_distance(
                        best_words
                    ):
                        best_candidate = corrected
                        best_words = corrected_words
                else:
                    candidate_words = best_words
                    _record_humanize_failure(
                        "correction_exhausted_"
                        + _word_range_failure_category(best_words).removeprefix(
                            "word_count_"
                        ),
                        source_words=source_words,
                        candidate_words=best_words,
                    )
                    raise ArticleStructureError(
                        "humanize provider returned an article outside the "
                        "required word range or changed locked content"
                    )
            return candidate
        except HumanizeGenerationUnavailable:
            raise
        except ArticleStructureError:
            raise HumanizeGenerationUnavailable(
                "humanize provider returned an invalid result"
            ) from None
        except PromptTemplateError:
            _record_humanize_failure(
                "prompt_template",
                source_words=source_words,
                candidate_words=candidate_words,
            )
            raise HumanizeGenerationUnavailable(
                "humanize provider returned an invalid result"
            ) from None
        except RuntimeError:
            _record_humanize_failure(
                "provider_runtime",
                source_words=source_words,
                candidate_words=candidate_words,
            )
            raise HumanizeGenerationUnavailable(
                "humanize provider returned an invalid result"
            ) from None


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
        _validate_locked_humanize_content(
            current_source,
            candidate,
            required_phrases=_required_phrases(task),
        )
    except ArticleStructureError:
        raise JobConflict("humanize result is invalid") from None
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
        knowledge_coverage: ServerKnowledgeCoverageService | None = None,
        audit: AuditEventWriter | None = None,
    ) -> None:
        self._engine = engine
        self._provider = provider
        self._ai_rate = ai_rate
        self._knowledge_coverage = knowledge_coverage
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
                user_id=requester,
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
        if self._knowledge_coverage is not None:
            self._knowledge_coverage.evaluate_task(
                task,
                organization_id=organization_id,
                user_id=requester,
                project_id=project_id,
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
                    "knowledge_coverage_status": (
                        task.knowledge_coverage.status
                    ),
                    "knowledge_supported_sentences": (
                        task.knowledge_coverage.supported_sentences
                    ),
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
