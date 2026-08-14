from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from .contracts import SourceSnapshot
from .contracts import KnowledgeChunk, KnowledgeSource
from .repository import _chunk_from_row
from .schema import (
    knowledge_assets,
    knowledge_chunks,
    knowledge_products,
    knowledge_sources,
    snapshot_assets,
    source_snapshot_review_receipts,
    source_snapshots,
)


@dataclass(frozen=True, slots=True)
class KnowledgeSourceSummary:
    project_id: str
    source_id: str
    display_name: str
    source_kind: str
    trust_tier: str
    status: str
    public_source: bool
    canonical_url: str | None
    current_snapshot_id: str | None
    snapshot_count: int
    chunk_count: int
    asset_count: int
    latest_fetched_at: datetime | None
    latest_snapshot_id: str | None
    metadata: Mapping[str, object]
    pending_snapshot_id: str | None = None
    pending_fetched_at: datetime | None = None
    pending_chunk_count: int = 0
    pending_asset_count: int = 0
    pending_review_decision: str | None = None
    pending_review_reason: str | None = None
    pending_review_version: int | None = None
    pending_reviewed_at: datetime | None = None

    @property
    def classification_reason(self) -> str:
        ingestion = self.metadata.get("ingestion")
        if isinstance(ingestion, Mapping):
            reason = ingestion.get("classification_reason")
            if isinstance(reason, str):
                return reason
        classification = self.metadata.get("classification")
        if isinstance(classification, Mapping):
            reason = classification.get("reason")
            if isinstance(reason, str):
                return reason
            reasons = classification.get("reasons")
            if isinstance(reasons, list) and all(
                isinstance(item, str) for item in reasons
            ):
                return "; ".join(reasons)
        return ""

    @property
    def review_decision(self) -> str | None:
        if self.pending_snapshot_id is not None:
            return self.pending_review_decision
        review = self.metadata.get("review")
        if not isinstance(review, Mapping):
            return None
        decision = review.get("decision")
        return decision if isinstance(decision, str) else None


@dataclass(frozen=True, slots=True)
class KnowledgeLibrarySummary:
    project_id: str
    source_count: int
    inbox_count: int
    pending_count: int
    published_count: int
    product_count: int
    confirmed_product_count: int
    asset_count: int


class PostgresKnowledgeLibrary:
    """Read model for the project-level M2 knowledge page."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def summary(self, project_id: str) -> KnowledgeLibrarySummary:
        normalized_project_id = _required_text(project_id, "project_id")
        with self._engine.connect() as connection:
            source_counts = connection.execute(
                sa.select(
                    sa.func.count().label("total"),
                    sa.func.count()
                    .filter(knowledge_sources.c.status == "inbox")
                    .label("inbox"),
                    sa.func.count()
                    .filter(knowledge_sources.c.status == "published")
                    .label("published"),
                    sa.func.count()
                    .filter(
                        knowledge_sources.c.pending_snapshot_id.is_not(None)
                    )
                    .label("pending"),
                ).where(knowledge_sources.c.project_id == normalized_project_id)
            ).mappings().one()
            product_counts = connection.execute(
                sa.select(
                    sa.func.count().label("total"),
                    sa.func.count()
                    .filter(knowledge_products.c.status == "confirmed")
                    .label("confirmed"),
                ).where(knowledge_products.c.project_id == normalized_project_id)
            ).mappings().one()
            asset_count = connection.execute(
                sa.select(sa.func.count())
                .select_from(knowledge_assets)
                .where(knowledge_assets.c.project_id == normalized_project_id)
            ).scalar_one()
        return KnowledgeLibrarySummary(
            project_id=normalized_project_id,
            source_count=int(source_counts["total"]),
            inbox_count=int(source_counts["inbox"]),
            pending_count=int(source_counts["pending"]),
            published_count=int(source_counts["published"]),
            product_count=int(product_counts["total"]),
            confirmed_product_count=int(product_counts["confirmed"]),
            asset_count=int(asset_count),
        )

    def list_sources(self, project_id: str) -> tuple[KnowledgeSourceSummary, ...]:
        normalized_project_id = _required_text(project_id, "project_id")
        snapshot_counts = (
            sa.select(
                source_snapshots.c.project_id,
                source_snapshots.c.source_id,
                sa.func.count().label("snapshot_count"),
                sa.func.max(source_snapshots.c.fetched_at).label("latest_fetched_at"),
            )
            .group_by(
                source_snapshots.c.project_id,
                source_snapshots.c.source_id,
            )
            .subquery()
        )
        chunk_counts = (
            sa.select(
                knowledge_chunks.c.project_id,
                knowledge_chunks.c.source_id,
                sa.func.count().label("chunk_count"),
            )
            .group_by(
                knowledge_chunks.c.project_id,
                knowledge_chunks.c.source_id,
            )
            .subquery()
        )
        asset_counts = (
            sa.select(
                snapshot_assets.c.project_id,
                snapshot_assets.c.source_id,
                sa.func.count(sa.distinct(snapshot_assets.c.asset_id)).label(
                    "asset_count"
                ),
            )
            .group_by(
                snapshot_assets.c.project_id,
                snapshot_assets.c.source_id,
            )
            .subquery()
        )
        latest_snapshot_id = (
            sa.select(source_snapshots.c.snapshot_id)
            .where(
                source_snapshots.c.project_id == knowledge_sources.c.project_id,
                source_snapshots.c.source_id == knowledge_sources.c.source_id,
            )
            .order_by(
                source_snapshots.c.fetched_at.desc(),
                source_snapshots.c.snapshot_id.desc(),
            )
            .limit(1)
            .scalar_subquery()
        )
        pending_fetched_at = (
            sa.select(source_snapshots.c.fetched_at)
            .where(
                source_snapshots.c.project_id
                == knowledge_sources.c.project_id,
                source_snapshots.c.source_id
                == knowledge_sources.c.source_id,
                source_snapshots.c.snapshot_id
                == knowledge_sources.c.pending_snapshot_id,
            )
            .scalar_subquery()
        )
        pending_chunk_count = (
            sa.select(sa.func.count())
            .select_from(knowledge_chunks)
            .where(
                knowledge_chunks.c.project_id
                == knowledge_sources.c.project_id,
                knowledge_chunks.c.source_id
                == knowledge_sources.c.source_id,
                knowledge_chunks.c.snapshot_id
                == knowledge_sources.c.pending_snapshot_id,
            )
            .scalar_subquery()
        )
        pending_asset_count = (
            sa.select(sa.func.count(sa.distinct(snapshot_assets.c.asset_id)))
            .select_from(snapshot_assets)
            .where(
                snapshot_assets.c.project_id
                == knowledge_sources.c.project_id,
                snapshot_assets.c.source_id
                == knowledge_sources.c.source_id,
                snapshot_assets.c.snapshot_id
                == knowledge_sources.c.pending_snapshot_id,
            )
            .scalar_subquery()
        )
        latest_review_decision = (
            sa.select(source_snapshot_review_receipts.c.decision)
            .where(
                source_snapshot_review_receipts.c.project_id
                == knowledge_sources.c.project_id,
                source_snapshot_review_receipts.c.source_id
                == knowledge_sources.c.source_id,
                source_snapshot_review_receipts.c.snapshot_id
                == knowledge_sources.c.pending_snapshot_id,
            )
            .order_by(
                source_snapshot_review_receipts.c.review_version.desc()
            )
            .limit(1)
            .scalar_subquery()
        )
        latest_review_version = (
            sa.select(source_snapshot_review_receipts.c.review_version)
            .where(
                source_snapshot_review_receipts.c.project_id
                == knowledge_sources.c.project_id,
                source_snapshot_review_receipts.c.source_id
                == knowledge_sources.c.source_id,
                source_snapshot_review_receipts.c.snapshot_id
                == knowledge_sources.c.pending_snapshot_id,
            )
            .order_by(
                source_snapshot_review_receipts.c.review_version.desc()
            )
            .limit(1)
            .scalar_subquery()
        )
        latest_review_reason = (
            sa.select(source_snapshot_review_receipts.c.reason)
            .where(
                source_snapshot_review_receipts.c.project_id
                == knowledge_sources.c.project_id,
                source_snapshot_review_receipts.c.source_id
                == knowledge_sources.c.source_id,
                source_snapshot_review_receipts.c.snapshot_id
                == knowledge_sources.c.pending_snapshot_id,
            )
            .order_by(
                source_snapshot_review_receipts.c.review_version.desc()
            )
            .limit(1)
            .scalar_subquery()
        )
        latest_reviewed_at = (
            sa.select(source_snapshot_review_receipts.c.reviewed_at)
            .where(
                source_snapshot_review_receipts.c.project_id
                == knowledge_sources.c.project_id,
                source_snapshot_review_receipts.c.source_id
                == knowledge_sources.c.source_id,
                source_snapshot_review_receipts.c.snapshot_id
                == knowledge_sources.c.pending_snapshot_id,
            )
            .order_by(
                source_snapshot_review_receipts.c.review_version.desc()
            )
            .limit(1)
            .scalar_subquery()
        )
        statement = (
            sa.select(
                knowledge_sources,
                sa.func.coalesce(snapshot_counts.c.snapshot_count, 0).label(
                    "snapshot_count"
                ),
                sa.func.coalesce(chunk_counts.c.chunk_count, 0).label(
                    "chunk_count"
                ),
                sa.func.coalesce(asset_counts.c.asset_count, 0).label(
                    "asset_count"
                ),
                snapshot_counts.c.latest_fetched_at,
                latest_snapshot_id.label("latest_snapshot_id"),
                pending_fetched_at.label("pending_fetched_at"),
                pending_chunk_count.label("pending_chunk_count"),
                pending_asset_count.label("pending_asset_count"),
                latest_review_decision.label("pending_review_decision"),
                latest_review_reason.label("pending_review_reason"),
                latest_review_version.label("pending_review_version"),
                latest_reviewed_at.label("pending_reviewed_at"),
            )
            .outerjoin(
                snapshot_counts,
                sa.and_(
                    snapshot_counts.c.project_id == knowledge_sources.c.project_id,
                    snapshot_counts.c.source_id == knowledge_sources.c.source_id,
                ),
            )
            .outerjoin(
                chunk_counts,
                sa.and_(
                    chunk_counts.c.project_id == knowledge_sources.c.project_id,
                    chunk_counts.c.source_id == knowledge_sources.c.source_id,
                ),
            )
            .outerjoin(
                asset_counts,
                sa.and_(
                    asset_counts.c.project_id == knowledge_sources.c.project_id,
                    asset_counts.c.source_id == knowledge_sources.c.source_id,
                ),
            )
            .where(knowledge_sources.c.project_id == normalized_project_id)
            .order_by(
                knowledge_sources.c.updated_at.desc(),
                knowledge_sources.c.source_id.asc(),
            )
        )
        with self._engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        return tuple(_source_summary_from_row(row) for row in rows)

    def get_snapshot(
        self,
        project_id: str,
        source_id: str,
        snapshot_id: str,
    ) -> SourceSnapshot | None:
        normalized_project_id = _required_text(project_id, "project_id")
        normalized_source_id = _required_text(source_id, "source_id")
        normalized_snapshot_id = _required_text(snapshot_id, "snapshot_id")
        with self._engine.connect() as connection:
            row = connection.execute(
                sa.select(source_snapshots).where(
                    source_snapshots.c.project_id == normalized_project_id,
                    source_snapshots.c.source_id == normalized_source_id,
                    source_snapshots.c.snapshot_id == normalized_snapshot_id,
                )
            ).mappings().one_or_none()
        if row is None:
            return None
        return SourceSnapshot(
            project_id=str(row["project_id"]),
            source_id=str(row["source_id"]),
            snapshot_id=str(row["snapshot_id"]),
            content_hash=str(row["content_hash"]),
            fetched_at=row["fetched_at"],  # type: ignore[arg-type]
            parser_name=str(row["parser_name"]),
            parser_version=str(row["parser_version"]),
            raw_artifact_uri=(
                None
                if row["raw_artifact_uri"] is None
                else str(row["raw_artifact_uri"])
            ),
            normalized_artifact_uri=(
                None
                if row["normalized_artifact_uri"] is None
                else str(row["normalized_artifact_uri"])
            ),
            metadata=dict(row["metadata"]),  # type: ignore[arg-type]
        )

    def get_source(
        self, project_id: str, source_id: str
    ) -> KnowledgeSource | None:
        normalized_project_id = _required_text(project_id, "project_id")
        normalized_source_id = _required_text(source_id, "source_id")
        with self._engine.connect() as connection:
            row = connection.execute(
                sa.select(knowledge_sources).where(
                    knowledge_sources.c.project_id == normalized_project_id,
                    knowledge_sources.c.source_id == normalized_source_id,
                )
            ).mappings().one_or_none()
        if row is None:
            return None
        return KnowledgeSource(
            project_id=str(row["project_id"]),
            source_id=str(row["source_id"]),
            display_name=str(row["display_name"]),
            source_kind=str(row["source_kind"]),  # type: ignore[arg-type]
            trust_tier=str(row["trust_tier"]),  # type: ignore[arg-type]
            status=str(row["status"]),  # type: ignore[arg-type]
            canonical_url=(
                None if row["canonical_url"] is None else str(row["canonical_url"])
            ),
            public_source=bool(row["public_source"]),
            current_snapshot_id=(
                None
                if row["current_snapshot_id"] is None
                else str(row["current_snapshot_id"])
            ),
            pending_snapshot_id=(
                None
                if row["pending_snapshot_id"] is None
                else str(row["pending_snapshot_id"])
            ),
            metadata=dict(row["metadata"]),  # type: ignore[arg-type]
        )

    def latest_snapshot(
        self, project_id: str, source_id: str
    ) -> SourceSnapshot | None:
        normalized_project_id = _required_text(project_id, "project_id")
        normalized_source_id = _required_text(source_id, "source_id")
        with self._engine.connect() as connection:
            snapshot_id = connection.execute(
                sa.select(source_snapshots.c.snapshot_id)
                .where(
                    source_snapshots.c.project_id == normalized_project_id,
                    source_snapshots.c.source_id == normalized_source_id,
                )
                .order_by(
                    source_snapshots.c.fetched_at.desc(),
                    source_snapshots.c.snapshot_id.desc(),
                )
                .limit(1)
            ).scalar_one_or_none()
        if snapshot_id is None:
            return None
        return self.get_snapshot(
            normalized_project_id,
            normalized_source_id,
            str(snapshot_id),
        )

    def get_snapshot_chunks(
        self,
        project_id: str,
        source_id: str,
        snapshot_id: str,
    ) -> tuple[KnowledgeChunk, ...]:
        normalized_project_id = _required_text(project_id, "project_id")
        normalized_source_id = _required_text(source_id, "source_id")
        normalized_snapshot_id = _required_text(snapshot_id, "snapshot_id")
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(knowledge_chunks)
                .where(
                    knowledge_chunks.c.project_id == normalized_project_id,
                    knowledge_chunks.c.source_id == normalized_source_id,
                    knowledge_chunks.c.snapshot_id == normalized_snapshot_id,
                )
                .order_by(knowledge_chunks.c.ordinal.asc())
            ).mappings().all()
        return tuple(_chunk_from_row(row) for row in rows)

    def get_snapshot_chunks_requiring_embedding(
        self,
        project_id: str,
        source_id: str,
        snapshot_id: str,
        embedding_model: str,
    ) -> tuple[KnowledgeChunk, ...]:
        """Return only chunks not yet prepared for one exact model."""

        normalized_project_id = _required_text(project_id, "project_id")
        normalized_source_id = _required_text(source_id, "source_id")
        normalized_snapshot_id = _required_text(snapshot_id, "snapshot_id")
        normalized_model = _required_text(embedding_model, "embedding_model")
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(knowledge_chunks)
                .where(
                    knowledge_chunks.c.project_id == normalized_project_id,
                    knowledge_chunks.c.source_id == normalized_source_id,
                    knowledge_chunks.c.snapshot_id == normalized_snapshot_id,
                    sa.or_(
                        knowledge_chunks.c.embedding.is_(None),
                        knowledge_chunks.c.embedding_model
                        != normalized_model,
                    ),
                )
                .order_by(knowledge_chunks.c.ordinal.asc())
            ).mappings().all()
        return tuple(_chunk_from_row(row) for row in rows)

    def find_snapshot_by_content(
        self,
        *,
        project_id: str,
        source_id: str,
        content_hash: str,
        parser_name: str,
        parser_version: str,
    ) -> SourceSnapshot | None:
        normalized_project_id = _required_text(project_id, "project_id")
        normalized_source_id = _required_text(source_id, "source_id")
        with self._engine.connect() as connection:
            snapshot_id = connection.execute(
                sa.select(source_snapshots.c.snapshot_id).where(
                    source_snapshots.c.project_id == normalized_project_id,
                    source_snapshots.c.source_id == normalized_source_id,
                    source_snapshots.c.content_hash == content_hash,
                    source_snapshots.c.parser_name == parser_name,
                    source_snapshots.c.parser_version == parser_version,
                )
            ).scalar_one_or_none()
        if snapshot_id is None:
            return None
        return self.get_snapshot(
            normalized_project_id,
            normalized_source_id,
            str(snapshot_id),
        )


def _required_text(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} is required")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized


def _source_summary_from_row(row: Mapping[str, object]) -> KnowledgeSourceSummary:
    return KnowledgeSourceSummary(
        project_id=str(row["project_id"]),
        source_id=str(row["source_id"]),
        display_name=str(row["display_name"]),
        source_kind=str(row["source_kind"]),
        trust_tier=str(row["trust_tier"]),
        status=str(row["status"]),
        public_source=bool(row["public_source"]),
        canonical_url=(
            None if row["canonical_url"] is None else str(row["canonical_url"])
        ),
        current_snapshot_id=(
            None
            if row["current_snapshot_id"] is None
            else str(row["current_snapshot_id"])
        ),
        snapshot_count=int(row["snapshot_count"]),
        chunk_count=int(row["chunk_count"]),
        asset_count=int(row["asset_count"]),
        latest_fetched_at=row["latest_fetched_at"],  # type: ignore[arg-type]
        latest_snapshot_id=(
            None
            if row["latest_snapshot_id"] is None
            else str(row["latest_snapshot_id"])
        ),
        metadata=dict(row["metadata"]),  # type: ignore[arg-type]
        pending_snapshot_id=(
            None
            if row["pending_snapshot_id"] is None
            else str(row["pending_snapshot_id"])
        ),
        pending_fetched_at=row["pending_fetched_at"],  # type: ignore[arg-type]
        pending_chunk_count=int(row["pending_chunk_count"] or 0),
        pending_asset_count=int(row["pending_asset_count"] or 0),
        pending_review_decision=(
            None
            if row["pending_review_decision"] is None
            else str(row["pending_review_decision"])
        ),
        pending_review_reason=(
            None
            if row["pending_review_reason"] is None
            else str(row["pending_review_reason"])
        ),
        pending_review_version=(
            None
            if row["pending_review_version"] is None
            else int(row["pending_review_version"])
        ),
        pending_reviewed_at=row["pending_reviewed_at"],  # type: ignore[arg-type]
    )
