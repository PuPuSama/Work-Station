from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Mapping

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection, Engine, RowMapping
from sqlalchemy.exc import SQLAlchemyError

from .contracts import SOURCE_KINDS, TRUST_TIERS, SourceKind, TrustTier
from .schema import source_snapshot_review_receipts, source_snapshots


ReviewDecision = Literal["approve", "needs_review", "reject"]
ReviewerKind = Literal["user", "automation", "legacy_migration"]

REVIEW_DECISIONS = frozenset({"approve", "needs_review", "reject"})
REVIEWER_KINDS = frozenset({"user", "automation", "legacy_migration"})


class SnapshotReviewRepositoryError(RuntimeError):
    """Base error for immutable snapshot-review persistence."""


class SnapshotReviewConflict(SnapshotReviewRepositoryError):
    """Raised when a stable receipt identity is reused with new content."""


def _required_text(
    value: str,
    field_name: str,
    *,
    max_length: int | None = None,
) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} is required")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    if max_length is not None and len(normalized) > max_length:
        raise ValueError(f"{field_name} is too long")
    return normalized


def _optional_text(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field_name)


def _timestamp(value: datetime | None, field_name: str) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _require_transaction(connection: Connection) -> None:
    if not connection.in_transaction():
        raise ValueError("snapshot review writes require a business transaction")


@dataclass(frozen=True, slots=True)
class SnapshotReviewReceipt:
    project_id: str
    source_id: str
    snapshot_id: str
    review_version: int
    receipt_id: str
    decision: ReviewDecision
    source_kind: SourceKind
    trust_tier: TrustTier
    reason: str
    reviewer_kind: ReviewerKind
    reviewer_id: str | None
    reviewed_at: datetime
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        for field_name in ("project_id", "source_id", "snapshot_id", "receipt_id"):
            object.__setattr__(
                self,
                field_name,
                _required_text(getattr(self, field_name), field_name),
            )
        if (
            isinstance(self.review_version, bool)
            or not isinstance(self.review_version, int)
            or self.review_version <= 0
        ):
            raise ValueError("review_version must be a positive integer")
        if self.decision not in REVIEW_DECISIONS:
            raise ValueError("review decision is unsupported")
        if self.source_kind not in SOURCE_KINDS:
            raise ValueError("source_kind is unsupported")
        if self.trust_tier not in TRUST_TIERS:
            raise ValueError("trust_tier is unsupported")
        if self.reviewer_kind not in REVIEWER_KINDS:
            raise ValueError("reviewer_kind is unsupported")
        object.__setattr__(
            self,
            "reason",
            _required_text(self.reason, "reason", max_length=500),
        )
        object.__setattr__(
            self,
            "reviewer_id",
            _optional_text(self.reviewer_id, "reviewer_id"),
        )
        if self.reviewer_kind == "legacy_migration":
            if self.reviewer_id is not None:
                raise ValueError(
                    "legacy migration reviews must not name a reviewer"
                )
        elif self.reviewer_id is None:
            raise ValueError("reviewer_id is required")
        object.__setattr__(
            self,
            "reviewed_at",
            _timestamp(self.reviewed_at, "reviewed_at"),
        )
        if self.created_at is not None:
            object.__setattr__(
                self,
                "created_at",
                _timestamp(self.created_at, "created_at"),
            )


@dataclass(frozen=True, slots=True)
class SnapshotReviewAppendResult:
    receipt: SnapshotReviewReceipt
    created: bool


def _receipt_from_row(
    row: Mapping[str, object] | RowMapping,
) -> SnapshotReviewReceipt:
    return SnapshotReviewReceipt(
        project_id=str(row["project_id"]),
        source_id=str(row["source_id"]),
        snapshot_id=str(row["snapshot_id"]),
        review_version=int(row["review_version"]),
        receipt_id=str(row["receipt_id"]),
        decision=str(row["decision"]),  # type: ignore[arg-type]
        source_kind=str(row["source_kind"]),  # type: ignore[arg-type]
        trust_tier=str(row["trust_tier"]),  # type: ignore[arg-type]
        reason=str(row["reason"]),
        reviewer_kind=str(row["reviewer_kind"]),  # type: ignore[arg-type]
        reviewer_id=(
            None if row["reviewer_id"] is None else str(row["reviewer_id"])
        ),
        reviewed_at=row["reviewed_at"],  # type: ignore[arg-type]
        created_at=row["created_at"],  # type: ignore[arg-type]
    )


def _business_signature(receipt: SnapshotReviewReceipt) -> tuple[object, ...]:
    return (
        receipt.project_id,
        receipt.source_id,
        receipt.snapshot_id,
        receipt.receipt_id,
        receipt.decision,
        receipt.source_kind,
        receipt.trust_tier,
        receipt.reason,
        receipt.reviewer_kind,
        receipt.reviewer_id,
    )


class PostgresSnapshotReviewRepository:
    """Append and read immutable, project-scoped Snapshot Review Receipts."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def append_review_in_transaction(
        self,
        connection: Connection,
        *,
        project_id: str,
        source_id: str,
        snapshot_id: str,
        receipt_id: str,
        decision: ReviewDecision,
        source_kind: SourceKind,
        trust_tier: TrustTier,
        reason: str,
        reviewer_kind: ReviewerKind,
        reviewer_id: str | None,
        reviewed_at: datetime | None = None,
    ) -> SnapshotReviewAppendResult:
        """Append one review in a caller-owned transaction.

        The Snapshot row lock serializes version allocation. Receipt identity is
        project-scoped, so a retry with the same immutable business payload is a
        no-op even when the caller supplies a different timestamp.
        """

        _require_transaction(connection)
        normalized_project_id = _required_text(project_id, "project_id")
        normalized_source_id = _required_text(source_id, "source_id")
        normalized_snapshot_id = _required_text(snapshot_id, "snapshot_id")
        normalized_receipt_id = _required_text(receipt_id, "receipt_id")
        normalized_reason = _required_text(reason, "reason", max_length=500)
        normalized_reviewer_id = _optional_text(reviewer_id, "reviewer_id")
        normalized_reviewed_at = _timestamp(reviewed_at, "reviewed_at")
        if decision not in REVIEW_DECISIONS:
            raise ValueError("review decision is unsupported")
        if source_kind not in SOURCE_KINDS:
            raise ValueError("source_kind is unsupported")
        if trust_tier not in TRUST_TIERS:
            raise ValueError("trust_tier is unsupported")
        if reviewer_kind not in REVIEWER_KINDS:
            raise ValueError("reviewer_kind is unsupported")
        if reviewer_kind == "legacy_migration":
            if normalized_reviewer_id is not None:
                raise ValueError(
                    "legacy migration reviews must not name a reviewer"
                )
        elif normalized_reviewer_id is None:
            raise ValueError("reviewer_id is required")

        requested = SnapshotReviewReceipt(
            project_id=normalized_project_id,
            source_id=normalized_source_id,
            snapshot_id=normalized_snapshot_id,
            review_version=1,
            receipt_id=normalized_receipt_id,
            decision=decision,
            source_kind=source_kind,
            trust_tier=trust_tier,
            reason=normalized_reason,
            reviewer_kind=reviewer_kind,
            reviewer_id=normalized_reviewer_id,
            reviewed_at=normalized_reviewed_at,
        )

        try:
            existing = self._get_by_receipt_id(
                connection,
                normalized_project_id,
                normalized_receipt_id,
            )
            if existing is not None:
                return self._idempotent_result(existing, requested)

            target_exists = connection.execute(
                sa.select(source_snapshots.c.snapshot_id)
                .where(
                    source_snapshots.c.project_id == normalized_project_id,
                    source_snapshots.c.source_id == normalized_source_id,
                    source_snapshots.c.snapshot_id == normalized_snapshot_id,
                )
                .with_for_update()
            ).scalar_one_or_none()
            if target_exists is None:
                raise SnapshotReviewRepositoryError(
                    "snapshot review target was not found in the requested project"
                )

            # Another transaction may have committed this receipt while this
            # transaction waited for the Snapshot row lock.
            existing = self._get_by_receipt_id(
                connection,
                normalized_project_id,
                normalized_receipt_id,
            )
            if existing is not None:
                return self._idempotent_result(existing, requested)

            latest_version = connection.execute(
                sa.select(
                    sa.func.coalesce(
                        sa.func.max(
                            source_snapshot_review_receipts.c.review_version
                        ),
                        0,
                    )
                ).where(
                    source_snapshot_review_receipts.c.project_id
                    == normalized_project_id,
                    source_snapshot_review_receipts.c.source_id
                    == normalized_source_id,
                    source_snapshot_review_receipts.c.snapshot_id
                    == normalized_snapshot_id,
                )
            ).scalar_one()
            next_version = int(latest_version) + 1

            row = connection.execute(
                insert(source_snapshot_review_receipts)
                .values(
                    project_id=normalized_project_id,
                    source_id=normalized_source_id,
                    snapshot_id=normalized_snapshot_id,
                    review_version=next_version,
                    receipt_id=normalized_receipt_id,
                    decision=decision,
                    source_kind=source_kind,
                    trust_tier=trust_tier,
                    reason=normalized_reason,
                    reviewer_kind=reviewer_kind,
                    reviewer_id=normalized_reviewer_id,
                    reviewed_at=normalized_reviewed_at,
                )
                .on_conflict_do_nothing()
                .returning(source_snapshot_review_receipts),
            ).mappings().one_or_none()
            if row is not None:
                return SnapshotReviewAppendResult(
                    receipt=_receipt_from_row(row),
                    created=True,
                )

            existing = self._get_by_receipt_id(
                connection,
                normalized_project_id,
                normalized_receipt_id,
            )
            if existing is None:
                raise SnapshotReviewConflict(
                    "snapshot review version could not be allocated"
                )
            return self._idempotent_result(existing, requested)
        except (SnapshotReviewConflict, SnapshotReviewRepositoryError):
            raise
        except SQLAlchemyError as exc:
            raise SnapshotReviewRepositoryError(
                "snapshot review could not be stored"
            ) from exc

    def get_latest_review(
        self,
        project_id: str,
        source_id: str,
        snapshot_id: str,
    ) -> SnapshotReviewReceipt | None:
        normalized_project_id = _required_text(project_id, "project_id")
        normalized_source_id = _required_text(source_id, "source_id")
        normalized_snapshot_id = _required_text(snapshot_id, "snapshot_id")
        try:
            with self._engine.connect() as connection:
                return self._get_latest_review(
                    connection,
                    normalized_project_id,
                    normalized_source_id,
                    normalized_snapshot_id,
                )
        except SQLAlchemyError as exc:
            raise SnapshotReviewRepositoryError(
                "snapshot review could not be read"
            ) from exc

    def get_latest_review_in_transaction(
        self,
        connection: Connection,
        project_id: str,
        source_id: str,
        snapshot_id: str,
        *,
        for_update: bool = False,
    ) -> SnapshotReviewReceipt | None:
        _require_transaction(connection)
        normalized_project_id = _required_text(project_id, "project_id")
        normalized_source_id = _required_text(source_id, "source_id")
        normalized_snapshot_id = _required_text(snapshot_id, "snapshot_id")
        try:
            return self._get_latest_review(
                connection,
                normalized_project_id,
                normalized_source_id,
                normalized_snapshot_id,
                for_update=for_update,
            )
        except SQLAlchemyError as exc:
            raise SnapshotReviewRepositoryError(
                "snapshot review could not be read"
            ) from exc

    def get_by_receipt_id_in_transaction(
        self,
        connection: Connection,
        project_id: str,
        receipt_id: str,
        *,
        for_update: bool = False,
    ) -> SnapshotReviewReceipt | None:
        _require_transaction(connection)
        normalized_project_id = _required_text(project_id, "project_id")
        normalized_receipt_id = _required_text(receipt_id, "receipt_id")
        try:
            return self._get_by_receipt_id(
                connection,
                normalized_project_id,
                normalized_receipt_id,
                for_update=for_update,
            )
        except SQLAlchemyError as exc:
            raise SnapshotReviewRepositoryError(
                "snapshot review could not be read"
            ) from exc

    @staticmethod
    def _idempotent_result(
        existing: SnapshotReviewReceipt,
        requested: SnapshotReviewReceipt,
    ) -> SnapshotReviewAppendResult:
        if _business_signature(existing) != _business_signature(requested):
            raise SnapshotReviewConflict(
                "snapshot review receipt already has different content"
            )
        return SnapshotReviewAppendResult(receipt=existing, created=False)

    @staticmethod
    def _get_latest_review(
        connection: Connection,
        project_id: str,
        source_id: str,
        snapshot_id: str,
        *,
        for_update: bool = False,
    ) -> SnapshotReviewReceipt | None:
        statement = (
            sa.select(source_snapshot_review_receipts)
            .where(
                source_snapshot_review_receipts.c.project_id == project_id,
                source_snapshot_review_receipts.c.source_id == source_id,
                source_snapshot_review_receipts.c.snapshot_id == snapshot_id,
            )
            .order_by(source_snapshot_review_receipts.c.review_version.desc())
            .limit(1)
        )
        if for_update:
            statement = statement.with_for_update()
        row = connection.execute(statement).mappings().one_or_none()
        return None if row is None else _receipt_from_row(row)

    @staticmethod
    def _get_by_receipt_id(
        connection: Connection,
        project_id: str,
        receipt_id: str,
        *,
        for_update: bool = False,
    ) -> SnapshotReviewReceipt | None:
        statement = sa.select(source_snapshot_review_receipts).where(
            source_snapshot_review_receipts.c.project_id == project_id,
            source_snapshot_review_receipts.c.receipt_id == receipt_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = connection.execute(statement).mappings().one_or_none()
        return None if row is None else _receipt_from_row(row)


__all__ = [
    "PostgresSnapshotReviewRepository",
    "ReviewDecision",
    "ReviewerKind",
    "SnapshotReviewAppendResult",
    "SnapshotReviewConflict",
    "SnapshotReviewReceipt",
    "SnapshotReviewRepositoryError",
]
