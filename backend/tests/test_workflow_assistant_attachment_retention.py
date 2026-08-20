from __future__ import annotations

import sys
import unittest
from pathlib import Path
from threading import Event


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from workflow_assistant.attachment_retention import (  # noqa: E402
    AttachmentRetentionRunner,
)


class Cleanup:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.called = Event()
        self.limits: list[int] = []

    def cleanup_expired(self, *, limit: int = 200) -> int:
        self.limits.append(limit)
        self.called.set()
        if self.fail:
            raise RuntimeError("temporary object store outage")
        return 0


class AttachmentRetentionRunnerTests(unittest.TestCase):
    def test_runs_immediately_and_stops_cleanly(self) -> None:
        cleanup = Cleanup()
        runner = AttachmentRetentionRunner(
            cleanup,
            interval_seconds=60,
            batch_limit=37,
        )

        runner.start()
        self.assertTrue(cleanup.called.wait(2))
        report = runner.stop(timeout_seconds=2)

        self.assertEqual(cleanup.limits, [37])
        self.assertTrue(report.stopped)
        self.assertFalse(report.alive)

    def test_transient_failure_does_not_kill_runner(self) -> None:
        cleanup = Cleanup(fail=True)
        runner = AttachmentRetentionRunner(cleanup, interval_seconds=60)

        runner.start()
        self.assertTrue(cleanup.called.wait(2))
        self.assertTrue(runner.running)
        self.assertFalse(runner.stop(timeout_seconds=2).alive)

    def test_rejects_unsafe_schedule_values(self) -> None:
        with self.assertRaises(ValueError):
            AttachmentRetentionRunner(Cleanup(), interval_seconds=0)
        with self.assertRaises(ValueError):
            AttachmentRetentionRunner(Cleanup(), batch_limit=1001)


if __name__ == "__main__":
    unittest.main()
