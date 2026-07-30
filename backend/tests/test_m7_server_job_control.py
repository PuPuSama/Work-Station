from __future__ import annotations

import os
import sys
import unittest
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import sqlalchemy as sa
from fastapi.testclient import TestClient


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from knowledge_agent.database import create_knowledge_engine  # noqa: E402
from knowledge_agent.schema import projects  # noqa: E402
from server_schema import (  # noqa: E402
    article_tasks,
    background_jobs,
    job_batches,
    organizations,
    project_memberships,
    project_ownership,
    task_store_state,
    workspace_users,
)
from services.access_control import (  # noqa: E402
    ActorIdentity,
    PostgresProjectAccessRepository,
    ProjectAccessDenied,
    ProjectAccessService,
)
from services.actor_sessions import PostgresActorSessionRepository  # noqa: E402
from services.postgres_job_queue import PostgresJobQueue  # noqa: E402
from services.postgres_task_repository import (  # noqa: E402
    PostgresTaskRepository,
)
from services.server_auth import (  # noqa: E402
    SERVER_AUTH_COOKIE_NAME,
    ServerActorSessionCodec,
)
from services.server_job_control import (  # noqa: E402
    PostgresServerJobControlService,
    ServerJobControlConflict,
    ServerJobControlUnavailable,
)
from services.server_request_security import (  # noqa: E402
    ServerRequestSecurity,
    server_http_route_available,
)


PRIVATE_REQUEST_VALUE = "private-category-url-and-worker-input"
PRIVATE_ERROR_VALUE = "private-provider-error-with-secret"


class RecordingAuditWriter:
    def __init__(self) -> None:
        self.events = []

    def append(self, connection, event) -> None:
        if not connection.in_transaction():
            raise AssertionError("audit must share the Job transaction")
        self.events.append(event)


class FailingAuditWriter:
    def append(self, connection, event) -> None:
        del connection, event
        raise RuntimeError("private-audit-failure")


@unittest.skipUnless(
    os.environ.get("ARTICLE_AGENT_DATABASE_URL"),
    "ARTICLE_AGENT_DATABASE_URL is required for PostgreSQL integration tests",
)
class ServerJobControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = create_knowledge_engine(
            os.environ["ARTICLE_AGENT_DATABASE_URL"]
        )
        cls.codec = ServerActorSessionCodec(b"j" * 32)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    def setUp(self) -> None:
        prefix = f"m7-jobctl-{uuid.uuid4().hex}"
        self.org_a = f"{prefix}-org-a"
        self.org_b = f"{prefix}-org-b"
        self.project_a = f"{prefix}-project-a"
        self.project_b = f"{prefix}-project-b"
        self.project_other_org = f"{prefix}-project-other-org"
        self.admin_a = f"{prefix}-admin-a"
        self.viewer_a = f"{prefix}-viewer-a"
        self.editor_a = f"{prefix}-editor-a"
        self.admin_b = f"{prefix}-admin-b"
        self.task_ids = tuple(f"{prefix}-task-{index}" for index in range(1, 8))
        with self.engine.begin() as connection:
            connection.execute(
                projects.insert(),
                (
                    {
                        "project_id": self.project_a,
                        "customer_name": "Project A",
                        "official_domain": "a.example.test",
                    },
                    {
                        "project_id": self.project_b,
                        "customer_name": "Project B",
                        "official_domain": "b.example.test",
                    },
                    {
                        "project_id": self.project_other_org,
                        "customer_name": "Other Project",
                        "official_domain": "other.example.test",
                    },
                ),
            )
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
                        "user_id": self.admin_a,
                        "display_name": "Admin A",
                        "organization_role": "org_admin",
                    },
                    {
                        "organization_id": self.org_a,
                        "user_id": self.viewer_a,
                        "display_name": "Viewer A",
                        "organization_role": "member",
                    },
                    {
                        "organization_id": self.org_a,
                        "user_id": self.editor_a,
                        "display_name": "Editor A",
                        "organization_role": "member",
                    },
                    {
                        "organization_id": self.org_b,
                        "user_id": self.admin_b,
                        "display_name": "Admin B",
                        "organization_role": "org_admin",
                    },
                ),
            )
            connection.execute(
                project_ownership.insert(),
                (
                    {
                        "organization_id": self.org_a,
                        "project_id": self.project_a,
                        "owning_team_id": None,
                    },
                    {
                        "organization_id": self.org_a,
                        "project_id": self.project_b,
                        "owning_team_id": None,
                    },
                    {
                        "organization_id": self.org_b,
                        "project_id": self.project_other_org,
                        "owning_team_id": None,
                    },
                ),
            )
            connection.execute(
                project_memberships.insert(),
                (
                    {
                        "organization_id": self.org_a,
                        "project_id": self.project_a,
                        "user_id": self.viewer_a,
                        "role": "viewer",
                        "granted_by_user_id": self.admin_a,
                    },
                    {
                        "organization_id": self.org_a,
                        "project_id": self.project_a,
                        "user_id": self.editor_a,
                        "role": "editor",
                        "granted_by_user_id": self.admin_a,
                    },
                ),
            )
        self.repository_a = PostgresTaskRepository(
            self.engine,
            organization_id=self.org_a,
            project_id=self.project_a,
        )
        self.repository_b = PostgresTaskRepository(
            self.engine,
            organization_id=self.org_a,
            project_id=self.project_b,
        )
        self.repository_other = PostgresTaskRepository(
            self.engine,
            organization_id=self.org_b,
            project_id=self.project_other_org,
        )
        for index, task_id in enumerate(self.task_ids, start=1):
            self.repository_a.upsert(self._task(task_id, index))
        self.repository_b.upsert(self._task(f"{prefix}-task-b", 20))
        self.repository_other.upsert(self._task(f"{prefix}-task-other", 30))
        self.queue_a = PostgresJobQueue(
            self.engine,
            organization_id=self.org_a,
            project_id=self.project_a,
        )
        self.queue_b = PostgresJobQueue(
            self.engine,
            organization_id=self.org_a,
            project_id=self.project_b,
        )
        self.queue_other = PostgresJobQueue(
            self.engine,
            organization_id=self.org_b,
            project_id=self.project_other_org,
        )

    def tearDown(self) -> None:
        project_ids = (
            self.project_a,
            self.project_b,
            self.project_other_org,
        )
        with self.engine.begin() as connection:
            connection.execute(
                background_jobs.delete().where(
                    background_jobs.c.organization_id.in_(
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
                job_batches.delete().where(
                    job_batches.c.organization_id.in_(
                        (self.org_a, self.org_b)
                    )
                )
            )
            connection.execute(
                article_tasks.delete().where(
                    article_tasks.c.organization_id.in_(
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
                    project_ownership.c.project_id.in_(project_ids)
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
                organizations.delete().where(
                    organizations.c.organization_id.in_(
                        (self.org_a, self.org_b)
                    )
                )
            )
            connection.execute(
                projects.delete().where(
                    projects.c.project_id.in_(project_ids)
                )
            )

    @staticmethod
    def _task(task_id: str, topic_index: int) -> dict[str, object]:
        return {
            "id": task_id,
            "customer": "example.test",
            "topic_index": topic_index,
            "topic": f"Topic {topic_index}",
            "revision": 2,
            "updated_at": "2026-07-31T08:00:00+00:00",
        }

    def _create_job(
        self,
        queue: PostgresJobQueue,
        task_id: str,
        *,
        operation: str = "product_rediscovery",
        requester: str | None = None,
    ) -> dict[str, object]:
        return queue.create_batch(
            operation,
            [
                {
                    "task_id": task_id,
                    "customer": "example.test",
                    "topic_index": 1,
                    "source_revision": 2,
                    "request": {
                        "category_url": PRIVATE_REQUEST_VALUE,
                        "max_products": 12,
                    },
                }
            ],
            requested_by_user_id=requester or self.admin_a,
        )

    def _service(self, *, audit=None) -> PostgresServerJobControlService:
        return PostgresServerJobControlService(
            self.engine,
            audit=audit or RecordingAuditWriter(),
        )

    def _security(self) -> ServerRequestSecurity:
        return ServerRequestSecurity(
            codec=self.codec,
            access=ProjectAccessService(
                PostgresProjectAccessRepository(self.engine)
            ),
            sessions=PostgresActorSessionRepository(self.engine),
        )

    def _token(self, organization_id: str, user_id: str) -> str:
        return self.codec.create(
            ActorIdentity(organization_id, user_id),
            session_version=1,
        )

    def test_public_projection_is_scoped_and_omits_private_fields(self) -> None:
        batch_a = self._create_job(self.queue_a, self.task_ids[0])
        self._create_job(
            self.queue_b,
            next(iter(self.repository_b.load_all()))["id"],
        )
        self._create_job(
            self.queue_other,
            next(iter(self.repository_other.load_all()))["id"],
            requester=self.admin_b,
        )
        self._create_job(
            self.queue_a,
            self.task_ids[1],
            operation="titles",
        )
        service = self._service()

        page = service.list_batches(
            actor=ActorIdentity(self.org_a, self.viewer_a),
            project_id=self.project_a,
        )

        self.assertEqual(len(page.items), 1)
        self.assertEqual(page.items[0].batch_id, batch_a["id"])
        serialized = str(asdict(page))
        self.assertNotIn(PRIVATE_REQUEST_VALUE, serialized)
        self.assertNotIn("requested_by_user_id", serialized)
        self.assertNotIn("category_url", serialized)
        self.assertNotIn("'error':", serialized)

        with self.assertRaises(KeyError):
            service.get_batch(
                actor=ActorIdentity(self.org_a, self.admin_a),
                project_id=self.project_a,
                batch_id=str(
                    self.queue_b.list_batches(limit=1)[0]["id"]
                ),
            )
        with self.assertRaises(ProjectAccessDenied):
            service.list_batches(
                actor=ActorIdentity(self.org_b, self.admin_b),
                project_id=self.project_a,
            )

    def test_keyset_pagination_is_stable_and_operation_bounded(self) -> None:
        created = [
            self._create_job(self.queue_a, task_id)["id"]
            for task_id in self.task_ids[:3]
        ]
        service = self._service()
        actor = ActorIdentity(self.org_a, self.viewer_a)

        first = service.list_batches(
            actor=actor,
            project_id=self.project_a,
            limit=2,
        )
        self.assertEqual(len(first.items), 2)
        self.assertIsNotNone(first.next_after_batch_id)
        second = service.list_batches(
            actor=actor,
            project_id=self.project_a,
            limit=2,
            after_batch_id=first.next_after_batch_id,
        )
        seen = {item.batch_id for item in (*first.items, *second.items)}
        self.assertEqual(seen, set(created))
        self.assertIsNone(second.next_after_batch_id)

    def test_cancel_and_retry_reauthorize_and_preserve_private_command(self) -> None:
        audit = RecordingAuditWriter()
        service = self._service(audit=audit)
        batch = self._create_job(self.queue_a, self.task_ids[0])
        job_id = str(batch["jobs"][0]["id"])
        viewer = ActorIdentity(self.org_a, self.viewer_a)
        editor = ActorIdentity(self.org_a, self.editor_a)

        with self.assertRaises(ProjectAccessDenied):
            service.cancel_job(
                actor=viewer,
                project_id=self.project_a,
                job_id=job_id,
            )
        cancelled = service.cancel_job(
            actor=editor,
            project_id=self.project_a,
            job_id=job_id,
        )
        self.assertEqual(cancelled.status, "cancelled")
        self.assertTrue(cancelled.cancel_requested)
        retried = service.retry_job(
            actor=editor,
            project_id=self.project_a,
            job_id=job_id,
        )
        self.assertEqual(retried.status, "queued")
        self.assertFalse(retried.cancel_requested)
        internal = self.queue_a.get_job(job_id)
        self.assertEqual(
            internal["request"]["category_url"],
            PRIVATE_REQUEST_VALUE,
        )
        self.assertEqual(internal["source_revision"], 2)
        self.assertEqual(
            [event.action for event in audit.events],
            [
                "background_job.terminal",
                "background_job.cancel_requested",
                "background_job.retried",
            ],
        )
        self.assertNotIn(
            PRIVATE_REQUEST_VALUE,
            str([event.details for event in audit.events]),
        )
        with self.engine.begin() as connection:
            connection.execute(
                project_memberships.delete().where(
                    project_memberships.c.organization_id == self.org_a,
                    project_memberships.c.project_id == self.project_a,
                    project_memberships.c.user_id == self.editor_a,
                )
            )
        with self.assertRaises(ProjectAccessDenied):
            service.cancel_job(
                actor=editor,
                project_id=self.project_a,
                job_id=job_id,
            )
        self.assertEqual(self.queue_a.get_job(job_id)["status"], "queued")

    def test_retry_conflict_does_not_change_active_job(self) -> None:
        batch = self._create_job(self.queue_a, self.task_ids[0])
        job_id = str(batch["jobs"][0]["id"])

        with self.assertRaises(ServerJobControlConflict):
            self._service().retry_job(
                actor=ActorIdentity(self.org_a, self.editor_a),
                project_id=self.project_a,
                job_id=job_id,
            )

        self.assertEqual(self.queue_a.get_job(job_id)["status"], "queued")

    def test_batch_cancel_handles_all_jobs_and_audits_actor_command(self) -> None:
        audit = RecordingAuditWriter()
        batch = self.queue_a.create_batch(
            "product_rediscovery",
            [
                {
                    "task_id": task_id,
                    "source_revision": 2,
                    "request": {"category_url": PRIVATE_REQUEST_VALUE},
                }
                for task_id in self.task_ids[:2]
            ],
            requested_by_user_id=self.admin_a,
        )

        result = self._service(audit=audit).cancel_batch(
            actor=ActorIdentity(self.org_a, self.editor_a),
            project_id=self.project_a,
            batch_id=str(batch["id"]),
        )

        self.assertEqual(result.status, "cancelled")
        self.assertEqual(result.status_counts, {"cancelled": 2})
        self.assertEqual(
            audit.events[-1].action,
            "background_batch.cancel_requested",
        )
        self.assertEqual(
            audit.events[-1].details["affected_job_count"],
            2,
        )
        repeated = self._service(audit=audit).cancel_batch(
            actor=ActorIdentity(self.org_a, self.editor_a),
            project_id=self.project_a,
            batch_id=str(batch["id"]),
        )
        self.assertEqual(repeated.status, "cancelled")
        self.assertEqual(
            audit.events[-1].details["affected_job_count"],
            0,
        )

    def test_audit_failure_rolls_back_cancel_without_leaking_cause(self) -> None:
        batch = self._create_job(self.queue_a, self.task_ids[0])
        job_id = str(batch["jobs"][0]["id"])

        with self.assertRaises(ServerJobControlUnavailable) as raised:
            self._service(audit=FailingAuditWriter()).cancel_job(
                actor=ActorIdentity(self.org_a, self.editor_a),
                project_id=self.project_a,
                job_id=job_id,
            )

        self.assertNotIn("private-audit-failure", str(raised.exception))
        job = self.queue_a.get_job(job_id)
        self.assertEqual(job["status"], "queued")
        self.assertFalse(job["cancel_requested"])

    def test_http_routes_authenticate_filter_and_do_not_accept_overrides(
        self,
    ) -> None:
        import app as app_module

        batch = self._create_job(self.queue_a, self.task_ids[0])
        job_id = str(batch["jobs"][0]["id"])
        previous_mode = getattr(
            app_module.app.state,
            "server_mode_enabled",
            None,
        )
        previous_security = getattr(
            app_module.app.state,
            "server_request_security",
            None,
        )
        previous_control = getattr(
            app_module.app.state,
            "server_job_control",
            None,
        )
        app_module.app.state.server_mode_enabled = True
        app_module.app.state.server_request_security = self._security()
        app_module.app.state.server_job_control = self._service()
        client = TestClient(app_module.app)
        batches_path = f"/api/projects/{self.project_a}/batches"
        try:
            self.assertEqual(client.get(batches_path).status_code, 401)
            client.cookies.set(
                SERVER_AUTH_COOKIE_NAME,
                self._token(self.org_a, self.viewer_a),
            )
            listed = client.get(batches_path)
            self.assertEqual(listed.status_code, 200, listed.text)
            response_text = listed.text
            self.assertNotIn(PRIVATE_REQUEST_VALUE, response_text)
            self.assertNotIn("requested_by_user_id", response_text)

            forbidden = client.post(
                f"/api/projects/{self.project_a}/jobs/{job_id}/cancel"
            )
            self.assertEqual(forbidden.status_code, 403)
            client.cookies.set(
                SERVER_AUTH_COOKIE_NAME,
                self._token(self.org_a, self.editor_a),
            )
            cancelled = client.post(
                f"/api/projects/{self.project_a}/jobs/{job_id}/cancel"
            )
            self.assertEqual(cancelled.status_code, 200, cancelled.text)
            rejected_override = client.post(
                f"/api/projects/{self.project_a}/jobs/{job_id}/retry",
                json={
                    "source_revision": 999,
                    "request": {"category_url": "attacker-value"},
                },
            )
            self.assertEqual(rejected_override.status_code, 422)
            retried = client.post(
                f"/api/projects/{self.project_a}/jobs/{job_id}/retry"
            )
            self.assertEqual(retried.status_code, 200, retried.text)
            internal = self.queue_a.get_job(job_id)
            self.assertEqual(internal["source_revision"], 2)
            self.assertEqual(
                internal["request"]["category_url"],
                PRIVATE_REQUEST_VALUE,
            )
        finally:
            app_module.app.state.server_mode_enabled = previous_mode
            app_module.app.state.server_request_security = previous_security
            app_module.app.state.server_job_control = previous_control

    def test_http_failed_job_exposes_boolean_not_private_error(self) -> None:
        import app as app_module

        batch = self._create_job(self.queue_a, self.task_ids[0])
        job_id = str(batch["jobs"][0]["id"])
        with self.engine.begin() as connection:
            connection.execute(
                background_jobs.update()
                .where(
                    background_jobs.c.organization_id == self.org_a,
                    background_jobs.c.project_id == self.project_a,
                    background_jobs.c.job_id == job_id,
                )
                .values(
                    status="failed",
                    error=PRIVATE_ERROR_VALUE,
                    finished_at=datetime.now(timezone.utc),
                )
            )
        previous_mode = getattr(
            app_module.app.state,
            "server_mode_enabled",
            None,
        )
        previous_security = getattr(
            app_module.app.state,
            "server_request_security",
            None,
        )
        previous_control = getattr(
            app_module.app.state,
            "server_job_control",
            None,
        )
        app_module.app.state.server_mode_enabled = True
        app_module.app.state.server_request_security = self._security()
        app_module.app.state.server_job_control = self._service()
        client = TestClient(app_module.app)
        client.cookies.set(
            SERVER_AUTH_COOKIE_NAME,
            self._token(self.org_a, self.viewer_a),
        )
        try:
            response = client.get(
                f"/api/projects/{self.project_a}/batches/{batch['id']}"
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()["jobs"][0]["has_error"])
            self.assertNotIn(PRIVATE_ERROR_VALUE, response.text)
        finally:
            app_module.app.state.server_mode_enabled = previous_mode
            app_module.app.state.server_request_security = previous_security
            app_module.app.state.server_job_control = previous_control

    def test_route_allowlist_is_exact_and_legacy_routes_stay_closed(self) -> None:
        base = f"/api/projects/{self.project_a}"
        self.assertTrue(server_http_route_available("GET", f"{base}/batches"))
        self.assertTrue(
            server_http_route_available("GET", f"{base}/batches/batch-id")
        )
        self.assertTrue(
            server_http_route_available(
                "POST",
                f"{base}/batches/batch-id/cancel",
            )
        )
        self.assertTrue(
            server_http_route_available(
                "POST",
                f"{base}/jobs/job-id/cancel",
            )
        )
        self.assertTrue(
            server_http_route_available(
                "POST",
                f"{base}/jobs/job-id/retry",
            )
        )
        self.assertFalse(server_http_route_available("GET", "/api/batches"))
        self.assertFalse(
            server_http_route_available(
                "GET",
                f"{base}/jobs/job-id",
            )
        )
        self.assertFalse(
            server_http_route_available(
                "POST",
                f"{base}/jobs/job-id/run",
            )
        )
        self.assertFalse(
            server_http_route_available(
                "DELETE",
                f"{base}/batches/batch-id",
            )
        )


if __name__ == "__main__":
    unittest.main()
