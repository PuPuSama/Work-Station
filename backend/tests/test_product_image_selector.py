from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from PIL import Image


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services.llm import responses_input_from_messages  # noqa: E402
from services.product_image_selector import (  # noqa: E402
    ProductImageSelectionError,
    build_contact_sheet,
    select_product_image,
)


def make_image(path: Path, size: tuple[int, int], color: tuple[int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color=color).save(path)


def asset(
    asset_id: str,
    path: Path,
    *,
    source_kind: str = "body_images",
    alt: str = "",
) -> dict[str, Any]:
    return {
        "id": asset_id,
        "local_path": str(path),
        "source_kind": source_kind,
        "source_kinds": [source_kind],
        "alt": alt,
        "title": "",
        "caption": "",
        "download_error": "",
    }


def manifest(*assets: dict[str, Any], name: str = "PET Bottle Mold") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "product_id": "pet-bottle-mold",
        "page": {"h1": name},
        "download_candidates": list(assets),
    }


class FakeVisionLLM:
    ready = True

    def __init__(self, response: str | Exception) -> None:
        self.response = response
        self.calls: list[list[dict[str, Any]]] = []

    def chat(
        self,
        messages: list[dict[str, Any]],
        temperature: float = 0.7,
        max_tokens: int = 1800,
    ) -> str:
        self.calls.append(messages)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class ContactSheetTests(unittest.TestCase):
    def test_builds_labeled_sheet_with_manifest_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first.png"
            second = root / "second.png"
            make_image(first, (180, 120), (210, 40, 40))
            make_image(second, (120, 180), (40, 90, 210))
            output = root / "contact-sheet.png"

            result = build_contact_sheet(
                manifest(
                    asset("A01", first, source_kind="main_gallery"),
                    asset("A02", second, source_kind="json_ld_product_images"),
                ),
                output,
                columns=2,
                thumbnail_size=(100, 80),
            )

            self.assertEqual(result.asset_ids, ("A01", "A02"))
            self.assertEqual(Path(result.path), output.resolve())
            self.assertEqual(result.to_dict()["asset_ids"], ["A01", "A02"])
            with Image.open(output) as sheet:
                self.assertEqual(sheet.size, (result.width, result.height))
                self.assertTrue(
                    any(
                        sheet.getpixel((x, y)) == (24, 74, 172)
                        for y in range(sheet.height)
                        for x in range(sheet.width)
                    )
                )

    def test_manifest_path_resolves_relative_image_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image_path = root / "images" / "candidate.png"
            make_image(image_path, (240, 160), (30, 140, 70))
            payload = manifest(
                asset("A01", Path("images/candidate.png"), source_kind="main_gallery")
            )
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps(payload), encoding="utf-8")

            result = build_contact_sheet(manifest_path, root / "sheet.jpg")

            self.assertEqual(result.asset_ids, ("A01",))

    def test_rejects_manifest_without_readable_local_assets(self) -> None:
        payload = manifest(
            {
                "id": "A01",
                "local_path": "missing.jpg",
                "source_kind": "main_gallery",
            }
        )
        with self.assertRaises(ProductImageSelectionError):
            build_contact_sheet(payload, Path("unused.jpg"))


class VisualSelectionTests(unittest.TestCase):
    def test_accepts_only_high_confidence_current_product_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "private-first-name.png"
            second = root / "private-second-name.png"
            make_image(first, (320, 240), (180, 50, 40))
            make_image(second, (480, 320), (40, 80, 180))
            llm = FakeVisionLLM(
                json.dumps(
                    {
                        "selected_asset_id": "A02",
                        "confidence": 0.91,
                        "reason": "Clear centered view of the complete product",
                    }
                )
            )

            result = select_product_image(
                manifest(
                    asset("A01", first, source_kind="main_gallery"),
                    asset("A02", second, source_kind="json_ld_product_images"),
                ),
                product_name="PET Bottle Mold",
                llm=llm,
                contact_sheet_path=root / "vision-sheet.jpg",
            )

            self.assertEqual(result.selected_asset_id, "A02")
            self.assertEqual(result.selection_method, "vision")
            self.assertEqual(result.confidence, 0.91)
            self.assertEqual(
                set(result.to_dict()),
                {"selected_asset_id", "confidence", "reason", "selection_method"},
            )
            serialized_messages = json.dumps(llm.calls[0])
            self.assertNotIn(str(first), serialized_messages)
            self.assertNotIn(str(second), serialized_messages)
            user_image = llm.calls[0][1]["content"][1]["image_url"]
            self.assertTrue(user_image.startswith("data:image/jpeg;base64,"))

    def test_invalid_id_and_model_generated_path_trigger_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            body = root / "body.png"
            gallery = root / "gallery.png"
            make_image(body, (900, 600), (160, 40, 40))
            make_image(gallery, (220, 180), (40, 160, 40))
            payload = manifest(
                asset(
                    "A01",
                    body,
                    source_kind="body_images",
                    alt="PET Bottle Mold",
                ),
                asset("A02", gallery, source_kind="main_gallery"),
            )

            for response in (
                '{"selected_asset_id":"A99","confidence":0.99,"reason":"Best view"}',
                (
                    '{"selected_asset_id":"A01","confidence":0.99,'
                    '"reason":"Use gallery.jpg","path":"C:\\\\secret\\\\gallery.jpg"}'
                ),
                (
                    '{"selected_asset_id":"A01","confidence":0.99,'
                    '"reason":"Use gallery.jpg"}'
                ),
            ):
                with self.subTest(response=response):
                    result = select_product_image(
                        payload,
                        llm=FakeVisionLLM(response),
                        contact_sheet_path=root / "sheet.jpg",
                    )
                    self.assertEqual(result.selected_asset_id, "A02")
                    self.assertEqual(result.selection_method, "fallback")
                    self.assertEqual(result.confidence, 0.0)

    def test_low_confidence_uses_deterministic_source_name_size_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            body = root / "body.png"
            gallery_generic = root / "gallery-generic.png"
            gallery_match = root / "gallery-match.png"
            make_image(body, (1000, 700), (130, 30, 30))
            make_image(gallery_generic, (800, 600), (30, 130, 30))
            make_image(gallery_match, (240, 180), (30, 30, 130))
            payload = manifest(
                asset(
                    "A01",
                    body,
                    source_kind="body_images",
                    alt="PET Bottle Mold",
                ),
                asset("A02", gallery_generic, source_kind="main_gallery"),
                asset(
                    "A03",
                    gallery_match,
                    source_kind="main_gallery",
                    alt="PET Bottle Mold",
                ),
            )
            llm = FakeVisionLLM(
                '{"selected_asset_id":"A01","confidence":0.30,"reason":"Possible match"}'
            )

            result = select_product_image(
                payload,
                llm=llm,
                min_confidence=0.65,
                contact_sheet_path=root / "sheet.jpg",
            )

            self.assertEqual(result.selected_asset_id, "A03")
            self.assertEqual(result.selection_method, "fallback")
            self.assertIn("confidence 0.30", result.reason)

    def test_model_exception_uses_deterministic_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            jsonld = root / "jsonld.png"
            body = root / "body.png"
            make_image(jsonld, (320, 240), (100, 90, 80))
            make_image(body, (640, 480), (80, 90, 100))

            result = select_product_image(
                manifest(
                    asset("A01", jsonld, source_kind="json_ld_product_images"),
                    asset(
                        "A02",
                        body,
                        source_kind="body_images",
                        alt="PET Bottle Mold",
                    ),
                ),
                llm=FakeVisionLLM(RuntimeError("network unavailable")),
                contact_sheet_path=root / "sheet.jpg",
            )

            self.assertEqual(result.selected_asset_id, "A01")
            self.assertEqual(result.selection_method, "fallback")


class ResponsesMultimodalCompatibilityTests(unittest.TestCase):
    def test_preserves_text_and_normalizes_multimodal_aliases(self) -> None:
        messages = [
            {"role": "system", "content": "Follow the rules."},
            {"role": "user", "content": "Plain text remains plain."},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Inspect this."},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64,AAAA",
                            "detail": "low",
                        },
                    },
                ],
            },
        ]

        converted = responses_input_from_messages(messages)

        self.assertEqual(converted[0]["role"], "developer")
        self.assertEqual(converted[0]["content"], "Follow the rules.")
        self.assertEqual(converted[1]["content"], "Plain text remains plain.")
        self.assertEqual(
            converted[2]["content"],
            [
                {"type": "input_text", "text": "Inspect this."},
                {
                    "type": "input_image",
                    "image_url": "data:image/png;base64,AAAA",
                    "detail": "low",
                },
            ],
        )


if __name__ == "__main__":
    unittest.main()
