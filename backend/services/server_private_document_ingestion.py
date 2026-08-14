from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, replace
from hashlib import sha256
from urllib.parse import urlsplit

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from knowledge_agent.artifact_store import ArtifactStoreError
from knowledge_agent.assets import (
    KnowledgeAssetConflictError,
    KnowledgeAssetNotFound,
    PostgresKnowledgeAssetRepository,
)
from knowledge_agent.catalog import (
    KnowledgeProduct,
    PostgresProductCatalogRepository,
    ProductSourceEvidence,
)
from knowledge_agent.contracts import TrustTier
from knowledge_agent.ingestion import (
    DocumentInput,
    DocumentParserError,
    IngestionResult,
    PrivateDocumentIngestionService,
)
from knowledge_agent.ingestion.mineru import (
    document_parser_router_from_environment,
)
from knowledge_agent.library import PostgresKnowledgeLibrary
from knowledge_agent.object_storage import ScopedS3ArtifactStore
from knowledge_agent.repository import (
    KnowledgeConflictError,
    PostgresKnowledgeRepository,
)
from knowledge_agent.schema import (
    knowledge_sources,
    projects,
    source_snapshots,
)
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
)


_SOURCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class ServerPrivateDocumentUploadConflict(RuntimeError):
    """An immutable upload identity conflicts with existing content."""


class ServerPrivateDocumentUploadUnavailable(RuntimeError):
    """Private document ingestion could not complete safely."""


@dataclass(frozen=True, slots=True)
class ServerPrivateDocumentUpload:
    """Minimal upload result; object URIs and content hashes stay private."""

    result: IngestionResult
    created: bool


def default_private_source_id(filename: str, content: bytes) -> str:
    """Return a retry-stable opaque identity for one filename/content pair."""

    digest = sha256(filename.encode("utf-8") + b"\x00" + content).hexdigest()
    return f"upload_{digest[:32]}"


def _validated_source_id(value: str) -> str:
    normalized = value.strip()
    if not _SOURCE_ID_PATTERN.fullmatch(normalized):
        raise ValueError(
            "source_id must use 1-128 letters, numbers, dot, dash, "
            "underscore, or colon"
        )
    return normalized


def _validated_display_name(value: str) -> str:
    normalized = " ".join(value.split())
    if not normalized:
        raise ValueError("display_name is required")
    if len(normalized) > 255:
        raise ValueError("display_name is too long")
    if any(ord(character) < 32 for character in normalized):
        raise ValueError("display_name contains unsupported characters")
    return normalized


def _asset_uri_matches_scope(
    artifact_uri: str,
    *,
    bucket: str,
    organization_id: str,
    project_id: str,
    content_hash: str,
) -> bool:
    parsed = urlsplit(artifact_uri)
    expected_key = build_project_object_key(
        organization_id,
        project_id,
        content_hash,
    )
    return (
        parsed.scheme == "s3"
        and parsed.netloc == bucket
        and parsed.path == f"/{expected_key}"
        and not parsed.query
        and not parsed.fragment
    )


def _normalized_match_text(value: str) -> str:
    return " ".join(
        "".join(
            character if character.isalnum() else " "
            for character in value.casefold()
        ).split()
    )


def _matching_confirmed_product(
    products: tuple[KnowledgeProduct, ...],
    *,
    display_name: str,
    chunks: tuple[object, ...],
) -> KnowledgeProduct | None:
    document_text = _normalized_match_text(
        "\n".join(
            (
                display_name,
                *(str(getattr(chunk, "text", "")) for chunk in chunks),
            )
        )
    )
    matches = tuple(
        product
        for product in products
        if len(_normalized_match_text(product.name)) >= 4
        and _normalized_match_text(product.name) in document_text
    )
    return matches[0] if len(matches) == 1 else None


class PostgresServerPrivateDocumentIngestion:
    """Prepare private S3 artifacts, then atomically commit Inbox evidence.

    Phase one performs project-scoped parsing and immutable object uploads
    after an initial authorization check. Phase two serializes the Source,
    locks all revocable access facts plus the active Project, and atomically
    commits Source/Snapshot/Chunk/Asset links with a redacted Audit event.
    A phase-two rejection can leave only content-addressed object orphans,
    which are handled by the existing delayed reconciliation boundary.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        store: ObjectStore,
        bucket: str,
        audit: AuditEventWriter | None = None,
    ) -> None:
        self._engine = engine
        self._store = store
        self._bucket = bucket.strip()
        if not self._bucket:
            raise ValueError("bucket is required")
        self._access = PostgresProjectAccessRepository(engine)
        self._repository = PostgresKnowledgeRepository(engine)
        self._assets = PostgresKnowledgeAssetRepository(engine)
        self._catalog = PostgresProductCatalogRepository(engine)
        self._library = PostgresKnowledgeLibrary(engine)
        self._audit = audit or PostgresAuditEventWriter()
        self._parser_router = document_parser_router_from_environment()

    def close(self) -> None:
        self._parser_router.close()

    def upload(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        source_id: str,
        display_name: str,
        document_input: DocumentInput,
        trust_tier: TrustTier = "reference_material",
    ) -> ServerPrivateDocumentUpload:
        normalized_source_id = _validated_source_id(source_id)
        normalized_display_name = _validated_display_name(display_name)
        try:
            # Avoid object writes for already-revoked callers. The final
            # transaction repeats this decision under revocation locks.
            facts = self._access.resolve_project_access(actor, project_id)
            if not decide_project_permission(
                facts,
                "knowledge.edit",
            ).allowed:
                raise ProjectAccessDenied("project access denied")
            ingestion = PrivateDocumentIngestionService(
                repository=self._repository,
                asset_repository=self._assets,
                artifact_store=ScopedS3ArtifactStore(
                    store=self._store,
                    bucket=self._bucket,
                    organization_id=actor.organization_id,
                    project_id=project_id,
                ),
                snapshot_lookup=self._library,
                parser_router=self._parser_router,
            )
            prepared = ingestion.prepare(
                project_id=project_id,
                source_id=normalized_source_id,
                display_name=normalized_display_name,
                document_input=document_input,
                trust_tier=trust_tier,
            )
        except (ProjectAccessDenied, DocumentParserError, ValueError):
            raise
        except (
            ArtifactStoreError,
            ObjectStoreError,
            RuntimeError,
            SQLAlchemyError,
        ) as exc:
            raise ServerPrivateDocumentUploadUnavailable(
                "private document artifacts could not be prepared"
            ) from exc

        try:
            confirmed_products = self._catalog.list_products(
                project_id,
                status="confirmed",
            )
            with self._engine.begin() as connection:
                facts = self._access.lock_project_access_in_connection(
                    connection,
                    actor,
                    project_id,
                )
                if not decide_project_permission(
                    facts,
                    "knowledge.edit",
                ).allowed:
                    raise ProjectAccessDenied("project access denied")
                active_project = connection.execute(
                    sa.select(projects.c.project_id)
                    .where(
                        projects.c.project_id == project_id,
                        projects.c.status == "active",
                    )
                    .with_for_update()
                ).scalar_one_or_none()
                if active_project is None:
                    raise ProjectAccessDenied("project access denied")

                # Serialize all snapshots for one Source. This avoids a
                # concurrent retry mutating Source metadata before discovering
                # that its immutable Snapshot already exists.
                connection.execute(
                    sa.select(
                        sa.func.pg_advisory_xact_lock(
                            sa.func.hashtextextended(
                                "\n".join(
                                    (
                                        actor.organization_id,
                                        project_id,
                                        normalized_source_id,
                                    )
                                ),
                                0,
                            )
                        )
                    )
                ).scalar_one()
                existing_snapshot = connection.execute(
                    sa.select(source_snapshots.c.snapshot_id).where(
                        source_snapshots.c.project_id == project_id,
                        source_snapshots.c.source_id
                        == normalized_source_id,
                        source_snapshots.c.snapshot_id
                        == prepared.snapshot.snapshot_id,
                    )
                ).scalar_one_or_none()
                source_row = connection.execute(
                    sa.select(
                        knowledge_sources.c.current_snapshot_id,
                        knowledge_sources.c.pending_snapshot_id,
                    )
                    .where(
                        knowledge_sources.c.project_id == project_id,
                        knowledge_sources.c.source_id
                        == normalized_source_id,
                    )
                    .with_for_update()
                ).mappings().one_or_none()

                if existing_snapshot is None:
                    if (
                        source_row is not None
                        and source_row["pending_snapshot_id"] is not None
                    ):
                        raise ServerPrivateDocumentUploadConflict(
                            "knowledge source already has a pending snapshot"
                        )
                    if source_row is None:
                        self._repository.upsert_source_in_transaction(
                            connection,
                            prepared.source,
                        )
                created = self._repository.store_snapshot_in_transaction(
                    connection,
                    project_id,
                    prepared.snapshot,
                    prepared.chunks,
                )
                if created:
                    self._repository.set_pending_snapshot_in_transaction(
                        connection,
                        project_id,
                        normalized_source_id,
                        prepared.snapshot.snapshot_id,
                    )
                stored_assets = []
                stored_asset_ids: dict[str, str] = {}
                for asset in prepared.assets:
                    stored = self._assets.put_asset_in_transaction(
                        connection,
                        asset,
                    )
                    if not _asset_uri_matches_scope(
                        stored.artifact_uri,
                        bucket=self._bucket,
                        organization_id=actor.organization_id,
                        project_id=project_id,
                        content_hash=stored.content_hash,
                    ):
                        raise ServerPrivateDocumentUploadConflict(
                            "deduplicated asset is outside the server scope"
                        )
                    stored_assets.append(
                        replace(stored, metadata=asset.metadata)
                    )
                    stored_asset_ids[asset.asset_id] = stored.asset_id
                stored_links = []
                for link in prepared.snapshot_assets:
                    stored_link = replace(
                        link,
                        asset_id=stored_asset_ids.get(
                            link.asset_id,
                            link.asset_id,
                        ),
                    )
                    self._assets.link_snapshot_asset_in_transaction(
                        connection,
                        stored_link,
                    )
                    stored_links.append(stored_link)
                stored_source = self._repository.get_source_in_transaction(
                    connection,
                    project_id,
                    normalized_source_id,
                )
                if stored_source is None:
                    raise ServerPrivateDocumentUploadConflict(
                        "stored knowledge source is unavailable"
                    )
                committed = replace(
                    prepared,
                    source=stored_source,
                    assets=tuple(stored_assets),
                    snapshot_assets=tuple(stored_links),
                )

                matched_product = _matching_confirmed_product(
                    confirmed_products,
                    display_name=normalized_display_name,
                    chunks=committed.chunks,
                )
                if matched_product is not None:
                    self._catalog.store_source_evidence_in_transaction(
                        connection,
                        ProductSourceEvidence(
                            project_id=project_id,
                            product_id=matched_product.product_id,
                            source_id=normalized_source_id,
                            snapshot_id=prepared.snapshot.snapshot_id,
                            relation="private_specification",
                            confidence=1.0,
                            reason=(
                                "confirmed product name appears exactly in "
                                "the uploaded document"
                            ),
                            metadata={"organizer": "exact_product_name_v1"},
                        ),
                    )

                if not created:
                    return ServerPrivateDocumentUpload(
                        result=committed,
                        created=False,
                    )
                self._audit.append(
                    connection,
                    AuditEvent(
                        organization_id=actor.organization_id,
                        event_id=(
                            "knowledge_upload_"
                            + uuid.uuid5(
                                uuid.NAMESPACE_URL,
                                "\x1f".join(
                                    (
                                        actor.organization_id,
                                        project_id,
                                        normalized_source_id,
                                        prepared.snapshot.snapshot_id,
                                    )
                                ),
                            ).hex
                        ),
                        actor_user_id=actor.user_id,
                        project_id=project_id,
                        action="knowledge.source.uploaded",
                        target_type="knowledge_source",
                        target_id=normalized_source_id,
                        details={
                            "snapshot_id": prepared.snapshot.snapshot_id,
                            "parser_name": prepared.snapshot.parser_name,
                            "parser_version": (
                                prepared.snapshot.parser_version
                            ),
                            "chunk_count": len(prepared.chunks),
                            "asset_count": len(prepared.assets),
                        },
                    ),
                )
                return ServerPrivateDocumentUpload(
                    result=committed,
                    created=True,
                )
        except (
            ProjectAccessDenied,
            ServerPrivateDocumentUploadConflict,
        ):
            raise
        except (
            IntegrityError,
            KnowledgeConflictError,
            KnowledgeAssetConflictError,
            KnowledgeAssetNotFound,
        ) as exc:
            raise ServerPrivateDocumentUploadConflict(
                "private document conflicts with existing immutable evidence"
            ) from exc
        except (RuntimeError, SQLAlchemyError) as exc:
            raise ServerPrivateDocumentUploadUnavailable(
                "private document could not be committed"
            ) from exc


__all__ = [
    "PostgresServerPrivateDocumentIngestion",
    "ServerPrivateDocumentUpload",
    "ServerPrivateDocumentUploadConflict",
    "ServerPrivateDocumentUploadUnavailable",
    "default_private_source_id",
]
