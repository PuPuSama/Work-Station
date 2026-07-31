from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from knowledge_agent.schema import projects
from models import STATUS_NEW, TaskRecord
from server_schema import article_tasks, task_intakes, task_store_state
from services.access_control import (
    ActorIdentity,
    PostgresProjectAccessRepository,
    ProjectAccessDenied,
    decide_project_permission,
)
from services.audit_log import (
    AuditEvent,
    AuditEventWriter,
    PostgresAuditEventWriter,
)
from services.postgres_task_repository import PostgresTaskRepository
from storage import now_iso


TaskIntakeKind = Literal["manual", "row_import"]


class ServerTaskIntakeConflict(RuntimeError):
    """The idempotency identity or a generated Task identity conflicted."""


class ServerTaskIntakeUnavailable(RuntimeError):
    """The atomic Task intake transaction could not be committed."""


def _required_text(
    value: str,
    field_name: str,
    *,
    maximum: int,
) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    if len(normalized) > maximum:
        raise ValueError(f"{field_name} is too long")
    return normalized


def _optional_text(
    value: str,
    field_name: str,
    *,
    maximum: int,
) -> str:
    normalized = str(value or "").strip()
    if len(normalized) > maximum:
        raise ValueError(f"{field_name} is too long")
    return normalized


def _optional_http_url(value: str) -> str:
    normalized = _optional_text(
        value,
        "competitor_blog",
        maximum=2048,
    )
    if not normalized:
        return ""
    parsed = urlsplit(normalized)
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError(
            "competitor_blog must be an HTTP(S) URL without credentials"
        )
    return normalized


@dataclass(frozen=True, slots=True)
class ServerTaskIntakeRow:
    topic: str
    competitor_keyword: str = ""
    competitor_blog: str = ""

    def normalized(self) -> ServerTaskIntakeRow:
        return ServerTaskIntakeRow(
            topic=_required_text(
                self.topic,
                "topic",
                maximum=500,
            ),
            competitor_keyword=_optional_text(
                self.competitor_keyword,
                "competitor_keyword",
                maximum=500,
            ),
            competitor_blog=_optional_http_url(
                self.competitor_blog,
            ),
        )


@dataclass(frozen=True, slots=True)
class ServerTaskIntakeResult:
    intake_id: str
    intake_kind: TaskIntakeKind
    source_name: str
    source_digest: str
    created: bool
    tasks: tuple[TaskRecord, ...]


def _payload_digest(
    *,
    intake_kind: TaskIntakeKind,
    source_name: str,
    rows: tuple[ServerTaskIntakeRow, ...],
) -> str:
    canonical = json.dumps(
        {
            "intake_kind": intake_kind,
            "source_name": source_name,
            "rows": [
                {
                    "competitor_blog": row.competitor_blog,
                    "competitor_keyword": row.competitor_keyword,
                    "topic": row.topic,
                }
                for row in rows
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _stable_id(prefix: str, *parts: str) -> str:
    identity = "\n".join(parts)
    return f"{prefix}_{uuid.uuid5(uuid.NAMESPACE_URL, identity).hex}"


class PostgresServerTaskIntakeService:
    """Create scoped Tasks without reading a Local workbook or filesystem.

    The caller supplies only topic rows plus an idempotency identity. This
    service allocates Task IDs and topic indexes under the project TaskStore
    lock, records a content-free receipt, and appends the AuditEvent in the
    same transaction. A retry with the same normalized payload returns the
    original Tasks; reusing the identity for different input fails closed.
    """

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

    def create_manual(
        self,
        *,
        actor: ActorIdentity,
        intake_id: str,
        row: ServerTaskIntakeRow,
    ) -> ServerTaskIntakeResult:
        return self._commit(
            actor=actor,
            intake_id=intake_id,
            intake_kind="manual",
            source_name="manual",
            rows=(row,),
        )

    def import_rows(
        self,
        *,
        actor: ActorIdentity,
        intake_id: str,
        source_name: str,
        rows: tuple[ServerTaskIntakeRow, ...],
    ) -> ServerTaskIntakeResult:
        return self._commit(
            actor=actor,
            intake_id=intake_id,
            intake_kind="row_import",
            source_name=source_name,
            rows=rows,
        )

    def _commit(
        self,
        *,
        actor: ActorIdentity,
        intake_id: str,
        intake_kind: TaskIntakeKind,
        source_name: str,
        rows: tuple[ServerTaskIntakeRow, ...],
    ) -> ServerTaskIntakeResult:
        normalized_intake = _required_text(
            intake_id,
            "intake_id",
            maximum=128,
        )
        normalized_source = _required_text(
            source_name,
            "source_name",
            maximum=255,
        )
        normalized_rows = tuple(row.normalized() for row in rows)
        if not normalized_rows:
            raise ValueError("at least one Task row is required")
        if len(normalized_rows) > 200:
            raise ValueError("one Task import cannot exceed 200 rows")
        digest = _payload_digest(
            intake_kind=intake_kind,
            source_name=normalized_source,
            rows=normalized_rows,
        )
        try:
            with self._engine.begin() as connection:
                return self._commit_in_transaction(
                    connection,
                    actor=actor,
                    intake_id=normalized_intake,
                    intake_kind=intake_kind,
                    source_name=normalized_source,
                    rows=normalized_rows,
                    digest=digest,
                )
        except (ProjectAccessDenied, ServerTaskIntakeConflict):
            raise
        except (SQLAlchemyError, RuntimeError) as exc:
            raise ServerTaskIntakeUnavailable(
                "server task intake is temporarily unavailable"
            ) from exc

    def _commit_in_transaction(
        self,
        connection: Connection,
        *,
        actor: ActorIdentity,
        intake_id: str,
        intake_kind: TaskIntakeKind,
        source_name: str,
        rows: tuple[ServerTaskIntakeRow, ...],
        digest: str,
    ) -> ServerTaskIntakeResult:
        if actor.organization_id != self.organization_id:
            raise ProjectAccessDenied("project access denied")
        facts = self._access.lock_project_access_in_connection(
            connection,
            actor,
            self.project_id,
        )
        if not decide_project_permission(
            facts,
            "article.edit",
        ).allowed:
            raise ProjectAccessDenied("project access denied")

        # Serialize a single idempotency identity without storing a pending
        # receipt that could outlive a rolled-back Task transaction.
        connection.execute(
            sa.select(
                sa.func.pg_advisory_xact_lock(
                    sa.func.hashtextextended(
                        "\n".join(
                            (
                                self.organization_id,
                                self.project_id,
                                intake_id,
                            )
                        ),
                        0,
                    )
                )
            )
        ).scalar_one()
        existing = connection.execute(
            sa.select(task_intakes).where(
                task_intakes.c.organization_id
                == self.organization_id,
                task_intakes.c.project_id == self.project_id,
                task_intakes.c.intake_id == intake_id,
            )
        ).mappings().one_or_none()
        if existing is not None:
            if (
                str(existing["intake_kind"]) != intake_kind
                or str(existing["payload_digest"]) != digest
            ):
                raise ServerTaskIntakeConflict(
                    "task intake identity was already used for different input"
                )
            return ServerTaskIntakeResult(
                intake_id=intake_id,
                intake_kind=intake_kind,
                source_name=str(existing["source_name"]),
                source_digest=digest,
                created=False,
                tasks=self._load_receipt_tasks(
                    connection,
                    tuple(str(value) for value in existing["task_ids"]),
                ),
            )

        project = connection.execute(
            sa.select(
                projects.c.customer_name,
                projects.c.official_domain,
            ).where(
                projects.c.project_id == self.project_id,
                projects.c.status == "active",
            )
        ).mappings().one_or_none()
        if project is None:
            raise ProjectAccessDenied("project access denied")

        state_insert = insert(task_store_state).values(
            organization_id=self.organization_id,
            project_id=self.project_id,
            initialized=False,
        )
        connection.execute(
            state_insert.on_conflict_do_nothing(
                index_elements=[
                    task_store_state.c.organization_id,
                    task_store_state.c.project_id,
                ]
            )
        )
        connection.execute(
            sa.select(task_store_state.c.initialized)
            .where(
                task_store_state.c.organization_id
                == self.organization_id,
                task_store_state.c.project_id == self.project_id,
            )
            .with_for_update()
        ).scalar_one()
        maximum_topic_index = int(
            connection.execute(
                sa.select(
                    sa.func.coalesce(
                        sa.func.max(article_tasks.c.topic_index),
                        0,
                    )
                ).where(
                    article_tasks.c.organization_id
                    == self.organization_id,
                    article_tasks.c.project_id == self.project_id,
                )
            ).scalar_one()
        )

        timestamp = now_iso()
        tasks: list[TaskRecord] = []
        for offset, row in enumerate(rows, start=1):
            topic_index = maximum_topic_index + offset
            task_id = _stable_id(
                "tsk",
                self.organization_id,
                self.project_id,
                intake_id,
                str(offset),
            )[:16]
            task = TaskRecord(
                id=task_id,
                week_folder="server",
                customer=str(project["official_domain"]),
                brand_name=str(project["customer_name"]),
                source_key=(
                    f"server:{intake_kind}:{intake_id}:{offset}"
                ),
                source_kind=(
                    "manual"
                    if intake_kind == "manual"
                    else "server_import"
                ),
                topic_index=topic_index,
                topic=row.topic,
                competitor_keyword=row.competitor_keyword,
                competitor_blog=row.competitor_blog,
                status=STATUS_NEW,
                task_dir="",
                created_at=timestamp,
                updated_at=timestamp,
            )
            inserted = self._repository.put_if_revision_in_transaction(
                connection,
                task.model_dump(mode="json"),
                expected_revision=None,
            )
            if not inserted:
                raise ServerTaskIntakeConflict(
                    "generated Task identity already exists"
                )
            tasks.append(task)

        task_ids = [task.id for task in tasks]
        connection.execute(
            task_intakes.insert().values(
                organization_id=self.organization_id,
                project_id=self.project_id,
                intake_id=intake_id,
                intake_kind=intake_kind,
                source_name=source_name,
                payload_digest=digest,
                task_count=len(tasks),
                task_ids=task_ids,
                created_by_user_id=actor.user_id,
            )
        )
        self._audit.append(
            connection,
            AuditEvent(
                organization_id=self.organization_id,
                event_id=_stable_id(
                    "task_intake",
                    self.organization_id,
                    self.project_id,
                    intake_id,
                ),
                actor_user_id=actor.user_id,
                project_id=self.project_id,
                action=(
                    "article.task.created"
                    if intake_kind == "manual"
                    else "article.tasks.imported"
                ),
                target_type="task_intake",
                target_id=intake_id,
                details={
                    "intake_kind": intake_kind,
                    "task_count": len(tasks),
                    "first_topic_index": tasks[0].topic_index,
                    "last_topic_index": tasks[-1].topic_index,
                },
            ),
        )
        return ServerTaskIntakeResult(
            intake_id=intake_id,
            intake_kind=intake_kind,
            source_name=source_name,
            source_digest=digest,
            created=True,
            tasks=tuple(tasks),
        )

    def _load_receipt_tasks(
        self,
        connection: Connection,
        task_ids: tuple[str, ...],
    ) -> tuple[TaskRecord, ...]:
        rows = {
            str(row.task_id): TaskRecord.model_validate(row.payload)
            for row in connection.execute(
                sa.select(
                    article_tasks.c.task_id,
                    article_tasks.c.payload,
                ).where(
                    article_tasks.c.organization_id
                    == self.organization_id,
                    article_tasks.c.project_id == self.project_id,
                    article_tasks.c.task_id.in_(task_ids),
                )
            )
        }
        if len(rows) != len(task_ids):
            raise ServerTaskIntakeUnavailable(
                "task intake receipt is incomplete"
            )
        return tuple(rows[task_id] for task_id in task_ids)


__all__ = [
    "PostgresServerTaskIntakeService",
    "ServerTaskIntakeConflict",
    "ServerTaskIntakeResult",
    "ServerTaskIntakeRow",
    "ServerTaskIntakeUnavailable",
]
