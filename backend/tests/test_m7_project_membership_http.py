from __future__ import annotations

import os
import sys
import unittest
import uuid
from pathlib import Path

import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from knowledge_agent.database import create_knowledge_engine  # noqa: E402
from knowledge_agent.schema import projects  # noqa: E402
from server_schema import (  # noqa: E402
    organizations,
    project_memberships,
    project_ownership,
    team_memberships,
    teams,
    workspace_users,
)
from services.access_control import (  # noqa: E402
    ActorIdentity,
    PostgresProjectAccessRepository,
    ProjectAccessService,
)
from services.actor_sessions import PostgresActorSessionRepository  # noqa: E402
from services.project_memberships import (  # noqa: E402
    PostgresProjectMembershipService,
)
from services.server_auth import (  # noqa: E402
    SERVER_AUTH_COOKIE_NAME,
    ServerActorSessionCodec,
)
from services.server_request_security import (  # noqa: E402
    ServerRequestSecurity,
)


PRIVATE_AUDIT_ERROR = "private-project-membership-audit-body"


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
class ProjectMembershipHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = create_knowledge_engine(
            os.environ["ARTICLE_AGENT_DATABASE_URL"]
        )
        cls.codec = ServerActorSessionCodec(b"m" * 32)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    def setUp(self) -> None:
        prefix = f"m7-member-http-{uuid.uuid4().hex}"
        self.org_a = f"{prefix}-org-a"
        self.org_b = f"{prefix}-org-b"
        self.project_a = f"{prefix}-a.example.test"
        self.project_b = f"{prefix}-b.example.test"
        self.team_a = f"{prefix}-team-a"
        self.admin_a = f"{prefix}-admin-a"
        self.lead_a = f"{prefix}-lead-a"
        self.editor_a = f"{prefix}-editor-a"
        self.target_a = f"{prefix}-target-a"
        self.candidate_a = f"{prefix}-candidate-a"
        self.candidate_b = f"{prefix}-candidate-b"
        self.disabled_a = f"{prefix}-disabled-a"
        self.admin_b = f"{prefix}-admin-b"
        with self.engine.begin() as connection:
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
                        "user_id": self.lead_a,
                        "display_name": "Lead A",
                        "organization_role": "member",
                    },
                    {
                        "organization_id": self.org_a,
                        "user_id": self.editor_a,
                        "display_name": "Editor A",
                        "organization_role": "member",
                    },
                    {
                        "organization_id": self.org_a,
                        "user_id": self.target_a,
                        "display_name": "Target A",
                        "organization_role": "member",
                    },
                    {
                        "organization_id": self.org_a,
                        "user_id": self.candidate_a,
                        "display_name": "Candidate A",
                        "organization_role": "member",
                    },
                    {
                        "organization_id": self.org_a,
                        "user_id": self.candidate_b,
                        "display_name": "Candidate B",
                        "organization_role": "member",
                    },
                    {
                        "organization_id": self.org_a,
                        "user_id": self.disabled_a,
                        "display_name": "Disabled A",
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
                workspace_users.update()
                .where(
                    workspace_users.c.organization_id == self.org_a,
                    workspace_users.c.user_id == self.disabled_a,
                )
                .values(status="disabled")
            )
            connection.execute(
                teams.insert().values(
                    organization_id=self.org_a,
                    team_id=self.team_a,
                    name="Team A",
                    manager_user_id=self.lead_a,
                )
            )
            connection.execute(
                team_memberships.insert().values(
                    organization_id=self.org_a,
                    team_id=self.team_a,
                    user_id=self.lead_a,
                    role="team_lead",
                    granted_by_user_id=self.admin_a,
                )
            )
            connection.execute(
                project_ownership.insert(),
                (
                    {
                        "organization_id": self.org_a,
                        "project_id": self.project_a,
                        "owning_team_id": self.team_a,
                    },
                    {
                        "organization_id": self.org_b,
                        "project_id": self.project_b,
                        "owning_team_id": None,
                    },
                ),
            )
            connection.execute(
                project_memberships.insert().values(
                    organization_id=self.org_a,
                    project_id=self.project_a,
                    user_id=self.editor_a,
                    role="editor",
                    granted_by_user_id=self.admin_a,
                )
            )

    def tearDown(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                project_memberships.delete().where(
                    project_memberships.c.organization_id.in_(
                        (self.org_a, self.org_b)
                    )
                )
            )
            connection.execute(
                team_memberships.delete().where(
                    team_memberships.c.organization_id.in_(
                        (self.org_a, self.org_b)
                    )
                )
            )
            connection.execute(
                project_ownership.delete().where(
                    project_ownership.c.organization_id.in_(
                        (self.org_a, self.org_b)
                    )
                )
            )
            connection.execute(
                teams.delete().where(
                    teams.c.organization_id.in_((self.org_a, self.org_b))
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
                    projects.c.project_id.in_(
                        (self.project_a, self.project_b)
                    )
                )
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

    def _role(self, user_id: str) -> str | None:
        with self.engine.connect() as connection:
            return connection.execute(
                sa.select(project_memberships.c.role).where(
                    project_memberships.c.organization_id == self.org_a,
                    project_memberships.c.project_id == self.project_a,
                    project_memberships.c.user_id == user_id,
                )
            ).scalar_one_or_none()

    def test_http_grant_and_revoke_are_scoped_and_role_bounded(self) -> None:
        import app as app_module

        audit = RecordingAuditWriter()
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
        previous_memberships = getattr(
            app_module.app.state,
            "server_project_memberships",
            None,
        )
        app_module.app.state.server_mode_enabled = True
        app_module.app.state.server_request_security = self._security()
        app_module.app.state.server_project_memberships = (
            PostgresProjectMembershipService(
                self.engine,
                audit=audit,
            )
        )
        client = TestClient(app_module.app)
        path = (
            f"/api/projects/{self.project_a}/members/{self.target_a}"
        )
        try:
            self.assertEqual(
                client.put(path, json={"role": "viewer"}).status_code,
                401,
            )
            client.cookies.set(
                SERVER_AUTH_COOKIE_NAME,
                self._token(self.org_a, self.lead_a),
            )
            self.assertEqual(
                client.put(
                    path,
                    json={"role": "viewer", "organization_id": self.org_a},
                ).status_code,
                422,
            )
            self.assertEqual(
                client.put(path, json={"role": "owner"}).status_code,
                422,
            )

            granted = client.put(path, json={"role": "editor"})
            self.assertEqual(granted.status_code, 200, granted.text)
            self.assertEqual(
                granted.json(),
                {"user_id": self.target_a, "role": "editor"},
            )
            self.assertEqual(self._role(self.target_a), "editor")
            self.assertEqual(audit.events[-1].details, {"role": "editor"})

            client.cookies.set(
                SERVER_AUTH_COOKIE_NAME,
                self._token(self.org_a, self.editor_a),
            )
            self.assertEqual(
                client.put(path, json={"role": "editor"}).status_code,
                403,
            )
            self.assertEqual(self._role(self.target_a), "editor")

            client.cookies.set(
                SERVER_AUTH_COOKIE_NAME,
                self._token(self.org_a, self.admin_a),
            )
            cross_project = client.put(
                (
                    f"/api/projects/{self.project_b}/members/"
                    f"{self.admin_b}"
                ),
                json={"role": "editor"},
            )
            self.assertEqual(cross_project.status_code, 403)
            unavailable_target = client.put(
                (
                    f"/api/projects/{self.project_a}/members/"
                    f"{self.admin_b}"
                ),
                json={"role": "editor"},
            )
            self.assertEqual(unavailable_target.status_code, 404)

            client.cookies.set(
                SERVER_AUTH_COOKIE_NAME,
                self._token(self.org_a, self.lead_a),
            )
            revoked = client.delete(path)
            self.assertEqual(revoked.status_code, 200, revoked.text)
            self.assertEqual(
                revoked.json(),
                {"user_id": self.target_a, "revoked": True},
            )
            self.assertIsNone(self._role(self.target_a))
            repeated = client.delete(path)
            self.assertEqual(
                repeated.json(),
                {"user_id": self.target_a, "revoked": False},
            )
            self.assertEqual(
                [event.action for event in audit.events],
                [
                    "project.membership.granted",
                    "project.membership.revoked",
                ],
            )
        finally:
            client.close()
            app_module.app.state.server_mode_enabled = previous_mode
            app_module.app.state.server_request_security = previous_security
            app_module.app.state.server_project_memberships = (
                previous_memberships
            )

    def test_http_candidates_only_include_active_users_without_access(
        self,
    ) -> None:
        import app as app_module

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
        previous_memberships = getattr(
            app_module.app.state,
            "server_project_memberships",
            None,
        )
        app_module.app.state.server_mode_enabled = True
        app_module.app.state.server_request_security = self._security()
        app_module.app.state.server_project_memberships = (
            PostgresProjectMembershipService(
                self.engine,
                audit=RecordingAuditWriter(),
            )
        )
        client = TestClient(app_module.app)
        path = f"/api/projects/{self.project_a}/members/candidates"
        try:
            self.assertEqual(client.get(path).status_code, 401)

            client.cookies.set(
                SERVER_AUTH_COOKIE_NAME,
                self._token(self.org_a, self.editor_a),
            )
            self.assertEqual(client.get(path).status_code, 403)

            client.cookies.set(
                SERVER_AUTH_COOKIE_NAME,
                self._token(self.org_a, self.lead_a),
            )
            first_page = client.get(path, params={"limit": 2})
            self.assertEqual(first_page.status_code, 200, first_page.text)
            self.assertEqual(
                first_page.json(),
                {
                    "items": [
                        {
                            "user_id": self.candidate_a,
                            "display_name": "Candidate A",
                        },
                        {
                            "user_id": self.candidate_b,
                            "display_name": "Candidate B",
                        },
                    ],
                    "next_after_user_id": self.candidate_b,
                },
            )
            second_page = client.get(
                path,
                params={
                    "limit": 2,
                    "after_user_id": self.candidate_b,
                },
            )
            self.assertEqual(
                second_page.json(),
                {
                    "items": [
                        {
                            "user_id": self.target_a,
                            "display_name": "Target A",
                        }
                    ],
                    "next_after_user_id": None,
                },
            )

            granted = client.put(
                (
                    f"/api/projects/{self.project_a}/members/"
                    f"{self.target_a}"
                ),
                json={"role": "editor"},
            )
            self.assertEqual(granted.status_code, 200, granted.text)
            refreshed = client.get(path)
            self.assertEqual(
                [item["user_id"] for item in refreshed.json()["items"]],
                [self.candidate_a, self.candidate_b],
            )
            self.assertNotIn(self.admin_a, refreshed.text)
            self.assertNotIn(self.lead_a, refreshed.text)
            self.assertNotIn(self.editor_a, refreshed.text)
            self.assertNotIn(self.disabled_a, refreshed.text)
            self.assertNotIn(self.admin_b, refreshed.text)

            with self.engine.begin() as connection:
                connection.execute(
                    teams.update()
                    .where(
                        teams.c.organization_id == self.org_a,
                        teams.c.team_id == self.team_a,
                    )
                    .values(status="archived")
                )
            self.assertEqual(client.get(path).status_code, 403)
            client.cookies.set(
                SERVER_AUTH_COOKIE_NAME,
                self._token(self.org_a, self.admin_a),
            )
            after_team_archive = client.get(path)
            self.assertEqual(
                [
                    item["user_id"]
                    for item in after_team_archive.json()["items"]
                ],
                [self.candidate_a, self.candidate_b, self.lead_a],
            )
            self.assertEqual(
                client.get(
                    f"/api/projects/{self.project_b}/members/candidates"
                ).status_code,
                403,
            )
        finally:
            client.close()
            app_module.app.state.server_mode_enabled = previous_mode
            app_module.app.state.server_request_security = previous_security
            app_module.app.state.server_project_memberships = (
                previous_memberships
            )

    def test_http_list_is_manage_only_scoped_bounded_and_deterministic(
        self,
    ) -> None:
        import app as app_module

        with self.engine.begin() as connection:
            connection.execute(
                project_memberships.insert(),
                (
                    {
                        "organization_id": self.org_a,
                        "project_id": self.project_a,
                        "user_id": self.lead_a,
                        "role": "reviewer",
                        "granted_by_user_id": self.admin_a,
                    },
                    {
                        "organization_id": self.org_a,
                        "project_id": self.project_a,
                        "user_id": self.target_a,
                        "role": "viewer",
                        "granted_by_user_id": self.admin_a,
                    },
                    {
                        "organization_id": self.org_b,
                        "project_id": self.project_b,
                        "user_id": self.admin_b,
                        "role": "editor",
                        "granted_by_user_id": self.admin_b,
                    },
                ),
            )
            connection.execute(
                workspace_users.update()
                .where(
                    workspace_users.c.organization_id == self.org_a,
                    workspace_users.c.user_id == self.target_a,
                )
                .values(status="disabled")
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
        previous_memberships = getattr(
            app_module.app.state,
            "server_project_memberships",
            None,
        )
        app_module.app.state.server_mode_enabled = True
        app_module.app.state.server_request_security = self._security()
        app_module.app.state.server_project_memberships = (
            PostgresProjectMembershipService(self.engine)
        )
        client = TestClient(app_module.app)
        path = f"/api/projects/{self.project_a}/members"
        try:
            self.assertEqual(client.get(path).status_code, 401)

            client.cookies.set(
                SERVER_AUTH_COOKIE_NAME,
                self._token(self.org_a, self.editor_a),
            )
            self.assertEqual(client.get(path).status_code, 403)

            client.cookies.set(
                SERVER_AUTH_COOKIE_NAME,
                self._token(self.org_a, self.lead_a),
            )
            listed = client.get(path)
            self.assertEqual(listed.status_code, 200, listed.text)
            self.assertEqual(
                listed.json(),
                {
                    "items": [
                        {
                            "user_id": self.editor_a,
                            "display_name": "Editor A",
                            "status": "active",
                            "role": "editor",
                        },
                        {
                            "user_id": self.lead_a,
                            "display_name": "Lead A",
                            "status": "active",
                            "role": "reviewer",
                        },
                        {
                            "user_id": self.target_a,
                            "display_name": "Target A",
                            "status": "disabled",
                            "role": "viewer",
                        },
                    ],
                    "next_after_user_id": None,
                },
            )
            first_page = client.get(path, params={"limit": 2})
            self.assertEqual(first_page.status_code, 200, first_page.text)
            self.assertEqual(
                [item["user_id"] for item in first_page.json()["items"]],
                [self.editor_a, self.lead_a],
            )
            self.assertEqual(
                first_page.json()["next_after_user_id"],
                self.lead_a,
            )
            second_page = client.get(
                path,
                params={"limit": 2, "after_user_id": self.lead_a},
            )
            self.assertEqual(
                [item["user_id"] for item in second_page.json()["items"]],
                [self.target_a],
            )
            self.assertIsNone(second_page.json()["next_after_user_id"])
            self.assertEqual(
                client.get(
                    path,
                    params={"after_user_id": "  "},
                ).status_code,
                422,
            )
            self.assertEqual(
                client.get(
                    f"/api/projects/{self.project_b}/members"
                ).status_code,
                403,
            )
        finally:
            client.close()
            app_module.app.state.server_mode_enabled = previous_mode
            app_module.app.state.server_request_security = previous_security
            app_module.app.state.server_project_memberships = (
                previous_memberships
            )

    def test_http_audit_failure_rolls_back_and_redacts_error(self) -> None:
        import app as app_module

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
        previous_memberships = getattr(
            app_module.app.state,
            "server_project_memberships",
            None,
        )
        app_module.app.state.server_mode_enabled = True
        app_module.app.state.server_request_security = self._security()
        app_module.app.state.server_project_memberships = (
            PostgresProjectMembershipService(
                self.engine,
                audit=FailingAuditWriter(),
            )
        )
        client = TestClient(app_module.app)
        try:
            client.cookies.set(
                SERVER_AUTH_COOKIE_NAME,
                self._token(self.org_a, self.admin_a),
            )
            response = client.put(
                (
                    f"/api/projects/{self.project_a}/members/"
                    f"{self.target_a}"
                ),
                json={"role": "editor"},
            )
            self.assertEqual(response.status_code, 503)
            self.assertNotIn(PRIVATE_AUDIT_ERROR, response.text)
            self.assertIsNone(self._role(self.target_a))
        finally:
            client.close()
            app_module.app.state.server_mode_enabled = previous_mode
            app_module.app.state.server_request_security = previous_security
            app_module.app.state.server_project_memberships = (
                previous_memberships
            )

    def test_authorization_fact_is_locked_until_membership_transaction_ends(
        self,
    ) -> None:
        audit = RecordingAuditWriter()
        service = PostgresProjectMembershipService(
            self.engine,
            audit=audit,
        )
        writer = self.engine.connect()
        writer_transaction = writer.begin()
        competing = self.engine.connect()
        competing_transaction = competing.begin()
        try:
            service.grant_in_transaction(
                writer,
                actor=ActorIdentity(self.org_a, self.lead_a),
                project_id=self.project_a,
                target_user_id=self.target_a,
                role="viewer",
                event_id=f"event-{uuid.uuid4().hex}",
            )
            competing.execute(
                sa.text("SET LOCAL lock_timeout = '150ms'")
            )
            with self.assertRaises(DBAPIError):
                competing.execute(
                    team_memberships.update()
                    .where(
                        team_memberships.c.organization_id == self.org_a,
                        team_memberships.c.team_id == self.team_a,
                        team_memberships.c.user_id == self.lead_a,
                    )
                    .values(role="member")
                )
        finally:
            competing_transaction.rollback()
            competing.close()
            writer_transaction.rollback()
            writer.close()


if __name__ == "__main__":
    unittest.main()
