from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services.access_control import ActorIdentity  # noqa: E402
from workflow_assistant.gap_fill import (  # noqa: E402
    GapFillCandidateEvidence,
    GapFillCandidateResponse,
    GapFillRequest,
    GapFillSnapshotResponse,
    WorkflowAssistantGapFillService,
)
from workflow_assistant.http import gap_fill_plan  # noqa: E402
from workflow_assistant.repository import (  # noqa: E402
    WorkflowPlan,
    WorkflowPlanStep,
)


class FakeResearchRegistry:
    def __init__(self) -> None:
        self.resume_calls: list[dict[str, object]] = []

    def gap_fill_snapshot(self, **_kwargs: object) -> dict[str, object]:
        return {
            "project_id": "project-a",
            "research_thread_id": "thread-a",
            "retrieval_plan_id": "retrieval-a",
            "status": "waiting_for_review",
            "current_scope_id": "scope-a",
            "gap_reasons": ("missing product dimensions",),
            "gap_fill_round": 1,
            "max_gap_fill_rounds": 2,
            "discovery_queries_used": 1,
            "max_discovery_queries": 2,
            "evidence_pack_ids": ("pack-a",),
            "review_candidates": (
                {
                    "candidate_id": "official-candidate",
                    "url": "https://example.test/products/a",
                    "page_type": "product",
                    "needs_review": True,
                    "evidence": {
                        "same_site": True,
                        "channel": "tavily_discovery",
                        "score": 0.9,
                    },
                },
                {
                    "candidate_id": "third-party",
                    "url": "https://other.test/copied",
                    "page_type": "blog",
                    "needs_review": True,
                    "evidence": {
                        "same_site": False,
                        "channel": "tavily_discovery",
                    },
                },
            ),
        }

    def enqueue_resume(self, **kwargs: object) -> dict[str, object]:
        self.resume_calls.append(dict(kwargs))
        return {"job": {"job_id": "resume-job", "status": "queued"}}


def _waiting_plan() -> WorkflowPlan:
    return WorkflowPlan(
        organization_id="org-a",
        plan_id="plan-a",
        creator_user_id="user-a",
        conversation_id="conversation-a",
        title="Research plan",
        natural_language_request="Research the article",
        normalized_plan={},
        plan_hash="a" * 64,
        revision=4,
        status="waiting_review",
        project_ids=("project-a",),
        paused_project_ids=(),
        steps=(
            WorkflowPlanStep(
                step_id="research-step",
                sequence=1,
                action_kind="start_research",
                project_id="project-a",
                article_task_id="task-a",
                expected_task_revision=2,
                pinned_prompt_version={},
                pinned_knowledge_snapshot={},
                status="waiting_review",
                background_job_id="start-job",
                retry_count=0,
                hard_gate=False,
                human_gate_confirmed=False,
                input_summary={},
                output_summary={"research_thread_id": "thread-a"},
                standardized_error_code="human_confirmation_required",
            ),
        ),
        concurrency_limit=3,
        budget_warning=False,
        attention_state="user_confirmation",
        approved_by="user-a",
        approved_at=None,
    )


class FakeAssistantRepository:
    def __init__(self, plan: WorkflowPlan) -> None:
        self.plan = plan
        self.release_calls: list[dict[str, object]] = []

    def get_plan(self, **_kwargs: object) -> WorkflowPlan:
        return self.plan

    def release_research_gap_fill(self, **kwargs: object) -> WorkflowPlan:
        self.release_calls.append(dict(kwargs))
        step = self.plan.steps[0]
        updated_step = replace(
            step,
            status="waiting_job",
            background_job_id=str(kwargs["background_job_id"]),
            human_gate_confirmed=True,
            input_summary={
                "research_thread_id": "thread-a",
                "approved_candidate_ids": ["official-candidate"],
                "gap_fill_request_id": str(kwargs["request_id"]),
            },
            standardized_error_code=None,
        )
        self.plan = replace(
            self.plan,
            revision=self.plan.revision + 1,
            status="running",
            attention_state="none",
            steps=(updated_step,),
        )
        return self.plan


class FakeGapFillService:
    def __init__(self) -> None:
        self.enqueue_calls: list[dict[str, object]] = []

    def snapshot(self, **_kwargs: object) -> GapFillSnapshotResponse:
        return GapFillSnapshotResponse(
            project_id="project-a",
            research_thread_id="thread-a",
            retrieval_plan_id="retrieval-a",
            status="waiting_for_review",
            current_scope_id="scope-a",
            gap_reasons=["missing product dimensions"],
            gap_fill_round=1,
            max_gap_fill_rounds=2,
            discovery_queries_used=1,
            max_discovery_queries=2,
            evidence_pack_ids=["pack-a"],
            review_candidates=[
                GapFillCandidateResponse(
                    candidate_id="official-candidate",
                    url="https://example.test/products/a",
                    page_type="product",
                    needs_review=True,
                    evidence=GapFillCandidateEvidence(
                        channel="tavily_discovery",
                        same_site=True,
                    ),
                )
            ],
        )

    def enqueue_resume(self, **kwargs: object) -> dict[str, str]:
        self.enqueue_calls.append(dict(kwargs))
        return {"job_id": "resume-job", "status": "queued"}


class WorkflowAssistantGapFillTests(unittest.TestCase):
    def test_request_allows_explicit_rejection_but_rejects_duplicate_ids(self) -> None:
        payload = GapFillRequest(
            revision=3,
            step_id="research-step",
            research_thread_id="thread-a",
            request_id="request-123",
            approved_candidate_ids=[],
        )
        self.assertEqual(payload.approved_candidate_ids, [])
        with self.assertRaises(ValueError):
            GapFillRequest(
                revision=3,
                step_id="research-step",
                research_thread_id="thread-a",
                request_id="request-123",
                approved_candidate_ids=["candidate", "candidate"],
            )

    def test_snapshot_exposes_structured_gaps_and_filters_non_official_candidates(self) -> None:
        service = WorkflowAssistantGapFillService(FakeResearchRegistry())
        snapshot = service.snapshot(
            actor=ActorIdentity("org-a", "user-a"),
            project_id="project-a",
            thread_id="thread-a",
        )
        self.assertEqual(snapshot.gap_reasons, ["missing product dimensions"])
        self.assertEqual(
            [candidate.candidate_id for candidate in snapshot.review_candidates],
            ["official-candidate"],
        )
        self.assertEqual(
            snapshot.review_candidates[0].evidence.channel,
            "tavily_discovery",
        )

    def test_resume_keeps_candidate_ids_and_hides_urls(self) -> None:
        registry = FakeResearchRegistry()
        service = WorkflowAssistantGapFillService(registry)
        result = service.enqueue_resume(
            actor=ActorIdentity("org-a", "user-a"),
            project_id="project-a",
            thread_id="thread-a",
            request_id="assistant-gap-fill-123",
            approved_candidate_ids=("official-candidate",),
        )
        self.assertEqual(result, {"job_id": "resume-job", "status": "queued"})
        self.assertEqual(
            registry.resume_calls[0]["approved_candidate_ids"],
            ("official-candidate",),
        )
        self.assertNotIn("url", result)

    def test_http_gap_fill_releases_plan_and_wakes_runner(self) -> None:
        repository = FakeAssistantRepository(_waiting_plan())
        service = FakeGapFillService()
        runner = SimpleNamespace(calls=0)
        runner.wake = lambda: setattr(runner, "calls", runner.calls + 1)
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(workflow_assistant_runner=runner)))
        payload = GapFillRequest(
            revision=4,
            step_id="research-step",
            research_thread_id="thread-a",
            request_id="request-123",
            approved_candidate_ids=["official-candidate"],
        )
        with (
            patch("workflow_assistant.http._gap_fill_feature_enabled"),
            patch("workflow_assistant.http._repository", return_value=repository),
            patch("workflow_assistant.http._reauthorize_plan_read"),
            patch("workflow_assistant.http._gap_fill_service", return_value=service),
        ):
            response = gap_fill_plan(
                "plan-a",
                payload,
                request,  # type: ignore[arg-type]
                ActorIdentity("org-a", "user-a"),
            )
        self.assertEqual(response.queue_job_id, "resume-job")
        self.assertEqual(response.plan.status, "running")
        self.assertEqual(response.plan.steps[0].status, "waiting_job")
        self.assertEqual(runner.calls, 1)
        self.assertEqual(service.enqueue_calls[0]["approved_candidate_ids"], ["official-candidate"])
        self.assertEqual(repository.release_calls[0]["expected_revision"], 4)


if __name__ == "__main__":
    unittest.main()
