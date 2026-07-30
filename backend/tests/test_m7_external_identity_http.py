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
from server_schema import (  # noqa: E402
    audit_events,
    external_identities,
    organizations,
    workspace_users,
)
from services.access_control import ActorIdentity  # noqa: E402
from services.actor_sessions import PostgresActorSessionRepository  # noqa: E402
from services.external_identity_provisioning import (  # noqa: E402
    PostgresExternalIdentityProvisioningService,
)
from services.server_auth import (  # noqa: E402
    SERVER_AUTH_COOKIE_NAME,
    ServerActorSessionCodec,
)
from services.server_request_security import (  # noqa: E402
    ServerRequestSecurity,
    server_http_route_available,
)


PRIVATE_AUDIT_ERROR = "private-external-identity-audit-body"


class FailingAuditWriter:
    def append(self, connection, event) -> None:
        raise RuntimeError(PRIVATE_AUDIT_ERROR)


@unittest.skipUnless(
    os.environ.get("ARTICLE_AGENT_DATABASE_URL"),
    "ARTICLE_AGENT_DATABASE_URL is required for PostgreSQL integration tests",
)
class ExternalIdentityHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = create_knowledge_engine(
            os.environ["ARTICLE_AGENT_DATABASE_URL"]
        )
        cls.codec = ServerActorSessionCodec(b"i" * 32)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    def setUp(self) -> None:
        prefix = f"m7-identity-http-{uuid.uuid4().hex}"
        self.org_a = f"{prefix}-org-a"
        self.org_b = f"{prefix}-org-b"
        self.admin_a = f"{prefix}-admin-a"
        self.member_a = f"{prefix}-member-a"
        self.target_a = f"{prefix}-target-a"
        self.admin_b = f"{prefix}-admin-b"
        self.issuer = f"https://idp-{uuid.uuid4().hex}.example.test"
        self.subject_one = f"{prefix}-private-subject-one"
        self.subject_two = f"{prefix}-private-subject-two"
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
                    self._user(self.org_a, self.target_a, "Target A", "member"),
                    self._user(self.org_b, self.admin_b, "Admin B", "org_admin"),
                ),
            )
            connection.execute(
                external_identities.insert(),
                (
                    {
                        "issuer": self.issuer,
                        "subject": self.subject_one,
                        "organization_id": self.org_a,
                        "user_id": self.member_a,
                    },
                    {
                        "issuer": self.issuer,
                        "subject": self.subject_two,
                        "organization_id": self.org_a,
                        "user_id": self.target_a,
                    },
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
        }

    def _token(self, organization_id: str, user_id: str) -> str:
        return self.codec.create(
            ActorIdentity(organization_id, user_id),
            session_version=1,
        )

    def _client(
        self,
        service: PostgresExternalIdentityProvisioningService,
    ) -> tuple[TestClient, tuple[object, object, object]]:
        import app as app_module

        previous = (
            getattr(app_module.app.state, "server_mode_enabled", None),
            getattr(app_module.app.state, "server_request_security", None),
            getattr(
                app_module.app.state,
                "server_external_identity_provisioning",
                None,
            ),
        )
        app_module.app.state.server_mode_enabled = True
        app_module.app.state.server_request_security = ServerRequestSecurity(
            codec=self.codec,
            access=object(),  # type: ignore[arg-type]
            sessions=PostgresActorSessionRepository(self.engine),
        )
        app_module.app.state.server_external_identity_provisioning = service
        return TestClient(app_module.app), previous

    @staticmethod
    def _restore(
        client: TestClient,
        previous: tuple[object, object, object],
    ) -> None:
        import app as app_module

        client.close()
        (
            app_module.app.state.server_mode_enabled,
            app_module.app.state.server_request_security,
            app_module.app.state.server_external_identity_provisioning,
        ) = previous

    def test_directory_is_paginated_and_never_returns_subject(self) -> None:
        client, previous = self._client(
            PostgresExternalIdentityProvisioningService(self.engine)
        )
        path = (
            f"/api/organizations/{self.org_a}/external-identities"
        )
        try:
            client.cookies.set(
                SERVER_AUTH_COOKIE_NAME,
                self._token(self.org_a, self.admin_a),
            )
            first = client.get(path, params={"limit": 1})
            self.assertEqual(first.status_code, 200, first.text)
            cursor = first.json()["next_after_mapping_id"]
            self.assertEqual(len(cursor), 64)
            second = client.get(
                path,
                params={"limit": 100, "after_mapping_id": cursor},
            )
            self.assertEqual(second.status_code, 200, second.text)
            items = first.json()["items"] + second.json()["items"]
            self.assertEqual(len(items), 2)
            self.assertTrue(
                all(len(item["mapping_id"]) == 64 for item in items)
            )
            self.assertNotIn("subject", first.text.lower())
            self.assertNotIn(self.subject_one, first.text + second.text)
            self.assertNotIn(self.subject_two, first.text + second.text)
        finally:
            self._restore(client, previous)

    def test_link_is_idempotent_and_revoke_uses_only_mapping_id(
        self,
    ) -> None:
        client, previous = self._client(
            PostgresExternalIdentityProvisioningService(self.engine)
        )
        path = (
            f"/api/organizations/{self.org_a}/external-identities"
        )
        subject = f"{self.subject_one}-new-secret"
        issuer = f"{self.issuer}/secondary"
        payload = {
            "issuer": issuer,
            "subject": subject,
            "user_id": self.target_a,
        }
        try:
            client.cookies.set(
                SERVER_AUTH_COOKIE_NAME,
                self._token(self.org_a, self.admin_a),
            )
            created = client.post(path, json=payload)
            self.assertEqual(created.status_code, 201, created.text)
            mapping_id = created.json()["mapping_id"]
            self.assertNotIn(subject, created.text)
            repeated = client.post(path, json=payload)
            self.assertEqual(repeated.status_code, 201, repeated.text)
            self.assertEqual(repeated.json()["mapping_id"], mapping_id)
            with self.engine.connect() as connection:
                link_audits = connection.execute(
                    sa.select(sa.func.count())
                    .select_from(audit_events)
                    .where(
                        audit_events.c.organization_id == self.org_a,
                        audit_events.c.action == "external_identity.link",
                        audit_events.c.target_id == mapping_id,
                    )
                ).scalar_one()
            self.assertEqual(link_audits, 1)

            revoked = client.delete(f"{path}/{mapping_id}")
            self.assertEqual(revoked.status_code, 200, revoked.text)
            self.assertEqual(revoked.json()["status"], "revoked")
            self.assertNotIn(subject, revoked.text)
            repeated_revoke = client.delete(f"{path}/{mapping_id}")
            self.assertEqual(repeated_revoke.status_code, 404)
            self.assertNotIn(subject, repeated_revoke.text)
        finally:
            self._restore(client, previous)

    def test_non_admin_cross_org_and_invalid_input_fail_closed(self) -> None:
        client, previous = self._client(
            PostgresExternalIdentityProvisioningService(self.engine)
        )
        path = (
            f"/api/organizations/{self.org_a}/external-identities"
        )
        try:
            client.cookies.set(
                SERVER_AUTH_COOKIE_NAME,
                self._token(self.org_a, self.member_a),
            )
            self.assertEqual(client.get(path).status_code, 403)
            client.cookies.set(
                SERVER_AUTH_COOKIE_NAME,
                self._token(self.org_a, self.admin_a),
            )
            self.assertEqual(
                client.get(
                    f"/api/organizations/{self.org_b}/external-identities"
                ).status_code,
                403,
            )
            self.assertEqual(
                client.get(
                    path,
                    params={"after_mapping_id": "not-a-hash"},
                ).status_code,
                422,
            )
            self.assertEqual(
                client.delete(f"{path}/not-a-hash").status_code,
                422,
            )
            invalid = client.post(
                path,
                json={
                    "issuer": "http://public.example.test",
                    "subject": "private",
                    "user_id": self.target_a,
                },
            )
            self.assertEqual(invalid.status_code, 422)
            extra = client.post(
                path,
                json={
                    "issuer": self.issuer,
                    "subject": "private",
                    "user_id": self.target_a,
                    "role": "org_admin",
                },
            )
            self.assertEqual(extra.status_code, 422)
        finally:
            self._restore(client, previous)

    def test_audit_failure_rolls_back_and_redacts_subject(self) -> None:
        client, previous = self._client(
            PostgresExternalIdentityProvisioningService(
                self.engine,
                audit=FailingAuditWriter(),
            )
        )
        path = (
            f"/api/organizations/{self.org_a}/external-identities"
        )
        secret_subject = f"{self.subject_one}-rollback-secret"
        issuer = f"{self.issuer}/rollback"
        try:
            client.cookies.set(
                SERVER_AUTH_COOKIE_NAME,
                self._token(self.org_a, self.admin_a),
            )
            response = client.post(
                path,
                json={
                    "issuer": issuer,
                    "subject": secret_subject,
                    "user_id": self.target_a,
                },
            )
            self.assertEqual(response.status_code, 503, response.text)
            self.assertNotIn(secret_subject, response.text)
            self.assertNotIn(PRIVATE_AUDIT_ERROR, response.text)
            with self.engine.connect() as connection:
                count = connection.execute(
                    sa.select(sa.func.count())
                    .select_from(external_identities)
                    .where(
                        external_identities.c.issuer == issuer,
                        external_identities.c.subject == secret_subject,
                    )
                ).scalar_one()
            self.assertEqual(count, 0)
        finally:
            self._restore(client, previous)

    def test_server_route_allowlist_is_exact(self) -> None:
        base = (
            f"/api/organizations/{self.org_a}/external-identities"
        )
        self.assertTrue(server_http_route_available("GET", base))
        self.assertTrue(server_http_route_available("POST", base))
        self.assertTrue(
            server_http_route_available("DELETE", f"{base}/mapping-id")
        )
        self.assertFalse(server_http_route_available("PATCH", base))
        self.assertFalse(
            server_http_route_available(
                "DELETE",
                f"{base}/mapping-id/subject",
            )
        )


if __name__ == "__main__":
    unittest.main()
