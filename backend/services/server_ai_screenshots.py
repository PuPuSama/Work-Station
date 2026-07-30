from __future__ import annotations

import hashlib
from typing import Protocol

from knowledge_agent.assets import KnowledgeAsset
from knowledge_agent.object_storage import (
    FINAL_AI_SCREENSHOT_ARTIFACT_KIND,
)
from models import TaskRecord
from services.access_control import ActorIdentity
from services.ai_screenshots import (
    AIScreenshotError,
    build_ai_rate_screenshot_png,
)


MAX_SERVER_AI_SCREENSHOT_BYTES = 25 * 1024 * 1024


class ServerAiScreenshotError(ValueError):
    """The supplied final AI-rate screenshot is not safe to persist."""


class ServerAiScreenshotObjectService(Protocol):
    """Private-object operation required by Server screenshot preparation."""

    def upload_final_ai_screenshot(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        asset_id: str,
        data: bytes,
        width: int,
        height: int,
    ) -> KnowledgeAsset: ...


class ServerFinalAiScreenshotPreparation:
    """Normalize and store one final-review screenshot without local paths."""

    def __init__(
        self,
        *,
        objects: ServerAiScreenshotObjectService,
    ) -> None:
        self._objects = objects

    def prepare(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        task: TaskRecord,
        content: bytes,
    ) -> TaskRecord:
        if not task.humanized_article.strip():
            raise ServerAiScreenshotError(
                "the humanized article must be saved first"
            )
        body = bytes(content)
        if len(body) > MAX_SERVER_AI_SCREENSHOT_BYTES:
            raise ServerAiScreenshotError(
                "AI-rate screenshot exceeds 25 MB"
            )
        try:
            png, width, height = build_ai_rate_screenshot_png(body)
        except AIScreenshotError as exc:
            raise ServerAiScreenshotError(str(exc)) from exc

        digest = hashlib.sha256(png).hexdigest()
        asset = self._objects.upload_final_ai_screenshot(
            actor=actor,
            project_id=project_id,
            asset_id=f"asset_{digest}",
            data=png,
            width=width,
            height=height,
        )
        if (
            asset.content_hash != digest
            or asset.content_type != "image/png"
            or asset.width != width
            or asset.height != height
            or str(asset.metadata.get("artifact_kind") or "")
            != FINAL_AI_SCREENSHOT_ARTIFACT_KIND
        ):
            raise ServerAiScreenshotError(
                "stored AI-rate screenshot identity is inconsistent"
            )

        check = task.final_ai_check
        check.screenshot_path = ""
        check.screenshot_asset_id = asset.asset_id
        check.screenshot_content_hash = asset.content_hash
        check.screenshot_filename = "final-ai-rate.png"
        check.screenshot_width = width
        check.screenshot_height = height
        task.delivery_package_path = ""
        task.workflow_error = None
        return task


__all__ = [
    "MAX_SERVER_AI_SCREENSHOT_BYTES",
    "ServerAiScreenshotError",
    "ServerAiScreenshotObjectService",
    "ServerFinalAiScreenshotPreparation",
]
