from __future__ import annotations

import uuid
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from server_schema import project_topics
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


class ServerProjectTopicConflict(RuntimeError):
    """A concurrent or duplicate topic import disagrees with current state."""


class ServerProjectTopicUnavailable(RuntimeError):
    """The topic import could not be committed safely."""


def _text(value: str, field_name: str, *, required: bool = False) -> str:
    normalized = " ".join(str(value or "").split())
    if required and not normalized:
        raise ValueError(f"{field_name} is required")
    if len(normalized) > 500:
        raise ValueError(f"{field_name} is too long")
    return normalized


def _identity(value: str) -> str:
    return " ".join(value.casefold().split())


def _stable_id(prefix: str, *parts: str) -> str:
    return f"{prefix}_{uuid.uuid5(uuid.NAMESPACE_URL, chr(31).join(parts)).hex}"


@dataclass(frozen=True, slots=True)
class ServerProjectTopicRow:
    topic: str
    primary_keyword: str = ""
    competitor_keyword: str = ""

    def normalized(self) -> ServerProjectTopicRow:
        return ServerProjectTopicRow(
            topic=_text(self.topic, "topic", required=True),
            primary_keyword=_text(self.primary_keyword, "primary_keyword"),
            competitor_keyword=_text(
                self.competitor_keyword,
                "competitor_keyword",
            ),
        )


@dataclass(frozen=True, slots=True)
class ServerProjectTopicItem:
    topic_id: str
    topic: str
    primary_keyword: str
    competitor_keyword: str
    created: bool


@dataclass(frozen=True, slots=True)
class ServerProjectTopicImportResult:
    items: tuple[ServerProjectTopicItem, ...]


class PostgresServerProjectTopicService:
    """Import published project topics with deterministic retry identities."""

    def __init__(
        self,
        engine: Engine,
        *,
        audit: AuditEventWriter | None = None,
    ) -> None:
        self._engine = engine
        self._access = PostgresProjectAccessRepository(engine)
        self._audit = audit or PostgresAuditEventWriter()

    def import_rows(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        idempotency_key: str,
        rows: tuple[ServerProjectTopicRow, ...],
    ) -> ServerProjectTopicImportResult:
        normalized_project = str(project_id or "").strip()
        normalized_key = str(idempotency_key or "").strip()
        if not normalized_project:
            raise ValueError("project_id is required")
        if not normalized_key:
            raise ValueError("idempotency_key is required")
        normalized_rows = tuple(row.normalized() for row in rows)
        if not normalized_rows:
            raise ValueError("at least one topic row is required")
        if len(normalized_rows) > 200:
            raise ValueError("one topic import cannot exceed 200 rows")
        identities = [_identity(row.topic) for row in normalized_rows]
        if len(set(identities)) != len(identities):
            raise ValueError("topic import contains duplicate topics")

        try:
            with self._engine.begin() as connection:
                facts = self._access.lock_project_access_in_connection(
                    connection,
                    actor,
                    normalized_project,
                )
                if not decide_project_permission(facts, "article.edit").allowed:
                    raise ProjectAccessDenied("project access denied")
                connection.execute(
                    sa.select(
                        sa.func.pg_advisory_xact_lock(
                            sa.func.hashtextextended(
                                "\n".join(
                                    (
                                        "project_topic_import_v1",
                                        actor.organization_id,
                                        normalized_project,
                                    )
                                ),
                                0,
                            )
                        )
                    )
                ).scalar_one()
                current_rows = connection.execute(
                    sa.select(project_topics).where(
                        project_topics.c.organization_id
                        == actor.organization_id,
                        project_topics.c.project_id == normalized_project,
                    )
                ).mappings().all()
                current = {
                    _identity(str(row["topic"])): row for row in current_rows
                }
                results: list[ServerProjectTopicItem] = []
                created_count = 0
                for offset, row in enumerate(normalized_rows):
                    key = _identity(row.topic)
                    existing = current.get(key)
                    if existing is not None:
                        if (
                            str(existing["primary_keyword"] or "")
                            != row.primary_keyword
                            or str(existing["competitor_keyword"] or "")
                            != row.competitor_keyword
                        ):
                            raise ServerProjectTopicConflict(
                                "topic changed after proposal preview"
                            )
                        results.append(
                            ServerProjectTopicItem(
                                topic_id=str(existing["topic_id"]),
                                topic=row.topic,
                                primary_keyword=row.primary_keyword,
                                competitor_keyword=row.competitor_keyword,
                                created=False,
                            )
                        )
                        continue
                    topic_id = _stable_id(
                        "topic",
                        actor.organization_id,
                        normalized_project,
                        normalized_key,
                        str(offset),
                        key,
                    )
                    connection.execute(
                        project_topics.insert().values(
                            organization_id=actor.organization_id,
                            project_id=normalized_project,
                            topic_id=topic_id,
                            topic=row.topic,
                            primary_keyword=row.primary_keyword,
                            competitor_keyword=row.competitor_keyword,
                            status="published",
                            revision=0,
                        )
                    )
                    item = ServerProjectTopicItem(
                        topic_id=topic_id,
                        topic=row.topic,
                        primary_keyword=row.primary_keyword,
                        competitor_keyword=row.competitor_keyword,
                        created=True,
                    )
                    current[key] = {
                        "topic_id": topic_id,
                        "topic": row.topic,
                        "primary_keyword": row.primary_keyword,
                        "competitor_keyword": row.competitor_keyword,
                    }
                    results.append(item)
                    created_count += 1
                if created_count:
                    self._audit.append(
                        connection,
                        AuditEvent(
                            organization_id=actor.organization_id,
                            event_id=_stable_id(
                                "project_topic_import",
                                actor.organization_id,
                                normalized_project,
                                normalized_key,
                            ),
                            actor_user_id=actor.user_id,
                            project_id=normalized_project,
                            action="project.topics.imported",
                            target_type="project_topic_import",
                            target_id=normalized_key,
                            details={
                                "created_count": created_count,
                                "skipped_count": len(results) - created_count,
                            },
                        ),
                    )
                return ServerProjectTopicImportResult(items=tuple(results))
        except (ProjectAccessDenied, ServerProjectTopicConflict, ValueError):
            raise
        except (RuntimeError, SQLAlchemyError) as exc:
            raise ServerProjectTopicUnavailable(
                "project topic import is temporarily unavailable"
            ) from exc


__all__ = [
    "PostgresServerProjectTopicService",
    "ServerProjectTopicConflict",
    "ServerProjectTopicImportResult",
    "ServerProjectTopicItem",
    "ServerProjectTopicRow",
    "ServerProjectTopicUnavailable",
]
