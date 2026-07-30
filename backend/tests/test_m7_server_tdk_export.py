from __future__ import annotations

import sys
import unittest
from io import BytesIO
from pathlib import Path

from docx import Document


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from config import load_config  # noqa: E402
from knowledge_agent.assets import KnowledgeAsset  # noqa: E402
from knowledge_agent.object_storage import (  # noqa: E402
    ARTICLE_DOCX_CONTENT_TYPE,
    TDK_DOCX_ARTIFACT_KIND,
)
from models import STATUS_DOCX_EXPORTED, TaskRecord  # noqa: E402
from services.access_control import ActorIdentity  # noqa: E402
from services.server_tdk_export import (  # noqa: E402
    ServerTdkDocxExport,
    ServerTdkError,
    ServerTdkUnavailable,
)


ARTICLE = """# Exact Server Article Title

Buyers should compare specifications, quality checks, and order requirements.
"""

VALID_RESPONSE = """{
  "description": "Compare supplier specifications, quality checks, and order requirements for a safer B2B sourcing decision.",
  "keywords": [
    "supplier comparison",
    "B2B sourcing",
    "product specifications",
    "quality checks",
    "order requirements",
    "supplier selection"
  ]
}"""


class StubLlm:
    ready = True

    def __init__(self, response: str = VALID_RESPONSE) -> None:
        self.response = response
        self.calls = 0

    def chat(self, messages, temperature=0.7, max_tokens=1800):
        del messages, temperature, max_tokens
        self.calls += 1
        return self.response


class FailingLlm:
    ready = True

    def chat(self, messages, temperature=0.7, max_tokens=1800):
        del messages, temperature, max_tokens
        raise RuntimeError(
            "provider-private-response-body-marker"
        )


class RecordingTdkObjects:
    def __init__(self, *, artifact_kind: str = TDK_DOCX_ARTIFACT_KIND):
        self.artifact_kind = artifact_kind
        self.data = b""
        self.asset_id = ""

    def upload_tdk_docx(
        self,
        *,
        actor,
        project_id,
        asset_id,
        data,
    ) -> KnowledgeAsset:
        self.data = bytes(data)
        self.asset_id = asset_id
        return KnowledgeAsset(
            project_id=project_id,
            asset_id=asset_id,
            content_hash=asset_id.removeprefix("asset_"),
            artifact_uri=(
                "s3://private-bucket/organizations/"
                f"{actor.organization_id}/projects/{project_id}/"
                f"blobs/aa/{asset_id}"
            ),
            content_type=ARTICLE_DOCX_CONTENT_TYPE,
            byte_size=len(self.data),
            metadata={"artifact_kind": self.artifact_kind},
        )


def server_task() -> TaskRecord:
    return TaskRecord(
        id="server-tdk-task",
        week_folder="server",
        customer="example.com",
        topic_index=1,
        topic="Supplier comparison",
        status=STATUS_DOCX_EXPORTED,
        task_dir="/server/server-tdk-task",
        selected_title="Different selected title",
        final_article=ARTICLE,
        docx_asset_id="asset_article_docx",
        created_at="2026-07-31T00:00:00+00:00",
        updated_at="2026-07-31T00:00:00+00:00",
    )


class ServerTdkExportTests(unittest.TestCase):
    def test_generates_private_d_docx_without_local_path(self) -> None:
        objects = RecordingTdkObjects()
        llm = StubLlm()
        task = server_task()

        saved = ServerTdkDocxExport(
            config=load_config(),
            objects=objects,
            llm=llm,
        ).generate(
            actor=ActorIdentity("org-a", "editor-a"),
            project_id="example.com",
            task=task,
        )

        self.assertIs(saved, task)
        self.assertEqual(llm.calls, 1)
        self.assertEqual(saved.tdk.title, "Exact Server Article Title")
        self.assertEqual(saved.tdk_path, "")
        self.assertEqual(saved.tdk_asset_id, objects.asset_id)
        self.assertEqual(len(saved.tdk_content_hash), 64)
        self.assertEqual(saved.tdk_filename, "D.docx")
        document = Document(BytesIO(objects.data))
        self.assertEqual(
            [paragraph.text for paragraph in document.paragraphs],
            [
                "T: Exact Server Article Title",
                f"D: {saved.tdk.description}",
                f"K: {', '.join(saved.tdk.keywords)}",
            ],
        )

    def test_requires_server_docx_and_valid_stored_identity(self) -> None:
        missing = server_task()
        missing.docx_asset_id = ""
        with self.assertRaisesRegex(
            ServerTdkError,
            "must be exported first",
        ):
            ServerTdkDocxExport(
                config=load_config(),
                objects=RecordingTdkObjects(),
                llm=StubLlm(),
            ).generate(
                actor=ActorIdentity("org-a", "editor-a"),
                project_id="example.com",
                task=missing,
            )

        with self.assertRaisesRegex(
            ServerTdkError,
            "identity is inconsistent",
        ):
            ServerTdkDocxExport(
                config=load_config(),
                objects=RecordingTdkObjects(
                    artifact_kind="article_docx"
                ),
                llm=StubLlm(),
            ).generate(
                actor=ActorIdentity("org-a", "editor-a"),
                project_id="example.com",
                task=server_task(),
            )

    def test_provider_failure_does_not_expose_provider_details(self) -> None:
        with self.assertRaises(ServerTdkUnavailable) as captured:
            ServerTdkDocxExport(
                config=load_config(),
                objects=RecordingTdkObjects(),
                llm=FailingLlm(),
            ).generate(
                actor=ActorIdentity("org-a", "editor-a"),
                project_id="example.com",
                task=server_task(),
            )

        self.assertEqual(
            str(captured.exception),
            "TDK generation is temporarily unavailable",
        )
        self.assertNotIn(
            "provider-private-response-body-marker",
            str(captured.exception),
        )


if __name__ == "__main__":
    unittest.main()
