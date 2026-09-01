from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from knowledge_agent.research_runs import ResearchGraphRun  # noqa: E402
from models import TaskRecord  # noqa: E402
from services.access_control import ActorIdentity  # noqa: E402
from services.server_knowledge_research import (  # noqa: E402
    KNOWLEDGE_RESEARCH_OPERATION,
)
from workflow_assistant.adapters import (  # noqa: E402
    WorkflowAssistantServiceAdapters,
)
from workflow_assistant.tools import (  # noqa: E402
    WorkflowToolHumanGateRequired,
    WorkflowToolInvocation,
)


class FakeTaskStore:
    def __init__(self, task: TaskRecord) -> None:
        self.task = task

    def get(self, task_id: str) -> TaskRecord:
        if task_id != self.task.id:
            raise KeyError(task_id)
        return self.task


class FakeTaskFactory:
    def __init__(self, task: TaskRecord) -> None:
        self.task = task

    def create(self, _authorized: object) -> object:
        return SimpleNamespace(store=FakeTaskStore(self.task))


class FakeResearchRuns:
    def __init__(self, *runs: ResearchGraphRun) -> None:
        self.runs = {run.thread_id: run for run in runs}

    def get_run(self, project_id: str, thread_id: str) -> ResearchGraphRun | None:
        run = self.runs.get(thread_id)
        return run if run is not None and run.project_id == project_id else None

    def list_runs(
        self,
        project_id: str,
        *,
        article_id: str | None = None,
        limit: int = 50,
    ) -> tuple[ResearchGraphRun, ...]:
        return tuple(
            run
            for run in self.runs.values()
            if run.project_id == project_id
            and (article_id is None or run.article_id == article_id)
        )[:limit]


class FakeKnowledgeResearch:
    def __init__(self) -> None:
        self.create_plan_calls: list[dict[str, object]] = []
        self.enqueue_start_calls: list[dict[str, object]] = []
        self.enqueue_resume_calls: list[dict[str, object]] = []

    def create_plan_from_task(self, **kwargs: object) -> object:
        self.create_plan_calls.append(dict(kwargs))
        return SimpleNamespace(retrieval_plan_id="new-retrieval-plan")

    def enqueue_start(self, **kwargs: object) -> dict[str, object]:
        self.enqueue_start_calls.append(dict(kwargs))
        return {"job": _job("start-job")}

    def enqueue_resume(self, **kwargs: object) -> dict[str, object]:
        self.enqueue_resume_calls.append(dict(kwargs))
        return {"job": _job("resume-job")}


def _task() -> TaskRecord:
    return TaskRecord(
        id="task-a",
        week_folder="server",
        customer="example.com",
        topic_index=1,
        topic="Buyer Guide",
        task_dir="/server/task-a",
        revision=1,
        created_at="2026-08-20T00:00:00Z",
        updated_at="2026-08-20T00:00:00Z",
    )


def _run(status: str) -> ResearchGraphRun:
    return ResearchGraphRun(
        project_id="project-a",
        thread_id="thread-a",
        organization_id="org-a",
        retrieval_plan_id="retrieval-a",
        article_id="topic_001",
        outline_version=1,
        status=status,  # type: ignore[arg-type]
        current_node=(
            "await_human_review"
            if status == "waiting_for_review"
            else "complete"
        ),
        current_scope_id="scope-a",
        gap_fill_round=1,
        max_gap_fill_rounds=2,
        discovery_queries_used=1,
        max_discovery_queries=2,
        evidence_pack_ids=("pack-a",),
        warnings=("bounded warning",),
        metadata={
            "task_id": "task-a",
            "request_id": "assistant-plan-a-research-a",
        },
    )


def _job(job_id: str, *, status: str = "queued") -> dict[str, object]:
    return {
        "job_id": job_id,
        "batch_id": "batch-a",
        "task_id": "task-a",
        "operation": KNOWLEDGE_RESEARCH_OPERATION,
        "status": status,
        "source_revision": 1,
        "result_revision": 1 if status == "succeeded" else None,
        "attempts": 1,
        "created_at": "2026-08-20T00:00:00Z",
        "started_at": None,
        "finished_at": None,
        "updated_at": "2026-08-20T00:00:01Z",
        "error": "",
    }


def _invocation(
    input_summary: dict[str, object],
    *,
    human_gate_confirmed: bool = True,
) -> WorkflowToolInvocation:
    return WorkflowToolInvocation(
        actor=ActorIdentity(organization_id="org-a", user_id="user-a"),
        plan_id="plan-a",
        step_id="research-a",
        action_kind="start_research",
        project_id="project-a",
        article_task_id="task-a",
        expected_task_revision=1,
        input_summary=input_summary,
        pinned_prompt_version={},
        pinned_knowledge_snapshot={},
        confirmed=True,
        human_gate_confirmed=human_gate_confirmed,
    )


def _adapter(
    run: ResearchGraphRun,
    *,
    engine: MagicMock | None = None,
) -> tuple[WorkflowAssistantServiceAdapters, FakeKnowledgeResearch]:
    service = FakeKnowledgeResearch()
    adapter = WorkflowAssistantServiceAdapters(
        engine=engine or MagicMock(),
        config=SimpleNamespace(),  # type: ignore[arg-type]
        task_factory=FakeTaskFactory(_task()),  # type: ignore[arg-type]
        knowledge_research=service,
    )
    adapter._research_runs = FakeResearchRuns(run)  # type: ignore[assignment]
    return adapter, service


def _job_status_adapter(
    run: ResearchGraphRun,
) -> WorkflowAssistantServiceAdapters:
    engine = MagicMock()
    connection = engine.connect.return_value.__enter__.return_value
    row = {
        **_job("research-job", status="succeeded"),
        "request": {
            "action": "start",
            "thread_id": "thread-a",
            "retrieval_plan_id": "retrieval-a",
        },
    }
    connection.execute.return_value.mappings.return_value.one_or_none.return_value = row
    adapter, _ = _adapter(run, engine=engine)
    return adapter


def _article_job_status_adapter(
    task: TaskRecord,
) -> WorkflowAssistantServiceAdapters:
    engine = MagicMock()
    connection = engine.connect.return_value.__enter__.return_value
    row = {
        **_job("article-job", status="succeeded"),
        "operation": "article",
        "request": {},
    }
    connection.execute.return_value.mappings.return_value.one_or_none.return_value = row
    return WorkflowAssistantServiceAdapters(
        engine=engine,
        config=SimpleNamespace(),  # type: ignore[arg-type]
        task_factory=FakeTaskFactory(task),  # type: ignore[arg-type]
    )


class WorkflowAssistantResearchGateTests(unittest.TestCase):
    def test_succeeded_article_job_requires_a_persisted_body(self) -> None:
        task = _task().model_copy(
            update={
                "initial_article": "A persisted article body.",
                "initial_article_word_count": 4,
            }
        )
        adapter = _article_job_status_adapter(task)
        step = SimpleNamespace(
            background_job_id="article-job",
            article_task_id="task-a",
            action_kind="generate_article",
            project_id="project-a",
            input_summary={"operation": "article"},
        )

        status = adapter.job_status(
            ActorIdentity(organization_id="org-a", user_id="user-a"),
            step,
        )

        self.assertEqual(status["status"], "succeeded")
        self.assertTrue(status["article_ready"])
        self.assertEqual(status["article_word_count"], 4)

    def test_succeeded_article_job_without_body_is_projected_as_failure(self) -> None:
        adapter = _article_job_status_adapter(_task())
        step = SimpleNamespace(
            background_job_id="article-job",
            article_task_id="task-a",
            action_kind="generate_article",
            project_id="project-a",
            input_summary={"operation": "article"},
        )

        status = adapter.job_status(
            ActorIdentity(organization_id="org-a", user_id="user-a"),
            step,
        )

        self.assertEqual(status["status"], "failed")
        self.assertTrue(status["has_error"])
        self.assertTrue(status["article_result_missing"])
        self.assertFalse(status["article_ready"])

    def test_succeeded_job_waiting_run_projects_runtime_human_gate(self) -> None:
        adapter = _job_status_adapter(_run("waiting_for_review"))
        step = SimpleNamespace(
            background_job_id="research-job",
            article_task_id="task-a",
            action_kind="start_research",
            project_id="project-a",
            input_summary={},
        )

        status = adapter.job_status(
            ActorIdentity(organization_id="org-a", user_id="user-a"),
            step,
        )

        self.assertEqual(status["status"], "waiting_review")
        self.assertTrue(status["review_required"])
        self.assertEqual(status["job_id"], "research-job")
        self.assertEqual(status["research_thread_id"], "thread-a")
        self.assertEqual(status["retrieval_plan_id"], "retrieval-a")
        self.assertEqual(status["warning_count"], 1)
        self.assertNotIn("request", status)
        self.assertNotIn("approved_candidate_ids", status)
        self.assertNotIn("candidate_urls", status)

    def test_completed_with_warnings_run_projects_success(self) -> None:
        adapter = _job_status_adapter(_run("completed_with_warnings"))
        step = SimpleNamespace(
            background_job_id="research-job",
            article_task_id="task-a",
            action_kind="start_research",
            project_id="project-a",
            input_summary={},
        )

        status = adapter.job_status(
            ActorIdentity(organization_id="org-a", user_id="user-a"),
            step,
        )

        self.assertEqual(status["status"], "succeeded")
        self.assertFalse(status["review_required"])
        self.assertEqual(status["evidence_pack_ids"], ["pack-a"])
        self.assertEqual(status["research_status"], "completed_with_warnings")

    def test_waiting_run_without_explicit_selection_re_gates(self) -> None:
        adapter, service = _adapter(_run("waiting_for_review"))

        with patch.object(adapter, "_active_research_job", return_value=None):
            with self.assertRaises(WorkflowToolHumanGateRequired):
                adapter.handlers()["start_research"](_invocation({}))

        self.assertEqual(service.enqueue_resume_calls, [])
        self.assertEqual(service.enqueue_start_calls, [])
        self.assertEqual(service.create_plan_calls, [])

    def test_waiting_run_resumes_only_with_explicit_candidate_selection(self) -> None:
        adapter, service = _adapter(_run("waiting_for_review"))
        invocation = _invocation(
            {
                "research_thread_id": "thread-a",
                "approved_candidate_ids": ["candidate-a", "candidate-a"],
            }
        )

        with patch.object(adapter, "_active_research_job", return_value=None):
            result = adapter.handlers()["start_research"](invocation)

        self.assertEqual(result["_workflow_status"], "waiting_job")
        self.assertEqual(result["job_id"], "resume-job")
        self.assertEqual(result["research_thread_id"], "thread-a")
        self.assertEqual(len(service.enqueue_resume_calls), 1)
        call = service.enqueue_resume_calls[0]
        self.assertEqual(call["thread_id"], "thread-a")
        self.assertEqual(call["approved_candidate_ids"], ("candidate-a",))
        self.assertTrue(str(call["request_id"]).startswith("assistant-resume-"))
        self.assertEqual(service.enqueue_start_calls, [])
        self.assertEqual(service.create_plan_calls, [])

    def test_explicit_selection_still_requires_runtime_gate_confirmation(self) -> None:
        adapter, service = _adapter(_run("waiting_for_review"))
        invocation = _invocation(
            {
                "research_thread_id": "thread-a",
                "approved_candidate_ids": ["candidate-a"],
            },
            human_gate_confirmed=False,
        )

        with patch.object(adapter, "_active_research_job", return_value=None):
            with self.assertRaises(WorkflowToolHumanGateRequired):
                adapter.handlers()["start_research"](invocation)

        self.assertEqual(service.enqueue_resume_calls, [])

    def test_explicit_empty_candidate_selection_can_reject_all(self) -> None:
        adapter, service = _adapter(_run("waiting_for_review"))
        invocation = _invocation(
            {
                "research_thread_id": "thread-a",
                "approved_candidate_ids": [],
            }
        )

        with patch.object(adapter, "_active_research_job", return_value=None):
            adapter.handlers()["start_research"](invocation)

        self.assertEqual(
            service.enqueue_resume_calls[0]["approved_candidate_ids"],
            (),
        )

    def test_completed_existing_run_returns_without_requeue(self) -> None:
        adapter, service = _adapter(_run("completed"))

        result = adapter.handlers()["start_research"](_invocation({}))

        self.assertEqual(result["evidence_pack_ids"], ["pack-a"])
        self.assertFalse(result["review_required"])
        self.assertEqual(service.enqueue_resume_calls, [])
        self.assertEqual(service.enqueue_start_calls, [])
        self.assertEqual(service.create_plan_calls, [])


if __name__ == "__main__":
    unittest.main()
