from __future__ import annotations

import hashlib
import json
import sys
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from knowledge_agent.assets import KnowledgeAsset  # noqa: E402
from knowledge_agent.object_storage import (  # noqa: E402
    DELIVERY_ZIP_ARTIFACT_KIND,
    ProjectKnowledgeObject,
)
from models import TaskRecord  # noqa: E402
from services.access_control import ActorIdentity  # noqa: E402
from services.delivery_package import build_delivery_zip_bytes  # noqa: E402
from services.server_delivery_package import (  # noqa: E402
    ServerBatchDeliveryPackage,
    ServerDeliveryPackageError,
)


class BatchDeliveryObjects:
    def __init__(self) -> None:
        self.objects: dict[str, ProjectKnowledgeObject] = {}
        self.archive = b""
        self.uploaded_asset_id = ""
        self.read_calls: list[tuple[str, str]] = []

    def add(self, asset_id: str, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        self.objects[asset_id] = ProjectKnowledgeObject(
            asset=KnowledgeAsset(
                project_id="www.example.com",
                asset_id=asset_id,
                content_hash=digest,
                artifact_uri=f"s3://private/{asset_id}",
                content_type="application/zip",
                byte_size=len(data),
                metadata={"artifact_kind": DELIVERY_ZIP_ARTIFACT_KIND},
            ),
            data=data,
        )
        return digest

    def read_for_article_delivery(
        self,
        *,
        actor,
        project_id,
        asset_id,
        max_bytes,
    ) -> ProjectKnowledgeObject:
        del actor, max_bytes
        self.read_calls.append((project_id, asset_id))
        return self.objects[asset_id]

    def upload_delivery_zip(
        self,
        *,
        actor,
        project_id,
        asset_id,
        data,
    ) -> KnowledgeAsset:
        del actor
        self.archive = bytes(data)
        self.uploaded_asset_id = asset_id
        return KnowledgeAsset(
            project_id=project_id,
            asset_id=asset_id,
            content_hash=hashlib.sha256(self.archive).hexdigest(),
            artifact_uri=f"s3://private/{asset_id}",
            content_type="application/zip",
            byte_size=len(self.archive),
            metadata={"artifact_kind": DELIVERY_ZIP_ARTIFACT_KIND},
        )


def delivery_task(
    *,
    task_id: str,
    asset_id: str,
    package_hash: str,
    topic_index: int,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=task_id,
        topic_index=topic_index,
        delivery_package_asset_id=asset_id,
        delivery_package_content_hash=package_hash,
        delivery_package_filename=f"www.example.com-topic_{topic_index:03d}.zip",
    )


class ServerBatchDeliveryPackageTests(unittest.TestCase):
    def test_adds_metadata_when_repacking_a_legacy_article_package(self) -> None:
        objects = BatchDeliveryObjects()
        package = build_delivery_zip_bytes(
            article_docx=b"article",
            article_filename="Article.docx",
            tdk_docx=b"tdk",
            images=[("hero.webp", b"image")],
        )
        package_hash = objects.add("legacy-package", package)
        task = TaskRecord(
            id="legacy-task",
            week_folder="server",
            customer="project-a",
            topic_index=7,
            topic="Legacy topic",
            task_dir="/server/legacy-task",
            final_article=(
                "# Legacy title\n\n"
                "Legacy article body with [Guide]"
                "(https://example.com/guide)."
            ),
            delivery_package_asset_id="legacy-package",
            delivery_package_content_hash=package_hash,
            delivery_package_filename="project-a-topic_007.zip",
            updated_at="2026-08-31T04:05:06+00:00",
            created_at="2026-08-31T00:00:00+00:00",
        )

        ServerBatchDeliveryPackage(objects=objects).package(
            actor=ActorIdentity("org-a", "user-a"),
            project_id="anchor.example.com",
            tasks=[task],
            task_project_ids={"legacy-task": "project-a"},
        )

        with ZipFile(BytesIO(objects.archive)) as archive:
            self.assertIn(
                "project-a-topic_007/metadata.json",
                archive.namelist(),
            )
            metadata = json.loads(
                archive.read("project-a-topic_007/metadata.json")
            )
        self.assertEqual(metadata["title"], "Legacy title")
        self.assertEqual(metadata["completion_date"], "2026-08-31")
        self.assertEqual(metadata["anchor_text"], ["Guide"])

    def test_refreshes_an_old_metadata_schema_when_repacking(self) -> None:
        objects = BatchDeliveryObjects()
        package = build_delivery_zip_bytes(
            article_docx=b"article",
            article_filename="Article.docx",
            tdk_docx=b"tdk",
            images=[("hero.webp", b"image")],
            metadata=b'{"schema_version":1,"title":"stale"}\n',
        )
        package_hash = objects.add("stale-package", package)
        task = TaskRecord(
            id="stale-task",
            week_folder="server",
            customer="project-a",
            topic_index=8,
            topic="PET preform manufacturing",
            task_dir="/server/stale-task",
            final_article=(
                "# PET preform manufacturing\n\n"
                "PET preform production supports reliable bottle production."
            ),
            tdk={"keywords": ["PET preform manufacturing"]},
            delivery_package_asset_id="stale-package",
            delivery_package_content_hash=package_hash,
            delivery_package_filename="project-a-topic_008.zip",
            updated_at="2026-08-31T04:05:06+00:00",
            created_at="2026-08-31T00:00:00+00:00",
        )

        ServerBatchDeliveryPackage(objects=objects).package(
            actor=ActorIdentity("org-a", "user-a"),
            project_id="anchor.example.com",
            tasks=[task],
            task_project_ids={"stale-task": "project-a"},
        )

        with ZipFile(BytesIO(objects.archive)) as archive:
            metadata = json.loads(
                archive.read("project-a-topic_008/metadata.json")
            )
        self.assertEqual(metadata["schema_version"], 2)
        self.assertEqual(metadata["title"], "PET preform manufacturing")
        self.assertEqual(metadata["keyword_density"][0]["match_mode"], "approximate")

    def test_combines_completed_packages_into_task_folders(self) -> None:
        objects = BatchDeliveryObjects()
        package_a = build_delivery_zip_bytes(
            article_docx=b"article-a",
            article_filename="Article-a.docx",
            tdk_docx=b"tdk-a",
            images=[("hero-a.webp", b"image-a")],
            metadata=b'{"title":"A"}\n',
        )
        package_b = build_delivery_zip_bytes(
            article_docx=b"article-b",
            article_filename="Article-b.docx",
            tdk_docx=b"tdk-b",
            images=[("hero-b.webp", b"image-b")],
            metadata=b'{"title":"B"}\n',
        )
        hash_a = objects.add("package-a", package_a)
        hash_b = objects.add("package-b", package_b)
        tasks = [
            delivery_task(
                task_id="task-a",
                asset_id="package-a",
                package_hash=hash_a,
                topic_index=1,
            ),
            delivery_task(
                task_id="task-b",
                asset_id="package-b",
                package_hash=hash_b,
                topic_index=2,
            ),
        ]

        asset = ServerBatchDeliveryPackage(objects=objects).package(
            actor=ActorIdentity("org-a", "user-a"),
            project_id="anchor.example.com",
            tasks=tasks,
            task_project_ids={
                "task-a": "project-a",
                "task-b": "project-b",
            },
        )

        self.assertEqual(asset.asset_id, objects.uploaded_asset_id)
        self.assertEqual(
            objects.read_calls,
            [("project-a", "package-a"), ("project-b", "package-b")],
        )
        self.assertEqual(asset.project_id, "anchor.example.com")
        with ZipFile(BytesIO(objects.archive)) as archive:
            names = set(archive.namelist())
            self.assertEqual(
                names,
                {
                    "project-a-topic_001/Article-a.docx",
                    "project-a-topic_001/D.docx",
                    "project-a-topic_001/metadata.json",
                    "project-a-topic_001/hero-a.webp",
                    "project-b-topic_002/Article-b.docx",
                    "project-b-topic_002/D.docx",
                    "project-b-topic_002/metadata.json",
                    "project-b-topic_002/hero-b.webp",
                    "manifest.json",
                },
            )
            manifest = json.loads(archive.read("manifest.json"))
        self.assertEqual([item["task_id"] for item in manifest["items"]], ["task-a", "task-b"])
        self.assertEqual(
            [item["folder"] for item in manifest["items"]],
            ["project-a-topic_001", "project-b-topic_002"],
        )
        self.assertEqual(
            [item["project_id"] for item in manifest["items"]],
            ["project-a", "project-b"],
        )

    def test_rejects_a_stale_package_identity(self) -> None:
        objects = BatchDeliveryObjects()
        package = build_delivery_zip_bytes(
            article_docx=b"article",
            article_filename="Article.docx",
            tdk_docx=b"tdk",
            images=[("hero.webp", b"image")],
        )
        objects.add("package", package)
        task = delivery_task(
            task_id="task-a",
            asset_id="package",
            package_hash="0" * 64,
            topic_index=1,
        )

        with self.assertRaisesRegex(
            ServerDeliveryPackageError,
            "identity is inconsistent",
        ):
            ServerBatchDeliveryPackage(objects=objects).package(
                actor=ActorIdentity("org-a", "user-a"),
                project_id="www.example.com",
                tasks=[task],
            )


if __name__ == "__main__":
    unittest.main()
