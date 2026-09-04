from __future__ import annotations

import copy
from typing import Any, Iterable, Mapping

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection, Engine

from server_schema import article_tasks, task_store_state


def _required_text(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized


class PostgresTaskRepository:
    """Project-scoped TaskStore backend preserving the workflow JSON contract."""

    def __init__(
        self,
        engine: Engine,
        *,
        organization_id: str,
        project_id: str,
    ) -> None:
        self._engine = engine
        self.organization_id = _required_text(
            organization_id,
            "organization_id",
        )
        self.project_id = _required_text(project_id, "project_id")

    def _scope(self) -> tuple[sa.ColumnElement[bool], ...]:
        return (
            article_tasks.c.organization_id == self.organization_id,
            article_tasks.c.project_id == self.project_id,
        )

    def _scoped_payload(self, record: Mapping[str, Any]) -> dict[str, Any]:
        payload = copy.deepcopy(dict(record))
        existing_organization = str(payload.get("organization_id") or "").strip()
        existing_project = str(payload.get("project_id") or "").strip()
        if existing_organization and existing_organization != self.organization_id:
            raise ValueError("task organization_id does not match repository scope")
        if existing_project and existing_project != self.project_id:
            raise ValueError("task project_id does not match repository scope")
        payload["organization_id"] = self.organization_id
        payload["project_id"] = self.project_id
        return payload

    def _serialized(
        self,
        record: Mapping[str, Any],
    ) -> tuple[str, str, int, int, str, dict[str, Any]]:
        payload = self._scoped_payload(record)
        task_id = _required_text(str(payload.get("id") or ""), "task id")
        customer = str(payload.get("customer") or "")
        record_updated_at = str(payload.get("updated_at") or "")
        try:
            topic_index = int(payload.get("topic_index") or 0)
        except (TypeError, ValueError):
            topic_index = 0
        try:
            revision = int(payload.get("revision") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("task revision must be an integer") from exc
        if revision < 0:
            raise ValueError("task revision must not be negative")
        return (
            task_id,
            customer,
            topic_index,
            revision,
            record_updated_at,
            payload,
        )

    def _lock_state(self, connection: Connection) -> None:
        statement = insert(task_store_state).values(
            organization_id=self.organization_id,
            project_id=self.project_id,
            initialized=False,
        )
        connection.execute(
            statement.on_conflict_do_nothing(
                index_elements=[
                    task_store_state.c.organization_id,
                    task_store_state.c.project_id,
                ]
            )
        )
        connection.execute(
            sa.select(task_store_state.c.initialized)
            .where(
                task_store_state.c.organization_id == self.organization_id,
                task_store_state.c.project_id == self.project_id,
            )
            .with_for_update()
        ).scalar_one()

    def _mark_initialized(self, connection: Connection) -> None:
        connection.execute(
            task_store_state.update()
            .where(
                task_store_state.c.organization_id == self.organization_id,
                task_store_state.c.project_id == self.project_id,
            )
            .values(initialized=True, updated_at=sa.func.now())
        )

    def is_initialized(self) -> bool:
        with self._engine.connect() as connection:
            value = connection.execute(
                sa.select(task_store_state.c.initialized).where(
                    task_store_state.c.organization_id == self.organization_id,
                    task_store_state.c.project_id == self.project_id,
                )
            ).scalar_one_or_none()
        return bool(value)

    def load_all(self) -> list[dict[str, Any]]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(article_tasks.c.payload)
                .where(*self._scope())
                .order_by(article_tasks.c.position, article_tasks.c.task_id)
            ).scalars()
            return [copy.deepcopy(dict(payload)) for payload in rows]

    def load_recent(self, limit: int) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValueError("limit must be an integer")
        if limit < 1 or limit > 20:
            raise ValueError("limit must be between 1 and 20")
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(
                    article_tasks.c.task_id.label("id"),
                    article_tasks.c.topic_index,
                    article_tasks.c.payload["topic"].astext.label("topic"),
                    article_tasks.c.payload["selected_title"].astext.label(
                        "selected_title"
                    ),
                    article_tasks.c.payload["status"].astext.label("status"),
                    article_tasks.c.record_updated_at.label("updated_at"),
                )
                .where(*self._scope())
                .order_by(
                    article_tasks.c.record_updated_at.desc(),
                    article_tasks.c.task_id,
                )
                .limit(limit)
            ).mappings()
            return [
                {
                    "id": str(row["id"]),
                    "topic_index": int(row["topic_index"] or 0),
                    "topic": str(row["topic"] or ""),
                    "selected_title": str(row["selected_title"] or ""),
                    "status": str(row["status"] or "new"),
                    "updated_at": str(row["updated_at"] or ""),
                }
                for row in rows
            ]

    def load_metrics(self, task_ids: Iterable[str]) -> list[dict[str, Any]]:
        """Read only the fields used by batch progress cards.

        The full Task payload contains article bodies, image metadata, prompt
        snapshots, and export state. Loading it for a five-second progress
        refresh made a small metrics request scale with the size of every
        task in the project.
        """

        normalized_ids = tuple(
            dict.fromkeys(
                _required_text(str(task_id), "task_id")
                for task_id in task_ids
            )
        )
        if not normalized_ids:
            return []
        final_ai_rate = (
            article_tasks.c.payload["final_ai_check"]["score"]
            .astext.cast(sa.Float)
            .label("final_ai_rate")
        )
        coverage_rate = (
            article_tasks.c.payload["knowledge_coverage"]["sentence_coverage"]
            .astext.cast(sa.Float)
            .label("knowledge_coverage_rate")
        )
        coverage_status = (
            article_tasks.c.payload["knowledge_coverage"]["status"]
            .astext
            .label("knowledge_coverage_status")
        )
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(
                    article_tasks.c.task_id,
                    article_tasks.c.revision,
                    final_ai_rate,
                    coverage_rate,
                    coverage_status,
                )
                .where(
                    *self._scope(),
                    article_tasks.c.task_id.in_(normalized_ids),
                )
                .order_by(article_tasks.c.position, article_tasks.c.task_id)
            ).mappings()
            result: list[dict[str, Any]] = []
            for row in rows:
                status = str(
                    row["knowledge_coverage_status"] or "not_checked"
                )
                result.append(
                    {
                        "task_id": str(row["task_id"]),
                        "revision": int(row["revision"] or 0),
                        "final_ai_rate": (
                            None
                            if row["final_ai_rate"] is None
                            else float(row["final_ai_rate"])
                        ),
                        "knowledge_coverage_rate": (
                            None
                            if status != "available"
                            or row["knowledge_coverage_rate"] is None
                            else float(row["knowledge_coverage_rate"])
                        ),
                        "knowledge_coverage_status": status,
                    }
                )
            return result

    def get(self, task_id: str) -> dict[str, Any] | None:
        normalized_task_id = _required_text(task_id, "task_id")
        with self._engine.connect() as connection:
            payload = connection.execute(
                sa.select(article_tasks.c.payload).where(
                    *self._scope(),
                    article_tasks.c.task_id == normalized_task_id,
                )
            ).scalar_one_or_none()
        return copy.deepcopy(dict(payload)) if payload is not None else None

    def replace_all(self, records: Iterable[Mapping[str, Any]]) -> None:
        serialized = [self._serialized(record) for record in records]
        task_ids = [item[0] for item in serialized]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("task ids must be unique within a project")

        with self._engine.begin() as connection:
            self._lock_state(connection)
            connection.execute(article_tasks.delete().where(*self._scope()))
            if serialized:
                connection.execute(
                    article_tasks.insert(),
                    tuple(
                        {
                            "organization_id": self.organization_id,
                            "project_id": self.project_id,
                            "task_id": task_id,
                            "customer": customer,
                            "topic_index": topic_index,
                            "revision": revision,
                            "position": position,
                            "record_updated_at": record_updated_at,
                            "payload": payload,
                        }
                        for position, (
                            task_id,
                            customer,
                            topic_index,
                            revision,
                            record_updated_at,
                            payload,
                        ) in enumerate(serialized)
                    ),
                )
            self._mark_initialized(connection)

    def upsert(self, record: Mapping[str, Any]) -> None:
        (
            task_id,
            customer,
            topic_index,
            revision,
            record_updated_at,
            payload,
        ) = self._serialized(record)
        with self._engine.begin() as connection:
            self._lock_state(connection)
            position = connection.execute(
                sa.select(article_tasks.c.position).where(
                    *self._scope(),
                    article_tasks.c.task_id == task_id,
                )
            ).scalar_one_or_none()
            if position is None:
                position = int(
                    connection.execute(
                        sa.select(
                            sa.func.coalesce(
                                sa.func.max(article_tasks.c.position),
                                -1,
                            )
                        ).where(*self._scope())
                    ).scalar_one()
                ) + 1
            statement = insert(article_tasks).values(
                organization_id=self.organization_id,
                project_id=self.project_id,
                task_id=task_id,
                customer=customer,
                topic_index=topic_index,
                revision=revision,
                position=position,
                record_updated_at=record_updated_at,
                payload=payload,
            )
            connection.execute(
                statement.on_conflict_do_update(
                    index_elements=[
                        article_tasks.c.organization_id,
                        article_tasks.c.project_id,
                        article_tasks.c.task_id,
                    ],
                    set_={
                        "customer": statement.excluded.customer,
                        "topic_index": statement.excluded.topic_index,
                        "revision": statement.excluded.revision,
                        "record_updated_at": statement.excluded.record_updated_at,
                        "payload": statement.excluded.payload,
                        "updated_at": sa.func.now(),
                    },
                )
            )
            self._mark_initialized(connection)

    def put_if_revision(
        self,
        record: Mapping[str, Any],
        *,
        expected_revision: int | None,
    ) -> bool:
        """Insert a new task or update only the exact persisted revision."""

        with self._engine.begin() as connection:
            return self.put_if_revision_in_transaction(
                connection,
                record,
                expected_revision=expected_revision,
            )

    def put_if_revision_in_transaction(
        self,
        connection: Connection,
        record: Mapping[str, Any],
        *,
        expected_revision: int | None,
    ) -> bool:
        """CAS one Task inside a caller-owned business transaction."""

        if not connection.in_transaction():
            raise ValueError(
                "task CAS requires a business transaction"
            )
        (
            task_id,
            customer,
            topic_index,
            revision,
            record_updated_at,
            payload,
        ) = self._serialized(record)
        self._lock_state(connection)
        if expected_revision is None:
            position = int(
                connection.execute(
                    sa.select(
                        sa.func.coalesce(
                            sa.func.max(article_tasks.c.position),
                            -1,
                        )
                    ).where(*self._scope())
                ).scalar_one()
            ) + 1
            statement = insert(article_tasks).values(
                organization_id=self.organization_id,
                project_id=self.project_id,
                task_id=task_id,
                customer=customer,
                topic_index=topic_index,
                revision=revision,
                position=position,
                record_updated_at=record_updated_at,
                payload=payload,
            )
            result = connection.execute(
                statement.on_conflict_do_nothing(
                    index_elements=[
                        article_tasks.c.organization_id,
                        article_tasks.c.project_id,
                        article_tasks.c.task_id,
                    ]
                )
            )
        else:
            result = connection.execute(
                article_tasks.update()
                .where(
                    *self._scope(),
                    article_tasks.c.task_id == task_id,
                    article_tasks.c.revision == int(expected_revision),
                )
                .values(
                    customer=customer,
                    topic_index=topic_index,
                    revision=revision,
                    record_updated_at=record_updated_at,
                    payload=payload,
                    updated_at=sa.func.now(),
                )
            )
        if result.rowcount:
            self._mark_initialized(connection)
            return True
        return False

    def current_revision_in_transaction(
        self,
        connection: Connection,
        task_id: str,
    ) -> int | None:
        """Read the scoped Revision inside an existing transaction."""

        if not connection.in_transaction():
            raise ValueError(
                "task revision read requires a business transaction"
            )
        normalized_task_id = _required_text(task_id, "task_id")
        value = connection.execute(
            sa.select(article_tasks.c.revision).where(
                *self._scope(),
                article_tasks.c.task_id == normalized_task_id,
            )
        ).scalar_one_or_none()
        return int(value) if value is not None else None

    def upsert_many(self, records: Iterable[Mapping[str, Any]]) -> None:
        serialized = [self._serialized(record) for record in records]
        if not serialized:
            return
        task_ids = [item[0] for item in serialized]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("task ids must be unique within one upsert")

        with self._engine.begin() as connection:
            self._lock_state(connection)
            positions = {
                str(row.task_id): int(row.position)
                for row in connection.execute(
                    sa.select(
                        article_tasks.c.task_id,
                        article_tasks.c.position,
                    ).where(*self._scope())
                )
            }
            maximum = max(positions.values(), default=-1)
            for (
                task_id,
                customer,
                topic_index,
                revision,
                record_updated_at,
                payload,
            ) in serialized:
                position = positions.get(task_id)
                if position is None:
                    maximum += 1
                    position = maximum
                    positions[task_id] = position
                statement = insert(article_tasks).values(
                    organization_id=self.organization_id,
                    project_id=self.project_id,
                    task_id=task_id,
                    customer=customer,
                    topic_index=topic_index,
                    revision=revision,
                    position=position,
                    record_updated_at=record_updated_at,
                    payload=payload,
                )
                connection.execute(
                    statement.on_conflict_do_update(
                        index_elements=[
                            article_tasks.c.organization_id,
                            article_tasks.c.project_id,
                            article_tasks.c.task_id,
                        ],
                        set_={
                            "customer": statement.excluded.customer,
                            "topic_index": statement.excluded.topic_index,
                            "revision": statement.excluded.revision,
                            "record_updated_at": (
                                statement.excluded.record_updated_at
                            ),
                            "payload": statement.excluded.payload,
                            "updated_at": sa.func.now(),
                        },
                    )
                )
            self._mark_initialized(connection)

    def delete_many(self, task_ids: Iterable[str]) -> int:
        values = tuple(
            dict.fromkeys(
                _required_text(str(task_id), "task_id")
                for task_id in task_ids
                if str(task_id).strip()
            )
        )
        if not values:
            return 0
        with self._engine.begin() as connection:
            result = connection.execute(
                article_tasks.delete().where(
                    *self._scope(),
                    article_tasks.c.task_id.in_(values),
                )
            )
        return int(result.rowcount or 0)


__all__ = ["PostgresTaskRepository"]
