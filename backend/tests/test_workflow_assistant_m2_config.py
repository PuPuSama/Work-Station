from __future__ import annotations

import os
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from config import load_config, public_config  # noqa: E402


class WorkflowAssistantM2ConfigTests(unittest.TestCase):
    def test_subfeatures_default_to_disabled(self) -> None:
        names = {
            "WORKFLOW_ASSISTANT_ATTACHMENTS_ENABLED",
            "WORKFLOW_ASSISTANT_PROJECT_CHANGES_ENABLED",
            "WORKFLOW_ASSISTANT_GAP_FILL_ENABLED",
        }
        environment = {key: value for key, value in os.environ.items() if key not in names}
        with patch.dict(os.environ, environment, clear=True):
            config = load_config()

        self.assertFalse(config.workflow_assistant_attachments_enabled)
        self.assertFalse(config.workflow_assistant_project_changes_enabled)
        self.assertFalse(config.workflow_assistant_gap_fill_enabled)

    def test_environment_can_enable_each_subfeature(self) -> None:
        with patch.dict(
            os.environ,
            {
                "WORKFLOW_ASSISTANT_ATTACHMENTS_ENABLED": "true",
                "WORKFLOW_ASSISTANT_PROJECT_CHANGES_ENABLED": "true",
                "WORKFLOW_ASSISTANT_GAP_FILL_ENABLED": "true",
            },
        ):
            config = load_config()

        self.assertTrue(config.workflow_assistant_attachments_enabled)
        self.assertTrue(config.workflow_assistant_project_changes_enabled)
        self.assertTrue(config.workflow_assistant_gap_fill_enabled)

    def test_public_flags_are_only_exposed_under_the_master_switch(self) -> None:
        config = load_config()
        enabled = replace(
            config,
            workflow_assistant_enabled=True,
            workflow_assistant_attachments_enabled=True,
            workflow_assistant_project_changes_enabled=False,
            workflow_assistant_gap_fill_enabled=True,
        )
        features = public_config(enabled)["features"]
        self.assertEqual(features["workflow_assistant_enabled"], True)
        self.assertEqual(features["workflow_assistant_attachments_enabled"], True)
        self.assertEqual(features["workflow_assistant_project_changes_enabled"], False)
        self.assertEqual(features["workflow_assistant_gap_fill_enabled"], True)

        disabled = replace(enabled, workflow_assistant_enabled=False)
        disabled_features = public_config(disabled)["features"]
        self.assertNotIn("workflow_assistant_attachments_enabled", disabled_features)
        self.assertNotIn("workflow_assistant_project_changes_enabled", disabled_features)
        self.assertNotIn("workflow_assistant_gap_fill_enabled", disabled_features)


if __name__ == "__main__":
    unittest.main()
