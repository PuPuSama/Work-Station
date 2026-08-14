from __future__ import annotations

import os
import sys
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.testclient import TestClient


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from knowledge_agent import (  # noqa: E402
    EMBEDDING_DIMENSIONS,
    ChunkEmbedding,
    EmbeddingBatch,
    KnowledgeChunk,
    KnowledgeProduct,
    KnowledgeProject,
    KnowledgeSource,
    PostgresKnowledgeRepository,
    PostgresProductCatalogRepository,
    ProductSourceEvidence,
    SourceSnapshot,
    create_knowledge_engine,
)
from knowledge_agent.catalog import ProductConfirmationError  # noqa: E402
from knowledge_agent.http import router  # noqa: E402
from knowledge_agent.library import PostgresKnowledgeLibrary  # noqa: E402
from knowledge_agent.publication import (  # noqa: E402
    KnowledgePublicationError,
    KnowledgePublicationService,
)
from knowledge_agent.schema import (  # noqa: E402
    knowledge_chunks,
    knowledge_product_source_evidence,
    knowledge_products,
    knowledge_sources,
    projects,
    source_snapshot_review_receipts,
    source_snapshots,
)
from knowledge_agent.snapshot_reviews import (  # noqa: E402
    PostgresSnapshotReviewRepository,
    SnapshotReviewConflict,
)
from server_schema import (  # noqa: E402
    organizations,
    project_memberships,
    project_ownership,
    workspace_users,
)
from services.access_control import (  # noqa: E402
    ActorIdentity,
    PostgresProjectAccessRepository,
    ProjectAccessDenied,
    ProjectAccessService,
)
from services.server_auth import (  # noqa: E402
    SERVER_AUTH_COOKIE_NAME,
    ServerActorSessionCodec,
)
from services.server_knowledge_commands import (  # noqa: E402
    PostgresServerKnowledgeCommands,
    ServerKnowledgeCommandUnavailable,
)
from services.server_request_security import (  # noqa: E402
    ServerRequestSecurity,
)


DATABASE_URL_ENV = "ARTICLE_AGENT_DATABASE_URL"
MODEL_ID = "m7-deterministic-embedding"


def axis_vector(index: int = 0) -> tuple[float, ...]:
    return tuple(
        1.0 if position == index else 0.0
        for position in range(EMBEDDING_DIMENSIONS)
    )


class DeterministicEmbeddingProvider:
    model_id = MODEL_ID
    dimensions = EMBEDDING_DIMENSIONS

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def embed(self, texts: Sequence[str]) -> EmbeddingBatch:
        requested = tuple(texts)
        self.calls.append(requested)
        return EmbeddingBatch(
            vectors=tuple(axis_vector() for _ in requested),
            model=self.model_id,
        )


class RevokingEmbeddingProvider(DeterministicEmbeddingProvider):
    def __init__(
        self,
        engine: sa.Engine,
        *,
        organization_id: str,
        project_id: str,
        user_id: str,
    ) -> None:
        super().__init__()
        self._engine = engine
        self._scope = (organization_id, project_id, user_id)

    def embed(self, texts: Sequence[str]) -> EmbeddingBatch:
        batch = super().embed(texts)
        organization_id, project_id, user_id = self._scope
        with self._engine.begin() as connection:
            connection.execute(
                project_memberships.delete().where(
                    project_memberships.c.organization_id
                    == organization_id,
                    project_memberships.c.project_id == project_id,
                    project_memberships.c.user_id == user_id,
                )
            )
        return batch


class DriftingReceiptEmbeddingProvider(DeterministicEmbeddingProvider):
    def __init__(
        self,
        *,
        commands: PostgresServerKnowledgeCommands,
        actor: ActorIdentity,
        project_id: str,
        source_id: str,
        snapshot_id: str,
    ) -> None:
        super().__init__()
        self._commands = commands
        self._actor = actor
        self._project_id = project_id
        self._source_id = source_id
        self._snapshot_id = snapshot_id
        self._drifted = False

    def embed(self, texts: Sequence[str]) -> EmbeddingBatch:
        batch = super().embed(texts)
        if not self._drifted:
            self._drifted = True
            self._commands.review_snapshot(
                actor=self._actor,
                project_id=self._project_id,
                source_id=self._source_id,
                snapshot_id=self._snapshot_id,
                receipt_id="publish-review-drift",
                source_kind="official_blog",
                trust_tier="reference_material",
                decision="needs_review",
                reason="Classification changed while embeddings were prepared.",
            )
        return batch


class RecordingAuditWriter:
    def __init__(self) -> None:
        self.events: list[object] = []

    def append(self, connection, event) -> None:
        if not connection.in_transaction():
            raise AssertionError("audit must share the business transaction")
        self.events.append(event)


class FailingAuditWriter:
    def append(self, connection, event) -> None:
        del connection, event
        raise RuntimeError(
            "private audit failure containing https://secret.example"
        )


class CurrentSessionVersions:
    def is_current(self, session) -> bool:
        del session
        return True


@unittest.skipUnless(
    os.environ.get(DATABASE_URL_ENV),
    f"{DATABASE_URL_ENV} is required for PostgreSQL integration tests",
)
class ServerKnowledgeCommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = create_knowledge_engine(
            os.environ[DATABASE_URL_ENV]
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    def setUp(self) -> None:
        prefix = f"m7-knowledge-{uuid.uuid4().hex}"
        self.organization_id = f"{prefix}-org"
        self.project_id = f"{prefix}.example.test"
        self.other_project_id = f"other-{prefix}.example.test"
        self.editor_id = f"{prefix}-editor"
        self.viewer_id = f"{prefix}-viewer"
        self.repository = PostgresKnowledgeRepository(self.engine)
        self.catalog = PostgresProductCatalogRepository(self.engine)
        self.library = PostgresKnowledgeLibrary(self.engine)
        self.reviews = PostgresSnapshotReviewRepository(self.engine)
        self.provider = DeterministicEmbeddingProvider()
        self.publication = KnowledgePublicationService(
            repository=self.repository,
            library=self.library,
            embedding_provider=self.provider,
        )
        self.audit = RecordingAuditWriter()

        for project_id in (self.project_id, self.other_project_id):
            self.repository.upsert_project(
                KnowledgeProject(
                    project_id=project_id,
                    customer_name=project_id,
                    official_domain=project_id,
                )
            )
        with self.engine.begin() as connection:
            connection.execute(
                organizations.insert().values(
                    organization_id=self.organization_id,
                    name="Knowledge Command Test Organization",
                )
            )
            connection.execute(
                workspace_users.insert(),
                (
                    {
                        "organization_id": self.organization_id,
                        "user_id": self.editor_id,
                        "display_name": "Knowledge Editor",
                    },
                    {
                        "organization_id": self.organization_id,
                        "user_id": self.viewer_id,
                        "display_name": "Knowledge Viewer",
                    },
                ),
            )
            connection.execute(
                project_ownership.insert(),
                (
                    {
                        "organization_id": self.organization_id,
                        "project_id": self.project_id,
                    },
                    {
                        "organization_id": self.organization_id,
                        "project_id": self.other_project_id,
                    },
                ),
            )
            connection.execute(
                project_memberships.insert(),
                (
                    {
                        "organization_id": self.organization_id,
                        "project_id": self.project_id,
                        "user_id": self.editor_id,
                        "role": "editor",
                        "granted_by_user_id": self.editor_id,
                    },
                    {
                        "organization_id": self.organization_id,
                        "project_id": self.project_id,
                        "user_id": self.viewer_id,
                        "role": "viewer",
                        "granted_by_user_id": self.editor_id,
                    },
                ),
            )

        self.editor = ActorIdentity(
            self.organization_id,
            self.editor_id,
        )
        self.viewer = ActorIdentity(
            self.organization_id,
            self.viewer_id,
        )
        self.service = self._service(audit=self.audit)
        self._store_source(self.project_id, "review-source")
        self._store_source(self.other_project_id, "other-source")
        self._store_publication_source()
        self._store_products()

    def tearDown(self) -> None:
        project_ids = (self.project_id, self.other_project_id)
        with self.engine.begin() as connection:
            connection.execute(
                knowledge_product_source_evidence.delete().where(
                    knowledge_product_source_evidence.c.project_id.in_(
                        project_ids
                    )
                )
            )
            connection.execute(
                knowledge_products.delete().where(
                    knowledge_products.c.project_id.in_(project_ids)
                )
            )
            connection.execute(
                knowledge_chunks.delete().where(
                    knowledge_chunks.c.project_id.in_(project_ids)
                )
            )
            try:
                connection.execute(
                    sa.text(
                        "ALTER TABLE source_snapshot_review_receipts "
                        "DISABLE TRIGGER "
                        "trg_snapshot_review_receipts_append_only"
                    )
                )
                connection.execute(
                    source_snapshot_review_receipts.delete().where(
                        source_snapshot_review_receipts.c.project_id.in_(
                            project_ids
                        )
                    )
                )
            finally:
                # PostgreSQL DDL is transactional. If deletion fails and the
                # transaction becomes aborted, the rollback restores the
                # trigger even when this ENABLE statement cannot execute.
                connection.execute(
                    sa.text(
                        "ALTER TABLE source_snapshot_review_receipts "
                        "ENABLE TRIGGER "
                        "trg_snapshot_review_receipts_append_only"
                    )
                )
            connection.execute(
                source_snapshots.delete().where(
                    source_snapshots.c.project_id.in_(project_ids)
                )
            )
            connection.execute(
                knowledge_sources.delete().where(
                    knowledge_sources.c.project_id.in_(project_ids)
                )
            )
            connection.execute(
                project_memberships.delete().where(
                    project_memberships.c.organization_id
                    == self.organization_id
                )
            )
            connection.execute(
                project_ownership.delete().where(
                    project_ownership.c.organization_id
                    == self.organization_id
                )
            )
            connection.execute(
                workspace_users.delete().where(
                    workspace_users.c.organization_id
                    == self.organization_id
                )
            )
            connection.execute(
                organizations.delete().where(
                    organizations.c.organization_id
                    == self.organization_id
                )
            )
            connection.execute(
                projects.delete().where(
                    projects.c.project_id.in_(project_ids)
                )
            )

    def _service(
        self,
        *,
        audit,
        publication: KnowledgePublicationService | None = None,
    ) -> PostgresServerKnowledgeCommands:
        return PostgresServerKnowledgeCommands(
            self.engine,
            repository=self.repository,
            catalog=self.catalog,
            publication=publication or self.publication,
            audit=audit,
        )

    def _store_source(self, project_id: str, source_id: str) -> None:
        self.repository.upsert_source(
            KnowledgeSource(
                project_id=project_id,
                source_id=source_id,
                display_name="Private source title",
                source_kind="private_file",
                trust_tier="reference_material",
                canonical_url=f"https://{project_id}/{source_id}",
                public_source=True,
            )
        )
        self.repository.store_snapshot(
            project_id,
            SourceSnapshot(
                project_id=project_id,
                source_id=source_id,
                snapshot_id=f"{source_id}-snapshot",
                content_hash="a" * 64,
                fetched_at=datetime(2026, 7, 31, tzinfo=timezone.utc),
                parser_name="test",
                parser_version="1",
            ),
            (
                KnowledgeChunk(
                    project_id=project_id,
                    chunk_id=f"{source_id}-snapshot:0000",
                    source_id=source_id,
                    snapshot_id=f"{source_id}-snapshot",
                    text="Private source body that must not enter audit.",
                ),
            ),
        )
        with self.engine.begin() as connection:
            self.repository.set_pending_snapshot_in_transaction(
                connection,
                project_id,
                source_id,
                f"{source_id}-snapshot",
            )

    def _store_publication_source(self) -> None:
        source_id = "publish-source"
        old_snapshot_id = "publish-old"
        new_snapshot_id = "publish-new"
        self.repository.upsert_source(
            KnowledgeSource(
                project_id=self.project_id,
                source_id=source_id,
                display_name="Publish source",
                source_kind="knowledge_page",
                trust_tier="hard_fact",
                canonical_url=f"https://{self.project_id}/publish",
                public_source=True,
                metadata={
                    "review": {
                        "decision": "approve",
                        "reason": "Private approval reason.",
                    }
                },
            )
        )
        for index, snapshot_id in enumerate(
            (old_snapshot_id, new_snapshot_id)
        ):
            self.repository.store_snapshot(
                self.project_id,
                SourceSnapshot(
                    project_id=self.project_id,
                    source_id=source_id,
                    snapshot_id=snapshot_id,
                    content_hash=f"{index + 1}" * 64,
                    fetched_at=(
                        datetime(2026, 7, 31, tzinfo=timezone.utc)
                        + timedelta(minutes=index)
                    ),
                    parser_name="test",
                    parser_version="1",
                    metadata=(
                        {
                            "source_projection": {
                                "schema_version": 1,
                                "display_name": "Projected publish source",
                                "public_source": True,
                                "canonical_url": (
                                    f"https://{self.project_id}/projected"
                                ),
                                "metadata": {
                                    "projection_marker": "new-snapshot"
                                },
                            }
                        }
                        if snapshot_id == new_snapshot_id
                        else {}
                    ),
                ),
                (
                    KnowledgeChunk(
                        project_id=self.project_id,
                        chunk_id=f"{snapshot_id}:0000",
                        source_id=source_id,
                        snapshot_id=snapshot_id,
                        text=f"Private snapshot {index} body.",
                    ),
                ),
            )
            with self.engine.begin() as connection:
                self.repository.set_pending_snapshot_in_transaction(
                    connection,
                    self.project_id,
                    source_id,
                    snapshot_id,
                )
                if snapshot_id == old_snapshot_id:
                    self.reviews.append_review_in_transaction(
                        connection,
                        project_id=self.project_id,
                        source_id=source_id,
                        snapshot_id=old_snapshot_id,
                        receipt_id="publish-old-approved",
                        decision="approve",
                        source_kind="knowledge_page",
                        trust_tier="hard_fact",
                        reason="Current Snapshot was approved before refresh.",
                        reviewer_kind="legacy_migration",
                        reviewer_id=None,
                    )
            if snapshot_id == old_snapshot_id:
                self.repository.store_embeddings(
                    self.project_id,
                    (
                        ChunkEmbedding(
                            project_id=self.project_id,
                            chunk_id=f"{old_snapshot_id}:0000",
                            snapshot_id=old_snapshot_id,
                            embedding_model=MODEL_ID,
                            vector=axis_vector(),
                        ),
                    ),
                )
                self.repository.activate_snapshot(
                    self.project_id,
                    source_id,
                    old_snapshot_id,
                    MODEL_ID,
                )

    def _store_products(self) -> None:
        for product_id in ("pending-product", "current-product"):
            self.catalog.upsert_product(
                KnowledgeProduct(
                    project_id=self.project_id,
                    product_id=product_id,
                    name=f"Private {product_id}",
                    canonical_url=f"https://{self.project_id}/{product_id}",
                )
            )
        self.catalog.store_source_evidence(
            ProductSourceEvidence(
                project_id=self.project_id,
                product_id="pending-product",
                source_id="review-source",
                snapshot_id="review-source-snapshot",
                relation="primary_detail",
                confidence=0.99,
                reason="Pending evidence must not authorize confirmation.",
            )
        )
        for snapshot_id in ("publish-old", "publish-new"):
            self.catalog.store_source_evidence(
                ProductSourceEvidence(
                    project_id=self.project_id,
                    product_id="current-product",
                    source_id="publish-source",
                    snapshot_id=snapshot_id,
                    relation="primary_detail",
                    confidence=0.99,
                    reason="Only the current published version is authoritative.",
                )
            )

    def _review(
        self,
        *,
        source_id: str = "publish-source",
        snapshot_id: str = "publish-new",
        receipt_id: str = "publish-new-approved",
        source_kind: str = "product_detail",
        trust_tier: str = "reference_material",
        decision: str = "approve",
        reason: str = "Reviewed exact Snapshot.",
    ):
        return self.service.review_snapshot(
            actor=self.editor,
            project_id=self.project_id,
            source_id=source_id,
            snapshot_id=snapshot_id,
            receipt_id=receipt_id,
            source_kind=source_kind,
            trust_tier=trust_tier,
            decision=decision,
            reason=reason,
        )

    def test_review_receipt_is_idempotent_conflict_safe_and_versioned(
        self,
    ) -> None:
        first = self._review(
            source_id="review-source",
            snapshot_id="review-source-snapshot",
            receipt_id="review-receipt-1",
            source_kind="knowledge_page",
            trust_tier="hard_fact",
            reason="Secret reason https://private.example/source",
        )
        retry = self._review(
            source_id="review-source",
            snapshot_id="review-source-snapshot",
            receipt_id="review-receipt-1",
            source_kind="knowledge_page",
            trust_tier="hard_fact",
            reason="Secret reason https://private.example/source",
        )
        self.assertEqual(first, retry)
        self.assertEqual(first.review_version, 1)
        self.assertEqual(len(self.audit.events), 1)

        with self.assertRaisesRegex(
            SnapshotReviewConflict,
            "different content",
        ):
            self._review(
                source_id="review-source",
                snapshot_id="review-source-snapshot",
                receipt_id="review-receipt-1",
                source_kind="knowledge_page",
                trust_tier="hard_fact",
                reason="A reused identity cannot change its payload.",
            )

        second = self._review(
            source_id="review-source",
            snapshot_id="review-source-snapshot",
            receipt_id="review-receipt-2",
            source_kind="official_blog",
            trust_tier="reference_material",
            decision="needs_review",
            reason="A later classification needs another review.",
        )
        self.assertEqual(second.review_version, 2)
        self.assertEqual(len(self.audit.events), 2)
        event = self.audit.events[0]
        self.assertEqual(event.action, "knowledge.snapshot.reviewed")
        self.assertEqual(event.target_type, "source_snapshot")
        self.assertEqual(event.target_id, "review-source-snapshot")
        self.assertEqual(
            event.details,
            {
                "source_id": "review-source",
                "decision": "approve",
                "source_kind": "knowledge_page",
                "trust_tier": "hard_fact",
                "review_version": 1,
                "receipt_id": "review-receipt-1",
            },
        )
        serialized = str(event)
        self.assertNotIn("Secret reason", serialized)
        self.assertNotIn("private.example", serialized)
        self.assertNotIn("Private source body", serialized)

    def test_review_reauthorizes_and_rejects_invalid_requests(self) -> None:
        with self.assertRaises(ProjectAccessDenied):
            self.service.review_snapshot(
                actor=self.viewer,
                project_id=self.project_id,
                source_id="review-source",
                snapshot_id="review-source-snapshot",
                receipt_id="viewer-review",
                source_kind="private_file",
                trust_tier="reference_material",
                decision="reject",
                reason="Viewer may not review.",
            )
        with self.assertRaises(ProjectAccessDenied):
            self.service.review_snapshot(
                actor=self.editor,
                project_id=self.other_project_id,
                source_id="other-source",
                snapshot_id="other-source-snapshot",
                receipt_id="cross-project-review",
                source_kind="private_file",
                trust_tier="reference_material",
                decision="reject",
                reason="Cross-project attempt.",
            )
        with self.assertRaisesRegex(ValueError, "reason is too long"):
            self.service.review_snapshot(
                actor=self.editor,
                project_id=self.project_id,
                source_id="review-source",
                snapshot_id="review-source-snapshot",
                receipt_id="long-reason-review",
                source_kind="private_file",
                trust_tier="reference_material",
                decision="approve",
                reason="x" * 501,
            )
        self.assertEqual(self.audit.events, [])

    def test_legacy_source_metadata_does_not_authorize_snapshot(self) -> None:
        self._store_source(self.project_id, "metadata-only-source")
        with self.engine.begin() as connection:
            connection.execute(
                knowledge_sources.update()
                .where(
                    knowledge_sources.c.project_id == self.project_id,
                    knowledge_sources.c.source_id == "metadata-only-source",
                )
                .values(
                    metadata={
                        "review": {
                            "decision": "approve",
                            "reason": "Legacy source-scoped decision.",
                        }
                    }
                )
            )

        with self.assertRaisesRegex(
            KnowledgePublicationError,
            "snapshot classification must be approved",
        ):
            self.service.publish_source(
                actor=self.editor,
                project_id=self.project_id,
                source_id="metadata-only-source",
                snapshot_id="metadata-only-source-snapshot",
            )
        self.assertIsNone(
            self.reviews.get_latest_review(
                self.project_id,
                "metadata-only-source",
                "metadata-only-source-snapshot",
            )
        )

    def test_current_snapshot_receipt_does_not_authorize_pending_snapshot(
        self,
    ) -> None:
        current_receipt = self.reviews.get_latest_review(
            self.project_id,
            "publish-source",
            "publish-old",
        )
        self.assertIsNotNone(current_receipt)
        self.assertEqual(current_receipt.decision, "approve")
        self.assertIsNone(
            self.reviews.get_latest_review(
                self.project_id,
                "publish-source",
                "publish-new",
            )
        )
        with self.assertRaisesRegex(
            KnowledgePublicationError,
            "snapshot classification must be approved",
        ):
            self.service.publish_source(
                actor=self.editor,
                project_id=self.project_id,
                source_id="publish-source",
                snapshot_id="publish-new",
            )
        source = self.library.get_source(self.project_id, "publish-source")
        self.assertEqual(source.current_snapshot_id, "publish-old")
        self.assertEqual(source.pending_snapshot_id, "publish-new")

    def test_published_source_keeps_current_during_pending_review_and_reject(
        self,
    ) -> None:
        self._review(
            receipt_id="publish-new-needs-review",
            source_kind="official_blog",
            decision="needs_review",
        )
        source = self.library.get_source(self.project_id, "publish-source")
        self.assertEqual(source.status, "published")
        self.assertEqual(source.current_snapshot_id, "publish-old")
        self.assertEqual(source.pending_snapshot_id, "publish-new")
        self.assertEqual(source.source_kind, "knowledge_page")
        self.assertEqual(source.trust_tier, "hard_fact")

        self._review(
            receipt_id="publish-new-rejected",
            source_kind="official_blog",
            decision="reject",
            reason="The pending Snapshot is not authoritative.",
        )
        source = self.library.get_source(self.project_id, "publish-source")
        self.assertEqual(source.status, "published")
        self.assertEqual(source.current_snapshot_id, "publish-old")
        self.assertIsNone(source.pending_snapshot_id)
        self.assertEqual(source.display_name, "Publish source")
        self.assertEqual(source.source_kind, "knowledge_page")
        self.assertEqual(source.trust_tier, "hard_fact")

    def test_approved_snapshot_publish_switches_pointer_and_projection(
        self,
    ) -> None:
        receipt = self._review(receipt_id="publish-new-approved")
        result = self.service.publish_source(
            actor=self.editor,
            project_id=self.project_id,
            source_id="publish-source",
            snapshot_id="publish-new",
        )
        self.assertEqual(result.snapshot_id, "publish-new")
        source = self.library.get_source(self.project_id, "publish-source")
        self.assertEqual(source.status, "published")
        self.assertEqual(source.current_snapshot_id, "publish-new")
        self.assertIsNone(source.pending_snapshot_id)
        self.assertEqual(source.display_name, "Projected publish source")
        self.assertEqual(source.source_kind, "product_detail")
        self.assertEqual(source.trust_tier, "reference_material")
        self.assertEqual(
            source.canonical_url,
            f"https://{self.project_id}/projected",
        )
        self.assertEqual(source.metadata, {"projection_marker": "new-snapshot"})
        self.assertEqual(
            [event.action for event in self.audit.events],
            ["knowledge.snapshot.reviewed", "knowledge.snapshot.published"],
        )
        self.assertEqual(
            self.audit.events[1].details,
            {
                "source_id": "publish-source",
                "snapshot_id": "publish-new",
                "previous_snapshot_id": "publish-old",
                "review_version": receipt.review_version,
                "receipt_id": receipt.receipt_id,
                "chunk_count": 1,
                "embedding_model": MODEL_ID,
            },
        )

    def test_audit_failure_rolls_back_receipt_pointer_projection_and_product(
        self,
    ) -> None:
        failing = self._service(audit=FailingAuditWriter())
        with self.assertRaises(
            ServerKnowledgeCommandUnavailable
        ) as captured:
            failing.review_snapshot(
                actor=self.editor,
                project_id=self.project_id,
                source_id="review-source",
                snapshot_id="review-source-snapshot",
                receipt_id="review-must-roll-back",
                source_kind="knowledge_page",
                trust_tier="hard_fact",
                decision="reject",
                reason="Must roll back Receipt and aggregate changes.",
            )
        self.assertNotIn("secret.example", str(captured.exception))
        self.assertIsNone(
            self.reviews.get_latest_review(
                self.project_id,
                "review-source",
                "review-source-snapshot",
            )
        )
        source = self.library.get_source(self.project_id, "review-source")
        self.assertEqual(source.status, "inbox")
        self.assertEqual(source.pending_snapshot_id, "review-source-snapshot")
        self.assertEqual(source.display_name, "Private source title")
        self.assertEqual(source.source_kind, "private_file")
        self.assertEqual(source.trust_tier, "reference_material")

        with self.assertRaises(ServerKnowledgeCommandUnavailable):
            failing.confirm_product(
                actor=self.editor,
                project_id=self.project_id,
                product_id="current-product",
            )
        product = self.catalog.get_product(self.project_id, "current-product")
        self.assertEqual(product.status, "inbox")

    def test_publish_audit_failure_retains_old_serving_snapshot(self) -> None:
        self._review(receipt_id="publish-audit-failure-approved")
        self.audit.events.clear()
        failing = self._service(audit=FailingAuditWriter())
        with self.assertRaises(
            ServerKnowledgeCommandUnavailable
        ) as captured:
            failing.publish_source(
                actor=self.editor,
                project_id=self.project_id,
                source_id="publish-source",
                snapshot_id="publish-new",
            )
        self.assertNotIn("secret.example", str(captured.exception))
        source = self.library.get_source(self.project_id, "publish-source")
        self.assertEqual(source.status, "published")
        self.assertEqual(source.current_snapshot_id, "publish-old")
        self.assertEqual(source.pending_snapshot_id, "publish-new")
        self.assertEqual(source.display_name, "Publish source")
        self.assertEqual(source.source_kind, "knowledge_page")
        self.assertEqual(source.trust_tier, "hard_fact")
        self.assertIn("review", source.metadata)
        with self.engine.connect() as connection:
            prepared_model = connection.execute(
                sa.select(knowledge_chunks.c.embedding_model).where(
                    knowledge_chunks.c.project_id == self.project_id,
                    knowledge_chunks.c.chunk_id == "publish-new:0000",
                )
            ).scalar_one()
        self.assertEqual(prepared_model, MODEL_ID)

    def test_publish_rechecks_revocation_after_embedding(self) -> None:
        self._review(receipt_id="publish-revocation-approved")
        self.audit.events.clear()
        revoking_provider = RevokingEmbeddingProvider(
            self.engine,
            organization_id=self.organization_id,
            project_id=self.project_id,
            user_id=self.editor_id,
        )
        publication = KnowledgePublicationService(
            repository=self.repository,
            library=self.library,
            embedding_provider=revoking_provider,
        )
        service = self._service(audit=self.audit, publication=publication)
        with self.assertRaises(ProjectAccessDenied):
            service.publish_source(
                actor=self.editor,
                project_id=self.project_id,
                source_id="publish-source",
                snapshot_id="publish-new",
            )
        source = self.library.get_source(self.project_id, "publish-source")
        self.assertEqual(source.current_snapshot_id, "publish-old")
        self.assertEqual(source.pending_snapshot_id, "publish-new")
        self.assertEqual(self.audit.events, [])

    def test_publish_rejects_receipt_drift_after_embedding(self) -> None:
        self._review(receipt_id="publish-initial-approval")
        self.audit.events.clear()
        drifting_provider = DriftingReceiptEmbeddingProvider(
            commands=self.service,
            actor=self.editor,
            project_id=self.project_id,
            source_id="publish-source",
            snapshot_id="publish-new",
        )
        publication = KnowledgePublicationService(
            repository=self.repository,
            library=self.library,
            embedding_provider=drifting_provider,
        )
        service = self._service(audit=self.audit, publication=publication)
        with self.assertRaisesRegex(
            KnowledgePublicationError,
            "snapshot review changed during publication",
        ):
            service.publish_source(
                actor=self.editor,
                project_id=self.project_id,
                source_id="publish-source",
                snapshot_id="publish-new",
            )
        source = self.library.get_source(self.project_id, "publish-source")
        self.assertEqual(source.status, "published")
        self.assertEqual(source.current_snapshot_id, "publish-old")
        self.assertEqual(source.pending_snapshot_id, "publish-new")
        latest = self.reviews.get_latest_review(
            self.project_id,
            "publish-source",
            "publish-new",
        )
        self.assertEqual(latest.receipt_id, "publish-review-drift")
        self.assertEqual(latest.review_version, 2)
        self.assertEqual(latest.decision, "needs_review")
        self.assertEqual(
            [event.action for event in self.audit.events],
            ["knowledge.snapshot.reviewed"],
        )

    def test_server_confirm_requires_current_published_primary_detail(
        self,
    ) -> None:
        with self.assertRaisesRegex(
            ProductConfirmationError,
            "current published primary detail",
        ):
            self.service.confirm_product(
                actor=self.editor,
                project_id=self.project_id,
                product_id="pending-product",
            )
        pending = self.catalog.get_product(self.project_id, "pending-product")
        self.assertEqual(pending.status, "inbox")

        self.assertTrue(
            self.service.confirm_product(
                actor=self.editor,
                project_id=self.project_id,
                product_id="current-product",
            )
        )
        self.assertFalse(
            self.service.confirm_product(
                actor=self.editor,
                project_id=self.project_id,
                product_id="current-product",
            )
        )
        self.assertEqual(
            [event.action for event in self.audit.events],
            ["knowledge.product.confirmed"],
        )

    def test_editor_can_correct_confirmed_product_specifications(self) -> None:
        self.service.confirm_product(
            actor=self.editor,
            project_id=self.project_id,
            product_id="current-product",
        )
        self.audit.events.clear()

        product = self.service.update_product_specifications(
            actor=self.editor,
            project_id=self.project_id,
            product_id="current-product",
            specification_tables=[
                {
                    "headers": ["Parameter", "6000W", "8000W"],
                    "rows": [["Surge Power", "12000VA", "16000VA"]],
                }
            ],
        )

        self.assertEqual(
            product.metadata["manual_specification_tables"][0]["rows"][0],
            ["Surge Power", "12000VA", "16000VA"],
        )
        self.assertEqual(
            [event.action for event in self.audit.events],
            ["knowledge.product.specifications.updated"],
        )
        self.assertEqual(
            self.audit.events[0].details,
            {"table_count": 1, "row_count": 1},
        )
        self.service.update_product_specifications(
            actor=self.editor,
            project_id=self.project_id,
            product_id="current-product",
            specification_tables=[
                {
                    "headers": ["Parameter", "6000W", "8000W"],
                    "rows": [["Surge Power", "12000VA", "16000VA"]],
                }
            ],
        )
        self.assertEqual(len(self.audit.events), 1)

        with self.assertRaises(ProjectAccessDenied):
            self.service.update_product_specifications(
                actor=self.viewer,
                project_id=self.project_id,
                product_id="current-product",
                specification_tables=[],
            )

    def test_publish_and_confirm_retries_do_not_duplicate_audit(self) -> None:
        receipt = self._review(receipt_id="publish-retry-approved")
        self.audit.events.clear()
        first = self.service.publish_source(
            actor=self.editor,
            project_id=self.project_id,
            source_id="publish-source",
            snapshot_id="publish-new",
        )
        second = self.service.publish_source(
            actor=self.editor,
            project_id=self.project_id,
            source_id="publish-source",
            snapshot_id="publish-new",
        )
        self.assertEqual(first, second)
        self.assertEqual(len(self.provider.calls), 1)
        self.assertTrue(
            self.service.confirm_product(
                actor=self.editor,
                project_id=self.project_id,
                product_id="current-product",
            )
        )
        self.assertFalse(
            self.service.confirm_product(
                actor=self.editor,
                project_id=self.project_id,
                product_id="current-product",
            )
        )
        self.assertEqual(
            [event.action for event in self.audit.events],
            ["knowledge.snapshot.published", "knowledge.product.confirmed"],
        )
        self.assertEqual(
            self.audit.events[0].details,
            {
                "source_id": "publish-source",
                "snapshot_id": "publish-new",
                "previous_snapshot_id": "publish-old",
                "review_version": receipt.review_version,
                "receipt_id": receipt.receipt_id,
                "chunk_count": 1,
                "embedding_model": MODEL_ID,
            },
        )
        self.assertNotIn("Private", str(self.audit.events))
        self.assertNotIn("https://", str(self.audit.events))

    def test_server_http_uses_atomic_command_service(self) -> None:
        codec = ServerActorSessionCodec(b"k" * 32)
        app = FastAPI()
        app.state.server_mode_enabled = True
        app.state.knowledge_agent_runtime = type(
            "Runtime",
            (),
            {
                "engine": self.engine,
                "repository": self.repository,
                "catalog_repository": self.catalog,
                "library": self.library,
                "publication": self.publication,
            },
        )()
        app.state.server_knowledge_commands = self.service
        app.state.server_request_security = ServerRequestSecurity(
            codec=codec,
            access=ProjectAccessService(
                PostgresProjectAccessRepository(self.engine)
            ),
            sessions=CurrentSessionVersions(),
        )
        app.include_router(router)
        with TestClient(app) as client:
            client.cookies.set(
                SERVER_AUTH_COOKIE_NAME,
                codec.create(self.editor),
            )
            response = client.put(
                (
                    f"/api/knowledge/{self.project_id}/sources/"
                    "review-source/snapshots/"
                    "review-source-snapshot/review"
                ),
                json={
                    "receipt_id": "http-review-receipt",
                    "source_kind": "knowledge_page",
                    "trust_tier": "hard_fact",
                    "decision": "approve",
                    "reason": "HTTP private review reason.",
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["snapshot_id"], "review-source-snapshot")
        self.assertEqual(response.json()["receipt_id"], "http-review-receipt")
        self.assertEqual(response.json()["review_version"], 1)
        self.assertEqual(
            [event.action for event in self.audit.events],
            ["knowledge.snapshot.reviewed"],
        )


if __name__ == "__main__":
    unittest.main()
