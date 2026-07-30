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
    organizations,
    project_ownership,
    team_memberships,
    teams,
    workspace_users,
)
from services.access_control import (  # noqa: E402
    ActorIdentity,
    PostgresProjectAccessRepository,
    ProjectAccessDenied,
    ProjectAccessService,
)
from services.actor_sessions import PostgresActorSessionRepository  # noqa: E402
from services.server_auth import (  # noqa: E402
    SERVER_AUTH_COOKIE_NAME,
    ServerActorSessionCodec,
)
from services.server_request_security import (  # noqa: E402
    ServerRequestSecurity,
    server_http_route_available,
)
from services.team_administration import (  # noqa: E402
    PostgresTeamAdministrationService,
)


PRIVATE_AUDIT_ERROR = "private-team-administration-audit-body"


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
class TeamAdministrationHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = create_knowledge_engine(
            os.environ["ARTICLE_AGENT_DATABASE_URL"]
        )
        cls.codec = ServerActorSessionCodec(b"t" * 32)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    def setUp(self) -> None:
        prefix = f"m7-team-http-{uuid.uuid4().hex}"
        self.org_a = f"{prefix}-org-a"
        self.org_b = f"{prefix}-org-b"
        self.admin_a = f"{prefix}-admin-a"
        self.member_a = f"{prefix}-member-a"
        self.manager_a = f"{prefix}-manager-a"
        self.lead_a = f"{prefix}-lead-a"
        self.disabled_a = f"{prefix}-disabled-a"
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
                    self._user(self.org_a, self.member_a, "Member A", "member"),
                    self._user(self.org_a, self.manager_a, "Manager A", "member"),
                    self._user(self.org_a, self.lead_a, "Lead A", "member"),
                    self._user(self.org_b, self.admin_b, "Admin B", "org_admin"),
                ),
            )
            connection.execute(
                workspace_users.insert().values(
                    **self._user(
                        self.org_a,
                        self.disabled_a,
                        "Disabled A",
                        "member",
                    ),
                    status="disabled",
                )
            )
            connection.execute(
                teams.insert().values(
                    organization_id=self.org_a,
                    team_id=self.team_a,
                    name="Team A",
                    manager_user_id=self.manager_a,
                )
            )
            connection.execute(
                team_memberships.insert(),
                (
                    {
                        "organization_id": self.org_a,
                        "team_id": self.team_a,
                        "user_id": self.lead_a,
                        "role": "team_lead",
                        "granted_by_user_id": self.admin_a,
                    },
                    {
                        "organization_id": self.org_a,
                        "team_id": self.team_a,
                        "user_id": self.disabled_a,
                        "role": "member",
                        "granted_by_user_id": self.admin_a,
                    },
                ),
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

    def _token(self, organization_id: str, user_id: str) -> str:
        return self.codec.create(
            ActorIdentity(organization_id, user_id),
            session_version=1,
        )

    def _client(
        self,
        service: PostgresTeamAdministrationService,
    ) -> tuple[TestClient, tuple[object, object, object]]:
        import app as app_module

        previous = (
            getattr(app_module.app.state, "server_mode_enabled", None),
            getattr(app_module.app.state, "server_request_security", None),
            getattr(
                app_module.app.state,
                "server_team_administration",
                None,
            ),
        )
        app_module.app.state.server_mode_enabled = True
        app_module.app.state.server_request_security = ServerRequestSecurity(
            codec=self.codec,
            access=object(),  # type: ignore[arg-type]
            sessions=PostgresActorSessionRepository(self.engine),
        )
        app_module.app.state.server_team_administration = service
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
            app_module.app.state.server_team_administration,
        ) = previous

    def _team_status(self) -> str:
        with self.engine.connect() as connection:
            value = connection.execute(
                sa.select(teams.c.status).where(
                    teams.c.organization_id == self.org_a,
                    teams.c.team_id == self.team_a,
                )
            ).scalar_one()
        return str(value)

    def _member_role(self, user_id: str) -> str | None:
        with self.engine.connect() as connection:
            value = connection.execute(
                sa.select(team_memberships.c.role).where(
                    team_memberships.c.organization_id == self.org_a,
                    team_memberships.c.team_id == self.team_a,
                    team_memberships.c.user_id == user_id,
                )
            ).scalar_one_or_none()
        return str(value) if value is not None else None

    def test_team_directory_create_update_are_admin_only_and_scoped(
        self,
    ) -> None:
        audit = RecordingAuditWriter()
        client, previous = self._client(
            PostgresTeamAdministrationService(self.engine, audit=audit)
        )
        path = f"/api/organizations/{self.org_a}/teams"
        try:
            self.assertEqual(client.get(path).status_code, 401)
            client.cookies.set(
                SERVER_AUTH_COOKIE_NAME,
                self._token(self.org_a, self.member_a),
            )
            self.assertEqual(client.get(path).status_code, 403)
            client.cookies.set(
                SERVER_AUTH_COOKIE_NAME,
                self._token(self.org_a, self.admin_a),
            )
            listed = client.get(path, params={"limit": 1})
            self.assertEqual(listed.status_code, 200, listed.text)
            team = listed.json()["items"][0]
            self.assertEqual(team["member_count"], 2)
            self.assertEqual(team["team_lead_count"], 1)
            self.assertEqual(team["project_count"], 1)
            self.assertEqual(team["manager_user_id"], self.manager_a)
            self.assertEqual(
                client.get(f"/api/organizations/{self.org_b}/teams").status_code,
                403,
            )

            new_team_id = f"{self.org_a}-new-team"
            rejected = client.post(
                path,
                json={
                    "team_id": new_team_id,
                    "name": "New Team",
                    "status": "archived",
                },
            )
            self.assertEqual(rejected.status_code, 422)
            cross_manager = client.post(
                path,
                json={
                    "team_id": new_team_id,
                    "name": "New Team",
                    "manager_user_id": self.admin_b,
                },
            )
            self.assertEqual(cross_manager.status_code, 404)
            created = client.post(
                path,
                json={
                    "team_id": new_team_id,
                    "name": "New Team",
                    "manager_user_id": self.member_a,
                },
            )
            self.assertEqual(created.status_code, 201, created.text)
            self.assertEqual(created.json()["status"], "active")
            first_page = client.get(path, params={"limit": 1}).json()
            second_page = client.get(
                path,
                params={
                    "limit": 100,
                    "after_team_id": first_page["next_after_team_id"],
                },
            ).json()
            paged_ids = [
                item["team_id"]
                for item in first_page["items"] + second_page["items"]
            ]
            self.assertEqual(paged_ids, sorted([self.team_a, new_team_id]))
            self.assertEqual(
                client.post(
                    path,
                    json={
                        "team_id": new_team_id,
                        "name": "Duplicate",
                    },
                ).status_code,
                409,
            )
            updated = client.patch(
                f"{path}/{new_team_id}",
                json={"name": "Renamed Team", "manager_user_id": None},
            )
            self.assertEqual(updated.status_code, 200, updated.text)
            self.assertEqual(updated.json()["name"], "Renamed Team")
            self.assertIsNone(updated.json()["manager_user_id"])
            self.assertEqual(
                [event.action for event in audit.events],
                ["team.created", "team.updated"],
            )
            self.assertNotIn("Renamed Team", str(audit.events[-1].details))
        finally:
            self._restore_client(client, previous)

    def test_membership_lifecycle_keeps_disabled_rows_for_cleanup(
        self,
    ) -> None:
        audit = RecordingAuditWriter()
        client, previous = self._client(
            PostgresTeamAdministrationService(self.engine, audit=audit)
        )
        base = (
            f"/api/organizations/{self.org_a}/teams/{self.team_a}/members"
        )
        try:
            client.cookies.set(
                SERVER_AUTH_COOKIE_NAME,
                self._token(self.org_a, self.admin_a),
            )
            listed = client.get(base, params={"limit": 100})
            self.assertEqual(listed.status_code, 200, listed.text)
            disabled = next(
                item
                for item in listed.json()["items"]
                if item["user_id"] == self.disabled_a
            )
            self.assertEqual(disabled["user_status"], "disabled")
            first_page = client.get(base, params={"limit": 1}).json()
            second_page = client.get(
                base,
                params={
                    "limit": 100,
                    "after_user_id": first_page["next_after_user_id"],
                },
            ).json()
            paged_ids = [
                item["user_id"]
                for item in first_page["items"] + second_page["items"]
            ]
            self.assertEqual(
                paged_ids,
                sorted([self.disabled_a, self.lead_a]),
            )
            self.assertEqual(
                client.put(
                    f"{base}/{self.disabled_a}",
                    json={"role": "team_lead"},
                ).status_code,
                404,
            )
            self.assertEqual(
                client.put(
                    f"{base}/{self.admin_b}",
                    json={"role": "member"},
                ).status_code,
                404,
            )

            granted = client.put(
                f"{base}/{self.member_a}",
                json={"role": "team_lead"},
            )
            self.assertEqual(granted.status_code, 200, granted.text)
            self.assertEqual(self._member_role(self.member_a), "team_lead")
            changed = client.put(
                f"{base}/{self.member_a}",
                json={"role": "member"},
            )
            self.assertEqual(changed.status_code, 200, changed.text)
            self.assertEqual(self._member_role(self.member_a), "member")

            archived = client.patch(
                f"/api/organizations/{self.org_a}/teams/{self.team_a}",
                json={"status": "archived"},
            )
            self.assertEqual(archived.status_code, 200, archived.text)
            self.assertEqual(
                client.put(
                    f"{base}/{self.manager_a}",
                    json={"role": "team_lead"},
                ).status_code,
                409,
            )
            revoked = client.delete(f"{base}/{self.member_a}")
            self.assertEqual(revoked.status_code, 200, revoked.text)
            self.assertTrue(revoked.json()["revoked"])
            repeated = client.delete(f"{base}/{self.member_a}")
            self.assertFalse(repeated.json()["revoked"])
            self.assertEqual(
                [event.action for event in audit.events],
                [
                    "team.membership.granted",
                    "team.membership.updated",
                    "team.updated",
                    "team.membership.revoked",
                ],
            )
        finally:
            self._restore_client(client, previous)

    def test_manager_metadata_never_grants_project_access(self) -> None:
        access = ProjectAccessService(
            PostgresProjectAccessRepository(self.engine)
        )
        with self.assertRaises(ProjectAccessDenied):
            access.require(
                ActorIdentity(self.org_a, self.manager_a),
                self.project_a,
                "project.view",
            )
        access.require(
            ActorIdentity(self.org_a, self.lead_a),
            self.project_a,
            "project.view",
        )
        service = PostgresTeamAdministrationService(self.engine)
        service.update_team(
            actor=ActorIdentity(self.org_a, self.admin_a),
            organization_id=self.org_a,
            team_id=self.team_a,
            status="archived",
            event_id=f"team_archive_{uuid.uuid4().hex}",
        )
        with self.assertRaises(ProjectAccessDenied):
            access.require(
                ActorIdentity(self.org_a, self.lead_a),
                self.project_a,
                "project.view",
            )

    def test_audit_failure_rolls_back_team_and_membership(self) -> None:
        client, previous = self._client(
            PostgresTeamAdministrationService(
                self.engine,
                audit=FailingAuditWriter(),
            )
        )
        try:
            client.cookies.set(
                SERVER_AUTH_COOKIE_NAME,
                self._token(self.org_a, self.admin_a),
            )
            archived = client.patch(
                f"/api/organizations/{self.org_a}/teams/{self.team_a}",
                json={"status": "archived"},
            )
            self.assertEqual(archived.status_code, 503, archived.text)
            self.assertNotIn(PRIVATE_AUDIT_ERROR, archived.text)
            self.assertEqual(self._team_status(), "active")

            member = client.put(
                (
                    f"/api/organizations/{self.org_a}/teams/"
                    f"{self.team_a}/members/{self.member_a}"
                ),
                json={"role": "team_lead"},
            )
            self.assertEqual(member.status_code, 503, member.text)
            self.assertNotIn(PRIVATE_AUDIT_ERROR, member.text)
            self.assertIsNone(self._member_role(self.member_a))
        finally:
            self._restore_client(client, previous)

    def test_server_route_allowlist_is_exact(self) -> None:
        base = f"/api/organizations/{self.org_a}/teams"
        team = f"{base}/{self.team_a}"
        members = f"{team}/members"
        target = f"{members}/{self.member_a}"
        self.assertTrue(server_http_route_available("GET", base))
        self.assertTrue(server_http_route_available("POST", base))
        self.assertTrue(server_http_route_available("PATCH", team))
        self.assertTrue(server_http_route_available("GET", members))
        self.assertTrue(server_http_route_available("PUT", target))
        self.assertTrue(server_http_route_available("DELETE", target))
        self.assertFalse(server_http_route_available("DELETE", team))
        self.assertFalse(server_http_route_available("POST", members))


if __name__ == "__main__":
    unittest.main()
