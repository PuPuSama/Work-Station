from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Literal, cast

from pydantic import ValidationError
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from config import AppConfig
from models import PromptSnapshot, TaskRecord
from services.access_control import (
    ActorIdentity,
    PostgresProjectAccessRepository,
    ProjectAccessDenied,
    ProjectAccessService,
)
from services.audit_log import AuditEventWriter
from services.generator import normalized_article_word_count, primary_keyword
from services.postgres_task_repository import PostgresTaskRepository
from services.server_article_generation import build_server_article_prompt
from services.server_outline_generation import (
    PostgresPublishedGenerationContext,
    build_server_outline_prompt,
)
from services.server_project_prompts import (
    PostgresProjectPromptService,
    ServerProjectPromptError,
    ServerProjectPromptUnavailable,
)
from services.server_request_security import AuthorizedProjectRequest
from services.server_task_commands import (
    PostgresAuditedTaskWriter,
    ServerTaskAuditAction,
    ServerTaskCommandUnavailable,
)
from storage import RevisionConflictError


PromptPreviewKind = Literal["outline", "article"]

_MAX_NOTES_LENGTH = 30_000
_MAX_CUSTOM_PROMPT_LENGTH = 40_000
_MAX_SELECTION_LENGTH = 255


@dataclass(frozen=True, slots=True)
class ServerTaskWritingSettings:
    """The complete mutable writing-settings surface for one Task."""

    topic_notes: str = ""
    outline_custom_prompt: str = ""
    article_custom_prompt: str = ""
    use_outline_custom_prompt: bool = False
    use_article_custom_prompt: bool = False
    outline_prompt_selection: str = "project_default"
    article_prompt_selection: str = "project_default"
    include_project_introduction: bool = True
    include_project_notes: bool = True
    include_topic_notes: bool = True


@dataclass(frozen=True, slots=True)
class ServerTaskWritingSettingsPreview:
    """A current-time prompt render; generation still pins at enqueue time."""

    snapshot: PromptSnapshot
    effective_prompt: str
    context_chunk_count: int
    target_words: int


class ServerTaskWritingSettingsError(ValueError):
    """Writing settings violate the public service contract."""


class ServerTaskWritingSettingsConflict(RuntimeError):
    """The requested preview conflicts with the current Task state."""


class ServerTaskWritingSettingsUnavailable(RuntimeError):
    """Writing settings could not be read or committed safely."""


def _normalized_text(value: object, field_name: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise ServerTaskWritingSettingsError(f"{field_name} must be text")
    if len(value) > max_length:
        raise ServerTaskWritingSettingsError(f"{field_name} is too long")
    return value.replace("\r\n", "\n").replace("\r", "\n").strip()


def _normalized_selection(value: object, field_name: str) -> str:
    normalized = _normalized_text(value, field_name, _MAX_SELECTION_LENGTH)
    return normalized or "project_default"


def _validated_bool(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ServerTaskWritingSettingsError(f"{field_name} must be boolean")
    return value


def _normalize_settings(
    settings: ServerTaskWritingSettings,
) -> ServerTaskWritingSettings:
    if not isinstance(settings, ServerTaskWritingSettings):
        raise ServerTaskWritingSettingsError("writing settings are invalid")
    return ServerTaskWritingSettings(
        topic_notes=_normalized_text(
            settings.topic_notes,
            "topic_notes",
            _MAX_NOTES_LENGTH,
        ),
        outline_custom_prompt=_normalized_text(
            settings.outline_custom_prompt,
            "outline_custom_prompt",
            _MAX_CUSTOM_PROMPT_LENGTH,
        ),
        article_custom_prompt=_normalized_text(
            settings.article_custom_prompt,
            "article_custom_prompt",
            _MAX_CUSTOM_PROMPT_LENGTH,
        ),
        use_outline_custom_prompt=_validated_bool(
            settings.use_outline_custom_prompt,
            "use_outline_custom_prompt",
        ),
        use_article_custom_prompt=_validated_bool(
            settings.use_article_custom_prompt,
            "use_article_custom_prompt",
        ),
        outline_prompt_selection=_normalized_selection(
            settings.outline_prompt_selection,
            "outline_prompt_selection",
        ),
        article_prompt_selection=_normalized_selection(
            settings.article_prompt_selection,
            "article_prompt_selection",
        ),
        include_project_introduction=_validated_bool(
            settings.include_project_introduction,
            "include_project_introduction",
        ),
        include_project_notes=_validated_bool(
            settings.include_project_notes,
            "include_project_notes",
        ),
        include_topic_notes=_validated_bool(
            settings.include_topic_notes,
            "include_topic_notes",
        ),
    )


def _apply_settings(
    task: TaskRecord,
    settings: ServerTaskWritingSettings,
) -> None:
    task.topic_notes = settings.topic_notes
    task.outline_custom_prompt = settings.outline_custom_prompt
    task.article_custom_prompt = settings.article_custom_prompt
    task.use_outline_custom_prompt = settings.use_outline_custom_prompt
    task.use_article_custom_prompt = settings.use_article_custom_prompt
    task.outline_prompt_selection = settings.outline_prompt_selection
    task.article_prompt_selection = settings.article_prompt_selection
    task.include_project_introduction = settings.include_project_introduction
    task.include_project_notes = settings.include_project_notes
    task.include_topic_notes = settings.include_topic_notes


class PostgresServerTaskWritingSettingsService:
    """Update or preview Task writing settings inside an authorized scope."""

    def __init__(
        self,
        engine: Engine,
        config: AppConfig,
        *,
        organization_id: str,
        project_id: str,
        audit: AuditEventWriter | None = None,
    ) -> None:
        self._engine = engine
        self._config = config
        self._repository = PostgresTaskRepository(
            engine,
            organization_id=organization_id,
            project_id=project_id,
        )
        self._access = ProjectAccessService(
            PostgresProjectAccessRepository(engine)
        )
        self._prompts = PostgresProjectPromptService(
            engine,
            organization_id=organization_id,
            project_id=project_id,
            audit=audit,
        )
        self._writer = PostgresAuditedTaskWriter(
            engine,
            organization_id=organization_id,
            project_id=project_id,
            audit=audit,
        )
        self._context = PostgresPublishedGenerationContext(engine)
        self.organization_id = self._repository.organization_id
        self.project_id = self._repository.project_id

    def _require(
        self,
        actor: ActorIdentity,
        permission: Literal["project.view", "article.edit"],
    ) -> None:
        if actor.organization_id != self.organization_id:
            raise ProjectAccessDenied("project access denied")
        self._access.require(actor, self.project_id, permission)

    def _task(self, task_id: str, expected_revision: int) -> TaskRecord:
        if not isinstance(task_id, str) or not task_id.strip():
            raise ServerTaskWritingSettingsError("task_id is required")
        if (
            not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 0
        ):
            raise ServerTaskWritingSettingsError(
                "expected_revision must be a non-negative integer"
            )
        payload = self._repository.get(task_id.strip())
        if payload is None:
            raise KeyError(task_id.strip())
        task = TaskRecord.model_validate(payload)
        if task.revision != expected_revision:
            raise RevisionConflictError(
                task.id,
                expected_revision,
                task.revision,
            )
        return task

    @staticmethod
    def _context_query(task: TaskRecord) -> str:
        return " ".join(
            value
            for value in (
                task.selected_title,
                task.topic,
                primary_keyword(task),
            )
            if value.strip()
        )

    def update(
        self,
        actor: ActorIdentity,
        task_id: str,
        expected_revision: int,
        settings: ServerTaskWritingSettings,
    ) -> TaskRecord:
        normalized = _normalize_settings(settings)
        try:
            self._require(actor, "article.edit")
            current = self._task(task_id, expected_revision)
            updated = copy.deepcopy(current)
            _apply_settings(updated, normalized)
            with self._engine.begin() as connection:
                outline_snapshot = (
                    self._prompts.resolve_for_update_in_transaction(
                        connection,
                        actor,
                        kind="outline",
                        selection=normalized.outline_prompt_selection,
                    )
                )
                article_snapshot = (
                    self._prompts.resolve_for_update_in_transaction(
                        connection,
                        actor,
                        kind="article",
                        selection=normalized.article_prompt_selection,
                    )
                )
                details: dict[str, object] = {
                    "topic_notes_changed": (
                        current.topic_notes != normalized.topic_notes
                    ),
                    "outline_custom_prompt_changed": (
                        current.outline_custom_prompt
                        != normalized.outline_custom_prompt
                    ),
                    "article_custom_prompt_changed": (
                        current.article_custom_prompt
                        != normalized.article_custom_prompt
                    ),
                    "outline_prompt_selection_changed": (
                        current.outline_prompt_selection
                        != normalized.outline_prompt_selection
                    ),
                    "article_prompt_selection_changed": (
                        current.article_prompt_selection
                        != normalized.article_prompt_selection
                    ),
                    "use_outline_custom_prompt": (
                        normalized.use_outline_custom_prompt
                    ),
                    "use_article_custom_prompt": (
                        normalized.use_article_custom_prompt
                    ),
                    "include_project_introduction": (
                        normalized.include_project_introduction
                    ),
                    "include_project_notes": normalized.include_project_notes,
                    "include_topic_notes": normalized.include_topic_notes,
                    "outline_prompt_source": outline_snapshot.source,
                    "outline_prompt_version": outline_snapshot.version,
                    "article_prompt_source": article_snapshot.source,
                    "article_prompt_version": article_snapshot.version,
                }
                return self._writer.put_in_transaction(
                    connection,
                    updated,
                    expected_revision=expected_revision,
                    actor=actor,
                    action=cast(
                        ServerTaskAuditAction,
                        "article.writing_settings.updated",
                    ),
                    details=details,
                )
        except (
            KeyError,
            ProjectAccessDenied,
            RevisionConflictError,
            ServerTaskWritingSettingsError,
        ):
            raise
        except ServerTaskCommandUnavailable as exc:
            raise ServerTaskWritingSettingsUnavailable(
                "writing settings could not be committed"
            ) from exc
        except ServerProjectPromptError as exc:
            raise ServerTaskWritingSettingsError(
                "selected prompt is unavailable"
            ) from exc
        except ServerProjectPromptUnavailable as exc:
            raise ServerTaskWritingSettingsUnavailable(
                "writing settings could not be committed"
            ) from exc
        except (SQLAlchemyError, ValidationError, RuntimeError) as exc:
            raise ServerTaskWritingSettingsUnavailable(
                "writing settings could not be committed"
            ) from exc

    def preview(
        self,
        actor: ActorIdentity,
        task_id: str,
        expected_revision: int,
        kind: PromptPreviewKind,
        settings: ServerTaskWritingSettings,
    ) -> ServerTaskWritingSettingsPreview:
        normalized = _normalize_settings(settings)
        if not isinstance(kind, str) or kind not in {"outline", "article"}:
            raise ServerTaskWritingSettingsError(
                "prompt preview kind is unsupported"
            )
        preview_kind = cast(PromptPreviewKind, kind)
        try:
            self._require(actor, "project.view")
            task = copy.deepcopy(self._task(task_id, expected_revision))
            _apply_settings(task, normalized)
            if preview_kind == "article" and (
                not task.selected_title.strip() or not task.outline.strip()
            ):
                raise ServerTaskWritingSettingsConflict(
                    "confirmed title and outline are required"
                )
            selection = (
                task.outline_prompt_selection
                if preview_kind == "outline"
                else task.article_prompt_selection
            )
            snapshot = self._prompts.resolve(
                actor,
                kind=preview_kind,
                selection=selection,
            )
            chunks = self._context.select(
                project_id=self.project_id,
                query=self._context_query(task),
            )
            target_words = normalized_article_word_count(
                None,
                self._config.default_word_count,
            )
            if preview_kind == "outline":
                effective_prompt = build_server_outline_prompt(
                    self._config,
                    task,
                    prompt_snapshot=snapshot,
                    context_chunks=chunks,
                )
            else:
                effective_prompt = build_server_article_prompt(
                    task,
                    target_words=target_words,
                    prompt_snapshot=snapshot,
                    context_chunks=chunks,
                )
            self._require(actor, "project.view")
            return ServerTaskWritingSettingsPreview(
                snapshot=snapshot,
                effective_prompt=effective_prompt,
                context_chunk_count=len(chunks),
                target_words=target_words,
            )
        except (
            KeyError,
            ProjectAccessDenied,
            RevisionConflictError,
            ServerTaskWritingSettingsConflict,
            ServerTaskWritingSettingsError,
        ):
            raise
        except ServerProjectPromptError as exc:
            raise ServerTaskWritingSettingsError(
                "selected prompt is unavailable"
            ) from exc
        except ServerProjectPromptUnavailable as exc:
            raise ServerTaskWritingSettingsUnavailable(
                "writing settings preview is temporarily unavailable"
            ) from exc
        except (
            SQLAlchemyError,
            ValidationError,
            OSError,
            RuntimeError,
            ValueError,
        ) as exc:
            raise ServerTaskWritingSettingsUnavailable(
                "writing settings preview is temporarily unavailable"
            ) from exc


class ServerTaskWritingSettingsServiceFactory:
    """Create a writing-settings service from an authorized project scope."""

    def __init__(
        self,
        engine: Engine,
        config: AppConfig,
        *,
        audit: AuditEventWriter | None = None,
    ) -> None:
        self._engine = engine
        self._config = config
        self._audit = audit

    def create(
        self,
        authorized: AuthorizedProjectRequest,
    ) -> PostgresServerTaskWritingSettingsService:
        return PostgresServerTaskWritingSettingsService(
            self._engine,
            self._config,
            organization_id=authorized.actor.organization_id,
            project_id=authorized.project_id,
            audit=self._audit,
        )


__all__ = [
    "PostgresServerTaskWritingSettingsService",
    "PromptPreviewKind",
    "ServerTaskWritingSettings",
    "ServerTaskWritingSettingsConflict",
    "ServerTaskWritingSettingsError",
    "ServerTaskWritingSettingsPreview",
    "ServerTaskWritingSettingsServiceFactory",
    "ServerTaskWritingSettingsUnavailable",
]
