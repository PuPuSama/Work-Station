from __future__ import annotations

import sys
import tempfile
import unittest
from io import BytesIO
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image
from docx import Document


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import app as app_module  # noqa: E402
from config import load_config  # noqa: E402
from models import (  # noqa: E402
    Product,
    STATUS_OUTLINE_CONFIRMED,
    STATUS_OUTLINE_READY,
    TaskRecord,
    TdkMetadata,
)
from storage import TaskStore  # noqa: E402


ARTICLE = """# Example Buyer Guide

This introduction explains the buying context and points readers to the [company website](https://example.com/).

## What Should Buyers Check First?

Start with the real application, required specifications, order quantity, and quality expectations.

## FAQ

**Q: What should you send with an inquiry?**

A: Send the application, dimensions, quantity, and destination market.

**Q: When should you request a sample?**

A: Request one before approval when fit or finish must be confirmed.

**Q: Why should you compare supplier capability?**

A: Capability affects quality control, timing, and support.
"""

EXTERNAL_HUMANIZED_ARTICLE = """# Example Buyer Guide

This practical introduction helps buyers define the sourcing context before comparing offers from the [company website](https://example.com/).

## What Should Buyers Check First?

Begin with the real application, exact specifications, order quantity, and documented quality expectations.

## FAQ

**Q: What should you send with an inquiry?**

A: Send the application, dimensions, quantity, and destination market.

**Q: When should you request a sample?**

A: Request one before approval when fit or finish must be confirmed.

**Q: Why should you compare supplier capability?**

A: Capability affects quality control, timing, and support.
"""

LONG_ARTICLE = """# Example Buyer Guide

This introduction gives buyers enough context before the detailed section.

## Detailed Buyer Considerations

""" + " ".join("word" for _ in range(1821)) + """

## FAQ

**Q: What should buyers check first?**

A: Buyers should check the application requirements first.

**Q: When should buyers request a sample?**

A: Buyers should request one before approval when fit matters.

**Q: Why should buyers compare suppliers?**

A: Buyers should compare capability, quality control, delivery, and support.
"""


class WorkflowApiTests(unittest.TestCase):
    def test_initial_checkpoint_requires_a_known_markdown_link(self) -> None:
        task = TaskRecord(
            id="link-readiness-test",
            week_folder="week",
            customer="example.com",
            topic_index=1,
            topic="Example",
            status="draft_ready",
            task_dir="D:/article/link-readiness-test",
            selected_title="Example Buyer Guide",
            initial_article=ARTICLE.replace(
                "[company website](https://example.com/)",
                "company website",
            ),
            created_at="2026-07-10T00:00:00",
            updated_at="2026-07-10T00:00:00",
        )

        issues = app_module.initial_readiness_issues(task)

        self.assertTrue(any("Markdown 超链接" in issue for issue in issues))

    def test_product_processing_guard_blocks_a_second_mutation(self) -> None:
        task = TaskRecord(
            id="product-processing-lock-test",
            week_folder="week",
            customer="example.com",
            topic_index=1,
            topic="Example",
            status="title_selected",
            task_dir="D:/article/product-processing-lock-test",
            selected_title="Example",
            created_at="2026-07-10T00:00:00",
            updated_at="2026-07-10T00:00:00",
        )

        with app_module.product_processing(task.id):
            with self.assertRaisesRegex(Exception, "processing is running"):
                app_module.require_action(task, "update_products")

    def test_public_config_exposes_tavily_readiness_but_never_the_key(self) -> None:
        marker = "unit-test-tavily-secret"
        with patch.dict("os.environ", {"TAVILY_API_KEY": marker}, clear=False):
            response = TestClient(app_module.app).get("/api/config")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["integrations"]["tavily_ready"])
        self.assertNotIn(marker, response.text)

    def test_auto_products_uses_tavily_discovery_then_official_asset_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = replace(
                load_config(),
                output_root=root,
                data_file=root / "tasks.json",
                knowledge_base=root / "knowledge",
                topic_library=root / "topics",
            )
            task = TaskRecord(
                id="product-assets-test",
                week_folder=cfg.current_week_folder,
                customer="example.com",
                topic_index=1,
                topic="PET bottle mould",
                status="title_selected",
                task_dir=str(root / "task"),
                selected_title="PET Bottle Mould Guide",
                created_at="2026-07-10T00:00:00",
                updated_at="2026-07-10T00:00:00",
            )
            TaskStore(cfg).save([task])
            discovered = [
                Product(name="Candidate", url="https://example.com/products/a/")
            ]
            enriched = [
                Product(
                    product_id="a",
                    name="Official Product A",
                    url="https://example.com/products/a/",
                    canonical_url="https://example.com/products/a/",
                    image_path=str(root / "task" / "images" / "Official Product A.jpg"),
                    detail_page_verified=True,
                    asset_count=5,
                    selected_asset_id="A03",
                    asset_status="selected",
                )
            ]
            fake_tavily = object()

            with (
                patch.object(app_module, "config", return_value=cfg),
                patch.object(app_module, "TavilyClient", return_value=fake_tavily),
                patch.object(
                    app_module,
                    "recommend_products",
                    return_value=discovered,
                ) as discover,
                patch.object(
                    app_module,
                    "enrich_product_assets",
                    return_value=enriched,
                ) as enrich,
            ):
                response = TestClient(app_module.app).post(
                    "/api/tasks/product-assets-test/products/auto?limit=3"
                )

            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["products"][0]["selected_asset_id"], "A03")
            self.assertIs(discover.call_args.kwargs["tavily_client"], fake_tavily)
            self.assertFalse(discover.call_args.kwargs["download_images"])
            self.assertEqual(enrich.call_args.args[2], discovered)

    def test_empty_auto_discovery_keeps_existing_products_and_outline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = replace(
                load_config(),
                output_root=root,
                data_file=root / "tasks.json",
                knowledge_base=root / "knowledge",
                topic_library=root / "topics",
            )
            existing = Product(name="Existing", url="https://example.com/products/existing/")
            task = TaskRecord(
                id="empty-auto-products-test",
                week_folder=cfg.current_week_folder,
                customer="example.com",
                topic_index=1,
                topic="Example",
                status=STATUS_OUTLINE_READY,
                task_dir=str(root / "task"),
                selected_title="Example",
                outline="## Existing Outline\n\n## FAQ",
                products=[existing],
                created_at="2026-07-10T00:00:00",
                updated_at="2026-07-10T00:00:00",
            )
            TaskStore(cfg).save([task])

            with (
                patch.object(app_module, "config", return_value=cfg),
                patch.object(app_module, "recommend_products", return_value=[]),
            ):
                response = TestClient(app_module.app).post(
                    "/api/tasks/empty-auto-products-test/products/auto"
                )

            self.assertEqual(response.status_code, 422, response.text)
            reloaded = TaskStore(cfg).get(task.id)
            self.assertEqual(reloaded.products[0].name, "Existing")
            self.assertEqual(reloaded.outline, task.outline)
            self.assertEqual(reloaded.status, STATUS_OUTLINE_READY)

    def test_product_asset_refresh_requires_a_saved_official_url(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = replace(
                load_config(),
                output_root=root,
                data_file=root / "tasks.json",
                knowledge_base=root / "knowledge",
                topic_library=root / "topics",
            )
            task = TaskRecord(
                id="product-assets-empty-test",
                week_folder=cfg.current_week_folder,
                customer="example.com",
                topic_index=1,
                topic="Example",
                status="title_selected",
                task_dir=str(root / "task"),
                selected_title="Example",
                products=[Product(name="No URL")],
                created_at="2026-07-10T00:00:00",
                updated_at="2026-07-10T00:00:00",
            )
            TaskStore(cfg).save([task])

            with patch.object(app_module, "config", return_value=cfg):
                response = TestClient(app_module.app).post(
                    "/api/tasks/product-assets-empty-test/products/assets"
                )

            self.assertEqual(response.status_code, 422, response.text)
            self.assertIn("official product URL", response.json()["detail"])

    def test_editing_product_url_clears_hidden_old_asset_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = replace(
                load_config(),
                output_root=root,
                data_file=root / "tasks.json",
                knowledge_base=root / "knowledge",
                topic_library=root / "topics",
            )
            task = TaskRecord(
                id="product-url-change-test",
                week_folder=cfg.current_week_folder,
                customer="example.com",
                topic_index=1,
                topic="Example",
                status="title_selected",
                task_dir=str(root / "task"),
                selected_title="Example",
                products=[
                    Product(
                        product_id="old-product",
                        name="Old Product",
                        url="https://example.com/products/old/",
                        canonical_url="https://example.com/products/old/",
                        image_path=str(root / "old.jpg"),
                        reference_summary="Old official facts",
                        asset_manifest_path=str(root / "old-manifest.json"),
                        asset_count=4,
                        selected_asset_id="A02",
                        detail_page_verified=True,
                        asset_status="selected",
                    )
                ],
                created_at="2026-07-10T00:00:00",
                updated_at="2026-07-10T00:00:00",
            )
            TaskStore(cfg).save([task])
            edited = task.products[0].model_copy(
                update={"url": "https://example.com/products/new/"}
            )

            with patch.object(app_module, "config", return_value=cfg):
                response = TestClient(app_module.app).put(
                    "/api/tasks/product-url-change-test/products",
                    json={"products": [edited.model_dump(mode="json")]},
                )

            self.assertEqual(response.status_code, 200, response.text)
            product = response.json()["products"][0]
            self.assertEqual(product["url"], "https://example.com/products/new/")
            self.assertEqual(product["canonical_url"], "")
            self.assertEqual(product["image_path"], "")
            self.assertEqual(product["asset_count"], 0)
            self.assertFalse(product["detail_page_verified"])

    def test_product_asset_refresh_preserves_manual_rows_without_urls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = replace(
                load_config(),
                output_root=root,
                data_file=root / "tasks.json",
                knowledge_base=root / "knowledge",
                topic_library=root / "topics",
            )
            official = Product(name="Official", url="https://example.com/products/a/")
            manual = Product(name="Manual note only", description="Keep this row")
            task = TaskRecord(
                id="product-refresh-merge-test",
                week_folder=cfg.current_week_folder,
                customer="example.com",
                topic_index=1,
                topic="Example",
                status="title_selected",
                task_dir=str(root / "task"),
                selected_title="Example",
                products=[official, manual],
                created_at="2026-07-10T00:00:00",
                updated_at="2026-07-10T00:00:00",
            )
            TaskStore(cfg).save([task])
            enriched = official.model_copy(
                update={
                    "product_id": "a",
                    "detail_page_verified": True,
                    "asset_status": "selected",
                }
            )

            with (
                patch.object(app_module, "config", return_value=cfg),
                patch.object(
                    app_module,
                    "enrich_product_assets",
                    return_value=[enriched],
                ),
            ):
                response = TestClient(app_module.app).post(
                    "/api/tasks/product-refresh-merge-test/products/assets"
                )

            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(len(response.json()["products"]), 2)
            self.assertEqual(response.json()["products"][1]["name"], "Manual note only")

    def test_generation_above_former_limit_is_saved_without_compression(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = replace(
                load_config(),
                output_root=root,
                data_file=root / "tasks.json",
                knowledge_base=root / "knowledge",
                topic_library=root / "topics",
            )
            task = TaskRecord(
                id="no-maximum-test",
                week_folder=cfg.current_week_folder,
                customer="example.com",
                topic_index=1,
                topic="Example Buyer Guide",
                status=STATUS_OUTLINE_CONFIRMED,
                task_dir=str(root / "task"),
                selected_title="Example Buyer Guide",
                outline="## Detailed Buyer Considerations",
                workflow_error={
                    "code": "compression_failed",
                    "message": "Legacy maximum-word error",
                    "stage": "article",
                    "blocking": True,
                },
                created_at="2026-07-10T00:00:00",
                updated_at="2026-07-10T00:00:00",
            )
            TaskStore(cfg).save([task])

            with (
                patch.object(app_module, "config", return_value=cfg),
                patch.object(app_module, "generate_raw_article", return_value=LONG_ARTICLE),
            ):
                client = TestClient(app_module.app)
                response = client.post(
                    "/api/tasks/no-maximum-test/article",
                    json={"word_count": 1200},
                )

            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["status"], "draft_ready")
            self.assertGreater(response.json()["initial_article_word_count"], 1600)
            self.assertFalse(response.json()["compression"]["required"])
            self.assertIsNone(response.json()["workflow_error"])

    def test_open_folder_uses_the_selected_tasks_validated_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = replace(
                load_config(),
                output_root=root,
                data_file=root / "tasks.json",
                knowledge_base=root / "knowledge",
                topic_library=root / "topics",
            )
            task_dir = root / "customer" / "topic_001"
            task_dir.mkdir(parents=True)
            task = TaskRecord(
                id="open-folder-test",
                week_folder=cfg.current_week_folder,
                customer="example.com",
                topic_index=1,
                topic="Example Buyer Guide",
                task_dir=str(task_dir),
                created_at="2026-07-10T00:00:00",
                updated_at="2026-07-10T00:00:00",
            )
            TaskStore(cfg).save([task])

            with (
                patch.object(app_module, "config", return_value=cfg),
                patch.object(
                    app_module,
                    "open_task_directory",
                    return_value=task_dir,
                ) as open_directory,
            ):
                client = TestClient(app_module.app)
                response = client.post("/api/tasks/open-folder-test/open-folder")

            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["data"]["path"], str(task_dir))
            open_directory.assert_called_once()
            self.assertEqual(open_directory.call_args.args[1].id, task.id)

    def test_external_humanized_article_can_skip_initial_ai_check(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = replace(
                load_config(),
                output_root=root,
                data_file=root / "tasks.json",
                knowledge_base=root / "knowledge",
                topic_library=root / "topics",
            )
            task = TaskRecord(
                id="external-humanized-test",
                week_folder=cfg.current_week_folder,
                customer="example.com",
                topic_index=1,
                topic="Example Buyer Guide",
                status="draft_ready",
                task_dir=str(root / "task"),
                selected_title="Example Buyer Guide",
                initial_article=ARTICLE,
                initial_article_word_count=55,
                article=ARTICLE,
                created_at="2026-07-10T00:00:00",
                updated_at="2026-07-10T00:00:00",
            )
            TaskStore(cfg).save([task])

            with patch.object(app_module, "config", return_value=cfg):
                client = TestClient(app_module.app)
                response = client.put(
                    "/api/tasks/external-humanized-test/humanized-article",
                    json={"article": EXTERNAL_HUMANIZED_ARTICLE},
                )

            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["status"], "humanized_ready")
            self.assertFalse(response.json()["initial_ai_check"]["confirmed"])
            self.assertEqual(
                response.json()["humanized_article"], EXTERNAL_HUMANIZED_ARTICLE.strip()
            )
            self.assertIn("confirm_final_ai_check", response.json()["allowed_actions"])
            self.assertTrue(
                (root / "task" / "04_humanized_candidate.md").is_file()
            )

    def test_pasted_first_version_can_replace_a_failed_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = replace(
                load_config(),
                output_root=root,
                data_file=root / "tasks.json",
                knowledge_base=root / "knowledge",
                topic_library=root / "topics",
            )
            task = TaskRecord(
                id="manual-first-version-test",
                week_folder=cfg.current_week_folder,
                customer="example.com",
                topic_index=1,
                topic="Example Buyer Guide",
                status=STATUS_OUTLINE_READY,
                task_dir=str(root / "task"),
                selected_title="Example Buyer Guide",
                outline="## What Should Buyers Check First?\n\n## FAQ",
                workflow_error={
                    "code": "compression_failed",
                    "message": "Generated article is still over the word limit.",
                    "stage": "article",
                    "blocking": True,
                    "recoverable": True,
                },
                created_at="2026-07-10T00:00:00",
                updated_at="2026-07-10T00:00:00",
            )
            TaskStore(cfg).save([task])

            with patch.object(app_module, "config", return_value=cfg):
                client = TestClient(app_module.app)
                response = client.put(
                    "/api/tasks/manual-first-version-test/article",
                    json={"article": ARTICLE},
                )

            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["status"], "draft_ready")
            self.assertEqual(response.json()["initial_article"], ARTICLE)
            self.assertIsNone(response.json()["workflow_error"])
            self.assertIn("confirm_initial_ai_check", response.json()["allowed_actions"])

    def test_full_manual_review_image_and_export_chain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = replace(
                load_config(),
                output_root=root,
                data_file=root / "tasks.json",
                knowledge_base=root / "knowledge",
                topic_library=root / "topics",
            )
            task_dir = root / "task"
            task = TaskRecord(
                id="workflow-api-test",
                week_folder=cfg.current_week_folder,
                customer="example.com",
                topic_index=1,
                topic="Example Buyer Guide",
                status=STATUS_OUTLINE_READY,
                task_dir=str(task_dir),
                selected_title="Example Buyer Guide",
                outline="## What Should Buyers Check First?\n\n## FAQ",
                created_at="2026-07-10T00:00:00",
                updated_at="2026-07-10T00:00:00",
            )
            TaskStore(cfg).save([task])

            hero = root / "hero.png"
            Image.new("RGB", (320, 180), (32, 96, 160)).save(hero)
            screenshot = BytesIO()
            Image.new("RGB", (640, 360), (245, 245, 245)).save(
                screenshot, format="PNG"
            )
            screenshot_bytes = screenshot.getvalue()

            with (
                patch.object(app_module, "config", return_value=cfg),
                patch.object(app_module, "generate_raw_article", return_value=ARTICLE),
                patch.object(app_module, "humanize_article", return_value=ARTICLE),
                patch.object(
                    app_module,
                    "generate_tdk_metadata",
                    return_value=TdkMetadata(
                        title="Example Buyer Guide",
                        description="Need clearer sourcing decisions? Compare specifications, quality, and inquiry details before choosing a supplier.",
                        keywords=[
                            "B2B buyer guide",
                            "supplier selection",
                            "product specifications",
                            "quality requirements",
                            "sourcing checklist",
                            "purchase inquiry",
                        ],
                        description_character_count=113,
                    ),
                ),
            ):
                client = TestClient(app_module.app)

                response = client.put(
                    "/api/tasks/workflow-api-test/outline",
                    json={"outline": task.outline},
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["status"], "outline_confirmed")
                self.assertIn("generate_article", response.json()["allowed_actions"])

                response = client.post(
                    "/api/tasks/workflow-api-test/article",
                    json={"word_count": 1200},
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["status"], "draft_ready")
                self.assertEqual(response.json()["initial_article"], ARTICLE)
                self.assertIn("confirm_initial_ai_check", response.json()["allowed_actions"])

                response = client.post(
                    "/api/tasks/workflow-api-test/checks/initial-ai/screenshot",
                    files={"file": ("initial.png", screenshot_bytes, "image/png")},
                )
                self.assertEqual(response.status_code, 200, response.text)
                initial_screenshot = Path(
                    response.json()["initial_ai_check"]["screenshot_path"]
                )
                self.assertTrue(initial_screenshot.is_file())

                response = client.put(
                    "/api/tasks/workflow-api-test/checks/initial-ai",
                    json={"score": 48.2, "report": "Manual ZeroGPT baseline"},
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["status"], "initial_ai_checked")
                self.assertEqual(
                    response.json()["initial_ai_check"]["screenshot_path"],
                    str(initial_screenshot),
                )

                response = client.post("/api/tasks/workflow-api-test/humanize")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["status"], "humanized_ready")

                response = client.put(
                    "/api/tasks/workflow-api-test/checks/final-ai",
                    json={"score": 16.4, "report": "Manual ZeroGPT recheck"},
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["status"], "final_ai_checked")

                response = client.post("/api/tasks/workflow-api-test/humanize")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["status"], "humanized_ready")
                self.assertFalse(response.json()["final_ai_check"]["confirmed"])

                response = client.post(
                    "/api/tasks/workflow-api-test/checks/final-ai/screenshot",
                    files={"file": ("final.png", screenshot_bytes, "image/png")},
                )
                self.assertEqual(response.status_code, 200, response.text)
                final_screenshot = Path(
                    response.json()["final_ai_check"]["screenshot_path"]
                )
                self.assertTrue(final_screenshot.is_file())

                response = client.put(
                    "/api/tasks/workflow-api-test/checks/final-ai",
                    json={"score": 14.1, "report": "Manual recheck after a second pass"},
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["status"], "final_ai_checked")

                response = client.post("/api/tasks/workflow-api-test/restore-links")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["status"], "links_verified")
                self.assertTrue(response.json()["link_validation"]["passed"])

                response = client.put(
                    "/api/tasks/workflow-api-test/images",
                    json={"hero_image": str(hero)},
                )
                self.assertEqual(response.status_code, 200, response.text)

                response = client.post("/api/tasks/workflow-api-test/prepare-images")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["status"], "images_ready")
                self.assertEqual(response.json()["images"][0]["marker"], "img.Example Buyer Guide.webp")
                prepared_image_name = response.json()["images"][0]["filename"]

                response = client.post("/api/tasks/workflow-api-test/export-docx")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["status"], "docx_exported")
                self.assertTrue(Path(response.json()["docx_path"]).is_file())

                response = client.post("/api/tasks/workflow-api-test/generate-tdk")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["status"], "docx_exported")
                self.assertEqual(response.json()["tdk"]["title"], "Example Buyer Guide")
                self.assertLessEqual(
                    response.json()["tdk"]["description_character_count"], 150
                )
                self.assertEqual(len(response.json()["tdk"]["keywords"]), 6)
                tdk_path = Path(response.json()["tdk_path"])
                self.assertEqual(tdk_path.name, "D.docx")
                self.assertTrue(tdk_path.is_file())
                self.assertEqual(
                    [paragraph.text for paragraph in Document(tdk_path).paragraphs],
                    [
                        "T: Example Buyer Guide",
                        "D: Need clearer sourcing decisions? Compare specifications, quality, and inquiry details before choosing a supplier.",
                        "K: B2B buyer guide, supplier selection, product specifications, quality requirements, sourcing checklist, purchase inquiry",
                    ],
                )

                response = client.post(
                    "/api/tasks/workflow-api-test/package-delivery"
                )
                self.assertEqual(response.status_code, 200, response.text)
                delivery = Path(response.json()["delivery_package_path"])
                self.assertEqual(delivery.name, "example.com")
                self.assertTrue((delivery / "Example Buyer Guide.docx").is_file())
                self.assertTrue((delivery / "D.docx").is_file())
                self.assertTrue((delivery / prepared_image_name).is_file())
                self.assertFalse((delivery / "images").exists())
                self.assertFalse((delivery / "initial-ai-rate.png").exists())
                self.assertTrue((delivery / "final-ai-rate.png").is_file())
                self.assertFalse((delivery / "AI rate screenshots").exists())

                self.assertIn(
                    "update_humanized_article",
                    response.json()["allowed_actions"],
                )
                response = client.put(
                    "/api/tasks/workflow-api-test/humanized-article",
                    json={"article": EXTERNAL_HUMANIZED_ARTICLE},
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["status"], "humanized_ready")
                self.assertFalse(response.json()["final_ai_check"]["confirmed"])
                self.assertEqual(response.json()["linked_article"], "")
                self.assertEqual(response.json()["images"], [])
                self.assertEqual(response.json()["docx_path"], "")
                self.assertEqual(response.json()["tdk_path"], "")
                self.assertEqual(response.json()["delivery_package_path"], "")


if __name__ == "__main__":
    unittest.main()
