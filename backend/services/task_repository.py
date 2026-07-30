from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Protocol


class TaskRecordRepository(Protocol):
    """Storage boundary consumed by TaskStore in local and server modes."""

    def is_initialized(self) -> bool: ...

    def load_all(self) -> list[dict[str, Any]]: ...

    def get(self, task_id: str) -> dict[str, Any] | None: ...

    def replace_all(self, records: Iterable[Mapping[str, Any]]) -> None: ...

    def upsert(self, record: Mapping[str, Any]) -> None: ...

    def upsert_many(self, records: Iterable[Mapping[str, Any]]) -> None: ...

    def delete_many(self, task_ids: Iterable[str]) -> int: ...


class SQLiteTaskRepository:
    """Transactional task persistence with one task per SQLite row.

    The public workflow model still uses ``TaskRecord`` in ``storage.py``. This
    repository deliberately stores plain mappings so schema migration remains
    the responsibility of that layer.
    """

    def __init__(self, legacy_path: Path):
        self.legacy_path = legacy_path
        self.database_path = legacy_path.with_suffix(".sqlite3")
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize_schema(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS task_store_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS task_records (
                    id TEXT PRIMARY KEY,
                    customer TEXT NOT NULL DEFAULT '',
                    topic_index INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT '',
                    position INTEGER NOT NULL,
                    payload TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_task_records_customer
                    ON task_records(customer, topic_index, position);
                CREATE INDEX IF NOT EXISTS idx_task_records_updated
                    ON task_records(updated_at);
                """
            )

    def is_initialized(self) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT value FROM task_store_meta WHERE key = 'initialized'"
            ).fetchone()
        return bool(row and row["value"] == "1")

    @staticmethod
    def _serialized(record: Mapping[str, Any]) -> tuple[str, str, int, str]:
        task_id = str(record.get("id") or "").strip()
        if not task_id:
            raise ValueError("Every persisted task requires a non-empty id.")
        customer = str(record.get("customer") or "")
        try:
            topic_index = int(record.get("topic_index") or 0)
        except (TypeError, ValueError):
            topic_index = 0
        updated_at = str(record.get("updated_at") or "")
        payload = json.dumps(dict(record), ensure_ascii=False, separators=(",", ":"))
        return task_id, customer, topic_index, updated_at, payload

    def load_all(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT payload FROM task_records ORDER BY position, id"
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT payload FROM task_records WHERE id = ?", (task_id,)
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def replace_all(self, records: Iterable[Mapping[str, Any]]) -> None:
        serialized = [self._serialized(record) for record in records]
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM task_records")
            connection.executemany(
                """
                INSERT INTO task_records(
                    id, customer, topic_index, updated_at, position, payload
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (task_id, customer, topic_index, updated_at, position, payload)
                    for position, (
                        task_id,
                        customer,
                        topic_index,
                        updated_at,
                        payload,
                    ) in enumerate(serialized)
                ],
            )
            connection.execute(
                """
                INSERT INTO task_store_meta(key, value) VALUES ('initialized', '1')
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """
            )

    def upsert(self, record: Mapping[str, Any]) -> None:
        task_id, customer, topic_index, updated_at, payload = self._serialized(record)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT position FROM task_records WHERE id = ?", (task_id,)
            ).fetchone()
            if row:
                position = int(row["position"])
            else:
                maximum = connection.execute(
                    "SELECT COALESCE(MAX(position), -1) AS value FROM task_records"
                ).fetchone()
                position = int(maximum["value"]) + 1
            connection.execute(
                """
                INSERT INTO task_records(
                    id, customer, topic_index, updated_at, position, payload
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    customer = excluded.customer,
                    topic_index = excluded.topic_index,
                    updated_at = excluded.updated_at,
                    payload = excluded.payload
                """,
                (task_id, customer, topic_index, updated_at, position, payload),
            )
            connection.execute(
                """
                INSERT INTO task_store_meta(key, value) VALUES ('initialized', '1')
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """
            )

    def upsert_many(self, records: Iterable[Mapping[str, Any]]) -> None:
        serialized = [self._serialized(record) for record in records]
        if not serialized:
            return
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            positions = {
                str(row["id"]): int(row["position"])
                for row in connection.execute(
                    "SELECT id, position FROM task_records"
                ).fetchall()
            }
            maximum = max(positions.values(), default=-1)
            for task_id, customer, topic_index, updated_at, payload in serialized:
                position = positions.get(task_id)
                if position is None:
                    maximum += 1
                    position = maximum
                    positions[task_id] = position
                connection.execute(
                    """
                    INSERT INTO task_records(
                        id, customer, topic_index, updated_at, position, payload
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        customer = excluded.customer,
                        topic_index = excluded.topic_index,
                        updated_at = excluded.updated_at,
                        payload = excluded.payload
                    """,
                    (task_id, customer, topic_index, updated_at, position, payload),
                )
            connection.execute(
                """
                INSERT INTO task_store_meta(key, value) VALUES ('initialized', '1')
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """
            )

    def delete_many(self, task_ids: Iterable[str]) -> int:
        values = list(dict.fromkeys(str(task_id) for task_id in task_ids if task_id))
        if not values:
            return 0
        placeholders = ",".join("?" for _ in values)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                f"DELETE FROM task_records WHERE id IN ({placeholders})",
                values,
            )
        return int(cursor.rowcount)
