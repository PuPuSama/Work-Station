from __future__ import annotations

import tempfile
import threading
import time
import unittest
import sys
from pathlib import Path
from unittest.mock import patch

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services.job_queue import (
    ActiveJobError,
    BatchJobRunner,
    JobConflict,
    JobQueue,
)


def queue_item(task_id: str, revision: int = 0) -> dict:
    return {
        "task_id": task_id,
        "customer": "example.com",
        "topic_index": int(task_id.rsplit("-", 1)[-1]) if "-" in task_id else 1,
        "topic": f"Topic for {task_id}",
        "source_revision": revision,
        "request": {"revision": revision},
    }


class JobQueueTests(unittest.TestCase):
    def test_queue_persists_recovers_interrupted_jobs_and_blocks_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.sqlite3"
            queue = JobQueue(path)
            created = queue.create_batch("outline", [queue_item("task-1")])
            self.assertEqual(created["status"], "queued")
            claimed = queue.claim_jobs(1)
            self.assertEqual(claimed[0]["status"], "running")

            reopened = JobQueue(path)
            self.assertEqual(reopened.get_batch(created["id"])["jobs"][0]["status"], "running")
            self.assertEqual(reopened.recover_interrupted(), 1)
            self.assertEqual(reopened.get_batch(created["id"])["jobs"][0]["status"], "queued")
            with self.assertRaises(ActiveJobError):
                reopened.create_batch("outline", [queue_item("task-1")])

    def test_runner_enforces_concurrency_and_completes_every_job(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            queue = JobQueue(Path(directory) / "jobs.sqlite3")
            batch = queue.create_batch(
                "outline",
                [queue_item(f"task-{index}") for index in range(1, 7)],
            )
            guard = threading.Lock()
            active = 0
            maximum = 0

            def handler(job, cancelled):
                nonlocal active, maximum
                with guard:
                    active += 1
                    maximum = max(maximum, active)
                time.sleep(0.06)
                with guard:
                    active -= 1
                return int(job["source_revision"]) + 1

            runner = BatchJobRunner(queue, handler, concurrency=2, poll_seconds=0.01)
            runner.start()
            try:
                deadline = time.time() + 5
                while time.time() < deadline:
                    current = queue.get_batch(batch["id"])
                    if current["status"] == "succeeded":
                        break
                    time.sleep(0.02)
                else:
                    self.fail("Batch did not complete before the test deadline.")
            finally:
                runner.stop()

            self.assertEqual(maximum, 2)
            self.assertTrue(
                all(job["status"] == "succeeded" for job in current["jobs"])
            )

    def test_transient_failure_retries_and_conflict_does_not_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            queue = JobQueue(Path(directory) / "jobs.sqlite3")
            retry_batch = queue.create_batch("outline", [queue_item("task-1")])
            conflict_batch = queue.create_batch("outline", [queue_item("task-2")])
            attempts: dict[str, int] = {}

            def handler(job, cancelled):
                task_id = str(job["task_id"])
                attempts[task_id] = attempts.get(task_id, 0) + 1
                if task_id == "task-1" and attempts[task_id] == 1:
                    raise RuntimeError("503 temporarily unavailable")
                if task_id == "task-2":
                    raise JobConflict("Task changed after it was queued")
                return 1

            with patch("services.job_queue.RETRY_DELAYS_SECONDS", (0.01, 0.01, 0.01)):
                runner = BatchJobRunner(queue, handler, concurrency=2, poll_seconds=0.01)
                runner.start()
                try:
                    deadline = time.time() + 5
                    while time.time() < deadline:
                        retried = queue.get_batch(retry_batch["id"])
                        conflicted = queue.get_batch(conflict_batch["id"])
                        if (
                            retried["status"] == "succeeded"
                            and conflicted["status"] == "completed_with_errors"
                        ):
                            break
                        time.sleep(0.02)
                    else:
                        self.fail("Retry/conflict jobs did not settle before the deadline.")
                finally:
                    runner.stop()

            self.assertEqual(attempts["task-1"], 2)
            self.assertEqual(attempts["task-2"], 1)
            self.assertEqual(conflicted["jobs"][0]["status"], "conflict")

    def test_cancelled_queued_job_never_runs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            queue = JobQueue(Path(directory) / "jobs.sqlite3")
            batch = queue.create_batch("article", [queue_item("task-1")])
            job_id = batch["jobs"][0]["id"]
            cancelled = queue.request_cancel(job_id)
            self.assertEqual(cancelled["status"], "cancelled")
            self.assertEqual(queue.claim_jobs(1), [])

    def test_runner_only_claims_its_configured_operation_family(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            queue = JobQueue(Path(directory) / "jobs.sqlite3")
            writing_batch = queue.create_batch("titles", [queue_item("task-1")])
            product_batch = queue.create_batch("products", [queue_item("task-2")])
            handled: list[str] = []

            def handler(job, cancelled):
                handled.append(str(job["operation"]))
                return 1

            runner = BatchJobRunner(
                queue,
                handler,
                concurrency=2,
                poll_seconds=0.01,
                operations=("products",),
            )
            runner.start()
            try:
                deadline = time.time() + 5
                while time.time() < deadline:
                    current = queue.get_batch(product_batch["id"])
                    if current["status"] == "succeeded":
                        break
                    time.sleep(0.02)
                else:
                    self.fail("Product-only runner did not complete its product job.")
            finally:
                runner.stop()

            self.assertEqual(handled, ["products"])
            self.assertEqual(queue.get_batch(writing_batch["id"])["status"], "queued")


if __name__ == "__main__":
    unittest.main()
