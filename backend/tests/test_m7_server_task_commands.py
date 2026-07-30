from __future__ import annotations

import os
import sys
import unittest
import uuid
from pathlib import Path

import sqlalchemy as sa


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from knowledge_agent.database import create_knowledge_engine  # noqa: E402
from knowledge_agent.schema import projects  # noqa: E402
from models import TaskRecord  # noqa: E402
from server_schema import (  # noqa: E402
    article_tasks,
    audit_events,
    organizations,
    project_memberships,
    project_ownership,
    task_store_state,
    workspace_users,
)
from services.access_control import (  # noqa: E402
    ActorIdentity,
    ProjectAccessDenied,
)
from services.postgres_task_repository import (  # noqa: E402
    PostgresTaskRepository,
)
from services.server_task_commands import (  # noqa: E402
    PostgresAuditedTaskWriter,
    ServerTaskCommandUnavailable,
)
from storage import RevisionConflictError  # noqa: E402


class FailingAuditWriter:
    def append(self, connection, event) -> None:
        del connection, event
        raise RuntimeError("injected audit failure")


@unittest.skipUnless(
    os.environ.get("ARTICLE_AGENT_DATABASE_URL"),
    "ARTICLE_AGENT_DATABASE_URL is required for PostgreSQL integration tests",
)
class ServerTaskCommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = create_knowledge_engine(
            os.environ["ARTICLE_AGENT_DATABASE_URL"]
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    def setUp(self) -> None:
        prefix = f"m7-task-command-{uuid.uuid4().hex}"
        self.organization_id = f"{prefix}-org"
        self.user_id = f"{prefix}-editor"
        self.project_id = f"{prefix}.example.test"
        self.task_id = f"{prefix}-task"
        with self.engine.begin() as connection:
            connection.execute(
                organizations.insert().values(
                    organization_id=self.organization_id,
                    name="Task Command Org",
                )
            )
            connection.execute(
                workspace_users.insert().values(
                    organization_id=self.organization_id,
                    user_id=self.user_id,
                    display_name="Task Editor",
                )
            )
            connection.execute(
                projects.insert().values(
                    project_id=self.project_id,
                    customer_name="Task Command Project",
                    official_domain=self.project_id,
                )
            )
            connection.execute(
                project_ownership.insert().values(
                    organization_id=self.organization_id,
                    project_id=self.project_id,
                )
            )
            connection.execute(
                project_memberships.insert().values(
                    organization_id=self.organization_id,
                    project_id=self.project_id,
                    user_id=self.user_id,
                    role="editor",
                    granted_by_user_id=self.user_id,
                )
            )
        self.repository = PostgresTaskRepository(
            self.engine,
            organization_id=self.organization_id,
            project_id=self.project_id,
        )
        self.repository.upsert(
            TaskRecord(
                id=self.task_id,
                week_folder="server",
                customer=self.project_id,
                topic_index=1,
                topic="Audited task",
                status="title_selected",
                task_dir=f"/server/{self.task_id}",
                selected_title="Audited task",
                created_at="2026-07-31T00:00:00+00:00",
                updated_at="2026-07-31T00:00:00+00:00",
            ).model_dump(mode="json")
        )
        self.actor = ActorIdentity(
            self.organization_id,
            self.user_id,
        )

    def tearDown(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                article_tasks.delete().where(
                    article_tasks.c.organization_id
                    == self.organization_id
                )
            )
            connection.execute(
                task_store_state.delete().where(
                    task_store_state.c.organization_id
                    == self.organization_id
                )
            )
            connection.execute(
                project_memberships.delete().where(
                    project_memberships.c.organization_id
                    == self.organization_id
                )
            )
            connection.execute(
                project_ownership.delete().where(
                    project_ownership.c.organization_id
                    == self.organization_id
                )
            )
            connection.execute(
                workspace_users.delete().where(
                    workspace_users.c.organization_id
                    == self.organization_id
                )
            )
            connection.execute(
                projects.delete().where(
                    projects.c.project_id == self.project_id
                )
            )
            connection.execute(
                organizations.delete().where(
                    organizations.c.organization_id
                    == self.organization_id
                )
            )

    def _task(self) -> TaskRecord:
        record = self.repository.get(self.task_id)
        assert record is not None
        return TaskRecord.model_validate(record)

    def _audit_count(self) -> int:
        with self.engine.connect() as connection:
            return int(
                connection.execute(
                    sa.select(sa.func.count())
                    .select_from(audit_events)
                    .where(
                        audit_events.c.organization_id
                        == self.organization_id
                    )
                ).scalar_one()
            )

    def test_task_cas_and_audit_share_one_transaction(self) -> None:
        task = self._task()
        task.selected_title = "Rewritten title"
        writer = PostgresAuditedTaskWriter(
            self.engine,
            organization_id=self.organization_id,
            project_id=self.project_id,
        )
        connection = self.engine.connect()
        transaction = connection.begin()
        try:
            saved = writer.put_in_transaction(
                connection,
                task,
                expected_revision=0,
                actor=self.actor,
                action="article.task.rewritten",
            )
            stored = connection.execute(
                sa.select(
                    article_tasks.c.revision,
                    article_tasks.c.payload,
                ).where(
                    article_tasks.c.organization_id
                    == self.organization_id,
                    article_tasks.c.project_id == self.project_id,
                    article_tasks.c.task_id == self.task_id,
                )
            ).one()
            event = connection.execute(
                sa.select(audit_events).where(
                    audit_events.c.organization_id
                    == self.organization_id
                )
            ).mappings().one()

            self.assertEqual(saved.revision, 1)
            self.assertEqual(stored.revision, 1)
            self.assertEqual(
                stored.payload["selected_title"],
                "Rewritten title",
            )
            self.assertEqual(
                event["action"],
                "article.task.rewritten",
            )
            self.assertEqual(event["target_id"], self.task_id)
            self.assertEqual(
                event["details"],
                {
                    "from_revision": 0,
                    "to_revision": 1,
                    "status": "title_selected",
                },
            )
            self.assertNotIn("selected_title", event["details"])
        finally:
            transaction.rollback()
            connection.close()

        self.assertEqual(self.repository.get(self.task_id)["revision"], 0)
        self.assertEqual(self._audit_count(), 0)

    def test_audit_failure_rolls_back_task_update(self) -> None:
        task = self._task()
        task.selected_title = "Must roll back"
        task.schema_version = 1
        writer = PostgresAuditedTaskWriter(
            self.engine,
            organization_id=self.organization_id,
            project_id=self.project_id,
            audit=FailingAuditWriter(),
        )

        with self.assertRaisesRegex(
            ServerTaskCommandUnavailable,
            "temporarily unavailable",
        ):
            writer.put(
                task,
                expected_revision=0,
                actor=self.actor,
                action="article.task.rewritten",
            )

        stored = self.repository.get(self.task_id)
        assert stored is not None
        self.assertEqual(stored["revision"], 0)
        self.assertEqual(stored["selected_title"], "Audited task")
        self.assertEqual(task.revision, 0)
        self.assertEqual(task.schema_version, 1)
        self.assertEqual(self._audit_count(), 0)

    def test_audit_detail_allowlist_rejects_article_content(self) -> None:
        task = self._task()
        writer = PostgresAuditedTaskWriter(
            self.engine,
            organization_id=self.organization_id,
            project_id=self.project_id,
        )

        with self.assertRaisesRegex(
            ValueError,
            "unsupported keys",
        ):
            writer.put(
                task,
                expected_revision=0,
                actor=self.actor,
                action="article.task.rewritten",
                details={"article_body": "must never enter audit"},
            )

        self.assertEqual(task.revision, 0)
        self.assertEqual(self.repository.get(self.task_id)["revision"], 0)
        self.assertEqual(self._audit_count(), 0)

    def test_revoked_permission_and_stale_revision_write_no_audit(
        self,
    ) -> None:
        writer = PostgresAuditedTaskWriter(
            self.engine,
            organization_id=self.organization_id,
            project_id=self.project_id,
        )
        with self.engine.begin() as connection:
            connection.execute(
                project_memberships.update()
                .where(
                    project_memberships.c.organization_id
                    == self.organization_id,
                    project_memberships.c.project_id == self.project_id,
                    project_memberships.c.user_id == self.user_id,
                )
                .values(role="viewer")
            )
        with self.assertRaisesRegex(
            ProjectAccessDenied,
            "^project access denied$",
        ):
            writer.put(
                self._task(),
                expected_revision=0,
                actor=self.actor,
                action="article.task.rewritten",
            )

        with self.engine.begin() as connection:
            connection.execute(
                project_memberships.update()
                .where(
                    project_memberships.c.organization_id
                    == self.organization_id,
                    project_memberships.c.project_id == self.project_id,
                    project_memberships.c.user_id == self.user_id,
                )
                .values(role="editor")
            )
        stale = self._task()
        concurrent = self.repository.get(self.task_id)
        assert concurrent is not None
        concurrent["revision"] = 1
        self.repository.upsert(concurrent)
        with self.assertRaises(RevisionConflictError):
            writer.put(
                stale,
                expected_revision=0,
                actor=self.actor,
                action="article.task.rewritten",
            )

        self.assertEqual(self.repository.get(self.task_id)["revision"], 1)
        self.assertEqual(self._audit_count(), 0)
