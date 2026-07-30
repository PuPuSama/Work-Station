from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import app as app_module  # noqa: E402
from config import load_config  # noqa: E402
from models import STATUS_TITLE_SELECTED, TaskRecord  # noqa: E402
from storage import TaskStore  # noqa: E402


class ImagePreviewTests(unittest.TestCase):
    def _task(self, root: Path) -> tuple[object, TaskRecord]:
        cfg = replace(
            load_config(),
            output_root=root,
            data_file=root / "tasks.json",
        )
        task_dir = root / "example.com" / "topic_001"
        task_dir.mkdir(parents=True)
        task = TaskRecord(
            id="preview-test",
            week_folder=cfg.current_week_folder,
            customer="example.com",
            topic_index=1,
            topic="Preview image",
            selected_title="Preview image",
            status=STATUS_TITLE_SELECTED,
            task_dir=str(task_dir),
            created_at="2026-07-16T00:00:00",
            updated_at="2026-07-16T00:00:00",
        )
        TaskStore(cfg).save([task])
        return cfg, task

    def test_previews_a_valid_image_inside_the_task_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg, task = self._task(root)
            image_path = Path(task.task_dir) / "product_assets" / "hero.png"
            image_path.parent.mkdir()
            Image.new("RGB", (12, 8), "green").save(image_path)

            with patch.object(app_module, "config", return_value=cfg):
                response = TestClient(app_module.app).get(
                    "/api/tasks/preview-test/images/preview",
                    params={"path": str(image_path)},
                )

            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.headers["content-type"], "image/png")
            self.assertTrue(response.content)

    def test_rejects_an_image_outside_the_task_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg, _task = self._task(root)
            image_path = root / "other-task.png"
            Image.new("RGB", (12, 8), "blue").save(image_path)

            with patch.object(app_module, "config", return_value=cfg):
                response = TestClient(app_module.app).get(
                    "/api/tasks/preview-test/images/preview",
                    params={"path": str(image_path)},
                )

            self.assertEqual(response.status_code, 403, response.text)

    def test_uploads_a_product_image_into_the_task_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg, task = self._task(root)
            image_bytes = BytesIO()
            Image.new("RGB", (24, 16), "orange").save(image_bytes, format="PNG")

            with patch.object(app_module, "config", return_value=cfg):
                client = TestClient(app_module.app)
                response = client.post(
                    "/api/tasks/preview-test/products/image-upload",
                    files={"file": ("clipboard-image.png", image_bytes.getvalue(), "image/png")},
                )

                self.assertEqual(response.status_code, 200, response.text)
                image_path = response.json()["data"]["image_path"]
                self.assertEqual(Path(image_path).parts[:3], ("images", "uploads", "products"))
                self.assertTrue((Path(task.task_dir) / image_path).is_file())

                preview = client.get(
                    "/api/tasks/preview-test/images/preview",
                    params={"path": image_path},
                )

            self.assertEqual(preview.status_code, 200, preview.text)
            self.assertEqual(preview.headers["content-type"], "image/png")

    def test_rejects_a_non_image_product_upload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg, task = self._task(root)

            with patch.object(app_module, "config", return_value=cfg):
                response = TestClient(app_module.app).post(
                    "/api/tasks/preview-test/products/image-upload",
                    files={"file": ("not-image.png", b"not an image", "image/png")},
                )

            self.assertEqual(response.status_code, 422, response.text)
            upload_dir = Path(task.task_dir) / "images" / "uploads" / "products"
            self.assertFalse(upload_dir.exists())


if __name__ == "__main__":
    unittest.main()
