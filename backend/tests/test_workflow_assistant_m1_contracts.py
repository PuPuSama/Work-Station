from __future__ import annotations

import json
import sys
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from models import (  # noqa: E402
    AICheck,
    PromptSnapshot,
    SeoReviewChange,
    SeoReviewRisk,
    SeoReviewRun,
    TaskRecord,
)
from services.access_control import ActorIdentity  # noqa: E402
from services.server_request_security import server_http_route_available  # noqa: E402
from storage import content_hash  # noqa: E402
from workflow_assistant.adapters import (  # noqa: E402
    WorkflowAssistantServiceAdapters,
)
from workflow_assistant.contracts import (  # noqa: E402
    PlanCommandRequest,
    PlanDraft,
    PlanStep,
)
from workflow_assistant.context import (  # noqa: E402
    AssistantKnowledgeContext,
    AssistantProjectContext,
    AssistantPromptContext,
    AssistantPublishedTopicContext,
    AssistantTaskContext,
    AssistantWorkspaceContext,
)
from workflow_assistant.execution import (  # noqa: E402
    StepExecutionResult,
    WorkflowExecutionResult,
    _available_dispatch_slots,
    _permission_for_action,
    _should_wait_for_review,
)
from workflow_assistant.graph import WorkflowAssistantGraph  # noqa: E402
from workflow_assistant.http import (  # noqa: E402
    _apply_execution_limits,
    _error,
    _merge_natural_language_revision,
    _normalize_explicit_revision_execution,
    _record_planner_usage,
)
from workflow_assistant.repository import (  # noqa: E402
    WorkflowAssistantConflict,
    WorkflowExecutionCandidate,
    WorkflowPlan,
    WorkflowPlanStep,
)
from workflow_assistant.runner import WorkflowAssistantRunner  # noqa: E402
from workflow_assistant.tools import (  # noqa: E402
    WorkflowToolError,
    WorkflowToolInvocation,
    WorkflowToolRegistry,
)
from workflow_assistant.planner import (  # noqa: E402
    PlannerModelIdentity,
    PlannerOutputError,
    StructuredWorkflowPlanner,
    _planned_action_counts,
    _planner_max_tokens,
    _requested_article_counts,
    parse_planner_output,
    request_skips_review,
)
from workflow_assistant.policy import (  # noqa: E402
    AssistantPolicyError,
    bind_plan_context,
    canonical_plan_hash,
    requires_human_gate,
    sanitize_public_summary,
    sanitize_message,
    validate_plan_scope,
)


class FakeAccess:
    def __init__(self, allowed: set[str]) -> None:
        self.allowed = allowed
        self.calls: list[tuple[str, str]] = []

    def require(self, actor: ActorIdentity, project_id: str, permission: str) -> None:
        self.calls.append((project_id, permission))
        if project_id not in self.allowed or permission != "project.view":
            raise PermissionError("denied")


class FakeCoordinator:
    def __init__(self) -> None:
        self.calls = 0

    def execute_plan(self, *, actor: ActorIdentity, plan_id: str) -> WorkflowExecutionResult:
        self.calls += 1
        status = "waiting_review" if self.calls == 1 else "succeeded"
        return WorkflowExecutionResult(
            plan_id=plan_id,
            revision=self.calls,
            results=(
                StepExecutionResult(
                    step_id="step-1",
                    status=status,
                    output_summary={},
                    error_code=(
                        "human_confirmation_required"
                        if status == "waiting_review"
                        else None
                    ),
                ),
            ),
        )


def make_plan(*, project_id: str = "project-a", action_kind: str = "list_tasks") -> PlanDraft:
    return PlanDraft(
        title="Read tasks",
        natural_language_request="List the tasks",
        project_ids=[project_id],
        steps=[
            PlanStep(
                step_id="step-1",
                sequence=1,
                action_kind=action_kind,  # type: ignore[arg-type]
                project_id=project_id,
            )
        ],
    )


class WorkflowAssistantContractTests(unittest.TestCase):
    def test_batch_quantity_parser_supports_single_project_chinese_request(self) -> None:
        self.assertEqual(
            _requested_article_counts(
                "随机选择5个YEHUI B2B话题并撰写五篇文章",
                ("yehui.com",),
            ),
            {"yehui.com": 5},
        )
        self.assertEqual(
            _planner_max_tokens({"yehui.com": 10}),
            20_000,
        )

    def test_batch_quantity_requires_a_generate_article_chain(self) -> None:
        plan = PlanDraft(
            title="Batch",
            natural_language_request="write two articles",
            project_ids=["project-a"],
            steps=[
                PlanStep(
                    step_id="research-1",
                    sequence=1,
                    action_kind="start_research",
                    project_id="project-a",
                    article_task_id="task-1",
                ),
                PlanStep(
                    step_id="article-1",
                    sequence=2,
                    action_kind="generate_article",
                    project_id="project-a",
                    article_task_id="task-1",
                    input_summary={"use_evidence_pack": True},
                ),
            ],
        )

        self.assertEqual(_planned_action_counts(plan, "generate_article"), {"project-a": 1})

    def test_natural_language_skip_review_removes_review_step_server_side(self) -> None:
        output = json.dumps(
            {
                "title": "Write article without review",
                "natural_language_request": "ignored",
                "project_ids": ["project-a"],
                "steps": [
                    {
                        "step_id": "review-1",
                        "sequence": 1,
                        "action_kind": "review",
                        "project_id": "project-a",
                        "article_task_id": "task-a",
                    },
                    {
                        "step_id": "read-1",
                        "sequence": 2,
                        "action_kind": "list_tasks",
                        "project_id": "project-a",
                    },
                ],
            }
        )
        actor = ActorIdentity("org-a", "user-a")
        plan = parse_planner_output(
            output,
            request="这篇不用复检",
            actor=actor,
            access=FakeAccess({"project-a"}),  # type: ignore[arg-type]
            accessible_project_ids=["project-a"],
        )

        self.assertTrue(request_skips_review("不用复检"))
        self.assertTrue(request_skips_review("skip SEO review"))
        self.assertFalse(request_skips_review("不要跳过复检"))
        self.assertEqual([step.action_kind for step in plan.steps], ["list_tasks"])
        self.assertEqual([step.sequence for step in plan.steps], [1])

    def test_assistant_skips_humanize_when_initial_ai_rate_is_below_threshold(self) -> None:
        article = "# Article\n\nA short initial article."
        task = TaskRecord(
            id="task-a",
            week_folder="server",
            customer="project-a",
            topic_index=1,
            topic="Article topic",
            status="draft_ready",
            task_dir="/server/task-a",
            initial_article=article,
            initial_article_hash=content_hash(article),
            initial_ai_check=AICheck(
                score=12.5,
                provider="zerogpt",
                checked_at="2026-08-25T00:00:00",
                article_hash=content_hash(article),
            ),
            created_at="2026-08-25T00:00:00",
            updated_at="2026-08-25T00:00:00",
        )

        class Store:
            def get(self, task_id: str) -> TaskRecord:
                if task_id != task.id:
                    raise KeyError(task_id)
                return task

        class Writer:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            def put(self, value: TaskRecord, **kwargs: object) -> TaskRecord:
                self.calls.append(dict(kwargs))
                value.revision += 1
                return value

        writer = Writer()
        runtime = SimpleNamespace(store=Store(), audited_writer=writer)
        factory = SimpleNamespace(create=lambda _authorized: runtime)
        humanize_service = SimpleNamespace(
            enqueue=lambda **_kwargs: (_ for _ in ()).throw(
                AssertionError("low AI-rate articles must not enqueue humanize")
            )
        )
        adapter = WorkflowAssistantServiceAdapters(
            engine=object(),  # type: ignore[arg-type]
            config=SimpleNamespace(ai_pass_threshold=30),
            task_factory=factory,  # type: ignore[arg-type]
            humanize_generation=humanize_service,
        )

        result = adapter._queue_generation(
            WorkflowToolInvocation(
                actor=ActorIdentity("org-a", "user-a"),
                plan_id="plan-a",
                step_id="humanize-1",
                action_kind="humanize",
                project_id="project-a",
                article_task_id=task.id,
                expected_task_revision=0,
                input_summary={},
                pinned_prompt_version={},
                pinned_knowledge_snapshot={},
                confirmed=True,
            )
        )

        self.assertEqual(result["_workflow_status"], "skipped")
        self.assertEqual(result["result_revision"], 1)
        self.assertTrue(result["humanization_skipped"])
        self.assertEqual(len(writer.calls), 1)
        self.assertEqual(task.status, "final_ai_checked")
        self.assertTrue(task.final_ai_check.confirmed)

    def test_natural_language_project_notes_change_is_previewed_and_executed(self) -> None:
        class NotesLlm:
            ready = True

            def __init__(self) -> None:
                self.input: dict[str, object] = {}

            def chat(self, messages, temperature=0.7, max_tokens=1800):
                del temperature, max_tokens
                self.input = json.loads(messages[1]["content"])
                return json.dumps(
                    {
                        "title": "Update project notes",
                        "natural_language_request": "ignored",
                        "project_ids": ["project-a"],
                        "steps": [
                            {
                                "step_id": "notes-1",
                                "sequence": 1,
                                "action_kind": "update_project_notes",
                                "project_id": "project-a",
                                "input_summary": {
                                    "notes_to_add": "- Prefer hybrid inverter positioning.",
                                    "notes_to_remove": [],
                                    "change_summary": "Add hybrid positioning rule",
                                },
                            }
                        ],
                    }
                )

        context = AssistantWorkspaceContext(
            projects=(
                AssistantProjectContext(
                    project_id="project-a",
                    customer_name="A",
                    official_domain="a.example",
                    project_notes="- Existing requirement.",
                    revision=7,
                    effective_role="editor",
                    tasks=(),
                    prompts=(),
                    knowledge=(),
                ),
            ),
        )
        llm = NotesLlm()
        planner = StructuredWorkflowPlanner(
            SimpleNamespace(
                workflow_assistant_max_concurrency=3,
                workflow_assistant_soft_budget_tokens=24_000,
                workflow_assistant_project_changes_enabled=True,
            ),
            access=FakeAccess({"project-a"}),  # type: ignore[arg-type]
            llm=llm,
        )

        draft = planner.plan(
            actor=ActorIdentity("org-a", "user-a"),
            request="Add a hybrid inverter positioning rule to project notes",
            context=context,
            selected_project_ids=("project-a",),
        )
        self.assertIn(
            "update_project_notes",
            llm.input["allowed_action_kinds"],  # type: ignore[operator]
        )
        bound = bind_plan_context(draft, context=context)
        summary = bound.steps[0].input_summary
        self.assertEqual(summary["previous_project_notes"], "- Existing requirement.")
        self.assertEqual(
            summary["project_notes"],
            "- Existing requirement.\n- Prefer hybrid inverter positioning.",
        )
        self.assertEqual(summary["expected_project_revision"], 7)

        class Metadata:
            def __init__(self) -> None:
                self.current = SimpleNamespace(
                    project_id="project-a",
                    customer_name="A",
                    official_domain="a.example",
                    project_notes="- Existing requirement.",
                    revision=7,
                )

            def get(self, **_: object) -> SimpleNamespace:
                return self.current

            def update(self, **kwargs: object) -> SimpleNamespace:
                self.assertion = kwargs
                return SimpleNamespace(
                    project_notes=str(kwargs["project_notes"]),
                    revision=8,
                )

        metadata = Metadata()
        adapter = WorkflowAssistantServiceAdapters(
            engine=object(),  # type: ignore[arg-type]
            config=SimpleNamespace(
                workflow_assistant_project_changes_enabled=True,
            ),
            task_factory=None,
            project_metadata=metadata,
        )
        result = adapter.handlers()["update_project_notes"](
            WorkflowToolInvocation(
                actor=ActorIdentity("org-a", "user-a"),
                plan_id="plan-1",
                step_id="notes-1",
                action_kind="update_project_notes",
                project_id="project-a",
                article_task_id=None,
                expected_task_revision=None,
                input_summary=summary,
                pinned_prompt_version={},
                pinned_knowledge_snapshot={},
                confirmed=True,
            )
        )
        self.assertTrue(result["project_notes_updated"])
        self.assertEqual(result["project_revision"], 8)
        self.assertEqual(metadata.assertion["expected_revision"], 7)
        self.assertEqual(
            metadata.assertion["project_notes"],
            "- Existing requirement.\n- Prefer hybrid inverter positioning.",
        )

    def test_planner_retries_a_contract_mismatch(self) -> None:
        class RetryContractLlm:
            ready = True

            def __init__(self) -> None:
                self.calls = 0

            def chat(self, _messages, temperature=0.7, max_tokens=1800):
                del temperature, max_tokens
                self.calls += 1
                return json.dumps(
                    {
                        "title": "Read tasks",
                        "natural_language_request": "ignored",
                        "project_ids": ["project-a"],
                        "steps": [] if self.calls == 1 else [
                            {
                                "step_id": "step-1",
                                "sequence": 1,
                                "action_kind": "list_tasks",
                                "project_id": "project-a",
                            }
                        ],
                    }
                )

        context = AssistantWorkspaceContext(
            projects=(
                AssistantProjectContext(
                    project_id="project-a",
                    customer_name="A",
                    official_domain="a.example",
                    project_notes="",
                    revision=1,
                    effective_role="editor",
                    tasks=(),
                    prompts=(),
                    knowledge=(),
                ),
            ),
        )
        llm = RetryContractLlm()
        plan = StructuredWorkflowPlanner(
            SimpleNamespace(workflow_assistant_max_concurrency=3),
            access=FakeAccess({"project-a"}),  # type: ignore[arg-type]
            llm=llm,
        ).plan(
            actor=ActorIdentity("org-a", "user-a"),
            request="list tasks",
            context=context,
            selected_project_ids=("project-a",),
        )

        self.assertEqual(llm.calls, 2)
        self.assertEqual(plan.steps[0].action_kind, "list_tasks")

    def test_project_notes_change_cannot_mix_with_article_steps(self) -> None:
        context = AssistantWorkspaceContext(
            projects=(
                AssistantProjectContext(
                    project_id="project-a",
                    customer_name="A",
                    official_domain="a.example",
                    project_notes="Existing notes",
                    revision=1,
                    effective_role="editor",
                    tasks=(),
                    prompts=(),
                    knowledge=(),
                ),
            ),
        )
        plan = PlanDraft(
            title="Invalid mixed plan",
            natural_language_request="change notes and list tasks",
            project_ids=("project-a",),
            steps=(
                PlanStep(
                    step_id="notes-1",
                    sequence=1,
                    action_kind="update_project_notes",
                    project_id="project-a",
                    input_summary={
                        "notes_to_add": "New note",
                        "notes_to_remove": [],
                    },
                ),
                PlanStep(
                    step_id="read-1",
                    sequence=2,
                    action_kind="list_tasks",
                    project_id="project-a",
                ),
            ),
        )

        with self.assertRaisesRegex(
            AssistantPolicyError,
            "cannot be combined",
        ):
            bind_plan_context(plan, context=context)

    def test_plan_keeps_running_when_gate_arrives_before_other_jobs(self) -> None:
        plan = SimpleNamespace(
            paused_project_ids=(),
            steps=(
                SimpleNamespace(
                    sequence=1,
                    project_id="project-a",
                    article_task_id="task-a",
                    status="waiting_review",
                    hard_gate=True,
                    human_gate_confirmed=False,
                ),
                SimpleNamespace(
                    sequence=2,
                    project_id="project-b",
                    article_task_id="task-b",
                    status="waiting_job",
                    hard_gate=False,
                    human_gate_confirmed=False,
                ),
            ),
        )
        self.assertFalse(_should_wait_for_review(plan))  # type: ignore[arg-type]

    def test_plan_waits_when_only_gate_blocked_chains_remain(self) -> None:
        plan = SimpleNamespace(
            paused_project_ids=(),
            steps=(
                SimpleNamespace(
                    sequence=1,
                    project_id="project-a",
                    article_task_id="task-a",
                    status="waiting_review",
                    hard_gate=True,
                    human_gate_confirmed=False,
                ),
                SimpleNamespace(
                    sequence=2,
                    project_id="project-a",
                    article_task_id="task-a",
                    status="pending",
                    hard_gate=False,
                    human_gate_confirmed=False,
                ),
            ),
        )
        self.assertTrue(_should_wait_for_review(plan))  # type: ignore[arg-type]

    def test_waiting_jobs_consume_later_dispatch_slots(self) -> None:
        plan = SimpleNamespace(
            concurrency_limit=3,
            steps=[
                SimpleNamespace(status="waiting_job"),
                SimpleNamespace(status="waiting_job"),
                SimpleNamespace(status="pending"),
            ],
        )
        self.assertEqual(
            _available_dispatch_slots(
                plan,  # type: ignore[arg-type]
                max_concurrency=3,
            ),
            1,
        )

    def test_composite_review_requires_article_edit_permission(self) -> None:
        self.assertEqual(_permission_for_action("review"), "article.edit")

    def test_review_reconciliation_applies_safe_changes_and_rejects_risks(
        self,
    ) -> None:
        article = """# Buyer Guide

This introduction explains the buying decision.

## Buyer Checks

### Confirm fit

Keep the application requirements.

### Compare evidence

Keep the supplier evidence.

## FAQ

**Q: What should buyers send?**

A: Send requirements.

**Q: When should buyers request samples?**

A: Before approval.

**Q: Why compare capability?**

A: It affects support.
""".strip()
        safe_target = "Keep the supplier evidence."
        risky_target = "Keep the application requirements."
        job_id = "job-review-a"
        review_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"seo-review\n{job_id}",
        ).hex[:12]
        current = TaskRecord(
            id="task-a",
            week_folder="server",
            customer="example.com",
            topic_index=1,
            topic="Buyer Guide",
            selected_title="Buyer Guide",
            status="draft_ready",
            task_dir="/server/task-a",
            revision=1,
            initial_article=article,
            initial_article_hash=content_hash(article),
            article=article,
            seo_reviews=[
                SeoReviewRun(
                    id=review_id,
                    source_article=article,
                    source_article_hash=content_hash(article),
                    source_revision=0,
                    score=80,
                    report="Review report",
                    changes=[
                        SeoReviewChange(
                            id="safe",
                            operation="replace",
                            title="Clarify evidence",
                            target_text=safe_target,
                            model_proposed_text=(
                                "Compare supplier evidence before approval."
                            ),
                            reviewed_text=(
                                "Compare supplier evidence before approval."
                            ),
                            source_start=article.index(safe_target),
                            source_end=(
                                article.index(safe_target) + len(safe_target)
                            ),
                        ),
                        SeoReviewChange(
                            id="risky",
                            operation="replace",
                            title="Risky brand edit",
                            target_text=risky_target,
                            model_proposed_text="Keep Acme requirements.",
                            reviewed_text="Keep Acme requirements.",
                            source_start=article.index(risky_target),
                            source_end=(
                                article.index(risky_target) + len(risky_target)
                            ),
                            risks=[
                                SeoReviewRisk(
                                    kind="brand",
                                    label="Acme",
                                    message="Brand change",
                                )
                            ],
                        ),
                    ],
                    prompt_snapshot=PromptSnapshot(
                        kind="review",
                        source="system",
                        content="rubric",
                    ),
                    created_at="2026-08-18T00:00:00+00:00",
                )
            ],
            created_at="2026-08-18T00:00:00+00:00",
            updated_at="2026-08-18T00:00:00+00:00",
        )

        class Store:
            def get(self, task_id: str) -> TaskRecord:
                self.task_id = task_id
                return current

        class Writer:
            action = ""
            details: dict[str, object] = {}

            def put(self, task: TaskRecord, **kwargs: object) -> TaskRecord:
                self.action = str(kwargs["action"])
                self.details = dict(kwargs["details"])  # type: ignore[arg-type]
                task.revision += 1
                return task

        store = Store()
        writer = Writer()
        runtime = SimpleNamespace(store=store, audited_writer=writer)
        factory = SimpleNamespace(create=lambda _request: runtime)
        adapter = WorkflowAssistantServiceAdapters(
            engine=object(),  # type: ignore[arg-type]
            config=object(),  # type: ignore[arg-type]
            task_factory=factory,  # type: ignore[arg-type]
        )

        result = adapter._apply_generated_review(
            actor=ActorIdentity("org-a", "user-a"),
            step=SimpleNamespace(project_id="project-a", article_task_id="task-a"),
            job={
                "job_id": job_id,
                "result_revision": 1,
                "status": "succeeded",
            },
        )

        self.assertEqual(result["result_revision"], 2)
        self.assertEqual(result["accepted_count"], 1)
        self.assertEqual(result["rejected_count"], 1)
        self.assertEqual(current.seo_reviews[0].status, "applied")
        self.assertEqual(current.seo_reviews[0].changes[0].decision, "accepted")
        self.assertEqual(current.seo_reviews[0].changes[1].decision, "rejected")
        self.assertIn("Compare supplier evidence", current.initial_article)
        self.assertNotIn("Acme", current.initial_article)
        self.assertEqual(writer.action, "article.seo_review.applied")
        self.assertEqual(
            set(writer.details),
            {
                "accepted_count",
                "invalid_count",
                "pending_count",
                "rejected_count",
            },
        )

    def test_project_lane_command_normalizes_ids(self) -> None:
        command = PlanCommandRequest(
            revision=3,
            project_ids=[" project-a ", "project-b"],
        )
        self.assertEqual(command.project_ids, ["project-a", "project-b"])

    def test_runner_retries_queued_project_lane_cas_race(self) -> None:
        class ConflictingCoordinator:
            def execute_plan(self, **_: object) -> None:
                raise WorkflowAssistantConflict("stale queued revision")

        class RecordingRepository:
            def __init__(self) -> None:
                self.failed = False

            def get_plan(self, **_: object) -> SimpleNamespace:
                return SimpleNamespace(status="queued")

            def append_event(self, **_: object) -> None:
                self.failed = True

            def set_plan_status(self, **_: object) -> None:
                self.failed = True

        repository = RecordingRepository()
        runner = WorkflowAssistantRunner(
            repository=repository,  # type: ignore[arg-type]
            coordinator=ConflictingCoordinator(),  # type: ignore[arg-type]
        )
        runner._run_candidate(
            WorkflowExecutionCandidate(
                organization_id="org-a",
                creator_user_id="user-a",
                plan_id="plan-a",
                status="queued",
            )
        )
        self.assertFalse(repository.failed)

    def test_only_formal_delivery_has_a_generic_second_gate(self) -> None:
        self.assertFalse(requires_human_gate("start_research"))
        self.assertTrue(requires_human_gate("package_delivery"))

    def test_explicit_revision_plan_cannot_raise_server_concurrency_ceiling(self) -> None:
        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    article_agent_config=SimpleNamespace(
                        workflow_assistant_max_concurrency=2,
                    )
                )
            )
        )
        plan = make_plan().model_copy(update={"concurrency_limit": 32})
        limited = _apply_execution_limits(request, plan)
        self.assertEqual(limited.concurrency_limit, 2)

    def test_revision_conflict_exposes_safe_current_step_projection(self) -> None:
        error = _error(
            WorkflowAssistantConflict(
                "plan revision conflict",
                code="plan_revision_conflict",
                current_revision=4,
                current_plan_hash="a" * 64,
                current_steps=[
                    {
                        "step_id": "step-1",
                        "sequence": 1,
                        "action_kind": "generate_article",
                        "project_id": "project-a",
                        "article_task_id": "task-1",
                        "status": "succeeded",
                        "input_summary": {"topic": "must not cross boundary"},
                    }
                ],
            )
        )
        self.assertEqual(error.status_code, 409)
        self.assertEqual(
            error.detail,
            {
                "error_code": "plan_revision_conflict",
                "message": "plan revision conflict",
                "current_revision": 4,
                "current_plan_hash": "a" * 64,
                "current_steps": [
                    {
                        "step_id": "step-1",
                        "sequence": 1,
                        "action_kind": "generate_article",
                        "project_id": "project-a",
                        "article_task_id": "task-1",
                        "status": "succeeded",
                    }
                ],
                "current_step_count": 1,
                "current_steps_truncated": False,
            },
        )

    def test_unknown_action_is_rejected_by_closed_contract(self) -> None:
        with self.assertRaises(ValueError):
            make_plan(action_kind="run_shell")

    def test_plan_hash_is_stable_and_changes_with_plan_content(self) -> None:
        first = make_plan()
        second = PlanDraft.model_validate(json.loads(first.model_dump_json()))
        self.assertEqual(canonical_plan_hash(first), canonical_plan_hash(second))
        changed = first.model_copy(update={"title": "Changed"})
        self.assertNotEqual(canonical_plan_hash(first), canonical_plan_hash(changed))

    def test_plan_scope_checks_every_project_and_step(self) -> None:
        actor = ActorIdentity("org-a", "user-a")
        access = FakeAccess({"project-a"})
        validate_plan_scope(
            make_plan(),
            actor=actor,
            access=access,  # type: ignore[arg-type]
            accessible_project_ids=["project-a"],
        )
        self.assertEqual(access.calls, [("project-a", "project.view")])
        with self.assertRaisesRegex(AssistantPolicyError, "inaccessible"):
            validate_plan_scope(
                make_plan(project_id="project-b"),
                actor=actor,
                access=access,  # type: ignore[arg-type]
                accessible_project_ids=["project-a"],
            )

    def test_message_sanitization_removes_controls_and_rejects_empty(self) -> None:
        self.assertEqual(sanitize_message("  hi\x00\r\nthere  "), "hi\nthere")
        with self.assertRaisesRegex(AssistantPolicyError, "required"):
            sanitize_message("\x00\t")

    def test_planner_output_replaces_model_request_and_adds_hard_gate(self) -> None:
        actor = ActorIdentity("org-a", "user-a")
        output = json.dumps(
            {
                "title": "Run\x00 article workflow",
                "natural_language_request": "model text must not win",
                "project_ids": ["project-a"],
                "steps": [
                    {
                        "step_id": "step-1",
                        "sequence": 1,
                        "action_kind": "package_delivery",
                        "project_id": "project-a",
                        "pinned_prompt_version": "model metadata must not win",
                        "pinned_knowledge_snapshot": "model metadata must not win",
                        "status": "succeeded",
                        "background_job_id": "model-job",
                        "retry_count": 9,
                        "output_summary": {"model": "must not win"},
                        "standardized_error_code": "model_error",
                        "human_gate_confirmed": True,
                    }
                ],
                "concurrency_limit": 3,
                "budget_warning": False,
                "attention_state": "none",
            }
        )
        plan = parse_planner_output(
            output,
            request="user request",
            actor=actor,
            access=FakeAccess({"project-a"}),  # type: ignore[arg-type]
            accessible_project_ids=["project-a"],
        )
        self.assertEqual(plan.natural_language_request, "user request")
        self.assertEqual(plan.title, "Run article workflow")
        self.assertTrue(plan.steps[0].hard_gate)
        self.assertEqual(plan.steps[0].pinned_prompt_version, {})
        self.assertEqual(plan.steps[0].pinned_knowledge_snapshot, {})
        self.assertEqual(plan.steps[0].status, "pending")
        self.assertIsNone(plan.steps[0].background_job_id)
        self.assertEqual(plan.steps[0].retry_count, 0)
        self.assertEqual(plan.steps[0].output_summary, {})
        self.assertIsNone(plan.steps[0].standardized_error_code)
        self.assertFalse(plan.steps[0].human_gate_confirmed)
        self.assertEqual(plan.attention_state, "user_confirmation")

    def test_planner_drops_known_non_article_ids_from_create_bindings(self) -> None:
        output = json.dumps(
            {
                "title": "Create two tasks",
                "natural_language_request": "ignored",
                "project_ids": ["project-a"],
                "steps": [
                    {
                        "step_id": "create-1",
                        "sequence": 1,
                        "action_kind": "create_task",
                        "project_id": "project-a",
                        "input_summary": {
                            "published_topic_id": "topic-1",
                            "topic": "Topic one",
                            "bind_step_ids": ["article-1", "create-2"],
                        },
                    },
                    {
                        "step_id": "article-1",
                        "sequence": 2,
                        "action_kind": "generate_article",
                        "project_id": "project-a",
                    },
                    {
                        "step_id": "create-2",
                        "sequence": 3,
                        "action_kind": "create_task",
                        "project_id": "project-a",
                        "input_summary": {
                            "published_topic_id": "topic-2",
                            "topic": "Topic two",
                        },
                    },
                ],
            }
        )

        plan = parse_planner_output(
            output,
            request="create two tasks",
            actor=ActorIdentity("org-a", "user-a"),
            access=FakeAccess({"project-a"}),  # type: ignore[arg-type]
            accessible_project_ids=["project-a"],
        )

        self.assertEqual(
            plan.steps[0].input_summary["bind_step_ids"],
            ["article-1"],
        )

    def test_plan_step_normalizes_empty_optional_task_binding(self) -> None:
        step = PlanStep.model_validate(
            {
                "step_id": "generated-task-step",
                "sequence": 1,
                "action_kind": "generate_titles",
                "project_id": "project-a",
                "article_task_id": "  ",
                "input_summary": {
                    "create_task_step_id": "create-task-step",
                },
            }
        )

        self.assertIsNone(step.article_task_id)

    def test_planner_output_flattens_create_task_summary(self) -> None:
        output = json.dumps(
            {
                "title": "Create a published topic task",
                "natural_language_request": "model text must not win",
                "project_ids": ["project-a"],
                "steps": [
                    {
                        "step_id": "create-task-step",
                        "sequence": 1,
                        "action_kind": "create_task",
                        "project_id": "project-a",
                        "article_task_id": "",
                        "input_summary": {
                            "create_task": {
                                "published_topic_id": "topic-1",
                                "topic": "Published topic",
                                "bind_step_ids": "generated-task-step, delivery-step",
                            }
                        },
                    }
                ],
                "concurrency_limit": 3,
                "budget_warning": False,
                "attention_state": "user_confirmation",
            }
        )

        plan = parse_planner_output(
            output,
            request="create one task",
            actor=ActorIdentity("org-a", "user-a"),
            access=FakeAccess({"project-a"}),  # type: ignore[arg-type]
            accessible_project_ids=["project-a"],
        )

        self.assertEqual(
            plan.steps[0].input_summary,
            {
                "published_topic_id": "topic-1",
                "topic": "Published topic",
                "bind_step_ids": ["generated-task-step", "delivery-step"],
            },
        )

    def test_planner_output_assigns_contiguous_step_sequence(self) -> None:
        output = json.dumps(
            {
                "title": "Two project reads",
                "natural_language_request": "model text must not win",
                "project_ids": ["project-a"],
                "steps": [
                    {
                        "step_id": "read-project",
                        "sequence": 1,
                        "action_kind": "read_project_context",
                        "project_id": "project-a",
                    },
                    {
                        "step_id": "read-tasks",
                        "sequence": 1,
                        "action_kind": "list_tasks",
                        "project_id": "project-a",
                    },
                ],
                "concurrency_limit": 3,
                "budget_warning": False,
                "attention_state": "none",
            }
        )

        plan = parse_planner_output(
            output,
            request="read both",
            actor=ActorIdentity("org-a", "user-a"),
            access=FakeAccess({"project-a"}),  # type: ignore[arg-type]
            accessible_project_ids=["project-a"],
        )

        self.assertEqual([step.sequence for step in plan.steps], [1, 2])

    def test_planner_retries_only_transient_provider_failures(self) -> None:
        class RetryLlm:
            ready = True

            def __init__(self) -> None:
                self.calls = 0

            def chat(self, _messages, temperature=0.7, max_tokens=1800):
                del temperature, max_tokens
                self.calls += 1
                if self.calls < 3:
                    raise RuntimeError("HTTP 503 temporarily unavailable")
                return json.dumps(
                    {
                        "title": "Read tasks",
                        "natural_language_request": "ignored",
                        "project_ids": ["project-a"],
                        "steps": [
                            {
                                "step_id": "step-1",
                                "sequence": 1,
                                "action_kind": "list_tasks",
                                "project_id": "project-a",
                            }
                        ],
                        "concurrency_limit": 3,
                    }
                )

        context = AssistantWorkspaceContext(
            projects=(
                AssistantProjectContext(
                    project_id="project-a",
                    customer_name="A",
                    official_domain="a.example",
                    project_notes="",
                    revision=1,
                    effective_role="editor",
                    tasks=(),
                    prompts=(),
                    knowledge=(),
                ),
            ),
        )
        llm = RetryLlm()
        plan = StructuredWorkflowPlanner(
            SimpleNamespace(
                workflow_assistant_max_concurrency=2,
                workflow_assistant_soft_budget_tokens=1,
            ),
            access=FakeAccess({"project-a"}),  # type: ignore[arg-type]
            llm=llm,
        ).plan(
            actor=ActorIdentity("org-a", "user-a"),
            request="list tasks",
            context=context,
            selected_project_ids=("project-a",),
        )
        self.assertEqual(llm.calls, 3)
        self.assertEqual(plan.concurrency_limit, 2)
        self.assertTrue(plan.budget_warning)

    def test_planner_resolves_actor_model_and_records_actual_identity(self) -> None:
        class ActorLlm:
            ready = True
            model = "user-selected-model"
            config = SimpleNamespace(llm_provider="main-provider")

            def chat(self, _messages, temperature=0.7, max_tokens=1800):
                del temperature, max_tokens
                return json.dumps(
                    {
                        "title": "Read tasks",
                        "natural_language_request": "ignored",
                        "project_ids": ["project-a"],
                        "steps": [
                            {
                                "step_id": "step-1",
                                "sequence": 1,
                                "action_kind": "list_tasks",
                                "project_id": "project-a",
                            }
                        ],
                    }
                )

        class ActorFactory:
            ready = True

            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []

            def client(self, organization_id: str, user_id: str) -> ActorLlm:
                self.calls.append((organization_id, user_id))
                return ActorLlm()

        context = AssistantWorkspaceContext(
            projects=(
                AssistantProjectContext(
                    project_id="project-a",
                    customer_name="A",
                    official_domain="a.example",
                    project_notes="",
                    revision=1,
                    effective_role="editor",
                    tasks=(),
                    prompts=(),
                    knowledge=(),
                ),
            ),
        )
        factory = ActorFactory()
        planner = StructuredWorkflowPlanner(
            SimpleNamespace(workflow_assistant_max_concurrency=3),
            access=FakeAccess({"project-a"}),  # type: ignore[arg-type]
            llm_factory=factory,
        )

        planner.plan(
            actor=ActorIdentity("org-a", "user-a"),
            request="list tasks",
            context=context,
            selected_project_ids=("project-a",),
        )

        self.assertEqual(factory.calls, [("org-a", "user-a")])
        self.assertEqual(
            planner.consume_model_identity(),
            PlannerModelIdentity("main-provider", "user-selected-model"),
        )
        self.assertIsNone(planner.consume_model_identity())

    def test_usage_ledger_uses_actor_selected_model_identity(self) -> None:
        class RecordingUsageRepository:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            def record_usage(self, **kwargs) -> None:
                self.calls.append(kwargs)

        repository = RecordingUsageRepository()
        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    article_agent_config=SimpleNamespace(
                        llm_provider="default-provider",
                        llm_model="default-model",
                    )
                )
            )
        )
        plan = SimpleNamespace(
            project_ids=("project-a",),
            plan_id="plan-a",
        )

        with patch(
            "workflow_assistant.http._repository",
            return_value=repository,
        ):
            _record_planner_usage(
                request,  # type: ignore[arg-type]
                actor=ActorIdentity("org-a", "user-a"),
                plan=plan,  # type: ignore[arg-type]
                request_id="request-12345678",
                input_tokens=10,
                output_tokens=5,
                model_identity=PlannerModelIdentity(
                    "main-provider",
                    "user-selected-model",
                ),
            )

        self.assertEqual(repository.calls[0]["provider"], "main-provider")
        self.assertEqual(repository.calls[0]["model"], "user-selected-model")

    def test_planner_retries_an_explicit_article_quantity_mismatch(self) -> None:
        class QuantityLlm:
            ready = True

            def __init__(self) -> None:
                self.calls = 0

            def chat(self, _messages, temperature=0.7, max_tokens=1800):
                del temperature, max_tokens
                self.calls += 1
                count = 1 if self.calls == 1 else 2
                steps = []
                sequence = 0
                for index in range(1, count + 1):
                    create_step_id = f"create-{index}"
                    actions = (
                        "generate_titles",
                        "select_title",
                        "generate_products",
                        "confirm_products",
                        "generate_outline",
                        "start_research",
                        "generate_article",
                        "review",
                    )
                    step_ids = [f"{action}-{index}" for action in actions]
                    steps.append(
                        {
                            "step_id": create_step_id,
                            "sequence": sequence + 1,
                            "action_kind": "create_task",
                            "project_id": "project-a",
                            "input_summary": {
                                "published_topic_id": f"topic-{index}",
                                "topic": f"Topic {index}",
                                "bind_step_ids": step_ids,
                            },
                        }
                    )
                    for offset, (action, step_id) in enumerate(
                        zip(actions, step_ids, strict=True),
                        start=2,
                    ):
                        steps.append(
                            {
                                "step_id": step_id,
                                "sequence": sequence + offset,
                                "action_kind": action,
                                "project_id": "project-a",
                                "input_summary": {
                                    "create_task_step_id": create_step_id,
                                    **(
                                        {"use_evidence_pack": True}
                                        if action == "generate_article"
                                        else {}
                                    ),
                                },
                            }
                        )
                    sequence += 1 + len(actions)
                return json.dumps(
                    {
                        "title": "Create requested articles",
                        "natural_language_request": "ignored",
                        "project_ids": ["project-a"],
                        "steps": steps,
                    }
                )

        context = AssistantWorkspaceContext(
            projects=(
                AssistantProjectContext(
                    project_id="project-a",
                    customer_name="A",
                    official_domain="a.example",
                    project_notes="",
                    revision=1,
                    effective_role="editor",
                    tasks=(),
                    prompts=(),
                    knowledge=(),
                ),
            ),
        )
        llm = QuantityLlm()

        plan = StructuredWorkflowPlanner(
            SimpleNamespace(workflow_assistant_max_concurrency=3),
            access=FakeAccess({"project-a"}),  # type: ignore[arg-type]
            llm=llm,
        ).plan(
            actor=ActorIdentity("org-a", "user-a"),
            request="two articles per project",
            context=context,
            selected_project_ids=("project-a",),
        )

        self.assertEqual(llm.calls, 2)
        self.assertEqual(len(plan.steps), 18)
        self.assertEqual(_planned_action_counts(plan, "generate_article"), {"project-a": 2})

    def test_planner_repairs_missing_product_and_tdk_delivery_steps(self) -> None:
        class DeliveryLlm:
            ready = True

            def __init__(self) -> None:
                self.calls = 0

            def chat(self, _messages, temperature=0.7, max_tokens=1800):
                del temperature, max_tokens
                self.calls += 1
                actions = (
                    (
                        "generate_titles",
                        "select_title",
                        "generate_outline",
                        "start_research",
                        "generate_article",
                        "humanize",
                        "restore_links",
                        "prepare_images",
                        "export_docx",
                        "package_delivery",
                    )
                    if self.calls == 1
                    else (
                        "generate_titles",
                        "select_title",
                        "generate_products",
                        "confirm_products",
                        "generate_outline",
                        "start_research",
                        "generate_article",
                        "humanize",
                        "restore_links",
                        "prepare_images",
                        "export_docx",
                        "generate_tdk",
                        "package_delivery",
                    )
                )
                return json.dumps(
                    {
                        "title": "Write and package one article",
                        "natural_language_request": "ignored",
                        "project_ids": ["project-a"],
                        "steps": [
                            {
                                "step_id": f"{action}-1",
                                "sequence": sequence,
                                "action_kind": action,
                                "project_id": "project-a",
                                "article_task_id": "task-1",
                                "input_summary": (
                                    {"use_evidence_pack": True}
                                    if action == "generate_article"
                                    else {}
                                ),
                            }
                            for sequence, action in enumerate(actions, start=1)
                        ],
                    }
                )

        context = AssistantWorkspaceContext(
            projects=(
                AssistantProjectContext(
                    project_id="project-a",
                    customer_name="A",
                    official_domain="a.example",
                    project_notes="",
                    revision=1,
                    effective_role="editor",
                    tasks=(
                        AssistantTaskContext(
                            task_id="task-1",
                            topic="Topic",
                            primary_keyword="keyword",
                            competitor_keyword="",
                            status="new",
                            revision=0,
                            selected_title=None,
                        ),
                    ),
                    prompts=(),
                    knowledge=(),
                ),
            ),
        )
        llm = DeliveryLlm()

        plan = StructuredWorkflowPlanner(
            SimpleNamespace(workflow_assistant_max_concurrency=3),
            access=FakeAccess({"project-a"}),  # type: ignore[arg-type]
            llm=llm,
        ).plan(
            actor=ActorIdentity("org-a", "user-a"),
            request="生成一篇文章并打包下载，从头开始，跳过复检",
            context=context,
            selected_project_ids=("project-a",),
            selected_task_ids=("task-1",),
        )

        actions = [step.action_kind for step in plan.steps]
        self.assertEqual(llm.calls, 2)
        self.assertIn("generate_products", actions)
        self.assertIn("confirm_products", actions)
        self.assertIn("generate_tdk", actions)
        self.assertNotIn("review", actions)

    def test_planner_requires_review_for_new_article_without_explicit_skip(self) -> None:
        class ReviewLlm:
            ready = True

            def __init__(self) -> None:
                self.calls = 0

            def chat(self, _messages, temperature=0.7, max_tokens=1800):
                del temperature, max_tokens
                self.calls += 1
                actions = [
                    "generate_titles",
                    "select_title",
                    "generate_products",
                    "confirm_products",
                    "generate_outline",
                    "start_research",
                    "generate_article",
                ]
                if self.calls > 1:
                    actions.append("review")
                return json.dumps(
                    {
                        "title": "Write one article",
                        "natural_language_request": "ignored",
                        "project_ids": ["project-a"],
                        "steps": [
                            {
                                "step_id": f"{action}-1",
                                "sequence": sequence,
                                "action_kind": action,
                                "project_id": "project-a",
                                "article_task_id": "task-1",
                                "input_summary": (
                                    {"use_evidence_pack": True}
                                    if action == "generate_article"
                                    else {}
                                ),
                            }
                            for sequence, action in enumerate(actions, start=1)
                        ],
                    }
                )

        context = AssistantWorkspaceContext(
            projects=(
                AssistantProjectContext(
                    project_id="project-a",
                    customer_name="A",
                    official_domain="a.example",
                    project_notes="",
                    revision=1,
                    effective_role="editor",
                    tasks=(
                        AssistantTaskContext(
                            task_id="task-1",
                            topic="Topic",
                            primary_keyword="keyword",
                            competitor_keyword="",
                            status="new",
                            revision=0,
                            selected_title=None,
                        ),
                    ),
                    prompts=(),
                    knowledge=(),
                ),
            ),
        )
        llm = ReviewLlm()

        plan = StructuredWorkflowPlanner(
            SimpleNamespace(workflow_assistant_max_concurrency=3),
            access=FakeAccess({"project-a"}),  # type: ignore[arg-type]
            llm=llm,
        ).plan(
            actor=ActorIdentity("org-a", "user-a"),
            request="生成一篇文章",
            context=context,
            selected_project_ids=("project-a",),
            selected_task_ids=("task-1",),
        )

        self.assertEqual(llm.calls, 2)
        self.assertEqual(plan.steps[-1].action_kind, "review")

    def test_planner_receives_and_validates_selected_article_range(self) -> None:
        class CaptureLlm:
            ready = True

            def __init__(self) -> None:
                self.payload: dict[str, object] | None = None

            def chat(self, messages, temperature=0.7, max_tokens=1800):
                del temperature, max_tokens
                self.payload = json.loads(messages[1]["content"])
                return json.dumps(
                    {
                        "title": "Read tasks",
                        "natural_language_request": "ignored",
                        "project_ids": ["project-a"],
                        "steps": [
                            {
                                "step_id": "step-1",
                                "sequence": 1,
                                "action_kind": "list_tasks",
                                "project_id": "project-a",
                            }
                        ],
                    }
                )

        context = AssistantWorkspaceContext(
            projects=(
                AssistantProjectContext(
                    project_id="project-a",
                    customer_name="A",
                    official_domain="a.example",
                    project_notes="",
                    revision=1,
                    effective_role="editor",
                    tasks=(
                        AssistantTaskContext(
                            task_id="task-1",
                            topic="Topic",
                            primary_keyword="keyword",
                            competitor_keyword="competitor",
                            status="new",
                            revision=0,
                            selected_title=None,
                        ),
                        AssistantTaskContext(
                            task_id="task-completed",
                            topic="Completed Topic",
                            primary_keyword="completed keyword",
                            competitor_keyword="",
                            status="docx_exported",
                            revision=9,
                            selected_title="Completed Topic",
                        ),
                        AssistantTaskContext(
                            task_id="task-manual-completed",
                            topic="Manually completed topic",
                            primary_keyword="manual keyword",
                            competitor_keyword="",
                            status="new",
                            revision=3,
                            selected_title=None,
                            manual_completed=True,
                        ),
                    ),
                    prompts=(),
                    knowledge=(),
                ),
            ),
        )
        llm = CaptureLlm()
        planner = StructuredWorkflowPlanner(
            SimpleNamespace(workflow_assistant_max_concurrency=3),
            access=FakeAccess({"project-a"}),  # type: ignore[arg-type]
            llm=llm,
        )
        planner.plan(
            actor=ActorIdentity("org-a", "user-a"),
            request="read the selected task",
            context=context,
            selected_project_ids=("project-a",),
            selected_task_ids=("task-1",),
        )
        self.assertEqual(llm.payload["selected_article_task_ids"], ["task-1"])
        selection_policy = llm.payload["task_selection_policy"]
        self.assertEqual(
            selection_policy["recommended_task_ids_by_project"],
            {"project-a": ["task-1"]},
        )
        task_summaries = llm.payload["context"][0]["tasks"]
        manual_summary = next(
            item
            for item in task_summaries
            if item["task_id"] == "task-manual-completed"
        )
        self.assertTrue(manual_summary["manual_completed"])
        with self.assertRaisesRegex(PlannerOutputError, "outside"):
            planner.plan(
                actor=ActorIdentity("org-a", "user-a"),
                request="read the selected task",
                context=context,
                selected_project_ids=("project-a",),
                selected_task_ids=("missing-task",),
            )

    def test_context_binding_replaces_untrusted_snapshot_and_pins_task_revision(self) -> None:
        context = AssistantWorkspaceContext(
            projects=(
                AssistantProjectContext(
                    project_id="project-a",
                    customer_name="A",
                    official_domain="a.example",
                    project_notes="",
                    revision=7,
                    effective_role="editor",
                    tasks=(
                        AssistantTaskContext(
                            task_id="task-1",
                            topic="Topic",
                            primary_keyword="keyword",
                            competitor_keyword="competitor",
                            status="not_started",
                            revision=4,
                            selected_title=None,
                        ),
                    ),
                    prompts=(AssistantPromptContext("article", "prompt-1", 3),),
                    knowledge=(AssistantKnowledgeContext("source-1", "Source", "knowledge_page", "hard_fact", "snap-1"),),
                ),
            ),
        )
        plan = make_plan().model_copy(
            update={
                "steps": [
                    PlanStep(
                        step_id="research-1",
                        sequence=1,
                        action_kind="start_research",
                        project_id="project-a",
                        article_task_id="task-1",
                    ),
                    PlanStep(
                        step_id="step-1",
                        sequence=2,
                        action_kind="generate_article",
                        project_id="project-a",
                        article_task_id="task-1",
                        pinned_prompt_version={"prompt_id": "attacker"},
                        pinned_knowledge_snapshot={"snapshot_id": "attacker"},
                    ),
                ],
            }
        )
        bound = bind_plan_context(plan, context=context)
        self.assertEqual(bound.steps[1].expected_task_revision, 4)
        self.assertEqual(bound.steps[1].pinned_prompt_version["prompts"][0]["version"], 3)
        self.assertEqual(bound.steps[1].pinned_knowledge_snapshot["sources"][0]["snapshot_id"], "snap-1")
        self.assertTrue(bound.steps[1].input_summary["use_evidence_pack"])

    def test_completed_tasks_require_explicit_server_side_selection(self) -> None:
        context = AssistantWorkspaceContext(
            projects=(
                AssistantProjectContext(
                    project_id="project-a",
                    customer_name="A",
                    official_domain="a.example",
                    project_notes="",
                    revision=1,
                    effective_role="editor",
                    tasks=(
                        AssistantTaskContext(
                            task_id="task-manual",
                            topic="Manual",
                            primary_keyword="manual",
                            competitor_keyword="",
                            status="new",
                            revision=1,
                            selected_title=None,
                            manual_completed=True,
                        ),
                        AssistantTaskContext(
                            task_id="task-exported",
                            topic="Exported",
                            primary_keyword="exported",
                            competitor_keyword="",
                            status="docx_exported",
                            revision=2,
                            selected_title="Exported",
                        ),
                        AssistantTaskContext(
                            task_id="task-completed",
                            topic="Completed",
                            primary_keyword="completed",
                            competitor_keyword="",
                            status="completed",
                            revision=3,
                            selected_title="Completed",
                        ),
                        AssistantTaskContext(
                            task_id="task-delivered",
                            topic="Delivered",
                            primary_keyword="delivered",
                            competitor_keyword="",
                            status="delivered",
                            revision=4,
                            selected_title="Delivered",
                        ),
                    ),
                    prompts=(),
                    knowledge=(),
                ),
            ),
        )

        def research_plan(task_id: str) -> PlanDraft:
            return PlanDraft(
                title="Research selected task",
                natural_language_request="research",
                project_ids=["project-a"],
                steps=[
                    PlanStep(
                        step_id="research-1",
                        sequence=1,
                        action_kind="start_research",
                        project_id="project-a",
                        article_task_id=task_id,
                    )
                ],
            )

        for task_id in (
            "task-manual",
            "task-exported",
            "task-completed",
            "task-delivered",
        ):
            with self.subTest(task_id=task_id):
                with self.assertRaisesRegex(AssistantPolicyError, "explicitly selected"):
                    bind_plan_context(research_plan(task_id), context=context)

        selected = bind_plan_context(
            research_plan("task-manual"),
            context=context,
            selected_task_ids=["task-manual"],
        )
        self.assertEqual(selected.steps[0].expected_task_revision, 1)

    def test_article_generation_requires_research_evidence_pack(self) -> None:
        context = AssistantWorkspaceContext(
            projects=(
                AssistantProjectContext(
                    project_id="project-a",
                    customer_name="A",
                    official_domain="a.example",
                    project_notes="",
                    revision=1,
                    effective_role="editor",
                    tasks=(
                        AssistantTaskContext(
                            task_id="task-1",
                            topic="Topic",
                            primary_keyword="keyword",
                            competitor_keyword="",
                            status="new",
                            revision=0,
                            selected_title=None,
                        ),
                    ),
                    prompts=(),
                    knowledge=(),
                ),
            ),
        )

        without_research = PlanDraft(
            title="Write article",
            natural_language_request="write",
            project_ids=["project-a"],
            steps=[
                PlanStep(
                    step_id="article-1",
                    sequence=1,
                    action_kind="generate_article",
                    project_id="project-a",
                    article_task_id="task-1",
                )
            ],
        )
        with self.assertRaisesRegex(AssistantPolicyError, "earlier research"):
            bind_plan_context(without_research, context=context)

        researched = PlanDraft(
            title="Research and write",
            natural_language_request="research and write",
            project_ids=["project-a"],
            steps=[
                PlanStep(
                    step_id="research-1",
                    sequence=1,
                    action_kind="start_research",
                    project_id="project-a",
                    article_task_id="task-1",
                ),
                PlanStep(
                    step_id="article-1",
                    sequence=2,
                    action_kind="generate_article",
                    project_id="project-a",
                    article_task_id="task-1",
                    input_summary={"use_evidence_pack": False},
                ),
            ],
        )
        with self.assertRaisesRegex(AssistantPolicyError, "cannot disable"):
            bind_plan_context(researched, context=context)

        allowed = researched.model_copy(
            update={
                "steps": [
                    researched.steps[0],
                    researched.steps[1].model_copy(update={"input_summary": {}}),
                ]
            }
        )
        bound = bind_plan_context(allowed, context=context)
        self.assertIs(bound.steps[1].input_summary["use_evidence_pack"], True)

    def test_context_binding_rejects_private_plan_summary_fields(self) -> None:
        context = AssistantWorkspaceContext(
            projects=(
                AssistantProjectContext(
                    project_id="project-a",
                    customer_name="A",
                    official_domain="a.example",
                    project_notes="",
                    revision=1,
                    effective_role="editor",
                    tasks=(),
                    prompts=(),
                    knowledge=(),
                ),
            ),
        )
        with self.assertRaisesRegex(AssistantPolicyError, "private"):
            bind_plan_context(
                make_plan().model_copy(
                    update={
                        "steps": [
                            PlanStep(
                                step_id="step-1",
                                sequence=1,
                                action_kind="list_tasks",
                                project_id="project-a",
                                input_summary={"apiKey": "do-not-store"},
                            ),
                        ],
                    }
                ),
                context=context,
            )

    def test_context_binding_requires_task_identity_for_article_steps(self) -> None:
        context = AssistantWorkspaceContext(
            projects=(
                AssistantProjectContext(
                    project_id="project-a",
                    customer_name="A",
                    official_domain="a.example",
                    project_notes="",
                    revision=1,
                    effective_role="editor",
                    tasks=(),
                    prompts=(),
                    knowledge=(),
                ),
            ),
        )
        with self.assertRaisesRegex(AssistantPolicyError, "bind an article task"):
            bind_plan_context(
                make_plan(action_kind="generate_article"),
                context=context,
            )

    def test_context_binding_supports_server_allocated_task_suffix(self) -> None:
        context = AssistantWorkspaceContext(
            projects=(
                AssistantProjectContext(
                    project_id="project-a",
                    customer_name="A",
                    official_domain="a.example",
                    project_notes="",
                    revision=1,
                    effective_role="editor",
                    tasks=(),
                    prompts=(),
                    knowledge=(),
                    published_topics=(
                        AssistantPublishedTopicContext(
                            topic_id="topic-1",
                            topic="New topic",
                            primary_keyword="new keyword",
                            competitor_keyword="",
                        ),
                    ),
                ),
            ),
        )
        plan = PlanDraft(
            title="Create and write",
            natural_language_request="create a task and write it",
            project_ids=["project-a"],
            steps=[
                PlanStep(
                    step_id="create-1",
                    sequence=1,
                    action_kind="create_task",
                    project_id="project-a",
                    input_summary={
                        "published_topic_id": "topic-1",
                        "topic": "New topic",
                        "bind_step_ids": ["research-1", "article-1"],
                    },
                ),
                PlanStep(
                    step_id="research-1",
                    sequence=2,
                    action_kind="start_research",
                    project_id="project-a",
                ),
                PlanStep(
                    step_id="article-1",
                    sequence=3,
                    action_kind="generate_article",
                    project_id="project-a",
                ),
            ],
        )
        bound = bind_plan_context(plan, context=context)
        self.assertEqual(
            bound.steps[2].input_summary["create_task_step_id"],
            "create-1",
        )
        self.assertEqual(
            bound.steps[1].input_summary["create_task_step_id"],
            "create-1",
        )
        self.assertIsNone(bound.steps[2].article_task_id)

    def test_revision_preserves_completed_create_with_allocated_task_targets(
        self,
    ) -> None:
        context = AssistantWorkspaceContext(
            projects=(
                AssistantProjectContext(
                    project_id="project-a",
                    customer_name="A",
                    official_domain="a.example",
                    project_notes="",
                    revision=2,
                    effective_role="editor",
                    tasks=(
                        AssistantTaskContext(
                            task_id="task-created",
                            topic="New topic",
                            primary_keyword="new keyword",
                            competitor_keyword="",
                            status="draft_ready",
                            revision=4,
                            selected_title="New topic",
                        ),
                    ),
                    prompts=(),
                    knowledge=(),
                    published_topics=(
                        AssistantPublishedTopicContext(
                            topic_id="topic-1",
                            topic="New topic",
                            primary_keyword="new keyword",
                            competitor_keyword="",
                        ),
                    ),
                ),
            ),
        )
        completed_create = PlanStep(
            step_id="create-1",
            sequence=1,
            action_kind="create_task",
            project_id="project-a",
            input_summary={
                "published_topic_id": "topic-1",
                "topic": "New topic",
                "bind_step_ids": ["research-1", "article-1"],
            },
            status="succeeded",
            output_summary={"task_ids": ["task-created"]},
        )
        plan = PlanDraft(
            title="Retry article",
            natural_language_request="retry",
            project_ids=["project-a"],
            steps=[
                completed_create,
                PlanStep(
                    step_id="research-1",
                    sequence=2,
                    action_kind="start_research",
                    project_id="project-a",
                    article_task_id="task-created",
                    status="succeeded",
                ),
                PlanStep(
                    step_id="article-1",
                    sequence=3,
                    action_kind="generate_article",
                    project_id="project-a",
                    article_task_id="task-created",
                    status="failed",
                ),
            ],
        )

        bound = bind_plan_context(plan, context=context)

        self.assertEqual(bound.steps[0], completed_create)
        self.assertEqual(bound.steps[1].status, "succeeded")
        self.assertEqual(bound.steps[2].status, "pending")
        self.assertEqual(bound.steps[2].expected_task_revision, 4)

    def test_context_binding_rejects_duplicate_actions_in_one_new_task_chain(
        self,
    ) -> None:
        context = AssistantWorkspaceContext(
            projects=(
                AssistantProjectContext(
                    project_id="project-a",
                    customer_name="A",
                    official_domain="a.example",
                    project_notes="",
                    revision=1,
                    effective_role="editor",
                    tasks=(),
                    prompts=(),
                    knowledge=(),
                    published_topics=(
                        AssistantPublishedTopicContext(
                            topic_id="topic-1",
                            topic="New topic",
                            primary_keyword="new keyword",
                            competitor_keyword="",
                        ),
                    ),
                ),
            ),
        )
        plan = PlanDraft(
            title="Duplicate package",
            natural_language_request="create and package",
            project_ids=["project-a"],
            steps=[
                PlanStep(
                    step_id="create-1",
                    sequence=1,
                    action_kind="create_task",
                    project_id="project-a",
                    input_summary={
                        "published_topic_id": "topic-1",
                        "topic": "New topic",
                        "bind_step_ids": ["package-1", "package-2"],
                    },
                ),
                PlanStep(
                    step_id="package-1",
                    sequence=2,
                    action_kind="package_delivery",
                    project_id="project-a",
                ),
                PlanStep(
                    step_id="package-2",
                    sequence=3,
                    action_kind="package_delivery",
                    project_id="project-a",
                ),
            ],
        )

        with self.assertRaisesRegex(AssistantPolicyError, "duplicate action"):
            bind_plan_context(plan, context=context)

    def test_context_binding_does_not_add_tasks_to_an_explicit_article_range(self) -> None:
        context = AssistantWorkspaceContext(
            projects=(
                AssistantProjectContext(
                    project_id="project-a",
                    customer_name="A",
                    official_domain="a.example",
                    project_notes="",
                    revision=1,
                    effective_role="editor",
                    tasks=(),
                    prompts=(),
                    knowledge=(),
                ),
            ),
        )
        plan = PlanDraft(
            title="Create task",
            natural_language_request="create one",
            project_ids=["project-a"],
            steps=[
                PlanStep(
                    step_id="create-1",
                    sequence=1,
                    action_kind="create_task",
                    project_id="project-a",
                    input_summary={"topic": "New topic"},
                )
            ],
        )
        with self.assertRaisesRegex(AssistantPolicyError, "selected task range"):
            bind_plan_context(plan, context=context, selected_task_ids=[])

    def test_context_binding_rejects_duplicate_article_intent(self) -> None:
        context = AssistantWorkspaceContext(
            projects=(
                AssistantProjectContext(
                    project_id="project-a",
                    customer_name="A",
                    official_domain="a.example",
                    project_notes="",
                    revision=1,
                    effective_role="editor",
                    tasks=(
                        AssistantTaskContext(
                            task_id="task-1",
                            topic="Same topic",
                            primary_keyword="same keyword",
                            competitor_keyword="",
                            status="new",
                            revision=0,
                            selected_title=None,
                        ),
                        AssistantTaskContext(
                            task_id="task-2",
                            topic="Same topic",
                            primary_keyword="other keyword",
                            competitor_keyword="",
                            status="new",
                            revision=0,
                            selected_title=None,
                        ),
                    ),
                    prompts=(),
                    knowledge=(),
                ),
            ),
        )
        plan = PlanDraft(
            title="Duplicate topics",
            natural_language_request="write both",
            project_ids=["project-a"],
            steps=[
                PlanStep(
                    step_id="article-1",
                    sequence=1,
                    action_kind="generate_article",
                    project_id="project-a",
                    article_task_id="task-1",
                ),
                PlanStep(
                    step_id="article-2",
                    sequence=2,
                    action_kind="generate_article",
                    project_id="project-a",
                    article_task_id="task-2",
                ),
            ],
        )
        with self.assertRaisesRegex(AssistantPolicyError, "duplicate topics"):
            bind_plan_context(plan, context=context)

    def test_context_binding_rejects_undeclared_created_task_source(self) -> None:
        context = AssistantWorkspaceContext(
            projects=(
                AssistantProjectContext(
                    project_id="project-a",
                    customer_name="A",
                    official_domain="a.example",
                    project_notes="",
                    revision=1,
                    effective_role="editor",
                    tasks=(),
                    prompts=(),
                    knowledge=(),
                    published_topics=(
                        AssistantPublishedTopicContext(
                            topic_id="topic-1",
                            topic="New topic",
                            primary_keyword="new keyword",
                            competitor_keyword="",
                        ),
                    ),
                ),
            ),
        )
        plan = PlanDraft(
            title="Create and write",
            natural_language_request="create a task and write it",
            project_ids=["project-a"],
            steps=[
                PlanStep(
                    step_id="create-1",
                    sequence=1,
                    action_kind="create_task",
                    project_id="project-a",
                    input_summary={
                        "published_topic_id": "topic-1",
                        "topic": "New topic",
                    },
                ),
                PlanStep(
                    step_id="article-1",
                    sequence=2,
                    action_kind="generate_article",
                    project_id="project-a",
                    input_summary={"create_task_step_id": "create-1"},
                ),
            ],
        )
        with self.assertRaisesRegex(AssistantPolicyError, "explicitly bound"):
            bind_plan_context(plan, context=context)

    def test_context_binding_detects_duplicate_unicode_topics(self) -> None:
        context = AssistantWorkspaceContext(
            projects=(
                AssistantProjectContext(
                    project_id="project-a",
                    customer_name="A",
                    official_domain="a.example",
                    project_notes="",
                    revision=1,
                    effective_role="editor",
                    tasks=(),
                    prompts=(),
                    knowledge=(),
                    published_topics=(
                        AssistantPublishedTopicContext(
                            topic_id="topic-1",
                            topic="工业机器人",
                            primary_keyword="工业机器人",
                            competitor_keyword="",
                        ),
                        AssistantPublishedTopicContext(
                            topic_id="topic-2",
                            topic="工业机器人",
                            primary_keyword="机器人采购",
                            competitor_keyword="",
                        ),
                    ),
                ),
            ),
        )
        plan = PlanDraft(
            title="Duplicate topics",
            natural_language_request="create two topics",
            project_ids=["project-a"],
            steps=[
                PlanStep(
                    step_id="create-1",
                    sequence=1,
                    action_kind="create_task",
                    project_id="project-a",
                    input_summary={
                        "published_topic_id": "topic-1",
                        "topic": "工业机器人",
                    },
                ),
                PlanStep(
                    step_id="create-2",
                    sequence=2,
                    action_kind="create_task",
                    project_id="project-a",
                    input_summary={
                        "published_topic_id": "topic-2",
                        "topic": "工业机器人",
                    },
                ),
            ],
        )
        with self.assertRaisesRegex(AssistantPolicyError, "duplicate topics"):
            bind_plan_context(plan, context=context)

    def test_context_binding_rejects_private_summary_key_variants(self) -> None:
        context = AssistantWorkspaceContext(
            projects=(
                AssistantProjectContext(
                    project_id="project-a",
                    customer_name="A",
                    official_domain="a.example",
                    project_notes="",
                    revision=1,
                    effective_role="editor",
                    tasks=(),
                    prompts=(),
                    knowledge=(),
                ),
            ),
        )
        with self.assertRaisesRegex(AssistantPolicyError, "private"):
            bind_plan_context(
                make_plan().model_copy(
                    update={
                        "steps": [
                            PlanStep(
                                step_id="step-1",
                                sequence=1,
                                action_kind="list_tasks",
                                project_id="project-a",
                                input_summary={"prompt_text": "do-not-store"},
                            ),
                        ],
                    }
                ),
                context=context,
            )

    def test_context_binding_preserves_safe_list_summary_values(self) -> None:
        context = AssistantWorkspaceContext(
            projects=(
                AssistantProjectContext(
                    project_id="project-a",
                    customer_name="A",
                    official_domain="a.example",
                    project_notes="",
                    revision=1,
                    effective_role="editor",
                    tasks=(),
                    prompts=(),
                    knowledge=(),
                ),
            ),
        )
        bound = bind_plan_context(
            make_plan().model_copy(
                update={
                    "steps": [
                        PlanStep(
                            step_id="step-1",
                            sequence=1,
                            action_kind="list_tasks",
                            project_id="project-a",
                            input_summary={"filters": ["not_started", "draft"]},
                        ),
                    ],
                }
            ),
            context=context,
        )
        self.assertEqual(
            bound.steps[0].input_summary,
            {"filters": ["not_started", "draft"]},
        )

    def test_assistant_routes_are_in_server_allow_list(self) -> None:
        self.assertTrue(
            server_http_route_available(
                "POST",
                "/api/workflow-assistant/conversations",
            )
        )
        self.assertTrue(
            server_http_route_available(
                "GET",
                "/api/workflow-assistant/plans/plan-1/events/stream",
            )
        )
        self.assertTrue(
            server_http_route_available(
                "POST",
                "/api/workflow-assistant/plans/plan-1/retry",
            )
        )
        self.assertFalse(
            server_http_route_available(
                "DELETE",
                "/api/workflow-assistant/conversations/conversation-1",
            )
        )

    def test_graph_checkpoints_human_gate_without_private_state(self) -> None:
        from langgraph.checkpoint.memory import InMemorySaver
        from langgraph.types import Command

        coordinator = FakeCoordinator()
        graph = WorkflowAssistantGraph(coordinator).compile(
            checkpointer=InMemorySaver()
        )
        config = {"configurable": {"thread_id": "workflow-plan-1"}}
        first = graph.invoke(
            {
                "plan_id": "plan-1",
                "organization_id": "org-a",
                "user_id": "user-a",
            },
            config=config,
        )
        self.assertTrue(first["waiting_for_review"])
        self.assertEqual(coordinator.calls, 1)
        resumed = graph.invoke(Command(resume={"approved": True}), config=config)
        self.assertFalse(resumed["waiting_for_review"])
        self.assertEqual(resumed["results"][0]["status"], "succeeded")
        self.assertEqual(coordinator.calls, 2)

    def test_graph_does_not_treat_a_rejected_review_as_approval(self) -> None:
        from langgraph.checkpoint.memory import InMemorySaver
        from langgraph.types import Command

        coordinator = FakeCoordinator()
        graph = WorkflowAssistantGraph(coordinator).compile(
            checkpointer=InMemorySaver()
        )
        config = {"configurable": {"thread_id": "workflow-plan-rejected"}}
        first = graph.invoke(
            {
                "plan_id": "plan-1",
                "organization_id": "org-a",
                "user_id": "user-a",
            },
            config=config,
        )
        self.assertTrue(first["waiting_for_review"])

        rejected = graph.invoke(
            Command(resume={"approved": False}),
            config=config,
        )

        self.assertTrue(rejected["waiting_for_review"])
        self.assertEqual(coordinator.calls, 1)

    def test_graph_without_checkpointer_returns_wait_state_once(self) -> None:
        coordinator = FakeCoordinator()
        graph = WorkflowAssistantGraph(coordinator).compile()
        result = graph.invoke(
            {
                "plan_id": "plan-1",
                "organization_id": "org-a",
                "user_id": "user-a",
            }
        )
        self.assertTrue(result["waiting_for_review"])
        self.assertEqual(coordinator.calls, 1)

    def test_tool_registry_rejects_private_public_output(self) -> None:
        registry = WorkflowToolRegistry(
            access=FakeAccess({"project-a"}),
            handlers={
                "read_project_context": lambda _invocation: {
                    "apiKey": "must-not-persist",
                },
            },
        )
        with self.assertRaises(WorkflowToolError):
            registry.invoke(
                WorkflowToolInvocation(
                    actor=ActorIdentity("org-a", "user-a"),
                    plan_id="plan-1",
                    step_id="step-1",
                    action_kind="read_project_context",
                    project_id="project-a",
                    article_task_id=None,
                    expected_task_revision=None,
                    input_summary={},
                    pinned_prompt_version={},
                    pinned_knowledge_snapshot={},
                )
            )

    def test_public_output_allows_empty_projection_fields(self) -> None:
        self.assertEqual(
            sanitize_public_summary(
                {
                    "project_notes": "",
                    "confirmed_products": [
                        {"description": "", "canonical_url": None}
                    ],
                    "job": {"batch_id": ""},
                }
            ),
            {
                "project_notes": "",
                "confirmed_products": [
                    {"description": "", "canonical_url": None}
                ],
                "job": {"batch_id": ""},
            },
        )

    def test_natural_language_revision_preserves_completed_steps(self) -> None:
        current = WorkflowPlan(
            organization_id="org-a",
            plan_id="plan-1",
            creator_user_id="user-a",
            conversation_id="conversation-1",
            title="Article workflow",
            natural_language_request="write an article",
            normalized_plan={},
            plan_hash="a" * 64,
            revision=2,
            status="paused",
            project_ids=("project-a",),
            paused_project_ids=(),
            steps=(
                WorkflowPlanStep(
                    step_id="step-1",
                    sequence=1,
                    action_kind="generate_titles",
                    project_id="project-a",
                    article_task_id="task-1",
                    expected_task_revision=3,
                    pinned_prompt_version={"version": 1},
                    pinned_knowledge_snapshot={"snapshot": "one"},
                    status="succeeded",
                    background_job_id=None,
                    retry_count=0,
                    hard_gate=False,
                    human_gate_confirmed=False,
                    input_summary={},
                    output_summary={"result_revision": 4},
                    standardized_error_code=None,
                ),
                WorkflowPlanStep(
                    step_id="step-2",
                    sequence=2,
                    action_kind="generate_article",
                    project_id="project-a",
                    article_task_id="task-1",
                    expected_task_revision=4,
                    pinned_prompt_version={"version": 1},
                    pinned_knowledge_snapshot={"snapshot": "one"},
                    status="pending",
                    background_job_id=None,
                    retry_count=0,
                    hard_gate=False,
                    human_gate_confirmed=False,
                    input_summary={},
                    output_summary={},
                    standardized_error_code=None,
                ),
            ),
            concurrency_limit=3,
            budget_warning=False,
            attention_state="none",
            approved_by="user-a",
            approved_at=None,
        )
        generated = make_plan(
            project_id="project-a",
            action_kind="review",
        )
        merged = _merge_natural_language_revision(current, generated)
        self.assertEqual(merged.steps[0].step_id, "step-1")
        self.assertEqual(merged.steps[0].action_kind, "generate_titles")
        self.assertEqual(merged.steps[0].status, "succeeded")
        self.assertEqual(merged.steps[1].action_kind, "review")
        self.assertEqual([step.sequence for step in merged.steps], [1, 2])

    def test_explicit_revision_cannot_fabricate_execution_state(self) -> None:
        current = WorkflowPlan(
            organization_id="org-a",
            plan_id="plan-1",
            creator_user_id="user-a",
            conversation_id="conversation-1",
            title="Article workflow",
            natural_language_request="write an article",
            normalized_plan={},
            plan_hash="a" * 64,
            revision=2,
            status="failed",
            project_ids=("project-a",),
            paused_project_ids=(),
            steps=(
                WorkflowPlanStep(
                    step_id="step-1",
                    sequence=1,
                    action_kind="generate_titles",
                    project_id="project-a",
                    article_task_id="task-1",
                    expected_task_revision=3,
                    pinned_prompt_version={"version": 1},
                    pinned_knowledge_snapshot={"snapshot": "one"},
                    status="succeeded",
                    background_job_id="job-real",
                    retry_count=1,
                    hard_gate=False,
                    human_gate_confirmed=False,
                    input_summary={"server": "owned"},
                    output_summary={"result_revision": 4},
                    standardized_error_code=None,
                ),
                WorkflowPlanStep(
                    step_id="step-2",
                    sequence=2,
                    action_kind="generate_article",
                    project_id="project-a",
                    article_task_id="task-1",
                    expected_task_revision=4,
                    pinned_prompt_version={},
                    pinned_knowledge_snapshot={},
                    status="failed",
                    background_job_id="job-failed",
                    retry_count=2,
                    hard_gate=False,
                    human_gate_confirmed=False,
                    input_summary={},
                    output_summary={"provider": "failed"},
                    standardized_error_code="background_job_failed",
                ),
            ),
            concurrency_limit=3,
            budget_warning=False,
            attention_state="error",
            approved_by="user-a",
            approved_at=None,
        )
        proposed = PlanDraft(
            title="Retry article",
            natural_language_request="retry",
            project_ids=["project-a"],
            steps=[
                PlanStep(
                    step_id="step-1",
                    sequence=1,
                    action_kind="generate_titles",
                    project_id="project-a",
                    article_task_id="task-1",
                    status="pending",
                    input_summary={"client": "changed"},
                ),
                PlanStep(
                    step_id="step-2",
                    sequence=2,
                    action_kind="generate_article",
                    project_id="project-a",
                    article_task_id="task-1",
                    status="succeeded",
                    background_job_id="job-fake",
                    retry_count=9,
                    output_summary={"fabricated": True},
                    human_gate_confirmed=True,
                ),
                PlanStep(
                    step_id="step-3",
                    sequence=3,
                    action_kind="review",
                    project_id="project-a",
                    article_task_id="task-1",
                    status="skipped",
                    output_summary={"fabricated": True},
                    human_gate_confirmed=True,
                ),
            ],
        )

        normalized = _normalize_explicit_revision_execution(current, proposed)

        self.assertEqual(normalized.steps[0].status, "succeeded")
        self.assertEqual(normalized.steps[0].input_summary, {"server": "owned"})
        self.assertEqual(normalized.steps[0].output_summary, {"result_revision": 4})
        for step in normalized.steps[1:]:
            self.assertEqual(step.status, "pending")
            self.assertIsNone(step.background_job_id)
            self.assertEqual(step.retry_count, 0)
            self.assertEqual(step.output_summary, {})
            self.assertIsNone(step.standardized_error_code)
            self.assertFalse(step.human_gate_confirmed)

    def test_natural_language_revision_remaps_dynamic_task_step_ids(self) -> None:
        current = WorkflowPlan(
            organization_id="org-a",
            plan_id="plan-1",
            creator_user_id="user-a",
            conversation_id="conversation-1",
            title="Article workflow",
            natural_language_request="write an article",
            normalized_plan={},
            plan_hash="a" * 64,
            revision=2,
            status="paused",
            project_ids=("project-a",),
            paused_project_ids=(),
            steps=(
                WorkflowPlanStep(
                    step_id="step-1",
                    sequence=1,
                    action_kind="generate_titles",
                    project_id="project-a",
                    article_task_id="task-1",
                    expected_task_revision=3,
                    pinned_prompt_version={},
                    pinned_knowledge_snapshot={},
                    status="succeeded",
                    background_job_id=None,
                    retry_count=0,
                    hard_gate=False,
                    human_gate_confirmed=False,
                    input_summary={},
                    output_summary={},
                    standardized_error_code=None,
                ),
                WorkflowPlanStep(
                    step_id="step-2",
                    sequence=2,
                    action_kind="generate_article",
                    project_id="project-a",
                    article_task_id="task-1",
                    expected_task_revision=3,
                    pinned_prompt_version={},
                    pinned_knowledge_snapshot={},
                    status="pending",
                    background_job_id=None,
                    retry_count=0,
                    hard_gate=False,
                    human_gate_confirmed=False,
                    input_summary={},
                    output_summary={},
                    standardized_error_code=None,
                ),
            ),
            concurrency_limit=3,
            budget_warning=False,
            attention_state="none",
            approved_by="user-a",
            approved_at=None,
        )
        generated = PlanDraft(
            title="Create and write",
            natural_language_request="create and write",
            project_ids=["project-a"],
            steps=[
                PlanStep(
                    step_id="step-1",
                    sequence=1,
                    action_kind="create_task",
                    project_id="project-a",
                    input_summary={"topic": "new topic", "bind_step_ids": ["step-2"]},
                ),
                PlanStep(
                    step_id="step-2",
                    sequence=2,
                    action_kind="generate_article",
                    project_id="project-a",
                    input_summary={"create_task_step_id": "step-1"},
                ),
            ],
        )
        merged = _merge_natural_language_revision(current, generated)
        self.assertEqual(merged.steps[0].status, "succeeded")
        self.assertNotEqual(merged.steps[1].step_id, "step-1")
        self.assertNotEqual(merged.steps[2].step_id, "step-2")
        self.assertEqual(
            merged.steps[1].input_summary["bind_step_ids"],
            [merged.steps[2].step_id],
        )
        self.assertEqual(
            merged.steps[2].input_summary["create_task_step_id"],
            merged.steps[1].step_id,
        )


if __name__ == "__main__":
    unittest.main()
