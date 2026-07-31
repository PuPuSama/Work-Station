from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal, Mapping

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from knowledge_agent.catalog import (
    PostgresProductCatalogRepository,
    ProductCatalogRepositoryError,
)
from knowledge_agent.contracts import (
    KnowledgeSource,
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
from knowledge_agent.schema import knowledge_sources, source_snapshots
from services.access_control import (
    ActorIdentity,
    PostgresProjectAccessRepository,
    ProjectAccessDenied,
    ProjectAccessService,
    ProjectPermission,
    decide_project_permission,
)
from services.audit_log import (
    AuditEvent,
    AuditEventWriter,
    PostgresAuditEventWriter,
)


ReviewDecision = Literal["approve", "needs_review", "reject"]


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
    ) -> None:
        self._engine = engine
        self._repository = repository
        self._catalog = catalog
        self._publication = publication
        self._access_repository = PostgresProjectAccessRepository(engine)
        self._access = ProjectAccessService(self._access_repository)
        self._audit = audit or PostgresAuditEventWriter()

    def _require_initial_access(
        self,
        actor: ActorIdentity,
        project_id: str,
        permission: ProjectPermission,
    ) -> None:
        self._access.require(actor, project_id, permission)

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

    def review_source(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        source_id: str,
        source_kind: SourceKind,
        trust_tier: TrustTier,
        decision: ReviewDecision,
        reason: str,
    ) -> KnowledgeSource:
        normalized_project_id = _required_text(project_id, "project_id")
        normalized_source_id = _required_text(source_id, "source_id")
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
                if row["status"] == "published":
                    raise KnowledgePublicationError(
                        "published source review requires a new snapshot"
                    )
                metadata = dict(row["metadata"] or {})
                metadata["review"] = {
                    "decision": decision,
                    "reason": normalized_reason,
                    "reviewed_at": datetime.now(timezone.utc).isoformat(),
                }
                reviewed = KnowledgeSource(
                    project_id=normalized_project_id,
                    source_id=normalized_source_id,
                    display_name=str(row["display_name"]),
                    source_kind=source_kind,
                    trust_tier=trust_tier,
                    status=status_by_decision[decision],  # type: ignore[arg-type]
                    canonical_url=(
                        None
                        if row["canonical_url"] is None
                        else str(row["canonical_url"])
                    ),
                    public_source=bool(row["public_source"]),
                    metadata=metadata,
                )
                self._repository.upsert_source_in_transaction(
                    connection,
                    reviewed,
                )
                self._append_audit(
                    connection,
                    actor=actor,
                    project_id=normalized_project_id,
                    action="knowledge.source.reviewed",
                    target_type="knowledge_source",
                    target_id=normalized_source_id,
                    details={
                        "decision": decision,
                        "source_kind": source_kind,
                        "trust_tier": trust_tier,
                    },
                )
                return reviewed
        except (
            KnowledgePublicationError,
            KnowledgeRecordNotFound,
            KnowledgeRepositoryError,
            ProjectAccessDenied,
            ValueError,
        ):
            raise
        except (RuntimeError, SQLAlchemyError) as exc:
            raise ServerKnowledgeCommandUnavailable(
                "knowledge source review could not be committed"
            ) from exc

    def publish_source(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        source_id: str,
        snapshot_id: str | None = None,
    ) -> PublicationResult:
        normalized_project_id = _required_text(project_id, "project_id")
        normalized_source_id = _required_text(source_id, "source_id")
        if self._publication is None:
            raise ServerKnowledgeCommandUnavailable(
                "embedding provider is not configured"
            )

        # Do not spend provider capacity for an actor who is already denied.
        # The authoritative recheck still happens in the commit transaction.
        try:
            self._require_initial_access(
                actor,
                normalized_project_id,
                "knowledge.publish",
            )
        except ProjectAccessDenied:
            raise
        except (RuntimeError, SQLAlchemyError) as exc:
            raise ServerKnowledgeCommandUnavailable(
                "knowledge source publication is unavailable"
            ) from exc
        candidate = self._publication.prepare(
            project_id=normalized_project_id,
            source_id=normalized_source_id,
            snapshot_id=snapshot_id,
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
                        knowledge_sources.c.metadata,
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
                review = dict(source["metadata"] or {}).get("review")
                if (
                    not isinstance(review, Mapping)
                    or review.get("decision") != "approve"
                ):
                    raise KnowledgePublicationError(
                        "source classification must be approved before publication"
                    )
                if snapshot_id is None:
                    latest_snapshot_id = connection.execute(
                        sa.select(source_snapshots.c.snapshot_id)
                        .where(
                            source_snapshots.c.project_id
                            == normalized_project_id,
                            source_snapshots.c.source_id
                            == normalized_source_id,
                        )
                        .order_by(
                            source_snapshots.c.fetched_at.desc(),
                            source_snapshots.c.snapshot_id.desc(),
                        )
                        .limit(1)
                    ).scalar_one_or_none()
                    if latest_snapshot_id != candidate.snapshot_id:
                        raise KnowledgePublicationError(
                            "source snapshot changed during publication"
                        )
                already_active = (
                    source["status"] == "published"
                    and source["current_snapshot_id"] == candidate.snapshot_id
                )
                if not already_active:
                    self._repository.activate_snapshot_in_transaction(
                        connection,
                        candidate.project_id,
                        candidate.source_id,
                        candidate.snapshot_id,
                        candidate.embedding_model,
                    )
                    self._append_audit(
                        connection,
                        actor=actor,
                        project_id=normalized_project_id,
                        action="knowledge.source.published",
                        target_type="knowledge_source",
                        target_id=normalized_source_id,
                        details={
                            "snapshot_id": candidate.snapshot_id,
                            "chunk_count": candidate.chunk_count,
                            "embedding_model": candidate.embedding_model,
                        },
                    )
        except (
            KnowledgePublicationError,
            KnowledgeRepositoryError,
            ProjectAccessDenied,
            ValueError,
        ):
            raise
        except (RuntimeError, SQLAlchemyError) as exc:
            raise ServerKnowledgeCommandUnavailable(
                "knowledge source publication could not be committed"
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
