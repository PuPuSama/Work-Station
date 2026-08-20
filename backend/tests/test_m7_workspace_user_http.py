from __future__ import annotations

import os
import sys
import unittest
import uuid
from pathlib import Path

import sqlalchemy as sa
from fastapi.testclient import TestClient


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from knowledge_agent.database import create_knowledge_engine  # noqa: E402
from knowledge_agent.schema import projects  # noqa: E402
from server_schema import (  # noqa: E402
    external_identities,
    organizations,
    project_memberships,
    project_ownership,
    team_memberships,
    teams,
    workspace_users,
)
from services.access_control import ActorIdentity  # noqa: E402
from services.actor_sessions import PostgresActorSessionRepository  # noqa: E402
from services.server_auth import (  # noqa: E402
    SERVER_AUTH_COOKIE_NAME,
    ServerActorSessionCodec,
)
from services.server_request_security import (  # noqa: E402
    ServerRequestSecurity,
    server_http_route_available,
)
from services.workspace_users import (  # noqa: E402
    PostgresWorkspaceUserService,
)


PRIVATE_AUDIT_ERROR = "private-workspace-user-audit-body"


class RecordingAuditWriter:
    def __init__(self) -> None:
        self.events = []

    def append(self, connection, event) -> None:
        self.events.append(event)


class FailingAuditWriter:
    def append(self, connection, event) -> None:
        raise RuntimeError(PRIVATE_AUDIT_ERROR)


@unittest.skipUnless(
    os.environ.get("ARTICLE_AGENT_DATABASE_URL"),
    "ARTICLE_AGENT_DATABASE_URL is required for PostgreSQL integration tests",
)
class WorkspaceUserHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = create_knowledge_engine(
            os.environ["ARTICLE_AGENT_DATABASE_URL"]
        )
        cls.codec = ServerActorSessionCodec(b"u" * 32)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    def setUp(self) -> None:
        prefix = f"m7-workspace-user-{uuid.uuid4().hex}"
        self.org_a = f"{prefix}-org-a"
        self.org_b = f"{prefix}-org-b"
        self.admin_a = f"{prefix}-admin-a"
        self.admin2_a = f"{prefix}-admin2-a"
        self.member_a = f"{prefix}-member-a"
        self.target_a = f"{prefix}-target-a"
        self.admin_b = f"{prefix}-admin-b"
        self.team_a = f"{prefix}-team-a"
        self.project_a = f"{prefix}-project.example.test"
        with self.engine.begin() as connection:
            connection.execute(
                organizations.insert(),
                (
                    {
                        "organization_id": self.org_a,
                        "name": "Organization A",
                    },
                    {
                        "organization_id": self.org_b,
                        "name": "Organization B",
                    },
                ),
            )
            connection.execute(
                workspace_users.insert(),
                (
                    self._user(self.org_a, self.admin_a, "Admin A", "org_admin"),
                    self._user(
                        self.org_a,
                        self.admin2_a,
                        "Admin A2",
                        "org_admin",
                    ),
                    self._user(self.org_a, self.member_a, "Member A", "member"),
                    self._user(self.org_a, self.target_a, "Target A", "member"),
                    self._user(self.org_b, self.admin_b, "Admin B", "org_admin"),
                ),
            )
            connection.execute(
                teams.insert().values(
                    organization_id=self.org_a,
                    team_id=self.team_a,
                    name="Team A",
                )
            )
            connection.execute(
                team_memberships.insert().values(
                    organization_id=self.org_a,
                    team_id=self.team_a,
                    user_id=self.target_a,
                    role="member",
                    granted_by_user_id=self.admin_a,
                )
            )
            connection.execute(
                projects.insert().values(
                    project_id=self.project_a,
                    customer_name="Project A",
                    official_domain=self.project_a,
                    status="active",
                )
            )
            connection.execute(
                project_ownership.insert().values(
                    organization_id=self.org_a,
                    project_id=self.project_a,
                    owning_team_id=self.team_a,
                )
            )
            connection.execute(
                project_memberships.insert().values(
                    organization_id=self.org_a,
                    project_id=self.project_a,
                    user_id=self.target_a,
                    role="viewer",
                    granted_by_user_id=self.admin_a,
                )
            )
            connection.execute(
                external_identities.insert().values(
                    issuer="https://issuer.example.test",
                    subject=f"subject-{prefix}",
                    organization_id=self.org_a,
                    user_id=self.target_a,
                )
            )

    @staticmethod
    def _user(
        organization_id: str,
        user_id: str,
        display_name: str,
        role: str,
    ) -> dict[str, str]:
        return {
            "organization_id": organization_id,
            "user_id": user_id,
            "display_name": display_name,
            "organization_role": role,
        }

    def _token(
        self,
        organization_id: str,
        user_id: str,
        *,
        version: int = 1,
    ) -> str:
        return self.codec.create(
            ActorIdentity(organization_id, user_id),
            session_version=version,
        )

    def _security(self) -> ServerRequestSecurity:
        return ServerRequestSecurity(
            codec=self.codec,
            access=object(),  # type: ignore[arg-type]
            sessions=PostgresActorSessionRepository(self.engine),
        )

    def _client(
        self,
        service: PostgresWorkspaceUserService,
    ) -> tuple[TestClient, tuple[object, object, object]]:
        import app as app_module

        previous = (
            getattr(app_module.app.state, "server_mode_enabled", None),
            getattr(app_module.app.state, "server_request_security", None),
            getattr(app_module.app.state, "server_workspace_users", None),
        )
        app_module.app.state.server_mode_enabled = True
        app_module.app.state.server_request_security = self._security()
        app_module.app.state.server_workspace_users = service
        return TestClient(app_module.app), previous

    @staticmethod
    def _restore_client(
        client: TestClient,
        previous: tuple[object, object, object],
    ) -> None:
        import app as app_module

        client.close()
        (
            app_module.app.state.server_mode_enabled,
            app_module.app.state.server_request_security,
            app_module.app.state.server_workspace_users,
        ) = previous

    def _version_status_role(self, user_id: str) -> tuple[int, str, str]:
        with self.engine.connect() as connection:
            row = connection.execute(
                sa.select(
                    workspace_users.c.session_version,
                    workspace_users.c.status,
                    workspace_users.c.organization_role,
                ).where(
                    workspace_users.c.organization_id == self.org_a,
                    workspace_users.c.user_id == user_id,
                )
            ).one()
        return int(row[0]), str(row[1]), str(row[2])

    def test_directory_create_and_update_are_scoped_and_audited(self) -> None:
        audit = RecordingAuditWriter()
        client, previous = self._client(
            PostgresWorkspaceUserService(self.engine, audit=audit)
        )
        path = f"/api/organizations/{self.org_a}/users"
        try:
            self.assertEqual(client.get(path).status_code, 401)
            client.cookies.set(
                SERVER_AUTH_COOKIE_NAME,
                self._token(self.org_a, self.admin_a),
            )
            auth_status = client.get("/api/auth/status")
            self.assertEqual(
                auth_status.json()["data"]["organization_id"],
                self.org_a,
            )
            self.assertEqual(
                auth_status.json()["data"]["user_id"],
                self.admin_a,
            )
            first = client.get(path, params={"limit": 2})
            self.assertEqual(first.status_code, 200, first.text)
            self.assertEqual(len(first.json()["items"]), 2)
            cursor = first.json()["next_after_user_id"]
            self.assertIsNotNone(cursor)
            second = client.get(
                path,
                params={"limit": 100, "after_user_id": cursor},
            )
            self.assertEqual(second.status_code, 200, second.text)
            all_items = first.json()["items"] + second.json()["items"]
            target = next(
                item for item in all_items if item["user_id"] == self.target_a
            )
            self.assertEqual(target["team_membership_count"], 1)
            self.assertEqual(target["project_membership_count"], 1)
            self.assertTrue(target["login_linked"])
            self.assertNotIn("session_version", target)

            client.cookies.set(
                SERVER_AUTH_COOKIE_NAME,
                self._token(self.org_a, self.member_a),
            )
            self.assertEqual(client.get(path).status_code, 403)
            client.cookies.set(
                SERVER_AUTH_COOKIE_NAME,
                self._token(self.org_a, self.admin_a),
            )
            cross_org = client.get(
                f"/api/organizations/{self.org_b}/users"
            )
            self.assertEqual(cross_org.status_code, 403)

            created_id = f"{self.org_a}-created"
            rejected = client.post(
                path,
                json={
                    "user_id": created_id,
                    "display_name": "Created User",
                    "organization_role": "member",
                    "session_version": 99,
                },
            )
            self.assertEqual(rejected.status_code, 422)
            created = client.post(
                path,
                json={
                    "user_id": created_id,
                    "display_name": "Created User",
                    "organization_role": "member",
                },
            )
            self.assertEqual(created.status_code, 201, created.text)
            self.assertNotIn("session_version", created.json())
            self.assertEqual(
                client.post(
                    path,
                    json={
                        "user_id": created_id,
                        "display_name": "Duplicate",
                        "organization_role": "member",
                    },
                ).status_code,
                409,
            )
            updated = client.patch(
                f"{path}/{created_id}",
                json={
                    "display_name": "Updated User",
                    "organization_role": "org_admin",
                },
            )
            self.assertEqual(updated.status_code, 200, updated.text)
            self.assertEqual(updated.json()["display_name"], "Updated User")
            self.assertEqual(
                updated.json()["organization_role"],
                "org_admin",
            )
            self.assertEqual(
                [event.action for event in audit.events],
                ["workspace_user.created", "workspace_user.updated"],
            )
            self.assertNotIn(
                "Updated User",
                str(audit.events[-1].details),
            )
        finally:
            self._restore_client(client, previous)

    def test_member_can_update_only_their_own_profile_display_name(self) -> None:
        audit = RecordingAuditWriter()
        client, previous = self._client(
            PostgresWorkspaceUserService(self.engine, audit=audit)
        )
        try:
            self.assertEqual(client.get("/api/account/profile").status_code, 401)
            client.cookies.set(
                SERVER_AUTH_COOKIE_NAME,
                self._token(self.org_a, self.member_a),
            )
            profile = client.get("/api/account/profile")
            self.assertEqual(profile.status_code, 200, profile.text)
            self.assertEqual(profile.json()["user_id"], self.member_a)
            self.assertEqual(profile.json()["display_name"], "Member A")

            updated = client.patch(
                "/api/account/profile",
                json={"display_name": "成员 A"},
            )
            self.assertEqual(updated.status_code, 200, updated.text)
            self.assertEqual(updated.json()["display_name"], "成员 A")
            self.assertEqual(
                [event.action for event in audit.events],
                ["workspace_user.profile_updated"],
            )
            self.assertNotIn("成员 A", str(audit.events[0].details))

            with self.engine.connect() as connection:
                target_name = connection.execute(
                    sa.select(workspace_users.c.display_name).where(
                        workspace_users.c.organization_id == self.org_a,
                        workspace_users.c.user_id == self.target_a,
                    )
                ).scalar_one()
            self.assertEqual(target_name, "Target A")
        finally:
            self._restore_client(client, previous)

    def test_disable_and_reenable_each_invalidate_prior_sessions(self) -> None:
        client, previous = self._client(
            PostgresWorkspaceUserService(self.engine)
        )
        path = (
            f"/api/organizations/{self.org_a}/users/{self.target_a}"
        )
        old_target_token = self._token(self.org_a, self.target_a)
        try:
            client.cookies.set(
                SERVER_AUTH_COOKIE_NAME,
                self._token(self.org_a, self.admin_a),
            )
            disabled = client.patch(path, json={"status": "disabled"})
            self.assertEqual(disabled.status_code, 200, disabled.text)
            self.assertNotIn("session_version", disabled.json())
            self.assertEqual(
                self._version_status_role(self.target_a),
                (2, "disabled", "member"),
            )

            client.cookies.set(SERVER_AUTH_COOKIE_NAME, old_target_token)
            self.assertEqual(client.get("/api/projects").status_code, 401)
            client.cookies.set(
                SERVER_AUTH_COOKIE_NAME,
                self._token(self.org_a, self.admin_a),
            )
            enabled = client.patch(path, json={"status": "active"})
            self.assertEqual(enabled.status_code, 200, enabled.text)
            self.assertEqual(
                self._version_status_role(self.target_a),
                (3, "active", "member"),
            )
            client.cookies.set(SERVER_AUTH_COOKIE_NAME, old_target_token)
            self.assertEqual(client.get("/api/projects").status_code, 401)
        finally:
            self._restore_client(client, previous)

    def test_last_active_admin_cannot_be_demoted_or_disabled(self) -> None:
        client, previous = self._client(
            PostgresWorkspaceUserService(self.engine)
        )
        base = f"/api/organizations/{self.org_a}/users"
        try:
            client.cookies.set(
                SERVER_AUTH_COOKIE_NAME,
                self._token(self.org_a, self.admin_a),
            )
            demoted = client.patch(
                f"{base}/{self.admin_a}",
                json={
                    "organization_role": "member",
                    "team_id": self.team_a,
                },
            )
            self.assertEqual(demoted.status_code, 200, demoted.text)
            client.cookies.set(
                SERVER_AUTH_COOKIE_NAME,
                self._token(self.org_a, self.admin2_a),
            )
            protected = client.patch(
                f"{base}/{self.admin2_a}",
                json={"status": "disabled"},
            )
            self.assertEqual(protected.status_code, 409, protected.text)
            self.assertEqual(
                self._version_status_role(self.admin2_a),
                (1, "active", "org_admin"),
            )
        finally:
            self._restore_client(client, previous)

    def test_audit_failure_rolls_back_and_redacts_private_error(self) -> None:
        client, previous = self._client(
            PostgresWorkspaceUserService(
                self.engine,
                audit=FailingAuditWriter(),
            )
        )
        try:
            client.cookies.set(
                SERVER_AUTH_COOKIE_NAME,
                self._token(self.org_a, self.admin_a),
            )
            response = client.patch(
                (
                    f"/api/organizations/{self.org_a}/users/"
                    f"{self.target_a}"
                ),
                json={"status": "disabled"},
            )
            self.assertEqual(response.status_code, 503, response.text)
            self.assertNotIn(PRIVATE_AUDIT_ERROR, response.text)
            self.assertEqual(
                self._version_status_role(self.target_a),
                (1, "active", "member"),
            )
        finally:
            self._restore_client(client, previous)

    def test_server_route_allowlist_is_exact(self) -> None:
        base = f"/api/organizations/{self.org_a}/users"
        self.assertTrue(server_http_route_available("GET", base))
        self.assertTrue(server_http_route_available("POST", base))
        self.assertTrue(
            server_http_route_available("PATCH", f"{base}/{self.target_a}")
        )
        self.assertFalse(server_http_route_available("PUT", base))
        self.assertFalse(
            server_http_route_available(
                "PATCH",
                f"{base}/{self.target_a}/sessions",
            )
        )


if __name__ == "__main__":
    unittest.main()
