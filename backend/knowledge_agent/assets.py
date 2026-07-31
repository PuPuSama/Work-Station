from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, Mapping, Protocol, runtime_checkable
from urllib.parse import urlsplit

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from .schema import knowledge_assets, snapshot_assets


Metadata = Mapping[str, object]
AssetEvidenceKind = Literal[
    "embedded",
    "json_ld",
    "gallery",
    "body",
    "featured_media",
    "wordpress_media",
    "manual_upload",
]
ASSET_EVIDENCE_KINDS = frozenset(
    {
        "embedded",
        "json_ld",
        "gallery",
        "body",
        "featured_media",
        "wordpress_media",
        "manual_upload",
    }
)


class KnowledgeAssetRepositoryError(RuntimeError):
    """Base error for project-scoped knowledge asset persistence."""


class KnowledgeAssetConflictError(KnowledgeAssetRepositoryError):
    """Raised when an immutable asset identity is reused with different data."""


class KnowledgeAssetNotFound(KnowledgeAssetRepositoryError):
    """Raised when an asset or snapshot evidence target does not exist."""


def _required_text(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} is required")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized


def _optional_text(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field_name)


def _metadata(value: Metadata, field_name: str) -> Metadata:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a mapping")
    return MappingProxyType(dict(value))


def _sha256(value: str) -> str:
    normalized = _required_text(value, "content_hash").lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError("content_hash must be a 64-character SHA-256 hex digest")
    return normalized


def _absolute_uri(value: str, field_name: str) -> str:
    normalized = _required_text(value, field_name)
    try:
        parsed = urlsplit(normalized)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an absolute URI") from exc
    if (
        not parsed.scheme
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
        or (
            parsed.scheme.lower() == "file"
            and not parsed.path.startswith("/")
        )
        or (
            parsed.scheme.lower() != "file"
            and not parsed.netloc
            and not parsed.path.startswith("/")
        )
    ):
        raise ValueError(f"{field_name} must be an absolute URI")
    return normalized


def _http_url(value: str | None, field_name: str) -> str | None:
    normalized = _optional_text(value, field_name)
    if normalized is None:
        return None
    try:
        parsed = urlsplit(normalized)
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an absolute HTTP(S) URL") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port is not None
        and not 0 < port < 65536
    ):
        raise ValueError(f"{field_name} must be an absolute HTTP(S) URL")
    return normalized


def _positive_optional(value: int | None, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


@dataclass(frozen=True, slots=True)
class KnowledgeAsset:
    """Project-deduplicated immutable asset stored outside PostgreSQL."""

    project_id: str
    asset_id: str
    content_hash: str
    artifact_uri: str
    content_type: str
    byte_size: int
    width: int | None = None
    height: int | None = None
    metadata: Metadata = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in ("project_id", "asset_id", "content_type"):
            object.__setattr__(
                self, field_name, _required_text(getattr(self, field_name), field_name)
            )
        object.__setattr__(self, "content_hash", _sha256(self.content_hash))
        object.__setattr__(
            self, "artifact_uri", _absolute_uri(self.artifact_uri, "artifact_uri")
        )
        object.__setattr__(
            self, "byte_size", _positive_optional(self.byte_size, "byte_size")
        )
        object.__setattr__(self, "width", _positive_optional(self.width, "width"))
        object.__setattr__(self, "height", _positive_optional(self.height, "height"))
        if (self.width is None) != (self.height is None):
            raise ValueError("width and height must both be present or both be absent")
        object.__setattr__(self, "metadata", _metadata(self.metadata, "metadata"))


@dataclass(frozen=True, slots=True)
class SnapshotAsset:
    """Evidence that one immutable asset appeared in a specific source snapshot."""

    project_id: str
    source_id: str
    snapshot_id: str
    asset_id: str
    evidence_kind: AssetEvidenceKind
    ordinal: int
    source_url: str | None = None
    alt_text: str | None = None
    title: str | None = None
    caption: str | None = None
    locator: Metadata = field(default_factory=dict)
    metadata: Metadata = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in ("project_id", "source_id", "snapshot_id", "asset_id"):
            object.__setattr__(
                self, field_name, _required_text(getattr(self, field_name), field_name)
            )
        if self.evidence_kind not in ASSET_EVIDENCE_KINDS:
            raise ValueError(
                "evidence_kind must be one of: "
                + ", ".join(sorted(ASSET_EVIDENCE_KINDS))
            )
        if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int):
            raise ValueError("ordinal must be a non-negative integer")
        if self.ordinal < 0:
            raise ValueError("ordinal must be a non-negative integer")
        object.__setattr__(
            self, "source_url", _http_url(self.source_url, "source_url")
        )
        for field_name in ("alt_text", "title", "caption"):
            object.__setattr__(
                self,
                field_name,
                _optional_text(getattr(self, field_name), field_name),
            )
        object.__setattr__(self, "locator", _metadata(self.locator, "locator"))
        object.__setattr__(self, "metadata", _metadata(self.metadata, "metadata"))


@runtime_checkable
class KnowledgeAssetRepository(Protocol):
    """Persistence boundary for immutable assets and snapshot evidence."""

    def put_asset(self, asset: KnowledgeAsset) -> KnowledgeAsset: ...

    def link_snapshot_asset(self, link: SnapshotAsset) -> None: ...

    def get_asset(self, project_id: str, asset_id: str) -> KnowledgeAsset | None: ...

    def list_snapshot_assets(
        self, project_id: str, source_id: str, snapshot_id: str
    ) -> tuple[SnapshotAsset, ...]: ...


class PostgresKnowledgeAssetRepository:
    """SQLAlchemy Core implementation of the M2 asset boundary."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def put_asset(self, asset: KnowledgeAsset) -> KnowledgeAsset:
        try:
            with self._engine.begin() as connection:
                return self.put_asset_in_transaction(connection, asset)
        except IntegrityError as exc:
            raise KnowledgeAssetConflictError(
                "asset conflicts with an existing project-scoped record"
            ) from exc

    def put_asset_in_transaction(
        self,
        connection: Connection,
        asset: KnowledgeAsset,
    ) -> KnowledgeAsset:
        """Store or verify an immutable asset in a business transaction."""

        if not connection.in_transaction():
            raise ValueError(
                "knowledge asset writes require a business transaction"
            )
        statement = (
            insert(knowledge_assets)
            .values(
                project_id=asset.project_id,
                asset_id=asset.asset_id,
                content_hash=asset.content_hash,
                artifact_uri=asset.artifact_uri,
                content_type=asset.content_type,
                byte_size=asset.byte_size,
                width=asset.width,
                height=asset.height,
                metadata=dict(asset.metadata),
            )
            .on_conflict_do_nothing()
        )
        connection.execute(statement)
        stored_row = connection.execute(
            sa.select(knowledge_assets).where(
                knowledge_assets.c.project_id == asset.project_id,
                knowledge_assets.c.content_hash == asset.content_hash,
            )
        ).mappings().one_or_none()
        if stored_row is None:
            reused_id = connection.execute(
                sa.select(knowledge_assets.c.asset_id).where(
                    knowledge_assets.c.project_id == asset.project_id,
                    knowledge_assets.c.asset_id == asset.asset_id,
                )
            ).scalar_one_or_none()
            if reused_id is not None:
                raise KnowledgeAssetConflictError(
                    "asset ID is already used by different content"
                )
            raise KnowledgeAssetConflictError(
                "asset could not be stored in the requested project"
            )
        return _asset_from_row(stored_row)

    def link_snapshot_asset(self, link: SnapshotAsset) -> None:
        try:
            with self._engine.begin() as connection:
                self.link_snapshot_asset_in_transaction(connection, link)
        except IntegrityError as exc:
            raise KnowledgeAssetNotFound(
                "asset or source snapshot was not found in the requested project"
            ) from exc

    def link_snapshot_asset_in_transaction(
        self,
        connection: Connection,
        link: SnapshotAsset,
    ) -> bool:
        """Link immutable evidence in a caller transaction.

        Returns ``True`` for a new link and ``False`` for an exact retry.
        """

        if not connection.in_transaction():
            raise ValueError(
                "snapshot asset links require a business transaction"
            )
        statement = (
            insert(snapshot_assets)
            .values(
                project_id=link.project_id,
                source_id=link.source_id,
                snapshot_id=link.snapshot_id,
                asset_id=link.asset_id,
                evidence_kind=link.evidence_kind,
                ordinal=link.ordinal,
                source_url=link.source_url,
                alt_text=link.alt_text,
                title=link.title,
                caption=link.caption,
                locator=dict(link.locator),
                metadata=dict(link.metadata),
            )
            .on_conflict_do_nothing()
            .returning(snapshot_assets.c.asset_id)
        )
        inserted_asset_id = connection.execute(
            statement
        ).scalar_one_or_none()
        if inserted_asset_id is not None:
            return True
        stored = connection.execute(
            sa.select(snapshot_assets).where(
                snapshot_assets.c.project_id == link.project_id,
                snapshot_assets.c.source_id == link.source_id,
                snapshot_assets.c.snapshot_id == link.snapshot_id,
                snapshot_assets.c.asset_id == link.asset_id,
            )
        ).mappings().one_or_none()
        if stored is None or _snapshot_asset_from_row(stored) != link:
            raise KnowledgeAssetConflictError(
                "snapshot asset conflicts with an existing immutable record"
            )
        return False

    def get_asset(self, project_id: str, asset_id: str) -> KnowledgeAsset | None:
        normalized_project_id = _required_text(project_id, "project_id")
        normalized_asset_id = _required_text(asset_id, "asset_id")
        with self._engine.connect() as connection:
            row = connection.execute(
                sa.select(knowledge_assets).where(
                    knowledge_assets.c.project_id == normalized_project_id,
                    knowledge_assets.c.asset_id == normalized_asset_id,
                )
            ).mappings().one_or_none()
        return None if row is None else _asset_from_row(row)

    def list_snapshot_assets(
        self, project_id: str, source_id: str, snapshot_id: str
    ) -> tuple[SnapshotAsset, ...]:
        normalized_project_id = _required_text(project_id, "project_id")
        normalized_source_id = _required_text(source_id, "source_id")
        normalized_snapshot_id = _required_text(snapshot_id, "snapshot_id")
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(snapshot_assets)
                .where(
                    snapshot_assets.c.project_id == normalized_project_id,
                    snapshot_assets.c.source_id == normalized_source_id,
                    snapshot_assets.c.snapshot_id == normalized_snapshot_id,
                )
                .order_by(
                    snapshot_assets.c.ordinal.asc(),
                    snapshot_assets.c.asset_id.asc(),
                )
            ).mappings().all()
        return tuple(_snapshot_asset_from_row(row) for row in rows)


def _asset_from_row(row: Mapping[str, object]) -> KnowledgeAsset:
    return KnowledgeAsset(
        project_id=str(row["project_id"]),
        asset_id=str(row["asset_id"]),
        content_hash=str(row["content_hash"]),
        artifact_uri=str(row["artifact_uri"]),
        content_type=str(row["content_type"]),
        byte_size=int(row["byte_size"]),
        width=None if row["width"] is None else int(row["width"]),
        height=None if row["height"] is None else int(row["height"]),
        metadata=dict(row["metadata"]),  # type: ignore[arg-type]
    )


def _snapshot_asset_from_row(row: Mapping[str, object]) -> SnapshotAsset:
    return SnapshotAsset(
        project_id=str(row["project_id"]),
        source_id=str(row["source_id"]),
        snapshot_id=str(row["snapshot_id"]),
        asset_id=str(row["asset_id"]),
        evidence_kind=str(row["evidence_kind"]),  # type: ignore[arg-type]
        ordinal=int(row["ordinal"]),
        source_url=None if row["source_url"] is None else str(row["source_url"]),
        alt_text=None if row["alt_text"] is None else str(row["alt_text"]),
        title=None if row["title"] is None else str(row["title"]),
        caption=None if row["caption"] is None else str(row["caption"]),
        locator=dict(row["locator"]),  # type: ignore[arg-type]
        metadata=dict(row["metadata"]),  # type: ignore[arg-type]
    )
