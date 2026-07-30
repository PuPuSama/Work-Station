from __future__ import annotations

import os
import sys
import unittest
import uuid
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError, IntegrityError


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from knowledge_agent.contracts import KnowledgeProject  # noqa: E402
from knowledge_agent.database import create_knowledge_engine  # noqa: E402
from knowledge_agent.repository import PostgresKnowledgeRepository  # noqa: E402
from knowledge_agent.schema import projects  # noqa: E402
from server_schema import (  # noqa: E402
    audit_events,
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
    ProjectAccessDenied,
    ProjectAccessService,
)
from services.audit_log import (  # noqa: E402
    AuditEvent,
    PostgresAuditEventWriter,
)


DATABASE_URL_ENV = "ARTICLE_AGENT_DATABASE_URL"


@unittest.skipUnless(
    os.environ.get(DATABASE_URL_ENV),
    f"{DATABASE_URL_ENV} is required for PostgreSQL integration tests",
)
class M7AccessControlPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = create_knowledge_engine(os.environ[DATABASE_URL_ENV])
        cls.knowledge = PostgresKnowledgeRepository(cls.engine)
        cls.access = ProjectAccessService(
            PostgresProjectAccessRepository(cls.engine)
        )
        cls.audit = PostgresAuditEventWriter()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    def setUp(self) -> None:
        self.prefix = f"m7-{uuid.uuid4().hex}"
        self.org_a = f"{self.prefix}-org-a"
        self.org_b = f"{self.prefix}-org-b"
        self.team_a = f"{self.prefix}-team-a"
        self.project_a = f"{self.prefix}-project-a"
        self.project_b = f"{self.prefix}-project-b"
        self.unbound_project = f"{self.prefix}-legacy"
        self.user_ids = {
            "admin": f"{self.prefix}-admin",
            "lead": f"{self.prefix}-lead",
            "member": f"{self.prefix}-member",
            "editor": f"{self.prefix}-editor",
            "reviewer": f"{self.prefix}-reviewer",
            "viewer": f"{self.prefix}-viewer",
            "disabled": f"{self.prefix}-disabled",
            "other_admin": f"{self.prefix}-other-admin",
        }
        self._seed()

    def tearDown(self) -> None:
        project_ids = (self.project_a, self.project_b, self.unbound_project)
        organization_ids = (self.org_a, self.org_b)
        with self.engine.begin() as connection:
            connection.execute(
                project_memberships.delete().where(
                    project_memberships.c.organization_id.in_(organization_ids)
                )
            )
            connection.execute(
                team_memberships.delete().where(
                    team_memberships.c.organization_id.in_(organization_ids)
                )
            )
            connection.execute(
                project_ownership.delete().where(
                    project_ownership.c.organization_id.in_(organization_ids)
                )
            )
            connection.execute(
                teams.delete().where(
                    teams.c.organization_id.in_(organization_ids)
                )
            )
            connection.execute(
                workspace_users.delete().where(
                    workspace_users.c.organization_id.in_(organization_ids)
                )
            )
            connection.execute(
                organizations.delete().where(
                    organizations.c.organization_id.in_(organization_ids)
                )
            )
            connection.execute(
                projects.delete().where(projects.c.project_id.in_(project_ids))
            )

    def _seed(self) -> None:
        for project_id, customer in (
            (self.project_a, "Project A"),
            (self.project_b, "Project B"),
            (self.unbound_project, "Legacy project"),
        ):
            self.knowledge.upsert_project(
                KnowledgeProject(
                    project_id=project_id,
                    customer_name=customer,
                    official_domain=f"{project_id}.example.test",
                )
            )

        users_a = (
            ("admin", "org_admin", "active"),
            ("lead", "member", "active"),
            ("member", "member", "active"),
            ("editor", "member", "active"),
            ("reviewer", "member", "active"),
            ("viewer", "member", "active"),
            ("disabled", "org_admin", "disabled"),
        )
        with self.engine.begin() as connection:
            connection.execute(
                organizations.insert(),
                (
                    {
                        "organization_id": self.org_a,
                        "name": "M7 organization A",
                    },
                    {
                        "organization_id": self.org_b,
                        "name": "M7 organization B",
                    },
                ),
            )
            connection.execute(
                workspace_users.insert(),
                tuple(
                    {
                        "organization_id": self.org_a,
                        "user_id": self.user_ids[key],
                        "display_name": key.title(),
                        "organization_role": role,
                        "status": status,
                    }
                    for key, role, status in users_a
                )
                + (
                    {
                        "organization_id": self.org_b,
                        "user_id": self.user_ids["other_admin"],
                        "display_name": "Other admin",
                        "organization_role": "org_admin",
                        "status": "active",
                    },
                ),
            )
            connection.execute(
                teams.insert().values(
                    organization_id=self.org_a,
                    team_id=self.team_a,
                    name="M7 team A",
                    manager_user_id=self.user_ids["lead"],
                )
            )
            connection.execute(
                team_memberships.insert(),
                (
                    {
                        "organization_id": self.org_a,
                        "team_id": self.team_a,
                        "user_id": self.user_ids["lead"],
                        "role": "team_lead",
                        "granted_by_user_id": self.user_ids["admin"],
                    },
                    {
                        "organization_id": self.org_a,
                        "team_id": self.team_a,
                        "user_id": self.user_ids["member"],
                        "role": "member",
                        "granted_by_user_id": self.user_ids["admin"],
                    },
                ),
            )
            connection.execute(
                project_ownership.insert(),
                (
                    {
                        "project_id": self.project_a,
                        "organization_id": self.org_a,
                        "owning_team_id": self.team_a,
                    },
                    {
                        "project_id": self.project_b,
                        "organization_id": self.org_b,
                        "owning_team_id": None,
                    },
                ),
            )
            connection.execute(
                project_memberships.insert(),
                tuple(
                    {
                        "organization_id": self.org_a,
                        "project_id": self.project_a,
                        "user_id": self.user_ids[role],
                        "role": role,
                        "granted_by_user_id": self.user_ids["admin"],
                    }
                    for role in ("editor", "reviewer", "viewer")
                ),
            )

    def _actor(self, key: str, organization_id: str | None = None) -> ActorIdentity:
        return ActorIdentity(
            organization_id or self.org_a,
            self.user_ids[key],
        )

    def test_roles_are_resolved_from_database(self) -> None:
        cases = (
            ("admin", "project.delete", True, "org_admin"),
            ("lead", "project.members.manage", True, "team_lead"),
            ("lead", "knowledge.delete", False, "team_lead"),
            ("member", "project.view", False, None),
            ("editor", "article.deliver", True, "editor"),
            ("editor", "project.members.manage", False, "editor"),
            ("reviewer", "article.review", True, "reviewer"),
            ("reviewer", "article.edit", False, "reviewer"),
            ("viewer", "project.view", True, "viewer"),
            ("viewer", "article.review", False, "viewer"),
        )
        for key, permission, allowed, effective_role in cases:
            with self.subTest(key=key, permission=permission):
                decision = self.access.decide(
                    self._actor(key),
                    self.project_a,
                    permission,  # type: ignore[arg-type]
                )
                self.assertEqual(decision.allowed, allowed)
                self.assertEqual(decision.effective_role, effective_role)

    def test_cross_organization_disabled_and_unbound_projects_fail_closed(
        self,
    ) -> None:
        cases = (
            (self._actor("admin"), self.project_b),
            (self._actor("other_admin", self.org_b), self.project_a),
            (self._actor("disabled"), self.project_a),
            (self._actor("admin"), self.unbound_project),
        )
        for actor, project_id in cases:
            with self.subTest(actor=actor.user_id, project_id=project_id):
                decision = self.access.decide(
                    actor,
                    project_id,
                    "project.view",
                )
                self.assertFalse(decision.allowed)
                self.assertIsNone(decision.effective_role)
                with self.assertRaisesRegex(
                    ProjectAccessDenied,
                    "^project access denied$",
                ):
                    self.access.require(actor, project_id, "project.view")

    def test_database_rejects_cross_organization_membership(self) -> None:
        connection = self.engine.connect()
        transaction = connection.begin()
        try:
            with self.assertRaises(IntegrityError):
                connection.execute(
                    project_memberships.insert().values(
                        organization_id=self.org_b,
                        project_id=self.project_a,
                        user_id=self.user_ids["other_admin"],
                        role="viewer",
                        granted_by_user_id=self.user_ids["other_admin"],
                    )
                )
        finally:
            transaction.rollback()
            connection.close()

    def test_schema_contains_scoped_constraints_and_indexes(self) -> None:
        inspector = sa.inspect(self.engine)
        self.assertTrue(
            {
                "organizations",
                "workspace_users",
                "teams",
                "team_memberships",
                "project_ownership",
                "project_memberships",
                "audit_events",
            }.issubset(inspector.get_table_names())
        )
        with self.engine.connect() as connection:
            constraint_names = set(
                connection.execute(
                    sa.text(
                        """
                        SELECT conname
                        FROM pg_constraint
                        WHERE conrelid IN (
                            'organizations'::regclass,
                            'workspace_users'::regclass,
                            'teams'::regclass,
                            'team_memberships'::regclass,
                            'project_ownership'::regclass,
                            'project_memberships'::regclass,
                            'audit_events'::regclass
                        )
                        """
                    )
                ).scalars()
            )
            trigger_names = set(
                connection.execute(
                    sa.text(
                        """
                        SELECT tgname
                        FROM pg_trigger
                        WHERE tgrelid = 'audit_events'::regclass
                          AND NOT tgisinternal
                        """
                    )
                ).scalars()
            )
        self.assertTrue(
            {
                "pk_workspace_users",
                "fk_teams_manager",
                "pk_team_memberships",
                "fk_team_memberships_user",
                "uq_project_ownership_organization_project",
                "fk_project_ownership_team",
                "pk_project_memberships",
                "fk_project_memberships_project",
                "fk_project_memberships_user",
                "pk_audit_events",
                "fk_audit_events_actor",
                "fk_audit_events_project",
            }.issubset(constraint_names)
        )
        self.assertIn("trg_audit_events_append_only", trigger_names)

    def test_audit_events_are_append_only(self) -> None:
        event_id = f"{self.prefix}-event"
        connection = self.engine.connect()
        transaction = connection.begin()
        try:
            self.audit.append(
                connection,
                AuditEvent(
                    organization_id=self.org_a,
                    event_id=event_id,
                    actor_user_id=self.user_ids["admin"],
                    project_id=self.project_a,
                    action="project.membership.granted",
                    target_type="project_membership",
                    target_id=self.user_ids["editor"],
                    details={"role": "editor"},
                ),
            )

            update_savepoint = connection.begin_nested()
            with self.assertRaisesRegex(DBAPIError, "append-only"):
                connection.execute(
                    audit_events.update()
                    .where(
                        audit_events.c.organization_id == self.org_a,
                        audit_events.c.event_id == event_id,
                    )
                    .values(action="tampered")
                )
            update_savepoint.rollback()

            delete_savepoint = connection.begin_nested()
            with self.assertRaisesRegex(DBAPIError, "append-only"):
                connection.execute(
                    audit_events.delete().where(
                        audit_events.c.organization_id == self.org_a,
                        audit_events.c.event_id == event_id,
                    )
                )
            delete_savepoint.rollback()
        finally:
            transaction.rollback()
            connection.close()


if __name__ == "__main__":
    unittest.main()
