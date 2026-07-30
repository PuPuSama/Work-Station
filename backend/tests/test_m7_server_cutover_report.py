from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services.server_cutover_report import (  # noqa: E402
    ReadOnlySQLiteJobSource,
    ReadOnlySQLiteTaskSource,
    build_server_cutover_report,
    compare_job_queues,
    compare_task_stores,
)
from services.job_queue import JobQueue  # noqa: E402
from services.task_repository import SQLiteTaskRepository  # noqa: E402


class FakeTaskRepository:
    def __init__(self, records):
        self.records = copy.deepcopy(records)
        self.read_count = 0

    def load_all(self):
        self.read_count += 1
        return copy.deepcopy(self.records)


class FakePostgresTaskRepository(FakeTaskRepository):
    def __init__(self, records, organization_id="org-a", project_id="project-a"):
        super().__init__(records)
        self.organization_id = organization_id
        self.project_id = project_id


class FakeJobQueue:
    def __init__(self, batches, organization_id=None, project_id=None):
        self.batches = copy.deepcopy(batches)
        self.organization_id = organization_id
        self.project_id = project_id
        self.read_count = 0

    def export_batches(self):
        self.read_count += 1
        return copy.deepcopy(self.batches)


def task(task_id, *, value, organization_id=None, project_id=None):
    record = {"id": task_id, "revision": 1, "value": value}
    if organization_id is not None:
        record["organization_id"] = organization_id
    if project_id is not None:
        record["project_id"] = project_id
    return record


def batch(status="completed"):
    return {
        "id": "batch-a",
        "operation": "article",
        "customer": "project-a",
        "jobs": [
            {
                "id": "job-a",
                "task_id": "task-a",
                "customer": "project-a",
                "topic_index": 1,
                "topic": "Topic",
                "operation": "article",
                "status": status,
                "request": {"mode": "test"},
                "source_revision": 1,
                "result_revision": 2 if status == "completed" else None,
                "attempts": 1,
                "max_attempts": 4,
                "cancel_requested": False,
                "error": "",
            }
        ],
    }


class ServerCutoverReportTests(unittest.TestCase):
    def test_matching_report_is_read_only_and_contains_no_payload_text(self) -> None:
        source_tasks = FakeTaskRepository(
            [task("task-a", value="private article body")]
        )
        target_tasks = FakePostgresTaskRepository(
            [
                task(
                    "task-a",
                    value="private article body",
                    organization_id="org-a",
                    project_id="project-a",
                )
            ]
        )
        source_jobs = FakeJobQueue([batch()])
        target_jobs = FakeJobQueue(
            [batch()],
            organization_id="org-a",
            project_id="project-a",
        )

        report = build_server_cutover_report(
            task_source=source_tasks,
            task_target=target_tasks,  # type: ignore[arg-type]
            job_source=source_jobs,
            job_target=target_jobs,  # type: ignore[arg-type]
        )

        self.assertTrue(report.ready_for_single_write)
        self.assertEqual(source_tasks.read_count, 1)
        self.assertEqual(target_tasks.read_count, 1)
        self.assertEqual(source_jobs.read_count, 1)
        self.assertEqual(target_jobs.read_count, 1)
        self.assertNotIn(
            "private article body",
            str(report.public_values()),
        )

    def test_task_report_identifies_order_missing_and_changed_ids(self) -> None:
        source = FakeTaskRepository(
            [
                task("task-a", value="source"),
                task("task-b", value="same"),
            ]
        )
        target = FakePostgresTaskRepository(
            [
                task(
                    "task-b",
                    value="same",
                    organization_id="org-a",
                    project_id="project-a",
                ),
                task(
                    "task-a",
                    value="target",
                    organization_id="org-a",
                    project_id="project-a",
                ),
                task(
                    "task-c",
                    value="extra",
                    organization_id="org-a",
                    project_id="project-a",
                ),
            ]
        )

        report = compare_task_stores(source, target)  # type: ignore[arg-type]

        self.assertFalse(report.matches)
        self.assertFalse(report.order_matches)
        self.assertEqual(report.source_only_ids, ())
        self.assertEqual(report.target_only_ids, ("task-c",))
        self.assertEqual(report.changed_ids, ("task-a",))

    def test_active_sqlite_job_blocks_cutover_even_when_histories_match(self) -> None:
        source = FakeJobQueue([batch("queued")])
        target = FakeJobQueue(
            [batch("queued")],
            organization_id="org-a",
            project_id="project-a",
        )

        report = compare_job_queues(source, target)  # type: ignore[arg-type]

        self.assertFalse(report.matches)
        self.assertEqual(report.source_active_job_ids, ("job-a",))

    def test_mismatched_task_and_job_target_scope_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "same scope"):
            build_server_cutover_report(
                task_source=FakeTaskRepository([]),
                task_target=FakePostgresTaskRepository([]),  # type: ignore[arg-type]
                job_source=FakeJobQueue([]),
                job_target=FakeJobQueue(  # type: ignore[arg-type]
                    [],
                    organization_id="org-b",
                    project_id="project-a",
                ),
            )

    def test_invalid_or_duplicate_task_ids_never_compare_as_ready(self) -> None:
        source = FakeTaskRepository(
            [
                task("", value="invalid"),
                task("task-a", value="first"),
                task("task-a", value="duplicate"),
            ]
        )
        target = FakePostgresTaskRepository([])

        report = compare_task_stores(source, target)  # type: ignore[arg-type]

        self.assertFalse(report.matches)
        self.assertEqual(report.source_invalid_id_count, 1)
        self.assertEqual(report.source_duplicate_ids, ("task-a",))

    def test_read_only_sqlite_sources_match_existing_repository_exports(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            writable_tasks = SQLiteTaskRepository(root / "tasks.json")
            writable_tasks.replace_all(
                [task("task-a", value="private")]
            )
            writable_jobs = JobQueue(root / "jobs.sqlite3")
            writable_jobs.create_batch(
                "outline",
                [{"task_id": "task-a", "source_revision": 1}],
            )
            claimed = writable_jobs.claim_jobs(1)[0]
            writable_jobs.mark_succeeded(claimed["id"], 2)
            cancelled = writable_jobs.create_batch(
                "article",
                [{"task_id": "task-b", "source_revision": 3}],
            )
            writable_jobs.cancel_batch(cancelled["id"])

            read_only_tasks = ReadOnlySQLiteTaskSource(
                writable_tasks.database_path
            )
            read_only_jobs = ReadOnlySQLiteJobSource(
                writable_jobs.path
            )

            self.assertEqual(
                read_only_tasks.load_all(),
                writable_tasks.load_all(),
            )
            self.assertEqual(
                read_only_jobs.export_batches(),
                writable_jobs.export_batches(),
            )


if __name__ == "__main__":
    unittest.main()
