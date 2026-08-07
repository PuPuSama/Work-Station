from __future__ import annotations

import hashlib
import mimetypes
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlsplit

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from knowledge_agent.artifact_store import (
    ArtifactStoreError,
    LocalKnowledgeArtifactStore,
)
from knowledge_agent.schema import knowledge_assets, projects, source_snapshots
from services.access_control import (
    ActorIdentity,
    PostgresProjectAccessRepository,
    ProjectAccessDenied,
    decide_project_permission,
)
from services.audit_log import (
    AuditEvent,
    AuditEventWriter,
    PostgresAuditEventWriter,
)
from services.object_store import (
    ObjectStore,
    ObjectStoreError,
    build_project_object_key,
    build_project_object_prefix,
)


DEFAULT_MAX_MIGRATION_OBJECT_BYTES = 50 * 1024 * 1024


class LegacyKnowledgeArtifactMigrationError(RuntimeError):
    """A legacy artifact cannot be migrated without weakening its identity."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class LegacyKnowledgeArtifactMigrationReport:
    project_id: str
    reference_count: int
    snapshot_artifact_count: int
    asset_count: int
    unique_object_count: int
    already_managed_count: int
    migrated_reference_count: int
    applied: bool

    def public_values(self) -> dict[str, object]:
        return {
            "project_id": self.project_id,
            "reference_count": self.reference_count,
            "snapshot_artifact_count": self.snapshot_artifact_count,
            "asset_count": self.asset_count,
            "unique_object_count": self.unique_object_count,
            "already_managed_count": self.already_managed_count,
            "migrated_reference_count": self.migrated_reference_count,
            "applied": self.applied,
        }


@dataclass(frozen=True, slots=True)
class _LegacyReference:
    kind: Literal["snapshot_raw", "snapshot_normalized", "asset"]
    identity: str
    uri: str
    expected_hash: str | None
    expected_byte_size: int | None
    content_type: str | None


@dataclass(frozen=True, slots=True)
class _PreparedReference:
    reference: _LegacyReference
    key: str
    uri: str
    content_hash: str
    byte_size: int
    content_type: str
    data: bytes | None

    @property
    def already_managed(self) -> bool:
        return self.data is None


def _required_text(value: str, field_name: str) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized


def _safe_content_type(value: str | None, path: Path | None = None) -> str:
    candidate = str(value or "").strip()
    if not candidate and path is not None:
        candidate = str(mimetypes.guess_type(path.name)[0] or "")
    if (
        not candidate
        or len(candidate) > 255
        or "\r" in candidate
        or "\n" in candidate
    ):
        return "application/octet-stream"
    return candidate.partition(";")[0].strip().lower()


class LegacyKnowledgeArtifactMigrator:
    """Copy legacy file artifacts into project-scoped S3 and switch URIs.

    Snapshot content, chunks, product evidence, hashes, and publication pointers
    remain unchanged. Local files are deliberately retained after a successful
    migration so the one-time storage-location change remains recoverable.
    """

    def __init__(
        self,
        engine: Engine,
        store: ObjectStore,
        *,
        bucket: str,
        local_root: Path,
        access_repository: PostgresProjectAccessRepository | None = None,
        audit: AuditEventWriter | None = None,
        max_object_bytes: int = DEFAULT_MAX_MIGRATION_OBJECT_BYTES,
    ) -> None:
        if max_object_bytes <= 0:
            raise ValueError("max_object_bytes must be greater than zero")
        self._engine = engine
        self._store = store
        self._bucket = _required_text(bucket, "bucket")
        self._local_store = LocalKnowledgeArtifactStore(local_root)
        self._access_repository = (
            access_repository or PostgresProjectAccessRepository(engine)
        )
        self._audit = audit or PostgresAuditEventWriter()
        self._max_object_bytes = max_object_bytes

    def inspect(
        self,
        actor: ActorIdentity,
        project_id: str,
    ) -> LegacyKnowledgeArtifactMigrationReport:
        normalized_project_id = _required_text(project_id, "project_id")
        facts = self._access_repository.resolve_project_access(
            actor,
            normalized_project_id,
        )
        if not decide_project_permission(facts, "knowledge.delete").allowed:
            raise ProjectAccessDenied("project access denied")
        try:
            with self._engine.connect() as connection:
                references = self._load_references(
                    connection,
                    normalized_project_id,
                    lock=False,
                )
        except SQLAlchemyError as exc:
            raise LegacyKnowledgeArtifactMigrationError(
                "legacy_artifact_database_unavailable"
            ) from exc
        prepared = self._prepare(actor, normalized_project_id, references)
        return self._report(
            normalized_project_id,
            prepared,
            applied=False,
        )

    def apply(
        self,
        actor: ActorIdentity,
        project_id: str,
        *,
        confirm_project_id: str,
    ) -> LegacyKnowledgeArtifactMigrationReport:
        normalized_project_id = _required_text(project_id, "project_id")
        if (
            _required_text(confirm_project_id, "confirm_project_id")
            != normalized_project_id
        ):
            raise ValueError(
                "confirm_project_id must exactly match project_id"
            )
        inspected = self.inspect(actor, normalized_project_id)
        try:
            with self._engine.connect() as connection:
                references = self._load_references(
                    connection,
                    normalized_project_id,
                    lock=False,
                )
        except SQLAlchemyError as exc:
            raise LegacyKnowledgeArtifactMigrationError(
                "legacy_artifact_database_unavailable"
            ) from exc
        prepared = self._prepare(actor, normalized_project_id, references)
        legacy = tuple(item for item in prepared if not item.already_managed)
        if not legacy:
            return inspected

        self._upload(legacy)
        try:
            with self._engine.begin() as connection:
                facts = self._access_repository.lock_project_access_in_connection(
                    connection,
                    actor,
                    normalized_project_id,
                )
                if not decide_project_permission(
                    facts,
                    "knowledge.delete",
                ).allowed:
                    raise ProjectAccessDenied("project access denied")
                active_project = connection.execute(
                    sa.select(projects.c.project_id)
                    .where(
                        projects.c.project_id == normalized_project_id,
                        projects.c.status == "active",
                    )
                    .with_for_update()
                ).scalar_one_or_none()
                if active_project is None:
                    raise ProjectAccessDenied("project access denied")
                locked_references = self._load_references(
                    connection,
                    normalized_project_id,
                    lock=True,
                )
                if locked_references != references:
                    raise LegacyKnowledgeArtifactMigrationError(
                        "legacy_artifact_concurrent_change"
                    )
                for item in legacy:
                    self._switch_reference(
                        connection,
                        normalized_project_id,
                        item,
                    )
                self._audit.append(
                    connection,
                    AuditEvent(
                        organization_id=actor.organization_id,
                        event_id=(
                            "legacy-knowledge-artifacts-"
                            + uuid.uuid4().hex
                        ),
                        actor_user_id=actor.user_id,
                        project_id=normalized_project_id,
                        action="knowledge.artifacts.storage_migrated",
                        target_type="project",
                        target_id=normalized_project_id,
                        details={
                            "schema_version": 1,
                            "reference_count": len(legacy),
                            "snapshot_artifact_count": sum(
                                item.reference.kind.startswith("snapshot_")
                                for item in legacy
                            ),
                            "asset_count": sum(
                                item.reference.kind == "asset"
                                for item in legacy
                            ),
                            "unique_object_count": len(
                                {item.key for item in legacy}
                            ),
                        },
                    ),
                )
        except ProjectAccessDenied:
            raise
        except LegacyKnowledgeArtifactMigrationError:
            raise
        except (RuntimeError, SQLAlchemyError) as exc:
            raise LegacyKnowledgeArtifactMigrationError(
                "legacy_artifact_commit_failed"
            ) from exc
        return self._report(
            normalized_project_id,
            prepared,
            applied=True,
        )

    def _prepare(
        self,
        actor: ActorIdentity,
        project_id: str,
        references: tuple[_LegacyReference, ...],
    ) -> tuple[_PreparedReference, ...]:
        prefix = build_project_object_prefix(
            actor.organization_id,
            project_id,
        )
        prepared: list[_PreparedReference] = []
        for reference in references:
            parsed = urlsplit(reference.uri)
            if parsed.scheme.lower() == "file":
                prepared.append(
                    self._prepare_local(
                        actor,
                        project_id,
                        reference,
                    )
                )
                continue
            key = self._managed_key(reference.uri, prefix=prefix)
            if key is None:
                raise LegacyKnowledgeArtifactMigrationError(
                    "legacy_artifact_location_unsupported"
                )
            try:
                head = self._store.head(key)
            except ObjectStoreError as exc:
                raise LegacyKnowledgeArtifactMigrationError(
                    "managed_artifact_unavailable"
                ) from exc
            content_hash = str(head.sha256).strip().lower()
            byte_size = int(head.byte_size)
            content_type = _safe_content_type(head.content_type)
            self._verify_identity(
                reference,
                content_hash=content_hash,
                byte_size=byte_size,
            )
            if (
                reference.content_type is not None
                and content_type
                != _safe_content_type(reference.content_type)
            ):
                raise LegacyKnowledgeArtifactMigrationError(
                    "managed_artifact_content_type_mismatch"
                )
            if key != build_project_object_key(
                actor.organization_id,
                project_id,
                content_hash,
            ):
                raise LegacyKnowledgeArtifactMigrationError(
                    "managed_artifact_identity_mismatch"
                )
            prepared.append(
                _PreparedReference(
                    reference=reference,
                    key=key,
                    uri=reference.uri,
                    content_hash=content_hash,
                    byte_size=byte_size,
                    content_type=content_type,
                    data=None,
                )
            )
        return tuple(prepared)

    def _prepare_local(
        self,
        actor: ActorIdentity,
        project_id: str,
        reference: _LegacyReference,
    ) -> _PreparedReference:
        try:
            path = self._local_store.resolve_local_uri(reference.uri)
            project_root = (
                self._local_store.root / project_id
            ).resolve()
            try:
                relative_path = path.relative_to(project_root)
            except ValueError as exc:
                raise LegacyKnowledgeArtifactMigrationError(
                    "legacy_artifact_project_scope_mismatch"
                ) from exc
            size = path.stat().st_size
            if size <= 0 or size > self._max_object_bytes:
                raise LegacyKnowledgeArtifactMigrationError(
                    "legacy_artifact_size_invalid"
                )
            data = path.read_bytes()
        except LegacyKnowledgeArtifactMigrationError:
            raise
        except (ArtifactStoreError, OSError) as exc:
            raise LegacyKnowledgeArtifactMigrationError(
                "legacy_artifact_file_unavailable"
            ) from exc
        if len(data) != size:
            raise LegacyKnowledgeArtifactMigrationError(
                "legacy_artifact_size_invalid"
            )
        content_hash = hashlib.sha256(data).hexdigest()
        path_parts = relative_path.parts
        if (
            len(path_parts) != 4
            or path_parts[1] != content_hash[:2]
            or path_parts[2] != content_hash
        ):
            raise LegacyKnowledgeArtifactMigrationError(
                "legacy_artifact_path_identity_mismatch"
            )
        self._verify_identity(
            reference,
            content_hash=content_hash,
            byte_size=len(data),
        )
        key = build_project_object_key(
            actor.organization_id,
            project_id,
            content_hash,
        )
        return _PreparedReference(
            reference=reference,
            key=key,
            uri=f"s3://{self._bucket}/{key}",
            content_hash=content_hash,
            byte_size=len(data),
            content_type=_safe_content_type(reference.content_type, path),
            data=data,
        )

    @staticmethod
    def _verify_identity(
        reference: _LegacyReference,
        *,
        content_hash: str,
        byte_size: int,
    ) -> None:
        if (
            reference.expected_hash is not None
            and reference.expected_hash != content_hash
        ):
            raise LegacyKnowledgeArtifactMigrationError(
                "legacy_artifact_hash_mismatch"
            )
        if (
            reference.expected_byte_size is not None
            and reference.expected_byte_size != byte_size
        ):
            raise LegacyKnowledgeArtifactMigrationError(
                "legacy_artifact_size_mismatch"
            )

    def _managed_key(self, uri: str, *, prefix: str) -> str | None:
        parsed = urlsplit(uri)
        if (
            parsed.scheme != "s3"
            or parsed.netloc != self._bucket
            or parsed.query
            or parsed.fragment
        ):
            return None
        key = unquote(parsed.path.lstrip("/"))
        segments = key.split("/")
        if (
            not key.startswith(prefix)
            or "\\" in key
            or any(segment in {"", ".", ".."} for segment in segments)
        ):
            return None
        return key

    def _upload(self, legacy: tuple[_PreparedReference, ...]) -> None:
        uploads: dict[str, _PreparedReference] = {}
        for item in legacy:
            existing = uploads.get(item.key)
            if existing is not None and (
                existing.content_hash != item.content_hash
                or existing.data != item.data
            ):
                raise LegacyKnowledgeArtifactMigrationError(
                    "legacy_artifact_identity_mismatch"
                )
            uploads.setdefault(item.key, item)
        for item in uploads.values():
            assert item.data is not None
            try:
                stored = self._store.put(
                    key=item.key,
                    data=item.data,
                    content_type=item.content_type,
                    metadata={
                        "artifact-migration": "legacy-knowledge-v1",
                    },
                )
                head = self._store.head(item.key)
                downloaded = self._store.get(
                    item.key,
                    max_bytes=item.byte_size,
                )
            except (ObjectStoreError, ValueError) as exc:
                raise LegacyKnowledgeArtifactMigrationError(
                    "legacy_artifact_upload_failed"
                ) from exc
            if (
                stored.content_hash != item.content_hash
                or str(head.sha256).strip().lower() != item.content_hash
                or int(head.byte_size) != item.byte_size
                or _safe_content_type(stored.content_type)
                != item.content_type
                or _safe_content_type(head.content_type)
                != item.content_type
                or len(downloaded) != item.byte_size
                or hashlib.sha256(downloaded).hexdigest()
                != item.content_hash
            ):
                raise LegacyKnowledgeArtifactMigrationError(
                    "legacy_artifact_upload_verification_failed"
                )

    @staticmethod
    def _switch_reference(
        connection: Connection,
        project_id: str,
        item: _PreparedReference,
    ) -> None:
        reference = item.reference
        if reference.kind == "asset":
            statement = (
                knowledge_assets.update()
                .where(
                    knowledge_assets.c.project_id == project_id,
                    knowledge_assets.c.asset_id == reference.identity,
                    knowledge_assets.c.artifact_uri == reference.uri,
                )
                .values(artifact_uri=item.uri)
            )
        else:
            column = (
                source_snapshots.c.raw_artifact_uri
                if reference.kind == "snapshot_raw"
                else source_snapshots.c.normalized_artifact_uri
            )
            statement = (
                source_snapshots.update()
                .where(
                    source_snapshots.c.project_id == project_id,
                    source_snapshots.c.snapshot_id == reference.identity,
                    column == reference.uri,
                )
                .values({column.key: item.uri})
            )
        if connection.execute(statement).rowcount != 1:
            raise LegacyKnowledgeArtifactMigrationError(
                "legacy_artifact_concurrent_change"
            )

    @staticmethod
    def _load_references(
        connection: Connection,
        project_id: str,
        *,
        lock: bool,
    ) -> tuple[_LegacyReference, ...]:
        snapshot_statement = sa.select(
            source_snapshots.c.snapshot_id,
            source_snapshots.c.content_hash,
            source_snapshots.c.raw_artifact_uri,
            source_snapshots.c.normalized_artifact_uri,
        ).where(source_snapshots.c.project_id == project_id)
        asset_statement = sa.select(
            knowledge_assets.c.asset_id,
            knowledge_assets.c.content_hash,
            knowledge_assets.c.artifact_uri,
            knowledge_assets.c.content_type,
            knowledge_assets.c.byte_size,
        ).where(knowledge_assets.c.project_id == project_id)
        if lock:
            snapshot_statement = snapshot_statement.with_for_update()
            asset_statement = asset_statement.with_for_update()
        references: list[_LegacyReference] = []
        for row in connection.execute(snapshot_statement).mappings():
            if row["raw_artifact_uri"] is not None:
                references.append(
                    _LegacyReference(
                        kind="snapshot_raw",
                        identity=str(row["snapshot_id"]),
                        uri=str(row["raw_artifact_uri"]),
                        expected_hash=str(row["content_hash"]),
                        expected_byte_size=None,
                        content_type=None,
                    )
                )
            if row["normalized_artifact_uri"] is not None:
                references.append(
                    _LegacyReference(
                        kind="snapshot_normalized",
                        identity=str(row["snapshot_id"]),
                        uri=str(row["normalized_artifact_uri"]),
                        expected_hash=None,
                        expected_byte_size=None,
                        content_type="application/json",
                    )
                )
        for row in connection.execute(asset_statement).mappings():
            references.append(
                _LegacyReference(
                    kind="asset",
                    identity=str(row["asset_id"]),
                    uri=str(row["artifact_uri"]),
                    expected_hash=str(row["content_hash"]),
                    expected_byte_size=int(row["byte_size"]),
                    content_type=str(row["content_type"]),
                )
            )
        references.sort(
            key=lambda item: (item.kind, item.identity, item.uri)
        )
        return tuple(references)

    @staticmethod
    def _report(
        project_id: str,
        prepared: tuple[_PreparedReference, ...],
        *,
        applied: bool,
    ) -> LegacyKnowledgeArtifactMigrationReport:
        return LegacyKnowledgeArtifactMigrationReport(
            project_id=project_id,
            reference_count=len(prepared),
            snapshot_artifact_count=sum(
                item.reference.kind.startswith("snapshot_")
                for item in prepared
            ),
            asset_count=sum(
                item.reference.kind == "asset" for item in prepared
            ),
            unique_object_count=len({item.key for item in prepared}),
            already_managed_count=sum(
                item.already_managed for item in prepared
            ),
            migrated_reference_count=sum(
                not item.already_managed for item in prepared
            ),
            applied=applied,
        )


__all__ = [
    "DEFAULT_MAX_MIGRATION_OBJECT_BYTES",
    "LegacyKnowledgeArtifactMigrationError",
    "LegacyKnowledgeArtifactMigrationReport",
    "LegacyKnowledgeArtifactMigrator",
]
