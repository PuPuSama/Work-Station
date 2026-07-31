from __future__ import annotations

import hashlib
import mimetypes
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import unquote, urlsplit

from services.access_control import (
    ActorIdentity,
    ProjectAccessService,
    ProjectPermission,
)
from services.object_store import (
    ObjectStore,
    ObjectStoreError,
    ObjectTooLarge,
    ProjectObjectUploader,
)

from .assets import (
    KnowledgeAsset,
    KnowledgeAssetConflictError,
    KnowledgeAssetRepository,
)


class KnowledgeObjectNotFound(LookupError):
    """Generic missing object error after project authorization succeeds."""


class KnowledgeObjectIntegrityError(ObjectStoreError):
    """Stored bytes no longer match their immutable database identity."""


@dataclass(frozen=True, slots=True)
class ProjectKnowledgeObject:
    """Authorized immutable asset bytes plus their persisted identity."""

    asset: KnowledgeAsset
    data: bytes


ARTICLE_DOCX_ARTIFACT_KIND = "article_docx"
ARTICLE_DOCX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument."
    "wordprocessingml.document"
)
TDK_DOCX_ARTIFACT_KIND = "tdk_docx"
INITIAL_AI_SCREENSHOT_ARTIFACT_KIND = "initial_ai_rate_screenshot"
FINAL_AI_SCREENSHOT_ARTIFACT_KIND = "final_ai_rate_screenshot"
DELIVERY_ZIP_ARTIFACT_KIND = "delivery_zip"
PRIVATE_TASK_ARTIFACT_KINDS = frozenset(
    {
        ARTICLE_DOCX_ARTIFACT_KIND,
        DELIVERY_ZIP_ARTIFACT_KIND,
        FINAL_AI_SCREENSHOT_ARTIFACT_KIND,
        INITIAL_AI_SCREENSHOT_ARTIFACT_KIND,
        TDK_DOCX_ARTIFACT_KIND,
    }
)


class ScopedS3ArtifactStore:
    """S3 adapter for the existing M2 parser/ingestion ArtifactStore contract.

    The caller constructs this only after authorization and binds it to one
    organization/project pair. A parser cannot redirect bytes through the
    legacy ``project_id`` method argument.
    """

    def __init__(
        self,
        *,
        store: ObjectStore,
        bucket: str,
        organization_id: str,
        project_id: str,
    ) -> None:
        self._organization_id = organization_id
        self._project_id = project_id
        self._uploader = ProjectObjectUploader(store, bucket=bucket)

    def put(
        self,
        *,
        project_id: str,
        namespace: str,
        content_hash: str,
        filename: str,
        content: bytes,
    ) -> str:
        if project_id != self._project_id:
            raise ValueError("artifact project does not match the bound scope")
        body = bytes(content)
        if hashlib.sha256(body).hexdigest() != content_hash:
            raise ValueError("content_hash does not match content bytes")
        # Namespace and filename remain evidence metadata in PostgreSQL. The
        # immutable object key is based on tenant scope and bytes only.
        del namespace
        content_type = (
            mimetypes.guess_type(filename, strict=False)[0]
            or "application/octet-stream"
        )
        return self._uploader.upload(
            organization_id=self._organization_id,
            project_id=self._project_id,
            data=body,
            content_type=content_type,
        ).object_uri


def _s3_key(uri: str, expected_bucket: str) -> str:
    parsed = urlsplit(uri)
    if (
        parsed.scheme != "s3"
        or parsed.netloc != expected_bucket
        or not parsed.path.lstrip("/")
    ):
        raise ValueError("knowledge asset is not in the configured object store")
    return unquote(parsed.path.lstrip("/"))


class ProjectKnowledgeObjectService:
    """Authorize, upload, persist, and sign immutable knowledge assets."""

    def __init__(
        self,
        *,
        store: ObjectStore,
        bucket: str,
        repository: KnowledgeAssetRepository,
        access: ProjectAccessService,
    ) -> None:
        self._store = store
        self._bucket = bucket.strip()
        if not self._bucket:
            raise ValueError("bucket is required")
        self._uploader = ProjectObjectUploader(store, bucket=self._bucket)
        self._repository = repository
        self._access = access

    def upload(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        asset_id: str,
        data: bytes,
        content_type: str,
        width: int | None = None,
        height: int | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> KnowledgeAsset:
        self._access.require(actor, project_id, "knowledge.edit")
        return self._store_asset(
            actor=actor,
            project_id=project_id,
            asset_id=asset_id,
            data=data,
            content_type=content_type,
            width=width,
            height=height,
            metadata=metadata,
        )

    def upload_article_derivative(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        asset_id: str,
        data: bytes,
        width: int,
        height: int,
        metadata: Mapping[str, object] | None = None,
    ) -> KnowledgeAsset:
        """Persist a re-creatable WebP after reauthorizing article mutation.

        Metadata here must describe the immutable bytes. Article-specific
        source, role, product, and placement relations belong on ArticleImage.
        """

        self._access.require(actor, project_id, "article.edit")
        derivative_metadata = dict(metadata or {})
        derivative_metadata["derivative_kind"] = "article_image_webp"
        return self._store_asset(
            actor=actor,
            project_id=project_id,
            asset_id=asset_id,
            data=data,
            content_type="image/webp",
            width=width,
            height=height,
            metadata=derivative_metadata,
        )

    def upload_article_docx(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        asset_id: str,
        data: bytes,
    ) -> KnowledgeAsset:
        """Persist a content-addressed private Word document."""

        self._access.require(actor, project_id, "article.deliver")
        return self._store_asset(
            actor=actor,
            project_id=project_id,
            asset_id=asset_id,
            data=data,
            content_type=ARTICLE_DOCX_CONTENT_TYPE,
            width=None,
            height=None,
            metadata={"artifact_kind": ARTICLE_DOCX_ARTIFACT_KIND},
        )

    def upload_tdk_docx(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        asset_id: str,
        data: bytes,
    ) -> KnowledgeAsset:
        """Persist a content-addressed private TDK reference document."""

        self._access.require(actor, project_id, "article.deliver")
        return self._store_asset(
            actor=actor,
            project_id=project_id,
            asset_id=asset_id,
            data=data,
            content_type=ARTICLE_DOCX_CONTENT_TYPE,
            width=None,
            height=None,
            metadata={"artifact_kind": TDK_DOCX_ARTIFACT_KIND},
        )

    def upload_final_ai_screenshot(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        asset_id: str,
        data: bytes,
        width: int,
        height: int,
    ) -> KnowledgeAsset:
        """Persist a normalized final AI-rate review screenshot."""

        self._access.require(actor, project_id, "article.review")
        return self._store_asset(
            actor=actor,
            project_id=project_id,
            asset_id=asset_id,
            data=data,
            content_type="image/png",
            width=width,
            height=height,
            metadata={
                "artifact_kind": FINAL_AI_SCREENSHOT_ARTIFACT_KIND
            },
        )

    def upload_initial_ai_screenshot(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        asset_id: str,
        data: bytes,
        width: int,
        height: int,
    ) -> KnowledgeAsset:
        """Persist a normalized initial AI-rate review screenshot."""

        self._access.require(actor, project_id, "article.review")
        return self._store_asset(
            actor=actor,
            project_id=project_id,
            asset_id=asset_id,
            data=data,
            content_type="image/png",
            width=width,
            height=height,
            metadata={
                "artifact_kind": INITIAL_AI_SCREENSHOT_ARTIFACT_KIND
            },
        )

    def upload_delivery_zip(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        asset_id: str,
        data: bytes,
    ) -> KnowledgeAsset:
        """Persist one content-addressed private delivery archive."""

        self._access.require(actor, project_id, "article.deliver")
        return self._store_asset(
            actor=actor,
            project_id=project_id,
            asset_id=asset_id,
            data=data,
            content_type="application/zip",
            width=None,
            height=None,
            metadata={"artifact_kind": DELIVERY_ZIP_ARTIFACT_KIND},
        )

    def _store_asset(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        asset_id: str,
        data: bytes,
        content_type: str,
        width: int | None,
        height: int | None,
        metadata: Mapping[str, object] | None,
    ) -> KnowledgeAsset:
        body = bytes(data)
        digest = hashlib.sha256(body).hexdigest()
        existing = self._repository.get_asset(project_id, asset_id)
        if existing is not None:
            if existing.content_hash != digest:
                raise KnowledgeAssetConflictError(
                    "asset ID is already used by different content"
                )
            # Do not let an older or manually corrupted catalog row bypass the
            # organization/project boundary merely because its content hash
            # matches an idempotent retry.
            self._scoped_key(
                actor=actor,
                project_id=project_id,
                asset=existing,
            )
            return existing

        upload = self._uploader.upload(
            organization_id=actor.organization_id,
            project_id=project_id,
            data=body,
            content_type=content_type,
            # User-authored metadata remains in PostgreSQL. S3 user metadata is
            # an HTTP-header surface with narrower character rules; the object
            # store itself adds only the verified SHA-256 value.
            metadata=None,
        )
        asset_metadata = dict(metadata or {})
        asset_metadata.update(
            {
                "organization_id": actor.organization_id,
                "created_by_user_id": actor.user_id,
                "object_key": upload.stored.key,
                "object_etag": upload.stored.etag,
            }
        )
        return self._repository.put_asset(
            KnowledgeAsset(
                project_id=project_id,
                asset_id=asset_id,
                content_hash=upload.stored.content_hash,
                artifact_uri=upload.object_uri,
                content_type=upload.stored.content_type,
                byte_size=upload.stored.byte_size,
                width=width,
                height=height,
                metadata=asset_metadata,
            )
        )

    def read_for_article_edit(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        asset_id: str,
        max_bytes: int,
    ) -> ProjectKnowledgeObject:
        """Reauthorize and verify private bytes before article derivation."""

        return self._read_verified(
            actor=actor,
            project_id=project_id,
            asset_id=asset_id,
            max_bytes=max_bytes,
            permission="article.edit",
        )

    def read_for_article_delivery(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        asset_id: str,
        max_bytes: int,
    ) -> ProjectKnowledgeObject:
        """Reauthorize and verify private bytes before delivery rendering."""

        return self._read_verified(
            actor=actor,
            project_id=project_id,
            asset_id=asset_id,
            max_bytes=max_bytes,
            permission="article.deliver",
        )

    def _read_verified(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        asset_id: str,
        max_bytes: int,
        permission: ProjectPermission,
    ) -> ProjectKnowledgeObject:
        self._access.require(actor, project_id, permission)
        if max_bytes <= 0:
            raise ValueError("max_bytes must be greater than zero")
        asset = self._repository.get_asset(project_id, asset_id)
        if asset is None:
            raise KnowledgeObjectNotFound("knowledge object not found")
        key = self._scoped_key(
            actor=actor,
            project_id=project_id,
            asset=asset,
        )
        if asset.byte_size > max_bytes:
            raise ObjectTooLarge("object exceeds the requested size limit")
        body = self._store.get(key, max_bytes=max_bytes)
        if (
            len(body) != asset.byte_size
            or hashlib.sha256(body).hexdigest() != asset.content_hash
        ):
            raise KnowledgeObjectIntegrityError(
                "knowledge object integrity verification failed"
            )
        return ProjectKnowledgeObject(asset=asset, data=body)

    def _scoped_key(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        asset: KnowledgeAsset,
    ) -> str:
        key = _s3_key(asset.artifact_uri, self._bucket)
        expected_prefix = (
            f"organizations/{actor.organization_id}/projects/{project_id}/"
        )
        if not key.startswith(expected_prefix):
            raise KnowledgeObjectNotFound("knowledge object not found")
        return key

    def create_download_url(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        asset_id: str,
        expires_seconds: int = 300,
    ) -> str:
        self._access.require(actor, project_id, "project.view")
        asset = self._repository.get_asset(project_id, asset_id)
        if asset is None:
            raise KnowledgeObjectNotFound("knowledge object not found")
        artifact_kind = str(
            asset.metadata.get("artifact_kind") or ""
        )
        if artifact_kind in PRIVATE_TASK_ARTIFACT_KINDS:
            # Private Task artifacts require a dedicated authorized route;
            # knowing their Asset ID must not downgrade access to project.view.
            raise KnowledgeObjectNotFound("knowledge object not found")
        key = self._scoped_key(
            actor=actor,
            project_id=project_id,
            asset=asset,
        )
        return self._store.create_download_url(
            key,
            expires_seconds=expires_seconds,
        )

    def create_article_docx_download_url(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        asset_id: str,
        expires_seconds: int = 300,
    ) -> str:
        """Sign one DOCX only after a fresh article.deliver decision."""

        self._access.require(actor, project_id, "article.deliver")
        asset = self._repository.get_asset(project_id, asset_id)
        if (
            asset is None
            or str(asset.metadata.get("artifact_kind") or "")
            != ARTICLE_DOCX_ARTIFACT_KIND
            or asset.content_type != ARTICLE_DOCX_CONTENT_TYPE
        ):
            raise KnowledgeObjectNotFound("knowledge object not found")
        key = self._scoped_key(
            actor=actor,
            project_id=project_id,
            asset=asset,
        )
        return self._store.create_download_url(
            key,
            expires_seconds=expires_seconds,
        )

    def create_tdk_docx_download_url(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        asset_id: str,
        expires_seconds: int = 300,
    ) -> str:
        """Sign one TDK DOCX only after a fresh article.deliver decision."""

        self._access.require(actor, project_id, "article.deliver")
        asset = self._repository.get_asset(project_id, asset_id)
        if (
            asset is None
            or str(asset.metadata.get("artifact_kind") or "")
            != TDK_DOCX_ARTIFACT_KIND
            or asset.content_type != ARTICLE_DOCX_CONTENT_TYPE
        ):
            raise KnowledgeObjectNotFound("knowledge object not found")
        key = self._scoped_key(
            actor=actor,
            project_id=project_id,
            asset=asset,
        )
        return self._store.create_download_url(
            key,
            expires_seconds=expires_seconds,
        )

    def create_final_ai_screenshot_download_url(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        asset_id: str,
        content_hash: str,
        width: int,
        height: int,
        expires_seconds: int = 300,
    ) -> str:
        """Sign a final-review screenshot after fresh review permission."""

        self._access.require(actor, project_id, "article.review")
        asset = self._repository.get_asset(project_id, asset_id)
        if (
            asset is None
            or str(asset.metadata.get("artifact_kind") or "")
            != FINAL_AI_SCREENSHOT_ARTIFACT_KIND
            or asset.content_type != "image/png"
            or asset.content_hash != content_hash.strip().casefold()
            or asset.width != width
            or asset.height != height
        ):
            raise KnowledgeObjectNotFound("knowledge object not found")
        key = self._scoped_key(
            actor=actor,
            project_id=project_id,
            asset=asset,
        )
        return self._store.create_download_url(
            key,
            expires_seconds=expires_seconds,
        )

    def create_initial_ai_screenshot_download_url(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        asset_id: str,
        content_hash: str,
        width: int,
        height: int,
        expires_seconds: int = 300,
    ) -> str:
        """Sign an initial-review screenshot after fresh review permission."""

        self._access.require(actor, project_id, "article.review")
        asset = self._repository.get_asset(project_id, asset_id)
        if (
            asset is None
            or str(asset.metadata.get("artifact_kind") or "")
            != INITIAL_AI_SCREENSHOT_ARTIFACT_KIND
            or asset.content_type != "image/png"
            or asset.content_hash != content_hash.strip().casefold()
            or asset.width != width
            or asset.height != height
        ):
            raise KnowledgeObjectNotFound("knowledge object not found")
        key = self._scoped_key(
            actor=actor,
            project_id=project_id,
            asset=asset,
        )
        return self._store.create_download_url(
            key,
            expires_seconds=expires_seconds,
        )

    def create_delivery_zip_download_url(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        asset_id: str,
        content_hash: str,
        expires_seconds: int = 300,
    ) -> str:
        """Sign one complete archive after a fresh delivery decision."""

        self._access.require(actor, project_id, "article.deliver")
        asset = self._repository.get_asset(project_id, asset_id)
        if (
            asset is None
            or str(asset.metadata.get("artifact_kind") or "")
            != DELIVERY_ZIP_ARTIFACT_KIND
            or asset.content_type != "application/zip"
            or asset.content_hash != content_hash.strip().casefold()
        ):
            raise KnowledgeObjectNotFound("knowledge object not found")
        key = self._scoped_key(
            actor=actor,
            project_id=project_id,
            asset=asset,
        )
        return self._store.create_download_url(
            key,
            expires_seconds=expires_seconds,
        )


__all__ = [
    "ARTICLE_DOCX_ARTIFACT_KIND",
    "ARTICLE_DOCX_CONTENT_TYPE",
    "DELIVERY_ZIP_ARTIFACT_KIND",
    "FINAL_AI_SCREENSHOT_ARTIFACT_KIND",
    "INITIAL_AI_SCREENSHOT_ARTIFACT_KIND",
    "KnowledgeObjectIntegrityError",
    "KnowledgeObjectNotFound",
    "ProjectKnowledgeObject",
    "ProjectKnowledgeObjectService",
    "PRIVATE_TASK_ARTIFACT_KINDS",
    "ScopedS3ArtifactStore",
    "TDK_DOCX_ARTIFACT_KIND",
]
