from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from config import load_config  # noqa: E402
from models import TaskRecord  # noqa: E402
from services.task_files import (  # noqa: E402
    TaskDirectoryError,
    resolve_task_directory,
)


def task_for(path: Path) -> TaskRecord:
    return TaskRecord(
        id="folder-test",
        week_folder="week",
        customer="example.com",
        topic_index=1,
        topic="Topic",
        task_dir=str(path),
        created_at="2026-07-10T00:00:00",
        updated_at="2026-07-10T00:00:00",
    )


class TaskDirectoryTests(unittest.TestCase):
    def test_resolves_a_task_directory_inside_output_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task_dir = root / "customer" / "topic_001"
            task_dir.mkdir(parents=True)
            cfg = replace(load_config(), output_root=root)
            self.assertEqual(resolve_task_directory(cfg, task_for(task_dir)), task_dir.resolve())

    def test_rejects_a_directory_outside_output_root(self) -> None:
        with tempfile.TemporaryDirectory() as output, tempfile.TemporaryDirectory() as outside:
            cfg = replace(load_config(), output_root=Path(output))
            with self.assertRaises(TaskDirectoryError):
                resolve_task_directory(cfg, task_for(Path(outside)))

if __name__ == "__main__":
    unittest.main()
