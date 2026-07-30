from __future__ import annotations

import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services.access_control import (  # noqa: E402
    ActorIdentity,
    ProjectAccessFacts,
    ProjectAccessService,
)
from services.server_auth import (  # noqa: E402
    SERVER_AUTH_COOKIE_NAME,
    ServerActorSessionCodec,
)
from services.server_request_security import (  # noqa: E402
    ServerRequestForbidden,
    ServerRequestSecurity,
    ServerRequestUnauthenticated,
    knowledge_permission_for,
    server_http_route_available,
    server_knowledge_route_ready,
)


class FakeAccessRepository:
    def __init__(self, facts):
        self.facts = facts
        self.calls = []

    def resolve_project_access(self, actor, project_id):
        self.calls.append((actor, project_id))
        return self.facts


class FakeSessionVersions:
    def __init__(self, current: bool = True):
        self.current = current
        self.calls = []

    def is_current(self, session):
        self.calls.append(session)
        return self.current


class ServerRequestSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.codec = ServerActorSessionCodec(b"s" * 32)
        self.actor = ActorIdentity("org-a", "editor-a")

    def test_valid_session_normalizes_project_and_uses_database_facts(self) -> None:
        repository = FakeAccessRepository(
            ProjectAccessFacts(
                organization_role="member",
                project_role="editor",
            )
        )
        security = ServerRequestSecurity(
            codec=self.codec,
            access=ProjectAccessService(repository),
            sessions=FakeSessionVersions(),
        )
        authorized = security.authorize_project(
            token=self.codec.create(self.actor),
            project="https://WWW.Example.COM/",
            permission="knowledge.edit",
        )

        self.assertEqual(authorized.actor, self.actor)
        self.assertEqual(authorized.project_id, "example.com")
        self.assertEqual(
            repository.calls,
            [(self.actor, "example.com")],
        )

    def test_invalid_session_and_denial_are_generic(self) -> None:
        repository = FakeAccessRepository(None)
        security = ServerRequestSecurity(
            codec=self.codec,
            access=ProjectAccessService(repository),
            sessions=FakeSessionVersions(),
        )
        with self.assertRaisesRegex(
            ServerRequestUnauthenticated,
            "^authentication required$",
        ):
            security.authorize_project(
                token="tampered",
                project="example.com",
                permission="project.view",
            )
        with self.assertRaisesRegex(
            ServerRequestForbidden,
            "^project access denied$",
        ):
            security.authorize_project(
                token=self.codec.create(self.actor),
                project="other.example",
                permission="project.view",
            )

    def test_stale_session_is_rejected_before_project_access(self) -> None:
        repository = FakeAccessRepository(
            ProjectAccessFacts(
                organization_role="member",
                project_role="viewer",
            )
        )
        versions = FakeSessionVersions(current=False)
        security = ServerRequestSecurity(
            codec=self.codec,
            access=ProjectAccessService(repository),
            sessions=versions,
        )

        with self.assertRaisesRegex(
            ServerRequestUnauthenticated,
            "^authentication required$",
        ):
            security.authorize_project(
                token=self.codec.create(self.actor),
                project="example.com",
                permission="project.view",
            )

        self.assertEqual(repository.calls, [])
        self.assertEqual(len(versions.calls), 1)

    def test_knowledge_routes_have_explicit_conservative_permissions(self) -> None:
        cases = {
            ("GET", "/api/knowledge/{project}"): "project.view",
            (
                "POST",
                "/api/knowledge/{project}/research-assistant/messages",
            ): "project.view",
            (
                "POST",
                "/api/knowledge/{project}/x/evidence-packs",
            ): "project.view",
            (
                "POST",
                "/api/knowledge/{project}/articles/x/knowledge-coverage",
            ): "project.view",
            (
                "POST",
                "/api/knowledge/{project}/sources/x/publish",
            ): "knowledge.publish",
            (
                "POST",
                "/api/knowledge/{project}/products/x/confirm",
            ): "knowledge.publish",
            (
                "POST",
                "/api/knowledge/{project}/sources/upload",
            ): "knowledge.edit",
            (
                "DELETE",
                "/api/knowledge/{project}/sources/x",
            ): "knowledge.delete",
        }
        for (method, path), expected in cases.items():
            with self.subTest(method=method, path=path):
                self.assertEqual(
                    knowledge_permission_for(method, path),
                    expected,
                )

    def test_unmigrated_server_routes_fail_closed(self) -> None:
        blocked_knowledge_routes = (
            ("POST", "/api/knowledge/{project}/wordpress/sync"),
            ("POST", "/api/knowledge/{project}/sources/upload"),
            ("POST", "/api/knowledge/{project}/research-runs"),
            (
                "POST",
                "/api/knowledge/{project}/research-runs/{thread_id}/resume",
            ),
            (
                "GET",
                "/api/knowledge/{project}/sources/{source_id}/"
                "snapshots/{snapshot_id}/raw",
            ),
            (
                "POST",
                "/api/knowledge/{project}/tasks/{task_id}/retrieval-plan",
            ),
        )
        for method, path in blocked_knowledge_routes:
            with self.subTest(method=method, path=path):
                self.assertFalse(
                    server_knowledge_route_ready(method, path)
                )
        self.assertTrue(
            server_knowledge_route_ready(
                "GET",
                "/api/knowledge/{project}",
            )
        )
        self.assertTrue(
            server_http_route_available(
                "GET",
                "/api/knowledge/example.com",
            )
        )
        self.assertTrue(
            server_http_route_available(
                "POST",
                "/api/projects/example.com/tasks/task-a/"
                "product-rediscovery",
            )
        )
        self.assertTrue(
            server_http_route_available(
                "GET",
                "/api/projects/example.com/tasks/task-a/"
                "product-rediscovery/jobs/job-a",
            )
        )
        self.assertFalse(
            server_http_route_available(
                "PUT",
                "/api/projects/example.com/tasks/task-a/"
                "product-rediscovery",
            )
        )
        self.assertTrue(
            server_http_route_available(
                "GET",
                "/api/auth/oidc/start",
            )
        )
        self.assertTrue(
            server_http_route_available(
                "GET",
                "/api/auth/oidc/callback",
            )
        )
        self.assertTrue(
            server_http_route_available(
                "POST",
                "/api/projects/project-a/tasks/task-a/prepare-images",
            )
        )
        self.assertTrue(
            server_http_route_available(
                "POST",
                "/api/projects/project-a/tasks/task-a/checks/"
                "final-ai/screenshot",
            )
        )
        self.assertTrue(
            server_http_route_available(
                "PUT",
                "/api/projects/project-a/tasks/task-a/checks/final-ai",
            )
        )
        self.assertTrue(
            server_http_route_available(
                "GET",
                "/api/projects/project-a/tasks/task-a/checks/"
                "final-ai/screenshot/download",
            )
        )
        self.assertFalse(
            server_http_route_available(
                "POST",
                "/api/projects/project-a/tasks/task-a/checks/"
                "final-ai/screenshot/download",
            )
        )
        self.assertTrue(
            server_http_route_available(
                "POST",
                "/api/projects/project-a/tasks/task-a/export-docx",
            )
        )
        self.assertTrue(
            server_http_route_available(
                "GET",
                "/api/projects/project-a/tasks/task-a/docx/download",
            )
        )
        self.assertFalse(
            server_http_route_available(
                "POST",
                "/api/projects/project-a/tasks/task-a/docx/download",
            )
        )
        self.assertTrue(
            server_http_route_available(
                "POST",
                "/api/projects/project-a/tasks/task-a/generate-tdk",
            )
        )
        self.assertTrue(
            server_http_route_available(
                "POST",
                "/api/projects/project-a/tasks/task-a/package-delivery",
            )
        )
        self.assertTrue(
            server_http_route_available(
                "GET",
                "/api/projects/project-a/tasks/task-a/"
                "delivery-package/download",
            )
        )
        self.assertTrue(
            server_http_route_available(
                "GET",
                "/api/projects/project-a/tasks/task-a/tdk/download",
            )
        )
        self.assertFalse(
            server_http_route_available(
                "POST",
                "/api/projects/project-a/tasks/task-a/tdk/download",
            )
        )
        self.assertFalse(
            server_http_route_available(
                "POST",
                "/api/auth/oidc/callback",
            )
        )
        self.assertTrue(
            server_http_route_available("GET", "/api/health")
        )
        self.assertTrue(
            server_http_route_available("GET", "/api/projects")
        )
        self.assertTrue(
            server_http_route_available(
                "GET",
                "/api/projects/example.com/tasks",
            )
        )
        self.assertTrue(
            server_http_route_available(
                "GET",
                "/api/projects/example.com/tasks/task-a",
            )
        )
        self.assertFalse(
            server_http_route_available(
                "POST",
                "/api/projects/example.com/tasks",
            )
        )
        self.assertFalse(
            server_http_route_available(
                "GET",
                "/api/projects/example.com/prompts",
            )
        )
        self.assertTrue(
            server_http_route_available(
                "GET",
                "/api/projects/example.com/assets/asset-a/download",
            )
        )
        self.assertFalse(
            server_http_route_available(
                "POST",
                "/api/projects/example.com/assets/asset-a/download",
            )
        )
        self.assertTrue(
            server_http_route_available(
                "POST",
                "/api/projects/example.com/tasks/task-a/"
                "rewrite-from-scratch",
            )
        )
        self.assertTrue(
            server_http_route_available(
                "PUT",
                "/api/projects/example.com/tasks/task-a/products",
            )
        )
        self.assertTrue(
            server_http_route_available(
                "PUT",
                "/api/projects/example.com/tasks/task-a/article/sections",
            )
        )
        self.assertFalse(
            server_http_route_available(
                "POST",
                "/api/projects/example.com/tasks/task-a/article/sections",
            )
        )
        self.assertFalse(
            server_http_route_available(
                "POST",
                "/api/projects/example.com/tasks/task-a/products",
            )
        )
        self.assertFalse(
            server_http_route_available(
                "PUT",
                "/api/projects/example.com/tasks/task-a/"
                "rewrite-from-scratch",
            )
        )
        self.assertFalse(
            server_http_route_available("GET", "/api/tasks")
        )

    def test_every_knowledge_route_remains_project_scoped(self) -> None:
        from knowledge_agent.http import router

        self.assertTrue(router.routes)
        for route in router.routes:
            with self.subTest(path=route.path):
                self.assertIn("{project}", route.path)

    def test_app_server_mode_blocks_legacy_and_scopes_knowledge_routes(
        self,
    ) -> None:
        import app as app_module

        repository = FakeAccessRepository(
            ProjectAccessFacts(
                organization_role="member",
                project_role="viewer",
            )
        )
        security = ServerRequestSecurity(
            codec=self.codec,
            access=ProjectAccessService(repository),
            sessions=FakeSessionVersions(),
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
        app_module.app.state.server_mode_enabled = True
        app_module.app.state.server_request_security = security
        app_module.app.state.knowledge_agent_runtime = None
        try:
            client = TestClient(app_module.app)
            self.assertEqual(client.get("/api/tasks").status_code, 503)
            self.assertEqual(
                client.get("/api/knowledge/example.com").status_code,
                401,
            )
            self.assertEqual(
                client.post(
                    "/api/knowledge/example.com/tasks/task-a/retrieval-plan"
                ).status_code,
                401,
            )
            client.cookies.set(
                SERVER_AUTH_COOKIE_NAME,
                self.codec.create(self.actor),
            )
            # Authorization passed, then the disabled runtime returns its
            # existing 404. The dependency therefore ran before business code.
            self.assertEqual(
                client.get("/api/knowledge/example.com").status_code,
                404,
            )
            self.assertEqual(
                client.post(
                    "/api/knowledge/example.com/sources/source-a/publish"
                ).status_code,
                403,
            )
            self.assertEqual(
                client.post(
                    "/api/knowledge/example.com/tasks/task-a/retrieval-plan"
                ).status_code,
                403,
            )
            repository.facts = ProjectAccessFacts(
                organization_role="org_admin"
            )
            self.assertEqual(
                client.post(
                    "/api/knowledge/example.com/research-runs",
                    json={},
                ).status_code,
                503,
            )
            self.assertEqual(
                client.post(
                    "/api/knowledge/example.com/tasks/task-a/retrieval-plan"
                ).status_code,
                503,
            )
            self.assertEqual(
                client.post(
                    "/api/auth/login",
                    json={"password": "legacy-must-not-work"},
                ).status_code,
                503,
            )
        finally:
            app_module.app.state.server_mode_enabled = previous_mode
            app_module.app.state.server_request_security = previous_security

    @unittest.skipUnless(
        os.environ.get("ARTICLE_AGENT_DATABASE_URL"),
        "ARTICLE_AGENT_DATABASE_URL is required for server lifespan wiring",
    )
    def test_real_server_lifespan_builds_security_and_blocks_legacy_api(
        self,
    ) -> None:
        import app as app_module

        base_config = app_module.config()
        with tempfile.TemporaryDirectory() as directory:
            isolated = replace(
                base_config,
                data_file=Path(directory) / "tasks.json",
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
                self.assertIsInstance(
                    app_module.app.state.server_request_security,
                    ServerRequestSecurity,
                )
                self.assertEqual(client.get("/api/health").status_code, 200)
                self.assertEqual(client.get("/api/tasks").status_code, 503)
                self.assertIsNone(app_module.app.state.job_queue)
                self.assertEqual(
                    app_module.app.state.batch_runners,
                    (),
                )
                self.assertFalse(
                    (
                        Path(directory)
                        / "job_queue.sqlite3"
                    ).exists()
                )
                with self.assertRaisesRegex(
                    RuntimeError,
                    "TaskStore is unavailable",
                ):
                    app_module.store()
                with self.assertRaisesRegex(
                    RuntimeError,
                    "JobQueue is unavailable",
                ):
                    app_module.batch_queue()
                status = client.get("/api/auth/status")
                self.assertEqual(status.status_code, 200)
                self.assertEqual(status.json()["data"]["mode"], "server")
                self.assertFalse(status.json()["data"]["authenticated"])


if __name__ == "__main__":
    unittest.main()
