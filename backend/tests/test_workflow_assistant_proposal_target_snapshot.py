from __future__ import annotations

import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

from services.access_control import ActorIdentity
from workflow_assistant.proposal_target_snapshot import (
    PostgresProposalTargetSnapshotProvider,
    ProposalTargetSnapshotError,
    _task_item,
)


ACTOR = ActorIdentity("org-1", "user-1")


class _Rows:
    def __init__(self, values: list[object]) -> None:
        self._values = values

    def scalars(self) -> _Rows:
        return self

    def mappings(self) -> _Rows:
        return self

    def all(self) -> list[object]:
        return self._values


class _Connection:
    def __init__(self, results: list[list[object]]) -> None:
        self._results = list(results)

    def execute(self, _statement: object) -> _Rows:
        return _Rows(self._results.pop(0))


class _Engine:
    def __init__(self, results: list[list[object]]) -> None:
        self.connection = _Connection(results)

    def connect(self) -> object:
        return nullcontext(self.connection)


class _Access:
    def __init__(self) -> None:
        self.calls: list[tuple[ActorIdentity, str, str]] = []

    def require(self, actor: ActorIdentity, project_id: str, permission: str) -> None:
        self.calls.append((actor, project_id, permission))


class ProposalTargetSnapshotTests(unittest.TestCase):
    def test_task_projection_is_bounded_and_skips_missing_topic(self) -> None:
        self.assertIsNone(_task_item({"id": "task-1", "topic": "  "}))
        item = _task_item(
            {
                "id": "task-1",
                "topic": "  Roof   ladders ",
                "primary_keyword": " commercial  access ",
                "competitor_blog": "https://example.com/a",
            }
        )
        self.assertIsNotNone(item)
        self.assertEqual("Roof ladders", item.topic)  # type: ignore[union-attr]
        self.assertEqual("commercial access", item.primary_keyword)  # type: ignore[union-attr]

    def test_load_combines_only_authorized_project_state(self) -> None:
        access = _Access()
        engine = _Engine(
            [
                [
                    {
                        "prompt_id": "prompt-1",
                        "kind": "outline",
                        "status": "active",
                        "version": 2,
                        "name": "Outline",
                        "content": "Write a safe outline.",
                    },
                    {
                        "prompt_id": "prompt-archived",
                        "kind": "article",
                        "status": "archived",
                        "version": 1,
                        "name": "Archived",
                        "content": "Archived content.",
                    },
                ],
                [["a" * 64][0]],
                [
                    {
                        "topic_id": "topic-1",
                        "topic": "Topic one",
                        "primary_keyword": "primary",
                        "competitor_keyword": "competitor",
                    }
                ],
                [
                    {
                        "id": "task-1",
                        "topic": "Task one",
                        "primary_keyword": "task keyword",
                    }
                ],
            ]
        )
        with patch(
            "workflow_assistant.proposal_target_snapshot.PostgresServerProjectMetadata"
        ) as metadata_class:
            metadata_class.return_value.get.return_value = SimpleNamespace(
                project_notes="Project note",
                revision=7,
            )
            provider = PostgresProposalTargetSnapshotProvider(
                engine,  # type: ignore[arg-type]
                access=access,  # type: ignore[arg-type]
            )
            snapshot = provider.load(actor=ACTOR, project_id="project-1")

        self.assertEqual([(ACTOR, "project-1", "project.view")], access.calls)
        self.assertEqual(frozenset({"a" * 64}), snapshot.knowledge_content_hashes)
        self.assertEqual(
            ("prompt-1", "prompt-archived"),
            tuple(item.prompt_id for item in snapshot.prompts),
        )
        self.assertEqual(("task-1",), tuple(item.item_id for item in snapshot.task_rows))
        self.assertEqual(("topic-1",), tuple(item.item_id for item in snapshot.topics))
        self.assertEqual(7, snapshot.project_notes_revision)

    def test_rejects_unbounded_project_state(self) -> None:
        access = _Access()
        engine = _Engine([[], [str(index) for index in range(5_001)], [], []])
        with patch(
            "workflow_assistant.proposal_target_snapshot.PostgresServerProjectMetadata"
        ) as metadata_class:
            metadata_class.return_value.get.return_value = SimpleNamespace(
                project_notes="",
                revision=0,
            )
            provider = PostgresProposalTargetSnapshotProvider(
                engine,  # type: ignore[arg-type]
                access=access,  # type: ignore[arg-type]
            )
            with self.assertRaises(ProposalTargetSnapshotError):
                provider.load(actor=ACTOR, project_id="project-1")


if __name__ == "__main__":
    unittest.main()
