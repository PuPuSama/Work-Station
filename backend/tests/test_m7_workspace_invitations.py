from __future__ import annotations

from datetime import timedelta
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
from server_schema import (  # noqa: E402
    audit_events,
    external_identities,
    organizations,
    workspace_invitations,
    workspace_users,
)
from services.access_control import ActorIdentity  # noqa: E402
from services.actor_sessions import (  # noqa: E402
    PostgresActorSessionRepository,
)
from services.external_identity import (  # noqa: E402
    VerifiedExternalIdentity,
)
from services.workspace_invitations import (  # noqa: E402
    PostgresWorkspaceInvitationService,
    WorkspaceInvitationConflict,
    WorkspaceInvitationDenied,
    WorkspaceInvitationUnavailable,
)
from services.server_auth import (  # noqa: E402
    SERVER_AUTH_COOKIE_NAME,
    ServerActorSessionCodec,
)
from services.server_request_security import (  # noqa: E402
    ServerRequestSecurity,
    server_http_route_available,
)


PRIVATE_ERROR = "private-invitation-audit-error"


class FailingAuditWriter:
    def append(self, connection, event) -> None:
        raise RuntimeError(PRIVATE_ERROR)


@unittest.skipUnless(
    os.environ.get("ARTICLE_AGENT_DATABASE_URL"),
    "ARTICLE_AGENT_DATABASE_URL is required for PostgreSQL integration tests",
)
class WorkspaceInvitationPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = create_knowledge_engine(
            os.environ["ARTICLE_AGENT_DATABASE_URL"]
        )
        cls.codec = ServerActorSessionCodec(b"v" * 32)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    def setUp(self) -> None:
        prefix = f"m7-invitation-{uuid.uuid4().hex}"
        self.org_a = f"{prefix}-org-a"
        self.org_b = f"{prefix}-org-b"
        self.admin_a = f"{prefix}-admin-a"
        self.member_a = f"{prefix}-member-a"
        self.target_a = f"{prefix}-target-a"
        self.disabled_a = f"{prefix}-disabled-a"
        self.admin_b = f"{prefix}-admin-b"
        self.issuer = f"https://idp-{uuid.uuid4().hex}.example.test"
        self.actor = ActorIdentity(self.org_a, self.admin_a)
        self.service = PostgresWorkspaceInvitationService(self.engine)
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
                    self._user(
                        self.org_a,
                        self.admin_a,
                        "Admin A",
                        "org_admin",
                    ),
                    self._user(
                        self.org_a,
                        self.member_a,
                        "Member A",
                        "member",
                    ),
                    self._user(
                        self.org_a,
                        self.target_a,
                        "Target A",
                        "member",
                    ),
                    {
                        **self._user(
                            self.org_a,
                            self.disabled_a,
                            "Disabled A",
                            "member",
                        ),
                        "status": "disabled",
                    },
                    self._user(
                        self.org_b,
                        self.admin_b,
                        "Admin B",
                        "org_admin",
                    ),
                ),
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
            "status": "active",
        }

    def _issue(
        self,
        *,
        service: PostgresWorkspaceInvitationService | None = None,
        user_id: str | None = None,
        issuer: str | None = None,
    ):
        return (service or self.service).issue(
            actor=self.actor,
            organization_id=self.org_a,
            user_id=user_id or self.target_a,
            issuer=issuer or self.issuer,
            expires_in_hours=24,
            event_id=f"issue-{uuid.uuid4().hex}",
        )

    def _client(
        self,
        service: PostgresWorkspaceInvitationService,
    ) -> tuple[TestClient, tuple[object, object, object]]:
        import app as app_module

        previous = (
            getattr(app_module.app.state, "server_mode_enabled", None),
            getattr(app_module.app.state, "server_request_security", None),
            getattr(
                app_module.app.state,
                "server_workspace_invitations",
                None,
            ),
        )
        app_module.app.state.server_mode_enabled = True
        app_module.app.state.server_request_security = ServerRequestSecurity(
            codec=self.codec,
            access=object(),  # type: ignore[arg-type]
            sessions=PostgresActorSessionRepository(self.engine),
        )
        app_module.app.state.server_workspace_invitations = service
        client = TestClient(app_module.app)
        return client, previous

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
            app_module.app.state.server_workspace_invitations,
        ) = previous

    def _session(self, user_id: str) -> str:
        return self.codec.create(
            ActorIdentity(self.org_a, user_id),
            session_version=1,
        )

    def test_issue_lists_without_token_hash_and_requires_revoke_to_reissue(
        self,
    ) -> None:
        issued = self._issue()
        self.assertGreaterEqual(len(issued.invitation_token), 40)
        page = self.service.list_invitations(
            actor=self.actor,
            organization_id=self.org_a,
            limit=1,
        )
        self.assertEqual(page.items[0].status, "pending")
        self.assertFalse(hasattr(page.items[0], "token_hash"))
        self.assertFalse(hasattr(page.items[0], "invitation_token"))
        with self.assertRaises(WorkspaceInvitationConflict):
            self._issue()
        revoked = self.service.revoke(
            actor=self.actor,
            organization_id=self.org_a,
            invitation_id=issued.invitation.invitation_id,
            event_id=f"revoke-{uuid.uuid4().hex}",
        )
        self.assertEqual(revoked.status, "revoked")
        replacement = self._issue()
        self.assertNotEqual(
            replacement.invitation.invitation_id,
            issued.invitation.invitation_id,
        )

    def test_redeem_binds_verified_identity_once_and_audits(self) -> None:
        issued = self._issue()
        identity = VerifiedExternalIdentity(
            self.issuer,
            f"subject-{uuid.uuid4().hex}",
        )
        resolved = self.service.redeem(
            invitation_token=issued.invitation_token,
            identity=identity,
            event_id=f"accept-{uuid.uuid4().hex}",
        )
        self.assertEqual(resolved.actor, ActorIdentity(self.org_a, self.target_a))
        with self.assertRaises(WorkspaceInvitationDenied):
            self.service.redeem(
                invitation_token=issued.invitation_token,
                identity=identity,
                event_id=f"replay-{uuid.uuid4().hex}",
            )
        with self.engine.connect() as connection:
            invitation_status = connection.execute(
                sa.select(workspace_invitations.c.status).where(
                    workspace_invitations.c.organization_id == self.org_a,
                    workspace_invitations.c.invitation_id
                    == issued.invitation.invitation_id,
                )
            ).scalar_one()
            mapping = connection.execute(
                sa.select(
                    external_identities.c.organization_id,
                    external_identities.c.user_id,
                ).where(
                    external_identities.c.issuer == identity.issuer,
                    external_identities.c.subject == identity.subject,
                )
            ).one()
            accepted_audits = connection.execute(
                sa.select(sa.func.count())
                .select_from(audit_events)
                .where(
                    audit_events.c.organization_id == self.org_a,
                    audit_events.c.action
                    == "workspace_invitation.accepted",
                    audit_events.c.target_id
                    == issued.invitation.invitation_id,
                )
            ).scalar_one()
        self.assertEqual(invitation_status, "accepted")
        self.assertEqual(mapping, (self.org_a, self.target_a))
        self.assertEqual(accepted_audits, 1)

    def test_wrong_issuer_expired_or_disabled_target_is_denied(self) -> None:
        issued = self._issue()
        with self.assertRaises(WorkspaceInvitationDenied):
            self.service.redeem(
                invitation_token=issued.invitation_token,
                identity=VerifiedExternalIdentity(
                    f"{self.issuer}/other",
                    "subject",
                ),
                event_id=f"wrong-{uuid.uuid4().hex}",
            )
        with self.engine.begin() as connection:
            created_at = connection.execute(
                sa.select(workspace_invitations.c.created_at).where(
                    workspace_invitations.c.organization_id == self.org_a,
                    workspace_invitations.c.invitation_id
                    == issued.invitation.invitation_id,
                )
            ).scalar_one()
            connection.execute(
                workspace_invitations.update()
                .where(
                    workspace_invitations.c.organization_id == self.org_a,
                    workspace_invitations.c.invitation_id
                    == issued.invitation.invitation_id,
                )
                .values(
                    expires_at=created_at + timedelta(microseconds=1)
                )
            )
        page = self.service.list_invitations(
            actor=self.actor,
            organization_id=self.org_a,
        )
        self.assertEqual(page.items[0].status, "expired")
        with self.assertRaises(WorkspaceInvitationDenied):
            self.service.redeem(
                invitation_token=issued.invitation_token,
                identity=VerifiedExternalIdentity(
                    self.issuer,
                    "subject",
                ),
                event_id=f"expired-{uuid.uuid4().hex}",
            )
        with self.assertRaises(WorkspaceInvitationDenied):
            self._issue(user_id=self.disabled_a)

    def test_non_admin_cross_org_and_existing_mapping_fail_closed(self) -> None:
        with self.assertRaises(WorkspaceInvitationDenied):
            self.service.list_invitations(
                actor=ActorIdentity(self.org_a, self.member_a),
                organization_id=self.org_a,
            )
        with self.assertRaises(WorkspaceInvitationDenied):
            self.service.list_invitations(
                actor=self.actor,
                organization_id=self.org_b,
            )
        issued = self._issue()
        subject = f"subject-{uuid.uuid4().hex}"
        with self.engine.begin() as connection:
            connection.execute(
                external_identities.insert().values(
                    issuer=self.issuer,
                    subject=subject,
                    organization_id=self.org_b,
                    user_id=self.admin_b,
                )
            )
        with self.assertRaises(WorkspaceInvitationDenied):
            self.service.redeem(
                invitation_token=issued.invitation_token,
                identity=VerifiedExternalIdentity(self.issuer, subject),
                event_id=f"cross-{uuid.uuid4().hex}",
            )

    def test_audit_failure_rolls_back_issue_and_redeem(self) -> None:
        failing = PostgresWorkspaceInvitationService(
            self.engine,
            audit=FailingAuditWriter(),
        )
        with self.assertRaises(WorkspaceInvitationUnavailable) as caught:
            self._issue(service=failing)
        self.assertNotIn(PRIVATE_ERROR, str(caught.exception))
        with self.engine.connect() as connection:
            issue_count = connection.execute(
                sa.select(sa.func.count())
                .select_from(workspace_invitations)
                .where(
                    workspace_invitations.c.organization_id == self.org_a
                )
            ).scalar_one()
        self.assertEqual(issue_count, 0)

        issued = self._issue()
        identity = VerifiedExternalIdentity(
            self.issuer,
            f"subject-{uuid.uuid4().hex}",
        )
        with self.assertRaises(WorkspaceInvitationUnavailable) as caught:
            failing.redeem(
                invitation_token=issued.invitation_token,
                identity=identity,
                event_id=f"accept-{uuid.uuid4().hex}",
            )
        self.assertNotIn(PRIVATE_ERROR, str(caught.exception))
        with self.engine.connect() as connection:
            status = connection.execute(
                sa.select(workspace_invitations.c.status).where(
                    workspace_invitations.c.organization_id == self.org_a,
                    workspace_invitations.c.invitation_id
                    == issued.invitation.invitation_id,
                )
            ).scalar_one()
            mapping_count = connection.execute(
                sa.select(sa.func.count())
                .select_from(external_identities)
                .where(
                    external_identities.c.issuer == identity.issuer,
                    external_identities.c.subject == identity.subject,
                )
            ).scalar_one()
        self.assertEqual(status, "pending")
        self.assertEqual(mapping_count, 0)

    def test_schema_constraints_and_indexes_exist(self) -> None:
        inspector = sa.inspect(self.engine)
        checks = {
            item["name"]
            for item in inspector.get_check_constraints(
                "workspace_invitations"
            )
        }
        uniques = {
            item["name"]
            for item in inspector.get_unique_constraints(
                "workspace_invitations"
            )
        }
        indexes = {
            item["name"]
            for item in inspector.get_indexes("workspace_invitations")
        }
        self.assertIn("ck_workspace_invitations_token_hash", checks)
        self.assertIn("ck_workspace_invitations_acceptance", checks)
        self.assertIn("uq_workspace_invitations_token_hash", uniques)
        self.assertIn(
            "uq_workspace_invitations_pending_target_issuer",
            indexes,
        )

    def test_admin_http_returns_token_once_and_revokes_by_id(self) -> None:
        client, previous = self._client(self.service)
        path = f"/api/organizations/{self.org_a}/invitations"
        try:
            client.cookies.set(
                SERVER_AUTH_COOKIE_NAME,
                self._session(self.admin_a),
            )
            created = client.post(
                path,
                json={
                    "user_id": self.target_a,
                    "issuer": self.issuer,
                    "expires_in_hours": 24,
                },
            )
            self.assertEqual(created.status_code, 201, created.text)
            token = created.json()["invitation_token"]
            invitation_id = created.json()["invitation_id"]
            listing = client.get(path)
            self.assertEqual(listing.status_code, 200, listing.text)
            self.assertNotIn(token, listing.text)
            self.assertNotIn("token_hash", listing.text)
            self.assertNotIn("invitation_token", listing.text)
            revoked = client.delete(f"{path}/{invitation_id}")
            self.assertEqual(revoked.status_code, 200, revoked.text)
            self.assertEqual(revoked.json()["status"], "revoked")
            self.assertEqual(
                client.delete(f"{path}/{invitation_id}").status_code,
                404,
            )
        finally:
            self._restore_client(client, previous)

    def test_admin_http_fails_closed_and_redacts_audit_error(self) -> None:
        client, previous = self._client(
            PostgresWorkspaceInvitationService(
                self.engine,
                audit=FailingAuditWriter(),
            )
        )
        path = f"/api/organizations/{self.org_a}/invitations"
        payload = {
            "user_id": self.target_a,
            "issuer": self.issuer,
            "expires_in_hours": 24,
        }
        try:
            client.cookies.set(
                SERVER_AUTH_COOKIE_NAME,
                self._session(self.member_a),
            )
            self.assertEqual(client.get(path).status_code, 403)
            client.cookies.set(
                SERVER_AUTH_COOKIE_NAME,
                self._session(self.admin_a),
            )
            self.assertEqual(
                client.get(
                    f"/api/organizations/{self.org_b}/invitations"
                ).status_code,
                403,
            )
            extra = client.post(path, json={**payload, "role": "org_admin"})
            self.assertEqual(extra.status_code, 422)
            failed = client.post(path, json=payload)
            self.assertEqual(failed.status_code, 503, failed.text)
            self.assertNotIn(PRIVATE_ERROR, failed.text)
        finally:
            self._restore_client(client, previous)

    def test_invitation_route_allowlist_is_exact(self) -> None:
        path = f"/api/organizations/{self.org_a}/invitations"
        self.assertTrue(server_http_route_available("GET", path))
        self.assertTrue(server_http_route_available("POST", path))
        self.assertTrue(
            server_http_route_available("DELETE", f"{path}/invitation-id")
        )
        self.assertFalse(server_http_route_available("PATCH", path))
        self.assertFalse(
            server_http_route_available(
                "DELETE",
                f"{path}/invitation-id/token",
            )
        )


if __name__ == "__main__":
    unittest.main()
