from __future__ import annotations

import os
import sys
import tempfile
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import sqlalchemy as sa
from fastapi.testclient import TestClient


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from knowledge_agent.database import create_knowledge_engine  # noqa: E402
from knowledge_agent.schema import projects  # noqa: E402
from server_schema import (  # noqa: E402
    organizations,
    project_memberships,
    project_ownership,
    project_prompt_defaults,
    project_prompt_heads,
    project_prompt_versions,
    workspace_users,
)
from services.access_control import (  # noqa: E402
    ActorIdentity,
    ProjectAccessDenied,
)
from services.server_project_prompts import (  # noqa: E402
    PostgresProjectPromptService,
    ServerProjectPromptConflict,
    ServerProjectPromptError,
    ServerProjectPromptUnavailable,
    ServerProjectPromptServiceFactory,
)
from services.project_prompts import ProjectPromptRepository  # noqa: E402
from services.server_project_prompt_migration import (  # noqa: E402
    ProjectPromptMigrationConflict,
    ProjectPromptMigrationUnavailable,
    migrate_project_prompts,
)
from services.server_auth import (  # noqa: E402
    SERVER_AUTH_COOKIE_NAME,
    ServerActorSessionCodec,
)


class RecordingAuditWriter:
    def __init__(self) -> None:
        self.events: list[object] = []

    def append(self, connection, event) -> None:
        if not connection.in_transaction():
            raise AssertionError("audit must share the prompt transaction")
        self.events.append(event)


class FailingAuditWriter:
    def append(self, connection, event) -> None:
        del connection, event
        raise RuntimeError("private injected audit failure")


@unittest.skipUnless(
    os.environ.get("ARTICLE_AGENT_DATABASE_URL"),
    "ARTICLE_AGENT_DATABASE_URL is required for PostgreSQL integration tests",
)
class ServerProjectPromptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = create_knowledge_engine(
            os.environ["ARTICLE_AGENT_DATABASE_URL"]
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    def setUp(self) -> None:
        prefix = f"m7-prompt-{uuid.uuid4().hex}"
        self.organization_id = f"{prefix}-org"
        self.project_id = f"{prefix}.example.test"
        self.other_project_id = f"other-{prefix}.example.test"
        self.editor_id = f"{prefix}-editor"
        self.viewer_id = f"{prefix}-viewer"
        with self.engine.begin() as connection:
            connection.execute(
                organizations.insert().values(
                    organization_id=self.organization_id,
                    name="Prompt Test Organization",
                )
            )
            connection.execute(
                workspace_users.insert(),
                [
                    {
                        "organization_id": self.organization_id,
                        "user_id": self.editor_id,
                        "display_name": "Prompt Editor",
                    },
                    {
                        "organization_id": self.organization_id,
                        "user_id": self.viewer_id,
                        "display_name": "Prompt Viewer",
                    },
                ],
            )
            connection.execute(
                projects.insert(),
                [
                    {
                        "project_id": self.project_id,
                        "customer_name": "Prompt Project",
                        "official_domain": self.project_id,
                    },
                    {
                        "project_id": self.other_project_id,
                        "customer_name": "Other Prompt Project",
                        "official_domain": self.other_project_id,
                    },
                ],
            )
            connection.execute(
                project_ownership.insert(),
                [
                    {
                        "organization_id": self.organization_id,
                        "project_id": self.project_id,
                    },
                    {
                        "organization_id": self.organization_id,
                        "project_id": self.other_project_id,
                    },
                ],
            )
            connection.execute(
                project_memberships.insert(),
                [
                    {
                        "organization_id": self.organization_id,
                        "project_id": self.project_id,
                        "user_id": self.editor_id,
                        "role": "editor",
                        "granted_by_user_id": self.editor_id,
                    },
                    {
                        "organization_id": self.organization_id,
                        "project_id": self.project_id,
                        "user_id": self.viewer_id,
                        "role": "viewer",
                        "granted_by_user_id": self.editor_id,
                    },
                ],
            )
        self.editor = ActorIdentity(
            self.organization_id,
            self.editor_id,
        )
        self.viewer = ActorIdentity(
            self.organization_id,
            self.viewer_id,
        )
        self.audit = RecordingAuditWriter()
        self.service = PostgresProjectPromptService(
            self.engine,
            organization_id=self.organization_id,
            project_id=self.project_id,
            audit=self.audit,
        )

    def _legacy_source(
        self,
        directory: str,
    ) -> tuple[ProjectPromptRepository, str]:
        customer = self.project_id
        source = ProjectPromptRepository(
            Path(directory) / "legacy-tasks.json"
        )
        outline = source.create(
            customer,
            "Legacy outline",
            "outline",
            "Legacy outline version one.",
        )
        source.update(
            customer,
            outline.id,
            "Legacy outline v2",
            "Legacy outline version two.",
        )
        article = source.create(
            customer,
            "Archived article",
            "article",
            "Archived article prompt.",
        )
        source.set_active(customer, article.id, False)
        source.set_defaults(customer, outline.id, "")
        return source, outline.id

    def test_versions_are_immutable_and_default_is_exactly_pinned(
        self,
    ) -> None:
        created = self.service.create(
            self.editor,
            name="  Buyer outline  ",
            kind="outline",
            content="First\r\nprompt",
        )
        self.assertEqual(created.version, 1)
        self.assertEqual(created.name, "Buyer outline")
        self.assertEqual(created.content, "First\nprompt")

        default_v1 = self.service.set_default(
            self.editor,
            kind="outline",
            prompt_id=created.prompt_id,
        )
        self.assertEqual(default_v1.version, 1)
        updated = self.service.update(
            self.editor,
            prompt_id=created.prompt_id,
            expected_version=1,
            name="Buyer outline v2",
            content="Second prompt",
        )
        self.assertEqual(updated.version, 2)
        self.assertEqual(
            self.service.resolve(
                self.viewer,
                kind="outline",
                selection="project_default",
            ).version,
            1,
        )
        self.assertEqual(
            self.service.resolve(
                self.viewer,
                kind="outline",
                selection=created.prompt_id,
            ).version,
            2,
        )
        default_v2 = self.service.set_default(
            self.editor,
            kind="outline",
            prompt_id=created.prompt_id,
        )
        self.assertEqual(default_v2.version, 2)

        with self.engine.connect() as connection:
            versions = connection.execute(
                sa.select(
                    project_prompt_versions.c.version,
                    project_prompt_versions.c.content,
                    project_prompt_versions.c.content_hash,
                )
                .where(
                    project_prompt_versions.c.organization_id
                    == self.organization_id,
                    project_prompt_versions.c.project_id
                    == self.project_id,
                    project_prompt_versions.c.prompt_id
                    == created.prompt_id,
                )
                .order_by(project_prompt_versions.c.version)
            ).all()
        self.assertEqual(
            [(row.version, row.content) for row in versions],
            [(1, "First\nprompt"), (2, "Second prompt")],
        )
        self.assertTrue(
            all(len(row.content_hash) == 64 for row in versions)
        )
        self.assertEqual(
            [event.action for event in self.audit.events],
            [
                "project_prompt.created",
                "project_prompt.default.updated",
                "project_prompt.version.created",
                "project_prompt.default.updated",
            ],
        )
        self.assertNotIn("First", str(self.audit.events))
        self.assertNotIn("Second", str(self.audit.events))

    def test_archive_clears_default_without_deleting_versions(self) -> None:
        created = self.service.create(
            self.editor,
            name="Article prompt",
            kind="article",
            content="Write a useful article.",
        )
        self.service.set_default(
            self.editor,
            kind="article",
            prompt_id=created.prompt_id,
        )
        archived = self.service.set_active(
            self.editor,
            prompt_id=created.prompt_id,
            expected_version=1,
            active=False,
        )
        self.assertEqual(archived.status, "archived")
        resolved = self.service.resolve(
            self.viewer,
            kind="article",
            selection="project_default",
        )
        self.assertEqual(resolved.source, "system")
        with self.assertRaises(ServerProjectPromptError):
            self.service.resolve(
                self.viewer,
                kind="article",
                selection=created.prompt_id,
            )
        self.service.set_active(
            self.editor,
            prompt_id=created.prompt_id,
            expected_version=1,
            active=True,
        )
        self.assertEqual(
            self.service.resolve(
                self.viewer,
                kind="article",
                selection=created.prompt_id,
            ).version,
            1,
        )
        with self.engine.connect() as connection:
            self.assertEqual(
                connection.execute(
                    sa.select(sa.func.count())
                    .select_from(project_prompt_versions)
                    .where(
                        project_prompt_versions.c.organization_id
                        == self.organization_id,
                        project_prompt_versions.c.project_id
                        == self.project_id,
                        project_prompt_versions.c.prompt_id
                        == created.prompt_id,
                    )
                ).scalar_one(),
                1,
            )

    def test_viewer_can_resolve_but_cannot_write_or_cross_project(
        self,
    ) -> None:
        created = self.service.create(
            self.editor,
            name="Review prompt",
            kind="review",
            content="Review evidence.",
        )
        self.assertEqual(
            self.service.resolve(
                self.viewer,
                kind="review",
                selection=created.prompt_id,
            ).prompt_id,
            created.prompt_id,
        )
        with self.assertRaises(ProjectAccessDenied):
            self.service.create(
                self.viewer,
                name="Denied",
                kind="review",
                content="Must not persist.",
            )
        other = PostgresProjectPromptService(
            self.engine,
            organization_id=self.organization_id,
            project_id=self.other_project_id,
        )
        with self.assertRaises(ProjectAccessDenied):
            other.resolve(
                self.editor,
                kind="review",
                selection=created.prompt_id,
            )
        with self.assertRaises(ProjectAccessDenied):
            self.service.resolve(
                ActorIdentity("another-organization", self.editor_id),
                kind="review",
                selection=created.prompt_id,
            )

    def test_stale_update_and_kind_mismatch_fail_closed(self) -> None:
        created = self.service.create(
            self.editor,
            name="Outline prompt",
            kind="outline",
            content="Outline v1.",
        )
        self.service.update(
            self.editor,
            prompt_id=created.prompt_id,
            expected_version=1,
            name="Outline prompt",
            content="Outline v2.",
        )
        event_count = len(self.audit.events)
        with self.assertRaises(ServerProjectPromptConflict):
            self.service.update(
                self.editor,
                prompt_id=created.prompt_id,
                expected_version=1,
                name="Stale",
                content="Must not persist.",
            )
        with self.assertRaises(ServerProjectPromptError):
            self.service.set_default(
                self.editor,
                kind="article",
                prompt_id=created.prompt_id,
            )
        self.assertEqual(len(self.audit.events), event_count)
        with self.engine.connect() as connection:
            self.assertEqual(
                connection.execute(
                    sa.select(sa.func.count())
                    .select_from(project_prompt_versions)
                    .where(
                        project_prompt_versions.c.organization_id
                        == self.organization_id,
                        project_prompt_versions.c.project_id
                        == self.project_id,
                        project_prompt_versions.c.prompt_id
                        == created.prompt_id,
                    )
                ).scalar_one(),
                2,
            )

    def test_audit_failure_rolls_back_prompt_creation(self) -> None:
        service = PostgresProjectPromptService(
            self.engine,
            organization_id=self.organization_id,
            project_id=self.project_id,
            audit=FailingAuditWriter(),
        )
        with self.assertRaisesRegex(
            ServerProjectPromptUnavailable,
            "temporarily unavailable",
        ) as captured:
            service.create(
                self.editor,
                name="Rollback prompt",
                kind="outline",
                content="Private prompt body.",
            )
        self.assertNotIn(
            "private injected audit failure",
            str(captured.exception),
        )
        with self.engine.connect() as connection:
            self.assertEqual(
                connection.execute(
                    sa.select(sa.func.count())
                    .select_from(project_prompt_heads)
                    .where(
                        project_prompt_heads.c.organization_id
                        == self.organization_id,
                        project_prompt_heads.c.project_id
                        == self.project_id,
                    )
                ).scalar_one(),
                0,
            )

    def test_audit_failure_rolls_back_new_version_and_default(self) -> None:
        created = self.service.create(
            self.editor,
            name="Stable prompt",
            kind="outline",
            content="Stable version one.",
        )
        service = PostgresProjectPromptService(
            self.engine,
            organization_id=self.organization_id,
            project_id=self.project_id,
            audit=FailingAuditWriter(),
        )
        with self.assertRaises(ServerProjectPromptUnavailable):
            service.update(
                self.editor,
                prompt_id=created.prompt_id,
                expected_version=1,
                name="Must roll back",
                content="Private version two.",
            )
        with self.assertRaises(ServerProjectPromptUnavailable):
            service.set_default(
                self.editor,
                kind="outline",
                prompt_id=created.prompt_id,
            )
        with self.engine.connect() as connection:
            head = connection.execute(
                sa.select(project_prompt_heads.c.current_version).where(
                    project_prompt_heads.c.organization_id
                    == self.organization_id,
                    project_prompt_heads.c.project_id
                    == self.project_id,
                    project_prompt_heads.c.prompt_id
                    == created.prompt_id,
                )
            ).scalar_one()
            version_count = connection.execute(
                sa.select(sa.func.count())
                .select_from(project_prompt_versions)
                .where(
                    project_prompt_versions.c.organization_id
                    == self.organization_id,
                    project_prompt_versions.c.project_id
                    == self.project_id,
                    project_prompt_versions.c.prompt_id
                    == created.prompt_id,
                )
            ).scalar_one()
            default_count = connection.execute(
                sa.select(sa.func.count())
                .select_from(project_prompt_defaults)
                .where(
                    project_prompt_defaults.c.organization_id
                    == self.organization_id,
                    project_prompt_defaults.c.project_id
                    == self.project_id,
                )
            ).scalar_one()
        self.assertEqual(head, 1)
        self.assertEqual(version_count, 1)
        self.assertEqual(default_count, 0)

    def test_database_rejects_version_mutation_and_cross_project_pointer(
        self,
    ) -> None:
        created = self.service.create(
            self.editor,
            name="Immutable prompt",
            kind="outline",
            content="Immutable body.",
        )
        with self.assertRaises(sa.exc.DBAPIError):
            with self.engine.begin() as connection:
                connection.execute(
                    project_prompt_versions.update()
                    .where(
                        project_prompt_versions.c.organization_id
                        == self.organization_id,
                        project_prompt_versions.c.project_id
                        == self.project_id,
                        project_prompt_versions.c.prompt_id
                        == created.prompt_id,
                    )
                    .values(content="mutated")
                )
        with self.assertRaises(sa.exc.IntegrityError):
            with self.engine.begin() as connection:
                connection.execute(
                    project_prompt_defaults.insert().values(
                        organization_id=self.organization_id,
                        project_id=self.other_project_id,
                        kind="outline",
                        prompt_id=created.prompt_id,
                        version=1,
                    )
                )
        with self.assertRaises(sa.exc.IntegrityError):
            with self.engine.begin() as connection:
                connection.execute(
                    project_prompt_defaults.insert().values(
                        organization_id=self.organization_id,
                        project_id=self.project_id,
                        kind="article",
                        prompt_id=created.prompt_id,
                        version=1,
                    )
                )

    def test_schema_exposes_prompt_constraints_and_indexes(self) -> None:
        inspector = sa.inspect(self.engine)
        self.assertTrue(
            {
                "project_prompt_heads",
                "project_prompt_versions",
                "project_prompt_defaults",
            }.issubset(inspector.get_table_names())
        )
        head_checks = {
            item["name"]
            for item in inspector.get_check_constraints(
                "project_prompt_heads"
            )
        }
        version_checks = {
            item["name"]
            for item in inspector.get_check_constraints(
                "project_prompt_versions"
            )
        }
        head_foreign_keys = {
            item["name"]
            for item in inspector.get_foreign_keys(
                "project_prompt_heads"
            )
        }
        default_foreign_keys = {
            item["name"]
            for item in inspector.get_foreign_keys(
                "project_prompt_defaults"
            )
        }
        indexes = {
            item["name"]
            for item in inspector.get_indexes(
                "project_prompt_heads"
            )
        }
        self.assertIn("ck_project_prompt_heads_kind", head_checks)
        self.assertIn(
            "ck_project_prompt_heads_current_version",
            head_checks,
        )
        self.assertIn(
            "ck_project_prompt_versions_hash",
            version_checks,
        )
        self.assertIn(
            "ck_project_prompt_versions_kind",
            version_checks,
        )
        self.assertIn(
            "fk_project_prompt_heads_current_version",
            head_foreign_keys,
        )
        self.assertIn(
            "fk_project_prompt_defaults_version",
            default_foreign_keys,
        )
        self.assertIn(
            "ix_project_prompt_heads_directory",
            indexes,
        )

    def test_project_scoped_http_uses_postgres_and_exact_bodies(
        self,
    ) -> None:
        import app as app_module

        codec = ServerActorSessionCodec(b"p" * 32)
        base_config = app_module.config()
        with tempfile.TemporaryDirectory() as directory:
            local_state = Path(directory) / "must-not-exist"
            isolated = replace(
                base_config,
                data_file=local_state / "tasks.json",
                knowledge_agent_enabled=False,
            )
            with (
                patch.object(app_module, "config", return_value=isolated),
                patch.dict(
                    os.environ,
                    {
                        "ARTICLE_AGENT_SERVER_MODE": "true",
                        "ARTICLE_AGENT_SERVER_SESSION_SECRET": "p" * 32,
                        "ARTICLE_AGENT_OBJECT_STORE_BUCKET": "",
                    },
                    clear=False,
                ),
                TestClient(app_module.app) as client,
            ):
                audit = RecordingAuditWriter()
                client.app.state.server_project_prompt_service_factory = (
                    ServerProjectPromptServiceFactory(
                        self.engine,
                        audit=audit,
                    )
                )
                client.cookies.set(
                    SERVER_AUTH_COOKIE_NAME,
                    codec.create(self.viewer),
                )
                base_path = (
                    f"/api/projects/{self.project_id}/prompt-snapshots"
                )
                self.assertEqual(
                    client.get(base_path).status_code,
                    200,
                )
                self.assertEqual(
                    client.post(
                        base_path,
                        json={
                            "name": "Denied",
                            "kind": "outline",
                            "content": "Must not persist.",
                        },
                    ).status_code,
                    403,
                )
                client.cookies.set(
                    SERVER_AUTH_COOKIE_NAME,
                    codec.create(self.editor),
                )
                self.assertEqual(
                    client.post(
                        base_path,
                        json={
                            "name": "Unsafe",
                            "kind": "outline",
                            "content": "Prompt.",
                            "role": "admin",
                        },
                    ).status_code,
                    422,
                )
                created = client.post(
                    base_path,
                    json={
                        "name": "HTTP outline",
                        "kind": "outline",
                        "content": "Version one.",
                    },
                )
                self.assertEqual(created.status_code, 201, created.text)
                prompt_id = created.json()["prompt_id"]
                updated = client.put(
                    f"{base_path}/{prompt_id}",
                    json={
                        "expected_version": 1,
                        "name": "HTTP outline v2",
                        "content": "Version two.",
                    },
                )
                self.assertEqual(updated.status_code, 200, updated.text)
                self.assertEqual(updated.json()["version"], 2)
                self.assertEqual(
                    client.put(
                        f"{base_path}/{prompt_id}",
                        json={
                            "expected_version": 1,
                            "name": "Stale",
                            "content": "Must not persist.",
                        },
                    ).status_code,
                    409,
                )
                default = client.put(
                    f"/api/projects/{self.project_id}/"
                    "prompt-defaults/outline",
                    json={"prompt_id": prompt_id},
                )
                self.assertEqual(default.status_code, 200, default.text)
                self.assertEqual(default.json()["version"], 2)
                self.assertEqual(
                    client.put(
                        f"{base_path}/{prompt_id}/active",
                        json={
                            "expected_version": 1,
                            "active": False,
                        },
                    ).status_code,
                    409,
                )
                archived = client.put(
                    f"{base_path}/{prompt_id}/active",
                    json={
                        "expected_version": 2,
                        "active": False,
                    },
                )
                self.assertEqual(
                    archived.status_code,
                    200,
                    archived.text,
                )
                self.assertEqual(
                    archived.json()["status"],
                    "archived",
                )
                listing = client.get(base_path)
                self.assertEqual(listing.status_code, 200)
                self.assertEqual(
                    listing.json()["prompts"][0]["status"],
                    "archived",
                )
                self.assertNotIn(
                    "outline",
                    listing.json()["defaults"],
                )
                self.assertEqual(
                    client.post(
                        f"/api/projects/{self.other_project_id}/"
                        "prompt-snapshots",
                        json={
                            "name": "Cross project",
                            "kind": "outline",
                            "content": "Must not persist.",
                        },
                    ).status_code,
                    403,
                )
                self.assertEqual(
                    [event.action for event in audit.events],
                    [
                        "project_prompt.created",
                        "project_prompt.version.created",
                        "project_prompt.default.updated",
                        "project_prompt.status.updated",
                    ],
                )
                self.assertNotIn("Version one", str(audit.events))
                self.assertNotIn("Version two", str(audit.events))
                self.assertFalse(local_state.exists())

    def test_project_prompt_http_is_not_added_to_local_mode(self) -> None:
        import app as app_module

        previous_mode = getattr(
            app_module.app.state,
            "server_mode_enabled",
            None,
        )
        app_module.app.state.server_mode_enabled = False
        try:
            self.assertEqual(
                TestClient(app_module.app).get(
                    f"/api/projects/{self.project_id}/prompt-snapshots"
                ).status_code,
                404,
            )
        finally:
            app_module.app.state.server_mode_enabled = previous_mode

    def test_explicit_sqlite_migration_preserves_current_snapshot(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, outline_id = self._legacy_source(directory)
            audit = RecordingAuditWriter()
            report = migrate_project_prompts(
                source,
                customer=self.project_id,
                engine=self.engine,
                actor=self.editor,
                project_id=self.project_id,
                audit=audit,
            )
            self.assertTrue(report.imported)
            self.assertEqual(report.source.prompt_count, 2)
            self.assertEqual(report.source.active_count, 1)
            self.assertEqual(report.source.default_count, 1)
            resolved = self.service.resolve(
                self.viewer,
                kind="outline",
                selection="project_default",
            )
            self.assertEqual(resolved.prompt_id, outline_id)
            self.assertEqual(resolved.version, 2)
            self.assertEqual(
                resolved.content,
                "Legacy outline version two.",
            )
            listing = self.service.list(self.viewer)
            self.assertEqual(
                {item.status for item in listing.prompts},
                {"active", "archived"},
            )
            repeated = migrate_project_prompts(
                source,
                customer=self.project_id,
                engine=self.engine,
                actor=self.editor,
                project_id=self.project_id,
                audit=audit,
            )
            self.assertTrue(repeated.already_matched)
            self.assertFalse(repeated.imported)
            self.assertEqual(
                [event.action for event in audit.events],
                ["project_prompt.imported"],
            )
            self.assertNotIn(
                "Legacy outline",
                str(audit.events),
            )

    def test_prompt_migration_dry_run_and_divergent_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, _ = self._legacy_source(directory)
            dry_run = migrate_project_prompts(
                source,
                customer=self.project_id,
                engine=self.engine,
                actor=self.editor,
                project_id=self.project_id,
                audit=self.audit,
                dry_run=True,
            )
            self.assertFalse(dry_run.imported)
            with self.engine.connect() as connection:
                self.assertEqual(
                    connection.execute(
                        sa.select(sa.func.count())
                        .select_from(project_prompt_heads)
                        .where(
                            project_prompt_heads.c.organization_id
                            == self.organization_id,
                            project_prompt_heads.c.project_id
                            == self.project_id,
                        )
                    ).scalar_one(),
                    0,
                )
            self.service.create(
                self.editor,
                name="Divergent target",
                kind="outline",
                content="Different content.",
            )
            with self.assertRaises(ProjectPromptMigrationConflict):
                migrate_project_prompts(
                    source,
                    customer=self.project_id,
                    engine=self.engine,
                    actor=self.editor,
                    project_id=self.project_id,
                )

    def test_prompt_migration_requires_editor_and_rolls_back_audit(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, _ = self._legacy_source(directory)
            with self.assertRaises(ProjectAccessDenied):
                migrate_project_prompts(
                    source,
                    customer=self.project_id,
                    engine=self.engine,
                    actor=self.viewer,
                    project_id=self.project_id,
                )
            with self.assertRaises(ProjectAccessDenied):
                migrate_project_prompts(
                    source,
                    customer=self.project_id,
                    engine=self.engine,
                    actor=self.editor,
                    project_id=self.other_project_id,
                )
            with self.assertRaises(ProjectPromptMigrationUnavailable):
                migrate_project_prompts(
                    source,
                    customer=self.project_id,
                    engine=self.engine,
                    actor=self.editor,
                    project_id=self.project_id,
                    audit=FailingAuditWriter(),
                )
            with self.engine.connect() as connection:
                self.assertEqual(
                    connection.execute(
                        sa.select(sa.func.count())
                        .select_from(project_prompt_heads)
                        .where(
                            project_prompt_heads.c.organization_id
                            == self.organization_id,
                            project_prompt_heads.c.project_id
                            == self.project_id,
                        )
                    ).scalar_one(),
                    0,
                )


if __name__ == "__main__":
    unittest.main()
