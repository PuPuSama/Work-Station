from __future__ import annotations

import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import app as app_module  # noqa: E402
from config import load_config  # noqa: E402
from models import (  # noqa: E402
    Product,
    STATUS_INITIAL_AI_CHECKED,
    STATUS_NEW,
    STATUS_OUTLINE_READY,
    STATUS_TITLE_SELECTED,
    TaskRecord,
)
from services.job_queue import JobConflict, JobQueue  # noqa: E402
from storage import TaskStore, now_iso  # noqa: E402


def make_task(cfg, task_id: str, status: str) -> TaskRecord:
    return TaskRecord(
        id=task_id,
        week_folder=cfg.current_week_folder,
        customer="example.com",
        topic_index=1 if task_id == "valid" else 2,
        topic=f"Topic {task_id}",
        status=status,
        task_dir=str(cfg.output_root / "example.com" / task_id),
        title_candidates=["Selected title"],
        selected_title="Selected title" if status == STATUS_TITLE_SELECTED else "",
        outline_custom_prompt="Use a buyer checklist.",
        use_outline_custom_prompt=True,
        created_at=now_iso(),
        updated_at=now_iso(),
    )


class BatchApiTests(unittest.TestCase):
    def test_extended_single_task_operations_dispatch_through_queue_worker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = replace(
                load_config(),
                output_root=root / "output",
                data_file=root / "tasks.json",
            )
            task = make_task(cfg, "valid", STATUS_TITLE_SELECTED)
            TaskStore(cfg).save([task])
            handlers = {
                "humanize": "perform_humanize",
                "restore_links": "perform_restore_links",
                "prepare_images": "perform_prepare_images",
                "export_docx": "perform_export_docx",
                "generate_tdk": "perform_generate_tdk",
                "package_delivery": "perform_package_delivery",
            }
            with patch.object(app_module, "config", return_value=cfg):
                for operation, handler_name in handlers.items():
                    with self.subTest(operation=operation):
                        result_task = task.model_copy(update={"revision": 7})
                        job = {
                            "task_id": task.id,
                            "source_revision": 0,
                            "operation": operation,
                            "request": {"revision": 0},
                        }
                        with (
                            patch.object(app_module, "batch_preflight_issue", return_value=""),
                            patch.object(app_module, handler_name, return_value=result_task) as handler,
                        ):
                            result = app_module._execute_batch_job(job, lambda: False)
                        self.assertEqual(result, 7)
                        handler.assert_called_once()

    def test_humanize_can_run_as_a_persistent_background_job(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = replace(
                load_config(),
                output_root=root / "output",
                data_file=root / "data" / "tasks.json",
                knowledge_base=root / "knowledge",
                topic_library=root / "topics",
            )
            task = make_task(cfg, "valid", STATUS_INITIAL_AI_CHECKED)
            task.selected_title = "Selected title"
            task.initial_article = "First article version"
            task.article = task.initial_article
            TaskStore(cfg).save([task])
            with (
                patch.object(app_module, "config", return_value=cfg),
                patch.object(app_module, "humanize_article", return_value="Humanized article"),
                TestClient(app_module.app) as client,
            ):
                response = client.post(
                    "/api/batches",
                    json={"operation": "humanize", "task_ids": ["valid"]},
                )
                self.assertEqual(response.status_code, 200, response.text)
                payload = response.json()
                self.assertEqual(payload["batch"]["operation"], "humanize")
                batch_id = payload["batch"]["id"]
                deadline = time.time() + 5
                while time.time() < deadline:
                    current = client.get(f"/api/batches/{batch_id}").json()
                    if current["status"] == "succeeded":
                        break
                    time.sleep(0.03)
                else:
                    self.fail("Background humanize job did not complete.")

            saved = TaskStore(cfg).get("valid")
            self.assertEqual(saved.humanized_article, "Humanized article")
            self.assertEqual(saved.status, "humanized_ready")

    def test_verified_product_discovery_can_run_in_a_product_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = replace(
                load_config(),
                output_root=root / "output",
                data_file=root / "data" / "tasks.json",
                knowledge_base=root / "knowledge",
                topic_library=root / "topics",
            )
            TaskStore(cfg).save([make_task(cfg, "valid", STATUS_TITLE_SELECTED)])
            discovered = Product(
                name="Verified product",
                url="https://example.com/verified-product/",
                image_path="",
                description="Official product detail.",
            )
            enriched = discovered.model_copy(
                update={
                    "asset_status": "selected",
                    "asset_count": 2,
                    "selected_asset_id": "A01",
                    "detail_page_verified": True,
                    "image_path": str(root / "output" / "example.com" / "valid" / "product.webp"),
                }
            )
            with (
                patch.object(app_module, "config", return_value=cfg),
                patch.object(app_module, "recommend_products", return_value=[discovered]),
                patch.object(app_module, "enrich_product_assets", return_value=[enriched]),
                TestClient(app_module.app) as client,
            ):
                response = client.post(
                    "/api/batches",
                    json={"operation": "products", "task_ids": ["valid"]},
                )
                self.assertEqual(response.status_code, 200, response.text)
                payload = response.json()
                self.assertEqual(payload["batch"]["operation"], "products")
                batch_id = payload["batch"]["id"]
                deadline = time.time() + 5
                while time.time() < deadline:
                    current = client.get(f"/api/batches/{batch_id}").json()
                    if current["status"] == "succeeded":
                        break
                    time.sleep(0.03)
                else:
                    self.fail("Background product batch did not complete.")

            saved = TaskStore(cfg).get("valid")
            self.assertEqual(saved.products[0].name, "Verified product")
            self.assertEqual(saved.products[0].asset_status, "selected")

    def test_title_candidates_can_be_generated_in_a_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = replace(
                load_config(),
                output_root=root / "output",
                data_file=root / "data" / "tasks.json",
                knowledge_base=root / "knowledge",
                topic_library=root / "topics",
            )
            TaskStore(cfg).save([make_task(cfg, "valid", STATUS_NEW)])
            candidates = [f"Candidate title {index}" for index in range(1, 11)]
            with (
                patch.object(app_module, "config", return_value=cfg),
                patch.object(app_module, "generate_titles", return_value=candidates),
                TestClient(app_module.app) as client,
            ):
                response = client.post(
                    "/api/batches",
                    json={"operation": "titles", "task_ids": ["valid"]},
                )
                self.assertEqual(response.status_code, 200, response.text)
                payload = response.json()
                self.assertEqual(payload["batch"]["operation"], "titles")
                batch_id = payload["batch"]["id"]
                deadline = time.time() + 5
                while time.time() < deadline:
                    current = client.get(f"/api/batches/{batch_id}").json()
                    if current["status"] == "succeeded":
                        break
                    time.sleep(0.03)
                else:
                    self.fail("Background title batch did not complete.")

            saved = TaskStore(cfg).get("valid")
            self.assertEqual(saved.title_candidates, candidates)
            self.assertEqual(saved.status, "titles_ready")

    def test_batch_preflight_enqueues_only_eligible_tasks_and_runs_in_background(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = replace(
                load_config(),
                output_root=root / "output",
                data_file=root / "data" / "tasks.json",
                knowledge_base=root / "knowledge",
                topic_library=root / "topics",
            )
            TaskStore(cfg).save(
                [
                    make_task(cfg, "valid", STATUS_TITLE_SELECTED),
                    make_task(cfg, "invalid", STATUS_NEW),
                ]
            )
            generated_outline = "# Selected title\n\n## Buyer checklist\n\n- Check specifications."
            with (
                patch.object(app_module, "config", return_value=cfg),
                patch.object(app_module, "generate_outline", return_value=generated_outline),
                TestClient(app_module.app) as client,
            ):
                response = client.post(
                    "/api/batches",
                    json={
                        "operation": "outline",
                        "task_ids": ["valid", "invalid"],
                    },
                )
                self.assertEqual(response.status_code, 200, response.text)
                payload = response.json()
                self.assertEqual(payload["batch"]["total"], 1)
                self.assertEqual(payload["rejected"][0]["task_id"], "invalid")
                self.assertTrue(payload["batch"]["jobs"][0]["request"]["use_custom_prompt"])

                batch_id = payload["batch"]["id"]
                deadline = time.time() + 5
                while time.time() < deadline:
                    current = client.get(f"/api/batches/{batch_id}").json()
                    if current["status"] == "succeeded":
                        break
                    time.sleep(0.03)
                else:
                    self.fail("Background outline batch did not complete.")

            tasks = {task.id: task for task in TaskStore(cfg).load()}
            self.assertEqual(tasks["valid"].status, STATUS_OUTLINE_READY)
            self.assertEqual(tasks["valid"].outline, generated_outline)
            self.assertEqual(tasks["invalid"].status, STATUS_NEW)

    def test_worker_rejects_stale_revision_before_calling_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = replace(
                load_config(),
                output_root=root / "output",
                data_file=root / "tasks.json",
            )
            task = make_task(cfg, "valid", STATUS_TITLE_SELECTED)
            task_store = TaskStore(cfg)
            task_store.save([task])
            queue = JobQueue(root / "jobs.sqlite3")
            with patch.object(app_module, "config", return_value=cfg):
                batch = queue.create_batch(
                    "outline",
                    [
                        {
                            "task_id": task.id,
                            "customer": task.customer,
                            "topic_index": task.topic_index,
                            "topic": task.topic,
                            "source_revision": task.revision,
                            "request": app_module.batch_request_snapshot(task, "outline"),
                        }
                    ],
                )
                changed = task_store.get(task.id)
                changed.topic_notes = "Operator edited this after queueing."
                task_store.put(changed, expected_revision=0)
                claimed = queue.claim_jobs(1)[0]
                with (
                    patch.object(app_module, "generate_outline") as generate,
                    self.assertRaises(JobConflict),
                ):
                    app_module._execute_batch_job(claimed, lambda: False)
                generate.assert_not_called()
                self.assertEqual(batch["jobs"][0]["source_revision"], 0)


if __name__ == "__main__":
    unittest.main()
