from __future__ import annotations

import sys
import unittest
from pathlib import Path

from sqlalchemy.dialects import postgresql


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services.postgres_task_repository import PostgresTaskRepository  # noqa: E402


class _Result:
    def mappings(self):
        return []


class _Connection:
    def __init__(self) -> None:
        self.statement = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement):
        self.statement = statement
        return _Result()


class _Engine:
    def __init__(self) -> None:
        self.connection = _Connection()

    def connect(self):
        return self.connection


class PostgresTaskMetricsTests(unittest.TestCase):
    def test_metrics_query_projects_only_progress_fields(self) -> None:
        engine = _Engine()
        repository = PostgresTaskRepository(
            engine,
            organization_id="org-a",
            project_id="project-a",
        )

        self.assertEqual(repository.load_metrics(["task-a"]), [])
        sql = str(
            engine.connection.statement.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        self.assertIn("article_tasks.task_id", sql)
        self.assertIn("final_ai_check", sql)
        self.assertIn("knowledge_coverage", sql)
        self.assertNotIn("SELECT article_tasks.payload", sql)


if __name__ == "__main__":
    unittest.main()
