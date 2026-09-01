from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
import sys

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from config import initialize_environment, load_config  # noqa: E402


BASE_CONFIG = """
paths:
  topic_library: workspace/topic-library
  knowledge_base: workspace/knowledge
  output_root: workspace/projects

article:
  language: English
  title_candidates: 10
  default_word_count: 1200
  ai_pass_threshold: 30

prompts:
  humanize: prompts/humanize.txt

docx_format:
  font: Times New Roman
  styles:
    title_1:
      size_pt: 22
    title_2:
      size_pt: 18
    title_3:
      size_pt: 13.5
    body:
      size_pt: 12

llm:
  provider: responses
  base_url: https://api.openai.com/v1
  model: gpt-5.6-sol
  reasoning_effort: medium
  available_models:
    - gpt-5.6-sol
  available_reasoning_efforts:
    - low
    - medium

features:
  knowledge_agent_enabled: false
  workflow_assistant_enabled: false
  workflow_assistant_attachments_enabled: false
  workflow_assistant_project_changes_enabled: false
  workflow_assistant_gap_fill_enabled: false
"""


class EnvironmentLoadingTests(unittest.TestCase):
    def test_root_env_wins_over_legacy_backend_env_and_process_wins_over_both(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "backend").mkdir()
            (root / ".env").write_text(
                "SHARED=root\nROOT_ONLY=root\nPROCESS_ONLY=root\n",
                encoding="utf-8",
            )
            (root / "backend" / ".env").write_text(
                "SHARED=legacy\nROOT_ONLY=legacy\nLEGACY_ONLY=legacy\n",
                encoding="utf-8",
            )
            environment = {
                "SHARED": "process",
                "PROCESS_ONLY": "process",
            }

            effective_root = initialize_environment(environment, app_root=root)

            self.assertEqual(effective_root, root.resolve())
            self.assertEqual(environment["SHARED"], "process")
            self.assertEqual(environment["ROOT_ONLY"], "root")
            self.assertEqual(environment["PROCESS_ONLY"], "process")
            self.assertEqual(environment["LEGACY_ONLY"], "legacy")

    def test_explicit_env_file_is_the_only_file_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "backend").mkdir()
            (root / ".env").write_text("ROOT_ONLY=root\n", encoding="utf-8")
            (root / "backend" / ".env").write_text(
                "LEGACY_ONLY=legacy\n",
                encoding="utf-8",
            )
            selected = root / "selected.env"
            selected.write_text(
                "SELECTED_ONLY=selected\nSHARED=selected\n",
                encoding="utf-8",
            )
            environment = {
                "ARTICLE_AGENT_ENV_FILE": "selected.env",
                "SHARED": "process",
            }

            initialize_environment(environment, app_root=root)

            self.assertEqual(environment["SELECTED_ONLY"], "selected")
            self.assertEqual(environment["SHARED"], "process")
            self.assertNotIn("ROOT_ONLY", environment)
            self.assertNotIn("LEGACY_ONLY", environment)

    def test_explicit_missing_env_file_fails_fast(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(
                FileNotFoundError,
                "ARTICLE_AGENT_ENV_FILE does not exist",
            ):
                initialize_environment(
                    {"ARTICLE_AGENT_ENV_FILE": "missing.env"},
                    app_root=Path(temporary),
                )


class YamlOverlayTests(unittest.TestCase):
    def test_project_job_concurrency_comes_from_yaml_and_environment_override(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "config.yaml").write_text(
                BASE_CONFIG
                + "\nserver_jobs:\n  project_concurrency: 5\n",
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {
                    "ARTICLE_AGENT_ROOT": str(root),
                    "ARTICLE_AGENT_CONFIG": "config.yaml",
                },
                clear=True,
            ):
                config = load_config()
                self.assertEqual(config.workflow_assistant_max_concurrency, 5)
                self.assertEqual(config.project_job_concurrency, 5)

            with patch.dict(
                os.environ,
                {
                    "ARTICLE_AGENT_ROOT": str(root),
                    "ARTICLE_AGENT_CONFIG": "config.yaml",
                    "ARTICLE_AGENT_PROJECT_JOB_CONCURRENCY": "7",
                },
                clear=True,
            ):
                self.assertEqual(load_config().project_job_concurrency, 7)

    def test_overlay_deep_merges_the_shared_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "config.yaml").write_text(BASE_CONFIG, encoding="utf-8")
            (root / "config.overlay.yaml").write_text(
                """
extends: config.yaml

paths:
  knowledge_base: docker/knowledge

llm:
  reasoning_effort: high

features:
  workflow_assistant_enabled: true
""",
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {
                    "ARTICLE_AGENT_ROOT": str(root),
                    "ARTICLE_AGENT_CONFIG": "config.overlay.yaml",
                    "KNOWLEDGE_AGENT_ENABLED": "false",
                },
                clear=True,
            ):
                config = load_config()

            self.assertEqual(config.topic_library, root / "workspace/topic-library")
            self.assertEqual(config.knowledge_base, root / "docker/knowledge")
            self.assertEqual(config.llm_model, "gpt-5.6-sol")
            self.assertEqual(config.llm_reasoning_effort, "high")
            self.assertTrue(config.workflow_assistant_enabled)


if __name__ == "__main__":
    unittest.main()
