from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import os
import unittest
import uuid

import sqlalchemy as sa

from knowledge_agent.database import create_knowledge_engine
from knowledge_agent.schema import projects
from server_schema import (
    audit_events,
    external_identities,
    organizations,
    project_ownership,
    team_memberships,
    teams,
    workspace_invitations,
    workspace_users,
)
from services.first_admin_bootstrap import (
    FirstAdminBootstrapConflict,
    FirstAdminBootstrapRequest,
    FirstAdminBootstrapService,
    FirstAdminBootstrapUnavailable,
)
from services.workspace_invitations import (
    PostgresWorkspaceInvitationService,
)
from services.external_identity import VerifiedExternalIdentity


class FailingAuditWriter:
    def append(self, connection, event) -> None:
        raise RuntimeError("private-bootstrap-audit-failure")


@unittest.skipUnless(
    os.environ.get("ARTICLE_AGENT_DATABASE_URL"),
    "ARTICLE_AGENT_DATABASE_URL is required for PostgreSQL integration tests",
)
class FirstAdminBootstrapPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = create_knowledge_engine(
            os.environ["ARTICLE_AGENT_DATABASE_URL"]
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    def setUp(self) -> None:
        prefix = f"m7-bootstrap-{uuid.uuid4().hex}"
        self.organization_id = f"{prefix}-org"
        self.user_id = f"{prefix}-admin"
        self.team_id = f"{prefix}-team"
        self.project_id = f"{prefix}.example.test"
        self.issuer = f"https://{prefix}.example.test"
        self.token = f"token-{uuid.uuid4().hex}"
        with self.engine.begin() as connection:
            connection.execute(
                projects.insert().values(
                    project_id=self.project_id,
                    customer_name="Bootstrap Project",
                    official_domain=self.project_id,
                    status="active",
                )
            )

    def _request(self) -> FirstAdminBootstrapRequest:
        return FirstAdminBootstrapRequest(
            organization_id=self.organization_id,
            organization_name="Bootstrap Organization",
            user_id=self.user_id,
            display_name="First Administrator",
            team_id=self.team_id,
            team_name="Bootstrap Team",
            project_id=self.project_id,
            issuer=self.issuer,
            invitation_token=self.token,
        )

    def test_bootstrap_is_idempotent_and_invitation_redeems(self) -> None:
        service = FirstAdminBootstrapService(
            self.engine,
            invitation_id_factory=lambda: f"inv_{uuid.uuid4().hex}",
        )
        now = datetime.now(timezone.utc)

        created = service.bootstrap(self._request(), now=now)
        pending = service.bootstrap(self._request(), now=now)

        self.assertEqual(created.state, "created")
        self.assertEqual(pending.state, "pending")
        self.assertEqual(created.invitation_id, pending.invitation_id)
        self.assertNotIn(self.token, repr(created))
        with self.engine.connect() as connection:
            invitation = connection.execute(
                sa.select(
                    workspace_invitations.c.token_hash,
                    workspace_invitations.c.status,
                ).where(
                    workspace_invitations.c.organization_id
                    == self.organization_id
                )
            ).mappings().one()
            self.assertEqual(
                invitation["token_hash"],
                hashlib.sha256(self.token.encode("utf-8")).hexdigest(),
            )
            self.assertEqual(invitation["status"], "pending")
            self.assertEqual(
                connection.execute(
                    sa.select(sa.func.count())
                    .select_from(audit_events)
                    .where(
                        audit_events.c.organization_id
                        == self.organization_id
                    )
                ).scalar_one(),
                2,
            )

        PostgresWorkspaceInvitationService(self.engine).redeem(
            invitation_token=self.token,
            identity=VerifiedExternalIdentity(
                self.issuer,
                f"subject-{uuid.uuid4().hex}",
            ),
            event_id=f"accept-{uuid.uuid4().hex}",
        )
        accepted = service.bootstrap(self._request(), now=now)
        self.assertEqual(accepted.state, "accepted")

    def test_audit_failure_rolls_back_every_bootstrap_row(self) -> None:
        service = FirstAdminBootstrapService(
            self.engine,
            audit=FailingAuditWriter(),
        )

        with self.assertRaises(FirstAdminBootstrapUnavailable) as raised:
            service.bootstrap(self._request())
        self.assertNotIn("private-bootstrap-audit-failure", str(raised.exception))

        with self.engine.connect() as connection:
            checks = (
                (organizations, organizations.c.organization_id),
                (workspace_users, workspace_users.c.organization_id),
                (teams, teams.c.organization_id),
                (team_memberships, team_memberships.c.organization_id),
                (project_ownership, project_ownership.c.organization_id),
                (
                    workspace_invitations,
                    workspace_invitations.c.organization_id,
                ),
            )
            for table, column in checks:
                self.assertEqual(
                    connection.execute(
                        sa.select(sa.func.count())
                        .select_from(table)
                        .where(column == self.organization_id)
                    ).scalar_one(),
                    0,
                )

    def test_existing_identity_for_issuer_blocks_bootstrap(self) -> None:
        existing_org = f"existing-{uuid.uuid4().hex}"
        existing_user = f"existing-{uuid.uuid4().hex}"
        with self.engine.begin() as connection:
            connection.execute(
                organizations.insert().values(
                    organization_id=existing_org,
                    name="Existing Organization",
                )
            )
            connection.execute(
                workspace_users.insert().values(
                    organization_id=existing_org,
                    user_id=existing_user,
                    display_name="Existing User",
                )
            )
            connection.execute(
                external_identities.insert().values(
                    issuer=self.issuer,
                    subject=f"subject-{uuid.uuid4().hex}",
                    organization_id=existing_org,
                    user_id=existing_user,
                )
            )

        with self.assertRaises(FirstAdminBootstrapConflict):
            FirstAdminBootstrapService(self.engine).bootstrap(self._request())
        with self.engine.connect() as connection:
            self.assertIsNone(
                connection.execute(
                    sa.select(organizations.c.organization_id).where(
                        organizations.c.organization_id
                        == self.organization_id
                    )
                ).scalar_one_or_none()
            )


if __name__ == "__main__":
    unittest.main()
