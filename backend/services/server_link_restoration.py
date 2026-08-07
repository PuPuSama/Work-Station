from __future__ import annotations

import hashlib
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from config import AppConfig
from models import ArticleVersion, LinkValidation, SourceLink, TaskRecord
from server_schema import article_tasks, background_jobs
from services.access_control import (
    ActorIdentity,
    PostgresProjectAccessRepository,
    ProjectAccessDenied,
    ProjectAccessService,
    decide_project_permission,
)
from services.article_validation import (
    LinkRestorationError,
    assert_no_unexpected_candidate_links,
    extract_link_inventory,
    missing_link_inventory,
    strip_llm_code_fence,
    validate_restored_links,
    visible_word_count,
)
from services.audit_log import (
    AuditEvent,
    AuditEventWriter,
    PostgresAuditEventWriter,
)
from services.authorized_job_queue import (
    authorized_batch_runner,
)
from services.generator import (
    article_output_token_limit,
    format_link_inventory,
    load_prompt_template,
    render_prompt,
)
from services.job_queue import (
    ACTIVE_JOB_STATUSES,
    ActiveJobError,
    BatchJobRunner,
    JobCancelled,
    JobConflict,
)
from services.llm import LLMClient
from services.postgres_job_queue import PostgresJobQueue
from services.postgres_task_repository import PostgresTaskRepository
from services.server_task_commands import (
    PostgresAuditedTaskWriter,
    ServerTaskCommandUnavailable,
)
from storage import RevisionConflictError, content_hash, now_iso
from workflow.state_machine import (
    ACTION_RESTORE_LINKS,
    STATUS_LINKS_VERIFIED,
    WorkflowActionNotAllowed,
    ensure_action_allowed,
    invalidate_downstream,
    transition_task,
)


LINK_RESTORATION_OPERATION = "restore_links"
MAX_RESTORED_ARTICLE_CHARACTERS = 200_000


class LinkRestorationUnavailable(RuntimeError):
    """The scoped link-restoration runner cannot safely complete work."""


class LinkRestorationLlmClient(Protocol):
    @property
    def ready(self) -> bool: ...

    def chat(
        self,
        messages: list[dict[str, Any]],
        temperature: float = 0.7,
        max_tokens: int = 1800,
    ) -> str: ...


class LinkRestorationProvider(Protocol):
    def restore(
        self,
        *,
        source_article: str,
        candidate_article: str,
        missing_links: list[dict[str, object]],
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class LinkTemplateReference:
    """Identity of the checked-in restoration prompt used by one Job."""

    template_name: str
    content_hash: str

    @classmethod
    def current(cls) -> LinkTemplateReference:
        content = (
            load_prompt_template("restore_links")
            .replace("\r\n", "\n")
            .replace("\r", "\n")
            .strip()
        )
        return cls(
            template_name="restore_links",
            content_hash=hashlib.sha256(
                content.encode("utf-8")
            ).hexdigest(),
        )

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, object],
    ) -> LinkTemplateReference:
        name = str(value.get("template_name") or "").strip()
        digest = str(value.get("template_hash") or "").strip()
        if (
            name != "restore_links"
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise JobConflict("link template identity is invalid")
        return cls(template_name=name, content_hash=digest)

    def verify_current(self) -> None:
        if self != self.current():
            raise JobConflict("pinned link template changed")

    def private_values(self) -> dict[str, object]:
        return {
            "template_name": self.template_name,
            "template_hash": self.content_hash,
        }


def _required_hash(value: object, field_name: str) -> str:
    digest = str(value or "").strip()
    if (
        len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise JobConflict(f"{field_name} identity is invalid")
    return digest


class LlmServerLinkRestorationProvider:
    """Server-only provider; it never fabricates a fallback article."""

    def __init__(
        self,
        config: AppConfig,
        *,
        llm: LinkRestorationLlmClient | None = None,
    ) -> None:
        self._llm = llm or LLMClient(config)

    @property
    def ready(self) -> bool:
        return self._llm.ready

    def restore(
        self,
        *,
        source_article: str,
        candidate_article: str,
        missing_links: list[dict[str, object]],
    ) -> str:
        if not missing_links:
            return candidate_article
        if not self.ready:
            raise LinkRestorationUnavailable(
                "link restoration provider is not configured"
            )
        prompt = render_prompt(
            "restore_links",
            MISSING_LINKS=format_link_inventory(missing_links),
            SOURCE_ARTICLE=source_article,
            CANDIDATE_ARTICLE=candidate_article,
        )
        try:
            result = self._llm.chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "Restore the first version's exact Markdown link "
                            "anchors and URLs. You may change visible wording "
                            "only inside an anchor restored to its first-version "
                            "name."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
                max_tokens=article_output_token_limit(
                    max(visible_word_count(candidate_article), 500)
                ),
            )
        except Exception as exc:
            raise LinkRestorationUnavailable(
                "link restoration provider is temporarily unavailable"
            ) from exc
        restored = strip_llm_code_fence(str(result or "")).strip()
        if (
            not restored
            or len(restored) > MAX_RESTORED_ARTICLE_CHARACTERS
        ):
            raise LinkRestorationUnavailable(
                "link restoration provider returned an invalid result"
            )
        return restored


def apply_restored_links(
    task: TaskRecord,
    *,
    source_article: str,
    candidate_article: str,
    restored_article: str,
    prompt_version: str,
) -> tuple[int, int]:
    """Apply a deterministically verified candidate and clear later artifacts."""

    restored = restored_article.strip()
    if not restored or len(restored) > MAX_RESTORED_ARTICLE_CHARACTERS:
        raise LinkRestorationUnavailable(
            "link restoration provider returned an invalid result"
        )
    try:
        assert_no_unexpected_candidate_links(
            source_article,
            candidate_article,
        )
        validate_restored_links(
            source_article,
            candidate_article,
            restored,
        )
    except LinkRestorationError as exc:
        raise LinkRestorationUnavailable(
            "link restoration provider returned an invalid result"
        ) from exc

    source_inventory = [
        SourceLink.model_validate(item)
        for item in extract_link_inventory(source_article)
    ]
    restored_inventory = [
        SourceLink.model_validate(item)
        for item in extract_link_inventory(restored)
    ]
    source_count = sum(item.count for item in source_inventory)
    restored_count = sum(item.count for item in restored_inventory)

    invalidate_downstream(task, "links")
    task.linked_article = restored
    task.linked_article_word_count = visible_word_count(restored)
    task.linked_article_hash = content_hash(restored)
    task.article = restored
    task.source_links = source_inventory
    task.link_validation = LinkValidation(
        passed=True,
        source_count=source_count,
        preserved_count=restored_count,
        missing_links=[],
        unexpected_links=[],
        visible_text_unchanged=True,
        article_hash=task.linked_article_hash,
        verified_at=now_iso(),
    )
    task.article_versions.append(
        ArticleVersion(
            kind="linked",
            content=restored,
            word_count=task.linked_article_word_count,
            content_hash=task.linked_article_hash,
            created_at=now_iso(),
            source_kind="humanized",
            source_hash=content_hash(candidate_article),
            prompt_version=prompt_version,
        )
    )
    transition_task(task, STATUS_LINKS_VERIFIED)
    return source_count, restored_count


LinkRestorationJobHandler = Callable[
    [dict[str, Any], Callable[[], bool]],
    int,
]


class ServerLinkRestorationHandler:
    """Restore links against pinned article and template identities."""

    def __init__(
        self,
        engine: Engine,
        *,
        provider: LinkRestorationProvider,
        audit: AuditEventWriter | None = None,
    ) -> None:
        self._engine = engine
        self._provider = provider
        self._audit = audit

    def __call__(
        self,
        job: dict[str, Any],
        cancelled: Callable[[], bool],
    ) -> int:
        if str(job.get("operation") or "") != LINK_RESTORATION_OPERATION:
            raise JobConflict("unsupported server job operation")
        organization_id = str(job.get("organization_id") or "").strip()
        project_id = str(job.get("project_id") or "").strip()
        task_id = str(job.get("task_id") or "").strip()
        requester = str(job.get("requested_by_user_id") or "").strip()
        source_revision = int(job.get("source_revision") or 0)
        request = dict(job.get("request") or {})
        reference = LinkTemplateReference.from_mapping(request)
        reference.verify_current()
        source_hash = _required_hash(
            request.get("source_article_hash"),
            "source article",
        )
        candidate_hash = _required_hash(
            request.get("candidate_article_hash"),
            "candidate article",
        )
        try:
            source_link_count = int(request.get("source_link_count"))
        except (TypeError, ValueError) as exc:
            raise JobConflict("source link count is invalid") from exc
        if source_link_count < 0:
            raise JobConflict("source link count is invalid")
        if cancelled():
            raise JobCancelled(
                "Link restoration cancelled before execution."
            )

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
        try:
            ensure_action_allowed(task, ACTION_RESTORE_LINKS)
        except WorkflowActionNotAllowed as exc:
            raise JobConflict("link restoration is not allowed") from exc

        source_article = task.initial_article
        candidate_article = task.humanized_article
        current_source_hash = content_hash(source_article)
        current_candidate_hash = content_hash(candidate_article)
        if (
            not source_article.strip()
            or task.initial_article_hash != source_hash
            or current_source_hash != source_hash
        ):
            raise JobConflict("source article changed")
        if (
            not candidate_article.strip()
            or task.humanized_article_hash != candidate_hash
            or current_candidate_hash != candidate_hash
        ):
            raise JobConflict("candidate article changed")
        if (
            not task.final_ai_check.confirmed
            or task.final_ai_check.article_hash != candidate_hash
        ):
            raise JobConflict("final AI check identity changed")
        try:
            assert_no_unexpected_candidate_links(
                source_article,
                candidate_article,
            )
            missing = missing_link_inventory(
                source_article,
                candidate_article,
            )
        except LinkRestorationError as exc:
            raise JobConflict(
                "candidate article link set is invalid"
            ) from exc
        actual_source_count = sum(
            int(item.get("count") or 0)
            for item in extract_link_inventory(source_article)
        )
        if actual_source_count != source_link_count:
            raise JobConflict("source link inventory changed")
        if cancelled():
            raise JobCancelled(
                "Link restoration cancelled before provider call."
            )
        restored = self._provider.restore(
            source_article=source_article,
            candidate_article=candidate_article,
            missing_links=missing,
        )
        if cancelled():
            raise JobCancelled(
                "Link restoration cancelled before result commit."
            )
        reference.verify_current()
        source_count, restored_count = apply_restored_links(
            task,
            source_article=source_article,
            candidate_article=candidate_article,
            restored_article=restored,
            prompt_version=reference.content_hash,
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
                action="article.links.restored",
                details={
                    "restored_link_count": restored_count,
                    "source_link_count": source_count,
                },
            )
        except ProjectAccessDenied as exc:
            raise JobConflict("job actor is not authorized") from exc
        except RevisionConflictError as exc:
            raise JobConflict("source task revision changed") from exc
        except ServerTaskCommandUnavailable:
            raise
        return saved.revision


@dataclass(frozen=True, slots=True)
class LinkRestorationStopReport:
    project_runner_count: int
    dispatcher_stopped: bool
    remaining_jobs: int

    @property
    def drained(self) -> bool:
        return self.dispatcher_stopped and self.remaining_jobs == 0


@dataclass(slots=True)
class _ProjectRunner:
    queue: PostgresJobQueue
    runner: BatchJobRunner | None


class ServerLinkRestorationRegistry:
    """Lazily run one authorized link-restoration queue per Project."""

    def __init__(
        self,
        engine: Engine,
        *,
        access: ProjectAccessService,
        handler: LinkRestorationJobHandler | None,
        access_repository: PostgresProjectAccessRepository | None = None,
        audit: AuditEventWriter | None = None,
    ) -> None:
        self._engine = engine
        self._access = access
        self._access_repository = (
            access_repository or PostgresProjectAccessRepository(engine)
        )
        self._audit = audit or PostgresAuditEventWriter()
        self._handler = handler
        self._lock = threading.Lock()
        self._closed = False
        self._projects: dict[tuple[str, str], _ProjectRunner] = {}
        self._stop_report: LinkRestorationStopReport | None = None

    def _ensure_project(
        self,
        organization_id: str,
        project_id: str,
        *,
        start_runner: bool,
    ) -> _ProjectRunner:
        scope = (organization_id, project_id)
        with self._lock:
            if self._closed:
                raise LinkRestorationUnavailable(
                    "link restoration runner is stopped"
                )
            current = self._projects.get(scope)
            if current is not None and (
                not start_runner or current.runner is not None
            ):
                return current
            if current is None:
                current = _ProjectRunner(
                    queue=PostgresJobQueue(
                        self._engine,
                        organization_id=organization_id,
                        project_id=project_id,
                        terminal_audit=self._audit,
                    ),
                    runner=None,
                )
                self._projects[scope] = current
            if not start_runner:
                return current
            if self._handler is None:
                raise LinkRestorationUnavailable(
                    "link restoration runner is not configured"
                )
            runner = authorized_batch_runner(
                current.queue,
                self._handler,
                access=self._access,
                operations=(LINK_RESTORATION_OPERATION,),
            )
            current.runner = runner
            try:
                runner.start()
            except Exception:
                current.runner = None
                runner.stop()
                raise
            return current

    def start_existing(self) -> None:
        if self._handler is None:
            return
        with self._engine.connect() as connection:
            scopes = connection.execute(
                sa.select(
                    background_jobs.c.organization_id,
                    background_jobs.c.project_id,
                )
                .where(
                    background_jobs.c.operation
                    == LINK_RESTORATION_OPERATION,
                    background_jobs.c.status.in_(ACTIVE_JOB_STATUSES),
                )
                .distinct()
            ).all()
        for organization_id, project_id in scopes:
            project = self._ensure_project(
                str(organization_id),
                str(project_id),
                start_runner=True,
            )
            if project.runner is None:
                raise LinkRestorationUnavailable(
                    "link restoration runner did not start"
                )
            project.runner.wake()

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
            ensure_action_allowed(task, ACTION_RESTORE_LINKS)
        except WorkflowActionNotAllowed as exc:
            raise JobConflict("link restoration is not allowed") from exc
        source_hash = content_hash(task.initial_article)
        candidate_hash = content_hash(task.humanized_article)
        if (
            not task.initial_article.strip()
            or task.initial_article_hash != source_hash
        ):
            raise JobConflict("source article identity is invalid")
        if (
            not task.humanized_article.strip()
            or task.humanized_article_hash != candidate_hash
        ):
            raise JobConflict("candidate article identity is invalid")
        if (
            not task.final_ai_check.confirmed
            or task.final_ai_check.article_hash != candidate_hash
        ):
            raise JobConflict("final AI check identity is invalid")
        try:
            assert_no_unexpected_candidate_links(
                task.initial_article,
                task.humanized_article,
            )
        except LinkRestorationError as exc:
            raise JobConflict(
                "candidate article link set is invalid"
            ) from exc
        source_link_count = sum(
            int(item.get("count") or 0)
            for item in extract_link_inventory(task.initial_article)
        )
        template = LinkTemplateReference.current()
        project = self._ensure_project(
            actor.organization_id,
            project_id,
            start_runner=True,
        )
        try:
            with self._engine.begin() as connection:
                facts = (
                    self._access_repository.lock_project_access_in_connection(
                        connection,
                        actor,
                        project_id,
                    )
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
                request = {
                    **template.private_values(),
                    "source_article_hash": source_hash,
                    "candidate_article_hash": candidate_hash,
                    "source_link_count": source_link_count,
                }
                batch = project.queue.create_batch_in_transaction(
                    connection,
                    LINK_RESTORATION_OPERATION,
                    [
                        {
                            "task_id": task_id,
                            "source_revision": source_revision,
                            "customer": project_id,
                            "topic_index": int(row.topic_index),
                            "request": request,
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
                        LINK_RESTORATION_OPERATION,
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
                        action="article.link_restoration.queued",
                        target_type="background_job",
                        target_id=job_id,
                        details={
                            "operation": LINK_RESTORATION_OPERATION,
                            "source_link_count": source_link_count,
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
            raise LinkRestorationUnavailable(
                "link restoration could not be queued"
            ) from exc
        if project.runner is None:
            raise LinkRestorationUnavailable(
                "link restoration runner did not start"
            )
        project.runner.wake()
        return self._public_job(job)

    def get_job(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        task_id: str,
        job_id: str,
    ) -> dict[str, object]:
        self._access.require(actor, project_id, "project.view")
        project = self._ensure_project(
            actor.organization_id,
            project_id,
            start_runner=False,
        )
        job = project.queue.get_job(job_id)
        if (
            str(job["task_id"]) != task_id
            or str(job["operation"]) != LINK_RESTORATION_OPERATION
        ):
            raise KeyError(job_id)
        return self._public_job(job)

    @staticmethod
    def _public_job(job: Mapping[str, object]) -> dict[str, object]:
        def optional_text(value: object) -> str | None:
            normalized = "" if value is None else str(value).strip()
            return normalized or None

        return {
            "job_id": str(job["id"]),
            "batch_id": str(job["batch_id"]),
            "task_id": str(job["task_id"]),
            "operation": str(job["operation"]),
            "status": str(job["status"]),
            "source_revision": int(job["source_revision"]),
            "result_revision": (
                None
                if job.get("result_revision") is None
                else int(job["result_revision"])
            ),
            "attempts": int(job["attempts"]),
            "created_at": str(job["created_at"]),
            "started_at": optional_text(job.get("started_at")),
            "finished_at": optional_text(job.get("finished_at")),
            "updated_at": str(job["updated_at"]),
            "has_error": bool(str(job.get("error") or "")),
        }

    def stop(
        self,
        *,
        timeout_seconds: float = 10.0,
    ) -> LinkRestorationStopReport:
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative")
        with self._lock:
            if self._closed:
                return self._stop_report or LinkRestorationStopReport(
                    project_runner_count=0,
                    dispatcher_stopped=True,
                    remaining_jobs=0,
                )
            self._closed = True
            runners = [
                project.runner
                for project in self._projects.values()
                if project.runner is not None
            ]
            self._projects.clear()
        deadline = time.monotonic() + timeout_seconds
        dispatcher_stopped = True
        remaining_jobs = 0
        for runner in runners:
            report = runner.stop(
                timeout_seconds=max(
                    0.0,
                    deadline - time.monotonic(),
                )
            )
            dispatcher_stopped = (
                dispatcher_stopped and report.dispatcher_stopped
            )
            remaining_jobs += report.remaining_jobs
        result = LinkRestorationStopReport(
            project_runner_count=len(runners),
            dispatcher_stopped=dispatcher_stopped,
            remaining_jobs=remaining_jobs,
        )
        with self._lock:
            self._stop_report = result
        return result


__all__ = [
    "LINK_RESTORATION_OPERATION",
    "LinkRestorationProvider",
    "LinkRestorationStopReport",
    "LinkRestorationUnavailable",
    "LinkTemplateReference",
    "LlmServerLinkRestorationProvider",
    "MAX_RESTORED_ARTICLE_CHARACTERS",
    "ServerLinkRestorationHandler",
    "ServerLinkRestorationRegistry",
    "apply_restored_links",
]
