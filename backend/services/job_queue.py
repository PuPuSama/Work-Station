from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Protocol
from uuid import uuid4


ACTIVE_JOB_STATUSES = ("queued", "running", "retry_wait")
TERMINAL_JOB_STATUSES = ("succeeded", "failed", "cancelled", "conflict")
RETRY_DELAYS_SECONDS = (5, 15, 45)


def _now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


class ActiveJobError(RuntimeError):
    def __init__(self, task_id: str):
        self.task_id = task_id
        super().__init__(f"Task {task_id} already has an active batch job.")


class JobCancelled(RuntimeError):
    pass


class JobConflict(RuntimeError):
    pass


class JobStateTransitionError(RuntimeError):
    """A durable terminal state could not be committed safely."""


@dataclass(frozen=True, slots=True)
class BatchJobRunnerStopReport:
    """Bounded evidence that a runner stopped claiming and joined its work."""

    dispatcher_stopped: bool
    claimed_at_stop: int
    remaining_jobs: int

    @property
    def drained(self) -> bool:
        return self.dispatcher_stopped and self.remaining_jobs == 0


class JobQueueBackend(Protocol):
    def recover_interrupted(
        self,
        operations: Iterable[str] | None = None,
    ) -> int: ...

    def claim_jobs(
        self,
        limit: int,
        operations: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]: ...

    def is_cancel_requested(self, job_id: str) -> bool: ...

    def mark_succeeded(self, job_id: str, result_revision: int) -> None: ...

    def mark_cancelled(self, job_id: str) -> None: ...

    def mark_interrupted(self, job_id: str) -> None: ...

    def mark_conflict(self, job_id: str, error: str) -> None: ...

    def mark_failed(
        self,
        job_id: str,
        error: str,
        *,
        retryable: bool,
    ) -> str: ...


class JobQueue:
    """Small persistent SQLite queue designed for the portable desktop app."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self.path,
            timeout=30,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS batches (
                    id TEXT PRIMARY KEY,
                    operation TEXT NOT NULL,
                    customer TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    batch_id TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
                    task_id TEXT NOT NULL,
                    customer TEXT NOT NULL DEFAULT '',
                    topic_index INTEGER NOT NULL DEFAULT 0,
                    topic TEXT NOT NULL DEFAULT '',
                    operation TEXT NOT NULL,
                    status TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    source_revision INTEGER NOT NULL,
                    result_revision INTEGER,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 4,
                    available_at REAL NOT NULL,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    started_at TEXT NOT NULL DEFAULT '',
                    finished_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_jobs_batch ON jobs(batch_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_jobs_runnable
                    ON jobs(status, available_at, created_at);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_one_active_per_task
                    ON jobs(task_id)
                    WHERE status IN ('queued', 'running', 'retry_wait');
                """
            )

    def recover_interrupted(self, operations: Iterable[str] | None = None) -> int:
        now = _now_iso()
        selected_operations = tuple(dict.fromkeys(operations or ()))
        operation_sql = ""
        parameters: list[Any] = [time.time(), now, now]
        if selected_operations:
            placeholders = ",".join("?" for _ in selected_operations)
            operation_sql = f" AND operation IN ({placeholders})"
            parameters.extend(selected_operations)
        with self._connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE jobs
                SET status = CASE WHEN cancel_requested = 1 THEN 'cancelled' ELSE 'queued' END,
                    available_at = ?,
                    finished_at = CASE WHEN cancel_requested = 1 THEN ? ELSE '' END,
                    updated_at = ?
                WHERE status = 'running'
                {operation_sql}
                """,
                parameters,
            )
            return cursor.rowcount

    def active_task_ids(self, task_ids: Iterable[str]) -> set[str]:
        values = list(dict.fromkeys(str(task_id) for task_id in task_ids))
        if not values:
            return set()
        placeholders = ",".join("?" for _ in values)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT DISTINCT task_id
                FROM jobs
                WHERE task_id IN ({placeholders})
                  AND status IN ('queued', 'running', 'retry_wait')
                """,
                values,
            ).fetchall()
        return {str(row["task_id"]) for row in rows}

    def delete_customer(self, customer: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                """SELECT 1 FROM jobs
                   WHERE customer = ? AND status IN ('queued', 'running', 'retry_wait')
                   LIMIT 1""",
                (customer,),
            ).fetchone()
            if active:
                raise ActiveJobError(f"project:{customer}")
            batch_ids = [
                str(row["id"])
                for row in connection.execute(
                    "SELECT id FROM batches WHERE customer = ?",
                    (customer,),
                ).fetchall()
            ]
            if batch_ids:
                placeholders = ",".join("?" for _ in batch_ids)
                connection.execute(
                    f"DELETE FROM jobs WHERE batch_id IN ({placeholders})",
                    batch_ids,
                )
                connection.execute(
                    f"DELETE FROM batches WHERE id IN ({placeholders})",
                    batch_ids,
                )

    def rename_customer(
        self,
        customer: str,
        new_customer: str,
        task_id_mapping: dict[str, str],
    ) -> None:
        if not task_id_mapping:
            return
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            old_ids = list(task_id_mapping)
            placeholders = ",".join("?" for _ in old_ids)
            batch_ids = [
                str(row["batch_id"])
                for row in connection.execute(
                    f"SELECT DISTINCT batch_id FROM jobs WHERE task_id IN ({placeholders})",
                    old_ids,
                ).fetchall()
            ]
            for old_id, new_id in task_id_mapping.items():
                connection.execute(
                    """UPDATE jobs
                       SET task_id = ?, customer = ?, updated_at = ?
                       WHERE task_id = ?""",
                    (new_id, new_customer, _now_iso(), old_id),
                )
            if batch_ids:
                batch_placeholders = ",".join("?" for _ in batch_ids)
                connection.execute(
                    f"""UPDATE batches
                        SET customer = ?, updated_at = ?
                        WHERE id IN ({batch_placeholders})""",
                    (new_customer, _now_iso(), *batch_ids),
                )
            connection.execute(
                """UPDATE batches
                   SET customer = ?, updated_at = ?
                   WHERE customer = ?""",
                (new_customer, _now_iso(), customer),
            )

    def create_batch(
        self,
        operation: str,
        items: list[dict[str, Any]],
        *,
        customer: str = "",
    ) -> dict[str, Any]:
        if not items:
            raise ValueError("A batch requires at least one job.")
        batch_id = uuid4().hex
        now = _now_iso()
        available_at = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for item in items:
                    active = connection.execute(
                        """
                        SELECT 1 FROM jobs
                        WHERE task_id = ?
                          AND status IN ('queued', 'running', 'retry_wait')
                        LIMIT 1
                        """,
                        (item["task_id"],),
                    ).fetchone()
                    if active:
                        raise ActiveJobError(str(item["task_id"]))

                connection.execute(
                    """
                    INSERT INTO batches(id, operation, customer, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (batch_id, operation, customer, now, now),
                )
                for item in items:
                    connection.execute(
                        """
                        INSERT INTO jobs(
                            id, batch_id, task_id, customer, topic_index, topic,
                            operation, status, request_json, source_revision,
                            available_at, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?)
                        """,
                        (
                            uuid4().hex,
                            batch_id,
                            item["task_id"],
                            item.get("customer", ""),
                            int(item.get("topic_index", 0)),
                            item.get("topic", ""),
                            operation,
                            json.dumps(item.get("request", {}), ensure_ascii=False),
                            int(item["source_revision"]),
                            available_at,
                            now,
                            now,
                        ),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self.get_batch(batch_id)

    def claim_jobs(
        self,
        limit: int,
        operations: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        selected_operations = tuple(dict.fromkeys(operations or ()))
        operation_sql = ""
        parameters: list[Any] = [time.time()]
        if selected_operations:
            placeholders = ",".join("?" for _ in selected_operations)
            operation_sql = f" AND operation IN ({placeholders})"
            parameters.extend(selected_operations)
        parameters.append(limit)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                f"""
                SELECT * FROM jobs
                WHERE status IN ('queued', 'retry_wait')
                  AND available_at <= ?
                  AND cancel_requested = 0
                  {operation_sql}
                ORDER BY available_at, created_at
                LIMIT ?
                """,
                parameters,
            ).fetchall()
            now = _now_iso()
            claimed: list[dict[str, Any]] = []
            for row in rows:
                connection.execute(
                    """
                    UPDATE jobs
                    SET status = 'running', attempts = attempts + 1,
                        started_at = ?, updated_at = ?
                    WHERE id = ? AND status IN ('queued', 'retry_wait')
                    """,
                    (now, now, row["id"]),
                )
                current = connection.execute(
                    "SELECT * FROM jobs WHERE id = ?",
                    (row["id"],),
                ).fetchone()
                if current is not None:
                    claimed.append(self._job_dict(current))
            connection.commit()
        return claimed

    def is_cancel_requested(self, job_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT cancel_requested, status FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        return bool(row and (row["cancel_requested"] or row["status"] == "cancelled"))

    def mark_succeeded(self, job_id: str, result_revision: int) -> None:
        now = _now_iso()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = 'succeeded', result_revision = ?, error = '',
                    cancel_requested = 0, finished_at = ?, updated_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (result_revision, now, now, job_id),
            )
            self._touch_batch(connection, job_id, now)

    def mark_cancelled(self, job_id: str) -> None:
        now = _now_iso()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = 'cancelled', cancel_requested = 1,
                    finished_at = ?, updated_at = ?
                WHERE id = ? AND status IN ('queued', 'retry_wait', 'running')
                """,
                (now, now, job_id),
            )
            self._touch_batch(connection, job_id, now)

    def mark_interrupted(self, job_id: str) -> None:
        """Release a controlled-shutdown claim without user cancellation."""

        now = _now_iso()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = 'queued', available_at = ?, error = '',
                    started_at = '', finished_at = '', updated_at = ?
                WHERE id = ? AND status = 'running'
                  AND cancel_requested = 0
                """,
                (time.time(), now, job_id),
            )
            self._touch_batch(connection, job_id, now)

    def mark_conflict(self, job_id: str, error: str) -> None:
        self._mark_terminal(job_id, "conflict", error)

    def mark_failed(self, job_id: str, error: str, *, retryable: bool) -> str:
        error = str(error or "Unknown batch job error")[:4000]
        now = _now_iso()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT attempts, max_attempts, cancel_requested FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                return "failed"
            if row["cancel_requested"]:
                status = "cancelled"
                connection.execute(
                    """
                    UPDATE jobs SET status = ?, error = ?, finished_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (status, error, now, now, job_id),
                )
            elif retryable and int(row["attempts"]) < int(row["max_attempts"]):
                retry_number = max(1, int(row["attempts"]))
                delay_index = min(retry_number - 1, len(RETRY_DELAYS_SECONDS) - 1)
                status = "retry_wait"
                connection.execute(
                    """
                    UPDATE jobs
                    SET status = ?, error = ?, available_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        status,
                        error,
                        time.time() + RETRY_DELAYS_SECONDS[delay_index],
                        now,
                        job_id,
                    ),
                )
            else:
                status = "failed"
                connection.execute(
                    """
                    UPDATE jobs SET status = ?, error = ?, finished_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (status, error, now, now, job_id),
                )
            self._touch_batch(connection, job_id, now)
        return status

    def _mark_terminal(self, job_id: str, status: str, error: str = "") -> None:
        now = _now_iso()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = ?, error = ?, finished_at = ?, updated_at = ?
                WHERE id = ? AND status IN ('queued', 'retry_wait', 'running')
                """,
                (status, str(error or "")[:4000], now, now, job_id),
            )
            self._touch_batch(connection, job_id, now)

    def request_cancel(self, job_id: str) -> dict[str, Any]:
        now = _now_iso()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            if row["status"] in ("queued", "retry_wait"):
                connection.execute(
                    """
                    UPDATE jobs
                    SET status = 'cancelled', cancel_requested = 1,
                        finished_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, now, job_id),
                )
            elif row["status"] == "running":
                connection.execute(
                    "UPDATE jobs SET cancel_requested = 1, updated_at = ? WHERE id = ?",
                    (now, job_id),
                )
            self._touch_batch(connection, job_id, now)
        return self.get_job(job_id)

    def cancel_batch(self, batch_id: str) -> dict[str, Any]:
        now = _now_iso()
        with self._connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM batches WHERE id = ?",
                (batch_id,),
            ).fetchone()
            if exists is None:
                raise KeyError(batch_id)
            connection.execute(
                """
                UPDATE jobs
                SET status = CASE
                        WHEN status IN ('queued', 'retry_wait') THEN 'cancelled'
                        ELSE status
                    END,
                    cancel_requested = CASE
                        WHEN status IN ('queued', 'retry_wait', 'running') THEN 1
                        ELSE cancel_requested
                    END,
                    finished_at = CASE
                        WHEN status IN ('queued', 'retry_wait') THEN ?
                        ELSE finished_at
                    END,
                    updated_at = ?
                WHERE batch_id = ?
                  AND status IN ('queued', 'retry_wait', 'running')
                """,
                (now, now, batch_id),
            )
            connection.execute(
                "UPDATE batches SET updated_at = ? WHERE id = ?",
                (now, batch_id),
            )
        return self.get_batch(batch_id)

    def retry_job(
        self,
        job_id: str,
        *,
        source_revision: int | None = None,
        request: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = _now_iso()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT task_id, status FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise KeyError(job_id)
            if row["status"] not in ("failed", "cancelled", "conflict"):
                connection.rollback()
                raise ValueError("Only failed, cancelled, or conflicted jobs can be retried.")
            active = connection.execute(
                """
                SELECT 1 FROM jobs
                WHERE task_id = ? AND id <> ?
                  AND status IN ('queued', 'running', 'retry_wait')
                LIMIT 1
                """,
                (row["task_id"], job_id),
            ).fetchone()
            if active:
                connection.rollback()
                raise ActiveJobError(str(row["task_id"]))
            snapshot_revision = source_revision
            snapshot_request = (
                json.dumps(request, ensure_ascii=False) if request is not None else None
            )
            connection.execute(
                """
                UPDATE jobs
                SET status = 'queued', attempts = 0, available_at = ?,
                    cancel_requested = 0, error = '', started_at = '',
                    finished_at = '', updated_at = ?,
                    source_revision = COALESCE(?, source_revision),
                    request_json = COALESCE(?, request_json)
                WHERE id = ?
                """,
                (time.time(), now, snapshot_revision, snapshot_request, job_id),
            )
            self._touch_batch(connection, job_id, now)
            connection.commit()
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._job_dict(row)

    def get_batch(self, batch_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            batch = connection.execute(
                "SELECT * FROM batches WHERE id = ?",
                (batch_id,),
            ).fetchone()
            if batch is None:
                raise KeyError(batch_id)
            jobs = connection.execute(
                "SELECT * FROM jobs WHERE batch_id = ? ORDER BY created_at, topic_index",
                (batch_id,),
            ).fetchall()
        return self._batch_dict(batch, jobs)

    def list_batches(self, *, customer: str = "", limit: int = 10) -> list[dict[str, Any]]:
        parameters: list[Any] = []
        where = ""
        if customer:
            where = "WHERE customer = ?"
            parameters.append(customer)
        parameters.append(max(1, min(int(limit), 100)))
        with self._connect() as connection:
            batches = connection.execute(
                f"SELECT * FROM batches {where} ORDER BY created_at DESC LIMIT ?",
                parameters,
            ).fetchall()
            result = []
            for batch in batches:
                jobs = connection.execute(
                    "SELECT * FROM jobs WHERE batch_id = ? ORDER BY created_at, topic_index",
                    (batch["id"],),
                ).fetchall()
                result.append(self._batch_dict(batch, jobs))
        return result

    def export_batches(self) -> list[dict[str, Any]]:
        """Return every batch for a controlled one-time server migration."""

        with self._connect() as connection:
            batches = connection.execute(
                "SELECT * FROM batches ORDER BY created_at, id"
            ).fetchall()
            result = []
            for batch in batches:
                jobs = connection.execute(
                    "SELECT * FROM jobs WHERE batch_id = ? "
                    "ORDER BY created_at, topic_index, id",
                    (batch["id"],),
                ).fetchall()
                result.append(self._batch_dict(batch, jobs))
        return result

    @staticmethod
    def _touch_batch(connection: sqlite3.Connection, job_id: str, now: str) -> None:
        connection.execute(
            """
            UPDATE batches SET updated_at = ?
            WHERE id = (SELECT batch_id FROM jobs WHERE id = ?)
            """,
            (now, job_id),
        )

    @staticmethod
    def _job_dict(row: sqlite3.Row) -> dict[str, Any]:
        payload = dict(row)
        payload["request"] = json.loads(payload.pop("request_json") or "{}")
        payload["cancel_requested"] = bool(payload["cancel_requested"])
        return payload

    def _batch_dict(
        self,
        row: sqlite3.Row,
        job_rows: Iterable[sqlite3.Row],
    ) -> dict[str, Any]:
        jobs = [self._job_dict(job) for job in job_rows]
        counts: dict[str, int] = {}
        for job in jobs:
            counts[job["status"]] = counts.get(job["status"], 0) + 1
        active_count = sum(counts.get(status, 0) for status in ACTIVE_JOB_STATUSES)
        succeeded = counts.get("succeeded", 0)
        if active_count:
            status = "running" if counts.get("running", 0) else "queued"
        elif jobs and succeeded == len(jobs):
            status = "succeeded"
        elif jobs and counts.get("cancelled", 0) == len(jobs):
            status = "cancelled"
        else:
            status = "completed_with_errors"
        return {
            **dict(row),
            "status": status,
            "total": len(jobs),
            "completed": len(jobs) - active_count,
            "status_counts": counts,
            "jobs": jobs,
        }


def is_retryable_error(error: BaseException) -> bool:
    status_code = getattr(error, "status_code", None)
    if status_code in (408, 429, 500, 502, 503, 504):
        return True
    text = str(error).lower()
    retry_signals = (
        "429",
        "502",
        "503",
        "504",
        "rate limit",
        "too many requests",
        "temporarily unavailable",
        "connection reset",
        "connection aborted",
        "connection error",
        "failed to fetch",
        "timed out",
        "timeout",
    )
    return any(signal in text for signal in retry_signals)


class BatchJobRunner:
    def __init__(
        self,
        queue: JobQueueBackend,
        handler: Callable[[dict[str, Any], Callable[[], bool]], int],
        *,
        concurrency: int = 3,
        poll_seconds: float = 0.4,
        operations: Iterable[str] | None = None,
    ):
        self.queue = queue
        self.handler = handler
        self.concurrency = max(1, int(concurrency))
        self.poll_seconds = poll_seconds
        self.operations = tuple(dict.fromkeys(operations or ()))
        self._executor = ThreadPoolExecutor(
            max_workers=self.concurrency,
            thread_name_prefix="article-batch",
        )
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._guard = threading.Lock()
        self._futures: set[Future[Any]] = set()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.queue.recover_interrupted(self.operations)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._dispatch_loop,
            name="article-batch-dispatcher",
            daemon=True,
        )
        self._thread.start()

    def wake(self) -> None:
        self._wake.set()

    def stop(
        self,
        *,
        timeout_seconds: float = 10.0,
    ) -> BatchJobRunnerStopReport:
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative")
        deadline = time.monotonic() + timeout_seconds
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(
                timeout=max(0.0, deadline - time.monotonic())
            )
        dispatcher_stopped = not (
            self._thread and self._thread.is_alive()
        )
        with self._guard:
            futures = set(self._futures)
        claimed_at_stop = len(futures)
        _done, pending = wait(
            futures,
            timeout=max(0.0, deadline - time.monotonic()),
        )
        self._executor.shutdown(
            wait=not pending,
            cancel_futures=False,
        )
        with self._guard:
            self._futures = {
                future for future in self._futures if not future.done()
            }
            remaining_jobs = len(self._futures)
        return BatchJobRunnerStopReport(
            dispatcher_stopped=dispatcher_stopped,
            claimed_at_stop=claimed_at_stop,
            remaining_jobs=remaining_jobs,
        )

    def _dispatch_loop(self) -> None:
        while not self._stop.is_set():
            with self._guard:
                self._futures = {future for future in self._futures if not future.done()}
                available = self.concurrency - len(self._futures)
            for job in self.queue.claim_jobs(available, self.operations):
                future = self._executor.submit(self._run_job, job)
                with self._guard:
                    self._futures.add(future)
            self._wake.wait(self.poll_seconds)
            self._wake.clear()

    def _run_job(self, job: dict[str, Any]) -> None:
        job_id = str(job["id"])
        user_cancelled = lambda: self.queue.is_cancel_requested(job_id)
        should_stop = lambda: self._stop.is_set() or user_cancelled()
        try:
            try:
                if self._stop.is_set():
                    self.queue.mark_interrupted(job_id)
                    return
                if user_cancelled():
                    raise JobCancelled(
                        "Job cancelled before generation started."
                    )
                result_revision = self.handler(job, should_stop)
                self.queue.mark_succeeded(job_id, result_revision)
            except JobCancelled:
                if self._stop.is_set() and not user_cancelled():
                    self.queue.mark_interrupted(job_id)
                else:
                    self.queue.mark_cancelled(job_id)
            except JobConflict as exc:
                self.queue.mark_conflict(job_id, str(exc))
            except JobStateTransitionError:
                raise
            except Exception as exc:  # keep dispatcher alive after one bad task
                self.queue.mark_failed(
                    job_id,
                    str(exc),
                    retryable=is_retryable_error(exc),
                )
        except JobStateTransitionError:
            # Terminal state and its mandatory Audit failed together. Release
            # the claim without persisting provider details; a later worker
            # may retry after the audit dependency recovers.
            try:
                self.queue.mark_interrupted(job_id)
            except Exception:
                pass
        finally:
            self.wake()
