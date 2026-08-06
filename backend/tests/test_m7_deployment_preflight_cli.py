from __future__ import annotations

import argparse
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from knowledge_agent import m7_deployment_preflight  # noqa: E402
from services.deployment_readiness import (  # noqa: E402
    DeploymentPreflightReport,
    PreflightCheck,
)
from services.recovery_evidence import RecoveryEvidenceError  # noqa: E402


class DeploymentPreflightCliTests(unittest.TestCase):
    def test_missing_evidence_arguments_emit_safe_no_go_json(self) -> None:
        report = DeploymentPreflightReport(
            (
                PreflightCheck(
                    "recovery_evidence_identity",
                    False,
                    "not verified",
                ),
            )
        )
        output = io.StringIO()
        with (
            patch.object(sys, "argv", ["m7-deployment-preflight"]),
            patch.dict(
                os.environ,
                {
                    "ARTICLE_AGENT_RECOVERY_EVIDENCE_PUBLIC_KEY": (
                        "private-invalid-key"
                    ),
                },
                clear=True,
            ),
            patch.object(
                m7_deployment_preflight,
                "run_deployment_preflight",
                return_value=report,
            ) as run_preflight,
            redirect_stdout(output),
        ):
            exit_code = m7_deployment_preflight.main()

        self.assertEqual(exit_code, 2)
        self.assertEqual(json.loads(output.getvalue()), report.public_values())
        self.assertNotIn("private-invalid-key", output.getvalue())
        self.assertIsNone(
            run_preflight.call_args.kwargs["recovery_evidence"]
        )

    def test_path_key_and_parse_failures_are_silently_fail_closed(self) -> None:
        arguments = argparse.Namespace(
            recovery_evidence=r"C:\private\recovery-secret.json",
            release_commit="1" * 40,
        )
        environment = {
            "ARTICLE_AGENT_RECOVERY_EVIDENCE_PUBLIC_KEY": (
                "private-invalid-public-key"
            )
        }
        with patch.object(
            m7_deployment_preflight,
            "load_verified_recovery_evidence",
            side_effect=RecoveryEvidenceError(),
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                verified = m7_deployment_preflight._load_recovery_evidence(
                    arguments,
                    environment,
                )

        self.assertIsNone(verified)
        self.assertEqual(output.getvalue(), "")

    def test_cli_has_no_manual_restore_attestation_flag(self) -> None:
        options = {
            option
            for action in m7_deployment_preflight._parser()._actions
            for option in action.option_strings
        }
        self.assertIn("--recovery-evidence", options)
        self.assertIn("--release-commit", options)
        self.assertNotIn("--backup-restore-drill-passed", options)


if __name__ == "__main__":
    unittest.main()
