from __future__ import annotations

import hashlib
import os
import sys
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sqlalchemy as sa


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from knowledge_agent.database import create_knowledge_engine  # noqa: E402
from knowledge_agent.schema import (  # noqa: E402
    knowledge_assets,
    knowledge_sources,
    projects,
    snapshot_assets,
    source_snapshots,
)
from server_schema import (  # noqa: E402
    article_tasks,
    object_orphan_observations,
    organizations,
    project_ownership,
    workspace_users,
)
from services.access_control import ActorIdentity, ProjectAccessDenied  # noqa: E402
from services.audit_log import AuditEvent  # noqa: E402
from services.object_orphan_reconciliation import (  # noqa: E402
    ProjectObjectOrphanReconciler,
)
from services.object_store import (  # noqa: E402
    ObjectMetadata,
    ObjectStoreError,
    build_project_object_prefix,
)


DATABASE_URL_ENV = "ARTICLE_AGENT_DATABASE_URL"
BUCKET = "article-agent-private"


class FakeInventoryStore:
    def __init__(self) -> None:
        self.objects: dict[str, ObjectMetadata] = {}
        self.deleted: list[str] = []
        self.fail_delete: set[str] = set()

    def list(self, *, prefix: str) -> tuple[ObjectMetadata, ...]:
        normalized = prefix.rstrip("/") + "/"
        return tuple(
            item
            for key, item in sorted(self.objects.items())
            if key.startswith(normalized)
        )

    def delete(self, key: str) -> None:
        if key in self.fail_delete:
            raise ObjectStoreError("object store delete failed")
        self.deleted.append(key)
        self.objects.pop(key, None)


class RecordingAudit:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def append(self, connection, event: AuditEvent) -> None:
        self.events.append(event)


@unittest.skipUnless(
    os.environ.get(DATABASE_URL_ENV),
    f"{DATABASE_URL_ENV} is required for PostgreSQL integration tests",
)
class ObjectOrphanReconciliationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = create_knowledge_engine(os.environ[DATABASE_URL_ENV])

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    def setUp(self) -> None:
        suffix = uuid.uuid4().hex
        self.org_id = f"orphan-org-{suffix}"
        self.project_id = f"orphan-project-{suffix}"
        self.other_org_id = f"orphan-other-org-{suffix}"
        self.other_project_id = f"orphan-other-project-{suffix}"
        self.admin_id = f"orphan-admin-{suffix}"
        self.other_admin_id = f"orphan-other-admin-{suffix}"
        self.store = FakeInventoryStore()
        self.audit = RecordingAudit()
        self.reconciler = ProjectObjectOrphanReconciler(
            self.engine,
            self.store,  # type: ignore[arg-type]
            bucket=BUCKET,
            audit=self.audit,
        )
        self._seed_scope()

    def tearDown(self) -> None:
        project_ids = (self.project_id, self.other_project_id)
        organization_ids = (self.org_id, self.other_org_id)
        with self.engine.begin() as connection:
            connection.execute(
                object_orphan_observations.delete().where(
                    object_orphan_observations.c.project_id.in_(project_ids)
                )
            )
            connection.execute(
                article_tasks.delete().where(
                    article_tasks.c.project_id.in_(project_ids)
                )
            )
            connection.execute(
                snapshot_assets.delete().where(
                    snapshot_assets.c.project_id.in_(project_ids)
                )
            )
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

    def _seed_scope(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                projects.insert(),
                (
                    {
                        "project_id": self.project_id,
                        "customer_name": "Orphan project",
                        "official_domain": "orphan.example.test",
                    },
                    {
                        "project_id": self.other_project_id,
                        "customer_name": "Other project",
                        "official_domain": "other.example.test",
                    },
                ),
            )
            connection.execute(
                organizations.insert(),
                (
                    {"organization_id": self.org_id, "name": "Orphan org"},
                    {
                        "organization_id": self.other_org_id,
                        "name": "Other orphan org",
                    },
                ),
            )
            connection.execute(
                workspace_users.insert(),
                (
                    {
                        "organization_id": self.org_id,
                        "user_id": self.admin_id,
                        "display_name": "Admin",
                        "organization_role": "org_admin",
                    },
                    {
                        "organization_id": self.other_org_id,
                        "user_id": self.other_admin_id,
                        "display_name": "Other admin",
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

    def _object(self, label: str, *, other_project: bool = False) -> tuple[str, str]:
        digest = hashlib.sha256(label.encode()).hexdigest()
        organization_id = self.other_org_id if other_project else self.org_id
        project_id = self.other_project_id if other_project else self.project_id
        prefix = build_project_object_prefix(organization_id, project_id)
        key = f"{prefix}blobs/{digest[:2]}/{digest}"
        uri = f"s3://{BUCKET}/{key}"
        self.store.objects[key] = ObjectMetadata(
            key=key,
            byte_size=len(label),
            last_modified=datetime(2026, 7, 1, tzinfo=timezone.utc),
            etag=label,
        )
        return key, uri

    def _asset(self, asset_id: str, uri: str, label: str) -> dict[str, object]:
        return {
            "project_id": self.project_id,
            "asset_id": asset_id,
            "content_hash": hashlib.sha256(label.encode()).hexdigest(),
            "artifact_uri": uri,
            "content_type": "application/octet-stream",
            "byte_size": len(label),
        }

    def test_schema_has_project_fk_checks_index_and_current_head(self) -> None:
        with self.engine.connect() as connection:
            revision = connection.execute(
                sa.text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            constraints = set(
                connection.execute(
                    sa.text(
                        """
                        SELECT conname
                        FROM pg_constraint
                        WHERE conrelid =
                            'object_orphan_observations'::regclass
                        """
                    )
                ).scalars()
            )
            indexes = set(
                connection.execute(
                    sa.text(
                        """
                        SELECT indexname
                        FROM pg_indexes
                        WHERE tablename = 'object_orphan_observations'
                        """
                    )
                ).scalars()
            )
        self.assertEqual(revision, "20260731_0014")
        self.assertTrue(
            {
                "pk_object_orphan_observations",
                "fk_object_orphan_observations_project",
                "ck_object_orphan_observations_identity_nonempty",
                "ck_object_orphan_observations_counts",
                "ck_object_orphan_observations_seen_order",
            }.issubset(constraints)
        )
        self.assertIn(
            "ix_object_orphan_observations_eligibility",
            indexes,
        )

    def test_inventory_delay_reference_union_and_cleanup(self) -> None:
        t0 = datetime(2026, 7, 10, tzinfo=timezone.utc)
        unregistered_key, _ = self._object("unregistered")
        registered_key, registered_uri = self._object("registered")
        changed_key, changed_uri = self._object("changed")
        linked_key, linked_uri = self._object("snapshot-linked")
        task_key, task_uri = self._object("task-linked")
        raw_key, raw_uri = self._object("raw-snapshot")
        other_key, _ = self._object("other-project", other_project=True)
        with self.engine.begin() as connection:
            connection.execute(
                knowledge_sources.insert().values(
                    project_id=self.project_id,
                    source_id="source-a",
                    display_name="Source A",
                    source_kind="private_file",
                    trust_tier="hard_fact",
                )
            )
            connection.execute(
                source_snapshots.insert().values(
                    project_id=self.project_id,
                    snapshot_id="snapshot-a",
                    source_id="source-a",
                    content_hash=hashlib.sha256(b"snapshot").hexdigest(),
                    parser_name="test",
                    parser_version="1",
                    raw_artifact_uri=raw_uri,
                    fetched_at=t0,
                )
            )
            connection.execute(
                knowledge_assets.insert(),
                (
                    self._asset("asset-registered", registered_uri, "registered"),
                    self._asset("asset-changed", changed_uri, "changed"),
                    self._asset("asset-linked", linked_uri, "snapshot-linked"),
                    self._asset("asset-task", task_uri, "task-linked"),
                ),
            )
            connection.execute(
                snapshot_assets.insert().values(
                    project_id=self.project_id,
                    source_id="source-a",
                    snapshot_id="snapshot-a",
                    asset_id="asset-linked",
                    evidence_kind="embedded",
                    ordinal=0,
                )
            )
            connection.execute(
                article_tasks.insert().values(
                    organization_id=self.org_id,
                    project_id=self.project_id,
                    task_id="task-a",
                    position=0,
                    payload={
                        "products": [{"selected_asset_id": "asset-task"}],
                    },
                )
            )

        actor = ActorIdentity(self.org_id, self.admin_id)
        first = self.reconciler.observe(actor, self.project_id, observed_at=t0)
        self.assertEqual(first.scanned_object_count, 6)
        self.assertEqual(first.live_object_count, 3)
        self.assertEqual(
            {candidate.key for candidate in first.candidates},
            {unregistered_key, registered_key, changed_key},
        )
        self.assertEqual(first.eligible_count, 0)

        changed = self.store.objects[changed_key]
        self.store.objects[changed_key] = ObjectMetadata(
            key=changed.key,
            byte_size=changed.byte_size,
            last_modified=changed.last_modified,
            etag="new-etag",
        )
        second = self.reconciler.observe(
            actor,
            self.project_id,
            observed_at=t0 + timedelta(days=8),
        )
        eligible = {item.key for item in second.candidates if item.eligible}
        self.assertEqual(eligible, {unregistered_key, registered_key})

        report = self.reconciler.cleanup(
            actor,
            self.project_id,
            confirm_project_id=self.project_id,
            observed_at=t0 + timedelta(days=8, minutes=1),
        )

        self.assertEqual(report.eligible_count, 2)
        self.assertEqual(report.retired_registered_asset_count, 1)
        self.assertEqual(report.deleted_object_count, 2)
        self.assertEqual(report.object_delete_failure_count, 0)
        self.assertEqual(set(self.store.deleted), {unregistered_key, registered_key})
        self.assertIn(changed_key, self.store.objects)
        self.assertIn(linked_key, self.store.objects)
        self.assertIn(task_key, self.store.objects)
        self.assertIn(raw_key, self.store.objects)
        self.assertIn(other_key, self.store.objects)
        with self.engine.connect() as connection:
            asset_ids = set(
                connection.execute(
                    sa.select(knowledge_assets.c.asset_id).where(
                        knowledge_assets.c.project_id == self.project_id
                    )
                ).scalars()
            )
        self.assertNotIn("asset-registered", asset_ids)
        self.assertIn("asset-linked", asset_ids)
        self.assertEqual(len(self.audit.events), 1)
        self.assertNotIn("object_key", self.audit.events[0].details)
        self.assertNotIn("artifact_uri", self.audit.events[0].details)

    def test_confirmation_and_cross_project_authorization_fail_closed(self) -> None:
        actor = ActorIdentity(self.org_id, self.admin_id)
        with self.assertRaisesRegex(ValueError, "exactly match"):
            self.reconciler.cleanup(
                actor,
                self.project_id,
                confirm_project_id=self.other_project_id,
            )
        with self.assertRaises(ProjectAccessDenied):
            self.reconciler.observe(
                ActorIdentity(self.other_org_id, self.other_admin_id),
                self.project_id,
            )

    def test_cleanup_rechecks_task_reuse_and_delete_failure(self) -> None:
        t0 = datetime(2026, 7, 10, tzinfo=timezone.utc)
        raced_key, raced_uri = self._object("raced")
        failed_key, _ = self._object("delete-fails")
        with self.engine.begin() as connection:
            connection.execute(
                knowledge_assets.insert().values(
                    self._asset("asset-raced", raced_uri, "raced")
                )
            )
        actor = ActorIdentity(self.org_id, self.admin_id)
        self.reconciler.observe(actor, self.project_id, observed_at=t0)
        self.reconciler.observe(
            actor,
            self.project_id,
            observed_at=t0 + timedelta(days=8),
        )
        with self.engine.begin() as connection:
            connection.execute(
                article_tasks.insert().values(
                    organization_id=self.org_id,
                    project_id=self.project_id,
                    task_id="task-race",
                    position=0,
                    payload={
                        "images": [{"prepared_asset_id": "asset-raced"}],
                    },
                )
            )
        self.store.fail_delete.add(failed_key)

        report = self.reconciler.cleanup(
            actor,
            self.project_id,
            confirm_project_id=self.project_id,
            observed_at=t0 + timedelta(days=8, minutes=1),
        )

        self.assertNotIn(raced_key, self.store.deleted)
        self.assertIn(raced_key, self.store.objects)
        self.assertEqual(report.eligible_count, 1)
        self.assertEqual(report.deleted_object_count, 0)
        self.assertEqual(report.object_delete_failure_count, 1)
        with self.engine.connect() as connection:
            raced_asset = connection.execute(
                sa.select(knowledge_assets.c.asset_id).where(
                    knowledge_assets.c.project_id == self.project_id,
                    knowledge_assets.c.asset_id == "asset-raced",
                )
            ).scalar_one_or_none()
            failed_observation = connection.execute(
                sa.select(object_orphan_observations.c.object_key).where(
                    object_orphan_observations.c.organization_id == self.org_id,
                    object_orphan_observations.c.project_id == self.project_id,
                    object_orphan_observations.c.object_key == failed_key,
                )
            ).scalar_one_or_none()
        self.assertEqual(raced_asset, "asset-raced")
        self.assertIsNone(failed_observation)
        retry_inventory = self.reconciler.observe(
            actor,
            self.project_id,
            observed_at=t0 + timedelta(days=8, minutes=2),
        )
        retried = next(
            item for item in retry_inventory.candidates if item.key == failed_key
        )
        self.assertEqual(retried.sighting_count, 1)
        self.assertFalse(retried.eligible)


if __name__ == "__main__":
    unittest.main()
