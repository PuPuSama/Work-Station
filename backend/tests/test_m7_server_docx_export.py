from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from PIL import Image


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from knowledge_agent.assets import KnowledgeAsset  # noqa: E402
from knowledge_agent.object_storage import (  # noqa: E402
    ARTICLE_DOCX_CONTENT_TYPE,
    ProjectKnowledgeObject,
)
from models import ArticleImage, TaskRecord  # noqa: E402
from services.access_control import ActorIdentity  # noqa: E402
from services.server_docx_export import (  # noqa: E402
    ServerArticleDocxError,
    ServerArticleDocxExport,
)


ARTICLE = """# Example Buyer Guide

This introduction points readers to [example.com](https://example.com/) before the detailed guidance.

## Buyer Checks

### Confirm the application

Product Alpha supports the application described in this section.

### Compare evidence

Keep the original evidence guidance.

## FAQ

**Q: What should buyers send?**

A: Send requirements and quantities.

**Q: When should buyers request samples?**

A: Request samples before approval.

**Q: Why compare supplier capability?**

A: Capability affects quality and support.
"""


def webp_bytes(color: str) -> bytes:
    output = BytesIO()
    Image.new("RGB", (320, 240), color).save(
        output,
        format="WEBP",
        quality=90,
        method=6,
    )
    return output.getvalue()


def docx_config() -> SimpleNamespace:
    return SimpleNamespace(
        docx_font="Arial",
        title_1_size=20.0,
        title_2_size=16.0,
        title_3_size=13.0,
        body_size=11.0,
    )


class FakeObjects:
    def __init__(
        self,
        values: dict[str, bytes],
        *,
        output_kind: str = "article_docx",
    ) -> None:
        self.values = dict(values)
        self.output_kind = output_kind
        self.reads: list[str] = []
        self.uploads: list[dict[str, object]] = []

    def read_for_article_delivery(
        self,
        *,
        actor,
        project_id,
        asset_id,
        max_bytes,
    ):
        del actor, max_bytes
        self.reads.append(asset_id)
        data = self.values[asset_id]
        return ProjectKnowledgeObject(
            asset=KnowledgeAsset(
                project_id=project_id,
                asset_id=asset_id,
                content_hash=hashlib.sha256(data).hexdigest(),
                artifact_uri=f"s3://private/{asset_id}",
                content_type="image/webp",
                byte_size=len(data),
                width=320,
                height=240,
            ),
            data=data,
        )

    def upload_article_docx(self, **kwargs):
        self.uploads.append(dict(kwargs))
        data = bytes(kwargs["data"])
        return KnowledgeAsset(
            project_id=str(kwargs["project_id"]),
            asset_id=str(kwargs["asset_id"]),
            content_hash=hashlib.sha256(data).hexdigest(),
            artifact_uri=f"s3://private/{kwargs['asset_id']}",
            content_type=ARTICLE_DOCX_CONTENT_TYPE,
            byte_size=len(data),
            metadata={"artifact_kind": self.output_kind},
        )


def task(task_dir: str, values: dict[str, bytes]) -> TaskRecord:
    hero_hash = hashlib.sha256(values["hero-webp"]).hexdigest()
    product_hash = hashlib.sha256(values["product-webp"]).hexdigest()
    return TaskRecord(
        id="task-a",
        week_folder="server",
        customer="example.com",
        topic_index=1,
        topic="Buyer guide",
        status="images_ready",
        task_dir=task_dir,
        selected_title="Example Buyer Guide",
        linked_article=ARTICLE,
        final_article=ARTICLE,
        article=ARTICLE,
        images=[
            ArticleImage(
                id="hero",
                role="hero",
                source_asset_id="hero-source",
                prepared_asset_id="hero-webp",
                prepared_content_hash=hero_hash,
                width=320,
                height=240,
                filename="example-buyer-guide.webp",
                marker="img.example-buyer-guide.webp",
                anchor_after="before_first_h2",
                status="ready",
            ),
            ArticleImage(
                id="product-1",
                role="product",
                source_asset_id="product-source",
                prepared_asset_id="product-webp",
                prepared_content_hash=product_hash,
                width=320,
                height=240,
                filename="product-alpha.webp",
                marker="img.product-alpha.webp",
                product_name="Product Alpha",
                product_url="https://example.com/products/alpha",
                anchor_heading="Confirm the application",
                anchor_text=(
                    "Product Alpha supports the application described in "
                    "this section."
                ),
                status="ready",
            ),
        ],
        created_at="2026-07-31T00:00:00+00:00",
        updated_at="2026-07-31T00:00:00+00:00",
    )


class ServerArticleDocxExportTests(unittest.TestCase):
    def test_exports_verified_private_images_without_local_files(
        self,
    ) -> None:
        values = {
            "hero-webp": webp_bytes("navy"),
            "product-webp": webp_bytes("orange"),
        }
        objects = FakeObjects(values)
        with tempfile.TemporaryDirectory() as directory:
            local_task_dir = Path(directory) / "must-not-exist"
            record = task(str(local_task_dir), values)

            saved = ServerArticleDocxExport(
                config=docx_config(),  # type: ignore[arg-type]
                objects=objects,
            ).export(
                actor=ActorIdentity("org-a", "editor"),
                project_id="example.com",
                task=record,
            )

            self.assertEqual(saved.status, "docx_exported")
            self.assertEqual(saved.docx_path, "")
            self.assertTrue(saved.docx_asset_id.startswith("asset_"))
            self.assertEqual(
                len(saved.docx_content_hash),
                64,
            )
            self.assertEqual(
                saved.docx_filename,
                "Example Buyer Guide.docx",
            )
            self.assertEqual(objects.reads, ["hero-webp", "product-webp"])
            self.assertEqual(len(objects.uploads), 1)
            self.assertFalse(local_task_dir.exists())

            docx = bytes(objects.uploads[0]["data"])
            self.assertEqual(
                hashlib.sha256(docx).hexdigest(),
                saved.docx_content_hash,
            )
            with zipfile.ZipFile(BytesIO(docx)) as archive:
                media = {
                    name
                    for name in archive.namelist()
                    if name.startswith("word/media/")
                }
                self.assertEqual(len(media), 2)
                document_xml = archive.read(
                    "word/document.xml"
                ).decode("utf-8")
                self.assertIn("Example Buyer Guide", document_xml)
                self.assertIn(
                    "img.example-buyer-guide.webp",
                    document_xml,
                )
                self.assertIn("img.product-alpha.webp", document_xml)

    def test_rejects_task_image_identity_mismatch_before_upload(
        self,
    ) -> None:
        values = {
            "hero-webp": webp_bytes("navy"),
            "product-webp": webp_bytes("orange"),
        }
        record = task("/server/task-a", values)
        record.images[0].prepared_content_hash = "0" * 64
        objects = FakeObjects(values)

        with self.assertRaisesRegex(
            ServerArticleDocxError,
            "metadata is inconsistent",
        ):
            ServerArticleDocxExport(
                config=docx_config(),  # type: ignore[arg-type]
                objects=objects,
            ).export(
                actor=ActorIdentity("org-a", "editor"),
                project_id="example.com",
                task=record,
            )

        self.assertEqual(objects.reads, ["hero-webp"])
        self.assertEqual(objects.uploads, [])

    def test_rejects_missing_server_images_without_upload(
        self,
    ) -> None:
        values = {
            "hero-webp": webp_bytes("navy"),
            "product-webp": webp_bytes("orange"),
        }
        record = task("/server/task-a", values)
        record.images = []
        objects = FakeObjects(values)

        with self.assertRaises(ServerArticleDocxError):
            ServerArticleDocxExport(
                config=docx_config(),  # type: ignore[arg-type]
                objects=objects,
            ).export(
                actor=ActorIdentity("org-a", "editor"),
                project_id="example.com",
                task=record,
            )

        self.assertEqual(objects.reads, [])
        self.assertEqual(objects.uploads, [])

    def test_rejects_a_deduplicated_asset_with_the_wrong_access_kind(
        self,
    ) -> None:
        values = {
            "hero-webp": webp_bytes("navy"),
            "product-webp": webp_bytes("orange"),
        }
        record = task("/server/task-a", values)
        objects = FakeObjects(values, output_kind="knowledge_upload")

        with self.assertRaisesRegex(
            ServerArticleDocxError,
            "stored Word document identity is inconsistent",
        ):
            ServerArticleDocxExport(
                config=docx_config(),  # type: ignore[arg-type]
                objects=objects,
            ).export(
                actor=ActorIdentity("org-a", "editor"),
                project_id="example.com",
                task=record,
            )

        self.assertEqual(record.status, "images_ready")
        self.assertEqual(record.docx_asset_id, "")
        self.assertEqual(len(objects.uploads), 1)
