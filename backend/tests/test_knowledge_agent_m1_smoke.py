from __future__ import annotations

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import sqlalchemy as sa


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from knowledge_agent import EMBEDDING_DIMENSIONS, EmbeddingBatch  # noqa: E402
from knowledge_agent import m1_smoke  # noqa: E402
from knowledge_agent.database import create_knowledge_engine  # noqa: E402
from knowledge_agent.schema import projects  # noqa: E402


def smoke_vector() -> tuple[float, ...]:
    return (1.0,) + (0.0,) * (EMBEDDING_DIMENSIONS - 1)


class FakeSmokeEmbeddingProvider:
    model_id = "m1-smoke-model"
    dimensions = EMBEDDING_DIMENSIONS

    def embed(self, texts: tuple[str, ...]) -> EmbeddingBatch:
        return EmbeddingBatch(
            vectors=tuple(smoke_vector() for _ in texts),
            model=self.model_id,
        )

    def __enter__(self) -> FakeSmokeEmbeddingProvider:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class M1SmokeCliTests(unittest.TestCase):
    def test_failure_uses_a_stable_safe_error_without_a_traceback(self) -> None:
        secret = "credential-that-must-not-appear"
        stdout = io.StringIO()
        stderr = io.StringIO()

        with (
            patch.object(
                m1_smoke,
                "main",
                side_effect=RuntimeError(
                    f"{secret} postgresql://user:password@example.test/database"
                ),
            ),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            with self.assertRaises(SystemExit) as raised:
                m1_smoke.cli()

        self.assertEqual(raised.exception.code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(
            json.loads(stderr.getvalue()),
            {"error_code": "M1_SMOKE_FAILED"},
        )
        self.assertNotIn(secret, stderr.getvalue())
        self.assertNotIn("postgresql://", stderr.getvalue())

    def test_success_adds_no_cli_wrapper_output(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()

        with (
            patch.object(m1_smoke, "main"),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            m1_smoke.cli()

        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

    def test_real_database_smoke_is_repeatable_and_cleans_its_fixture(self) -> None:
        database_url = os.environ.get("ARTICLE_AGENT_DATABASE_URL", "").strip()
        if not database_url:
            self.skipTest("ARTICLE_AGENT_DATABASE_URL is not set")

        outputs: list[dict[str, object]] = []
        with (
            patch.object(
                m1_smoke,
                "load_config",
                return_value=SimpleNamespace(knowledge_agent_enabled=False),
            ),
            patch.object(
                m1_smoke,
                "load_knowledge_agent_settings",
                return_value=SimpleNamespace(database_url=database_url),
            ),
            patch.object(
                m1_smoke.OpenAICompatibleEmbeddingProvider,
                "from_settings",
                return_value=FakeSmokeEmbeddingProvider(),
            ),
        ):
            for _ in range(2):
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    m1_smoke.main()
                outputs.append(json.loads(stdout.getvalue()))

        first_hit_ids = outputs[0]["hit_ids"]
        second_hit_ids = outputs[1]["hit_ids"]
        self.assertIsInstance(first_hit_ids, list)
        self.assertIsInstance(second_hit_ids, list)
        self.assertNotEqual(first_hit_ids, second_hit_ids)
        project_ids = tuple(
            hit_ids[0].split(":snapshot:", 1)[0]
            for hit_ids in (first_hit_ids, second_hit_ids)
        )

        engine = create_knowledge_engine(database_url)
        try:
            with engine.connect() as connection:
                remaining = connection.execute(
                    sa.select(sa.func.count())
                    .select_from(projects)
                    .where(projects.c.project_id.in_(project_ids))
                ).scalar_one()
        finally:
            engine.dispose()
        self.assertEqual(remaining, 0)


if __name__ == "__main__":
    unittest.main()
