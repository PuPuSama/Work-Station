from __future__ import annotations

from io import BytesIO
import sys
from pathlib import Path
import unittest

from PIL import Image


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from knowledge_agent.asset_review import (  # noqa: E402
    review_embedded_asset,
    summarize_asset_reviews,
)


def image_bytes(width: int = 640, height: int = 480, image_format: str = "PNG") -> bytes:
    output = BytesIO()
    Image.new("RGB", (width, height), color=(80, 40, 20)).save(
        output,
        format=image_format,
    )
    return output.getvalue()


class KnowledgeAssetReviewTests(unittest.TestCase):
    def test_product_image_passes_but_requires_product_confirmation_for_hero(self) -> None:
        result = review_embedded_asset(
            filename="metric-nut.jpg",
            content=image_bytes(image_format="JPEG"),
            content_type="image/jpeg",
        )

        self.assertEqual(result.decision, "approve")
        self.assertEqual(result.role, "product_image")
        self.assertFalse(result.to_metadata()["hero_eligible"])
        self.assertTrue(
            result.to_metadata()["requires_product_confirmation_for_hero"]
        )

    def test_technical_image_is_published_as_evidence_but_not_hero(self) -> None:
        result = review_embedded_asset(
            filename="DIN985-dimension-drawing.png",
            content=image_bytes(),
            content_type="image/png",
        )

        self.assertEqual(result.decision, "approve")
        self.assertEqual(result.role, "technical_illustration")
        self.assertFalse(result.to_metadata()["hero_eligible"])

    def test_small_image_is_sent_to_manual_review(self) -> None:
        result = review_embedded_asset(
            filename="icon.png",
            content=image_bytes(32, 32),
            content_type="image/png",
        )

        self.assertEqual(result.decision, "needs_review")
        self.assertFalse(result.to_metadata()["hero_eligible"])

    def test_corrupt_image_is_rejected_from_automatic_publication(self) -> None:
        result = review_embedded_asset(
            filename="broken.png",
            content=b"not an image",
            content_type="image/png",
        )

        self.assertEqual(result.decision, "reject")

    def test_summary_allows_text_publication_but_restricts_ambiguous_assets(self) -> None:
        decision, reason = summarize_asset_reviews(
            [
                {
                    "knowledge_asset_review": {
                        "decision": "needs_review",
                    }
                }
            ],
            chunk_count=2,
        )

        self.assertEqual(decision, "approve")
        self.assertIn("不会自动用于 Hero", reason)

    def test_clean_text_source_can_auto_publish_without_assets(self) -> None:
        decision, reason = summarize_asset_reviews([], chunk_count=2)

        self.assertEqual(decision, "approve")
        self.assertIn("自动审核通过", reason)


if __name__ == "__main__":
    unittest.main()
