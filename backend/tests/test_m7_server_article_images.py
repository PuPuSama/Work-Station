from __future__ import annotations

import hashlib
import sys
import unittest
from io import BytesIO
from pathlib import Path

from PIL import Image


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from knowledge_agent.assets import KnowledgeAsset  # noqa: E402
from knowledge_agent.object_storage import (  # noqa: E402
    ProjectKnowledgeObject,
)
from models import Product, TaskRecord  # noqa: E402
from services.access_control import ActorIdentity  # noqa: E402
from services.server_article_images import (  # noqa: E402
    ServerArticleImageAnchorRequired,
    ServerArticleImageError,
    ServerArticleImagePreparation,
    derive_webp,
)


ARTICLE = """# Product Selection Guide

This introduction helps buyers compare the available options.

## Product Alpha Applications

### Match the material

Product Alpha supports the application described in this section.

### Confirm dimensions

Compare Product Alpha dimensions with the project requirements.

## Product Beta Applications

### Review the environment

Product Beta is discussed for a different operating environment.

### Verify installation

Confirm Product Beta installation requirements before ordering.

## FAQ

**Q: What should buyers compare?**

A: Compare the published product evidence.

**Q: Should dimensions be confirmed?**

A: Yes, confirm dimensions before ordering.

**Q: Where do the images come from?**

A: They come from the published project catalog.
"""


def image_bytes(
    color: tuple[int, int, int],
    *,
    size: tuple[int, int] = (320, 240),
    image_format: str = "PNG",
) -> bytes:
    output = BytesIO()
    Image.new("RGB", size, color).save(
        output,
        format=image_format,
        quality=95,
    )
    return output.getvalue()


class FakeObjects:
    def __init__(self, values: dict[str, bytes]) -> None:
        self.values = dict(values)
        self.reads: list[str] = []
        self.uploads: list[dict[str, object]] = []

    def read_for_article_edit(
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
        digest = hashlib.sha256(data).hexdigest()
        return ProjectKnowledgeObject(
            asset=KnowledgeAsset(
                project_id=project_id,
                asset_id=asset_id,
                content_hash=digest,
                artifact_uri=f"s3://private/{asset_id}",
                content_type="image/png",
                byte_size=len(data),
                width=320,
                height=240,
            ),
            data=data,
        )

    def upload_article_derivative(self, **kwargs):
        self.uploads.append(dict(kwargs))
        data = bytes(kwargs["data"])
        return KnowledgeAsset(
            project_id=str(kwargs["project_id"]),
            asset_id=str(kwargs["asset_id"]),
            content_hash=hashlib.sha256(data).hexdigest(),
            artifact_uri=f"s3://private/{kwargs['asset_id']}",
            content_type="image/webp",
            byte_size=len(data),
            width=int(kwargs["width"]),
            height=int(kwargs["height"]),
            metadata=dict(kwargs.get("metadata") or {}),
        )


def task(*, article: str = ARTICLE) -> TaskRecord:
    return TaskRecord(
        id="task-a",
        week_folder="server",
        customer="project-a",
        topic_index=1,
        topic="Product selection",
        status="links_verified",
        task_dir="/server/task-a",
        selected_title="Product Selection Guide",
        linked_article=article,
        article=article,
        products=[
            Product(
                product_id="alpha",
                name="Product Alpha",
                url="https://project-a.test/products/alpha",
                selected_asset_id="alpha-source",
                asset_status="ready",
            ),
            Product(
                product_id="duplicate",
                name="Duplicate Hero",
                url="https://project-a.test/products/duplicate",
                selected_asset_id="hero-copy",
                asset_status="ready",
            ),
            Product(
                product_id="beta",
                name="Product Beta",
                url="https://project-a.test/products/beta",
                selected_asset_id="beta-source",
                asset_status="ready",
            ),
        ],
        created_at="2026-07-31T00:00:00+00:00",
        updated_at="2026-07-31T00:00:00+00:00",
    )


class ServerArticleImageTests(unittest.TestCase):
    def test_derivation_is_deterministic_and_rejects_invalid_bytes(
        self,
    ) -> None:
        source = image_bytes((20, 80, 160))

        first = derive_webp(source)
        second = derive_webp(source)

        self.assertEqual(first.content_hash, second.content_hash)
        self.assertEqual(first.data, second.data)
        self.assertEqual((first.width, first.height), (320, 240))
        with self.assertRaisesRegex(
            ServerArticleImageError,
            "not a supported article image",
        ):
            derive_webp(b"not-an-image")

    def test_prepares_private_assets_without_paths_and_skips_visual_duplicates(
        self,
    ) -> None:
        hero = image_bytes((180, 20, 20))
        objects = FakeObjects(
            {
                "hero-source": hero,
                "alpha-source": image_bytes((20, 150, 40)),
                "hero-copy": image_bytes(
                    (180, 20, 20),
                    size=(640, 480),
                    image_format="JPEG",
                ),
                "beta-source": image_bytes((30, 60, 190)),
            }
        )
        current = task()

        prepared = ServerArticleImagePreparation(objects).prepare(
            actor=ActorIdentity("org-a", "editor-a"),
            project_id="project-a",
            task=current,
            hero_asset_id="hero-source",
        )

        self.assertIs(prepared, current)
        self.assertEqual(prepared.status, "images_ready")
        self.assertEqual(len(prepared.images), 3)
        self.assertEqual(
            [image.source_asset_id for image in prepared.images],
            ["hero-source", "alpha-source", "beta-source"],
        )
        self.assertEqual(len(objects.uploads), 3)
        self.assertEqual(
            [image.role for image in prepared.images],
            ["hero", "product", "product"],
        )
        self.assertEqual(
            prepared.images[0].anchor_after,
            "before_first_h2",
        )
        self.assertEqual(
            prepared.images[1].anchor_heading,
            "Match the material",
        )
        self.assertEqual(
            prepared.images[2].anchor_heading,
            "Review the environment",
        )
        for image in prepared.images:
            self.assertEqual(image.source_path, "")
            self.assertEqual(image.prepared_path, "")
            self.assertTrue(image.prepared_asset_id)
            self.assertEqual(len(image.prepared_content_hash), 64)
            self.assertTrue(image.filename.endswith(".webp"))
            self.assertEqual(image.marker, f"img.{image.filename}")
        self.assertEqual(prepared.final_article, ARTICLE)
        self.assertEqual(
            prepared.article_versions[-1].source_kind,
            "server_asset_derivative",
        )

    def test_unresolved_product_anchor_writes_no_derivative(
        self,
    ) -> None:
        objects = FakeObjects(
            {
                "hero-source": image_bytes((180, 20, 20)),
                "alpha-source": image_bytes((20, 150, 40)),
                "hero-copy": image_bytes((180, 20, 20)),
                "beta-source": image_bytes((30, 60, 190)),
            }
        )
        current = task(
            article=ARTICLE.replace(
                "Product Alpha",
                "Unnamed option",
            )
        )
        before = current.model_dump(mode="json")

        with self.assertRaises(
            ServerArticleImageAnchorRequired
        ) as raised:
            ServerArticleImagePreparation(objects).prepare(
                actor=ActorIdentity("org-a", "editor-a"),
                project_id="project-a",
                task=current,
                hero_asset_id="hero-source",
            )

        self.assertEqual(
            raised.exception.unresolved[0]["product_id"],
            "alpha",
        )
        self.assertTrue(
            raised.exception.unresolved[0]["anchor_candidates"]
        )
        self.assertEqual(objects.uploads, [])
        self.assertEqual(current.model_dump(mode="json"), before)

        prepared = ServerArticleImagePreparation(objects).prepare(
            actor=ActorIdentity("org-a", "editor-a"),
            project_id="project-a",
            task=current,
            hero_asset_id="hero-source",
            product_anchors={"alpha": "Match the material"},
        )
        self.assertEqual(prepared.status, "images_ready")
        self.assertEqual(
            prepared.images[1].anchor_heading,
            "Match the material",
        )

    def test_manual_anchor_cannot_target_an_unselected_product(
        self,
    ) -> None:
        objects = FakeObjects(
            {"hero-source": image_bytes((180, 20, 20))}
        )
        current = task()

        with self.assertRaisesRegex(
            ServerArticleImageError,
            "do not match the selected products",
        ):
            ServerArticleImagePreparation(objects).prepare(
                actor=ActorIdentity("org-a", "editor-a"),
                project_id="project-a",
                task=current,
                hero_asset_id="hero-source",
                product_anchors={"attacker-product": "FAQ"},
            )

        self.assertEqual(objects.reads, [])
        self.assertEqual(objects.uploads, [])

    def test_uses_operator_product_image_choices(self) -> None:
        objects = FakeObjects(
            {
                "hero-choice": image_bytes((180, 20, 20)),
                "alpha-choice": image_bytes((20, 150, 40)),
                "beta-choice": image_bytes((30, 60, 190)),
            }
        )

        prepared = ServerArticleImagePreparation(objects).prepare(
            actor=ActorIdentity("org-a", "editor-a"),
            project_id="project-a",
            task=task(),
            hero_asset_id="hero-choice",
            product_asset_ids={
                "alpha": "alpha-choice",
                "duplicate": "hero-choice",
                "beta": "beta-choice",
            },
        )

        self.assertEqual(
            [image.source_asset_id for image in prepared.images],
            ["hero-choice", "alpha-choice", "beta-choice"],
        )
        self.assertEqual(
            [image.product_id for image in prepared.images],
            ["", "alpha", "beta"],
        )


if __name__ == "__main__":
    unittest.main()
