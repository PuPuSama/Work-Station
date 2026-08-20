from __future__ import annotations

from dataclasses import dataclass
from threading import Event, Thread
from typing import Protocol


class AttachmentCleanup(Protocol):
    def cleanup_expired(self, *, limit: int = 200) -> int: ...


@dataclass(frozen=True, slots=True)
class AttachmentRetentionStopReport:
    stopped: bool
    alive: bool


class AttachmentRetentionRunner:
    """Periodically retry the durable PostgreSQL-backed cleanup boundary."""

    def __init__(
        self,
        service: AttachmentCleanup,
        *,
        interval_seconds: float = 15 * 60,
        batch_limit: int = 200,
    ) -> None:
        if not 1 <= interval_seconds <= 24 * 60 * 60:
            raise ValueError("interval_seconds must be between 1 and 86400")
        if not 1 <= batch_limit <= 1000:
            raise ValueError("batch_limit must be between 1 and 1000")
        self._service = service
        self._interval_seconds = float(interval_seconds)
        self._batch_limit = int(batch_limit)
        self._stop = Event()
        self._wake = Event()
        self._thread: Thread | None = None

    @property
    def running(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._wake.clear()
        self._thread = Thread(
            target=self._run,
            name="workflow-assistant-attachment-retention",
            daemon=True,
        )
        self._thread.start()

    def wake(self) -> None:
        self._wake.set()

    def stop(self, *, timeout_seconds: float = 10.0) -> AttachmentRetentionStopReport:
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative")
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout_seconds)
        return AttachmentRetentionStopReport(
            stopped=True,
            alive=bool(thread is not None and thread.is_alive()),
        )

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._service.cleanup_expired(limit=self._batch_limit)
            except Exception:
                # Claims and metadata remain durable; the next interval retries.
                pass
            self._wake.wait(self._interval_seconds)
            self._wake.clear()


__all__ = [
    "AttachmentRetentionRunner",
    "AttachmentRetentionStopReport",
]
