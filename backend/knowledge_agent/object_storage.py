from __future__ import annotations

import hashlib
import mimetypes
from typing import Mapping
from urllib.parse import unquote, urlsplit

from services.access_control import ActorIdentity, ProjectAccessService
from services.object_store import (
    ObjectStore,
    ProjectObjectUploader,
)

from .assets import (
    KnowledgeAsset,
    KnowledgeAssetConflictError,
    KnowledgeAssetRepository,
)


class KnowledgeObjectNotFound(LookupError):
    """Generic missing object error after project authorization succeeds."""


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
        body = bytes(data)
        digest = hashlib.sha256(body).hexdigest()
        existing = self._repository.get_asset(project_id, asset_id)
        if existing is not None:
            if existing.content_hash != digest:
                raise KnowledgeAssetConflictError(
                    "asset ID is already used by different content"
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
        key = _s3_key(asset.artifact_uri, self._bucket)
        expected_prefix = (
            f"organizations/{actor.organization_id}/projects/{project_id}/"
        )
        if not key.startswith(expected_prefix):
            raise KnowledgeObjectNotFound("knowledge object not found")
        return self._store.create_download_url(
            key,
            expires_seconds=expires_seconds,
        )


__all__ = [
    "KnowledgeObjectNotFound",
    "ProjectKnowledgeObjectService",
    "ScopedS3ArtifactStore",
]
