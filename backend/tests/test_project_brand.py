from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import app as app_module  # noqa: E402
from config import load_config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from models import TaskRecord  # noqa: E402
from storage import TaskStore  # noqa: E402


def task(
    *,
    task_id: str,
    customer: str,
    topic: str,
    brand_name: str = "",
) -> TaskRecord:
    return TaskRecord(
        id=task_id,
        week_folder="legacy-week",
        customer=customer,
        brand_name=brand_name,
        topic_index=1,
        topic=topic,
        task_dir=f"D:/article/{task_id}",
        created_at="2026-07-01T00:00:00",
        updated_at="2026-07-01T00:00:00",
    )


class ProjectBrandStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = TaskStore(
            SimpleNamespace(data_file=Path(self.temp_dir.name) / "tasks.json")
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_brand_update_is_normalized_and_applies_to_the_whole_project(self) -> None:
        self.store.save(
            [
                task(
                    task_id="one",
                    customer="www.Example.com",
                    topic="First topic",
                ),
                task(
                    task_id="two",
                    customer="https://example.com/",
                    topic="Second topic",
                ),
                task(
                    task_id="other",
                    customer="other.example",
                    topic="Other topic",
                ),
            ]
        )

        updated = self.store.update_customer_brand(
            "example.com",
            "  Example   Industrial  ",
        )

        self.assertEqual(updated, 2)
        current = {record.id: record for record in self.store.load()}
        self.assertEqual(current["one"].brand_name, "Example Industrial")
        self.assertEqual(current["two"].brand_name, "Example Industrial")
        self.assertEqual(current["other"].brand_name, "")
        self.assertEqual(current["one"].revision, 1)
        self.assertEqual(current["two"].revision, 1)
        self.assertEqual(current["other"].revision, 0)

    def test_new_tasks_inherit_the_saved_project_brand(self) -> None:
        self.store.save(
            [
                task(
                    task_id="existing",
                    customer="www.example.com",
                    topic="Existing topic",
                    brand_name="Example Industrial",
                )
            ]
        )

        records = self.store.upsert_many(
            [
                task(
                    task_id="incoming",
                    customer="example.com",
                    topic="New topic",
                )
            ]
        )

        incoming = next(record for record in records if record.id == "incoming")
        self.assertEqual(incoming.brand_name, "Example Industrial")

    def test_project_context_applies_to_all_tasks_and_new_tasks_inherit_it(self) -> None:
        self.store.save(
            [
                task(task_id="one", customer="www.example.com", topic="First topic"),
                task(task_id="two", customer="example.com", topic="Second topic"),
            ]
        )

        updated = self.store.update_customer_context(
            "example.com",
            "  Manufacturer of industrial components.  ",
            "  Avoid unsupported performance claims.  ",
        )
        current_before_sync = {record.id: record for record in self.store.load()}
        records = self.store.upsert_many(
            [task(task_id="three", customer="www.example.com", topic="Third topic")]
        )

        self.assertEqual(updated, 2)
        for task_id in ("one", "two"):
            self.assertEqual(
                current_before_sync[task_id].project_introduction,
                "Manufacturer of industrial components.",
            )
            self.assertEqual(
                current_before_sync[task_id].project_notes,
                "Avoid unsupported performance claims.",
            )
        incoming = next(record for record in records if record.id == "three")
        self.assertEqual(
            incoming.project_introduction,
            "Manufacturer of industrial components.",
        )
        self.assertEqual(
            incoming.project_notes,
            "Avoid unsupported performance claims.",
        )


class ProjectBrandApiTests(unittest.TestCase):
    def test_homepage_save_contract_persists_the_brand(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = replace(
                load_config(),
                data_file=root / "tasks.json",
                output_root=root,
                topic_library=root / "topics",
                knowledge_base=root / "knowledge",
            )
            TaskStore(cfg).save(
                [
                    task(
                        task_id="brand-api",
                        customer="www.example.com",
                        topic="Example topic",
                    )
                ]
            )

            with patch.object(app_module, "config", return_value=cfg):
                response = TestClient(app_module.app).put(
                    "/api/projects/www.example.com/brand",
                    json={"brand_name": " Example   Industrial "},
                )

            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["data"]["brand_name"], "Example Industrial")
            self.assertEqual(TaskStore(cfg).get("brand-api").brand_name, "Example Industrial")

    def test_project_context_and_topic_writing_settings_are_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = replace(
                load_config(),
                data_file=root / "tasks.json",
                output_root=root,
                topic_library=root / "topics",
                knowledge_base=root / "knowledge",
            )
            TaskStore(cfg).save(
                [task(task_id="context-api", customer="www.example.com", topic="Example")]
            )

            with patch.object(app_module, "config", return_value=cfg):
                client = TestClient(app_module.app)
                project_response = client.put(
                    "/api/projects/www.example.com/context",
                    json={
                        "project_introduction": "Industrial component manufacturer.",
                        "project_notes": "Use a practical buyer-focused tone.",
                    },
                )
                writing_response = client.put(
                    "/api/tasks/context-api/writing-settings",
                    json={
                        "revision": 1,
                        "topic_notes": "Compare maintenance requirements.",
                        "outline_custom_prompt": "Give extra weight to selection criteria.",
                        "article_custom_prompt": "Use shorter paragraphs.",
                        "use_outline_custom_prompt": True,
                        "use_article_custom_prompt": True,
                        "include_project_introduction": True,
                        "include_project_notes": False,
                        "include_topic_notes": True,
                    },
                )

            self.assertEqual(project_response.status_code, 200, project_response.text)
            self.assertEqual(writing_response.status_code, 200, writing_response.text)
            persisted = TaskStore(cfg).get("context-api")
            self.assertEqual(persisted.project_introduction, "Industrial component manufacturer.")
            self.assertEqual(persisted.project_notes, "Use a practical buyer-focused tone.")
            self.assertEqual(persisted.topic_notes, "Compare maintenance requirements.")
            self.assertTrue(persisted.use_outline_custom_prompt)
            self.assertTrue(persisted.use_article_custom_prompt)
            self.assertFalse(persisted.include_project_notes)


if __name__ == "__main__":
    unittest.main()
