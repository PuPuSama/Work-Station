from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from urllib.parse import unquote, urlsplit

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from knowledge_agent.assets import (
    KnowledgeAsset,
    KnowledgeAssetConflictError,
    KnowledgeAssetNotFound,
    PostgresKnowledgeAssetRepository,
)
from knowledge_agent.artifact_store import ArtifactStoreError
from knowledge_agent.catalog import (
    PostgresProductCatalogRepository,
    ProductCatalogConflictError,
    ProductCatalogNotFound,
)
from knowledge_agent.contracts import KnowledgeChunk, SourceSnapshot
from knowledge_agent.repository import (
    KnowledgeConflictError,
    PostgresKnowledgeRepository,
)
from knowledge_agent.schema import knowledge_sources, projects, source_snapshots
from knowledge_agent.web_ingestion import (
    PreparedWebPageIngestion,
    WebPageIngestionResult,
    WebPagePreparation,
)
from knowledge_agent.wordpress import (
    MAX_WEB_RESOURCE_BYTES,
    ClassifiedWebPage,
    FetchedResource,
    OfficialSiteFetcher,
)
from services.access_control import (
    ActorIdentity,
    PostgresProjectAccessRepository,
    ProjectAccessDenied,
    ProjectPermission,
    decide_project_permission,
)
from services.audit_log import (
    AuditEvent,
    AuditEventWriter,
    PostgresAuditEventWriter,
)
from services.job_queue import JobCancelled, JobConflict
from services.object_store import (
    ObjectStoreError,
    build_project_object_key,
    build_project_object_prefix,
)


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ServerWebEvidenceConflict(JobConflict):
    """Prepared evidence conflicts with the authorized PostgreSQL scope."""


class ServerWebEvidenceUnavailable(RuntimeError):
    """The Server evidence unit of work could not complete safely."""


class CheckpointingOfficialSiteFetcher:
    """Run a trusted authorization/cancellation checkpoint around each fetch."""

    def __init__(
        self,
        delegate: OfficialSiteFetcher,
        *,
        checkpoint: Callable[[], None],
    ) -> None:
        self._delegate = delegate
        self._checkpoint = checkpoint

    def fetch(
        self,
        *,
        site_url: str,
        url: str,
        max_bytes: int = MAX_WEB_RESOURCE_BYTES,
    ) -> FetchedResource:
        self._checkpoint()
        resource = self._delegate.fetch(
            site_url=site_url,
            url=url,
            max_bytes=max_bytes,
        )
        self._checkpoint()
        return resource


@dataclass(frozen=True, slots=True)
class ServerWebEvidenceContext:
    """Trusted per-job identity; never constructed from an HTTP body."""

    actor: ActorIdentity
    project_id: str
    operation: str
    target_type: str
    target_id: str
    permission: ProjectPermission
    cancelled: Callable[[], bool] | None = None

    def __post_init__(self) -> None:
        for name in ("project_id", "operation", "target_type", "target_id"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} is required")
        if self.permission not in {"knowledge.edit", "knowledge.publish"}:
            raise ValueError("web evidence permission is unsupported")


def _scoped_object_uri(
    uri: str | None,
    *,
    bucket: str,
    organization_id: str,
    project_id: str,
    content_hash: str | None = None,
) -> bool:
    if not uri:
        return False
    parsed = urlsplit(uri)
    if (
        parsed.scheme != "s3"
        or parsed.netloc != bucket
        or parsed.query
        or parsed.fragment
    ):
        return False
    key = unquote(parsed.path.lstrip("/"))
    if content_hash is not None:
        return key == build_project_object_key(
            organization_id,
            project_id,
            content_hash,
        )
    prefix = build_project_object_prefix(organization_id, project_id)
    digest = key.rsplit("/", 1)[-1]
    return key.startswith(prefix + "blobs/") and bool(_SHA256.fullmatch(digest))


def _canonicalize_prepared(
    prepared: PreparedWebPageIngestion,
    snapshot: SourceSnapshot,
) -> PreparedWebPageIngestion:
    if snapshot.snapshot_id == prepared.snapshot.snapshot_id:
        return replace(prepared, snapshot=snapshot)

    def chunk_with_snapshot(chunk: KnowledgeChunk) -> KnowledgeChunk:
        _, separator, suffix = chunk.chunk_id.partition(":")
        if not separator or not suffix:
            raise ServerWebEvidenceConflict(
                "prepared chunk identity is invalid"
            )
        return replace(
            chunk,
            snapshot_id=snapshot.snapshot_id,
            chunk_id=f"{snapshot.snapshot_id}:{suffix}",
        )

    return replace(
        prepared,
        snapshot=snapshot,
        chunks=tuple(chunk_with_snapshot(chunk) for chunk in prepared.chunks),
        source_evidence=(
            None
            if prepared.source_evidence is None
            else replace(
                prepared.source_evidence,
                snapshot_id=snapshot.snapshot_id,
            )
        ),
        snapshot_assets=tuple(
            replace(link, snapshot_id=snapshot.snapshot_id)
            for link in prepared.snapshot_assets
        ),
        asset_evidence=tuple(
            replace(evidence, snapshot_id=snapshot.snapshot_id)
            for evidence in prepared.asset_evidence
        ),
    )


class PostgresServerWebEvidenceIngestion:
    """Prepare objects outside SQL, then atomically commit one webpage graph.

    The atomic unit is one classified webpage, not a category crawl or a
    Research attempt. Content-addressed objects may remain as recoverable
    orphans when authorization or the database commit fails.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        preparer: WebPagePreparation,
        context: ServerWebEvidenceContext,
        bucket: str,
        repository: PostgresKnowledgeRepository | None = None,
        assets: PostgresKnowledgeAssetRepository | None = None,
        catalog: PostgresProductCatalogRepository | None = None,
        access: PostgresProjectAccessRepository | None = None,
        audit: AuditEventWriter | None = None,
    ) -> None:
        self._engine = engine
        self._preparer = preparer
        self._context = context
        self._bucket = bucket.strip()
        if not self._bucket:
            raise ValueError("bucket is required")
        self._repository = repository or PostgresKnowledgeRepository(engine)
        self._assets = assets or PostgresKnowledgeAssetRepository(engine)
        self._catalog = catalog or PostgresProductCatalogRepository(engine)
        self._access = access or PostgresProjectAccessRepository(engine)
        self._audit = audit or PostgresAuditEventWriter()

    def ingest_url(
        self,
        *,
        project_id: str,
        site_url: str,
        url: str,
        metadata: Mapping[str, object] | None = None,
    ) -> WebPageIngestionResult:
        self._preauthorize(project_id)
        try:
            prepared = self._preparer.prepare_url(
                project_id=project_id,
                site_url=site_url,
                url=url,
                metadata=metadata,
            )
        except (ArtifactStoreError, ObjectStoreError) as exc:
            raise ServerWebEvidenceUnavailable(
                "web evidence artifacts are temporarily unavailable"
            ) from exc
        return self.commit(prepared)

    def ingest_resource(
        self,
        *,
        project_id: str,
        site_url: str,
        resource: FetchedResource,
        classification: ClassifiedWebPage,
        metadata: Mapping[str, object] | None = None,
    ) -> WebPageIngestionResult:
        self._preauthorize(project_id)
        try:
            prepared = self._preparer.prepare_resource(
                project_id=project_id,
                site_url=site_url,
                resource=resource,
                classification=classification,
                metadata=metadata,
            )
        except (ArtifactStoreError, ObjectStoreError) as exc:
            raise ServerWebEvidenceUnavailable(
                "web evidence artifacts are temporarily unavailable"
            ) from exc
        return self.commit(prepared)

    def _preauthorize(self, project_id: str) -> None:
        self._check_cancelled()
        if project_id != self._context.project_id:
            raise ProjectAccessDenied("project access denied")
        facts = self._access.resolve_project_access(
            self._context.actor,
            project_id,
        )
        if not decide_project_permission(
            facts,
            self._context.permission,
        ).allowed:
            raise ProjectAccessDenied("project access denied")

    def _check_cancelled(self) -> None:
        cancelled = self._context.cancelled
        if cancelled is not None and cancelled():
            raise JobCancelled("Web evidence ingestion cancelled.")

    def commit(
        self,
        prepared: PreparedWebPageIngestion,
    ) -> WebPageIngestionResult:
        context = self._context
        self._check_cancelled()
        if prepared.source.project_id != context.project_id:
            raise ProjectAccessDenied("project access denied")
        try:
            with self._engine.begin() as connection:
                facts = self._access.lock_project_access_in_connection(
                    connection,
                    context.actor,
                    context.project_id,
                )
                if not decide_project_permission(
                    facts,
                    context.permission,
                ).allowed:
                    raise ProjectAccessDenied("project access denied")
                active_project = connection.execute(
                    sa.select(projects.c.project_id)
                    .where(
                        projects.c.project_id == context.project_id,
                        projects.c.status == "active",
                    )
                    .with_for_update()
                ).scalar_one_or_none()
                if active_project is None:
                    raise ProjectAccessDenied("project access denied")

                connection.execute(
                    sa.select(
                        sa.func.pg_advisory_xact_lock(
                            sa.func.hashtextextended(
                                "\n".join(
                                    (
                                        context.actor.organization_id,
                                        context.project_id,
                                        prepared.source.source_id,
                                    )
                                ),
                                0,
                            )
                        )
                    )
                ).scalar_one()
                source_row = connection.execute(
                    sa.select(knowledge_sources)
                    .where(
                        knowledge_sources.c.project_id == context.project_id,
                        knowledge_sources.c.source_id
                        == prepared.source.source_id,
                    )
                    .with_for_update()
                ).mappings().one_or_none()
                if source_row is not None and (
                    str(source_row["canonical_url"] or "")
                    != str(prepared.source.canonical_url or "")
                ):
                    raise ServerWebEvidenceConflict(
                        "web source identity conflicts with existing evidence"
                    )

                canonical_snapshot = (
                    self._repository.find_snapshot_by_content_in_transaction(
                        connection,
                        project_id=context.project_id,
                        source_id=prepared.source.source_id,
                        content_hash=prepared.snapshot.content_hash,
                        parser_name=prepared.snapshot.parser_name,
                        parser_version=prepared.snapshot.parser_version,
                    )
                )
                existing_snapshot_count = int(
                    connection.execute(
                        sa.select(sa.func.count())
                        .select_from(source_snapshots)
                        .where(
                            source_snapshots.c.project_id == context.project_id,
                            source_snapshots.c.source_id
                            == prepared.source.source_id,
                        )
                    ).scalar_one()
                )
                if (
                    canonical_snapshot is None
                    and source_row is not None
                    and source_row["pending_snapshot_id"] is not None
                ):
                    raise ServerWebEvidenceConflict(
                        "web source already has a pending snapshot"
                    )
                committed = (
                    prepared
                    if canonical_snapshot is None
                    else _canonicalize_prepared(
                        prepared,
                        canonical_snapshot,
                    )
                )
                self._verify_object_scope(committed)

                new_source = source_row is None
                if new_source or existing_snapshot_count == 0:
                    self._repository.upsert_source_in_transaction(
                        connection,
                        committed.source,
                    )
                created_snapshot = (
                    self._repository.store_snapshot_in_transaction(
                        connection,
                        context.project_id,
                        committed.snapshot,
                        committed.chunks,
                    )
                )
                pending_changed = False
                if created_snapshot:
                    pending_changed = (
                        self._repository.set_pending_snapshot_in_transaction(
                            connection,
                            context.project_id,
                            committed.source.source_id,
                            committed.snapshot.snapshot_id,
                        )
                    )
                changed = new_source or created_snapshot or pending_changed

                if committed.product is not None:
                    changed = (
                        self._catalog.upsert_product_in_transaction(
                            connection,
                            committed.product,
                        )
                        or changed
                    )
                if committed.source_evidence is not None:
                    changed = (
                        self._catalog.store_source_evidence_in_transaction(
                            connection,
                            committed.source_evidence,
                        )
                        or changed
                    )

                stored_assets: list[KnowledgeAsset] = []
                stored_ids: dict[str, str] = {}
                for asset in committed.assets:
                    stored = self._assets.put_asset_in_transaction(
                        connection,
                        asset,
                    )
                    if not _scoped_object_uri(
                        stored.artifact_uri,
                        bucket=self._bucket,
                        organization_id=context.actor.organization_id,
                        project_id=context.project_id,
                        content_hash=stored.content_hash,
                    ):
                        raise ServerWebEvidenceConflict(
                            "deduplicated asset is outside the server scope"
                        )
                    stored_assets.append(stored)
                    stored_ids[asset.asset_id] = stored.asset_id

                stored_links = []
                for link in committed.snapshot_assets:
                    stored_link = replace(
                        link,
                        asset_id=stored_ids.get(
                            link.asset_id,
                            link.asset_id,
                        ),
                    )
                    changed = (
                        self._assets.link_snapshot_asset_in_transaction(
                            connection,
                            stored_link,
                        )
                        or changed
                    )
                    stored_links.append(stored_link)
                stored_asset_evidence = []
                for evidence in committed.asset_evidence:
                    stored_evidence = replace(
                        evidence,
                        asset_id=stored_ids.get(
                            evidence.asset_id,
                            evidence.asset_id,
                        ),
                    )
                    changed = (
                        self._catalog.store_asset_evidence_in_transaction(
                            connection,
                            stored_evidence,
                        )
                        or changed
                    )
                    stored_asset_evidence.append(stored_evidence)

                committed = replace(
                    committed,
                    assets=tuple(stored_assets),
                    snapshot_assets=tuple(stored_links),
                    asset_evidence=tuple(stored_asset_evidence),
                )
                if (
                    canonical_snapshot is not None
                    and source_row is not None
                    and str(source_row["status"]) == "published"
                    and changed
                ):
                    # Review is currently Source-scoped. A retry may verify a
                    # published graph, but cannot repair/add subordinate facts
                    # without an explicit snapshot-bound reconciliation flow.
                    raise ServerWebEvidenceConflict(
                        "published web evidence requires explicit reconciliation"
                    )
                stored_source = self._repository.get_source_in_transaction(
                    connection,
                    context.project_id,
                    committed.source.source_id,
                )
                if stored_source is None:
                    raise ServerWebEvidenceConflict(
                        "stored web source is unavailable"
                    )
                stored_product = None
                if committed.product is not None:
                    stored_product = self._catalog.get_product_in_transaction(
                        connection,
                        context.project_id,
                        committed.product.product_id,
                    )
                    if stored_product is None:
                        raise ServerWebEvidenceConflict(
                            "stored web product is unavailable"
                        )
                committed = replace(
                    committed,
                    source=stored_source,
                    product=stored_product,
                )
                if changed:
                    self._append_audit(
                        connection,
                        committed,
                        reconciled=canonical_snapshot is not None,
                    )
                return WebPageIngestionResult(
                    source=committed.source,
                    snapshot=committed.snapshot,
                    chunks=committed.chunks,
                    classification=committed.classification,
                    product=committed.product,
                    assets=committed.assets,
                    warnings=committed.warnings,
                )
        except (ProjectAccessDenied, ServerWebEvidenceConflict):
            raise
        except (
            IntegrityError,
            KnowledgeConflictError,
            KnowledgeAssetConflictError,
            KnowledgeAssetNotFound,
            ProductCatalogConflictError,
            ProductCatalogNotFound,
            ValueError,
        ) as exc:
            raise ServerWebEvidenceConflict(
                "web evidence conflicts with existing project data"
            ) from exc
        except (RuntimeError, SQLAlchemyError) as exc:
            raise ServerWebEvidenceUnavailable(
                "web evidence ingestion is temporarily unavailable"
            ) from exc

    def _verify_object_scope(
        self,
        prepared: PreparedWebPageIngestion,
    ) -> None:
        context = self._context
        if not _scoped_object_uri(
            prepared.snapshot.raw_artifact_uri,
            bucket=self._bucket,
            organization_id=context.actor.organization_id,
            project_id=context.project_id,
            content_hash=prepared.snapshot.content_hash,
        ) or not _scoped_object_uri(
            prepared.snapshot.normalized_artifact_uri,
            bucket=self._bucket,
            organization_id=context.actor.organization_id,
            project_id=context.project_id,
            content_hash=prepared.normalized_content_hash,
        ):
            raise ServerWebEvidenceConflict(
                "web artifacts are outside the server scope"
            )
        if any(
            not _scoped_object_uri(
                asset.artifact_uri,
                bucket=self._bucket,
                organization_id=context.actor.organization_id,
                project_id=context.project_id,
                content_hash=asset.content_hash,
            )
            for asset in prepared.assets
        ):
            raise ServerWebEvidenceConflict(
                "web assets are outside the server scope"
            )

    def _append_audit(
        self,
        connection: Connection,
        prepared: PreparedWebPageIngestion,
        *,
        reconciled: bool,
    ) -> None:
        context = self._context
        action = (
            "knowledge.web_snapshot.reconciled"
            if reconciled
            else "knowledge.web_snapshot.ingested"
        )
        identity = "\x1f".join(
            (
                context.actor.organization_id,
                context.project_id,
                prepared.source.source_id,
                prepared.snapshot.snapshot_id,
                action,
            )
        )
        self._audit.append(
            connection,
            AuditEvent(
                organization_id=context.actor.organization_id,
                event_id=(
                    "web_reconcile_" + uuid.uuid4().hex
                    if reconciled
                    else "web_ingestion_"
                    + uuid.uuid5(uuid.NAMESPACE_URL, identity).hex
                ),
                actor_user_id=context.actor.user_id,
                project_id=context.project_id,
                action=action,
                target_type="knowledge_source",
                target_id=prepared.source.source_id,
                details={
                    "operation": context.operation,
                    "context_type": context.target_type,
                    "context_id": context.target_id,
                    "source_kind": prepared.source.source_kind,
                    "page_type": prepared.classification.page_type,
                    "chunk_count": len(prepared.chunks),
                    "asset_count": len(prepared.assets),
                    "product_evidence_count": (
                        0 if prepared.source_evidence is None else 1
                    ),
                    "warning_count": len(prepared.warnings),
                },
            ),
        )


__all__ = [
    "CheckpointingOfficialSiteFetcher",
    "PostgresServerWebEvidenceIngestion",
    "ServerWebEvidenceConflict",
    "ServerWebEvidenceContext",
    "ServerWebEvidenceUnavailable",
]
