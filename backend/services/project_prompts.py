from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from models import (
    PromptDefaults,
    PromptLibraryItem,
    PromptSnapshot,
    ProjectPromptLibrary,
)
from services.task_identity import normalized_customer
from storage import now_iso


class PromptLibraryError(RuntimeError):
    pass


class PromptInUseError(PromptLibraryError):
    pass


class ProjectPromptRepository:
    """Project-scoped reusable prompts stored beside task records in SQLite."""

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
            prompt_table = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'project_prompts'"
            ).fetchone()
            table_sql = str(prompt_table["sql"] or "") if prompt_table else ""
            if prompt_table and "'review'" not in table_sql:
                connection.execute("DROP INDEX IF EXISTS idx_project_prompts_customer")
                connection.execute(
                    "ALTER TABLE project_prompts RENAME TO project_prompts_before_review"
                )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS project_prompts (
                    id TEXT PRIMARY KEY,
                    customer_key TEXT NOT NULL,
                    customer TEXT NOT NULL,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('outline', 'article', 'review')),
                    content TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    use_count INTEGER NOT NULL DEFAULT 0,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_project_prompts_customer
                    ON project_prompts(customer_key, kind, active, updated_at);

                CREATE TABLE IF NOT EXISTS project_prompt_defaults (
                    customer_key TEXT PRIMARY KEY,
                    customer TEXT NOT NULL,
                    default_outline_prompt_id TEXT NOT NULL DEFAULT '',
                    default_article_prompt_id TEXT NOT NULL DEFAULT '',
                    default_review_prompt_id TEXT NOT NULL DEFAULT ''
                );
                """
            )
            if prompt_table and "'review'" not in table_sql:
                connection.execute(
                    """INSERT INTO project_prompts(
                           id, customer_key, customer, name, kind, content,
                           version, use_count, active, created_at, updated_at
                       )
                       SELECT id, customer_key, customer, name, kind, content,
                              version, use_count, active, created_at, updated_at
                       FROM project_prompts_before_review"""
                )
                connection.execute("DROP TABLE project_prompts_before_review")

            default_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(project_prompt_defaults)"
                ).fetchall()
            }
            if "default_review_prompt_id" not in default_columns:
                connection.execute(
                    "ALTER TABLE project_prompt_defaults "
                    "ADD COLUMN default_review_prompt_id TEXT NOT NULL DEFAULT ''"
                )

    @staticmethod
    def _item(row: sqlite3.Row) -> PromptLibraryItem:
        return PromptLibraryItem(
            id=row["id"],
            customer=row["customer"],
            name=row["name"],
            kind=row["kind"],
            content=row["content"],
            version=int(row["version"]),
            use_count=int(row["use_count"]),
            active=bool(row["active"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def list(self, customer: str) -> ProjectPromptLibrary:
        key = normalized_customer(customer)
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT * FROM project_prompts WHERE customer_key = ?
                   ORDER BY active DESC, kind, name COLLATE NOCASE, updated_at DESC""",
                (key,),
            ).fetchall()
            default_row = connection.execute(
                "SELECT * FROM project_prompt_defaults WHERE customer_key = ?",
                (key,),
            ).fetchone()
        defaults = PromptDefaults(
            customer=customer,
            default_outline_prompt_id=(default_row["default_outline_prompt_id"] if default_row else ""),
            default_article_prompt_id=(default_row["default_article_prompt_id"] if default_row else ""),
            default_review_prompt_id=(default_row["default_review_prompt_id"] if default_row else ""),
        )
        return ProjectPromptLibrary(
            prompts=[self._item(row) for row in rows],
            defaults=defaults,
        )

    def get(self, customer: str, prompt_id: str, *, active_only: bool = False) -> PromptLibraryItem:
        key = normalized_customer(customer)
        query = "SELECT * FROM project_prompts WHERE customer_key = ? AND id = ?"
        parameters: list[object] = [key, prompt_id]
        if active_only:
            query += " AND active = 1"
        with self._connection() as connection:
            row = connection.execute(query, parameters).fetchone()
        if not row:
            raise KeyError(prompt_id)
        return self._item(row)

    def create(self, customer: str, name: str, kind: str, content: str) -> PromptLibraryItem:
        cleaned_name = " ".join(name.split())
        cleaned_content = content.replace("\r\n", "\n").strip()
        if not cleaned_name or not cleaned_content:
            raise PromptLibraryError("提示词名称和内容不能为空。")
        prompt_id = uuid4().hex
        timestamp = now_iso()
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO project_prompts(
                    id, customer_key, customer, name, kind, content, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    prompt_id,
                    normalized_customer(customer),
                    customer,
                    cleaned_name,
                    kind,
                    cleaned_content,
                    timestamp,
                    timestamp,
                ),
            )
        return self.get(customer, prompt_id)

    def update(self, customer: str, prompt_id: str, name: str, content: str) -> PromptLibraryItem:
        self.get(customer, prompt_id)
        cleaned_name = " ".join(name.split())
        cleaned_content = content.replace("\r\n", "\n").strip()
        if not cleaned_name or not cleaned_content:
            raise PromptLibraryError("提示词名称和内容不能为空。")
        with self._connection() as connection:
            connection.execute(
                """UPDATE project_prompts
                   SET name = ?, content = ?, version = version + 1, updated_at = ?
                   WHERE customer_key = ? AND id = ?""",
                (cleaned_name, cleaned_content, now_iso(), normalized_customer(customer), prompt_id),
            )
        return self.get(customer, prompt_id)

    def set_active(self, customer: str, prompt_id: str, active: bool) -> PromptLibraryItem:
        item = self.get(customer, prompt_id)
        with self._connection() as connection:
            connection.execute(
                "UPDATE project_prompts SET active = ?, updated_at = ? WHERE customer_key = ? AND id = ?",
                (int(active), now_iso(), normalized_customer(customer), prompt_id),
            )
            if not active:
                field = {
                    "outline": "default_outline_prompt_id",
                    "article": "default_article_prompt_id",
                    "review": "default_review_prompt_id",
                }[item.kind]
                connection.execute(
                    f"UPDATE project_prompt_defaults SET {field} = '' WHERE customer_key = ? AND {field} = ?",
                    (normalized_customer(customer), prompt_id),
                )
        return self.get(customer, prompt_id)

    def delete(self, customer: str, prompt_id: str) -> None:
        item = self.get(customer, prompt_id)
        if item.use_count:
            raise PromptInUseError("该提示词已经用于生成，不能彻底删除；请改为停用。")
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM project_prompts WHERE customer_key = ? AND id = ?",
                (normalized_customer(customer), prompt_id),
            )
            connection.execute(
                """UPDATE project_prompt_defaults
                   SET default_outline_prompt_id = CASE WHEN default_outline_prompt_id = ? THEN '' ELSE default_outline_prompt_id END,
                       default_article_prompt_id = CASE WHEN default_article_prompt_id = ? THEN '' ELSE default_article_prompt_id END,
                       default_review_prompt_id = CASE WHEN default_review_prompt_id = ? THEN '' ELSE default_review_prompt_id END
                   WHERE customer_key = ?""",
                (prompt_id, prompt_id, prompt_id, normalized_customer(customer)),
            )

    def set_defaults(
        self,
        customer: str,
        outline_id: str,
        article_id: str,
        review_id: str = "",
    ) -> PromptDefaults:
        for prompt_id, kind in (
            (outline_id, "outline"),
            (article_id, "article"),
            (review_id, "review"),
        ):
            if not prompt_id:
                continue
            item = self.get(customer, prompt_id, active_only=True)
            if item.kind != kind:
                label = {"outline": "大纲", "article": "正文", "review": "复检"}[kind]
                raise PromptLibraryError(f"{item.name} 不是{label}提示词。")
        key = normalized_customer(customer)
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO project_prompt_defaults(
                    customer_key, customer, default_outline_prompt_id,
                    default_article_prompt_id, default_review_prompt_id
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(customer_key) DO UPDATE SET
                    customer = excluded.customer,
                    default_outline_prompt_id = excluded.default_outline_prompt_id,
                    default_article_prompt_id = excluded.default_article_prompt_id,
                    default_review_prompt_id = excluded.default_review_prompt_id""",
                (key, customer, outline_id, article_id, review_id),
            )
        return self.list(customer).defaults

    def resolve(self, customer: str, kind: str, selection: str) -> PromptSnapshot:
        source = "library"
        prompt_id = selection.strip()
        if selection == "system":
            return PromptSnapshot(kind=kind, source="system", captured_at=now_iso())
        if not prompt_id or selection == "project_default":
            defaults = self.list(customer).defaults
            prompt_id = {
                "outline": defaults.default_outline_prompt_id,
                "article": defaults.default_article_prompt_id,
                "review": defaults.default_review_prompt_id,
            }[kind]
            source = "project_default"
        if not prompt_id:
            return PromptSnapshot(kind=kind, source="system", captured_at=now_iso())
        try:
            item = self.get(customer, prompt_id, active_only=True)
        except KeyError:
            return PromptSnapshot(kind=kind, source="system", captured_at=now_iso())
        if item.kind != kind:
            raise PromptLibraryError("所选提示词类型与当前生成环节不一致。")
        return PromptSnapshot(
            prompt_id=item.id,
            name=item.name,
            kind=item.kind,
            content=item.content,
            version=item.version,
            source=source,
            captured_at=now_iso(),
        )

    def mark_used(self, customer: str, prompt_id: str) -> None:
        if not prompt_id:
            return
        with self._connection() as connection:
            connection.execute(
                "UPDATE project_prompts SET use_count = use_count + 1 WHERE customer_key = ? AND id = ?",
                (normalized_customer(customer), prompt_id),
            )

    def delete_customer(self, customer: str) -> None:
        key = normalized_customer(customer)
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM project_prompt_defaults WHERE customer_key = ?",
                (key,),
            )
            connection.execute(
                "DELETE FROM project_prompts WHERE customer_key = ?",
                (key,),
            )
