from __future__ import annotations

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Protocol


ACTIVE_JOB_STATUSES = ("queued", "running", "retry_wait")
TERMINAL_JOB_STATUSES = ("succeeded", "failed", "cancelled", "conflict")
RETRY_DELAYS_SECONDS = (5, 15, 45)


class ActiveJobError(RuntimeError):
    def __init__(self, task_id: str):
        self.task_id = task_id
        super().__init__(f"Task {task_id} already has an active job.")


class JobCancelled(RuntimeError):
    pass


class JobConflict(RuntimeError):
    pass


class JobStateTransitionError(RuntimeError):
    """A durable terminal state could not be committed safely."""


@dataclass(frozen=True, slots=True)
class BatchJobRunnerStopReport:
    dispatcher_stopped: bool
    claimed_at_stop: int
    remaining_jobs: int

    @property
    def drained(self) -> bool:
        return self.dispatcher_stopped and self.remaining_jobs == 0


class JobQueueBackend(Protocol):
    def recover_interrupted(self, operations: Iterable[str] | None = None) -> int: ...

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

    def mark_failed(self, job_id: str, error: str, *, retryable: bool) -> str: ...


def is_retryable_error(error: BaseException) -> bool:
    text = str(error).casefold()
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
    """Shared worker runner for project-scoped PostgreSQL queues."""

    def __init__(
        self,
        queue: JobQueueBackend,
        handler: Callable[[dict[str, Any], Callable[[], bool]], int],
        *,
        concurrency: int = 3,
        poll_seconds: float = 0.4,
        operations: Iterable[str] | None = None,
    ) -> None:
        self.queue = queue
        self.handler = handler
        self.concurrency = max(1, int(concurrency))
        self.poll_seconds = poll_seconds
        self.operations = tuple(dict.fromkeys(operations or ()))
        self._executor = ThreadPoolExecutor(
            max_workers=self.concurrency,
            thread_name_prefix="article-server-job",
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
            name="article-server-job-dispatcher",
            daemon=True,
        )
        self._thread.start()

    def wake(self) -> None:
        self._wake.set()

    def stop(self, *, timeout_seconds: float = 10.0) -> BatchJobRunnerStopReport:
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative")
        deadline = time.monotonic() + timeout_seconds
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=max(0.0, deadline - time.monotonic()))
        dispatcher_stopped = not (self._thread and self._thread.is_alive())
        with self._guard:
            futures = set(self._futures)
        claimed_at_stop = len(futures)
        _done, pending = wait(
            futures,
            timeout=max(0.0, deadline - time.monotonic()),
        )
        self._executor.shutdown(wait=not pending, cancel_futures=False)
        with self._guard:
            self._futures = {future for future in self._futures if not future.done()}
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
                    raise JobCancelled("Job cancelled before generation started.")
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
            except Exception as exc:
                self.queue.mark_failed(
                    job_id,
                    str(exc),
                    retryable=is_retryable_error(exc),
                )
        except JobStateTransitionError:
            try:
                self.queue.mark_interrupted(job_id)
            except Exception:
                pass
        finally:
            self.wake()


__all__ = [
    "ACTIVE_JOB_STATUSES",
    "TERMINAL_JOB_STATUSES",
    "RETRY_DELAYS_SECONDS",
    "ActiveJobError",
    "BatchJobRunner",
    "BatchJobRunnerStopReport",
    "JobCancelled",
    "JobConflict",
    "JobQueueBackend",
    "JobStateTransitionError",
    "is_retryable_error",
]
