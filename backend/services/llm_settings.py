from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from storage import now_iso


@dataclass(frozen=True)
class LlmRuntimeSettings:
    model: str
    reasoning_effort: str
    updated_at: str


class LlmSettingsRepository:
    """Persist the global model choice beside task records in SQLite."""

    def __init__(self, legacy_data_path: Path):
        self.database_path = legacy_data_path.with_suffix(".sqlite3")
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
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS llm_runtime_settings (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    model TEXT NOT NULL,
                    reasoning_effort TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def get(self) -> LlmRuntimeSettings | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT model, reasoning_effort, updated_at
                FROM llm_runtime_settings
                WHERE singleton = 1
                """
            ).fetchone()
        if row is None:
            return None
        return LlmRuntimeSettings(
            model=str(row["model"]),
            reasoning_effort=str(row["reasoning_effort"]),
            updated_at=str(row["updated_at"]),
        )

    def save(self, *, model: str, reasoning_effort: str) -> LlmRuntimeSettings:
        record = LlmRuntimeSettings(
            model=model.strip(),
            reasoning_effort=reasoning_effort.strip(),
            updated_at=now_iso(),
        )
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO llm_runtime_settings(
                    singleton, model, reasoning_effort, updated_at
                ) VALUES (1, ?, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    model = excluded.model,
                    reasoning_effort = excluded.reasoning_effort,
                    updated_at = excluded.updated_at
                """,
                (record.model, record.reasoning_effort, record.updated_at),
            )
        return record
