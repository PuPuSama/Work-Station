from __future__ import annotations

import hashlib
from typing import Protocol

from knowledge_agent.assets import KnowledgeAsset
from knowledge_agent.object_storage import (
    ARTICLE_DOCX_ARTIFACT_KIND,
    ARTICLE_DOCX_CONTENT_TYPE,
    DELIVERY_ZIP_ARTIFACT_KIND,
    FINAL_AI_SCREENSHOT_ARTIFACT_KIND,
    ProjectKnowledgeObject,
    TDK_DOCX_ARTIFACT_KIND,
)
from models import TaskRecord
from services.access_control import ActorIdentity
from services.delivery_package import (
    DeliveryPackageError,
    build_delivery_zip_bytes,
    official_website_folder_name,
)
from services.server_ai_screenshots import (
    MAX_SERVER_AI_SCREENSHOT_BYTES,
)
from services.server_article_images import (
    MAX_SERVER_SOURCE_IMAGE_BYTES,
)
from services.server_docx_export import (
    MAX_SERVER_DOCX_BYTES,
    verified_server_article_webp,
)
from services.server_tdk_export import MAX_SERVER_TDK_DOCX_BYTES
from storage import content_hash


MAX_SERVER_DELIVERY_ZIP_BYTES = 128 * 1024 * 1024


class ServerDeliveryPackageError(ValueError):
    """A complete Server delivery archive cannot be assembled safely."""


class ServerDeliveryObjectService(Protocol):
    def read_for_article_delivery(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        asset_id: str,
        max_bytes: int,
    ) -> ProjectKnowledgeObject: ...

    def upload_delivery_zip(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        asset_id: str,
        data: bytes,
    ) -> KnowledgeAsset: ...


def _require_artifact(
    stored: ProjectKnowledgeObject,
    *,
    asset_id: str,
    content_hash: str,
    content_type: str,
    artifact_kind: str,
    label: str,
) -> bytes:
    if (
        not asset_id.strip()
        or stored.asset.asset_id != asset_id.strip()
        or stored.asset.content_hash != content_hash.strip().casefold()
        or stored.asset.content_type != content_type
        or str(stored.asset.metadata.get("artifact_kind") or "")
        != artifact_kind
    ):
        raise ServerDeliveryPackageError(
            f"{label} identity is inconsistent"
        )
    return stored.data


class ServerDeliveryPackage:
    """Assemble the complete flat delivery ZIP from verified private assets."""

    def __init__(
        self,
        *,
        objects: ServerDeliveryObjectService,
    ) -> None:
        self._objects = objects

    def package(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        task: TaskRecord,
    ) -> TaskRecord:
        if not task.final_ai_check.confirmed:
            raise ServerDeliveryPackageError(
                "the final AI review must be confirmed"
            )
        if (
            not task.humanized_article.strip()
            or task.final_ai_check.article_hash
            != content_hash(task.humanized_article)
        ):
            raise ServerDeliveryPackageError(
                "the final AI review does not match the current article"
            )
        required_asset_ids = (
            task.docx_asset_id,
            task.tdk_asset_id,
            task.final_ai_check.screenshot_asset_id,
        )
        if any(not value.strip() for value in required_asset_ids):
            raise ServerDeliveryPackageError(
                "required Server delivery assets are missing"
            )
        if not task.images or any(
            not image.prepared_asset_id.strip()
            for image in task.images
        ):
            raise ServerDeliveryPackageError(
                "prepared article images are required for delivery"
            )
        article = self._objects.read_for_article_delivery(
            actor=actor,
            project_id=project_id,
            asset_id=task.docx_asset_id,
            max_bytes=MAX_SERVER_DOCX_BYTES,
        )
        article_data = _require_artifact(
            article,
            asset_id=task.docx_asset_id,
            content_hash=task.docx_content_hash,
            content_type=ARTICLE_DOCX_CONTENT_TYPE,
            artifact_kind=ARTICLE_DOCX_ARTIFACT_KIND,
            label="Article Word document",
        )
        tdk = self._objects.read_for_article_delivery(
            actor=actor,
            project_id=project_id,
            asset_id=task.tdk_asset_id,
            max_bytes=MAX_SERVER_TDK_DOCX_BYTES,
        )
        tdk_data = _require_artifact(
            tdk,
            asset_id=task.tdk_asset_id,
            content_hash=task.tdk_content_hash,
            content_type=ARTICLE_DOCX_CONTENT_TYPE,
            artifact_kind=TDK_DOCX_ARTIFACT_KIND,
            label="TDK Word document",
        )
        screenshot = self._objects.read_for_article_delivery(
            actor=actor,
            project_id=project_id,
            asset_id=task.final_ai_check.screenshot_asset_id,
            max_bytes=MAX_SERVER_AI_SCREENSHOT_BYTES,
        )
        screenshot_data = _require_artifact(
            screenshot,
            asset_id=task.final_ai_check.screenshot_asset_id,
            content_hash=(
                task.final_ai_check.screenshot_content_hash
            ),
            content_type="image/png",
            artifact_kind=FINAL_AI_SCREENSHOT_ARTIFACT_KIND,
            label="Final AI-rate screenshot",
        )
        if (
            screenshot.asset.width
            != task.final_ai_check.screenshot_width
            or screenshot.asset.height
            != task.final_ai_check.screenshot_height
        ):
            raise ServerDeliveryPackageError(
                "Final AI-rate screenshot identity is inconsistent"
            )

        images: list[tuple[str, bytes]] = []
        for image in task.images:
            stored = self._objects.read_for_article_delivery(
                actor=actor,
                project_id=project_id,
                asset_id=image.prepared_asset_id,
                max_bytes=MAX_SERVER_SOURCE_IMAGE_BYTES,
            )
            try:
                verified = verified_server_article_webp(
                    image,
                    stored,
                )
            except ValueError as exc:
                raise ServerDeliveryPackageError(str(exc)) from exc
            images.append((verified.filename, verified.data))

        try:
            archive = build_delivery_zip_bytes(
                article_docx=article_data,
                article_filename=task.docx_filename,
                tdk_docx=tdk_data,
                images=images,
                final_screenshot=screenshot_data,
            )
        except DeliveryPackageError as exc:
            raise ServerDeliveryPackageError(str(exc)) from exc
        if not archive or len(archive) > MAX_SERVER_DELIVERY_ZIP_BYTES:
            raise ServerDeliveryPackageError(
                "generated delivery ZIP exceeds the delivery limit"
            )

        digest = hashlib.sha256(archive).hexdigest()
        asset = self._objects.upload_delivery_zip(
            actor=actor,
            project_id=project_id,
            asset_id=f"asset_{digest}",
            data=archive,
        )
        if (
            asset.content_hash != digest
            or asset.content_type != "application/zip"
            or str(asset.metadata.get("artifact_kind") or "")
            != DELIVERY_ZIP_ARTIFACT_KIND
        ):
            raise ServerDeliveryPackageError(
                "stored delivery ZIP identity is inconsistent"
            )

        folder = official_website_folder_name(project_id)
        task.delivery_package_path = ""
        task.delivery_package_asset_id = asset.asset_id
        task.delivery_package_content_hash = asset.content_hash
        task.delivery_package_filename = (
            f"{folder}-topic_{task.topic_index:03d}.zip"
        )
        task.workflow_error = None
        return task


__all__ = [
    "MAX_SERVER_DELIVERY_ZIP_BYTES",
    "ServerDeliveryObjectService",
    "ServerDeliveryPackage",
    "ServerDeliveryPackageError",
]
