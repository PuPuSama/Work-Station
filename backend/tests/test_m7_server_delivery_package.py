from __future__ import annotations

import hashlib
import json
import sys
import unittest
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

from PIL import Image


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from knowledge_agent.assets import KnowledgeAsset  # noqa: E402
from knowledge_agent.object_storage import (  # noqa: E402
    ARTICLE_DOCX_ARTIFACT_KIND,
    ARTICLE_DOCX_CONTENT_TYPE,
    DELIVERY_ZIP_ARTIFACT_KIND,
    FINAL_AI_SCREENSHOT_ARTIFACT_KIND,
    ProjectKnowledgeObject,
    TDK_DOCX_ARTIFACT_KIND,
)
from models import (  # noqa: E402
    AICheck,
    ArticleImage,
    KnowledgeCoverageCheck,
    TaskRecord,
    TdkMetadata,
)
from services.access_control import ActorIdentity  # noqa: E402
from services.server_delivery_package import (  # noqa: E402
    ServerDeliveryPackage,
    ServerDeliveryPackageError,
)
from storage import content_hash  # noqa: E402


def webp_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (320, 240), "navy").save(output, format="WEBP")
    return output.getvalue()


class RecordingDeliveryObjects:
    def __init__(self) -> None:
        self.objects: dict[str, ProjectKnowledgeObject] = {}
        self.archive = b""

    def add(
        self,
        asset_id: str,
        data: bytes,
        content_type: str,
        artifact_kind: str | None,
        *,
        width: int | None = None,
        height: int | None = None,
    ) -> None:
        digest = hashlib.sha256(data).hexdigest()
        self.objects[asset_id] = ProjectKnowledgeObject(
            asset=KnowledgeAsset(
                project_id="www.example.com",
                asset_id=asset_id,
                content_hash=digest,
                artifact_uri=f"s3://private/{asset_id}",
                content_type=content_type,
                byte_size=len(data),
                width=width,
                height=height,
                metadata=(
                    {"artifact_kind": artifact_kind}
                    if artifact_kind
                    else {}
                ),
            ),
            data=data,
        )

    def read_for_article_delivery(
        self,
        *,
        actor,
        project_id,
        asset_id,
        max_bytes,
    ):
        del actor, project_id, max_bytes
        return self.objects[asset_id]

    def upload_delivery_zip(
        self,
        *,
        actor,
        project_id,
        asset_id,
        data,
    ):
        del actor
        self.archive = bytes(data)
        return KnowledgeAsset(
            project_id=project_id,
            asset_id=asset_id,
            content_hash=hashlib.sha256(self.archive).hexdigest(),
            artifact_uri=f"s3://private/{asset_id}",
            content_type="application/zip",
            byte_size=len(self.archive),
            metadata={"artifact_kind": DELIVERY_ZIP_ARTIFACT_KIND},
        )


def prepared() -> tuple[TaskRecord, RecordingDeliveryObjects]:
    objects = RecordingDeliveryObjects()
    article = b"article-docx"
    tdk = b"tdk-docx"
    screenshot = b"final-screenshot"
    image = webp_bytes()
    objects.add(
        "article",
        article,
        ARTICLE_DOCX_CONTENT_TYPE,
        ARTICLE_DOCX_ARTIFACT_KIND,
    )
    objects.add(
        "tdk",
        tdk,
        ARTICLE_DOCX_CONTENT_TYPE,
        TDK_DOCX_ARTIFACT_KIND,
    )
    objects.add(
        "screenshot",
        screenshot,
        "image/png",
        FINAL_AI_SCREENSHOT_ARTIFACT_KIND,
        width=640,
        height=360,
    )
    objects.add(
        "image",
        image,
        "image/webp",
        None,
        width=320,
        height=240,
    )
    task = TaskRecord(
        id="delivery-task",
        week_folder="server",
        customer="www.example.com",
        topic_index=6,
        topic="Delivery",
        status="docx_exported",
        task_dir="/server/delivery-task",
        humanized_article="Reviewed article",
        docx_asset_id="article",
        docx_content_hash=hashlib.sha256(article).hexdigest(),
        docx_filename="Buyer Guide.docx",
        tdk_asset_id="tdk",
        tdk_content_hash=hashlib.sha256(tdk).hexdigest(),
        tdk_filename="D.docx",
        final_ai_check=AICheck(
            confirmed=True,
            screenshot_asset_id="screenshot",
            screenshot_content_hash=hashlib.sha256(
                screenshot
            ).hexdigest(),
            screenshot_filename="final-ai-rate.png",
            screenshot_width=640,
            screenshot_height=360,
            article_hash=content_hash("Reviewed article"),
        ),
        images=[
            ArticleImage(
                id="hero",
                role="hero",
                prepared_asset_id="image",
                prepared_content_hash=hashlib.sha256(image).hexdigest(),
                width=320,
                height=240,
                filename="hero.webp",
            )
        ],
        created_at="2026-07-31T00:00:00+00:00",
        updated_at="2026-07-31T00:00:00+00:00",
    )
    return task, objects


class ServerDeliveryPackageTests(unittest.TestCase):
    def test_packages_verified_assets_without_local_paths(self) -> None:
        task, objects = prepared()
        task.final_article = (
            "# Industrial Floors\n\n"
            "Industrial floors protect equipment. Industrial floors support "
            "busy facilities. See [Industrial floors guide]"
            "(https://www.example.com/floors)."
        )
        task.selected_title = "Fallback title"
        task.tdk = TdkMetadata(keywords=["industrial floors", "equipment"])
        task.final_ai_check.provider = "zerogpt"
        task.final_ai_check.score = 18.5
        task.final_ai_check.checked_at = "2026-07-31T01:02:03+00:00"
        task.knowledge_coverage = KnowledgeCoverageCheck(
            status="available",
            eligible_sentences=4,
            supported_sentences=3,
            sentence_coverage=0.75,
            evidence_link_count=1,
            checked_at="2026-07-31T01:03:04+00:00",
        )
        saved = ServerDeliveryPackage(objects=objects).package(
            actor=ActorIdentity("org-a", "editor-a"),
            project_id="www.example.com",
            task=task,
        )
        self.assertEqual(saved.delivery_package_path, "")
        self.assertTrue(saved.delivery_package_asset_id)
        self.assertEqual(
            saved.delivery_package_filename,
            "www.example.com-topic_006.zip",
        )
        with ZipFile(BytesIO(objects.archive)) as archive:
            self.assertEqual(
                set(archive.namelist()),
                {
                    "Buyer Guide.docx",
                    "D.docx",
                    "metadata.json",
                    "hero.webp",
                    "final-ai-rate.png",
                },
            )
            metadata = json.loads(archive.read("metadata.json"))
        self.assertEqual(metadata["schema_version"], 2)
        self.assertEqual(metadata["title"], "Industrial Floors")
        self.assertEqual(metadata["completion_date"], "2026-07-31")
        self.assertEqual(metadata["keywords"], ["industrial floors", "equipment"])
        self.assertEqual(
            [item["keyword"] for item in metadata["keyword_density"]],
            ["industrial floors", "equipment"],
        )
        self.assertEqual(metadata["keyword_density"][0]["occurrences"], 4)
        self.assertEqual(metadata["keyword_density"][1]["occurrences"], 1)
        self.assertEqual(metadata["keyword_density"][0]["match_mode"], "exact")
        self.assertEqual(metadata["ai_rate_percent"], 18.5)
        self.assertEqual(metadata["knowledge_base_citation_rate_percent"], 75.0)
        self.assertEqual(metadata["anchor_text"], ["Industrial floors guide"])
        self.assertEqual(metadata["anchors"][0]["occurrences"], 1)

    def test_requires_confirmation_and_matching_asset_identity(self) -> None:
        task, objects = prepared()
        task.final_ai_check.confirmed = False
        with self.assertRaisesRegex(
            ServerDeliveryPackageError,
            "must be confirmed",
        ):
            ServerDeliveryPackage(objects=objects).package(
                actor=ActorIdentity("org-a", "editor-a"),
                project_id="www.example.com",
                task=task,
            )
        task, objects = prepared()
        task.tdk_content_hash = "0" * 64
        with self.assertRaisesRegex(
            ServerDeliveryPackageError,
            "identity is inconsistent",
        ):
            ServerDeliveryPackage(objects=objects).package(
                actor=ActorIdentity("org-a", "editor-a"),
                project_id="www.example.com",
                task=task,
            )
        task, objects = prepared()
        task.humanized_article = "Changed after review"
        with self.assertRaisesRegex(
            ServerDeliveryPackageError,
            "does not match the current article",
        ):
            ServerDeliveryPackage(objects=objects).package(
                actor=ActorIdentity("org-a", "editor-a"),
                project_id="www.example.com",
                task=task,
            )

    def test_skipped_humanization_does_not_require_a_second_screenshot(self) -> None:
        task, objects = prepared()
        task.humanization_skipped = True
        task.final_ai_check = task.final_ai_check.model_copy(
            update={
                "score": 12.5,
                "screenshot_asset_id": "",
                "screenshot_content_hash": "",
                "screenshot_filename": "",
                "screenshot_width": None,
                "screenshot_height": None,
            }
        )
        objects.objects.pop("screenshot")

        ServerDeliveryPackage(objects=objects).package(
            actor=ActorIdentity("org-a", "editor-a"),
            project_id="www.example.com",
            task=task,
        )

        with ZipFile(BytesIO(objects.archive)) as archive:
            self.assertNotIn("final-ai-rate.png", archive.namelist())


if __name__ == "__main__":
    unittest.main()
