from __future__ import annotations

import sys
import threading
import unittest
from http.client import RemoteDisconnected
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services.job_queue import BatchJobRunner, is_retryable_error  # noqa: E402
from services.authorized_job_queue import (  # noqa: E402
    authorized_batch_runner,
)


class FlakyQueue:
    def __init__(self) -> None:
        self.claim_calls = 0
        self.succeeded = threading.Event()

    def recover_interrupted(self, _operations=None) -> int:
        return 0

    def claim_jobs(self, _limit, _operations=None):
        self.claim_calls += 1
        if self.claim_calls == 1:
            raise TimeoutError("QueuePool connection timed out")
        return [{"id": "job-a"}] if not self.succeeded.is_set() else []

    def is_cancel_requested(self, _job_id: str) -> bool:
        return False

    def mark_succeeded(self, _job_id: str, _result_revision: int) -> None:
        self.succeeded.set()

    def mark_cancelled(self, _job_id: str) -> None:
        self.succeeded.set()

    def mark_interrupted(self, _job_id: str) -> None:
        self.succeeded.set()

    def mark_conflict(self, _job_id: str, _error: str) -> None:
        self.succeeded.set()

    def mark_failed(self, _job_id: str, _error: str, *, retryable: bool) -> str:
        del retryable
        self.succeeded.set()
        return "failed"


class JobQueueResilienceTests(unittest.TestCase):
    def test_authorized_runner_preserves_configured_concurrency(self) -> None:
        queue = SimpleNamespace(
            organization_id="org-a",
            project_id="project-a",
            worker_id="worker-a",
        )

        with patch("services.authorized_job_queue.BatchJobRunner") as factory:
            runner = authorized_batch_runner(
                queue,
                lambda _job, _stop: 1,
                access=object(),
                operations=("article",),
                concurrency=5,
            )

        self.assertIs(runner, factory.return_value)
        self.assertEqual(factory.call_args.kwargs["concurrency"], 5)

    def test_transport_disconnect_is_retryable_even_without_error_text(self) -> None:
        self.assertTrue(is_retryable_error(RemoteDisconnected()))
        wrapped = RuntimeError("planner failed")
        wrapped.__cause__ = RemoteDisconnected()
        self.assertTrue(
            is_retryable_error(wrapped)
        )

    def test_dispatcher_survives_transient_claim_failure(self) -> None:
        queue = FlakyQueue()
        runner = BatchJobRunner(
            queue,
            lambda _job, _stop: 1,
            concurrency=1,
            poll_seconds=0.01,
            operations=("article",),
        )

        runner.start()
        self.assertTrue(
            queue.succeeded.wait(timeout=3),
            "dispatcher did not recover and execute the queued job",
        )
        self.assertGreaterEqual(queue.claim_calls, 2)
        report = runner.stop(timeout_seconds=1)

        self.assertTrue(report.dispatcher_stopped)
        self.assertEqual(report.remaining_jobs, 0)


if __name__ == "__main__":
    unittest.main()
