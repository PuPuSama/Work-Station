from __future__ import annotations

import os
import sys
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from pathlib import Path
from types import SimpleNamespace

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from knowledge_agent.contracts import KnowledgeProject  # noqa: E402
from knowledge_agent.database import create_knowledge_engine  # noqa: E402
from knowledge_agent.repository import PostgresKnowledgeRepository  # noqa: E402
from knowledge_agent.schema import projects  # noqa: E402
from models import TaskRecord  # noqa: E402
from server_schema import (  # noqa: E402
    article_tasks,
    background_jobs,
    job_batches,
    organizations,
    project_ownership,
    task_store_state,
    workspace_users,
)
from services.postgres_task_repository import PostgresTaskRepository  # noqa: E402
from services.postgres_job_queue import PostgresJobQueue  # noqa: E402
from services.job_queue import ActiveJobError, JobQueue  # noqa: E402
from services.job_queue_migration import (  # noqa: E402
    JobQueueMigrationConflict,
    migrate_terminal_job_history,
)
from services.task_repository import SQLiteTaskRepository  # noqa: E402
from services.task_store_migration import (  # noqa: E402
    TaskStoreMigrationConflict,
    migrate_task_store,
)
from storage import RevisionConflictError, TaskStore  # noqa: E402


DATABASE_URL_ENV = "ARTICLE_AGENT_DATABASE_URL"


@unittest.skipUnless(
    os.environ.get(DATABASE_URL_ENV),
    f"{DATABASE_URL_ENV} is required for PostgreSQL integration tests",
)
class M7PostgresTaskRepositoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = create_knowledge_engine(os.environ[DATABASE_URL_ENV])
        cls.knowledge = PostgresKnowledgeRepository(cls.engine)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    def setUp(self) -> None:
        self.prefix = f"m7-task-{uuid.uuid4().hex}"
        self.organization_id = f"{self.prefix}-org"
        self.other_organization_id = f"{self.prefix}-other-org"
        self.project_a = f"{self.prefix}-project-a"
        self.project_b = f"{self.prefix}-project-b"
        self.admin_id = f"{self.prefix}-admin"
        self.project_ids = (self.project_a, self.project_b)

        for project_id in self.project_ids:
            self.knowledge.upsert_project(
                KnowledgeProject(
                    project_id=project_id,
                    customer_name=project_id,
                    official_domain=f"{project_id}.example.test",
                )
            )
        with self.engine.begin() as connection:
            connection.execute(
                organizations.insert(),
                (
                    {
                        "organization_id": self.organization_id,
                        "name": "Task test organization",
                    },
                    {
                        "organization_id": self.other_organization_id,
                        "name": "Other task test organization",
                    },
                ),
            )
            connection.execute(
                workspace_users.insert().values(
                    organization_id=self.organization_id,
                    user_id=self.admin_id,
                    display_name="Task Admin",
                    organization_role="org_admin",
                )
            )
            connection.execute(
                project_ownership.insert(),
                tuple(
                    {
                        "project_id": project_id,
                        "organization_id": self.organization_id,
                        "owning_team_id": None,
                    }
                    for project_id in self.project_ids
                ),
            )

        self.repository_a = PostgresTaskRepository(
            self.engine,
            organization_id=self.organization_id,
            project_id=self.project_a,
        )
        self.repository_b = PostgresTaskRepository(
            self.engine,
            organization_id=self.organization_id,
            project_id=self.project_b,
        )

    def tearDown(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                background_jobs.delete().where(
                    background_jobs.c.organization_id == self.organization_id
                )
            )
            connection.execute(
                job_batches.delete().where(
                    job_batches.c.organization_id == self.organization_id
                )
            )
            connection.execute(
                article_tasks.delete().where(
                    article_tasks.c.organization_id == self.organization_id
                )
            )
            connection.execute(
                task_store_state.delete().where(
                    task_store_state.c.organization_id == self.organization_id
                )
            )
            connection.execute(
                project_ownership.delete().where(
                    project_ownership.c.project_id.in_(self.project_ids)
                )
            )
            connection.execute(
                workspace_users.delete().where(
                    workspace_users.c.organization_id == self.organization_id
                )
            )
            connection.execute(
                organizations.delete().where(
                    organizations.c.organization_id.in_(
                        (
                            self.organization_id,
                            self.other_organization_id,
                        )
                    )
                )
            )
            connection.execute(
                projects.delete().where(projects.c.project_id.in_(self.project_ids))
            )

    def _record(self, task_id: str, *, topic_index: int = 1) -> dict[str, object]:
        return {
            "id": task_id,
            "customer": "example.test",
            "topic_index": topic_index,
            "topic": f"Topic {topic_index}",
            "revision": 0,
            "updated_at": "2026-07-30T12:00:00",
            "extension_field": {"preserved": True},
        }

    def _task(self, task_id: str) -> TaskRecord:
        return TaskRecord(
            id=task_id,
            week_folder="全部项目",
            customer="example.test",
            topic_index=1,
            topic="TaskStore integration",
            task_dir=str(Path("projects") / task_id),
            created_at="2026-07-30T12:00:00",
            updated_at="2026-07-30T12:00:00",
        )

    def test_same_task_id_is_isolated_by_project(self) -> None:
        self.repository_a.upsert(
            {**self._record("shared"), "topic": "Project A"}
        )
        self.repository_b.upsert(
            {**self._record("shared"), "topic": "Project B"}
        )

        task_a = self.repository_a.get("shared")
        task_b = self.repository_b.get("shared")

        self.assertEqual(task_a["topic"], "Project A")  # type: ignore[index]
        self.assertEqual(task_b["topic"], "Project B")  # type: ignore[index]
        self.assertEqual(task_a["project_id"], self.project_a)  # type: ignore[index]
        self.assertEqual(task_b["project_id"], self.project_b)  # type: ignore[index]

    def test_replace_upsert_delete_and_scope_validation(self) -> None:
        self.repository_a.replace_all(
            (
                self._record("task-2", topic_index=2),
                self._record("task-1", topic_index=1),
            )
        )
        self.assertTrue(self.repository_a.is_initialized())
        self.assertEqual(
            [record["id"] for record in self.repository_a.load_all()],
            ["task-2", "task-1"],
        )

        changed = self._record("task-2", topic_index=22)
        changed["revision"] = 3
        self.repository_a.upsert(changed)
        self.repository_a.upsert_many(
            (
                self._record("task-1", topic_index=11),
                self._record("task-3", topic_index=3),
            )
        )
        self.assertEqual(
            [record["id"] for record in self.repository_a.load_all()],
            ["task-2", "task-1", "task-3"],
        )
        self.assertEqual(self.repository_a.get("task-2")["revision"], 3)  # type: ignore[index]
        self.assertEqual(self.repository_a.delete_many(("task-1", "task-3")), 2)

        with self.assertRaisesRegex(ValueError, "project_id"):
            self.repository_a.upsert(
                {
                    **self._record("wrong-project"),
                    "project_id": self.project_b,
                }
            )

    def test_task_store_keeps_revision_and_extension_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(
                SimpleNamespace(data_file=Path(directory) / "tasks.json"),
                repository=self.repository_a,
                legacy_import_enabled=False,
            )
            created = store.put(self._task("workflow-task"), expected_revision=0)
            stale = created.model_copy(deep=True)
            created.selected_title = "Chosen title"
            updated = store.put(created, expected_revision=0)

            self.assertEqual(updated.revision, 1)
            loaded = store.get("workflow-task")
            self.assertEqual(loaded.selected_title, "Chosen title")
            self.assertEqual(loaded.model_extra["organization_id"], self.organization_id)
            self.assertEqual(loaded.model_extra["project_id"], self.project_a)
            with self.assertRaises(RevisionConflictError):
                store.put(stale, expected_revision=0)

    def test_sqlite_import_is_verified_and_never_overwrites_divergence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = SQLiteTaskRepository(Path(directory) / "tasks.json")
            source.replace_all(
                (
                    self._record("import-1", topic_index=1),
                    self._record("import-2", topic_index=2),
                )
            )

            first = migrate_task_store(source, self.repository_a)
            repeated = migrate_task_store(source, self.repository_a)
            self.assertTrue(first.imported)
            self.assertEqual(first.target_after_count, 2)
            self.assertTrue(repeated.already_matched)
            self.assertFalse(repeated.imported)
            self.assertEqual(first.source_digest, first.target_digest)

            changed = self.repository_a.get("import-1")
            changed["topic"] = "Divergent target"  # type: ignore[index]
            self.repository_a.upsert(changed)  # type: ignore[arg-type]
            with self.assertRaisesRegex(
                TaskStoreMigrationConflict,
                "differs from source",
            ):
                migrate_task_store(source, self.repository_a)

    def test_database_rejects_scope_without_project_ownership(self) -> None:
        invalid_repository = PostgresTaskRepository(
            self.engine,
            organization_id=self.other_organization_id,
            project_id=self.project_a,
        )
        with self.assertRaises(IntegrityError):
            invalid_repository.upsert(self._record("cross-org"))

    def test_postgres_jobs_claim_once_and_preserve_batch_contract(self) -> None:
        self.repository_a.replace_all(
            (
                self._record("job-task-1", topic_index=1),
                self._record("job-task-2", topic_index=2),
            )
        )
        first_worker = PostgresJobQueue(
            self.engine,
            organization_id=self.organization_id,
            project_id=self.project_a,
            worker_id=f"{self.prefix}-worker-1",
        )
        second_worker = PostgresJobQueue(
            self.engine,
            organization_id=self.organization_id,
            project_id=self.project_a,
            worker_id=f"{self.prefix}-worker-2",
        )
        batch = first_worker.create_batch(
            "outline",
            [
                {
                    "task_id": "job-task-1",
                    "customer": "example.test",
                    "topic_index": 1,
                    "topic": "One",
                    "source_revision": 0,
                    "request": {"kind": "first"},
                },
                {
                    "task_id": "job-task-2",
                    "customer": "example.test",
                    "topic_index": 2,
                    "topic": "Two",
                    "source_revision": 0,
                    "request": {"kind": "second"},
                },
            ],
            customer="example.test",
        )
        with self.assertRaises(ActiveJobError):
            first_worker.create_batch(
                "article",
                [
                    {
                        "task_id": "job-task-1",
                        "source_revision": 0,
                    }
                ],
            )

        first_claim = first_worker.claim_jobs(1)
        second_claim = second_worker.claim_jobs(2)
        self.assertEqual(len(first_claim), 1)
        self.assertEqual(len(second_claim), 1)
        self.assertNotEqual(first_claim[0]["id"], second_claim[0]["id"])
        self.assertEqual(first_claim[0]["attempts"], 1)
        self.assertEqual(first_claim[0]["request"], {"kind": "first"})

        second_worker.mark_succeeded(first_claim[0]["id"], 99)
        self.assertEqual(
            first_worker.get_job(first_claim[0]["id"])["status"],
            "running",
        )
        first_worker.mark_succeeded(first_claim[0]["id"], 1)
        second_worker.mark_succeeded(second_claim[0]["id"], 2)
        completed = first_worker.get_batch(batch["id"])
        self.assertEqual(completed["status"], "succeeded")
        self.assertEqual(completed["completed"], 2)
        self.assertEqual(
            {job["result_revision"] for job in completed["jobs"]},
            {1, 2},
        )
        self.assertEqual(completed["organization_id"], self.organization_id)
        self.assertEqual(completed["project_id"], self.project_a)

    def test_expired_lease_retry_conflict_and_cancel_flow(self) -> None:
        self.repository_a.replace_all((self._record("lease-task"),))
        first_worker = PostgresJobQueue(
            self.engine,
            organization_id=self.organization_id,
            project_id=self.project_a,
            worker_id=f"{self.prefix}-lease-worker-1",
            lease_seconds=60,
        )
        second_worker = PostgresJobQueue(
            self.engine,
            organization_id=self.organization_id,
            project_id=self.project_a,
            worker_id=f"{self.prefix}-lease-worker-2",
            lease_seconds=60,
        )
        batch = first_worker.create_batch(
            "article",
            [{"task_id": "lease-task", "source_revision": 0}],
        )
        claimed = first_worker.claim_jobs(1)[0]
        with self.engine.begin() as connection:
            connection.execute(
                background_jobs.update()
                .where(
                    background_jobs.c.organization_id == self.organization_id,
                    background_jobs.c.project_id == self.project_a,
                    background_jobs.c.job_id == claimed["id"],
                )
                .values(
                    lease_expires_at=datetime.now(timezone.utc)
                    - timedelta(seconds=1)
                )
            )

        self.assertEqual(second_worker.recover_interrupted(), 1)
        reclaimed = second_worker.claim_jobs(1)[0]
        self.assertEqual(reclaimed["attempts"], 2)
        first_worker.mark_succeeded(reclaimed["id"], 100)
        self.assertEqual(
            second_worker.get_job(reclaimed["id"])["status"],
            "running",
        )

        status = second_worker.mark_failed(
            reclaimed["id"],
            "temporary timeout",
            retryable=True,
        )
        self.assertEqual(status, "retry_wait")
        with self.engine.begin() as connection:
            connection.execute(
                background_jobs.update()
                .where(
                    background_jobs.c.organization_id == self.organization_id,
                    background_jobs.c.project_id == self.project_a,
                    background_jobs.c.job_id == reclaimed["id"],
                )
                .values(available_at=datetime.now(timezone.utc))
            )
        retried = second_worker.claim_jobs(1)[0]
        second_worker.mark_conflict(retried["id"], "stale revision")
        conflict = second_worker.get_job(retried["id"])
        self.assertEqual(conflict["status"], "conflict")
        queued = second_worker.retry_job(
            retried["id"],
            source_revision=3,
            request={"retry": True},
        )
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(queued["source_revision"], 3)
        self.assertEqual(queued["request"], {"retry": True})
        cancelled = second_worker.request_cancel(retried["id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertTrue(cancelled["cancel_requested"])
        self.assertEqual(
            second_worker.get_batch(batch["id"])["status"],
            "cancelled",
        )

    def test_job_queue_is_project_scoped_and_requires_a_task(self) -> None:
        self.repository_a.replace_all((self._record("shared-job-task"),))
        self.repository_b.replace_all((self._record("shared-job-task"),))
        queue_a = PostgresJobQueue(
            self.engine,
            organization_id=self.organization_id,
            project_id=self.project_a,
        )
        queue_b = PostgresJobQueue(
            self.engine,
            organization_id=self.organization_id,
            project_id=self.project_b,
        )
        batch_a = queue_a.create_batch(
            "outline",
            [{"task_id": "shared-job-task", "source_revision": 0}],
        )
        batch_b = queue_b.create_batch(
            "outline",
            [{"task_id": "shared-job-task", "source_revision": 0}],
        )

        self.assertEqual(len(queue_a.list_batches()), 1)
        self.assertEqual(len(queue_b.list_batches()), 1)
        self.assertNotEqual(batch_a["id"], batch_b["id"])
        with self.assertRaises(KeyError):
            queue_a.get_batch(batch_b["id"])
        with self.assertRaises(KeyError):
            queue_a.create_batch(
                "outline",
                [{"task_id": "missing-task", "source_revision": 0}],
            )

    def test_concurrent_workers_claim_disjoint_jobs(self) -> None:
        task_ids = tuple(f"concurrent-{index}" for index in range(4))
        self.repository_a.replace_all(
            tuple(
                self._record(task_id, topic_index=index)
                for index, task_id in enumerate(task_ids)
            )
        )
        first_worker = PostgresJobQueue(
            self.engine,
            organization_id=self.organization_id,
            project_id=self.project_a,
            worker_id=f"{self.prefix}-concurrent-1",
        )
        second_worker = PostgresJobQueue(
            self.engine,
            organization_id=self.organization_id,
            project_id=self.project_a,
            worker_id=f"{self.prefix}-concurrent-2",
        )
        first_worker.create_batch(
            "outline",
            [
                {
                    "task_id": task_id,
                    "topic_index": index,
                    "source_revision": 0,
                }
                for index, task_id in enumerate(task_ids)
            ],
        )
        barrier = Barrier(2)

        def claim(queue: PostgresJobQueue) -> list[dict[str, object]]:
            barrier.wait(timeout=10)
            return queue.claim_jobs(2)

        with ThreadPoolExecutor(max_workers=2) as executor:
            first_future = executor.submit(claim, first_worker)
            second_future = executor.submit(claim, second_worker)
            first_claim = first_future.result(timeout=20)
            second_claim = second_future.result(timeout=20)

        first_ids = {str(job["id"]) for job in first_claim}
        second_ids = {str(job["id"]) for job in second_claim}
        self.assertEqual(len(first_ids), 2)
        self.assertEqual(len(second_ids), 2)
        self.assertFalse(first_ids.intersection(second_ids))
        self.assertEqual(len(first_ids.union(second_ids)), 4)

    def test_task_job_schema_has_scoped_constraints_and_partial_index(
        self,
    ) -> None:
        inspector = sa.inspect(self.engine)
        self.assertTrue(
            {
                "task_store_state",
                "article_tasks",
                "job_batches",
                "background_jobs",
            }.issubset(inspector.get_table_names())
        )
        with self.engine.connect() as connection:
            constraint_names = set(
                connection.execute(
                    sa.text(
                        """
                        SELECT conname
                        FROM pg_constraint
                        WHERE conrelid IN (
                            'task_store_state'::regclass,
                            'article_tasks'::regclass,
                            'job_batches'::regclass,
                            'background_jobs'::regclass
                        )
                        """
                    )
                ).scalars()
            )
            active_index = connection.execute(
                sa.text(
                    """
                    SELECT pg_get_indexdef(index_relation.oid)
                    FROM pg_class AS index_relation
                    WHERE index_relation.relname =
                        'uq_background_jobs_active_task'
                    """
                )
            ).scalar_one()
        self.assertTrue(
            {
                "fk_task_store_state_project",
                "pk_article_tasks",
                "fk_article_tasks_project",
                "pk_job_batches",
                "fk_job_batches_project",
                "pk_background_jobs",
                "fk_background_jobs_batch",
                "fk_background_jobs_task",
                "ck_background_jobs_status",
                "ck_background_jobs_lease_state",
            }.issubset(constraint_names)
        )
        self.assertIn(
            "WHERE (status = ANY",
            active_index,
        )

    def test_task_revision_compare_and_swap_allows_one_writer(self) -> None:
        self.repository_a.upsert(self._record("cas-task"))
        barrier = Barrier(2)

        def update(topic: str) -> bool:
            payload = self.repository_a.get("cas-task")
            payload["topic"] = topic  # type: ignore[index]
            payload["revision"] = 1  # type: ignore[index]
            barrier.wait(timeout=10)
            return self.repository_a.put_if_revision(
                payload,  # type: ignore[arg-type]
                expected_revision=0,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = [
                future.result(timeout=20)
                for future in (
                    executor.submit(update, "Writer A"),
                    executor.submit(update, "Writer B"),
                )
            ]
        stored = self.repository_a.get("cas-task")

        self.assertCountEqual(outcomes, (True, False))
        self.assertEqual(stored["revision"], 1)  # type: ignore[index]
        self.assertIn(stored["topic"], {"Writer A", "Writer B"})  # type: ignore[index]

    def test_terminal_sqlite_job_history_import_is_drained_and_verified(
        self,
    ) -> None:
        self.repository_a.replace_all(
            (
                self._record("history-task-1", topic_index=1),
                self._record("history-task-2", topic_index=2),
            )
        )
        target = PostgresJobQueue(
            self.engine,
            organization_id=self.organization_id,
            project_id=self.project_a,
        )
        with tempfile.TemporaryDirectory() as directory:
            source = JobQueue(Path(directory) / "jobs.sqlite3")
            succeeded_batch = source.create_batch(
                "outline",
                [
                    {
                        "task_id": "history-task-1",
                        "source_revision": 0,
                        "request": {"kind": "history"},
                    }
                ],
            )
            succeeded_job = source.claim_jobs(1)[0]
            source.mark_succeeded(succeeded_job["id"], 1)
            cancelled_batch = source.create_batch(
                "article",
                [
                    {
                        "task_id": "history-task-2",
                        "source_revision": 2,
                    }
                ],
            )
            source.cancel_batch(cancelled_batch["id"])

            first = migrate_terminal_job_history(source, target)
            repeated = migrate_terminal_job_history(source, target)

        self.assertTrue(first.imported)
        self.assertEqual(first.source.batch_count, 2)
        self.assertEqual(first.source.job_count, 2)
        self.assertEqual(
            first.source.status_counts,
            {"cancelled": 1, "succeeded": 1},
        )
        self.assertEqual(first.source, first.target_after)
        self.assertTrue(repeated.already_matched)
        self.assertEqual(
            target.get_batch(succeeded_batch["id"])["status"],
            "succeeded",
        )

        with self.engine.begin() as connection:
            connection.execute(
                background_jobs.update()
                .where(
                    background_jobs.c.organization_id == self.organization_id,
                    background_jobs.c.project_id == self.project_a,
                    background_jobs.c.job_id == succeeded_job["id"],
                )
                .values(error="divergent history")
            )
        with tempfile.TemporaryDirectory() as directory:
            source = JobQueue(Path(directory) / "jobs.sqlite3")
            source_batch = source.create_batch(
                "outline",
                [
                    {
                        "task_id": "history-task-1",
                        "source_revision": 0,
                        "request": {"kind": "history"},
                    }
                ],
            )
            source_job = source.claim_jobs(1)[0]
            source.mark_succeeded(source_job["id"], 1)
            # Stable IDs are part of the proof. This separately constructed
            # history must never be treated as the already-imported source.
            self.assertNotEqual(source_batch["id"], succeeded_batch["id"])
            with self.assertRaisesRegex(
                JobQueueMigrationConflict,
                "differs from SQLite history",
            ):
                migrate_terminal_job_history(source, target)

    def test_active_sqlite_job_blocks_history_cutover(self) -> None:
        target = PostgresJobQueue(
            self.engine,
            organization_id=self.organization_id,
            project_id=self.project_a,
        )
        with tempfile.TemporaryDirectory() as directory:
            source = JobQueue(Path(directory) / "jobs.sqlite3")
            source.create_batch(
                "outline",
                [{"task_id": "still-active", "source_revision": 0}],
            )
            with self.assertRaisesRegex(
                JobQueueMigrationConflict,
                "still contains active jobs",
            ):
                migrate_terminal_job_history(source, target)


if __name__ == "__main__":
    unittest.main()
