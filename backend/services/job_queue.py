from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Protocol


LOGGER = logging.getLogger(__name__)


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

    def renew_lease(self, job_id: str) -> bool: ...

    def mark_succeeded(self, job_id: str, result_revision: int) -> None: ...

    def mark_cancelled(self, job_id: str) -> None: ...

    def mark_interrupted(self, job_id: str) -> None: ...

    def mark_conflict(self, job_id: str, error: str) -> None: ...

    def mark_failed(self, job_id: str, error: str, *, retryable: bool) -> str: ...


def is_retryable_error(error: BaseException) -> bool:
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
        "invalid result",
        "timed out",
        "timeout",
    )
    retryable_exception_names = frozenset(
        {
            "BrokenPipeError",
            "ConnectionAbortedError",
            "ConnectionRefusedError",
            "ConnectionResetError",
            "IncompleteRead",
            "RemoteDisconnected",
            "TimeoutError",
        }
    )
    current: BaseException | None = error
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        if isinstance(current, (TimeoutError, ConnectionError)):
            return True
        if type(current).__name__ in retryable_exception_names:
            return True
        text = str(current).casefold()
        if any(signal in text for signal in retry_signals):
            return True
        current = current.__cause__ or current.__context__
    return False


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
        lease_renewal_seconds: float | None = None,
    ) -> None:
        self.queue = queue
        self.handler = handler
        self.concurrency = max(1, int(concurrency))
        self.poll_seconds = poll_seconds
        self.operations = tuple(dict.fromkeys(operations or ()))
        if lease_renewal_seconds is not None:
            if lease_renewal_seconds <= 0:
                raise ValueError("lease_renewal_seconds must be greater than zero")
            self._lease_renewal_seconds = float(lease_renewal_seconds)
        else:
            try:
                lease_seconds = float(getattr(queue, "lease_seconds", 15 * 60))
            except (TypeError, ValueError):
                lease_seconds = float(15 * 60)
            # PostgreSQL queues use a fifteen-minute lease by default. Keep
            # the heartbeat comfortably below that boundary without making
            # short-lived test or custom queues poll excessively.
            self._lease_renewal_seconds = max(
                0.5,
                min(60.0, lease_seconds / 3.0),
            )
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
        retry_delay = 0.5
        while not self._stop.is_set():
            try:
                with self._guard:
                    self._futures = {
                        future
                        for future in self._futures
                        if not future.done()
                    }
                    available = self.concurrency - len(self._futures)
                for job in self.queue.claim_jobs(available, self.operations):
                    future = self._executor.submit(self._run_job, job)
                    with self._guard:
                        self._futures.add(future)
            except Exception as exc:
                # A transient PostgreSQL/connection-pool failure must not
                # terminate the only dispatcher thread. Jobs already claimed
                # by the database remain durable and will be recovered by the
                # normal queue/restart boundary.
                if self._stop.is_set():
                    break
                LOGGER.warning(
                    "job dispatcher poll failed; retrying in %.1fs (%s)",
                    retry_delay,
                    type(exc).__name__,
                )
                LOGGER.debug(
                    "job dispatcher poll failure details",
                    exc_info=True,
                )
                self._wake.wait(retry_delay)
                self._wake.clear()
                retry_delay = min(retry_delay * 2, 10.0)
                continue
            retry_delay = 0.5
            self._wake.wait(self.poll_seconds)
            self._wake.clear()

    def _run_job(self, job: dict[str, Any]) -> None:
        job_id = str(job["id"])
        user_cancelled = lambda: self.queue.is_cancel_requested(job_id)
        should_stop = lambda: self._stop.is_set() or user_cancelled()
        heartbeat_stop: threading.Event | None = None
        heartbeat: threading.Thread | None = None
        try:
            renew_lease = getattr(self.queue, "renew_lease", None)
            if callable(renew_lease):
                heartbeat_stop = threading.Event()
                stop_event = heartbeat_stop

                def renew_until_done() -> None:
                    while not stop_event.wait(self._lease_renewal_seconds):
                        try:
                            if not bool(renew_lease(job_id)):
                                return
                        except Exception as exc:
                            # A single transient renewal failure must not
                            # abort a valid provider call. The next heartbeat
                            # retries while the durable lease still exists.
                            LOGGER.warning(
                                "job lease renewal failed for %s (%s)",
                                job_id,
                                type(exc).__name__,
                            )

                heartbeat = threading.Thread(
                    target=renew_until_done,
                    name=f"article-job-lease-{job_id[:12]}",
                    daemon=True,
                )
                heartbeat.start()
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
            if heartbeat_stop is not None:
                heartbeat_stop.set()
            if heartbeat is not None:
                heartbeat.join(timeout=1.0)
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
