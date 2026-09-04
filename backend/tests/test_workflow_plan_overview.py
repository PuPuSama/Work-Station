from __future__ import annotations

import sys
import unittest
from pathlib import Path

from sqlalchemy.dialects import postgresql


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services.access_control import ActorIdentity  # noqa: E402
from workflow_assistant.repository import (  # noqa: E402
    PostgresWorkflowAssistantRepository,
)


class _Result:
    def __init__(self, *, row=None, rows=()):
        self._row = row
        self._rows = rows

    def mappings(self):
        return self

    def one_or_none(self):
        return self._row

    def all(self):
        return list(self._rows)


class _Connection:
    def __init__(self):
        self.statements = []
        self._responses = [
            _Result(
                row={
                    "organization_id": "org-a",
                    "plan_id": "plan-a",
                    "creator_user_id": "user-a",
                    "conversation_id": "conversation-a",
                    "natural_language_request": "批量写作（结构化配置）：测试",
                    "title": "Batch overview",
                    "plan_hash": "a" * 64,
                    "revision": 3,
                    "status": "running",
                    "concurrency_limit": 5,
                    "budget_warning": False,
                    "attention_state": "none",
                    "approved_by": None,
                    "approved_at": None,
                }
            ),
            _Result(rows=({"project_id": "project-a", "paused": False},)),
            _Result(
                rows=(
                    {
                        "step_id": "step-a",
                        "sequence": 1,
                        "action_kind": "create_task",
                        "project_id": "project-a",
                        "article_task_id": "task-a",
                        "status": "pending",
                        "background_job_id": None,
                        "retry_count": 0,
                        "hard_gate": False,
                        "human_gate_confirmed": False,
                        "input_summary": {"topic": "test"},
                        "output_summary": {},
                        "standardized_error_code": None,
                        "updated_at": None,
                    },
                )
            ),
        ]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement):
        self.statements.append(statement)
        return self._responses[len(self.statements) - 1]


class _Engine:
    def __init__(self):
        self.connection = _Connection()

    def connect(self):
        return self.connection


class WorkflowPlanOverviewTests(unittest.TestCase):
    def test_overview_projection_omits_private_step_snapshots(self) -> None:
        engine = _Engine()
        repository = PostgresWorkflowAssistantRepository(engine)

        plan = repository.get_plan_overview(
            actor=ActorIdentity("org-a", "user-a"),
            plan_id="plan-a",
        )

        self.assertEqual(plan.title, "Batch overview")
        self.assertEqual(plan.steps[0].input_summary, {"topic": "test"})
        self.assertEqual(plan.steps[0].pinned_prompt_version, {})
        self.assertEqual(plan.steps[0].pinned_knowledge_snapshot, {})
        self.assertIsNone(plan.steps[0].expected_task_revision)

        plan_sql = str(
            engine.connection.statements[0].compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        step_sql = str(
            engine.connection.statements[2].compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        self.assertIn("normalized_plan ->> 'title'", plan_sql)
        self.assertNotIn("SELECT workflow_plans.normalized_plan,", plan_sql)
        self.assertNotIn("expected_task_revision", step_sql)
        self.assertNotIn("pinned_prompt_version", step_sql)
        self.assertNotIn("pinned_knowledge_snapshot", step_sql)


if __name__ == "__main__":
    unittest.main()
