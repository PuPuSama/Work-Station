from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping
from io import BytesIO
from typing import Protocol
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile, ZipInfo

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
from services.delivery_metadata import (
    DELIVERY_METADATA_FILENAME,
    build_delivery_metadata,
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
from services.tdk import current_article
from storage import content_hash


MAX_SERVER_DELIVERY_ZIP_BYTES = 128 * 1024 * 1024
MAX_SERVER_BATCH_DELIVERY_ZIP_BYTES = 256 * 1024 * 1024
MAX_SERVER_BATCH_DELIVERY_TASKS = 100
MAX_SERVER_BATCH_DELIVERY_ENTRIES = 1000


class ServerDeliveryPackageError(ValueError):
    """A complete Server delivery archive cannot be assembled safely."""


class _BatchArchiveTooLarge(ServerDeliveryPackageError):
    """The aggregate archive exceeded its bounded in-memory output."""


class _BoundedBytesIO(BytesIO):
    """A BytesIO that prevents zipfile from growing past the server limit."""

    def __init__(self, max_bytes: int) -> None:
        super().__init__()
        self._max_bytes = max_bytes

    def write(self, data: bytes) -> int:
        current_size = self.getbuffer().nbytes
        if max(self.tell() + len(data), current_size) > self._max_bytes:
            raise _BatchArchiveTooLarge(
                "generated batch delivery ZIP exceeds the delivery limit"
            )
        return super().write(data)


def _write_deterministic_zip_entry(
    archive: ZipFile,
    filename: str,
    data: bytes,
) -> None:
    info = ZipInfo(filename, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    info.create_system = 3
    archive.writestr(info, data, compress_type=ZIP_DEFLATED, compresslevel=6)


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
    content_type: str | None,
    artifact_kind: str,
    label: str,
) -> bytes:
    if (
        not asset_id.strip()
        or stored.asset.asset_id != asset_id.strip()
        or stored.asset.content_hash != content_hash.strip().casefold()
        or (
            content_type is not None
            and stored.asset.content_type != content_type
        )
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
        screenshot_deferred = (
            task.final_ai_check.deferred
            and not task.final_ai_check.confirmed
        ) or (
            task.humanization_skipped
            and task.final_ai_check.confirmed
            and not task.final_ai_check.screenshot_asset_id.strip()
        )
        if not task.final_ai_check.confirmed and not screenshot_deferred:
            raise ServerDeliveryPackageError(
                "the final AI review must be confirmed or explicitly deferred"
            )
        if (
            not task.humanized_article.strip()
            or task.final_ai_check.article_hash
            != content_hash(task.humanized_article)
        ):
            raise ServerDeliveryPackageError(
                "the final AI review does not match the current article"
            )
        required_asset_ids = [
            task.docx_asset_id,
            task.tdk_asset_id,
        ]
        if not screenshot_deferred:
            required_asset_ids.append(
                task.final_ai_check.screenshot_asset_id
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
        screenshot_data: bytes | None = None
        if not screenshot_deferred:
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
                content_type=None,
                artifact_kind=FINAL_AI_SCREENSHOT_ARTIFACT_KIND,
                label="Final AI-rate screenshot",
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

        article_text = current_article(task)
        folder = official_website_folder_name(project_id)
        delivery_filename = f"{folder}-topic_{task.topic_index:03d}.zip"
        metadata = build_delivery_metadata(
            task,
            article=article_text,
            project_id=project_id,
            delivery_filename=delivery_filename,
        )
        try:
            archive = build_delivery_zip_bytes(
                article_docx=article_data,
                article_filename=task.docx_filename,
                tdk_docx=tdk_data,
                images=images,
                final_screenshot=screenshot_data,
                final_screenshot_filename=(
                    task.final_ai_check.screenshot_filename
                    or "final-ai-rate.png"
                ),
                metadata=metadata,
                metadata_filename=DELIVERY_METADATA_FILENAME,
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

        task.delivery_package_path = ""
        task.delivery_package_asset_id = asset.asset_id
        task.delivery_package_content_hash = asset.content_hash
        task.delivery_package_filename = delivery_filename
        task.workflow_error = None
        return task


def _safe_batch_component(value: object, fallback: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).strip()
    normalized = re.sub(r"[\x00-\x1f\x7f]+", "-", normalized)
    normalized = normalized.replace("/", "-").replace("\\", "-")
    normalized = re.sub(
        r"[^A-Za-z0-9._\-\u3400-\u9fff]+",
        "-",
        normalized,
    )
    normalized = re.sub(r"-{2,}", "-", normalized).strip(" .-_")
    return normalized[:80].rstrip(" .-_") or fallback


def _safe_batch_member_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    if (
        not normalized
        or len(normalized) > 255
        or normalized.startswith(("/", "\\"))
        or "\\" in normalized
        or ":" in normalized
        or any(ord(character) < 32 for character in normalized)
    ):
        raise ServerDeliveryPackageError(
            "batch delivery contains an unsafe archive filename"
        )
    parts = normalized.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise ServerDeliveryPackageError(
            "batch delivery contains an unsafe archive path"
        )
    return normalized


class ServerBatchDeliveryPackage:
    """Combine completed per-article delivery packages into one ZIP.

    ``project_id`` is the authorized scope used to store the aggregate;
    ``task_project_ids`` optionally identifies the source scope for each task
    when a plan spans multiple projects.
    """

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
        tasks: list[TaskRecord] | tuple[TaskRecord, ...],
        task_project_ids: Mapping[str, str] | None = None,
    ) -> KnowledgeAsset:
        normalized_tasks = list(tasks)
        if not normalized_tasks:
            raise ServerDeliveryPackageError(
                "at least one completed delivery package is required"
            )
        if len(normalized_tasks) > MAX_SERVER_BATCH_DELIVERY_TASKS:
            raise ServerDeliveryPackageError(
                "batch delivery contains too many articles"
            )

        seen_task_ids: set[str] = set()
        used_folders: set[str] = set()
        manifest_items: list[dict[str, object]] = []
        output = _BoundedBytesIO(MAX_SERVER_BATCH_DELIVERY_ZIP_BYTES)
        entry_count = 0
        uncompressed_bytes = 0

        try:
            with ZipFile(
                output,
                "w",
                compression=ZIP_DEFLATED,
                compresslevel=6,
            ) as archive:
                for task in normalized_tasks:
                    task_id = str(task.id or "").strip()
                    if not task_id or task_id in seen_task_ids:
                        raise ServerDeliveryPackageError(
                            "batch delivery contains duplicate or missing task identities"
                        )
                    seen_task_ids.add(task_id)

                    asset_id = str(task.delivery_package_asset_id or "").strip()
                    expected_hash = str(
                        task.delivery_package_content_hash or ""
                    ).strip().casefold()
                    if not asset_id or not expected_hash:
                        raise ServerDeliveryPackageError(
                            f"delivery package is missing for task {task_id}"
                        )
                    source_project_id = str(
                        (task_project_ids or {}).get(task_id, project_id) or ""
                    ).strip()
                    if not source_project_id:
                        raise ServerDeliveryPackageError(
                            f"delivery package is missing a source project for task {task_id}"
                        )
                    stored = self._objects.read_for_article_delivery(
                        actor=actor,
                        project_id=source_project_id,
                        asset_id=asset_id,
                        max_bytes=MAX_SERVER_DELIVERY_ZIP_BYTES,
                    )
                    if (
                        stored.asset.asset_id != asset_id
                        or stored.asset.content_hash != expected_hash
                        or stored.asset.content_type != "application/zip"
                        or str(stored.asset.metadata.get("artifact_kind") or "")
                        != DELIVERY_ZIP_ARTIFACT_KIND
                    ):
                        raise ServerDeliveryPackageError(
                            f"delivery package identity is inconsistent for task {task_id}"
                        )

                    topic_index = max(0, int(task.topic_index))
                    try:
                        website_folder = official_website_folder_name(
                            source_project_id,
                        )
                    except DeliveryPackageError as exc:
                        raise ServerDeliveryPackageError(
                            f"delivery package has an invalid source project for task {task_id}"
                        ) from exc
                    folder = (
                        f"{website_folder}-topic_{topic_index:03d}"
                    )
                    base_folder = folder
                    suffix = 2
                    while folder.casefold() in used_folders:
                        folder = f"{base_folder}-{suffix}"
                        suffix += 1
                    used_folders.add(folder.casefold())

                    package_entry_names: set[str] = set()
                    has_metadata = False
                    try:
                        source = ZipFile(BytesIO(stored.data))
                    except (BadZipFile, OSError, ValueError) as exc:
                        raise ServerDeliveryPackageError(
                            f"delivery package is not a valid ZIP for task {task_id}"
                        ) from exc
                    with source:
                        for info in source.infolist():
                            member_name = _safe_batch_member_name(info.filename)
                            if info.is_dir():
                                continue
                            member_key = member_name.casefold()
                            if member_key in package_entry_names:
                                raise ServerDeliveryPackageError(
                                    f"delivery package contains duplicate files for task {task_id}"
                                )
                            package_entry_names.add(member_key)
                            if member_key == DELIVERY_METADATA_FILENAME.casefold():
                                has_metadata = True
                            if info.file_size > MAX_SERVER_DELIVERY_ZIP_BYTES:
                                raise ServerDeliveryPackageError(
                                    f"delivery package contains an oversized file for task {task_id}"
                                )
                            uncompressed_bytes += info.file_size
                            if (
                                uncompressed_bytes
                                > MAX_SERVER_BATCH_DELIVERY_ZIP_BYTES
                            ):
                                raise _BatchArchiveTooLarge(
                                    "batch delivery contains too much uncompressed content"
                                )
                            try:
                                member_data = source.read(info)
                            except (BadZipFile, OSError, RuntimeError) as exc:
                                raise ServerDeliveryPackageError(
                                    f"delivery package could not be read for task {task_id}"
                                ) from exc
                            if len(member_data) != info.file_size:
                                raise ServerDeliveryPackageError(
                                    f"delivery package integrity check failed for task {task_id}"
                                )
                            entry_count += 1
                            if entry_count > MAX_SERVER_BATCH_DELIVERY_ENTRIES:
                                raise ServerDeliveryPackageError(
                                    "batch delivery contains too many files"
                                )
                            _write_deterministic_zip_entry(
                                archive,
                                f"{folder}/{member_name}",
                                member_data,
                            )

                    # Packages created before metadata support are still
                    # valid source assets. Add the record while composing a
                    # new batch archive so an old article becomes complete
                    # on its next workflow-assistant download.
                    if not has_metadata and isinstance(task, TaskRecord):
                        metadata = build_delivery_metadata(
                            task,
                            article=current_article(task),
                            project_id=source_project_id,
                            delivery_filename=(
                                task.delivery_package_filename
                                or f"{website_folder}-topic_{topic_index:03d}.zip"
                            ),
                        )
                        uncompressed_bytes += len(metadata)
                        if (
                            uncompressed_bytes
                            > MAX_SERVER_BATCH_DELIVERY_ZIP_BYTES
                        ):
                            raise _BatchArchiveTooLarge(
                                "batch delivery contains too much uncompressed content"
                            )
                        entry_count += 1
                        if entry_count > MAX_SERVER_BATCH_DELIVERY_ENTRIES:
                            raise ServerDeliveryPackageError(
                                "batch delivery contains too many files"
                            )
                        _write_deterministic_zip_entry(
                            archive,
                            f"{folder}/{DELIVERY_METADATA_FILENAME}",
                            metadata,
                        )

                    manifest_items.append(
                        {
                            "project_id": source_project_id,
                            "task_id": task_id,
                            "topic_index": topic_index,
                            "folder": folder,
                            "delivery_filename": _safe_batch_component(
                                task.delivery_package_filename,
                                f"{folder}.zip",
                            ),
                        }
                    )

                manifest = json.dumps(
                    {
                        "schema_version": 1,
                        "items": manifest_items,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                entry_count += 1
                if entry_count > MAX_SERVER_BATCH_DELIVERY_ENTRIES:
                    raise ServerDeliveryPackageError(
                        "batch delivery contains too many files"
                    )
                _write_deterministic_zip_entry(
                    archive,
                    "manifest.json",
                    manifest,
                )
        except _BatchArchiveTooLarge:
            raise
        except ServerDeliveryPackageError:
            raise
        except (BadZipFile, OSError, RuntimeError) as exc:
            raise ServerDeliveryPackageError(
                "batch delivery ZIP could not be assembled"
            ) from exc

        archive_data = output.getvalue()
        if (
            not archive_data
            or len(archive_data) > MAX_SERVER_BATCH_DELIVERY_ZIP_BYTES
        ):
            raise ServerDeliveryPackageError(
                "generated batch delivery ZIP exceeds the delivery limit"
            )
        digest = hashlib.sha256(archive_data).hexdigest()
        asset = self._objects.upload_delivery_zip(
            actor=actor,
            project_id=project_id,
            asset_id=f"asset_{digest}",
            data=archive_data,
        )
        if (
            asset.content_hash != digest
            or asset.content_type != "application/zip"
            or str(asset.metadata.get("artifact_kind") or "")
            != DELIVERY_ZIP_ARTIFACT_KIND
        ):
            raise ServerDeliveryPackageError(
                "stored batch delivery ZIP identity is inconsistent"
            )
        return asset


__all__ = [
    "MAX_SERVER_BATCH_DELIVERY_ENTRIES",
    "MAX_SERVER_BATCH_DELIVERY_TASKS",
    "MAX_SERVER_BATCH_DELIVERY_ZIP_BYTES",
    "MAX_SERVER_DELIVERY_ZIP_BYTES",
    "ServerDeliveryObjectService",
    "ServerBatchDeliveryPackage",
    "ServerDeliveryPackage",
    "ServerDeliveryPackageError",
]
