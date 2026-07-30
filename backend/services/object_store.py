from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from typing import Mapping, Protocol
from urllib.parse import urlsplit

import boto3
from botocore.client import BaseClient
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError


_SCOPE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_METADATA_KEY = re.compile(r"[^a-z0-9-]+")


class ObjectStoreError(RuntimeError):
    """Stable object-store failure that does not expose provider details."""


class ObjectTooLarge(ObjectStoreError):
    """Raised before returning an object larger than the caller's limit."""


def _required_text(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized


def _scope_id(value: str, field_name: str) -> str:
    normalized = _required_text(value, field_name)
    if (
        not _SCOPE_ID.fullmatch(normalized)
        or normalized in {".", ".."}
        or "/" in normalized
        or "\\" in normalized
    ):
        raise ValueError(f"{field_name} contains unsupported characters")
    return normalized


def _sha256(value: str) -> str:
    normalized = value.strip().lower()
    if not _SHA256.fullmatch(normalized):
        raise ValueError("content_hash must be a lowercase SHA-256 digest")
    return normalized


def _object_key(value: str) -> str:
    normalized = _required_text(value, "key")
    segments = normalized.split("/")
    if (
        len(normalized) > 1024
        or normalized.startswith("/")
        or "\\" in normalized
        or any(segment in {"", ".", ".."} for segment in segments)
    ):
        raise ValueError("key is not a safe object path")
    return normalized


def _endpoint_url(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        return ""
    parsed = urlsplit(normalized)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(
            "endpoint_url must be an absolute HTTP(S) URL"
        ) from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or port is not None
        and not 0 < port < 65536
    ):
        raise ValueError("endpoint_url must be an absolute HTTP(S) URL")
    return normalized.rstrip("/")


def build_project_object_key(
    organization_id: str,
    project_id: str,
    content_hash: str,
) -> str:
    organization = _scope_id(organization_id, "organization_id")
    project = _scope_id(project_id, "project_id")
    digest = _sha256(content_hash)
    return (
        f"organizations/{organization}/projects/{project}/"
        f"blobs/{digest[:2]}/{digest}"
    )


@dataclass(frozen=True)
class StoredObject:
    key: str
    content_hash: str
    content_type: str
    byte_size: int
    etag: str = ""


class ObjectStore(Protocol):
    def check_ready(self) -> None: ...

    def put(
        self,
        *,
        key: str,
        data: bytes,
        content_type: str,
        metadata: Mapping[str, str] | None = None,
    ) -> StoredObject: ...

    def get(self, key: str, *, max_bytes: int) -> bytes: ...

    def create_download_url(
        self,
        key: str,
        *,
        expires_seconds: int,
    ) -> str: ...

    def delete(self, key: str) -> None: ...


def _environment_bool(
    environment: Mapping[str, str],
    name: str,
    default: bool,
) -> bool:
    raw = environment.get(name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean value")


@dataclass(frozen=True)
class S3ObjectStoreSettings:
    bucket: str
    region: str = "us-east-1"
    endpoint_url: str = ""
    access_key_id: str = field(default="", repr=False)
    secret_access_key: str = field(default="", repr=False)
    force_path_style: bool = True
    server_side_encryption: str = "AES256"
    kms_key_id: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "bucket", _required_text(self.bucket, "bucket"))
        object.__setattr__(self, "region", _required_text(self.region, "region"))
        object.__setattr__(
            self,
            "endpoint_url",
            _endpoint_url(self.endpoint_url),
        )
        if bool(self.access_key_id) != bool(self.secret_access_key):
            raise ValueError(
                "object store access key and secret must be configured together"
            )
        if self.server_side_encryption not in {"", "AES256", "aws:kms"}:
            raise ValueError(
                "server_side_encryption must be AES256, aws:kms, or empty"
            )
        if self.server_side_encryption == "aws:kms" and not self.kms_key_id:
            raise ValueError("kms_key_id is required for aws:kms encryption")

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> S3ObjectStoreSettings:
        source = os.environ if environment is None else environment
        configured_sse = source.get(
            "ARTICLE_AGENT_OBJECT_STORE_SSE",
            "AES256",
        ).strip()
        if configured_sse.casefold() == "none":
            configured_sse = ""
        return cls(
            bucket=source.get("ARTICLE_AGENT_OBJECT_STORE_BUCKET", ""),
            region=source.get(
                "ARTICLE_AGENT_OBJECT_STORE_REGION",
                "us-east-1",
            ),
            endpoint_url=source.get(
                "ARTICLE_AGENT_OBJECT_STORE_ENDPOINT",
                "",
            ).strip(),
            access_key_id=source.get(
                "ARTICLE_AGENT_OBJECT_STORE_ACCESS_KEY",
                "",
            ).strip(),
            secret_access_key=source.get(
                "ARTICLE_AGENT_OBJECT_STORE_SECRET_KEY",
                "",
            ).strip(),
            force_path_style=_environment_bool(
                source,
                "ARTICLE_AGENT_OBJECT_STORE_FORCE_PATH_STYLE",
                True,
            ),
            server_side_encryption=configured_sse,
            kms_key_id=source.get(
                "ARTICLE_AGENT_OBJECT_STORE_KMS_KEY_ID",
                "",
            ).strip(),
        )


def _metadata(
    values: Mapping[str, str] | None,
) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_key, raw_value in (values or {}).items():
        key = _METADATA_KEY.sub("-", str(raw_key).strip().lower()).strip("-")
        if not key:
            raise ValueError("object metadata key is invalid")
        value = str(raw_value)
        if "\r" in value or "\n" in value:
            raise ValueError("object metadata values must be one line")
        try:
            value.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError(
                "object metadata values must contain ASCII characters only"
            ) from exc
        result[key[:128]] = value[:1024]
    return result


class S3ObjectStore:
    """Small S3-compatible adapter with no public ACL behavior."""

    def __init__(
        self,
        settings: S3ObjectStoreSettings,
        *,
        client: BaseClient | None = None,
    ) -> None:
        self.settings = settings
        self._client = client or boto3.client(
            "s3",
            region_name=settings.region,
            endpoint_url=settings.endpoint_url or None,
            aws_access_key_id=settings.access_key_id or None,
            aws_secret_access_key=settings.secret_access_key or None,
            config=Config(
                signature_version="s3v4",
                s3={
                    "addressing_style": (
                        "path" if settings.force_path_style else "auto"
                    )
                },
            ),
        )

    def check_ready(self) -> None:
        try:
            self._client.head_bucket(Bucket=self.settings.bucket)
        except (BotoCoreError, ClientError) as exc:
            raise ObjectStoreError("object store readiness check failed") from exc

    def put(
        self,
        *,
        key: str,
        data: bytes,
        content_type: str,
        metadata: Mapping[str, str] | None = None,
    ) -> StoredObject:
        normalized_key = _object_key(key)
        normalized_content_type = _required_text(content_type, "content_type")
        body = bytes(data)
        if not body:
            raise ValueError("object data must not be empty")
        digest = hashlib.sha256(body).hexdigest()
        arguments: dict[str, object] = {
            "Bucket": self.settings.bucket,
            "Key": normalized_key,
            "Body": body,
            "ContentType": normalized_content_type,
            "Metadata": {
                **_metadata(metadata),
                "sha256": digest,
            },
        }
        if self.settings.server_side_encryption:
            arguments["ServerSideEncryption"] = (
                self.settings.server_side_encryption
            )
        if self.settings.kms_key_id:
            arguments["SSEKMSKeyId"] = self.settings.kms_key_id
        try:
            response = self._client.put_object(**arguments)
        except (BotoCoreError, ClientError) as exc:
            raise ObjectStoreError("object store put failed") from exc
        return StoredObject(
            key=normalized_key,
            content_hash=digest,
            content_type=normalized_content_type,
            byte_size=len(body),
            etag=str(response.get("ETag") or "").strip('"'),
        )

    def get(self, key: str, *, max_bytes: int) -> bytes:
        normalized_key = _object_key(key)
        if max_bytes <= 0:
            raise ValueError("max_bytes must be greater than zero")
        try:
            response = self._client.get_object(
                Bucket=self.settings.bucket,
                Key=normalized_key,
            )
            stream = response["Body"]
            declared_size = int(response.get("ContentLength") or 0)
            if declared_size > max_bytes:
                close = getattr(stream, "close", None)
                if callable(close):
                    close()
                raise ObjectTooLarge("object exceeds the requested size limit")
            try:
                body = bytes(stream.read(max_bytes + 1))
            finally:
                close = getattr(stream, "close", None)
                if callable(close):
                    close()
        except ObjectTooLarge:
            raise
        except (BotoCoreError, ClientError, KeyError, TypeError, ValueError) as exc:
            raise ObjectStoreError("object store get failed") from exc
        if len(body) > max_bytes:
            raise ObjectTooLarge("object exceeds the requested size limit")
        return body

    def create_download_url(
        self,
        key: str,
        *,
        expires_seconds: int,
    ) -> str:
        normalized_key = _object_key(key)
        if expires_seconds <= 0 or expires_seconds > 3600:
            raise ValueError("expires_seconds must be between 1 and 3600")
        try:
            return str(
                self._client.generate_presigned_url(
                    "get_object",
                    Params={
                        "Bucket": self.settings.bucket,
                        "Key": normalized_key,
                    },
                    ExpiresIn=int(expires_seconds),
                )
            )
        except (BotoCoreError, ClientError) as exc:
            raise ObjectStoreError(
                "object store download signing failed"
            ) from exc

    def delete(self, key: str) -> None:
        normalized_key = _object_key(key)
        try:
            self._client.delete_object(
                Bucket=self.settings.bucket,
                Key=normalized_key,
            )
        except (BotoCoreError, ClientError) as exc:
            raise ObjectStoreError("object store delete failed") from exc


@dataclass(frozen=True)
class ProjectObjectUpload:
    organization_id: str
    project_id: str
    object_uri: str
    stored: StoredObject


class ProjectObjectUploader:
    """Build a deterministic project key and upload immutable bytes."""

    def __init__(self, store: ObjectStore, *, bucket: str) -> None:
        self._store = store
        self._bucket = _required_text(bucket, "bucket")

    def upload(
        self,
        *,
        organization_id: str,
        project_id: str,
        data: bytes,
        content_type: str,
        metadata: Mapping[str, str] | None = None,
    ) -> ProjectObjectUpload:
        digest = hashlib.sha256(bytes(data)).hexdigest()
        key = build_project_object_key(
            organization_id,
            project_id,
            digest,
        )
        stored = self._store.put(
            key=key,
            data=data,
            content_type=content_type,
            metadata=metadata,
        )
        if stored.content_hash != digest:
            raise ObjectStoreError("object store hash verification failed")
        return ProjectObjectUpload(
            organization_id=_scope_id(
                organization_id,
                "organization_id",
            ),
            project_id=_scope_id(project_id, "project_id"),
            object_uri=f"s3://{self._bucket}/{key}",
            stored=stored,
        )


__all__ = [
    "ObjectStore",
    "ObjectStoreError",
    "ObjectTooLarge",
    "ProjectObjectUpload",
    "ProjectObjectUploader",
    "S3ObjectStore",
    "S3ObjectStoreSettings",
    "StoredObject",
    "build_project_object_key",
]
