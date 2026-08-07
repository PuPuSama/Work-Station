from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Literal

from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from models import SCHEMA_VERSION, TaskRecord
from services.access_control import (
    ActorIdentity,
    PostgresProjectAccessRepository,
    ProjectAccessDenied,
    ProjectPermission,
    decide_project_permission,
)
from services.audit_log import (
    AuditEvent,
    AuditEventWriter,
    PostgresAuditEventWriter,
)
from services.postgres_task_repository import PostgresTaskRepository
from storage import RevisionConflictError, now_iso


ServerTaskAuditAction = Literal[
    "article.task.rewritten",
    "article.titles.generated",
    "article.title.selected",
    "article.products.generated",
    "article.outline.updated",
    "article.outline_version.restored",
    "article.writing_settings.updated",
    "article.draft.generated",
    "article.draft.regenerated",
    "article.initial_ai_screenshot.uploaded",
    "article.initial_ai_check.updated",
    "article.humanized.updated",
    "article.humanized.generated",
    "article.links.restored",
    "article.seo_review_settings.updated",
    "article.seo_review.generated",
    "article.seo_review.change.updated",
    "article.seo_review.applied",
    "article.seo_review.completed",
    "article.products.confirmed",
    "article.section.replaced",
    "article.images.prepared",
    "article.docx.exported",
    "article.tdk.generated",
    "article.final_ai_screenshot.uploaded",
    "article.final_ai_check.updated",
    "article.delivery.packaged",
]

SERVER_TASK_ACTION_PERMISSIONS: dict[
    ServerTaskAuditAction,
    ProjectPermission,
] = {
    "article.task.rewritten": "article.edit",
    "article.titles.generated": "article.edit",
    "article.title.selected": "article.edit",
    "article.products.generated": "article.edit",
    "article.outline.updated": "article.edit",
    "article.outline_version.restored": "article.edit",
    "article.writing_settings.updated": "article.edit",
    "article.draft.generated": "article.edit",
    "article.draft.regenerated": "article.edit",
    "article.initial_ai_screenshot.uploaded": "article.review",
    "article.initial_ai_check.updated": "article.review",
    "article.humanized.updated": "article.edit",
    "article.humanized.generated": "article.edit",
    "article.links.restored": "article.edit",
    "article.seo_review_settings.updated": "article.edit",
    "article.seo_review.generated": "article.review",
    "article.seo_review.change.updated": "article.review",
    "article.seo_review.applied": "article.edit",
    "article.seo_review.completed": "article.review",
    "article.products.confirmed": "article.edit",
    "article.section.replaced": "article.edit",
    "article.images.prepared": "article.edit",
    "article.docx.exported": "article.deliver",
    "article.tdk.generated": "article.deliver",
    "article.final_ai_screenshot.uploaded": "article.review",
    "article.final_ai_check.updated": "article.review",
    "article.delivery.packaged": "article.deliver",
}

SERVER_TASK_ACTION_DETAIL_KEYS: dict[
    ServerTaskAuditAction,
    frozenset[str],
] = {
    "article.task.rewritten": frozenset(),
    "article.titles.generated": frozenset(
        {
            "candidate_count",
            "context_chunk_count",
        }
    ),
    "article.title.selected": frozenset(
        {
            "candidate_count",
            "candidate_index",
        }
    ),
    "article.products.generated": frozenset(
        {"candidate_count", "candidate_pool_count"}
    ),
    "article.outline.updated": frozenset(
        {
            "confirmed",
            "outline_characters",
        }
    ),
    "article.outline_version.restored": frozenset(
        {
            "restored_from",
            "version_index",
        }
    ),
    "article.writing_settings.updated": frozenset(
        {
            "topic_notes_changed",
            "outline_custom_prompt_changed",
            "article_custom_prompt_changed",
            "outline_prompt_selection_changed",
            "article_prompt_selection_changed",
            "use_outline_custom_prompt",
            "use_article_custom_prompt",
            "include_project_introduction",
            "include_project_notes",
            "include_topic_notes",
            "outline_prompt_source",
            "outline_prompt_version",
            "article_prompt_source",
            "article_prompt_version",
        }
    ),
    "article.draft.generated": frozenset(
        {
            "context_chunk_count",
            "initial_word_count",
            "prompt_source",
            "prompt_version",
            "raw_word_count",
            "target_words",
        }
    ),
    "article.draft.regenerated": frozenset(
        {
            "context_chunk_count",
            "initial_word_count",
            "prompt_source",
            "prompt_version",
            "raw_word_count",
            "target_words",
        }
    ),
    "article.initial_ai_screenshot.uploaded": frozenset(
        {
            "screenshot_height",
            "screenshot_width",
        }
    ),
    "article.initial_ai_check.updated": frozenset(
        {
            "confirmed",
            "score_recorded",
        }
    ),
    "article.humanized.updated": frozenset(
        {"humanized_word_count"}
    ),
    "article.humanized.generated": frozenset(
        {
            "article_characters",
            "prompt_source",
            "prompt_version",
            "rehumanizing",
        }
    ),
    "article.links.restored": frozenset(
        {
            "restored_link_count",
            "source_link_count",
        }
    ),
    "article.seo_review_settings.updated": frozenset(
        {
            "long_tail_keyword_count",
            "prompt_source",
            "prompt_version",
        }
    ),
    "article.seo_review.generated": frozenset(
        {
            "change_count",
            "context_chunk_count",
            "dimension_count",
            "prompt_source",
            "prompt_version",
            "publish_ready",
        }
    ),
    "article.seo_review.change.updated": frozenset(
        {"decision", "risk_confirmed", "risk_count"}
    ),
    "article.seo_review.applied": frozenset(
        {
            "accepted_count",
            "invalid_count",
            "pending_count",
            "rejected_count",
        }
    ),
    "article.seo_review.completed": frozenset(
        {
            "accepted_count",
            "invalid_count",
            "pending_count",
            "rejected_count",
        }
    ),
    "article.products.confirmed": frozenset({"product_count"}),
    "article.section.replaced": frozenset({"heading_depth"}),
    "article.images.prepared": frozenset({"image_count"}),
    "article.docx.exported": frozenset({"image_count"}),
    "article.tdk.generated": frozenset(
        {
            "description_characters",
            "keyword_count",
        }
    ),
    "article.final_ai_screenshot.uploaded": frozenset(
        {
            "screenshot_height",
            "screenshot_width",
        }
    ),
    "article.final_ai_check.updated": frozenset(
        {
            "confirmed",
            "score_recorded",
        }
    ),
    "article.delivery.packaged": frozenset(
        {
            "file_count",
            "image_count",
        }
    ),
}

_BASE_DETAIL_KEYS = frozenset(
    {"from_revision", "to_revision", "status"}
)


class ServerTaskCommandUnavailable(RuntimeError):
    """The Task and its mandatory AuditEvent could not commit together."""


def _event_id(
    *,
    organization_id: str,
    project_id: str,
    task_id: str,
    revision: int,
    action: str,
) -> str:
    identity = "\n".join(
        (
            organization_id,
            project_id,
            task_id,
            str(revision),
            action,
        )
    )
    return f"task_{uuid.uuid5(uuid.NAMESPACE_URL, identity).hex}"


class PostgresAuditedTaskWriter:
    """Commit one scoped Task CAS and append its AuditEvent atomically."""

    def __init__(
        self,
        engine: Engine,
        *,
        organization_id: str,
        project_id: str,
        audit: AuditEventWriter | None = None,
    ) -> None:
        self._engine = engine
        self._repository = PostgresTaskRepository(
            engine,
            organization_id=organization_id,
            project_id=project_id,
        )
        self._access = PostgresProjectAccessRepository(engine)
        self._audit = audit or PostgresAuditEventWriter()
        self.organization_id = self._repository.organization_id
        self.project_id = self._repository.project_id

    def put(
        self,
        task: TaskRecord,
        *,
        expected_revision: int,
        actor: ActorIdentity,
        action: ServerTaskAuditAction,
        details: Mapping[str, object] | None = None,
    ) -> TaskRecord:
        """Open the business transaction for one audited Task command."""

        try:
            with self._engine.begin() as connection:
                return self.put_in_transaction(
                    connection,
                    task,
                    expected_revision=expected_revision,
                    actor=actor,
                    action=action,
                    details=details,
                )
        except (ProjectAccessDenied, RevisionConflictError):
            raise
        except (SQLAlchemyError, RuntimeError) as exc:
            raise ServerTaskCommandUnavailable(
                "server task command is temporarily unavailable"
            ) from exc

    def put_in_transaction(
        self,
        connection: Connection,
        task: TaskRecord,
        *,
        expected_revision: int,
        actor: ActorIdentity,
        action: ServerTaskAuditAction,
        details: Mapping[str, object] | None = None,
    ) -> TaskRecord:
        """Reauthorize, CAS, and audit without storing article content in audit."""

        if not connection.in_transaction():
            raise ValueError(
                "server task commands require a business transaction"
            )
        if actor.organization_id != self.organization_id:
            raise ProjectAccessDenied("project access denied")
        if task.revision != expected_revision:
            raise RevisionConflictError(
                task.id,
                expected_revision,
                task.revision,
            )
        operation_details = dict(details or {})
        if _BASE_DETAIL_KEYS.intersection(operation_details):
            raise ValueError("audit details contain reserved keys")
        unsupported_keys = (
            operation_details.keys()
            - SERVER_TASK_ACTION_DETAIL_KEYS[action]
        )
        if unsupported_keys:
            raise ValueError(
                "audit details contain unsupported keys"
            )

        original_revision = task.revision
        original_schema_version = task.schema_version
        original_updated_at = task.updated_at
        task.revision = expected_revision + 1
        task.schema_version = max(SCHEMA_VERSION, task.schema_version)
        task.updated_at = now_iso()
        audit_details = {
            "from_revision": expected_revision,
            "to_revision": task.revision,
            "status": task.status,
            **operation_details,
        }
        try:
            permission = SERVER_TASK_ACTION_PERMISSIONS[action]
            facts = self._access.lock_project_access_in_connection(
                connection,
                actor,
                self.project_id,
            )
            if not decide_project_permission(
                facts,
                permission,
            ).allowed:
                raise ProjectAccessDenied("project access denied")
            persisted = (
                self._repository.put_if_revision_in_transaction(
                    connection,
                    task.model_dump(mode="json"),
                    expected_revision=expected_revision,
                )
            )
            if not persisted:
                actual = (
                    self._repository.current_revision_in_transaction(
                        connection,
                        task.id,
                    )
                    or 0
                )
                raise RevisionConflictError(
                    task.id,
                    expected_revision,
                    actual,
                )
            self._audit.append(
                connection,
                AuditEvent(
                    organization_id=self.organization_id,
                    event_id=_event_id(
                        organization_id=self.organization_id,
                        project_id=self.project_id,
                        task_id=task.id,
                        revision=task.revision,
                        action=action,
                    ),
                    actor_user_id=actor.user_id,
                    project_id=self.project_id,
                    action=action,
                    target_type="article_task",
                    target_id=task.id,
                    details=audit_details,
                ),
            )
        except Exception:
            task.revision = original_revision
            task.schema_version = original_schema_version
            task.updated_at = original_updated_at
            raise
        return task


__all__ = [
    "PostgresAuditedTaskWriter",
    "SERVER_TASK_ACTION_DETAIL_KEYS",
    "SERVER_TASK_ACTION_PERMISSIONS",
    "ServerTaskAuditAction",
    "ServerTaskCommandUnavailable",
]
