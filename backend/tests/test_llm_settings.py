from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import app as app_module  # noqa: E402
from config import load_config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from services.llm_settings import LlmSettingsRepository  # noqa: E402


class LlmSettingsRepositoryTests(unittest.TestCase):
    def test_settings_are_persisted_in_the_task_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = LlmSettingsRepository(Path(directory) / "tasks.json")

            self.assertIsNone(repository.get())
            saved = repository.save(
                model="gpt-5.6-terra",
                reasoning_effort="medium",
            )

            loaded = repository.get()
            self.assertIsNotNone(loaded)
            self.assertEqual(saved.model, "gpt-5.6-terra")
            self.assertEqual(loaded.model, "gpt-5.6-terra")
            self.assertEqual(loaded.reasoning_effort, "medium")


class LlmSettingsApiTests(unittest.TestCase):
    def test_homepage_can_update_the_effective_model_and_reasoning_effort(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = replace(
                load_config(),
                data_file=Path(directory) / "tasks.json",
            )
            with patch.object(app_module, "load_config", return_value=base):
                response = TestClient(app_module.app).put(
                    "/api/settings/llm",
                    json={
                        "model": "gpt-5.6-terra",
                        "reasoning_effort": "medium",
                    },
                )
                effective = app_module.config()

            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["llm"]["model"], "gpt-5.6-terra")
            self.assertEqual(response.json()["llm"]["reasoning_effort"], "medium")
            self.assertEqual(effective.llm_model, "gpt-5.6-terra")
            self.assertEqual(effective.llm_reasoning_effort, "medium")

    def test_unknown_model_is_rejected_without_saving(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = replace(
                load_config(),
                data_file=Path(directory) / "tasks.json",
            )
            with patch.object(app_module, "load_config", return_value=base):
                response = TestClient(app_module.app).put(
                    "/api/settings/llm",
                    json={
                        "model": "unknown-model",
                        "reasoning_effort": "high",
                    },
                )

            self.assertEqual(response.status_code, 422, response.text)
            self.assertIsNone(LlmSettingsRepository(base.data_file).get())


if __name__ == "__main__":
    unittest.main()
