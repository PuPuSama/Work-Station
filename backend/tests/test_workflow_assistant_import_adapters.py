from __future__ import annotations

import hashlib
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.access_control import ActorIdentity  # noqa: E402
from workflow_assistant.attachments import AssistantAttachment  # noqa: E402
from workflow_assistant.import_adapters import (  # noqa: E402
    KnowledgeSourceImportAdapter,
    ProjectNotesImportAdapter,
    PromptAssetImportAdapter,
    TaskWorkbookImportAdapter,
    TopicLibraryImportAdapter,
    TypedImportError,
    TypedImportExecutor,
    TypedImportRequest,
    TypedImportResult,
)
from workflow_assistant.import_proposals import ImportProposal  # noqa: E402


NOW = datetime(2026, 8, 20, tzinfo=timezone.utc)


def attachment(content: bytes = b"source") -> AssistantAttachment:
    return AssistantAttachment(
        attachment_id="attachment-one",
        organization_id="org-one",
        creator_user_id="user-one",
        conversation_id="conversation-one",
        proposed_project_id="project-one",
        plan_id=None,
        idempotency_key="upload-one",
        object_key="assistant/org-one/attachment-one/source.txt",
        original_filename="source.txt",
        mime_type="text/plain",
        byte_size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        classification="project_notes",
        classification_payload={},
        revision=4,
        status="proposal_ready",
        expires_at=NOW + timedelta(days=7),
        created_at=NOW,
        updated_at=NOW,
    )


def proposal(kind: str, body: dict[str, object]) -> ImportProposal:
    diff = {
        "schema_version": 1,
        "target_project_id": "project-one",
        "target_kind": kind,
        "source": {"attachment_id": "attachment-one"},
        "requires_publication_review": kind == "knowledge_source",
        "create": [],
        "update": [],
        "skip": [],
        "conflicts": [],
        "invalid": [],
        **body,
    }
    return ImportProposal(
        proposal_id="proposal-one",
        organization_id="org-one",
        attachment_id="attachment-one",
        creator_user_id="user-one",
        target_project_id="project-one",
        plan_id=None,
        target_kind=kind,  # type: ignore[arg-type]
        idempotency_key="preview-one",
        normalized_diff=diff,
        revision=2,
        status="confirmed",
        confirmed_by="user-one",
        confirmed_at=NOW,
        resulting_entity_refs=(),
        standardized_error_code=None,
        created_at=NOW,
        updated_at=NOW,
    )


def request(kind: str, body: dict[str, object], *, content: bytes = b"source") -> TypedImportRequest:
    return TypedImportRequest(
        actor=ActorIdentity("org-one", "user-one"),
        proposal=proposal(kind, body),
        attachment=attachment(content),
        expected_proposal_revision=2,
        idempotency_key="execute-one",
    )


class FakeAccess:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def require(self, actor: ActorIdentity, project_id: str, permission: str) -> object:
        self.calls.append((project_id, permission))
        return object()


class FakeAdapter:
    target_kind = "project_notes"

    def __init__(self) -> None:
        self.calls: list[TypedImportRequest] = []

    def execute(self, value: TypedImportRequest) -> TypedImportResult:
        self.calls.append(value)
        return TypedImportResult(status="completed", entity_refs=())


class ImportExecutorTests(unittest.TestCase):
    def test_routes_only_confirmed_exact_revision_and_reauthorizes(self) -> None:
        access = FakeAccess()
        adapter = FakeAdapter()
        executor = TypedImportExecutor(access=access, adapters=(adapter,))  # type: ignore[arg-type]
        value = request("project_notes", {})

        result = executor.execute(value)

        self.assertEqual(result.status, "completed")
        self.assertEqual(access.calls, [("project-one", "article.edit")])
        self.assertEqual(adapter.calls, [value])

    def test_rejects_stale_revision_before_adapter(self) -> None:
        adapter = FakeAdapter()
        value = request("project_notes", {})
        stale = TypedImportRequest(
            actor=value.actor,
            proposal=value.proposal,
            attachment=value.attachment,
            expected_proposal_revision=1,
            idempotency_key=value.idempotency_key,
        )
        with self.assertRaises(TypedImportError) as raised:
            TypedImportExecutor(
                access=FakeAccess(),  # type: ignore[arg-type]
                adapters=(adapter,),  # type: ignore[arg-type]
            ).execute(stale)
        self.assertEqual(raised.exception.code, "import_proposal_revision_conflict")
        self.assertEqual(adapter.calls, [])


class KnowledgeAdapterTests(unittest.TestCase):
    def test_upload_enters_waiting_publication_without_publish_call(self) -> None:
        content = b"private knowledge"

        class Store:
            def get(self, key: str, *, max_bytes: int) -> bytes:
                self.key = key
                self.max_bytes = max_bytes
                return content

        class Ingestion:
            def upload(self, **kwargs: object) -> object:
                self.kwargs = kwargs
                return SimpleNamespace(
                    created=True,
                    result=SimpleNamespace(
                        source=SimpleNamespace(source_id="source-one"),
                        snapshot=SimpleNamespace(snapshot_id="snapshot-one"),
                    ),
                )

        ingestion = Ingestion()
        result = KnowledgeSourceImportAdapter(
            object_store=Store(),  # type: ignore[arg-type]
            ingestion=ingestion,  # type: ignore[arg-type]
        ).execute(
            request(
                "knowledge_source",
                {"create": [{"title": "Private source"}]},
                content=content,
            )
        )

        self.assertEqual(result.status, "waiting_publication")
        self.assertEqual([item.entity_id for item in result.entity_refs], ["source-one", "snapshot-one"])
        self.assertEqual(ingestion.kwargs["project_id"], "project-one")
        self.assertFalse(hasattr(ingestion, "publish"))


class PromptAdapterTests(unittest.TestCase):
    def test_exact_prompt_is_skipped_on_retry(self) -> None:
        snapshot = SimpleNamespace(
            prompt_id="prompt-one",
            kind="outline",
            content="Write a safe outline",
            version=3,
        )

        class Service:
            def list(self, actor: ActorIdentity) -> object:
                return SimpleNamespace(prompts=(SimpleNamespace(snapshot=snapshot),))

            def create(self, *args: object, **kwargs: object) -> object:
                raise AssertionError("exact retries must not create a prompt")

        result = PromptAssetImportAdapter(
            lambda organization_id, project_id: Service()  # type: ignore[return-value]
        ).execute(
            request(
                "prompt_asset",
                {
                    "prompt_kind": "outline",
                    "create": [
                        {
                            "name": "Outline",
                            "prompt_kind": "outline",
                            "content": "Write a safe outline",
                        }
                    ],
                },
            )
        )
        self.assertEqual(result.skipped_count, 1)
        self.assertEqual(result.entity_refs[0].entity_id, "prompt-one")

    def test_new_prompt_uses_import_owned_deterministic_create(self) -> None:
        class Service:
            def list(self, actor: ActorIdentity) -> object:
                return SimpleNamespace(prompts=())

            def create_imported(self, actor: ActorIdentity, **kwargs: object) -> object:
                self.kwargs = kwargs
                return SimpleNamespace(
                    prompt_id=str(kwargs["prompt_id"]),
                    version=1,
                )

        service = Service()
        result = PromptAssetImportAdapter(
            lambda organization_id, project_id: service  # type: ignore[return-value]
        ).execute(
            request(
                "prompt_asset",
                {
                    "prompt_kind": "outline",
                    "create": [
                        {
                            "name": "Outline",
                            "prompt_kind": "outline",
                            "content": "Write a new outline",
                        }
                    ],
                },
            )
        )
        self.assertEqual(result.created_count, 1)
        self.assertEqual(result.entity_refs[0].action, "create")
        self.assertEqual(
            service.kwargs["prompt_id"],
            "assistant_prompt_10b13740bea252dd94ccebff0992a0ab",
        )


class TaskAdapterTests(unittest.TestCase):
    def test_imports_only_create_bucket_and_preserves_skip_refs(self) -> None:
        class Intake:
            def import_rows(self, **kwargs: object) -> object:
                self.kwargs = kwargs
                return SimpleNamespace(
                    created=True,
                    tasks=(SimpleNamespace(id="task-one", revision=0),),
                )

        intake = Intake()
        result = TaskWorkbookImportAdapter(
            lambda organization_id, project_id: intake  # type: ignore[return-value]
        ).execute(
            request(
                "task_workbook",
                {
                    "create": [{"source_row": 2, "topic": "New topic"}],
                    "skip": [
                        {
                            "source_row": 3,
                            "reason": "already_exists",
                            "existing_id": "task-existing",
                        }
                    ],
                },
            )
        )
        rows = intake.kwargs["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].topic, "New topic")
        self.assertEqual(result.created_count, 1)
        self.assertEqual(result.skipped_count, 1)


class ProjectNotesAdapterTests(unittest.TestCase):
    def test_replay_with_same_notes_skips_stale_expected_revision(self) -> None:
        current = SimpleNamespace(
            project_notes="New notes",
            revision=8,
            customer_name="Customer",
            official_domain="example.com",
        )

        class Metadata:
            def get(self, **kwargs: object) -> object:
                return current

            def update(self, **kwargs: object) -> object:
                raise AssertionError("an already-applied notes import must be idempotent")

        result = ProjectNotesImportAdapter(Metadata()).execute(  # type: ignore[arg-type]
            request(
                "project_notes",
                {
                    "update": [
                        {
                            "field": "project_notes",
                            "expected_revision": 7,
                            "before": "Old notes",
                            "after": "New notes",
                        }
                    ]
                },
            )
        )
        self.assertEqual(result.skipped_count, 1)
        self.assertEqual(result.entity_refs[0].revision, 8)


class TopicAdapterTests(unittest.TestCase):
    def test_routes_create_rows_and_keeps_existing_skips(self) -> None:
        class Topics:
            def import_rows(self, **kwargs: object) -> object:
                self.kwargs = kwargs
                return SimpleNamespace(
                    items=(
                        SimpleNamespace(topic_id="topic-new", created=True),
                    )
                )

        topics = Topics()
        result = TopicLibraryImportAdapter(topics).execute(  # type: ignore[arg-type]
            request(
                "topic_library",
                {
                    "create": [{"source_row": 2, "topic": "New topic"}],
                    "skip": [
                        {
                            "source_row": 3,
                            "reason": "already_exists",
                            "existing_id": "topic-existing",
                        }
                    ],
                },
            )
        )
        rows = topics.kwargs["rows"]
        self.assertEqual(rows[0].topic, "New topic")
        self.assertEqual(result.created_count, 1)
        self.assertEqual(result.skipped_count, 1)


if __name__ == "__main__":
    unittest.main()
