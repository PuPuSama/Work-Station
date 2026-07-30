from __future__ import annotations

import sys
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from models import AICheck, ArticleImage, TaskRecord  # noqa: E402
from services.delivery_package import (  # noqa: E402
    DeliveryPackageError,
    build_delivery_zip_bytes,
    build_delivery_zip,
    official_website_folder_name,
    package_delivery,
)


def task_at(root: Path) -> TaskRecord:
    return TaskRecord(
        id="delivery-test",
        week_folder="week",
        customer="https://www.Example.com/articles/",
        topic_index=1,
        topic="Delivery",
        task_dir=str(root / "task"),
        docx_path=str(root / "article.docx"),
        tdk_path=str(root / "D.docx"),
        images=[
            ArticleImage(
                id="hero",
                role="hero",
                prepared_path=str(root / "hero.webp"),
                filename="hero.webp",
            )
        ],
        initial_ai_check=AICheck(screenshot_path=str(root / "initial.png")),
        final_ai_check=AICheck(screenshot_path=str(root / "final.png")),
        created_at="2026-07-10T00:00:00",
        updated_at="2026-07-10T00:00:00",
    )


class DeliveryPackageTests(unittest.TestCase):
    def test_in_memory_zip_is_deterministic_and_keeps_flat_layout(
        self,
    ) -> None:
        arguments = {
            "article_docx": b"article",
            "article_filename": "Buyer Guide.docx",
            "tdk_docx": b"tdk",
            "images": [
                ("hero.webp", b"hero"),
                ("hero.webp", b"body"),
            ],
            "final_screenshot": b"screenshot",
        }
        first = build_delivery_zip_bytes(**arguments)
        second = build_delivery_zip_bytes(**arguments)
        self.assertEqual(first, second)
        with ZipFile(BytesIO(first)) as archive:
            self.assertEqual(
                archive.namelist(),
                [
                    "Buyer Guide.docx",
                    "D.docx",
                    "hero.webp",
                    "hero-2.webp",
                    "final-ai-rate.png",
                ],
            )
            self.assertEqual(
                archive.read("final-ai-rate.png"),
                b"screenshot",
            )

    def test_in_memory_zip_rejects_unsafe_article_filename(self) -> None:
        for filename in ("../article.docx", "final-ai-rate.png"):
            with self.subTest(filename=filename):
                with self.assertRaises(DeliveryPackageError):
                    build_delivery_zip_bytes(
                        article_docx=b"article",
                        article_filename=filename,
                        tdk_docx=b"tdk",
                        images=[("hero.webp", b"hero")],
                        final_screenshot=b"screenshot",
                    )

    def test_folder_uses_official_website_and_contains_all_deliverables(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = task_at(root)
            for path, content in (
                (Path(task.docx_path), b"article"),
                (Path(task.tdk_path), b"tdk"),
                (Path(task.images[0].prepared_path), b"image"),
                (Path(task.initial_ai_check.screenshot_path), b"initial"),
                (Path(task.final_ai_check.screenshot_path), b"final"),
            ):
                path.write_bytes(content)

            output = package_delivery(task)
            self.assertEqual(output.name, "www.example.com")
            self.assertTrue((output / "article.docx").is_file())
            self.assertTrue((output / "D.docx").is_file())
            self.assertTrue((output / "hero.webp").is_file())
            self.assertFalse((output / "initial-ai-rate.png").exists())
            self.assertTrue((output / "final-ai-rate.png").is_file())
            self.assertFalse((output / "images").exists())
            self.assertFalse((output / "AI rate screenshots").exists())
            self.assertFalse((output / "delivery_manifest.json").exists())

            second_output = package_delivery(task)
            self.assertEqual(second_output, output)
            self.assertEqual(len(list(output.glob("hero*.webp"))), 1)

    def test_screenshot_is_required(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = task_at(root)
            task.final_ai_check = AICheck()
            Path(task.docx_path).write_bytes(b"article")
            Path(task.tdk_path).write_bytes(b"tdk")
            Path(task.images[0].prepared_path).write_bytes(b"image")
            Path(task.initial_ai_check.screenshot_path).write_bytes(b"initial")
            with self.assertRaisesRegex(DeliveryPackageError, "Final AI-rate screenshot"):
                package_delivery(task)

    def test_more_than_three_article_images_cannot_be_packaged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = task_at(root)
            task.images = [
                ArticleImage(
                    id=f"image-{index}",
                    role="hero" if index == 1 else "body",
                    prepared_path=str(root / f"image-{index}.webp"),
                    filename=f"image-{index}.webp",
                )
                for index in range(1, 5)
            ]
            Path(task.docx_path).write_bytes(b"article")
            Path(task.tdk_path).write_bytes(b"tdk")
            Path(task.final_ai_check.screenshot_path).write_bytes(b"final")
            for index, image in enumerate(task.images, start=1):
                Path(image.prepared_path).write_bytes(f"image-{index}".encode("ascii"))

            with self.assertRaisesRegex(DeliveryPackageError, "at most 3 images"):
                package_delivery(task)

            self.assertFalse((Path(task.task_dir) / "www.example.com").exists())

    def test_duplicate_article_image_content_cannot_be_packaged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = task_at(root)
            task.images = [
                ArticleImage(
                    id="hero",
                    role="hero",
                    prepared_path=str(root / "hero.webp"),
                    filename="hero.webp",
                ),
                ArticleImage(
                    id="body",
                    role="body",
                    prepared_path=str(root / "different-name.webp"),
                    filename="different-name.webp",
                ),
            ]
            Path(task.docx_path).write_bytes(b"article")
            Path(task.tdk_path).write_bytes(b"tdk")
            Path(task.final_ai_check.screenshot_path).write_bytes(b"final")
            for image in task.images:
                Path(image.prepared_path).write_bytes(b"identical-image-bytes")

            with self.assertRaisesRegex(DeliveryPackageError, "Duplicate article image content"):
                package_delivery(task)

            self.assertFalse((Path(task.task_dir) / "www.example.com").exists())

    def test_repackage_removes_legacy_files_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = task_at(root)
            for path in (
                Path(task.docx_path),
                Path(task.tdk_path),
                Path(task.images[0].prepared_path),
                Path(task.final_ai_check.screenshot_path),
            ):
                path.write_bytes(b"content")

            delivery = Path(task.task_dir) / "www.example.com"
            old_image = delivery / "images" / "hero.webp"
            old_screenshot = delivery / "AI rate screenshots" / "final-ai-rate.png"
            old_image.parent.mkdir(parents=True)
            old_screenshot.parent.mkdir(parents=True)
            old_image.write_bytes(b"old-image")
            old_screenshot.write_bytes(b"old-screenshot")
            (delivery / "delivery_manifest.json").write_text("{}", encoding="utf-8")
            (delivery / "stale-file.txt").write_text("stale", encoding="utf-8")

            package_delivery(task)

            self.assertFalse((delivery / "images").exists())
            self.assertFalse((delivery / "AI rate screenshots").exists())
            self.assertFalse((delivery / "delivery_manifest.json").exists())
            self.assertFalse((delivery / "stale-file.txt").exists())
            self.assertTrue((delivery / "hero.webp").is_file())
            self.assertTrue((delivery / "final-ai-rate.png").is_file())

    def test_folder_name_parser_keeps_www_hostname(self) -> None:
        self.assertEqual(
            official_website_folder_name("https://www.Example.com/path?q=1"),
            "www.example.com",
        )

    def test_packaged_folder_can_be_downloaded_as_zip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = task_at(root)
            for path, content in (
                (Path(task.docx_path), b"article"),
                (Path(task.tdk_path), b"tdk"),
                (Path(task.images[0].prepared_path), b"image"),
                (Path(task.final_ai_check.screenshot_path), b"final"),
            ):
                path.write_bytes(content)

            task.delivery_package_path = str(package_delivery(task))
            archive = build_delivery_zip(task)

            self.assertEqual(archive.name, "www.example.com-topic_001.zip")
            with ZipFile(archive) as downloaded:
                self.assertEqual(
                    set(downloaded.namelist()),
                    {"article.docx", "D.docx", "hero.webp", "final-ai-rate.png"},
                )
                self.assertEqual(downloaded.read("article.docx"), b"article")

    def test_download_rejects_package_outside_task_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = task_at(root)
            outside = root / "outside"
            outside.mkdir()
            (outside / "article.docx").write_bytes(b"article")
            Path(task.task_dir).mkdir(parents=True)
            task.delivery_package_path = str(outside)

            with self.assertRaisesRegex(DeliveryPackageError, "outside the task directory"):
                build_delivery_zip(task)

    def test_download_requires_a_generated_package(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            task = task_at(Path(directory))

            with self.assertRaisesRegex(DeliveryPackageError, "has not been generated"):
                build_delivery_zip(task)


if __name__ == "__main__":
    unittest.main()
