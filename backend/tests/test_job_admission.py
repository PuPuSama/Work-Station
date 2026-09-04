from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services.job_admission import JobAdmissionController  # noqa: E402
from services.job_queue import BatchJobRunner  # noqa: E402


class _Queue:
    lease_seconds = 60

    def __init__(self, organization_id: str, project_id: str, job_id: str) -> None:
        self.organization_id = organization_id
        self.project_id = project_id
        self._job = {"id": job_id}
        self._lock = threading.Lock()
        self.completed = threading.Event()

    def recover_interrupted(self, _operations=None) -> int:
        return 0

    def claim_jobs(self, limit, _operations=None):
        if limit <= 0:
            return []
        with self._lock:
            if self._job is None:
                return []
            job = self._job
            self._job = None
            return [job]

    def has_pending_jobs(self, _operations=None) -> bool:
        with self._lock:
            return self._job is not None

    def is_cancel_requested(self, _job_id: str) -> bool:
        return False

    def mark_succeeded(self, _job_id: str, _result_revision: int) -> None:
        self.completed.set()

    def mark_cancelled(self, _job_id: str) -> None:
        self.completed.set()

    def mark_interrupted(self, _job_id: str) -> None:
        self.completed.set()

    def mark_conflict(self, _job_id: str, _error: str) -> None:
        self.completed.set()

    def mark_failed(self, _job_id: str, _error: str, *, retryable: bool) -> str:
        del retryable
        self.completed.set()
        return "failed"


class JobAdmissionTests(unittest.TestCase):
    def test_global_and_project_limits_are_counted_separately(self) -> None:
        controller = JobAdmissionController(global_limit=3, project_limit=2)

        self.assertEqual(controller.reserve("org", "project-a", 5), 2)
        self.assertEqual(controller.reserve("org", "project-a", 1), 0)
        self.assertEqual(controller.reserve("org", "project-b", 5), 1)
        self.assertEqual(controller.snapshot().active_jobs, 3)

        controller.release("org", "project-a", 2)
        self.assertEqual(controller.reserve("org", "project-b", 2), 1)
        controller.release("org", "project-b", 2)
        self.assertEqual(controller.snapshot().active_jobs, 0)

    def test_runner_does_not_exceed_shared_global_limit(self) -> None:
        controller = JobAdmissionController(global_limit=1, project_limit=1)
        first_queue = _Queue("org", "project-a", "job-a")
        second_queue = _Queue("org", "project-b", "job-b")
        active = 0
        maximum_active = 0
        guard = threading.Lock()

        def handler(_job, _stop):
            nonlocal active, maximum_active
            with guard:
                active += 1
                maximum_active = max(maximum_active, active)
            time.sleep(0.04)
            with guard:
                active -= 1
            return 1

        first = BatchJobRunner(
            first_queue,
            handler,
            concurrency=3,
            poll_seconds=0.005,
            admission_controller=controller,
            operations=("article",),
        )
        second = BatchJobRunner(
            second_queue,
            handler,
            concurrency=3,
            poll_seconds=0.005,
            admission_controller=controller,
            operations=("article",),
        )
        try:
            first.start()
            second.start()
            self.assertTrue(first_queue.completed.wait(timeout=3))
            self.assertTrue(second_queue.completed.wait(timeout=3))
        finally:
            first.stop(timeout_seconds=1)
            second.stop(timeout_seconds=1)

        self.assertEqual(maximum_active, 1)
        self.assertEqual(controller.snapshot().active_jobs, 0)


if __name__ == "__main__":
    unittest.main()
