from __future__ import annotations

import os
import sys
import tempfile
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import sqlalchemy as sa
from fastapi.testclient import TestClient


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from knowledge_agent.database import create_knowledge_engine  # noqa: E402
from knowledge_agent.schema import projects  # noqa: E402
from models import TaskRecord  # noqa: E402
from server_schema import (  # noqa: E402
    article_tasks,
    organizations,
    project_memberships,
    project_ownership,
    task_store_state,
    workspace_users,
)
from services.postgres_task_repository import (  # noqa: E402
    PostgresTaskRepository,
)
from services.server_auth import (  # noqa: E402
    SERVER_AUTH_COOKIE_NAME,
    ServerActorSessionCodec,
)
from services.access_control import ActorIdentity  # noqa: E402


@unittest.skipUnless(
    os.environ.get("ARTICLE_AGENT_DATABASE_URL"),
    "ARTICLE_AGENT_DATABASE_URL is required for PostgreSQL integration tests",
)
class ServerProjectTaskApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = create_knowledge_engine(
            os.environ["ARTICLE_AGENT_DATABASE_URL"]
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    def setUp(self) -> None:
        prefix = f"m7-server-tasks-{uuid.uuid4().hex}"
        self.org_a = f"{prefix}-org-a"
        self.org_b = f"{prefix}-org-b"
        self.user_a = f"{prefix}-user-a"
        self.user_b = f"{prefix}-user-b"
        self.project_a = f"{prefix}.a.example.test"
        self.project_b = f"{prefix}.b.example.test"
        self.task_a = f"{prefix}-task-a"
        self.task_b = f"{prefix}-task-b"
        with self.engine.begin() as connection:
            connection.execute(
                organizations.insert(),
                (
                    {"organization_id": self.org_a, "name": "Org A"},
                    {"organization_id": self.org_b, "name": "Org B"},
                ),
            )
            connection.execute(
                workspace_users.insert(),
                (
                    {
                        "organization_id": self.org_a,
                        "user_id": self.user_a,
                        "display_name": "User A",
                    },
                    {
                        "organization_id": self.org_b,
                        "user_id": self.user_b,
                        "display_name": "User B",
                    },
                ),
            )
            connection.execute(
                projects.insert(),
                (
                    {
                        "project_id": self.project_a,
                        "customer_name": "Project A",
                        "official_domain": self.project_a,
                    },
                    {
                        "project_id": self.project_b,
                        "customer_name": "Project B",
                        "official_domain": self.project_b,
                    },
                ),
            )
            connection.execute(
                project_ownership.insert(),
                (
                    {
                        "project_id": self.project_a,
                        "organization_id": self.org_a,
                    },
                    {
                        "project_id": self.project_b,
                        "organization_id": self.org_b,
                    },
                ),
            )
            connection.execute(
                project_memberships.insert(),
                (
                    {
                        "organization_id": self.org_a,
                        "project_id": self.project_a,
                        "user_id": self.user_a,
                        "role": "viewer",
                        "granted_by_user_id": self.user_a,
                    },
                    {
                        "organization_id": self.org_b,
                        "project_id": self.project_b,
                        "user_id": self.user_b,
                        "role": "viewer",
                        "granted_by_user_id": self.user_b,
                    },
                ),
            )
        self._task_repository(
            organization_id=self.org_a,
            project_id=self.project_a,
        ).upsert(self._task(self.task_a, self.project_a, 2))
        self._task_repository(
            organization_id=self.org_b,
            project_id=self.project_b,
        ).upsert(self._task(self.task_b, self.project_b, 1))

    def tearDown(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                article_tasks.delete().where(
                    article_tasks.c.organization_id.in_(
                        (self.org_a, self.org_b)
                    )
                )
            )
            connection.execute(
                task_store_state.delete().where(
                    task_store_state.c.organization_id.in_(
                        (self.org_a, self.org_b)
                    )
                )
            )
            connection.execute(
                project_memberships.delete().where(
                    project_memberships.c.organization_id.in_(
                        (self.org_a, self.org_b)
                    )
                )
            )
            connection.execute(
                project_ownership.delete().where(
                    project_ownership.c.project_id.in_(
                        (self.project_a, self.project_b)
                    )
                )
            )
            connection.execute(
                workspace_users.delete().where(
                    workspace_users.c.organization_id.in_(
                        (self.org_a, self.org_b)
                    )
                )
            )
            connection.execute(
                projects.delete().where(
                    projects.c.project_id.in_(
                        (self.project_a, self.project_b)
                    )
                )
            )
            connection.execute(
                organizations.delete().where(
                    organizations.c.organization_id.in_(
                        (self.org_a, self.org_b)
                    )
                )
            )

    def _task_repository(
        self,
        *,
        organization_id: str,
        project_id: str,
    ) -> PostgresTaskRepository:
        return PostgresTaskRepository(
            self.engine,
            organization_id=organization_id,
            project_id=project_id,
        )

    @staticmethod
    def _task(task_id: str, project_id: str, topic_index: int) -> dict:
        return TaskRecord(
            id=task_id,
            week_folder="server",
            customer=project_id,
            topic_index=topic_index,
            topic=f"Topic {topic_index}",
            task_dir=f"/server/{task_id}",
            created_at="2026-07-30T00:00:00+00:00",
            updated_at="2026-07-30T00:00:00+00:00",
        ).model_dump(mode="json")

    def test_server_task_reads_are_authorized_and_project_scoped(
        self,
    ) -> None:
        import app as app_module

        codec = ServerActorSessionCodec(b"z" * 32)
        actor = ActorIdentity(self.org_a, self.user_a)
        base_config = app_module.config()
        with tempfile.TemporaryDirectory() as directory:
            local_state = Path(directory) / "must-not-exist"
            isolated = replace(
                base_config,
                data_file=local_state / "tasks.json",
                knowledge_agent_enabled=False,
            )
            with (
                patch.object(app_module, "config", return_value=isolated),
                patch.dict(
                    os.environ,
                    {
                        "ARTICLE_AGENT_SERVER_MODE": "true",
                        "ARTICLE_AGENT_SERVER_SESSION_SECRET": "z" * 32,
                    },
                    clear=False,
                ),
                TestClient(app_module.app) as client,
            ):
                self.assertEqual(
                    client.get(
                        f"/api/projects/{self.project_a}/tasks"
                    ).status_code,
                    401,
                )
                client.cookies.set(
                    SERVER_AUTH_COOKIE_NAME,
                    codec.create(actor),
                )
                response = client.get(
                    f"/api/projects/{self.project_a}/tasks"
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    [item["id"] for item in response.json()],
                    [self.task_a],
                )
                self.assertEqual(
                    client.get(
                        f"/api/projects/{self.project_a}/tasks/{self.task_a}"
                    ).json()["id"],
                    self.task_a,
                )
                self.assertEqual(
                    client.get(
                        f"/api/projects/{self.project_a}/tasks/{self.task_b}"
                    ).status_code,
                    404,
                )
                self.assertEqual(
                    client.get(
                        f"/api/projects/{self.project_b}/tasks"
                    ).status_code,
                    403,
                )
                self.assertEqual(
                    client.get("/api/tasks").status_code,
                    503,
                )
                self.assertFalse(local_state.exists())

    def test_server_task_api_is_not_added_to_local_mode(self) -> None:
        import app as app_module

        previous_mode = getattr(
            app_module.app.state,
            "server_mode_enabled",
            None,
        )
        app_module.app.state.server_mode_enabled = False
        try:
            response = TestClient(app_module.app).get(
                f"/api/projects/{self.project_a}/tasks"
            )
            self.assertEqual(response.status_code, 404)
        finally:
            app_module.app.state.server_mode_enabled = previous_mode


if __name__ == "__main__":
    unittest.main()
