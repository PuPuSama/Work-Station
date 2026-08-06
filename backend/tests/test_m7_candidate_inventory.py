from __future__ import annotations

import argparse
import io
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from starlette.routing import Route


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app import app  # noqa: E402
from services.candidate_inventory import (  # noqa: E402
    CandidateInventoryError,
    build_candidate_inventory,
    build_operation_inventory,
    build_route_inventory,
    inventory_json,
    validate_release_commit,
    verify_release_checkout,
)
from services.server_job_control import (  # noqa: E402
    SERVER_JOB_CONTROL_OPERATIONS,
    SERVER_JOB_DOMAIN_CONTROL_BLOCKED,
)
from services.authorized_job_queue import worker_permission_for  # noqa: E402
from knowledge_agent import m7_candidate_inventory as cli  # noqa: E402
from services import candidate_inventory as candidate_module  # noqa: E402


RELEASE_COMMIT = "1" * 40
EXPECTED_ROUTE_COUNT = 182


def application_routes() -> list[Route]:
    return [route for route in app.routes if isinstance(route, Route)]


class CandidateInventoryTests(unittest.TestCase):
    def test_real_route_graph_and_operations_are_completely_classified(
        self,
    ) -> None:
        inventory = build_candidate_inventory(
            application_routes(),
            release_commit=RELEASE_COMMIT,
        )
        route_inventory = inventory["route_inventory"]
        operation_inventory = inventory["operation_inventory"]
        self.assertIsInstance(route_inventory, dict)
        self.assertIsInstance(operation_inventory, dict)
        assert isinstance(route_inventory, dict)
        assert isinstance(operation_inventory, dict)

        entries = route_inventory["entries"]
        self.assertEqual(len(entries), EXPECTED_ROUTE_COUNT)
        self.assertEqual(
            sum(route_inventory["counts"].values()),
            EXPECTED_ROUTE_COUNT,
        )
        by_identity = {
            (entry["method"], entry["path"]): entry for entry in entries
        }
        for identity in (
            ("POST", "/api/projects/{project}/tasks/{task_id}/products"),
            (
                "GET",
                "/api/projects/{project}/tasks/{task_id}/products/"
                "jobs/{job_id}",
            ),
            ("PUT", "/api/projects/{project}/tasks/{task_id}/products"),
        ):
            self.assertEqual(by_identity[identity]["state"], "server_ready")
        self.assertEqual(
            by_identity[
                ("POST", "/api/tasks/{task_id}/products/auto")
            ]["state"],
            "local_only_fail_closed",
        )
        self.assertEqual(
            by_identity[("GET", "/api/batches")]["state"],
            "intentionally_unsupported",
        )
        self.assertEqual(
            by_identity[
                ("POST", "/api/knowledge/{project}/wordpress/probe")
            ]["state"],
            "local_only_fail_closed",
        )

        operation_entries = operation_inventory["entries"]
        self.assertEqual(
            {entry["operation"] for entry in operation_entries},
            set(SERVER_JOB_CONTROL_OPERATIONS),
        )
        by_operation = {
            entry["operation"]: entry for entry in operation_entries
        }
        products = by_operation["products"]
        self.assertEqual(products["enqueue_authorization"], "article.edit")
        self.assertEqual(products["claim_authorization"], "article.edit")
        self.assertEqual(products["handler_authorization"], "article.edit")
        self.assertEqual(products["commit_boundary"], "postgres_task_cas_and_audit")
        self.assertEqual(products["cancel"], "project_job_control")
        self.assertEqual(products["retry"], "project_job_control")
        self.assertEqual(
            by_operation["knowledge_research"]["cancel"],
            "domain_controlled_only",
        )
        self.assertEqual(
            by_operation["knowledge_research"]["retry"],
            "domain_controlled_only",
        )

    def test_route_metadata_and_framework_boundaries(self) -> None:
        inventory = build_route_inventory(
            application_routes(),
            release_commit=RELEASE_COMMIT,
        )
        entries = inventory["entries"]
        required_fields = {
            "evidence_id", "gate", "method", "name", "path",
            "permission", "reauthorization", "scope", "state", "storage",
        }
        self.assertTrue(all(set(entry) == required_fields for entry in entries))
        evidence_ids = [entry["evidence_id"] for entry in entries]
        self.assertEqual(len(evidence_ids), len(set(evidence_ids)))
        by_identity = {
            (entry["method"], entry["path"]): entry for entry in entries
        }
        for path in (
            "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc",
        ):
            self.assertEqual(
                by_identity[("GET", path)]["state"],
                "local_only_fail_closed",
            )
        self.assertFalse(any(entry["method"] == "HEAD" for entry in entries))
        self.assertTrue(all(
            entry["gate"] == "server_http_and_knowledge"
            for entry in entries
            if entry["path"].startswith("/api/knowledge/")
        ))

        async def endpoint(request):
            del request

        options = Route(
            "/api/auth/status", endpoint,
            methods=["OPTIONS"], name="explicit_options",
        )
        option_inventory = build_route_inventory(
            [options], release_commit=RELEASE_COMMIT,
        )
        self.assertEqual(option_inventory["entries"][0]["method"], "OPTIONS")

    def test_route_enumeration_fails_closed_when_incomplete(self) -> None:
        async def endpoint(request):
            del request

        missing_name = Route("/api/test", endpoint, methods=["POST"])
        missing_name.name = ""
        missing_methods = Route(
            "/api/test", endpoint, methods=["POST"], name="test",
        )
        missing_methods.methods = None
        unknown_parameter = Route(
            "/api/{new_parameter}", endpoint,
            methods=["GET"], name="unknown_parameter",
        )
        cases = (
            ([missing_name], "route_name_missing"),
            ([missing_methods], "route_methods_missing"),
            ([unknown_parameter], "route_parameter_unsupported"),
            ([], "route_inventory_empty"),
        )
        for routes, error in cases:
            with self.subTest(error=error):
                with self.assertRaisesRegex(
                    CandidateInventoryError, f"^{error}$",
                ):
                    build_route_inventory(
                        routes, release_commit=RELEASE_COMMIT,
                    )

        unknown_metadata = Route(
            "/api/projects/{project}/tasks",
            endpoint,
            methods=["POST"],
            name="new_unclassified_server_route",
        )
        with self.assertRaisesRegex(
            CandidateInventoryError, "^route_metadata_incomplete$",
        ):
            build_route_inventory(
                [unknown_metadata], release_commit=RELEASE_COMMIT,
            )

    def test_operation_inventory_has_complete_authoritative_semantics(
        self,
    ) -> None:
        inventory = build_operation_inventory(release_commit=RELEASE_COMMIT)
        required_fields = {
            "audit_actions", "cancel", "claim_authorization",
            "commit_boundary", "drain", "enqueue_authorization",
            "enqueue_transaction", "handler_authorization", "operation",
            "queue_store", "retry", "state",
        }
        for entry in inventory["entries"]:
            operation = entry["operation"]
            permission = worker_permission_for(operation)
            self.assertEqual(set(entry), required_fields)
            for field in (
                "enqueue_authorization", "claim_authorization",
                "handler_authorization",
            ):
                self.assertEqual(entry[field], permission)
            self.assertEqual(entry["queue_store"], "postgresql")
            self.assertEqual(
                entry["enqueue_transaction"], "job_batch_audit_atomic",
            )
            self.assertEqual(entry["drain"], "bounded_stop_report")
            expected_control = (
                "domain_controlled_only"
                if operation in SERVER_JOB_DOMAIN_CONTROL_BLOCKED
                else "project_job_control"
            )
            self.assertEqual(entry["cancel"], expected_control)
            self.assertEqual(entry["retry"], expected_control)
            self.assertTrue(entry["audit_actions"])
            self.assertTrue(entry["commit_boundary"])

    def test_operation_declaration_drift_fails_closed(self) -> None:
        incomplete = dict(candidate_module._OPERATION_AUDIT_ACTIONS)
        incomplete.pop("products")
        with mock.patch.object(
            candidate_module, "_OPERATION_AUDIT_ACTIONS", incomplete,
        ):
            with self.assertRaisesRegex(
                CandidateInventoryError,
                "^operation_inventory_incomplete$",
            ):
                build_operation_inventory(release_commit=RELEASE_COMMIT)

    def test_digest_is_deterministic_and_commit_bound(self) -> None:
        routes = application_routes()
        first = build_candidate_inventory(
            routes,
            release_commit=RELEASE_COMMIT,
        )
        reordered = build_candidate_inventory(
            list(reversed(routes)),
            release_commit=RELEASE_COMMIT,
        )
        changed_commit = build_candidate_inventory(
            routes,
            release_commit="2" * 40,
        )
        self.assertEqual(inventory_json(first), inventory_json(reordered))
        self.assertNotEqual(
            first["route_inventory"]["sha256"],
            changed_commit["route_inventory"]["sha256"],
        )
        self.assertNotEqual(
            first["operation_inventory"]["sha256"],
            changed_commit["operation_inventory"]["sha256"],
        )

    def test_invalid_commit_and_duplicate_route_fail_closed(self) -> None:
        for value in (
            "", "A" * 40, "1" * 39, "1" * 64,
            " " + RELEASE_COMMIT, RELEASE_COMMIT + "\n",
        ):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    CandidateInventoryError,
                    "^invalid_release_commit$",
                ):
                    validate_release_commit(value)
        route = application_routes()[0]
        with self.assertRaisesRegex(
            CandidateInventoryError,
            "^duplicate_route_identity$",
        ):
            build_route_inventory(
                [route, route],
                release_commit=RELEASE_COMMIT,
            )

    def test_checkout_verification_requires_exact_head_and_clean_tree(
        self,
    ) -> None:
        calls: list[list[str]] = []
        root = Path("D:/candidate").resolve()

        def runner(command, **kwargs):
            calls.append(command)
            if command[-1] == "--show-toplevel":
                stdout = str(root)
            elif command[-1] == "HEAD":
                stdout = RELEASE_COMMIT + "\n"
            else:
                stdout = ""
            return subprocess.CompletedProcess(command, 0, stdout=stdout)

        verify_release_checkout(
            root,
            release_commit=RELEASE_COMMIT,
            runner=runner,
        )
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(
            any(part.startswith("safe.directory=") for part in command)
            for command in calls
        ))

        def wrong_head(command, **kwargs):
            if command[-1] == "--show-toplevel":
                stdout = str(root)
            elif command[-1] == "HEAD":
                stdout = "2" * 40
            else:
                stdout = ""
            return subprocess.CompletedProcess(command, 0, stdout=stdout)

        with self.assertRaisesRegex(
            CandidateInventoryError,
            "^release_commit_mismatch$",
        ):
            verify_release_checkout(
                root,
                release_commit=RELEASE_COMMIT,
                runner=wrong_head,
            )

        def dirty(command, **kwargs):
            if command[-1] == "--show-toplevel":
                stdout = str(root)
            elif command[-1] == "HEAD":
                stdout = RELEASE_COMMIT
            else:
                stdout = " M backend/app.py"
            return subprocess.CompletedProcess(command, 0, stdout=stdout)

        with self.assertRaisesRegex(
            CandidateInventoryError,
            "^release_checkout_not_clean$",
        ):
            verify_release_checkout(
                root,
                release_commit=RELEASE_COMMIT,
                runner=dirty,
            )

        def wrong_root(command, **kwargs):
            if command[-1] == "--show-toplevel":
                stdout = str(root.parent)
            elif command[-1] == "HEAD":
                stdout = RELEASE_COMMIT
            else:
                stdout = ""
            return subprocess.CompletedProcess(command, 0, stdout=stdout)

        with self.assertRaisesRegex(
            CandidateInventoryError, "^repository_root_mismatch$",
        ):
            verify_release_checkout(
                root, release_commit=RELEASE_COMMIT, runner=wrong_root,
            )

        def broken(command, **kwargs):
            raise subprocess.CalledProcessError(1, command, stderr="secret")

        with self.assertRaises(CandidateInventoryError) as raised:
            verify_release_checkout(
                root, release_commit=RELEASE_COMMIT, runner=broken,
            )
        self.assertEqual(raised.exception.code, "repository_check_failed")
        self.assertNotIn("secret", str(raised.exception))

    def test_cli_writes_external_deterministic_public_projection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repository_root = base / "repository"
            repository_root.mkdir()
            output = base / "candidate.json"
            arguments = argparse.Namespace(
                release_commit=RELEASE_COMMIT,
                output=output,
                repository_root=repository_root,
            )
            with mock.patch.object(
                cli, "verify_release_checkout",
            ) as verify:
                public = cli.run(arguments)
            self.assertEqual(verify.call_count, 3)
            self.assertEqual(public["release_commit"], RELEASE_COMMIT)
            self.assertEqual(public["route_count"], EXPECTED_ROUTE_COUNT)
            self.assertEqual(public["operation_count"], 9)
            written = output.read_text(encoding="utf-8")
            expected = inventory_json(build_candidate_inventory(
                application_routes(), release_commit=RELEASE_COMMIT,
            ))
            self.assertEqual(written, expected)
            self.assertEqual(set(public), {
                "operation_count", "operation_inventory_digest",
                "release_commit", "route_count", "route_inventory_digest",
            })

            with self.assertRaisesRegex(
                CandidateInventoryError, "^output_already_exists$",
            ):
                cli.run(arguments)

            inside = argparse.Namespace(
                release_commit=RELEASE_COMMIT,
                output=repository_root / "candidate.json",
                repository_root=repository_root,
            )
            with self.assertRaisesRegex(
                CandidateInventoryError, "^output_must_be_external$",
            ):
                cli.run(inside)

    def test_cli_cleans_staging_on_checkout_drift_and_write_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repository_root = base / "repository"
            repository_root.mkdir()
            output = base / "candidate.json"
            arguments = argparse.Namespace(
                release_commit=RELEASE_COMMIT,
                output=output,
                repository_root=repository_root,
            )
            with mock.patch.object(
                cli,
                "verify_release_checkout",
                side_effect=[
                    None, None,
                    CandidateInventoryError("release_checkout_not_clean"),
                ],
            ):
                with self.assertRaisesRegex(
                    CandidateInventoryError,
                    "^release_checkout_not_clean$",
                ):
                    cli.run(arguments)
            self.assertFalse(output.exists())
            self.assertEqual(list(base.glob(".*.tmp")), [])

            with mock.patch.object(
                cli, "verify_release_checkout",
            ), mock.patch.object(
                cli.tempfile, "NamedTemporaryFile",
                side_effect=OSError("secret path"),
            ):
                with self.assertRaises(CandidateInventoryError) as raised:
                    cli.run(arguments)
            self.assertEqual(raised.exception.code, "artifact_write_failed")
            self.assertNotIn("secret", str(raised.exception))
            self.assertFalse(output.exists())

    def test_cli_main_redacts_known_and_unknown_errors(self) -> None:
        for exception, expected in (
            (CandidateInventoryError("stable_code"), "stable_code"),
            (RuntimeError("secret provider detail"), "inventory_generation_failed"),
        ):
            stderr = io.StringIO()
            with self.subTest(expected=expected), mock.patch.object(
                cli, "run", side_effect=exception,
            ), redirect_stderr(stderr):
                self.assertEqual(cli.main([
                    "--release-commit", RELEASE_COMMIT,
                    "--output", "D:/candidate.json",
                ]), 2)
            message = stderr.getvalue()
            self.assertIn(expected, message)
            self.assertNotIn("secret provider detail", message)


if __name__ == "__main__":
    unittest.main()
