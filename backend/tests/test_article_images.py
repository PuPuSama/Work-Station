from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from PIL import Image


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services.article_images import (  # noqa: E402
    ImageValidationError,
    build_image_audit_markdown,
    prepare_task_images,
    resolve_image_placements,
    sanitize_image_stem,
)


def make_png(path: Path, color: tuple[int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (120, 72), color).save(path, format="PNG")


def make_webp(path: Path, color: tuple[int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (120, 72), color).save(path, format="WEBP", lossless=True)


class ArticleImageTests(unittest.TestCase):
    def test_safe_names_webp_conversion_and_duplicate_product_names(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task_dir = Path(temporary) / "task"
            hero = task_dir / "incoming" / "hero.png"
            product_one = task_dir / "incoming" / "one.png"
            product_two = task_dir / "incoming" / "two.png"
            make_png(hero, (20, 40, 60))
            make_png(product_one, (80, 100, 120))
            make_png(product_two, (140, 160, 180))

            article = """# A Safe Article Title

This transition explains what the reader will learn.

## Product Options

Choose [Product Alpha](https://example.com/one) for the first use case.

The second configuration uses [Product Alpha](https://example.com/two).
"""
            task = SimpleNamespace(
                task_dir=str(task_dir),
                selected_title="Stale Selected Title",
                article=article,
                hero_image=str(hero),
                images=[],
                products=[
                    SimpleNamespace(
                        name="Product Alpha", url="https://example.com/one", image_path=str(product_one)
                    ),
                    SimpleNamespace(
                        name="Product Alpha", url="https://example.com/two", image_path=str(product_two)
                    ),
                ],
            )

            prepared = prepare_task_images(task, require_hero=True)

            self.assertEqual(
                [item["filename"] for item in prepared],
                ["A Safe Article Title.webp", "Product Alpha.webp", "Product Alpha-2.webp"],
            )
            self.assertEqual(
                [item["marker"] for item in prepared],
                [
                    "img.A Safe Article Title.webp",
                    "img.Product Alpha.webp",
                    "img.Product Alpha-2.webp",
                ],
            )
            self.assertTrue(all(item["status"] == "ready" for item in prepared))
            self.assertEqual(prepared[1]["anchor_heading"], "Product Options")
            self.assertIn("Product Alpha", prepared[1]["anchor_text"])

            for item in prepared:
                prepared_path = Path(item["prepared_path"])
                self.assertTrue(prepared_path.is_file())
                with Image.open(prepared_path) as converted:
                    self.assertEqual(converted.format, "WEBP")

            audit = build_image_audit_markdown(article, prepared)
            self.assertLess(
                audit.index("img.A Safe Article Title.webp"),
                audit.index("## Product Options"),
            )
            self.assertIn(
                "![Product Alpha](images/Product%20Alpha.webp)\nimg.Product Alpha.webp",
                audit,
            )

    def test_hero_plus_three_products_is_capped_at_three_total_images(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task_dir = Path(temporary) / "task"
            hero = task_dir / "incoming" / "hero.png"
            product_paths = [task_dir / "incoming" / f"product-{index}.png" for index in range(1, 4)]
            make_png(hero, (10, 20, 30))
            for path, color in zip(
                product_paths,
                ((60, 80, 100), (120, 140, 160), (180, 200, 220)),
            ):
                make_png(path, color)

            article = """# Three Image Limit

Opening transition.

## Products

Choose [Product One](https://example.com/one), [Product Two](https://example.com/two), or [Product Three](https://example.com/three).
"""
            task = SimpleNamespace(
                task_dir=str(task_dir),
                selected_title="Three Image Limit",
                article=article,
                hero_image=str(hero),
                images=[],
                products=[
                    SimpleNamespace(
                        name=f"Product {name}",
                        url=f"https://example.com/{name.lower()}",
                        image_path=str(path),
                    )
                    for name, path in zip(("One", "Two", "Three"), product_paths)
                ],
            )

            prepared = prepare_task_images(task, require_hero=True)

            self.assertEqual(len(prepared), 3)
            self.assertEqual(
                [item["product_name"] for item in prepared],
                ["", "Product One", "Product Two"],
            )

    def test_same_byte_product_image_is_skipped_and_later_unique_image_fills_slot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task_dir = Path(temporary) / "task"
            hero = task_dir / "incoming" / "hero.png"
            first = task_dir / "incoming" / "first.png"
            duplicate = task_dir / "incoming" / "duplicate.png"
            later_unique = task_dir / "incoming" / "later-unique.png"
            make_png(hero, (10, 30, 50))
            make_png(first, (70, 90, 110))
            duplicate.write_bytes(first.read_bytes())
            make_png(later_unique, (150, 170, 190))

            article = """# Duplicate Filter

Opening transition.

## Products

[First Product](https://example.com/first), [Duplicate Product](https://example.com/duplicate), and [Later Product](https://example.com/later) are listed here.
"""
            task = SimpleNamespace(
                task_dir=str(task_dir),
                selected_title="Duplicate Filter",
                article=article,
                hero_image=str(hero),
                images=[],
                products=[
                    SimpleNamespace(
                        name="First Product",
                        url="https://example.com/first",
                        image_path=str(first),
                    ),
                    SimpleNamespace(
                        name="Duplicate Product",
                        url="https://example.com/duplicate",
                        image_path=str(duplicate),
                    ),
                    SimpleNamespace(
                        name="Later Product",
                        url="https://example.com/later",
                        image_path=str(later_unique),
                    ),
                ],
            )

            prepared = prepare_task_images(task, require_hero=True)

            self.assertEqual(len(prepared), 3)
            self.assertEqual(
                [item["product_name"] for item in prepared],
                ["", "First Product", "Later Product"],
            )

    def test_same_visual_with_different_encoding_and_size_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task_dir = Path(temporary) / "task"
            incoming = task_dir / "incoming"
            incoming.mkdir(parents=True)
            hero = incoming / "hero.png"
            original = incoming / "original.png"
            resized_jpeg = incoming / "resized.jpg"
            later_unique = incoming / "later.png"
            make_png(hero, (15, 35, 55))
            make_png(later_unique, (170, 190, 210))

            visual = Image.new("RGB", (120, 72), (235, 235, 235))
            visual.paste((35, 95, 155), (18, 12, 102, 62))
            visual.paste((210, 120, 45), (46, 25, 74, 49))
            visual.save(original, format="PNG")
            visual.resize((240, 144), Image.Resampling.LANCZOS).save(
                resized_jpeg,
                format="JPEG",
                quality=92,
            )

            article = """# Visual Duplicate Filter

Opening transition.

## Products

[Original](https://example.com/original), [Recoded](https://example.com/recoded), and [Later](https://example.com/later) are listed here.
"""
            task = SimpleNamespace(
                task_dir=str(task_dir),
                selected_title="Visual Duplicate Filter",
                article=article,
                hero_image=str(hero),
                images=[],
                products=[
                    SimpleNamespace(
                        name="Original",
                        url="https://example.com/original",
                        image_path=str(original),
                    ),
                    SimpleNamespace(
                        name="Recoded",
                        url="https://example.com/recoded",
                        image_path=str(resized_jpeg),
                    ),
                    SimpleNamespace(
                        name="Later",
                        url="https://example.com/later",
                        image_path=str(later_unique),
                    ),
                ],
            )

            prepared = prepare_task_images(task, require_hero=True)

            self.assertEqual(
                [item["product_name"] for item in prepared],
                ["", "Original", "Later"],
            )

    def test_resolver_rejects_legacy_prepared_sets_over_three_images(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task_dir = Path(temporary)
            paths = [task_dir / f"prepared-{index}.webp" for index in range(4)]
            for path, color in zip(
                paths,
                ((10, 20, 30), (60, 70, 80), (110, 120, 130), (160, 170, 180)),
            ):
                make_webp(path, color)
            images = [
                {
                    "id": "hero" if index == 0 else f"product-{index}",
                    "role": "hero" if index == 0 else "product",
                    "prepared_path": str(path),
                    "filename": path.name,
                    "product_name": "" if index == 0 else f"Product {index}",
                    "product_url": "" if index == 0 else f"https://example.com/{index}",
                }
                for index, path in enumerate(paths)
            ]
            article = "# Legacy Images\n\nOpening transition.\n\n## Products\n\nBody."

            with self.assertRaisesRegex(ImageValidationError, "最多使用 3 张"):
                resolve_image_placements(article, images)

    def test_resolver_rejects_duplicate_legacy_prepared_images(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task_dir = Path(temporary)
            hero = task_dir / "hero.webp"
            duplicate = task_dir / "duplicate.webp"
            make_webp(hero, (40, 80, 120))
            duplicate.write_bytes(hero.read_bytes())
            images = [
                {
                    "id": "hero",
                    "role": "hero",
                    "prepared_path": str(hero),
                    "filename": hero.name,
                },
                {
                    "id": "product-1",
                    "role": "product",
                    "prepared_path": str(duplicate),
                    "filename": duplicate.name,
                    "product_name": "Duplicate Product",
                    "product_url": "https://example.com/duplicate",
                },
            ]
            article = """# Legacy Duplicate

Opening transition.

## Products

Choose [Duplicate Product](https://example.com/duplicate).
"""

            with self.assertRaisesRegex(ImageValidationError, "图片内容重复"):
                resolve_image_placements(article, images)

    def test_unresolved_product_returns_heading_choices_instead_of_end_append(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task_dir = Path(temporary) / "task"
            product_image = task_dir / "incoming" / "missing-product.png"
            make_png(product_image, (10, 20, 30))
            task = SimpleNamespace(
                task_dir=str(task_dir),
                selected_title="Anchor Test",
                article="# Anchor Test\n\nIntro paragraph.\n\n## First Choice\n\nBody.\n\n## Second Choice\n\nBody.",
                hero_image="",
                images=[],
                products=[
                    SimpleNamespace(
                        name="Absent Product",
                        url="https://example.com/absent",
                        image_path=str(product_image),
                    )
                ],
            )

            prepared = prepare_task_images(task)

            self.assertEqual(prepared[0]["status"], "needs_anchor")
            self.assertIn("请选择", prepared[0]["error"])
            self.assertEqual(
                [choice["heading"] for choice in prepared[0]["anchor_candidates"]],
                ["First Choice", "Second Choice"],
            )
            self.assertNotEqual(prepared[0]["anchor_after"], "end_of_article")

    def test_product_image_is_placed_after_the_complete_hard_wrapped_paragraph(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task_dir = Path(temporary) / "task"
            product_image = task_dir / "incoming" / "product.png"
            make_png(product_image, (60, 80, 100))
            article = """# Paragraph Placement

Opening transition.

## Product Section

Choose [Product Alpha](https://example.com/alpha) for the specified application
when the material and dimensions match the confirmed requirements.

This is a separate paragraph.
"""
            task = SimpleNamespace(
                task_dir=str(task_dir),
                selected_title="Paragraph Placement",
                article=article,
                hero_image="",
                images=[],
                products=[
                    SimpleNamespace(
                        name="Product Alpha",
                        url="https://example.com/alpha",
                        image_path=str(product_image),
                    )
                ],
            )

            prepared = prepare_task_images(task)
            continuation_index = article.splitlines().index(
                "when the material and dimensions match the confirmed requirements."
            )
            self.assertEqual(prepared[0]["anchor_line"], continuation_index)

            audit = build_image_audit_markdown(article, prepared)
            self.assertLess(
                audit.index("when the material and dimensions match"),
                audit.index("img.Product Alpha.webp"),
            )
            self.assertLess(
                audit.index("img.Product Alpha.webp"),
                audit.index("This is a separate paragraph."),
            )

    def test_manual_heading_anchor_uses_the_end_of_its_first_prose_paragraph(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task_dir = Path(temporary) / "task"
            product_image = task_dir / "incoming" / "product.png"
            make_png(product_image, (100, 80, 60))
            article = """# Manual Placement

Opening transition.

## Target Section

The first line introduces this section
and this line completes the same paragraph.

Another paragraph follows.

## FAQ

**Q: What should buyers check first?**

A: Buyers should check the application requirements first.
"""
            task = SimpleNamespace(
                task_dir=str(task_dir),
                selected_title="Manual Placement",
                article=article,
                hero_image="",
                images=[],
                products=[
                    SimpleNamespace(
                        name="Absent Product",
                        url="https://example.com/absent",
                        image_path=str(product_image),
                    )
                ],
            )

            prepared = prepare_task_images(task)
            self.assertEqual(
                [choice["heading"] for choice in prepared[0]["anchor_candidates"]],
                ["Target Section"],
            )
            prepared[0]["anchor_heading"] = "Target Section"
            prepared[0]["status"] = "pending"
            placement = resolve_image_placements(article, prepared)[0]
            expected_index = article.splitlines().index(
                "and this line completes the same paragraph."
            )
            self.assertEqual(placement.line_index, expected_index)

    def test_corrupt_image_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task_dir = Path(temporary) / "task"
            corrupt = task_dir / "incoming" / "corrupt.jpg"
            corrupt.parent.mkdir(parents=True)
            corrupt.write_text("not an image", encoding="utf-8")
            task = SimpleNamespace(
                task_dir=str(task_dir),
                selected_title="Corrupt Test",
                article="# Corrupt Test\n\nIntro.\n\n## Section\n\nBody.",
                hero_image=str(corrupt),
                images=[],
                products=[],
            )

            with self.assertRaisesRegex(ImageValidationError, "损坏|不受支持"):
                prepare_task_images(task, require_hero=True)

    def test_relative_path_traversal_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task_dir = root / "task"
            outside = root / "outside.png"
            make_png(outside, (1, 2, 3))
            task = SimpleNamespace(
                task_dir=str(task_dir),
                selected_title="Traversal Test",
                article="# Traversal Test\n\nIntro.\n\n## Section\n\nBody.",
                hero_image="../outside.png",
                images=[],
                products=[],
            )

            with self.assertRaisesRegex(ImageValidationError, "不能越过任务目录"):
                prepare_task_images(task, require_hero=True)

    def test_windows_reserved_and_invalid_characters_are_sanitized(self) -> None:
        self.assertEqual(sanitize_image_stem("CON"), "_CON")
        sanitized = sanitize_image_stem('Title: A/B? <Guide> | Test*')
        self.assertFalse(any(character in sanitized for character in '<>:"/\\|?*'))


if __name__ == "__main__":
    unittest.main()
