from __future__ import annotations

import sys
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from workflow_assistant.attachment_jobs import (  # noqa: E402
    AttachmentJob,
    AttachmentJobOrganizationDispatcher,
    AttachmentJobResult,
    AttachmentJobRetryableError,
    AttachmentJobRunner,
    PendingAttachmentJobAuthorization,
    validate_attachment_job_target,
)


def job() -> AttachmentJob:
    return AttachmentJob(
        job_id="job-a",
        organization_id="org-a",
        requested_by_user_id="user-a",
        project_id=None,
        attachment_id="attachment-a",
        proposal_id=None,
        operation="classify_attachment",
        idempotency_key="classify-a",
        expected_attachment_revision=0,
        expected_proposal_revision=None,
        request_payload={"safe": True},
        status="running",
        attempts=1,
    )


class FakeRepository:
    def __init__(self) -> None:
        self.item = job()
        self.calls: list[object] = []
        self.cancelled = False

    def recover_interrupted(self) -> int:
        self.calls.append("recover")
        return 1

    def list_claim_candidates(self, *, limit: int):
        self.calls.append(("list", limit))
        return (
            PendingAttachmentJobAuthorization(
                job_id=self.item.job_id,
                organization_id=self.item.organization_id,
                requested_by_user_id=self.item.requested_by_user_id,
                project_id=self.item.project_id,
                attachment_id=self.item.attachment_id,
                proposal_id=self.item.proposal_id,
                operation=self.item.operation,
            ),
        )

    def claim_authorized(self, job_ids, *, limit):
        self.calls.append(("claim", job_ids, limit))
        return (self.item,) if job_ids else ()

    def reject_authorization(self, job_id):
        self.calls.append(("reject", job_id))
        return True

    def is_cancel_requested(self, job_id):
        return self.cancelled

    def mark_succeeded(self, current, result):
        self.calls.append(("succeeded", current.job_id, result))

    def mark_failed(self, current, *, error_code, retryable):
        self.calls.append(("failed", current.job_id, error_code, retryable))
        return "retry_wait" if retryable else "failed"

    def mark_cancelled(self, current):
        self.calls.append(("cancelled", current.job_id))

    def mark_interrupted(self, current):
        self.calls.append(("interrupted", current.job_id))


class AttachmentJobRunnerTests(unittest.TestCase):
    def test_preview_build_has_project_but_no_precreated_proposal(self) -> None:
        validate_attachment_job_target(
            operation="preview_import_proposal",
            project_id="project-a",
            proposal_id=None,
            expected_proposal_revision=None,
        )
        with self.assertRaises(ValueError):
            validate_attachment_job_target(
                operation="preview_import_proposal",
                project_id="project-a",
                proposal_id="empty-proposal",
                expected_proposal_revision=0,
            )
        with self.assertRaises(ValueError):
            validate_attachment_job_target(
                operation="execute_import_proposal",
                project_id="project-a",
                proposal_id=None,
                expected_proposal_revision=None,
            )
        validate_attachment_job_target(
            operation="execute_import_proposal",
            project_id="project-a",
            proposal_id="proposal-a",
            expected_proposal_revision=2,
        )

    def test_runner_reauthorizes_before_execution_and_commit(self) -> None:
        repository = FakeRepository()
        phases: list[str] = []
        handled: list[str] = []

        def authorize(_job, phase):
            phases.append(phase)

        runner = AttachmentJobRunner(
            repository,
            authorize=authorize,
            handlers={
                "classify_attachment": lambda current, cancelled, commit: (
                    handled.append(current.job_id)
                    or commit()
                    or AttachmentJobResult({"classification": "unsupported"}, 0)
                )
            },
        )

        self.assertEqual(runner.recover(), 1)
        self.assertEqual(runner.run_once(), 1)
        self.assertEqual(phases, ["execute", "execute", "commit"])
        self.assertEqual(handled, ["job-a"])
        self.assertEqual(repository.calls[-1][0], "succeeded")

    def test_missing_handler_fails_closed(self) -> None:
        repository = FakeRepository()
        runner = AttachmentJobRunner(
            repository,
            authorize=lambda _job, _phase: None,
            handlers={},
        )

        runner.run_once()

        self.assertIn(("failed", "job-a", "handler_unavailable", False), repository.calls)
        self.assertFalse(any(call[0] == "succeeded" for call in repository.calls if isinstance(call, tuple)))

    def test_commit_authorization_revocation_cannot_publish_success(self) -> None:
        repository = FakeRepository()

        def authorize(_job, phase):
            if phase == "commit":
                raise PermissionError("revoked")

        runner = AttachmentJobRunner(
            repository,
            authorize=authorize,
            handlers={
                "classify_attachment": lambda _job, _cancelled, commit: (
                    commit() or AttachmentJobResult({}, 0)
                )
            },
        )

        runner.run_once()

        self.assertIn(("failed", "job-a", "authorization_changed", False), repository.calls)
        self.assertFalse(any(call[0] == "succeeded" for call in repository.calls if isinstance(call, tuple)))

    def test_handler_cannot_publish_success_without_commit_authorization(self) -> None:
        repository = FakeRepository()
        runner = AttachmentJobRunner(
            repository,
            authorize=lambda _job, _phase: None,
            handlers={
                "classify_attachment": lambda _job, _cancelled, _commit: (
                    AttachmentJobResult({}, 0)
                )
            },
        )

        runner.run_once()

        self.assertIn(
            ("failed", "job-a", "commit_authorization_missing", False),
            repository.calls,
        )
        self.assertFalse(
            any(
                call[0] == "succeeded"
                for call in repository.calls
                if isinstance(call, tuple)
            )
        )

    def test_retryable_handler_error_uses_safe_standard_code(self) -> None:
        repository = FakeRepository()

        def fail(_job, _cancelled, _commit):
            raise AttachmentJobRetryableError("provider leaked secret")

        runner = AttachmentJobRunner(
            repository,
            authorize=lambda _job, _phase: None,
            handlers={"classify_attachment": fail},
        )

        runner.run_once()

        self.assertIn(("failed", "job-a", "transient_failure", True), repository.calls)
        self.assertNotIn("provider leaked secret", repr(repository.calls))

    def test_candidate_authorization_failure_rejects_without_claim_or_payload(self) -> None:
        repository = FakeRepository()
        runner = AttachmentJobRunner(
            repository,
            authorize=lambda _job, _phase: (_ for _ in ()).throw(PermissionError()),
            handlers={},
        )

        self.assertEqual(runner.run_once(), 0)
        self.assertIn(("reject", "job-a"), repository.calls)
        self.assertFalse(any(call[0] == "claim" for call in repository.calls if isinstance(call, tuple)))

    def test_global_dispatcher_discovers_durable_organization_ids(self) -> None:
        class Discovery:
            def list_pending_organization_ids(self, *, limit):
                self.limit = limit
                return ("org-a", "org-b")

        class Runner:
            def __init__(self, organization_id):
                self.organization_id = organization_id

            def recover(self):
                calls.append((self.organization_id, "recover"))
                return 0

            def run_once(self, *, limit):
                calls.append((self.organization_id, "run", limit))
                return 1

        calls: list[tuple[object, ...]] = []
        discovery = Discovery()
        dispatcher = AttachmentJobOrganizationDispatcher(
            discovery,
            runner_factory=Runner,
        )

        self.assertEqual(
            dispatcher.run_once(organization_limit=7, jobs_per_organization=2),
            2,
        )
        self.assertEqual(discovery.limit, 7)
        self.assertEqual(
            calls,
            [
                ("org-a", "recover"),
                ("org-a", "run", 2),
                ("org-b", "recover"),
                ("org-b", "run", 2),
            ],
        )


if __name__ == "__main__":
    unittest.main()
