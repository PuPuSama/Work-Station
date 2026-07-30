from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from knowledge_agent.assets import (  # noqa: E402
    KnowledgeAsset,
    KnowledgeAssetConflictError,
)
from knowledge_agent.object_storage import (  # noqa: E402
    KnowledgeObjectNotFound,
    ProjectKnowledgeObjectService,
    ScopedS3ArtifactStore,
)
from services.access_control import (  # noqa: E402
    ActorIdentity,
    ProjectAccessDenied,
)
from services.object_store import StoredObject  # noqa: E402


class FakeAccess:
    def __init__(self, allowed: set[str]) -> None:
        self.allowed = allowed
        self.calls: list[tuple[ActorIdentity, str, str]] = []

    def require(self, actor, project_id, permission):
        self.calls.append((actor, project_id, permission))
        if permission not in self.allowed:
            raise ProjectAccessDenied("project access denied")


class FakeStore:
    def __init__(self) -> None:
        self.put_calls: list[dict[str, object]] = []
        self.signed: list[tuple[str, int]] = []

    def put(self, *, key, data, content_type, metadata=None):
        self.put_calls.append(
            {
                "key": key,
                "data": data,
                "content_type": content_type,
                "metadata": metadata,
            }
        )
        return StoredObject(
            key=key,
            content_hash=hashlib.sha256(data).hexdigest(),
            content_type=content_type,
            byte_size=len(data),
            etag="etag",
        )

    def get(self, key, *, max_bytes):
        raise AssertionError("not used")

    def create_download_url(self, key, *, expires_seconds):
        self.signed.append((key, expires_seconds))
        return f"https://signed.example.test/{key}"

    def delete(self, key):
        raise AssertionError("not used")


class FakeAssetRepository:
    def __init__(self) -> None:
        self.assets: dict[tuple[str, str], KnowledgeAsset] = {}

    def put_asset(self, asset):
        duplicate = next(
            (
                existing
                for (project_id, _asset_id), existing in self.assets.items()
                if project_id == asset.project_id
                and existing.content_hash == asset.content_hash
            ),
            None,
        )
        if duplicate is not None:
            return duplicate
        self.assets[(asset.project_id, asset.asset_id)] = asset
        return asset

    def get_asset(self, project_id, asset_id):
        return self.assets.get((project_id, asset_id))

    def link_snapshot_asset(self, link):
        raise AssertionError("not used")

    def list_snapshot_assets(self, project_id, source_id, snapshot_id):
        return ()


class KnowledgeObjectStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.actor = ActorIdentity("org-a", "editor")
        self.access = FakeAccess({"knowledge.edit", "project.view"})
        self.store = FakeStore()
        self.repository = FakeAssetRepository()
        self.service = ProjectKnowledgeObjectService(
            store=self.store,
            bucket="private-bucket",
            repository=self.repository,
            access=self.access,  # type: ignore[arg-type]
        )

    def test_upload_binds_scope_hash_and_existing_asset_contract(self) -> None:
        stored = self.service.upload(
            actor=self.actor,
            project_id="project-a",
            asset_id="product-image-1",
            data=b"image",
            content_type="image/webp",
            width=800,
            height=600,
            metadata={"role": "primary"},
        )
        repeated = self.service.upload(
            actor=self.actor,
            project_id="project-a",
            asset_id="product-image-1",
            data=b"image",
            content_type="image/webp",
            width=800,
            height=600,
        )

        self.assertEqual(repeated, stored)
        self.assertEqual(len(self.store.put_calls), 1)
        self.assertEqual(stored.metadata["organization_id"], "org-a")
        self.assertEqual(stored.metadata["created_by_user_id"], "editor")
        self.assertTrue(
            stored.artifact_uri.startswith(
                "s3://private-bucket/organizations/org-a/"
                "projects/project-a/blobs/"
            )
        )
        self.assertEqual(
            self.access.calls[0][2],
            "knowledge.edit",
        )
        with self.assertRaisesRegex(
            KnowledgeAssetConflictError,
            "different content",
        ):
            self.service.upload(
                actor=self.actor,
                project_id="project-a",
                asset_id="product-image-1",
                data=b"different",
                content_type="image/webp",
            )

    def test_download_reauthorizes_and_rejects_mismatched_key_scope(self) -> None:
        asset = self.service.upload(
            actor=self.actor,
            project_id="project-a",
            asset_id="asset-a",
            data=b"asset",
            content_type="application/octet-stream",
        )
        url = self.service.create_download_url(
            actor=self.actor,
            project_id="project-a",
            asset_id=asset.asset_id,
            expires_seconds=120,
        )
        self.assertTrue(url.startswith("https://signed.example.test/"))
        self.assertEqual(self.access.calls[-1][2], "project.view")

        mismatched = KnowledgeAsset(
            project_id="project-a",
            asset_id="bad-scope",
            content_hash=hashlib.sha256(b"bad").hexdigest(),
            artifact_uri=(
                "s3://private-bucket/organizations/org-b/"
                "projects/project-a/blobs/00/bad"
            ),
            content_type="application/octet-stream",
            byte_size=3,
        )
        self.repository.assets[("project-a", "bad-scope")] = mismatched
        with self.assertRaisesRegex(
            KnowledgeObjectNotFound,
            "^knowledge object not found$",
        ):
            self.service.create_download_url(
                actor=self.actor,
                project_id="project-a",
                asset_id="bad-scope",
            )

    def test_denied_actor_never_touches_store_or_repository(self) -> None:
        service = ProjectKnowledgeObjectService(
            store=self.store,
            bucket="private-bucket",
            repository=self.repository,
            access=FakeAccess(set()),  # type: ignore[arg-type]
        )
        with self.assertRaisesRegex(
            ProjectAccessDenied,
            "^project access denied$",
        ):
            service.upload(
                actor=ActorIdentity("org-b", "intruder"),
                project_id="project-a",
                asset_id="blocked",
                data=b"private",
                content_type="application/octet-stream",
            )
        self.assertEqual(self.store.put_calls, [])
        self.assertEqual(self.repository.assets, {})

    def test_m2_artifact_adapter_is_bound_to_one_project(self) -> None:
        adapter = ScopedS3ArtifactStore(
            store=self.store,
            bucket="private-bucket",
            organization_id="org-a",
            project_id="project-a",
        )
        content = b"product image"
        uri = adapter.put(
            project_id="project-a",
            namespace="images",
            content_hash=hashlib.sha256(content).hexdigest(),
            filename="hero.webp",
            content=content,
        )

        self.assertTrue(
            uri.startswith("s3://private-bucket/organizations/org-a/")
        )
        self.assertEqual(
            self.store.put_calls[-1]["content_type"],
            "image/webp",
        )
        with self.assertRaisesRegex(ValueError, "bound scope"):
            adapter.put(
                project_id="project-b",
                namespace="images",
                content_hash=hashlib.sha256(content).hexdigest(),
                filename="hero.webp",
                content=content,
            )
        with self.assertRaisesRegex(ValueError, "does not match"):
            adapter.put(
                project_id="project-a",
                namespace="images",
                content_hash="0" * 64,
                filename="hero.webp",
                content=content,
            )


if __name__ == "__main__":
    unittest.main()
