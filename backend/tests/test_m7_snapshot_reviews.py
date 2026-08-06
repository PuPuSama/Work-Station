from __future__ import annotations

import hashlib
import os
import sys
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError, IntegrityError


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from knowledge_agent.database import create_knowledge_engine  # noqa: E402
from knowledge_agent.schema import (  # noqa: E402
    knowledge_chunks,
    knowledge_sources,
    projects,
    source_snapshot_review_receipts,
    source_snapshots,
)
from knowledge_agent.snapshot_reviews import (  # noqa: E402
    PostgresSnapshotReviewRepository,
    SnapshotReviewAppendResult,
    SnapshotReviewConflict,
    SnapshotReviewRepositoryError,
)


DATABASE_URL_ENV = "ARTICLE_AGENT_DATABASE_URL"
REVIEWED_AT = datetime(2026, 8, 6, 8, 0, tzinfo=timezone.utc)


@unittest.skipUnless(
    os.environ.get(DATABASE_URL_ENV),
    f"{DATABASE_URL_ENV} is required for PostgreSQL integration tests",
)
class M7SnapshotReviewPostgresTests(unittest.TestCase):
    engine: sa.Engine
    reviews: PostgresSnapshotReviewRepository

    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = create_knowledge_engine(os.environ[DATABASE_URL_ENV])
        with cls.engine.connect() as connection:
            connection.execute(sa.text("SELECT 1")).scalar_one()
        cls.reviews = PostgresSnapshotReviewRepository(cls.engine)

    @classmethod
    def tearDownClass(cls) -> None:
        if hasattr(cls, "engine"):
            cls.engine.dispose()

    def setUp(self) -> None:
        self.prefix = f"m7-snapshot-review-{uuid.uuid4().hex}"
        self.project_a = f"{self.prefix}-project-a"
        self.project_b = f"{self.prefix}-project-b"
        self.source_a = f"{self.prefix}-source-a"
        self.source_a_other = f"{self.prefix}-source-a-other"
        self.source_b = f"{self.prefix}-source-b"
        self.snapshot_a = f"{self.prefix}-snapshot-a"
        self.snapshot_a_other = f"{self.prefix}-snapshot-a-other"
        self.snapshot_b = f"{self.prefix}-snapshot-b"
        self.reviewer_id = f"{self.prefix}-reviewer"
        self.connection = self.engine.connect()
        self.transaction = self.connection.begin()
        try:
            self._seed()
        except BaseException:
            self.transaction.rollback()
            self.connection.close()
            raise

    def tearDown(self) -> None:
        if self.transaction.is_active:
            self.transaction.rollback()
        self.connection.close()

    def _seed(self) -> None:
        self.connection.execute(
            projects.insert(),
            (
                {
                    "project_id": self.project_a,
                    "customer_name": "Snapshot Review A",
                    "official_domain": f"{self.prefix}-a.example.test",
                },
                {
                    "project_id": self.project_b,
                    "customer_name": "Snapshot Review B",
                    "official_domain": f"{self.prefix}-b.example.test",
                },
            ),
        )
        self.connection.execute(
            knowledge_sources.insert(),
            (
                self._source_values(self.project_a, self.source_a),
                self._source_values(self.project_a, self.source_a_other),
                self._source_values(self.project_b, self.source_b),
            ),
        )
        snapshots = (
            (self.project_a, self.source_a, self.snapshot_a),
            (self.project_a, self.source_a_other, self.snapshot_a_other),
            (self.project_b, self.source_b, self.snapshot_b),
        )
        self.connection.execute(
            source_snapshots.insert(),
            tuple(
                self._snapshot_values(project_id, source_id, snapshot_id)
                for project_id, source_id, snapshot_id in snapshots
            ),
        )
        self.connection.execute(
            knowledge_chunks.insert(),
            tuple(
                {
                    "project_id": project_id,
                    "chunk_id": f"{snapshot_id}:0000",
                    "source_id": source_id,
                    "snapshot_id": snapshot_id,
                    "ordinal": 0,
                    "text": f"Review fixture for {snapshot_id}.",
                }
                for project_id, source_id, snapshot_id in snapshots
            ),
        )

    @staticmethod
    def _source_values(project_id: str, source_id: str) -> dict[str, object]:
        return {
            "project_id": project_id,
            "source_id": source_id,
            "display_name": source_id,
            "source_kind": "knowledge_page",
            "trust_tier": "reference_material",
            "status": "inbox",
            "public_source": False,
        }

    @staticmethod
    def _snapshot_values(
        project_id: str,
        source_id: str,
        snapshot_id: str,
    ) -> dict[str, object]:
        return {
            "project_id": project_id,
            "source_id": source_id,
            "snapshot_id": snapshot_id,
            "content_hash": hashlib.sha256(snapshot_id.encode("utf-8")).hexdigest(),
            "parser_name": "snapshot-review-test",
            "parser_version": "1",
            "fetched_at": REVIEWED_AT,
        }

    def _append(
        self,
        receipt_id: str,
        *,
        decision: str = "approve",
        reason: str = "Reviewed exact snapshot.",
        reviewed_at: datetime = REVIEWED_AT,
        reviewer_kind: str = "user",
        reviewer_id: str | None = None,
    ) -> SnapshotReviewAppendResult:
        return self.reviews.append_review_in_transaction(
            self.connection,
            project_id=self.project_a,
            source_id=self.source_a,
            snapshot_id=self.snapshot_a,
            receipt_id=receipt_id,
            decision=decision,  # type: ignore[arg-type]
            source_kind="knowledge_page",
            trust_tier="reference_material",
            reason=reason,
            reviewer_kind=reviewer_kind,  # type: ignore[arg-type]
            reviewer_id=self.reviewer_id if reviewer_id is None else reviewer_id,
            reviewed_at=reviewed_at,
        )

    def _receipt_values(
        self,
        *,
        review_version: int,
        receipt_id: str,
        decision: str = "approve",
    ) -> dict[str, object]:
        return {
            "project_id": self.project_a,
            "source_id": self.source_a,
            "snapshot_id": self.snapshot_a,
            "review_version": review_version,
            "receipt_id": receipt_id,
            "decision": decision,
            "source_kind": "knowledge_page",
            "trust_tier": "reference_material",
            "reason": "Direct constraint fixture.",
            "reviewer_kind": "user",
            "reviewer_id": self.reviewer_id,
            "reviewed_at": REVIEWED_AT,
        }

    def _assert_integrity_error(self, values: dict[str, object]) -> None:
        savepoint = self.connection.begin_nested()
        try:
            with self.assertRaises(IntegrityError):
                self.connection.execute(
                    source_snapshot_review_receipts.insert().values(**values)
                )
        finally:
            if savepoint.is_active:
                savepoint.rollback()

    def test_same_receipt_and_payload_is_an_idempotent_retry(self) -> None:
        receipt_id = f"{self.prefix}-idempotent"
        first = self._append(receipt_id)
        second = self._append(
            receipt_id,
            reviewed_at=REVIEWED_AT + timedelta(hours=2),
        )

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(second.receipt, first.receipt)
        self.assertEqual(second.receipt.reviewed_at, REVIEWED_AT)
        count = self.connection.execute(
            sa.select(sa.func.count())
            .select_from(source_snapshot_review_receipts)
            .where(
                source_snapshot_review_receipts.c.project_id == self.project_a,
                source_snapshot_review_receipts.c.receipt_id == receipt_id,
            )
        ).scalar_one()
        self.assertEqual(count, 1)

    def test_same_receipt_with_different_payload_conflicts(self) -> None:
        receipt_id = f"{self.prefix}-conflict"
        self._append(receipt_id)

        with self.assertRaisesRegex(
            SnapshotReviewConflict,
            "receipt already has different content",
        ) as captured:
            self._append(receipt_id, reason="Private replacement reason.")

        self.assertNotIn("Private replacement reason", str(captured.exception))

    def test_new_receipt_increments_version_and_latest_review(self) -> None:
        first = self._append(f"{self.prefix}-version-1")
        second = self._append(
            f"{self.prefix}-version-2",
            decision="needs_review",
            reason="Classification needs a human decision.",
        )
        latest = self.reviews.get_latest_review_in_transaction(
            self.connection,
            self.project_a,
            self.source_a,
            self.snapshot_a,
        )

        self.assertEqual(first.receipt.review_version, 1)
        self.assertEqual(second.receipt.review_version, 2)
        self.assertEqual(latest, second.receipt)
        by_id = self.reviews.get_by_receipt_id_in_transaction(
            self.connection,
            self.project_a,
            second.receipt.receipt_id,
        )
        self.assertEqual(by_id, second.receipt)

    def test_cross_project_and_cross_source_snapshots_are_rejected_safely(
        self,
    ) -> None:
        private_reason = "Secret review reason https://private.example/evidence"
        cases = (
            {
                "project_id": self.project_b,
                "source_id": self.source_a,
                "snapshot_id": self.snapshot_a,
            },
            {
                "project_id": self.project_a,
                "source_id": self.source_a_other,
                "snapshot_id": self.snapshot_a,
            },
        )
        for index, scope in enumerate(cases):
            with self.subTest(scope=scope):
                with self.assertRaisesRegex(
                    SnapshotReviewRepositoryError,
                    "not found in the requested project",
                ) as captured:
                    self.reviews.append_review_in_transaction(
                        self.connection,
                        **scope,
                        receipt_id=f"{self.prefix}-cross-scope-{index}",
                        decision="approve",
                        source_kind="knowledge_page",
                        trust_tier="hard_fact",
                        reason=private_reason,
                        reviewer_kind="user",
                        reviewer_id=self.reviewer_id,
                    )
                serialized = str(captured.exception)
                self.assertNotIn(private_reason, serialized)
                self.assertNotIn("private.example", serialized)
                self.assertNotIn(self.snapshot_a, serialized)

    def test_reviewer_identity_and_reason_rules_are_enforced(self) -> None:
        with self.assertRaisesRegex(ValueError, "reason is too long"):
            self._append(f"{self.prefix}-long-reason", reason="x" * 501)

        self._assert_integrity_error(
            {
                **self._receipt_values(
                    review_version=1,
                    receipt_id=f"{self.prefix}-missing-user",
                ),
                "reviewer_id": None,
            }
        )
        self._assert_integrity_error(
            {
                **self._receipt_values(
                    review_version=1,
                    receipt_id=f"{self.prefix}-legacy-user",
                ),
                "reviewer_kind": "legacy_migration",
                "reviewer_id": self.reviewer_id,
            }
        )

        legacy = self.reviews.append_review_in_transaction(
            self.connection,
            project_id=self.project_a,
            source_id=self.source_a,
            snapshot_id=self.snapshot_a,
            receipt_id=f"{self.prefix}-valid-legacy",
            decision="approve",
            source_kind="knowledge_page",
            trust_tier="reference_material",
            reason="Legacy published Snapshot cutover.",
            reviewer_kind="legacy_migration",
            reviewer_id=None,
        )
        self.assertTrue(legacy.created)
        self.assertIsNone(legacy.receipt.reviewer_id)

    def test_receipts_are_append_only(self) -> None:
        stored = self._append(f"{self.prefix}-immutable").receipt

        update_savepoint = self.connection.begin_nested()
        try:
            with self.assertRaisesRegex(DBAPIError, "append-only"):
                self.connection.execute(
                    source_snapshot_review_receipts.update()
                    .where(
                        source_snapshot_review_receipts.c.project_id
                        == stored.project_id,
                        source_snapshot_review_receipts.c.receipt_id
                        == stored.receipt_id,
                    )
                    .values(decision="reject")
                )
        finally:
            if update_savepoint.is_active:
                update_savepoint.rollback()

        delete_savepoint = self.connection.begin_nested()
        try:
            with self.assertRaisesRegex(DBAPIError, "append-only"):
                self.connection.execute(
                    source_snapshot_review_receipts.delete().where(
                        source_snapshot_review_receipts.c.project_id
                        == stored.project_id,
                        source_snapshot_review_receipts.c.receipt_id
                        == stored.receipt_id,
                    )
                )
        finally:
            if delete_savepoint.is_active:
                delete_savepoint.rollback()

        self.assertEqual(
            self.reviews.get_latest_review_in_transaction(
                self.connection,
                self.project_a,
                self.source_a,
                self.snapshot_a,
            ),
            stored,
        )

    def test_database_primary_key_receipt_unique_and_decision_checks(self) -> None:
        receipt_id = f"{self.prefix}-constraints"
        self.connection.execute(
            source_snapshot_review_receipts.insert().values(
                **self._receipt_values(
                    review_version=1,
                    receipt_id=receipt_id,
                )
            )
        )

        self._assert_integrity_error(
            self._receipt_values(
                review_version=1,
                receipt_id=f"{self.prefix}-duplicate-pk",
            )
        )
        self._assert_integrity_error(
            self._receipt_values(
                review_version=2,
                receipt_id=receipt_id,
            )
        )
        self._assert_integrity_error(
            self._receipt_values(
                review_version=2,
                receipt_id=f"{self.prefix}-invalid-decision",
                decision="publish",
            )
        )


if __name__ == "__main__":
    unittest.main()
