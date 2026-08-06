from __future__ import annotations

import uuid
from typing import Mapping

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from knowledge_agent.catalog import (
    PostgresProductCatalogRepository,
    ProductConfirmationError,
    ProductCatalogRepositoryError,
)
from knowledge_agent.contracts import (
    SourceKind,
    TrustTier,
)
from knowledge_agent.publication import (
    KnowledgePublicationError,
    KnowledgePublicationService,
    PublicationResult,
)
from knowledge_agent.repository import (
    KnowledgeRecordNotFound,
    KnowledgeRepositoryError,
    PostgresKnowledgeRepository,
)
from knowledge_agent.schema import (
    knowledge_chunks,
    knowledge_product_source_evidence,
    knowledge_sources,
    source_snapshots,
)
from knowledge_agent.snapshot_reviews import (
    PostgresSnapshotReviewRepository,
    ReviewDecision,
    SnapshotReviewConflict,
    SnapshotReviewReceipt,
    SnapshotReviewRepositoryError,
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


class ServerKnowledgeCommandUnavailable(RuntimeError):
    """A Server Knowledge write could not be committed safely."""


def _required_text(
    value: str,
    field_name: str,
    *,
    max_length: int | None = None,
) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    if max_length is not None and len(normalized) > max_length:
        raise ValueError(f"{field_name} is too long")
    return normalized


def _snapshot_source_projection(
    metadata: Mapping[str, object],
) -> dict[str, object]:
    projection = metadata.get("source_projection")
    if not isinstance(projection, Mapping):
        return {}
    if projection.get("schema_version") != 1:
        return {}
    display_name = projection.get("display_name")
    public_source = projection.get("public_source")
    canonical_url = projection.get("canonical_url")
    source_metadata = projection.get("metadata")
    if not isinstance(display_name, str) or not display_name.strip():
        return {}
    if not isinstance(public_source, bool):
        return {}
    if canonical_url is not None and (
        not isinstance(canonical_url, str) or not canonical_url.strip()
    ):
        return {}
    if public_source and canonical_url is None:
        return {}
    if not isinstance(source_metadata, Mapping):
        return {}
    return {
        "display_name": display_name.strip(),
        "public_source": public_source,
        "canonical_url": canonical_url,
        "metadata": dict(source_metadata),
    }


class PostgresServerKnowledgeCommands:
    """Atomic Server commands for Knowledge review, publish, and confirm.

    HTTP authorization is an early gate only. Every command locks all database
    facts that can revoke the required permission, performs the business write,
    and appends its redacted audit event in the same transaction.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        repository: PostgresKnowledgeRepository,
        catalog: PostgresProductCatalogRepository,
        publication: KnowledgePublicationService | None,
        audit: AuditEventWriter | None = None,
        reviews: PostgresSnapshotReviewRepository | None = None,
    ) -> None:
        self._engine = engine
        self._repository = repository
        self._catalog = catalog
        self._publication = publication
        self._reviews = reviews or PostgresSnapshotReviewRepository(engine)
        self._access_repository = PostgresProjectAccessRepository(engine)
        self._audit = audit or PostgresAuditEventWriter()

    def _lock_access(
        self,
        connection: Connection,
        actor: ActorIdentity,
        project_id: str,
        permission: ProjectPermission,
    ) -> None:
        facts = self._access_repository.lock_project_access_in_connection(
            connection,
            actor,
            project_id,
        )
        if not decide_project_permission(facts, permission).allowed:
            raise ProjectAccessDenied("project access denied")

    def _append_audit(
        self,
        connection: Connection,
        *,
        actor: ActorIdentity,
        project_id: str,
        action: str,
        target_type: str,
        target_id: str,
        details: Mapping[str, object],
    ) -> None:
        self._audit.append(
            connection,
            AuditEvent(
                organization_id=actor.organization_id,
                event_id=f"knowledge_{uuid.uuid4().hex}",
                actor_user_id=actor.user_id,
                project_id=project_id,
                action=action,
                target_type=target_type,
                target_id=target_id,
                details=dict(details),
            ),
        )

    def review_snapshot(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        source_id: str,
        snapshot_id: str,
        receipt_id: str,
        source_kind: SourceKind,
        trust_tier: TrustTier,
        decision: ReviewDecision,
        reason: str,
    ) -> SnapshotReviewReceipt:
        normalized_project_id = _required_text(project_id, "project_id")
        normalized_source_id = _required_text(source_id, "source_id")
        normalized_snapshot_id = _required_text(snapshot_id, "snapshot_id")
        normalized_receipt_id = _required_text(receipt_id, "receipt_id")
        normalized_reason = _required_text(
            reason,
            "reason",
            max_length=500,
        )
        status_by_decision = {
            "approve": "inbox",
            "needs_review": "needs_review",
            "reject": "rejected",
        }
        if decision not in status_by_decision:
            raise ValueError("review decision is unsupported")

        try:
            with self._engine.begin() as connection:
                self._lock_access(
                    connection,
                    actor,
                    normalized_project_id,
                    "knowledge.edit",
                )
                row = connection.execute(
                    sa.select(knowledge_sources)
                    .where(
                        knowledge_sources.c.project_id
                        == normalized_project_id,
                        knowledge_sources.c.source_id
                        == normalized_source_id,
                    )
                    .with_for_update()
                ).mappings().one_or_none()
                if row is None:
                    raise KnowledgeRecordNotFound(
                        "knowledge source was not found in the requested project"
                    )
                appended = self._reviews.append_review_in_transaction(
                    connection,
                    project_id=normalized_project_id,
                    source_id=normalized_source_id,
                    snapshot_id=normalized_snapshot_id,
                    receipt_id=normalized_receipt_id,
                    decision=decision,
                    source_kind=source_kind,
                    trust_tier=trust_tier,
                    reason=normalized_reason,
                    reviewer_kind="user",
                    reviewer_id=actor.user_id,
                )
                if not appended.created:
                    return appended.receipt
                if row["pending_snapshot_id"] != normalized_snapshot_id:
                    raise KnowledgePublicationError(
                        "only the pending snapshot can be reviewed"
                    )
                values: dict[str, object] = {}
                if row["current_snapshot_id"] is None:
                    values.update(
                        source_kind=source_kind,
                        trust_tier=trust_tier,
                        status=status_by_decision[decision],
                    )
                if decision == "reject":
                    values["pending_snapshot_id"] = None
                if values:
                    values["updated_at"] = sa.func.now()
                    connection.execute(
                        knowledge_sources.update()
                        .where(
                            knowledge_sources.c.project_id
                            == normalized_project_id,
                            knowledge_sources.c.source_id
                            == normalized_source_id,
                        )
                        .values(**values)
                    )
                self._append_audit(
                    connection,
                    actor=actor,
                    project_id=normalized_project_id,
                    action="knowledge.snapshot.reviewed",
                    target_type="source_snapshot",
                    target_id=normalized_snapshot_id,
                    details={
                        "source_id": normalized_source_id,
                        "decision": decision,
                        "source_kind": source_kind,
                        "trust_tier": trust_tier,
                        "review_version": appended.receipt.review_version,
                        "receipt_id": appended.receipt.receipt_id,
                    },
                )
                return appended.receipt
        except (
            KnowledgePublicationError,
            KnowledgeRecordNotFound,
            KnowledgeRepositoryError,
            ProjectAccessDenied,
            SnapshotReviewConflict,
            SnapshotReviewRepositoryError,
            ValueError,
        ):
            raise
        except (RuntimeError, SQLAlchemyError) as exc:
            raise ServerKnowledgeCommandUnavailable(
                "knowledge snapshot review could not be committed"
            ) from exc

    def publish_source(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        source_id: str,
        snapshot_id: str,
    ) -> PublicationResult:
        normalized_project_id = _required_text(project_id, "project_id")
        normalized_source_id = _required_text(source_id, "source_id")
        normalized_snapshot_id = _required_text(snapshot_id, "snapshot_id")
        if self._publication is None:
            raise ServerKnowledgeCommandUnavailable(
                "embedding provider is not configured"
            )

        try:
            with self._engine.begin() as connection:
                self._lock_access(
                    connection,
                    actor,
                    normalized_project_id,
                    "knowledge.publish",
                )
                source = connection.execute(
                    sa.select(
                        knowledge_sources.c.status,
                        knowledge_sources.c.current_snapshot_id,
                        knowledge_sources.c.pending_snapshot_id,
                    )
                    .where(
                        knowledge_sources.c.project_id
                        == normalized_project_id,
                        knowledge_sources.c.source_id
                        == normalized_source_id,
                    )
                    .with_for_update()
                ).mappings().one_or_none()
                if source is None:
                    raise KnowledgePublicationError(
                        "knowledge source was not found in the requested project"
                    )
                already_active = (
                    source["status"] == "published"
                    and source["current_snapshot_id"]
                    == normalized_snapshot_id
                    and source["pending_snapshot_id"] is None
                )
                if (
                    not already_active
                    and source["pending_snapshot_id"]
                    != normalized_snapshot_id
                ):
                    raise KnowledgePublicationError(
                        "only the pending snapshot can be published"
                    )
                approved_receipt = (
                    self._reviews.get_latest_review_in_transaction(
                        connection,
                        normalized_project_id,
                        normalized_source_id,
                        normalized_snapshot_id,
                    )
                )
                if (
                    approved_receipt is None
                    or approved_receipt.decision != "approve"
                ):
                    raise KnowledgePublicationError(
                        "snapshot classification must be approved before publication"
                    )
                if already_active:
                    total_chunks = connection.execute(
                        sa.select(sa.func.count())
                        .select_from(knowledge_chunks)
                        .where(
                            knowledge_chunks.c.project_id
                            == normalized_project_id,
                            knowledge_chunks.c.source_id
                            == normalized_source_id,
                            knowledge_chunks.c.snapshot_id
                            == normalized_snapshot_id,
                        )
                    ).scalar_one()
                    incomplete_chunks = connection.execute(
                        sa.select(sa.func.count())
                        .select_from(knowledge_chunks)
                        .where(
                            knowledge_chunks.c.project_id
                            == normalized_project_id,
                            knowledge_chunks.c.source_id
                            == normalized_source_id,
                            knowledge_chunks.c.snapshot_id
                            == normalized_snapshot_id,
                            sa.or_(
                                knowledge_chunks.c.embedding.is_(None),
                                knowledge_chunks.c.embedding_model
                                != self._publication.embedding_model,
                            ),
                        )
                    ).scalar_one()
                    if total_chunks and not incomplete_chunks:
                        return PublicationResult(
                            project_id=normalized_project_id,
                            source_id=normalized_source_id,
                            snapshot_id=normalized_snapshot_id,
                            embedding_model=(
                                self._publication.embedding_model
                            ),
                            chunk_count=int(total_chunks),
                        )
                expected_current_snapshot_id = source[
                    "current_snapshot_id"
                ]
        except (
            KnowledgePublicationError,
            ProjectAccessDenied,
            SnapshotReviewRepositoryError,
            ValueError,
        ):
            raise
        except (RuntimeError, SQLAlchemyError) as exc:
            raise ServerKnowledgeCommandUnavailable(
                "knowledge snapshot publication is unavailable"
            ) from exc

        candidate = self._publication.prepare_snapshot(
            project_id=normalized_project_id,
            source_id=normalized_source_id,
            snapshot_id=normalized_snapshot_id,
        )

        try:
            with self._engine.begin() as connection:
                self._lock_access(
                    connection,
                    actor,
                    normalized_project_id,
                    "knowledge.publish",
                )
                source = connection.execute(
                    sa.select(
                        knowledge_sources.c.status,
                        knowledge_sources.c.current_snapshot_id,
                        knowledge_sources.c.pending_snapshot_id,
                    )
                    .where(
                        knowledge_sources.c.project_id
                        == normalized_project_id,
                        knowledge_sources.c.source_id
                        == normalized_source_id,
                    )
                    .with_for_update()
                ).mappings().one_or_none()
                if source is None:
                    raise KnowledgePublicationError(
                        "knowledge source was not found in the requested project"
                    )
                latest_receipt = (
                    self._reviews.get_latest_review_in_transaction(
                        connection,
                        normalized_project_id,
                        normalized_source_id,
                        normalized_snapshot_id,
                        for_update=True,
                    )
                )
                if (
                    latest_receipt is None
                    or latest_receipt.decision != "approve"
                    or latest_receipt.receipt_id
                    != approved_receipt.receipt_id
                    or latest_receipt.review_version
                    != approved_receipt.review_version
                ):
                    raise KnowledgePublicationError(
                        "snapshot review changed during publication"
                    )
                already_active = (
                    source["status"] == "published"
                    and source["current_snapshot_id"] == candidate.snapshot_id
                    and source["pending_snapshot_id"] is None
                )
                if not already_active:
                    if (
                        source["pending_snapshot_id"]
                        != candidate.snapshot_id
                    ):
                        raise KnowledgePublicationError(
                            "pending snapshot changed during publication"
                        )
                    if (
                        source["current_snapshot_id"]
                        != expected_current_snapshot_id
                    ):
                        raise KnowledgePublicationError(
                            "current snapshot changed during publication"
                        )
                    self._repository.activate_snapshot_in_transaction(
                        connection,
                        candidate.project_id,
                        candidate.source_id,
                        candidate.snapshot_id,
                        candidate.embedding_model,
                    )
                    snapshot_metadata = connection.execute(
                        sa.select(source_snapshots.c.metadata).where(
                            source_snapshots.c.project_id
                            == normalized_project_id,
                            source_snapshots.c.source_id
                            == normalized_source_id,
                            source_snapshots.c.snapshot_id
                            == candidate.snapshot_id,
                        )
                    ).scalar_one()
                    aggregate_values = _snapshot_source_projection(
                        dict(snapshot_metadata or {})
                    )
                    aggregate_values.update(
                        source_kind=latest_receipt.source_kind,
                        trust_tier=latest_receipt.trust_tier,
                        updated_at=sa.func.now(),
                    )
                    connection.execute(
                        knowledge_sources.update()
                        .where(
                            knowledge_sources.c.project_id
                            == normalized_project_id,
                            knowledge_sources.c.source_id
                            == normalized_source_id,
                        )
                        .values(**aggregate_values)
                    )
                    self._append_audit(
                        connection,
                        actor=actor,
                        project_id=normalized_project_id,
                        action="knowledge.snapshot.published",
                        target_type="source_snapshot",
                        target_id=candidate.snapshot_id,
                        details={
                            "source_id": normalized_source_id,
                            "snapshot_id": candidate.snapshot_id,
                            "previous_snapshot_id": (
                                expected_current_snapshot_id
                            ),
                            "review_version": (
                                latest_receipt.review_version
                            ),
                            "receipt_id": latest_receipt.receipt_id,
                            "chunk_count": candidate.chunk_count,
                            "embedding_model": candidate.embedding_model,
                        },
                    )
        except (
            KnowledgePublicationError,
            KnowledgeRepositoryError,
            ProjectAccessDenied,
            SnapshotReviewRepositoryError,
            ValueError,
        ):
            raise
        except (RuntimeError, SQLAlchemyError) as exc:
            raise ServerKnowledgeCommandUnavailable(
                "knowledge snapshot publication could not be committed"
            ) from exc

        return PublicationResult(
            project_id=candidate.project_id,
            source_id=candidate.source_id,
            snapshot_id=candidate.snapshot_id,
            embedding_model=candidate.embedding_model,
            chunk_count=candidate.chunk_count,
        )

    def confirm_product(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        product_id: str,
    ) -> bool:
        normalized_project_id = _required_text(project_id, "project_id")
        normalized_product_id = _required_text(product_id, "product_id")
        try:
            with self._engine.begin() as connection:
                self._lock_access(
                    connection,
                    actor,
                    normalized_project_id,
                    "knowledge.publish",
                )
                current_primary_detail = connection.execute(
                    sa.select(
                        knowledge_product_source_evidence.c.product_id
                    )
                    .join(
                        knowledge_sources,
                        sa.and_(
                            knowledge_sources.c.project_id
                            == knowledge_product_source_evidence.c.project_id,
                            knowledge_sources.c.source_id
                            == knowledge_product_source_evidence.c.source_id,
                            knowledge_sources.c.current_snapshot_id
                            == knowledge_product_source_evidence.c.snapshot_id,
                            knowledge_sources.c.status == "published",
                        ),
                    )
                    .where(
                        knowledge_product_source_evidence.c.project_id
                        == normalized_project_id,
                        knowledge_product_source_evidence.c.product_id
                        == normalized_product_id,
                        knowledge_product_source_evidence.c.relation
                        == "primary_detail",
                    )
                    .limit(1)
                ).scalar_one_or_none()
                if current_primary_detail is None:
                    raise ProductConfirmationError(
                        "product requires current published primary detail "
                        "evidence before confirmation"
                    )
                changed = self._catalog.confirm_product_in_transaction(
                    connection,
                    normalized_project_id,
                    normalized_product_id,
                )
                if changed:
                    self._append_audit(
                        connection,
                        actor=actor,
                        project_id=normalized_project_id,
                        action="knowledge.product.confirmed",
                        target_type="knowledge_product",
                        target_id=normalized_product_id,
                        details={"status": "confirmed"},
                    )
                return changed
        except (
            ProductCatalogRepositoryError,
            ProjectAccessDenied,
            ValueError,
        ):
            raise
        except (RuntimeError, SQLAlchemyError) as exc:
            raise ServerKnowledgeCommandUnavailable(
                "knowledge product confirmation could not be committed"
            ) from exc


__all__ = [
    "PostgresServerKnowledgeCommands",
    "ReviewDecision",
    "ServerKnowledgeCommandUnavailable",
]
