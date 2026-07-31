from __future__ import annotations

import hashlib
import sys
import unittest
from io import BytesIO
from pathlib import Path

from PIL import Image


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from knowledge_agent.assets import KnowledgeAsset  # noqa: E402
from knowledge_agent.object_storage import (  # noqa: E402
    FINAL_AI_SCREENSHOT_ARTIFACT_KIND,
    INITIAL_AI_SCREENSHOT_ARTIFACT_KIND,
)
from models import (  # noqa: E402
    STATUS_DRAFT_READY,
    STATUS_HUMANIZED_READY,
    TaskRecord,
)
from services.access_control import ActorIdentity  # noqa: E402
from services.server_ai_screenshots import (  # noqa: E402
    MAX_SERVER_AI_SCREENSHOT_BYTES,
    ServerAiScreenshotError,
    ServerFinalAiScreenshotPreparation,
    ServerInitialAiScreenshotPreparation,
)


class RecordingScreenshotObjects:
    def __init__(
        self,
        *,
        artifact_kind: str = FINAL_AI_SCREENSHOT_ARTIFACT_KIND,
    ) -> None:
        self.artifact_kind = artifact_kind
        self.data = b""
        self.width = 0
        self.height = 0

    def upload_final_ai_screenshot(
        self,
        *,
        actor,
        project_id,
        asset_id,
        data,
        width,
        height,
    ) -> KnowledgeAsset:
        self.data = bytes(data)
        self.width = int(width)
        self.height = int(height)
        return KnowledgeAsset(
            project_id=project_id,
            asset_id=asset_id,
            content_hash=hashlib.sha256(self.data).hexdigest(),
            artifact_uri=(
                "s3://private-bucket/organizations/"
                f"{actor.organization_id}/projects/{project_id}/"
                f"blobs/aa/{asset_id}"
            ),
            content_type="image/png",
            byte_size=len(self.data),
            width=self.width,
            height=self.height,
            metadata={"artifact_kind": self.artifact_kind},
        )

    def upload_initial_ai_screenshot(
        self,
        *,
        actor,
        project_id,
        asset_id,
        data,
        width,
        height,
    ) -> KnowledgeAsset:
        self.artifact_kind = INITIAL_AI_SCREENSHOT_ARTIFACT_KIND
        return self.upload_final_ai_screenshot(
            actor=actor,
            project_id=project_id,
            asset_id=asset_id,
            data=data,
            width=width,
            height=height,
        )


def screenshot_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (640, 360), "white").save(
        output,
        format="JPEG",
        quality=90,
    )
    return output.getvalue()


def server_task() -> TaskRecord:
    return TaskRecord(
        id="server-final-ai-task",
        week_folder="server",
        customer="example.com",
        topic_index=1,
        topic="Final review",
        status=STATUS_HUMANIZED_READY,
        task_dir="/server/server-final-ai-task",
        humanized_article="# Final review\n\nReviewed article.",
        created_at="2026-07-31T00:00:00+00:00",
        updated_at="2026-07-31T00:00:00+00:00",
    )


class ServerAiScreenshotTests(unittest.TestCase):
    def test_normalizes_initial_review_png_without_local_path(self) -> None:
        objects = RecordingScreenshotObjects()
        task = server_task()
        task.status = STATUS_DRAFT_READY
        task.initial_article = "# Initial review\n\nReviewed draft."
        task.humanized_article = ""

        saved = ServerInitialAiScreenshotPreparation(
            objects=objects,
        ).prepare(
            actor=ActorIdentity("org-a", "reviewer-a"),
            project_id="example.com",
            task=task,
            content=screenshot_bytes(),
        )

        self.assertEqual(saved.initial_ai_check.screenshot_path, "")
        self.assertTrue(saved.initial_ai_check.screenshot_asset_id)
        self.assertEqual(
            saved.initial_ai_check.screenshot_filename,
            "initial-ai-rate.png",
        )
        self.assertEqual(
            (
                saved.initial_ai_check.screenshot_width,
                saved.initial_ai_check.screenshot_height,
            ),
            (640, 360),
        )
        self.assertEqual(
            objects.artifact_kind,
            INITIAL_AI_SCREENSHOT_ARTIFACT_KIND,
        )

    def test_normalizes_private_png_without_local_path(self) -> None:
        objects = RecordingScreenshotObjects()
        task = server_task()

        saved = ServerFinalAiScreenshotPreparation(
            objects=objects,
        ).prepare(
            actor=ActorIdentity("org-a", "reviewer-a"),
            project_id="example.com",
            task=task,
            content=screenshot_bytes(),
        )

        self.assertIs(saved, task)
        self.assertEqual(saved.final_ai_check.screenshot_path, "")
        self.assertTrue(saved.final_ai_check.screenshot_asset_id)
        self.assertEqual(
            saved.final_ai_check.screenshot_content_hash,
            hashlib.sha256(objects.data).hexdigest(),
        )
        self.assertEqual(
            saved.final_ai_check.screenshot_filename,
            "final-ai-rate.png",
        )
        self.assertEqual(
            (
                saved.final_ai_check.screenshot_width,
                saved.final_ai_check.screenshot_height,
            ),
            (640, 360),
        )
        with Image.open(BytesIO(objects.data)) as image:
            self.assertEqual(image.format, "PNG")
            self.assertEqual(image.size, (640, 360))

    def test_rejects_missing_article_invalid_and_oversized_input(self) -> None:
        missing = server_task()
        missing.humanized_article = ""
        preparation = ServerFinalAiScreenshotPreparation(
            objects=RecordingScreenshotObjects(),
        )
        with self.assertRaisesRegex(
            ServerAiScreenshotError,
            "must be saved first",
        ):
            preparation.prepare(
                actor=ActorIdentity("org-a", "reviewer-a"),
                project_id="example.com",
                task=missing,
                content=screenshot_bytes(),
            )
        with self.assertRaisesRegex(
            ServerAiScreenshotError,
            "not a valid image",
        ):
            preparation.prepare(
                actor=ActorIdentity("org-a", "reviewer-a"),
                project_id="example.com",
                task=server_task(),
                content=b"not-an-image",
            )
        with self.assertRaisesRegex(
            ServerAiScreenshotError,
            "exceeds 25 MB",
        ):
            preparation.prepare(
                actor=ActorIdentity("org-a", "reviewer-a"),
                project_id="example.com",
                task=server_task(),
                content=b"x" * (MAX_SERVER_AI_SCREENSHOT_BYTES + 1),
            )

    def test_rejects_mismatched_stored_access_classification(self) -> None:
        with self.assertRaisesRegex(
            ServerAiScreenshotError,
            "identity is inconsistent",
        ):
            ServerFinalAiScreenshotPreparation(
                objects=RecordingScreenshotObjects(
                    artifact_kind="article_docx"
                ),
            ).prepare(
                actor=ActorIdentity("org-a", "reviewer-a"),
                project_id="example.com",
                task=server_task(),
                content=screenshot_bytes(),
            )


if __name__ == "__main__":
    unittest.main()
