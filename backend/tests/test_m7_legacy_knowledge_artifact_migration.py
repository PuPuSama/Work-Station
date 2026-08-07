from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
import uuid
from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

import sqlalchemy as sa


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from knowledge_agent.database import create_knowledge_engine  # noqa: E402
from knowledge_agent import m7_legacy_artifact_migration as migration_cli  # noqa: E402
from knowledge_agent.schema import (  # noqa: E402
    knowledge_assets,
    knowledge_sources,
    projects,
    source_snapshots,
)
from server_schema import (  # noqa: E402
    organizations,
    project_ownership,
    workspace_users,
)
from services.access_control import ActorIdentity, ProjectAccessDenied  # noqa: E402
from services.audit_log import AuditEvent  # noqa: E402
from services.legacy_knowledge_artifact_migration import (  # noqa: E402
    LegacyKnowledgeArtifactMigrationError,
    LegacyKnowledgeArtifactMigrationReport,
    LegacyKnowledgeArtifactMigrator,
)
from services.object_store import (  # noqa: E402
    ObjectStat,
    ObjectStoreError,
    StoredObject,
)


DATABASE_URL_ENV = "ARTICLE_AGENT_DATABASE_URL"
BUCKET = "article-agent-private"


class FakeObjectStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.content_types: dict[str, str] = {}
        self.put_calls: list[dict[str, object]] = []
        self.fail_put = False
        self.corrupt_get = False
        self.corrupt_content_type = False
        self.after_first_put = None

    def put(self, *, key, data, content_type, metadata=None):
        self.put_calls.append(
            {
                "key": key,
                "data": bytes(data),
                "content_type": content_type,
                "metadata": dict(metadata or {}),
            }
        )
        if self.fail_put:
            raise ObjectStoreError("private provider failure")
        body = bytes(data)
        digest = hashlib.sha256(body).hexdigest()
        existing = self.objects.get(key)
        if existing is not None and existing != body:
            raise ObjectStoreError("object store put failed")
        self.objects[key] = body
        stored_content_type = (
            "text/plain" if self.corrupt_content_type else content_type
        )
        self.content_types[key] = stored_content_type
        callback = self.after_first_put if len(self.put_calls) == 1 else None
        if callback is not None:
            self.after_first_put = None
            callback()
        return StoredObject(
            key=key,
            content_hash=digest,
            content_type=stored_content_type,
            byte_size=len(body),
            etag=digest,
        )

    def head(self, key):
        try:
            body = self.objects[key]
        except KeyError as exc:
            raise ObjectStoreError("object store head failed") from exc
        digest = hashlib.sha256(body).hexdigest()
        return ObjectStat(
            key=key,
            byte_size=len(body),
            content_type=self.content_types[key],
            sha256=digest,
            etag=digest,
        )

    def get(self, key, *, max_bytes):
        try:
            body = self.objects[key]
        except KeyError as exc:
            raise ObjectStoreError("object store get failed") from exc
        if len(body) > max_bytes:
            raise ObjectStoreError("object store get failed")
        if self.corrupt_get:
            return b"x" * len(body)
        return body


class RecordingAudit:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def append(self, connection, event: AuditEvent) -> None:
        if not connection.in_transaction():
            raise AssertionError("audit must share the business transaction")
        self.events.append(event)


class FailingAudit:
    def __init__(self) -> None:
        self.calls = 0

    def append(self, connection, event: AuditEvent) -> None:
        self.calls += 1
        raise RuntimeError("private audit failure")


class LegacyKnowledgeArtifactMigrationContractTests(unittest.TestCase):
    def test_error_code_and_report_have_stable_public_contracts(self) -> None:
        error = LegacyKnowledgeArtifactMigrationError("artifact_test_failure")
        report = LegacyKnowledgeArtifactMigrationReport(
            project_id="project-a",
            reference_count=3,
            snapshot_artifact_count=2,
            asset_count=1,
            unique_object_count=3,
            already_managed_count=0,
            migrated_reference_count=3,
            applied=False,
        )

        self.assertEqual(error.code, "artifact_test_failure")
        self.assertEqual(str(error), "artifact_test_failure")
        self.assertEqual(
            report.public_values(),
            {
                "project_id": "project-a",
                "reference_count": 3,
                "snapshot_artifact_count": 2,
                "asset_count": 1,
                "unique_object_count": 3,
                "already_managed_count": 0,
                "migrated_reference_count": 3,
                "applied": False,
            },
        )

    def test_cli_initialization_failure_returns_only_stable_json(self) -> None:
        output = StringIO()
        environment = {
            "ARTICLE_AGENT_DATABASE_URL": (
                "postgresql://private-user:private-pass@private-host/db"
            ),
            "ARTICLE_AGENT_OBJECT_STORE_BUCKET": "private-bucket",
        }
        with patch.object(
            migration_cli,
            "create_knowledge_engine",
            side_effect=RuntimeError("private initialization details"),
        ), redirect_stdout(output):
            result = migration_cli.main(
                [
                    "inspect",
                    "--organization-id",
                    "organization-a",
                    "--user-id",
                    "user-a",
                    "--project-id",
                    "project-a",
                ],
                environment=environment,
            )

        self.assertEqual(result, 2)
        self.assertEqual(
            json.loads(output.getvalue()),
            {
                "ok": False,
                "error": "legacy_artifact_migration_unavailable",
            },
        )
        self.assertNotIn("private", output.getvalue())

    def test_cli_cleanup_failure_cannot_print_success_or_private_details(
        self,
    ) -> None:
        output = StringIO()
        engine = Mock()
        engine.dispose.side_effect = RuntimeError("private cleanup details")
        report = LegacyKnowledgeArtifactMigrationReport(
            project_id="project-a",
            reference_count=0,
            snapshot_artifact_count=0,
            asset_count=0,
            unique_object_count=0,
            already_managed_count=0,
            migrated_reference_count=0,
            applied=False,
        )
        environment = {
            "ARTICLE_AGENT_DATABASE_URL": "postgresql://database.invalid/db",
            "ARTICLE_AGENT_OBJECT_STORE_BUCKET": "private-bucket",
        }
        with patch.object(
            migration_cli,
            "create_knowledge_engine",
            return_value=engine,
        ), patch.object(migration_cli, "S3ObjectStore"), patch.object(
            migration_cli,
            "LegacyKnowledgeArtifactMigrator",
        ) as migrator_type, redirect_stdout(output):
            migrator_type.return_value.inspect.return_value = report
            result = migration_cli.main(
                [
                    "inspect",
                    "--organization-id",
                    "organization-a",
                    "--user-id",
                    "user-a",
                    "--project-id",
                    "project-a",
                ],
                environment=environment,
            )

        self.assertEqual(result, 2)
        self.assertEqual(
            json.loads(output.getvalue()),
            {
                "ok": False,
                "error": "legacy_artifact_migration_unavailable",
            },
        )
        self.assertNotIn("private", output.getvalue())


@unittest.skipUnless(
    os.environ.get(DATABASE_URL_ENV),
    f"{DATABASE_URL_ENV} is required for PostgreSQL integration tests",
)
class LegacyKnowledgeArtifactMigrationPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = create_knowledge_engine(os.environ[DATABASE_URL_ENV])

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    def setUp(self) -> None:
        suffix = uuid.uuid4().hex
        self.org_id = f"artifact-migration-org-{suffix}"
        self.project_id = f"artifact-migration-project-{suffix}"
        self.admin_id = f"artifact-migration-admin-{suffix}"
        self.other_org_id = f"artifact-migration-other-org-{suffix}"
        self.other_project_id = f"artifact-migration-other-project-{suffix}"
        self.other_admin_id = f"artifact-migration-other-admin-{suffix}"
        self.source_id = "source-a"
        self.snapshot_id = "snapshot-a"
        self.asset_id = "asset-a"
        self.raw_bytes = b"legacy raw private document"
        self.normalized_bytes = b'{"title":"Legacy document"}'
        self.asset_bytes = b"legacy-image-bytes"
        self.temporary = tempfile.TemporaryDirectory()
        self.local_root = Path(self.temporary.name).resolve()
        self.raw_path = self._write_local("raw", "document.bin", self.raw_bytes)
        self.normalized_path = self._write_local(
            "normalized",
            "document.json",
            self.normalized_bytes,
        )
        self.asset_path = self._write_local(
            "assets",
            "image.webp",
            self.asset_bytes,
        )
        self.other_path = self._write_local(
            "raw",
            "unchanged.bin",
            b"other",
            project_id=self.other_project_id,
        )
        self.store = FakeObjectStore()
        self.audit = RecordingAudit()
        self.migrator = LegacyKnowledgeArtifactMigrator(
            self.engine,
            self.store,  # type: ignore[arg-type]
            bucket=BUCKET,
            local_root=self.local_root,
            audit=self.audit,
        )
        self._seed_scope()

    def tearDown(self) -> None:
        project_ids = (self.project_id, self.other_project_id)
        organization_ids = (self.org_id, self.other_org_id)
        with self.engine.begin() as connection:
            connection.execute(
                knowledge_assets.delete().where(
                    knowledge_assets.c.project_id.in_(project_ids)
                )
            )
            connection.execute(
                source_snapshots.delete().where(
                    source_snapshots.c.project_id.in_(project_ids)
                )
            )
            connection.execute(
                knowledge_sources.delete().where(
                    knowledge_sources.c.project_id.in_(project_ids)
                )
            )
            connection.execute(
                project_ownership.delete().where(
                    project_ownership.c.project_id.in_(project_ids)
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
        self.temporary.cleanup()

    def _write_local(
        self,
        namespace: str,
        filename: str,
        body: bytes,
        *,
        project_id: str | None = None,
    ) -> Path:
        digest = hashlib.sha256(body).hexdigest()
        destination = (
            self.local_root
            / (project_id or self.project_id)
            / namespace
            / digest[:2]
            / digest
            / filename
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(body)
        return destination

    def _seed_scope(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                projects.insert(),
                (
                    {
                        "project_id": self.project_id,
                        "customer_name": "Artifact migration project",
                        "official_domain": "migration.example.test",
                    },
                    {
                        "project_id": self.other_project_id,
                        "customer_name": "Other migration project",
                        "official_domain": "other-migration.example.test",
                    },
                ),
            )
            connection.execute(
                organizations.insert(),
                (
                    {
                        "organization_id": self.org_id,
                        "name": "Artifact migration org",
                    },
                    {
                        "organization_id": self.other_org_id,
                        "name": "Other migration org",
                    },
                ),
            )
            connection.execute(
                workspace_users.insert(),
                (
                    {
                        "organization_id": self.org_id,
                        "user_id": self.admin_id,
                        "display_name": "Migration admin",
                        "organization_role": "org_admin",
                    },
                    {
                        "organization_id": self.other_org_id,
                        "user_id": self.other_admin_id,
                        "display_name": "Other migration admin",
                        "organization_role": "org_admin",
                    },
                ),
            )
            connection.execute(
                project_ownership.insert(),
                (
                    {
                        "organization_id": self.org_id,
                        "project_id": self.project_id,
                    },
                    {
                        "organization_id": self.other_org_id,
                        "project_id": self.other_project_id,
                    },
                ),
            )
            connection.execute(
                knowledge_sources.insert(),
                (
                    {
                        "project_id": self.project_id,
                        "source_id": self.source_id,
                        "display_name": "Legacy source",
                        "source_kind": "private_file",
                        "trust_tier": "hard_fact",
                    },
                    {
                        "project_id": self.other_project_id,
                        "source_id": self.source_id,
                        "display_name": "Other legacy source",
                        "source_kind": "private_file",
                        "trust_tier": "hard_fact",
                    },
                ),
            )
            connection.execute(
                source_snapshots.insert(),
                (
                    {
                        "project_id": self.project_id,
                        "source_id": self.source_id,
                        "snapshot_id": self.snapshot_id,
                        "content_hash": hashlib.sha256(
                            self.raw_bytes
                        ).hexdigest(),
                        "parser_name": "legacy-parser",
                        "parser_version": "1",
                        "raw_artifact_uri": self.raw_path.as_uri(),
                        "normalized_artifact_uri": self.normalized_path.as_uri(),
                        "fetched_at": datetime.now(timezone.utc),
                        "metadata": {"stable": "snapshot metadata"},
                    },
                    {
                        "project_id": self.other_project_id,
                        "source_id": self.source_id,
                        "snapshot_id": self.snapshot_id,
                        "content_hash": hashlib.sha256(b"other").hexdigest(),
                        "parser_name": "legacy-parser",
                        "parser_version": "1",
                        "raw_artifact_uri": self.other_path.as_uri(),
                        "normalized_artifact_uri": None,
                        "fetched_at": datetime.now(timezone.utc),
                        "metadata": {},
                    },
                ),
            )
            connection.execute(
                knowledge_assets.insert().values(
                    project_id=self.project_id,
                    asset_id=self.asset_id,
                    content_hash=hashlib.sha256(self.asset_bytes).hexdigest(),
                    artifact_uri=self.asset_path.as_uri(),
                    content_type="image/webp",
                    byte_size=len(self.asset_bytes),
                    width=100,
                    height=80,
                    metadata={"stable": "asset metadata"},
                )
            )

    def _actor(self) -> ActorIdentity:
        return ActorIdentity(self.org_id, self.admin_id)

    def _uris(self, project_id: str | None = None) -> tuple[str, str | None, str | None]:
        selected_project = project_id or self.project_id
        with self.engine.connect() as connection:
            snapshot = connection.execute(
                sa.select(
                    source_snapshots.c.raw_artifact_uri,
                    source_snapshots.c.normalized_artifact_uri,
                ).where(
                    source_snapshots.c.project_id == selected_project,
                    source_snapshots.c.snapshot_id == self.snapshot_id,
                )
            ).one()
            asset_uri = connection.execute(
                sa.select(knowledge_assets.c.artifact_uri).where(
                    knowledge_assets.c.project_id == selected_project,
                    knowledge_assets.c.asset_id == self.asset_id,
                )
            ).scalar_one_or_none()
        return str(snapshot.raw_artifact_uri), snapshot.normalized_artifact_uri, asset_uri

    def test_inspect_is_a_write_free_dry_run(self) -> None:
        before = self._uris()

        report = self.migrator.inspect(self._actor(), self.project_id)

        self.assertEqual(
            report.public_values(),
            {
                "project_id": self.project_id,
                "reference_count": 3,
                "snapshot_artifact_count": 2,
                "asset_count": 1,
                "unique_object_count": 3,
                "already_managed_count": 0,
                "migrated_reference_count": 3,
                "applied": False,
            },
        )
        self.assertEqual(self._uris(), before)
        self.assertEqual(self.store.put_calls, [])
        self.assertEqual(self.store.objects, {})
        self.assertEqual(self.audit.events, [])

    def test_apply_switches_only_uris_audits_counts_and_is_idempotent(self) -> None:
        with self.engine.connect() as connection:
            snapshot_before = connection.execute(
                sa.select(source_snapshots).where(
                    source_snapshots.c.project_id == self.project_id,
                    source_snapshots.c.snapshot_id == self.snapshot_id,
                )
            ).mappings().one()
            asset_before = connection.execute(
                sa.select(knowledge_assets).where(
                    knowledge_assets.c.project_id == self.project_id,
                    knowledge_assets.c.asset_id == self.asset_id,
                )
            ).mappings().one()
        other_before = self._uris(self.other_project_id)

        report = self.migrator.apply(
            self._actor(),
            self.project_id,
            confirm_project_id=self.project_id,
        )

        self.assertTrue(report.applied)
        self.assertEqual(report.migrated_reference_count, 3)
        self.assertEqual(len(self.store.put_calls), 3)
        prefix = f"organizations/{self.org_id}/projects/{self.project_id}/"
        self.assertTrue(
            all(call["key"].startswith(prefix) for call in self.store.put_calls)
        )
        raw_uri, normalized_uri, asset_uri = self._uris()
        for uri in (raw_uri, normalized_uri, asset_uri):
            self.assertIsNotNone(uri)
            self.assertTrue(str(uri).startswith(f"s3://{BUCKET}/{prefix}"))
        self.assertEqual(self._uris(self.other_project_id), other_before)
        self.assertTrue(self.raw_path.is_file())
        self.assertTrue(self.normalized_path.is_file())
        self.assertTrue(self.asset_path.is_file())

        with self.engine.connect() as connection:
            snapshot_after = connection.execute(
                sa.select(source_snapshots).where(
                    source_snapshots.c.project_id == self.project_id,
                    source_snapshots.c.snapshot_id == self.snapshot_id,
                )
            ).mappings().one()
            asset_after = connection.execute(
                sa.select(knowledge_assets).where(
                    knowledge_assets.c.project_id == self.project_id,
                    knowledge_assets.c.asset_id == self.asset_id,
                )
            ).mappings().one()
        self.assertEqual(
            {
                key: value
                for key, value in snapshot_after.items()
                if key not in {"raw_artifact_uri", "normalized_artifact_uri"}
            },
            {
                key: value
                for key, value in snapshot_before.items()
                if key not in {"raw_artifact_uri", "normalized_artifact_uri"}
            },
        )
        self.assertEqual(
            {key: value for key, value in asset_after.items() if key != "artifact_uri"},
            {key: value for key, value in asset_before.items() if key != "artifact_uri"},
        )
        self.assertEqual(len(self.audit.events), 1)
        event = self.audit.events[0]
        self.assertEqual(event.action, "knowledge.artifacts.storage_migrated")
        self.assertEqual(
            event.details,
            {
                "schema_version": 1,
                "reference_count": 3,
                "snapshot_artifact_count": 2,
                "asset_count": 1,
                "unique_object_count": 3,
            },
        )
        self.assertNotIn("artifact_uri", str(event.details))
        first_put_count = len(self.store.put_calls)

        retry = self.migrator.apply(
            self._actor(),
            self.project_id,
            confirm_project_id=self.project_id,
        )

        self.assertFalse(retry.applied)
        self.assertEqual(retry.already_managed_count, 3)
        self.assertEqual(retry.migrated_reference_count, 0)
        self.assertEqual(len(self.store.put_calls), first_put_count)
        self.assertEqual(len(self.audit.events), 1)

    def test_inspect_rejects_local_escape_and_hash_mismatch(self) -> None:
        original_normalized_uri = self.normalized_path.as_uri()
        with tempfile.TemporaryDirectory() as outside_directory:
            outside = Path(outside_directory) / "outside.json"
            outside.write_bytes(b"outside")
            with self.engine.begin() as connection:
                connection.execute(
                    source_snapshots.update()
                    .where(
                        source_snapshots.c.project_id == self.project_id,
                        source_snapshots.c.snapshot_id == self.snapshot_id,
                    )
                    .values(normalized_artifact_uri=outside.as_uri())
                )
            with self.assertRaises(
                LegacyKnowledgeArtifactMigrationError
            ) as escaped:
                self.migrator.inspect(self._actor(), self.project_id)
            self.assertEqual(
                escaped.exception.code,
                "legacy_artifact_file_unavailable",
            )

        other_project_path = self.local_root / self.other_project_id / "other.json"
        other_project_path.parent.mkdir(parents=True, exist_ok=True)
        other_project_path.write_bytes(self.normalized_bytes)
        with self.engine.begin() as connection:
            connection.execute(
                source_snapshots.update()
                .where(
                    source_snapshots.c.project_id == self.project_id,
                    source_snapshots.c.snapshot_id == self.snapshot_id,
                )
                .values(normalized_artifact_uri=other_project_path.as_uri())
            )
        with self.assertRaises(
            LegacyKnowledgeArtifactMigrationError
        ) as project_scope:
            self.migrator.inspect(self._actor(), self.project_id)
        self.assertEqual(
            project_scope.exception.code,
            "legacy_artifact_project_scope_mismatch",
        )

        invalid_layout = (
            self.local_root / self.project_id / "normalized" / "document.json"
        )
        invalid_layout.parent.mkdir(parents=True, exist_ok=True)
        invalid_layout.write_bytes(self.normalized_bytes)
        with self.engine.begin() as connection:
            connection.execute(
                source_snapshots.update()
                .where(
                    source_snapshots.c.project_id == self.project_id,
                    source_snapshots.c.snapshot_id == self.snapshot_id,
                )
                .values(normalized_artifact_uri=invalid_layout.as_uri())
            )
        with self.assertRaises(
            LegacyKnowledgeArtifactMigrationError
        ) as path_identity:
            self.migrator.inspect(self._actor(), self.project_id)
        self.assertEqual(
            path_identity.exception.code,
            "legacy_artifact_path_identity_mismatch",
        )

        with self.engine.begin() as connection:
            connection.execute(
                source_snapshots.update()
                .where(
                    source_snapshots.c.project_id == self.project_id,
                    source_snapshots.c.snapshot_id == self.snapshot_id,
                )
                .values(
                    normalized_artifact_uri=original_normalized_uri,
                    content_hash=hashlib.sha256(b"different raw").hexdigest(),
                )
            )
        with self.assertRaises(LegacyKnowledgeArtifactMigrationError) as mismatch:
            self.migrator.inspect(self._actor(), self.project_id)
        self.assertEqual(
            mismatch.exception.code,
            "legacy_artifact_hash_mismatch",
        )
        self.assertEqual(self.store.put_calls, [])

    def test_object_store_and_audit_failures_leave_database_uris_unchanged(
        self,
    ) -> None:
        before = self._uris()
        self.store.fail_put = True

        with self.assertRaises(LegacyKnowledgeArtifactMigrationError) as upload:
            self.migrator.apply(
                self._actor(),
                self.project_id,
                confirm_project_id=self.project_id,
            )
        self.assertEqual(upload.exception.code, "legacy_artifact_upload_failed")
        self.assertNotIn("private provider failure", str(upload.exception))
        self.assertEqual(self._uris(), before)
        self.assertEqual(self.audit.events, [])

        self.store.fail_put = False
        failing_audit = FailingAudit()
        migrator = LegacyKnowledgeArtifactMigrator(
            self.engine,
            self.store,  # type: ignore[arg-type]
            bucket=BUCKET,
            local_root=self.local_root,
            audit=failing_audit,
        )
        with self.assertRaises(LegacyKnowledgeArtifactMigrationError) as audit:
            migrator.apply(
                self._actor(),
                self.project_id,
                confirm_project_id=self.project_id,
            )
        self.assertEqual(audit.exception.code, "legacy_artifact_commit_failed")
        self.assertNotIn("private audit failure", str(audit.exception))
        self.assertEqual(failing_audit.calls, 1)
        self.assertEqual(self._uris(), before)

    def test_download_verification_failure_leaves_database_uris_unchanged(
        self,
    ) -> None:
        before = self._uris()
        self.store.corrupt_get = True

        with self.assertRaises(
            LegacyKnowledgeArtifactMigrationError
        ) as verification:
            self.migrator.apply(
                self._actor(),
                self.project_id,
                confirm_project_id=self.project_id,
            )

        self.assertEqual(
            verification.exception.code,
            "legacy_artifact_upload_verification_failed",
        )
        self.assertEqual(self._uris(), before)
        self.assertEqual(self.audit.events, [])

    def test_content_type_is_verified_during_upload_and_inspection(self) -> None:
        before = self._uris()
        self.store.corrupt_content_type = True

        with self.assertRaises(
            LegacyKnowledgeArtifactMigrationError
        ) as upload:
            self.migrator.apply(
                self._actor(),
                self.project_id,
                confirm_project_id=self.project_id,
            )
        self.assertEqual(
            upload.exception.code,
            "legacy_artifact_upload_verification_failed",
        )
        self.assertEqual(self._uris(), before)
        self.assertEqual(self.audit.events, [])

        self.store.corrupt_content_type = False
        self.migrator.apply(
            self._actor(),
            self.project_id,
            confirm_project_id=self.project_id,
        )
        asset_key = next(
            key
            for key, content_type in self.store.content_types.items()
            if content_type == "image/webp"
        )
        self.store.content_types[asset_key] = "text/plain"

        with self.assertRaises(
            LegacyKnowledgeArtifactMigrationError
        ) as managed:
            self.migrator.inspect(self._actor(), self.project_id)
        self.assertEqual(
            managed.exception.code,
            "managed_artifact_content_type_mismatch",
        )

    def test_concurrent_reference_addition_aborts_database_switch(self) -> None:
        before = self._uris()
        concurrent_asset_id = "asset-concurrent"
        concurrent_bytes = b"concurrent legacy asset"
        concurrent_path = self._write_local(
            "assets",
            "concurrent.bin",
            concurrent_bytes,
        )

        def add_reference() -> None:
            with self.engine.begin() as connection:
                connection.execute(
                    knowledge_assets.insert().values(
                        project_id=self.project_id,
                        asset_id=concurrent_asset_id,
                        content_hash=hashlib.sha256(concurrent_bytes).hexdigest(),
                        artifact_uri=concurrent_path.as_uri(),
                        content_type="application/octet-stream",
                        byte_size=len(concurrent_bytes),
                        metadata={},
                    )
                )

        self.store.after_first_put = add_reference

        with self.assertRaises(
            LegacyKnowledgeArtifactMigrationError
        ) as concurrent:
            self.migrator.apply(
                self._actor(),
                self.project_id,
                confirm_project_id=self.project_id,
            )

        self.assertEqual(
            concurrent.exception.code,
            "legacy_artifact_concurrent_change",
        )
        self.assertEqual(self._uris(), before)
        with self.engine.connect() as connection:
            concurrent_uri = connection.execute(
                sa.select(knowledge_assets.c.artifact_uri).where(
                    knowledge_assets.c.project_id == self.project_id,
                    knowledge_assets.c.asset_id == concurrent_asset_id,
                )
            ).scalar_one()
        self.assertEqual(concurrent_uri, concurrent_path.as_uri())
        self.assertEqual(self.audit.events, [])

    def test_confirmation_authorization_and_cross_project_scope_fail_closed(
        self,
    ) -> None:
        actor = self._actor()
        with self.assertRaisesRegex(ValueError, "exactly match"):
            self.migrator.apply(
                actor,
                self.project_id,
                confirm_project_id=self.other_project_id,
            )

        foreign_actor = ActorIdentity(self.other_org_id, self.other_admin_id)
        with self.assertRaises(ProjectAccessDenied):
            self.migrator.inspect(foreign_actor, self.project_id)
        with self.assertRaises(ProjectAccessDenied):
            self.migrator.apply(
                foreign_actor,
                self.project_id,
                confirm_project_id=self.project_id,
            )
        self.assertEqual(self.store.put_calls, [])
        self.assertEqual(self._uris()[0], self.raw_path.as_uri())


if __name__ == "__main__":
    unittest.main()
