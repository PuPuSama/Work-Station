from __future__ import annotations

import os
import sys
import unittest
import uuid
from pathlib import Path

import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.testclient import TestClient


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from knowledge_agent import (  # noqa: E402
    KnowledgeProject,
    PostgresKnowledgeRepository,
    create_knowledge_engine,
)
from knowledge_agent.schema import projects  # noqa: E402
from server_project_http import router  # noqa: E402
from server_schema import (  # noqa: E402
    organizations,
    project_memberships,
    project_ownership,
    teams,
    workspace_users,
)
from services.access_control import (  # noqa: E402
    ActorIdentity,
    PostgresProjectAccessRepository,
    ProjectAccessDenied,
    ProjectAccessService,
)
from services.server_auth import (  # noqa: E402
    SERVER_AUTH_COOKIE_NAME,
    ServerActorSessionCodec,
)
from services.server_project_metadata import (  # noqa: E402
    PostgresServerProjectMetadata,
    ServerProjectCreationConflict,
    ServerProjectMetadataConflict,
    ServerProjectMetadataUnavailable,
)
from services.server_request_security import (  # noqa: E402
    ServerRequestSecurity,
)


DATABASE_URL_ENV = "ARTICLE_AGENT_DATABASE_URL"


class RecordingAuditWriter:
    def __init__(self) -> None:
        self.events: list[object] = []

    def append(self, connection, event) -> None:
        if not connection.in_transaction():
            raise AssertionError("audit must share the metadata transaction")
        self.events.append(event)


class FailingAuditWriter:
    def append(self, connection, event) -> None:
        del connection, event
        raise RuntimeError(
            "private audit failure https://secret.example/project"
        )


class CurrentSessionVersions:
    def is_current(self, session) -> bool:
        del session
        return True


@unittest.skipUnless(
    os.environ.get(DATABASE_URL_ENV),
    f"{DATABASE_URL_ENV} is required for PostgreSQL integration tests",
)
class ServerProjectMetadataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = create_knowledge_engine(
            os.environ[DATABASE_URL_ENV]
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    def setUp(self) -> None:
        prefix = f"m7-project-meta-{uuid.uuid4().hex}"
        self.organization_id = f"{prefix}-org"
        self.project_id = f"{prefix}.example.test"
        self.other_project_id = f"other-{prefix}.example.test"
        self.admin_id = f"{prefix}-admin"
        self.editor_id = f"{prefix}-editor"
        self.team_id = f"{prefix}-team"
        self.new_project_id = f"new-{prefix}.example.test"
        self.http_project_id = f"http-{prefix}.example.test"
        self.repository = PostgresKnowledgeRepository(self.engine)
        for project_id in (self.project_id, self.other_project_id):
            self.repository.upsert_project(
                KnowledgeProject(
                    project_id=project_id,
                    customer_name="Original Customer",
                    official_domain=project_id,
                )
            )
        with self.engine.begin() as connection:
            connection.execute(
                organizations.insert().values(
                    organization_id=self.organization_id,
                    name="Project Metadata Test Organization",
                )
            )
            connection.execute(
                workspace_users.insert(),
                (
                    {
                        "organization_id": self.organization_id,
                        "user_id": self.admin_id,
                        "display_name": "Metadata Admin",
                        "organization_role": "org_admin",
                    },
                    {
                        "organization_id": self.organization_id,
                        "user_id": self.editor_id,
                        "display_name": "Metadata Editor",
                        "organization_role": "member",
                    },
                ),
            )
            connection.execute(
                teams.insert().values(
                    organization_id=self.organization_id,
                    team_id=self.team_id,
                    name="Project Team",
                    status="active",
                )
            )
            connection.execute(
                project_ownership.insert(),
                (
                    {
                        "organization_id": self.organization_id,
                        "project_id": self.project_id,
                    },
                    {
                        "organization_id": self.organization_id,
                        "project_id": self.other_project_id,
                    },
                ),
            )
            connection.execute(
                project_memberships.insert().values(
                    organization_id=self.organization_id,
                    project_id=self.project_id,
                    user_id=self.editor_id,
                    role="editor",
                    granted_by_user_id=self.admin_id,
                )
            )
        self.admin = ActorIdentity(
            self.organization_id,
            self.admin_id,
        )
        self.editor = ActorIdentity(
            self.organization_id,
            self.editor_id,
        )
        self.audit = RecordingAuditWriter()
        self.service = PostgresServerProjectMetadata(
            self.engine,
            audit=self.audit,
        )

    def tearDown(self) -> None:
        with self.engine.begin() as connection:
            project_ids = tuple(
                connection.execute(
                    sa.select(project_ownership.c.project_id).where(
                        project_ownership.c.organization_id
                        == self.organization_id
                    )
                ).scalars()
            )
            connection.execute(
                project_memberships.delete().where(
                    project_memberships.c.organization_id
                    == self.organization_id
                )
            )
            connection.execute(
                project_ownership.delete().where(
                    project_ownership.c.organization_id
                    == self.organization_id
                )
            )
            connection.execute(
                teams.delete().where(
                    teams.c.organization_id == self.organization_id
                )
            )
            connection.execute(
                workspace_users.delete().where(
                    workspace_users.c.organization_id
                    == self.organization_id
                )
            )
            connection.execute(
                organizations.delete().where(
                    organizations.c.organization_id
                    == self.organization_id
                )
            )
            connection.execute(
                projects.delete().where(
                    projects.c.project_id.in_(project_ids)
                )
            )

    def test_admin_creates_team_owned_project_atomically(self) -> None:
        created = self.service.create(
            actor=self.admin,
            customer_name="  New   Customer  ",
            official_domain="WWW." + self.new_project_id.upper() + ".",
            owning_team_id=self.team_id,
            event_id=f"create-{uuid.uuid4().hex}",
        )
        self.assertEqual(created.project_id, self.new_project_id)
        self.assertEqual(created.official_domain, f"www.{self.new_project_id}")
        self.assertEqual(created.customer_name, "New Customer")
        self.assertEqual(created.project_notes, "")
        self.assertEqual(created.revision, 0)
        with self.engine.connect() as connection:
            owner = connection.execute(
                sa.select(
                    project_ownership.c.organization_id,
                    project_ownership.c.owning_team_id,
                ).where(
                    project_ownership.c.project_id == self.new_project_id
                )
            ).one()
        self.assertEqual(owner.organization_id, self.organization_id)
        self.assertEqual(owner.owning_team_id, self.team_id)
        self.assertEqual(self.audit.events[-1].action, "project.created")

        with self.assertRaises(ServerProjectCreationConflict):
            self.service.create(
                actor=self.admin,
                customer_name="Duplicate",
                official_domain=self.new_project_id,
                owning_team_id=None,
                event_id=f"duplicate-{uuid.uuid4().hex}",
            )
        with self.assertRaises(ProjectAccessDenied):
            self.service.create(
                actor=self.editor,
                customer_name="Forbidden",
                official_domain=self.http_project_id,
                owning_team_id=None,
                event_id=f"forbidden-{uuid.uuid4().hex}",
            )

    def test_update_uses_revision_and_redacted_atomic_audit(self) -> None:
        before = self.service.get(
            actor=self.editor,
            project_id=self.project_id,
        )
        self.assertEqual(before.revision, 0)

        updated = self.service.update(
            actor=self.admin,
            project_id=self.project_id,
            expected_revision=before.revision,
            customer_name="  Qewit   Fastener  ",
            official_domain="WWW.QEWITFASTENER.COM.",
            project_notes="  Never claim unsupported certifications.\r\n  ",
        )
        self.assertEqual(updated.customer_name, "Qewit Fastener")
        self.assertEqual(
            updated.official_domain,
            "www.qewitfastener.com",
        )
        self.assertEqual(updated.revision, 1)
        self.assertEqual(
            updated.project_notes,
            "Never claim unsupported certifications.",
        )
        self.assertEqual(len(self.audit.events), 1)
        event = self.audit.events[0]
        self.assertEqual(event.action, "project.metadata.updated")
        self.assertEqual(
            event.details,
            {
                "from_revision": 0,
                "to_revision": 1,
                "customer_name_changed": True,
                "official_domain_changed": True,
                "project_notes_changed": True,
            },
        )
        self.assertNotIn("Qewit", str(event))
        self.assertNotIn("qewitfastener", str(event))
        self.assertNotIn("certifications", str(event))

        same = self.service.update(
            actor=self.admin,
            project_id=self.project_id,
            expected_revision=1,
            customer_name="Qewit Fastener",
            official_domain="www.qewitfastener.com",
            project_notes="Never claim unsupported certifications.",
        )
        self.assertEqual(same, updated)
        self.assertEqual(len(self.audit.events), 1)
        with self.assertRaises(ServerProjectMetadataConflict):
            self.service.update(
                actor=self.admin,
                project_id=self.project_id,
                expected_revision=0,
                customer_name="Stale Name",
                official_domain="stale.example.test",
                project_notes="",
            )

    def test_repository_upsert_only_advances_revision_on_change(self) -> None:
        unchanged = KnowledgeProject(
            project_id=self.project_id,
            customer_name="Original Customer",
            official_domain=self.project_id,
        )
        self.repository.upsert_project(unchanged)
        with self.engine.connect() as connection:
            same_revision = connection.execute(
                sa.select(projects.c.revision).where(
                    projects.c.project_id == self.project_id
                )
            ).scalar_one()
        self.assertEqual(same_revision, 0)

        self.repository.upsert_project(
            KnowledgeProject(
                project_id=self.project_id,
                customer_name="Repository Update",
                official_domain="repository.example.test",
            )
        )
        with self.engine.connect() as connection:
            changed_revision = connection.execute(
                sa.select(projects.c.revision).where(
                    projects.c.project_id == self.project_id
                )
            ).scalar_one()
        self.assertEqual(changed_revision, 1)

    def test_schema_exposes_nonnegative_project_revision(self) -> None:
        inspector = sa.inspect(self.engine)
        columns = {
            column["name"]: column
            for column in inspector.get_columns("projects")
        }
        checks = {
            item["name"]: item["sqltext"]
            for item in inspector.get_check_constraints("projects")
        }
        self.assertFalse(columns["revision"]["nullable"])
        self.assertFalse(columns["project_notes"]["nullable"])
        self.assertEqual(str(columns["revision"]["default"]), "0")
        self.assertIn("ck_projects_revision_nonnegative", checks)
        self.assertIn(
            "revision >= 0",
            checks["ck_projects_revision_nonnegative"],
        )

    def test_editor_cross_project_and_invalid_input_fail_closed(self) -> None:
        with self.assertRaises(ProjectAccessDenied):
            self.service.update(
                actor=self.editor,
                project_id=self.project_id,
                expected_revision=0,
                customer_name="Forbidden",
                official_domain=self.project_id,
                project_notes="",
            )
        with self.assertRaises(ProjectAccessDenied):
            self.service.get(
                actor=self.editor,
                project_id=self.other_project_id,
            )
        with self.assertRaisesRegex(ValueError, "square brackets"):
            self.service.update(
                actor=self.admin,
                project_id=self.project_id,
                expected_revision=0,
                customer_name="[Forged](brand)",
                official_domain=self.project_id,
                project_notes="",
            )
        with self.assertRaisesRegex(ValueError, "hostname"):
            self.service.update(
                actor=self.admin,
                project_id=self.project_id,
                expected_revision=0,
                customer_name="Valid Brand",
                official_domain="https://user:secret@example.test/path",
                project_notes="",
            )
        self.assertEqual(self.audit.events, [])

    def test_audit_failure_rolls_back_metadata_and_redacts_error(self) -> None:
        service = PostgresServerProjectMetadata(
            self.engine,
            audit=FailingAuditWriter(),
        )
        with self.assertRaises(
            ServerProjectMetadataUnavailable
        ) as captured:
            service.update(
                actor=self.admin,
                project_id=self.project_id,
                expected_revision=0,
                customer_name="Must Roll Back",
                official_domain="rollback.example.test",
                project_notes="secret operator note",
            )
        self.assertNotIn("secret.example", str(captured.exception))
        current = self.service.get(
            actor=self.admin,
            project_id=self.project_id,
        )
        self.assertEqual(current.customer_name, "Original Customer")
        self.assertEqual(current.official_domain, self.project_id)
        self.assertEqual(current.revision, 0)

    def test_http_is_strict_and_local_mode_does_not_mount_capability(
        self,
    ) -> None:
        codec = ServerActorSessionCodec(b"m" * 32)
        app = FastAPI()
        app.state.server_mode_enabled = True
        app.state.server_project_metadata = self.service
        app.state.server_request_security = ServerRequestSecurity(
            codec=codec,
            access=ProjectAccessService(
                PostgresProjectAccessRepository(self.engine)
            ),
            sessions=CurrentSessionVersions(),
        )
        app.include_router(router)
        with TestClient(app) as client:
            client.cookies.set(
                SERVER_AUTH_COOKIE_NAME,
                codec.create(self.admin),
            )
            fetched = client.get(
                f"/api/projects/{self.project_id}/metadata"
            )
            rejected = client.put(
                f"/api/projects/{self.project_id}/metadata",
                json={
                    "revision": 0,
                    "customer_name": "Injected",
                    "official_domain": self.project_id,
                    "project_id": self.other_project_id,
                    "project_context": "must not be accepted",
                },
            )
            updated = client.put(
                f"/api/projects/{self.project_id}/metadata",
                json={
                    "revision": 0,
                    "customer_name": "HTTP Brand",
                    "official_domain": "brand.example.test",
                    "project_notes": "Use only verified product claims.",
                },
            )
            created = client.post(
                "/api/projects",
                json={
                    "customer_name": "HTTP Project",
                    "official_domain": self.http_project_id,
                    "owning_team_id": self.team_id,
                },
            )

        self.assertEqual(fetched.status_code, 200, fetched.text)
        self.assertEqual(rejected.status_code, 422, rejected.text)
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["revision"], 1)
        self.assertEqual(
            updated.json()["project_notes"],
            "Use only verified product claims.",
        )
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.json()["project_id"], self.http_project_id)
        self.assertEqual(created.json()["effective_role"], "org_admin")

        local = FastAPI()
        local.state.server_mode_enabled = False
        local.state.server_project_metadata = self.service
        local.include_router(router)
        with TestClient(local) as client:
            response = client.get(
                f"/api/projects/{self.project_id}/metadata"
            )
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
