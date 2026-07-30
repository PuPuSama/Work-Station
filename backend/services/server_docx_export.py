from __future__ import annotations

import hashlib
import warnings
from io import BytesIO
from typing import Protocol

from PIL import Image, UnidentifiedImageError

from config import AppConfig
from knowledge_agent.assets import KnowledgeAsset
from knowledge_agent.object_storage import (
    ARTICLE_DOCX_ARTIFACT_KIND,
    ARTICLE_DOCX_CONTENT_TYPE,
    ProjectKnowledgeObject,
)
from models import STATUS_DOCX_EXPORTED, ArticleImage, TaskRecord
from services.access_control import ActorIdentity
from services.article_images import ArticleImageError
from services.article_validation import ArticleStructureError
from services.docx_export import (
    EmbeddedArticleImage,
    build_task_docx_bytes,
    safe_filename,
)
from services.server_article_images import (
    MAX_SERVER_IMAGE_PIXELS,
    MAX_SERVER_SOURCE_IMAGE_BYTES,
)
from workflow.state_machine import transition_task


MAX_SERVER_DOCX_BYTES = 32 * 1024 * 1024


class ServerArticleDocxError(ValueError):
    """A private Server Task cannot safely produce a Word document."""


class ServerArticleDocxObjectService(Protocol):
    """Private-object operations required by Server DOCX export."""

    def read_for_article_delivery(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        asset_id: str,
        max_bytes: int,
    ) -> ProjectKnowledgeObject: ...

    def upload_article_docx(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        asset_id: str,
        data: bytes,
    ) -> KnowledgeAsset: ...


def _verified_webp_payload(
    image: ArticleImage,
    stored: ProjectKnowledgeObject,
) -> EmbeddedArticleImage:
    asset_id = image.prepared_asset_id.strip()
    expected_hash = image.prepared_content_hash.strip().casefold()
    if (
        not asset_id
        or not isinstance(image.width, int)
        or isinstance(image.width, bool)
        or image.width <= 0
        or not isinstance(image.height, int)
        or isinstance(image.height, bool)
        or image.height <= 0
        or stored.asset.asset_id != asset_id
        or stored.asset.content_type.casefold() != "image/webp"
        or stored.asset.content_hash != expected_hash
        or stored.asset.width != image.width
        or stored.asset.height != image.height
        or not image.filename.strip()
    ):
        raise ServerArticleDocxError(
            "prepared article image metadata is inconsistent"
        )
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(stored.data)) as opened:
                if (
                    opened.format != "WEBP"
                    or opened.size != (image.width, image.height)
                    or opened.width * opened.height
                    > MAX_SERVER_IMAGE_PIXELS
                ):
                    raise ValueError("invalid WebP identity")
                opened.verify()
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        OSError,
        ValueError,
    ) as exc:
        raise ServerArticleDocxError(
            "prepared article image is not a valid WebP"
        ) from exc
    return EmbeddedArticleImage(
        asset_id=asset_id,
        data=stored.data,
        filename=image.filename,
        width=int(image.width),
        height=int(image.height),
    )


class ServerArticleDocxExport:
    """Render and store a DOCX without using a server-local Task directory."""

    def __init__(
        self,
        *,
        config: AppConfig,
        objects: ServerArticleDocxObjectService,
    ) -> None:
        self._config = config
        self._objects = objects

    def export(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        task: TaskRecord,
    ) -> TaskRecord:
        if not task.images:
            raise ServerArticleDocxError(
                "prepared article images are required for Word export"
            )
        embedded_images: dict[str, EmbeddedArticleImage] = {}
        for image in task.images:
            asset_id = image.prepared_asset_id.strip()
            if not asset_id:
                raise ServerArticleDocxError(
                    "prepared article image metadata is inconsistent"
                )
            stored = self._objects.read_for_article_delivery(
                actor=actor,
                project_id=project_id,
                asset_id=asset_id,
                max_bytes=MAX_SERVER_SOURCE_IMAGE_BYTES,
            )
            embedded_images[asset_id] = _verified_webp_payload(
                image,
                stored,
            )
        try:
            data = build_task_docx_bytes(
                self._config,
                task,
                embedded_images=embedded_images,
            )
        except (ArticleImageError, ArticleStructureError) as exc:
            raise ServerArticleDocxError(str(exc)) from exc
        if not data or len(data) > MAX_SERVER_DOCX_BYTES:
            raise ServerArticleDocxError(
                "generated Word document exceeds the delivery limit"
            )

        digest = hashlib.sha256(data).hexdigest()
        asset = self._objects.upload_article_docx(
            actor=actor,
            project_id=project_id,
            asset_id=f"asset_{digest}",
            data=data,
        )
        if (
            asset.content_hash != digest
            or asset.content_type != ARTICLE_DOCX_CONTENT_TYPE
            or str(asset.metadata.get("artifact_kind") or "")
            != ARTICLE_DOCX_ARTIFACT_KIND
        ):
            raise ServerArticleDocxError(
                "stored Word document identity is inconsistent"
            )

        title = task.selected_title or task.topic or "Article"
        task.docx_path = ""
        task.docx_asset_id = asset.asset_id
        task.docx_content_hash = asset.content_hash
        task.docx_filename = f"{safe_filename(title)}.docx"
        task.delivery_package_path = ""
        task.workflow_error = None
        transition_task(task, STATUS_DOCX_EXPORTED)
        return task


__all__ = [
    "MAX_SERVER_DOCX_BYTES",
    "ServerArticleDocxError",
    "ServerArticleDocxExport",
    "ServerArticleDocxObjectService",
]
