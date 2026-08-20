from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app import auth_status  # noqa: E402


def request(
    *,
    master: bool,
    attachments: bool,
    project_changes: bool = False,
    gap_fill: bool = False,
):
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                article_agent_config=SimpleNamespace(
                    workflow_assistant_enabled=master,
                    workflow_assistant_attachments_enabled=attachments,
                    workflow_assistant_project_changes_enabled=project_changes,
                    workflow_assistant_gap_fill_enabled=gap_fill,
                ),
                server_request_security=None,
                server_oidc_login=None,
            )
        ),
        cookies={},
    )


class WorkflowAssistantM2AppConfigTests(unittest.TestCase):
    def test_auth_status_exposes_attachment_gate_under_master_only(self) -> None:
        enabled = auth_status(  # type: ignore[arg-type]
            request(
                master=True,
                attachments=True,
                project_changes=True,
                gap_fill=True,
            )
        )
        self.assertTrue(enabled.data["workflow_assistant_enabled"])
        self.assertTrue(enabled.data["workflow_assistant_attachments_enabled"])
        self.assertTrue(enabled.data["workflow_assistant_project_changes_enabled"])
        self.assertTrue(enabled.data["workflow_assistant_gap_fill_enabled"])

        disabled = auth_status(
            request(master=False, attachments=True, project_changes=True)
        )  # type: ignore[arg-type]
        self.assertFalse(disabled.data["workflow_assistant_enabled"])
        self.assertFalse(disabled.data["workflow_assistant_attachments_enabled"])
        self.assertFalse(disabled.data["workflow_assistant_project_changes_enabled"])
        self.assertFalse(disabled.data["workflow_assistant_gap_fill_enabled"])


if __name__ == "__main__":
    unittest.main()
