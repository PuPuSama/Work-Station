from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from models import PromptSnapshot, TaskRecord  # noqa: E402
from services.access_control import ActorIdentity  # noqa: E402
from services.server_outline_generation import (  # noqa: E402
    PublishedGenerationContextChunk,
)
from services.server_task_writing_settings import (  # noqa: E402
    PostgresServerTaskWritingSettingsService,
    ServerTaskWritingSettings,
    ServerTaskWritingSettingsConflict,
    ServerTaskWritingSettingsError,
)
from storage import RevisionConflictError  # noqa: E402


def _task() -> TaskRecord:
    return TaskRecord(
        id="topic-1",
        revision=7,
        week_folder="server",
        customer="project-a",
        brand_name="Example Brand",
        project_introduction="Existing project introduction",
        project_notes="Existing project notes",
        topic_notes="Old topic notes",
        outline_custom_prompt="Old outline custom prompt",
        article_custom_prompt="Old article custom prompt",
        use_outline_custom_prompt=False,
        use_article_custom_prompt=False,
        outline_prompt_selection="system",
        article_prompt_selection="system",
        last_outline_prompt_snapshot=_snapshot("outline", version=2),
        last_article_prompt_snapshot=_snapshot("article", version=4),
        include_project_introduction=True,
        include_project_notes=True,
        include_topic_notes=True,
        topic_index=1,
        topic="Fastener sourcing guide",
        competitor_keyword="industrial fasteners",
        status="outline_confirmed",
        selected_title="How to source industrial fasteners",
        outline="# Confirmed outline\n\n## Materials",
        raw_draft_article="Existing downstream draft",
        task_dir="/server/topic-1",
        created_at="2026-07-31T00:00:00+00:00",
        updated_at="2026-07-31T00:00:00+00:00",
    )


def _snapshot(kind: str, *, version: int = 1) -> PromptSnapshot:
    return PromptSnapshot(
        prompt_id=f"private-{kind}-id",
        name=f"Current {kind}",
        kind=kind,  # type: ignore[arg-type]
        content=f"Current {kind} instructions.",
        version=version,
        source="project_default",
        captured_at="2026-08-06T00:00:00+00:00",
    )


class _Repository:
    def __init__(self, task: TaskRecord) -> None:
        self.payload = task.model_dump(mode="json")

    def get(self, task_id: str) -> dict[str, object] | None:
        if task_id != "topic-1":
            return None
        return copy.deepcopy(self.payload)


class _Access:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def require(
        self,
        actor: ActorIdentity,
        project_id: str,
        permission: str,
    ) -> None:
        self.calls.append((actor.user_id, project_id, permission))


class _Prompts:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.transaction_connections: list[object] = []

    def resolve(
        self,
        actor: ActorIdentity,
        kind: str,
        selection: str,
    ) -> PromptSnapshot:
        del actor
        self.calls.append((kind, selection))
        return _snapshot(kind, version=9 if kind == "outline" else 11)

    def resolve_for_update_in_transaction(
        self,
        connection: object,
        actor: ActorIdentity,
        *,
        kind: str,
        selection: str,
    ) -> PromptSnapshot:
        del actor
        self.transaction_connections.append(connection)
        self.calls.append((kind, selection))
        return _snapshot(kind, version=9 if kind == "outline" else 11)


class _Writer:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def put_in_transaction(
        self,
        connection: object,
        task: TaskRecord,
        *,
        expected_revision: int,
        actor: ActorIdentity,
        action: object,
        details: dict[str, object],
    ) -> TaskRecord:
        self.calls.append(
            {
                "task": copy.deepcopy(task),
                "connection": connection,
                "expected_revision": expected_revision,
                "actor": actor,
                "action": action,
                "details": copy.deepcopy(details),
            }
        )
        task.revision += 1
        return task


class _Engine:
    def __init__(self) -> None:
        self.connection = object()

    def begin(self) -> "_Engine":
        return self

    def __enter__(self) -> object:
        return self.connection

    def __exit__(self, *args: object) -> None:
        del args


class _Context:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def select(
        self,
        *,
        project_id: str,
        query: str,
    ) -> tuple[PublishedGenerationContextChunk, ...]:
        self.calls.append((project_id, query))
        return (
            PublishedGenerationContextChunk(
                chunk_id="chunk-1",
                heading_path=("Products",),
                text="Published fastener evidence.",
                canonical_url="https://example.invalid/products",
            ),
        )


class _OfficialLinks:
    def select(self, *, project_id: str, customer: str) -> tuple[object, ...]:
        del project_id, customer
        return ()


def _service(task: TaskRecord) -> PostgresServerTaskWritingSettingsService:
    service = object.__new__(PostgresServerTaskWritingSettingsService)
    service._engine = _Engine()
    service._config = SimpleNamespace(default_word_count=1_500)
    service._repository = _Repository(task)
    service._access = _Access()
    service._prompts = _Prompts()
    service._writer = _Writer()
    service._context = _Context()
    service._official_links = _OfficialLinks()
    service.organization_id = "org-a"
    service.project_id = "project-a"
    return service


def _settings(**changes: object) -> ServerTaskWritingSettings:
    values: dict[str, object] = {
        "topic_notes": " Draft topic notes ",
        "outline_custom_prompt": " Outline instruction ",
        "article_custom_prompt": " Article instruction ",
        "use_outline_custom_prompt": True,
        "use_article_custom_prompt": True,
        "outline_prompt_selection": "   ",
        "article_prompt_selection": " project_default ",
        "include_project_introduction": False,
        "include_project_notes": False,
        "include_topic_notes": True,
    }
    values.update(changes)
    return ServerTaskWritingSettings(**values)  # type: ignore[arg-type]


class ServerTaskWritingSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.actor = ActorIdentity(
            organization_id="org-a",
            user_id="editor-a",
        )

    def test_update_normalizes_and_commits_only_settings_with_safe_audit(
        self,
    ) -> None:
        original = _task()
        service = _service(original)

        result = service.update(
            self.actor,
            " topic-1 ",
            7,
            _settings(),
        )

        self.assertEqual(result.revision, 8)
        self.assertEqual(result.topic_notes, "Draft topic notes")
        self.assertEqual(result.outline_prompt_selection, "project_default")
        self.assertEqual(result.article_prompt_selection, "project_default")
        self.assertEqual(result.status, original.status)
        self.assertEqual(result.raw_draft_article, original.raw_draft_article)
        self.assertEqual(
            result.last_outline_prompt_snapshot,
            original.last_outline_prompt_snapshot,
        )
        self.assertEqual(
            result.last_article_prompt_snapshot,
            original.last_article_prompt_snapshot,
        )
        self.assertEqual(
            service._prompts.calls,
            [
                ("outline", "project_default"),
                ("article", "project_default"),
            ],
        )
        self.assertEqual(
            service._access.calls,
            [("editor-a", "project-a", "article.edit")],
        )
        call = service._writer.calls[0]
        self.assertEqual(call["action"], "article.writing_settings.updated")
        self.assertEqual(call["expected_revision"], 7)
        self.assertIs(call["connection"], service._engine.connection)
        self.assertEqual(
            service._prompts.transaction_connections,
            [service._engine.connection, service._engine.connection],
        )
        details = call["details"]
        assert isinstance(details, dict)
        self.assertEqual(details["outline_prompt_source"], "project_default")
        self.assertEqual(details["outline_prompt_version"], 9)
        self.assertEqual(details["article_prompt_version"], 11)
        self.assertTrue(details["topic_notes_changed"])
        self.assertFalse(details["include_project_notes"])
        serialized_details = repr(details)
        self.assertNotIn("Draft topic notes", serialized_details)
        self.assertNotIn("private-outline-id", serialized_details)
        self.assertNotIn("https://", serialized_details)

    def test_preview_is_memory_only_and_resolves_only_requested_kind(
        self,
    ) -> None:
        original = _task()
        service = _service(original)

        result = service.preview(
            self.actor,
            "topic-1",
            7,
            "outline",
            _settings(),
        )

        self.assertEqual(result.snapshot.kind, "outline")
        self.assertEqual(result.snapshot.version, 9)
        self.assertEqual(result.context_chunk_count, 1)
        self.assertEqual(result.target_words, 1_200)
        self.assertIn("Draft topic notes", result.effective_prompt)
        self.assertIn("Published fastener evidence", result.effective_prompt)
        self.assertEqual(
            service._prompts.calls,
            [("outline", "project_default")],
        )
        self.assertEqual(len(service._access.calls), 2)
        self.assertTrue(
            all(call[2] == "project.view" for call in service._access.calls)
        )
        self.assertEqual(service._writer.calls, [])
        self.assertEqual(original.topic_notes, "Old topic notes")
        self.assertEqual(
            service._repository.payload["topic_notes"],
            "Old topic notes",
        )
        self.assertEqual(
            service._context.calls,
            [
                (
                    "project-a",
                    "How to source industrial fasteners "
                    "Fastener sourcing guide industrial fasteners",
                )
            ],
        )

    def test_article_preview_requires_confirmed_title_and_outline(
        self,
    ) -> None:
        task = _task()
        task.outline = ""
        service = _service(task)

        with self.assertRaisesRegex(
            ServerTaskWritingSettingsConflict,
            "confirmed title and outline",
        ):
            service.preview(
                self.actor,
                "topic-1",
                7,
                "article",
                _settings(),
            )

        self.assertEqual(service._prompts.calls, [])
        self.assertEqual(service._context.calls, [])
        self.assertEqual(service._writer.calls, [])

    def test_rejects_stale_revision_and_defensive_length_or_type_errors(
        self,
    ) -> None:
        service = _service(_task())
        with self.assertRaises(RevisionConflictError):
            service.update(self.actor, "topic-1", 6, _settings())
        with self.assertRaisesRegex(
            ServerTaskWritingSettingsError,
            "topic_notes is too long",
        ):
            service.update(
                self.actor,
                "topic-1",
                7,
                _settings(topic_notes="x" * 30_001),
            )
        with self.assertRaisesRegex(
            ServerTaskWritingSettingsError,
            "outline_prompt_selection is too long",
        ):
            service.update(
                self.actor,
                "topic-1",
                7,
                _settings(outline_prompt_selection="x" * 256),
            )
        with self.assertRaisesRegex(
            ServerTaskWritingSettingsError,
            "must be boolean",
        ):
            service.update(
                self.actor,
                "topic-1",
                7,
                _settings(include_topic_notes=1),
            )


if __name__ == "__main__":
    unittest.main()
