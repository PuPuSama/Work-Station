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
    ARTICLE_DOCX_CONTENT_TYPE,
    DELIVERY_ZIP_ARTIFACT_KIND,
    FINAL_AI_SCREENSHOT_ARTIFACT_KIND,
    INITIAL_AI_SCREENSHOT_ARTIFACT_KIND,
    KnowledgeObjectIntegrityError,
    KnowledgeObjectNotFound,
    ProjectKnowledgeObjectService,
    ScopedS3ArtifactStore,
    TDK_DOCX_ARTIFACT_KIND,
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
        self.sign_options: list[dict[str, object]] = []
        self.objects: dict[str, bytes] = {}

    def check_ready(self):
        return None

    def put(self, *, key, data, content_type, metadata=None):
        self.objects[key] = bytes(data)
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
        return self.objects[key][: max_bytes + 1]

    def create_download_url(
        self,
        key,
        *,
        expires_seconds,
        response_content_type=None,
        response_content_disposition=None,
    ):
        self.signed.append((key, expires_seconds))
        self.sign_options.append(
            {
                "response_content_type": response_content_type,
                "response_content_disposition": response_content_disposition,
            }
        )
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
        self.access = FakeAccess(
            {
                "knowledge.edit",
                "article.edit",
                "article.deliver",
                "article.review",
                "project.view",
            }
        )
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

        self.repository.assets[("project-a", "product-image-1")] = (
            KnowledgeAsset(
                project_id="project-a",
                asset_id="product-image-1",
                content_hash=stored.content_hash,
                artifact_uri=(
                    "s3://private-bucket/organizations/org-b/"
                    "projects/project-a/blobs/00/mismatched"
                ),
                content_type=stored.content_type,
                byte_size=stored.byte_size,
                width=stored.width,
                height=stored.height,
            )
        )
        with self.assertRaisesRegex(
            KnowledgeObjectNotFound,
            "^knowledge object not found$",
        ):
            self.service.upload(
                actor=self.actor,
                project_id="project-a",
                asset_id="product-image-1",
                data=b"image",
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

    def test_article_read_and_derivative_reauthorize_and_verify_bytes(
        self,
    ) -> None:
        source = self.service.upload(
            actor=self.actor,
            project_id="project-a",
            asset_id="source-image",
            data=b"source-image-bytes",
            content_type="image/png",
            width=320,
            height=240,
        )

        loaded = self.service.read_for_article_edit(
            actor=self.actor,
            project_id="project-a",
            asset_id=source.asset_id,
            max_bytes=1024,
        )

        self.assertEqual(loaded.asset, source)
        self.assertEqual(loaded.data, b"source-image-bytes")
        self.assertEqual(self.access.calls[-1][2], "article.edit")

        derived = self.service.upload_article_derivative(
            actor=self.actor,
            project_id="project-a",
            asset_id="derived-webp",
            data=b"derived-webp-bytes",
            width=320,
            height=240,
            metadata={"difference_hash": "0000000000000000"},
        )
        self.assertEqual(derived.content_type, "image/webp")
        self.assertEqual(
            derived.metadata["derivative_kind"],
            "article_image_webp",
        )
        self.assertEqual(
            derived.metadata["difference_hash"],
            "0000000000000000",
        )
        self.assertNotIn("source_asset_id", derived.metadata)
        self.assertNotIn("article_image_role", derived.metadata)
        self.assertEqual(self.access.calls[-1][2], "article.edit")

        delivery_image = self.service.read_for_article_delivery(
            actor=self.actor,
            project_id="project-a",
            asset_id=derived.asset_id,
            max_bytes=1024,
        )
        self.assertEqual(delivery_image.data, b"derived-webp-bytes")
        self.assertEqual(
            self.access.calls[-1][2],
            "article.deliver",
        )

        docx = self.service.upload_article_docx(
            actor=self.actor,
            project_id="project-a",
            asset_id="article-docx",
            data=b"private-word-document",
        )
        self.assertEqual(docx.content_type, ARTICLE_DOCX_CONTENT_TYPE)
        self.assertEqual(
            docx.metadata["artifact_kind"],
            "article_docx",
        )
        self.assertEqual(
            self.access.calls[-1][2],
            "article.deliver",
        )
        with self.assertRaisesRegex(
            KnowledgeObjectNotFound,
            "^knowledge object not found$",
        ):
            self.service.create_download_url(
                actor=self.actor,
                project_id="project-a",
                asset_id=docx.asset_id,
            )
        docx_url = self.service.create_article_docx_download_url(
            actor=self.actor,
            project_id="project-a",
            asset_id=docx.asset_id,
            filename="Buyer Guide.docx",
            expires_seconds=90,
        )
        self.assertTrue(docx_url.startswith("https://signed.example.test/"))
        self.assertEqual(
            self.store.sign_options[-1],
            {
                "response_content_type": ARTICLE_DOCX_CONTENT_TYPE,
                "response_content_disposition": (
                    'attachment; filename="Buyer Guide.docx"; '
                    "filename*=UTF-8''Buyer%20Guide.docx"
                ),
            },
        )
        self.assertEqual(
            self.access.calls[-1][2],
            "article.deliver",
        )
        delivery_zip = self.service.upload_delivery_zip(
            actor=self.actor,
            project_id="project-a",
            asset_id="delivery-zip",
            data=b"private-delivery-archive",
        )
        self.assertEqual(delivery_zip.content_type, "application/zip")
        self.assertEqual(
            delivery_zip.metadata["artifact_kind"],
            DELIVERY_ZIP_ARTIFACT_KIND,
        )
        with self.assertRaisesRegex(
            KnowledgeObjectNotFound,
            "^knowledge object not found$",
        ):
            self.service.create_download_url(
                actor=self.actor,
                project_id="project-a",
                asset_id=delivery_zip.asset_id,
            )
        delivery_url = self.service.create_delivery_zip_download_url(
            actor=self.actor,
            project_id="project-a",
            asset_id=delivery_zip.asset_id,
            content_hash=delivery_zip.content_hash,
            filename="yehui-topic_001",
            expires_seconds=90,
        )
        self.assertTrue(
            delivery_url.startswith("https://signed.example.test/")
        )
        self.assertEqual(
            self.store.sign_options[-1]["response_content_disposition"],
            (
                'attachment; filename="yehui-topic_001.zip"; '
                "filename*=UTF-8''yehui-topic_001.zip"
            ),
        )
        self.assertEqual(
            self.access.calls[-1][2],
            "article.deliver",
        )
        tdk = self.service.upload_tdk_docx(
            actor=self.actor,
            project_id="project-a",
            asset_id="tdk-docx",
            data=b"private-tdk-word-document",
        )
        self.assertEqual(tdk.content_type, ARTICLE_DOCX_CONTENT_TYPE)
        self.assertEqual(
            tdk.metadata["artifact_kind"],
            TDK_DOCX_ARTIFACT_KIND,
        )
        with self.assertRaisesRegex(
            KnowledgeObjectNotFound,
            "^knowledge object not found$",
        ):
            self.service.create_download_url(
                actor=self.actor,
                project_id="project-a",
                asset_id=tdk.asset_id,
            )
        with self.assertRaisesRegex(
            KnowledgeObjectNotFound,
            "^knowledge object not found$",
        ):
            self.service.create_article_docx_download_url(
                actor=self.actor,
                project_id="project-a",
                asset_id=tdk.asset_id,
            )
        tdk_url = self.service.create_tdk_docx_download_url(
            actor=self.actor,
            project_id="project-a",
            asset_id=tdk.asset_id,
            expires_seconds=90,
        )
        self.assertTrue(tdk_url.startswith("https://signed.example.test/"))
        self.assertEqual(
            self.access.calls[-1][2],
            "article.deliver",
        )
        screenshot = self.service.upload_final_ai_screenshot(
            actor=self.actor,
            project_id="project-a",
            asset_id="final-ai-screenshot",
            data=b"normalized-png-bytes",
            width=640,
            height=360,
        )
        self.assertEqual(screenshot.content_type, "image/png")
        self.assertEqual(
            screenshot.metadata["artifact_kind"],
            FINAL_AI_SCREENSHOT_ARTIFACT_KIND,
        )
        self.assertEqual(
            self.access.calls[-1][2],
            "article.review",
        )
        initial_screenshot = self.service.upload_initial_ai_screenshot(
            actor=self.actor,
            project_id="project-a",
            asset_id="initial-ai-screenshot",
            data=b"normalized-initial-png-bytes",
            width=800,
            height=450,
        )
        self.assertEqual(
            initial_screenshot.metadata["artifact_kind"],
            INITIAL_AI_SCREENSHOT_ARTIFACT_KIND,
        )
        initial_url = (
            self.service.create_initial_ai_screenshot_download_url(
                actor=self.actor,
                project_id="project-a",
                asset_id=initial_screenshot.asset_id,
                content_hash=initial_screenshot.content_hash,
                width=800,
                height=450,
                expires_seconds=90,
            )
        )
        self.assertTrue(
            initial_url.startswith("https://signed.example.test/")
        )
        with self.assertRaisesRegex(
            KnowledgeObjectNotFound,
            "^knowledge object not found$",
        ):
            self.service.create_final_ai_screenshot_download_url(
                actor=self.actor,
                project_id="project-a",
                asset_id=initial_screenshot.asset_id,
                content_hash=initial_screenshot.content_hash,
                width=800,
                height=450,
            )
        with self.assertRaisesRegex(
            KnowledgeObjectNotFound,
            "^knowledge object not found$",
        ):
            self.service.create_download_url(
                actor=self.actor,
                project_id="project-a",
                asset_id=screenshot.asset_id,
            )
        screenshot_url = (
            self.service.create_final_ai_screenshot_download_url(
                actor=self.actor,
                project_id="project-a",
                asset_id=screenshot.asset_id,
                content_hash=screenshot.content_hash,
                width=640,
                height=360,
                expires_seconds=90,
            )
        )
        self.assertTrue(
            screenshot_url.startswith("https://signed.example.test/")
        )
        self.assertEqual(
            self.access.calls[-1][2],
            "article.review",
        )
        opaque_screenshot = self.service.upload_final_ai_screenshot(
            actor=self.actor,
            project_id="project-a",
            asset_id="final-ai-opaque-screenshot",
            data=b"opaque-jpeg-bytes",
            content_type="image/jpeg",
            width=None,
            height=None,
        )
        self.assertEqual(opaque_screenshot.content_type, "image/jpeg")
        opaque_url = self.service.create_final_ai_screenshot_download_url(
            actor=self.actor,
            project_id="project-a",
            asset_id=opaque_screenshot.asset_id,
            content_hash=opaque_screenshot.content_hash,
            width=None,
            height=None,
        )
        self.assertTrue(opaque_url.startswith("https://signed.example.test/"))
        with self.assertRaisesRegex(
            KnowledgeObjectNotFound,
            "^knowledge object not found$",
        ):
            self.service.create_final_ai_screenshot_download_url(
                actor=self.actor,
                project_id="project-a",
                asset_id=screenshot.asset_id,
                content_hash="0" * 64,
                width=640,
                height=360,
            )

        source_key = str(source.metadata["object_key"])
        self.store.objects[source_key] = b"corrupted"
        with self.assertRaisesRegex(
            KnowledgeObjectIntegrityError,
            "integrity verification failed",
        ):
            self.service.read_for_article_edit(
                actor=self.actor,
                project_id="project-a",
                asset_id=source.asset_id,
                max_bytes=1024,
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
