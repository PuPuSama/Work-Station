from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import PurePath
from typing import Mapping

from ..artifact_store import ArtifactStore
from ..assets import (
    KnowledgeAsset,
    KnowledgeAssetRepository,
    SnapshotAsset,
)
from ..contracts import (
    KnowledgeChunk,
    KnowledgeSource,
    SourceSnapshot,
    TrustTier,
)
from ..interfaces import KnowledgeRepository
from .chunking import ParsedDocumentChunker
from .contracts import DocumentInput, ParsedDocument
from .parsers import DocumentParserRouter


def _snapshot_id(
    *,
    source_id: str,
    document: ParsedDocument,
) -> str:
    identity = "\x1f".join(
        (
            source_id,
            document.content_hash,
            document.parser_name,
            document.parser_version,
        )
    ).encode("utf-8")
    return f"snapshot_{sha256(identity).hexdigest()[:32]}"


def _asset_id(content_hash: str) -> str:
    return f"asset_{content_hash[:32]}"


def _normalized_document_bytes(document: ParsedDocument) -> bytes:
    payload = {
        "schema_version": 1,
        "filename": document.filename,
        "content_type": document.content_type,
        "content_hash": document.content_hash,
        "parser": {
            "name": document.parser_name,
            "version": document.parser_version,
        },
        "title": document.title,
        "metadata": dict(document.metadata),
        "blocks": [
            {
                "kind": block.kind,
                "ordinal": block.ordinal,
                "text": block.text,
                "heading_path": list(block.heading_path),
                "locator": dict(block.locator),
                "metadata": dict(block.metadata),
            }
            for block in document.blocks
        ],
        "assets": [
            {
                "filename": asset.filename,
                "content_type": asset.content_type,
                "content_hash": asset.content_hash,
                "ordinal": asset.ordinal,
                "locator": dict(asset.locator),
                "metadata": dict(asset.metadata),
            }
            for asset in document.assets
        ],
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class IngestionResult:
    """Reviewable result; successful ingestion does not imply publication."""

    source: KnowledgeSource
    snapshot: SourceSnapshot
    chunks: tuple[KnowledgeChunk, ...]
    assets: tuple[KnowledgeAsset, ...]
    snapshot_assets: tuple[SnapshotAsset, ...]


class PrivateDocumentIngestionService:
    """M2 upload path from untrusted bytes to an immutable Inbox snapshot."""

    def __init__(
        self,
        *,
        repository: KnowledgeRepository,
        asset_repository: KnowledgeAssetRepository,
        artifact_store: ArtifactStore,
        parser_router: DocumentParserRouter | None = None,
        chunker: ParsedDocumentChunker | None = None,
    ) -> None:
        self._repository = repository
        self._asset_repository = asset_repository
        self._artifact_store = artifact_store
        self._parser_router = parser_router or DocumentParserRouter()
        self._chunker = chunker or ParsedDocumentChunker()

    def ingest(
        self,
        *,
        project_id: str,
        source_id: str,
        display_name: str,
        document_input: DocumentInput,
        trust_tier: TrustTier = "reference_material",
        metadata: Mapping[str, object] | None = None,
        fetched_at: datetime | None = None,
    ) -> IngestionResult:
        parsed = self._parser_router.parse(document_input)
        snapshot_id = _snapshot_id(source_id=source_id, document=parsed)
        chunks = self._chunker.chunk(
            project_id=project_id,
            source_id=source_id,
            snapshot_id=snapshot_id,
            document=parsed,
        )

        raw_uri = self._artifact_store.put(
            project_id=project_id,
            namespace="raw",
            content_hash=document_input.content_hash,
            filename=document_input.filename,
            content=document_input.content,
        )
        normalized_bytes = _normalized_document_bytes(parsed)
        normalized_hash = sha256(normalized_bytes).hexdigest()
        normalized_uri = self._artifact_store.put(
            project_id=project_id,
            namespace="normalized",
            content_hash=normalized_hash,
            filename=f"{PurePath(document_input.filename).stem}.json",
            content=normalized_bytes,
        )

        source_metadata = {
            "ingestion": {
                "filename": document_input.filename,
                "parser_name": parsed.parser_name,
                "parser_version": parsed.parser_version,
                "classification_reason": "operator uploaded private document",
            },
            **dict(metadata or {}),
        }
        source = KnowledgeSource(
            project_id=project_id,
            source_id=source_id,
            display_name=display_name,
            source_kind="private_file",
            trust_tier=trust_tier,
            status="inbox",
            public_source=False,
            metadata=source_metadata,
        )
        snapshot = SourceSnapshot(
            project_id=project_id,
            source_id=source_id,
            snapshot_id=snapshot_id,
            content_hash=document_input.content_hash,
            fetched_at=fetched_at or datetime.now(timezone.utc),
            parser_name=parsed.parser_name,
            parser_version=parsed.parser_version,
            raw_artifact_uri=raw_uri,
            normalized_artifact_uri=normalized_uri,
            metadata={
                "title": parsed.title,
                "block_count": len(parsed.blocks),
                "asset_count": len(parsed.assets),
            },
        )

        self._repository.upsert_source(source)
        self._repository.store_snapshot(project_id, snapshot, chunks)

        stored_assets: list[KnowledgeAsset] = []
        snapshot_asset_links: list[SnapshotAsset] = []
        for parsed_asset in parsed.assets:
            artifact_uri = self._artifact_store.put(
                project_id=project_id,
                namespace="assets",
                content_hash=parsed_asset.content_hash,
                filename=parsed_asset.filename,
                content=parsed_asset.content,
            )
            stored_asset = self._asset_repository.put_asset(
                KnowledgeAsset(
                    project_id=project_id,
                    asset_id=_asset_id(parsed_asset.content_hash),
                    content_hash=parsed_asset.content_hash,
                    artifact_uri=artifact_uri,
                    content_type=parsed_asset.content_type,
                    byte_size=len(parsed_asset.content),
                    metadata=dict(parsed_asset.metadata),
                )
            )
            link = SnapshotAsset(
                project_id=project_id,
                source_id=source_id,
                snapshot_id=snapshot_id,
                asset_id=stored_asset.asset_id,
                evidence_kind="embedded",
                ordinal=parsed_asset.ordinal,
                locator=dict(parsed_asset.locator),
                metadata={"original_filename": parsed_asset.filename},
            )
            self._asset_repository.link_snapshot_asset(link)
            stored_assets.append(stored_asset)
            snapshot_asset_links.append(link)

        return IngestionResult(
            source=source,
            snapshot=snapshot,
            chunks=chunks,
            assets=tuple(stored_assets),
            snapshot_assets=tuple(snapshot_asset_links),
        )
