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
from knowledge_agent.http import router  # noqa: E402
from knowledge_agent.library import PostgresKnowledgeLibrary  # noqa: E402
from knowledge_agent.publication import (  # noqa: E402
    KnowledgePublicationService,
)
from knowledge_agent.schema import (  # noqa: E402
    knowledge_chunks,
    knowledge_product_source_evidence,
    knowledge_products,
    knowledge_sources,
    projects,
    source_snapshots,
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


class DriftingSnapshotEmbeddingProvider(DeterministicEmbeddingProvider):
    def __init__(
        self,
        repository: PostgresKnowledgeRepository,
        *,
        project_id: str,
        source_id: str,
    ) -> None:
        super().__init__()
        self._repository = repository
        self._project_id = project_id
        self._source_id = source_id
        self._drifted = False

    def embed(self, texts: Sequence[str]) -> EmbeddingBatch:
        batch = super().embed(texts)
        if not self._drifted:
            self._drifted = True
            self._repository.store_snapshot(
                self._project_id,
                SourceSnapshot(
                    project_id=self._project_id,
                    source_id=self._source_id,
                    snapshot_id="publish-drifted",
                    content_hash="d" * 64,
                    fetched_at=datetime(
                        2026,
                        8,
                        1,
                        tzinfo=timezone.utc,
                    ),
                    parser_name="test",
                    parser_version="1",
                ),
                (
                    KnowledgeChunk(
                        project_id=self._project_id,
                        chunk_id="publish-drifted:0000",
                        source_id=self._source_id,
                        snapshot_id="publish-drifted",
                        text="Newer private snapshot.",
                    ),
                ),
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

        self._store_source(self.project_id, "review-source")
        self._store_source(self.other_project_id, "other-source")
        self._store_publication_source()
        self._store_product()
        self.editor = ActorIdentity(
            self.organization_id,
            self.editor_id,
        )
        self.viewer = ActorIdentity(
            self.organization_id,
            self.viewer_id,
        )
        self.service = self._service(audit=self.audit)

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

    def _store_product(self) -> None:
        self.catalog.upsert_product(
            KnowledgeProduct(
                project_id=self.project_id,
                product_id="confirmed-product",
                name="Private Product Name",
                canonical_url=(
                    f"https://{self.project_id}/confirmed-product"
                ),
            )
        )
        self.catalog.store_source_evidence(
            ProductSourceEvidence(
                project_id=self.project_id,
                product_id="confirmed-product",
                source_id="review-source",
                snapshot_id="review-source-snapshot",
                relation="primary_detail",
                confidence=0.99,
                reason="Private evidence reason.",
            )
        )

    def test_review_reauthorizes_and_redacts_audit_details(self) -> None:
        reviewed = self.service.review_source(
            actor=self.editor,
            project_id=self.project_id,
            source_id="review-source",
            source_kind="knowledge_page",
            trust_tier="hard_fact",
            decision="approve",
            reason="Secret reason https://private.example/source",
        )

        self.assertEqual(reviewed.status, "inbox")
        self.assertEqual(reviewed.metadata["review"]["decision"], "approve")
        self.assertEqual(len(self.audit.events), 1)
        event = self.audit.events[0]
        self.assertEqual(event.action, "knowledge.source.reviewed")
        self.assertEqual(
            event.details,
            {
                "decision": "approve",
                "source_kind": "knowledge_page",
                "trust_tier": "hard_fact",
            },
        )
        serialized = str(event)
        self.assertNotIn("Secret reason", serialized)
        self.assertNotIn("private.example", serialized)
        self.assertNotIn("Private source body", serialized)

        with self.assertRaises(ProjectAccessDenied):
            self.service.review_source(
                actor=self.viewer,
                project_id=self.project_id,
                source_id="review-source",
                source_kind="private_file",
                trust_tier="reference_material",
                decision="reject",
                reason="Viewer may not review.",
            )
        with self.assertRaises(ProjectAccessDenied):
            self.service.review_source(
                actor=self.editor,
                project_id=self.other_project_id,
                source_id="other-source",
                source_kind="private_file",
                trust_tier="reference_material",
                decision="reject",
                reason="Cross-project attempt.",
            )
        with self.assertRaisesRegex(ValueError, "reason is too long"):
            self.service.review_source(
                actor=self.editor,
                project_id=self.project_id,
                source_id="review-source",
                source_kind="private_file",
                trust_tier="reference_material",
                decision="approve",
                reason="x" * 501,
            )
        self.assertEqual(len(self.audit.events), 1)

    def test_audit_failure_rolls_back_review_and_product_confirmation(
        self,
    ) -> None:
        failing = self._service(audit=FailingAuditWriter())
        with self.assertRaises(
            ServerKnowledgeCommandUnavailable
        ) as captured:
            failing.review_source(
                actor=self.editor,
                project_id=self.project_id,
                source_id="review-source",
                source_kind="knowledge_page",
                trust_tier="hard_fact",
                decision="approve",
                reason="Must roll back.",
            )
        self.assertNotIn("secret.example", str(captured.exception))
        source = self.library.get_source(
            self.project_id,
            "review-source",
        )
        self.assertEqual(source.status, "inbox")
        self.assertNotIn("review", source.metadata)

        with self.assertRaises(ServerKnowledgeCommandUnavailable):
            failing.confirm_product(
                actor=self.editor,
                project_id=self.project_id,
                product_id="confirmed-product",
            )
        product = self.catalog.get_product(
            self.project_id,
            "confirmed-product",
        )
        self.assertEqual(product.status, "inbox")

    def test_publish_audit_failure_retains_old_serving_snapshot(self) -> None:
        failing = self._service(audit=FailingAuditWriter())
        with self.assertRaises(
            ServerKnowledgeCommandUnavailable
        ) as captured:
            failing.publish_source(
                actor=self.editor,
                project_id=self.project_id,
                source_id="publish-source",
            )
        self.assertNotIn("secret.example", str(captured.exception))
        source = self.library.get_source(
            self.project_id,
            "publish-source",
        )
        self.assertEqual(source.status, "published")
        self.assertEqual(source.current_snapshot_id, "publish-old")
        with self.engine.connect() as connection:
            prepared_model = connection.execute(
                sa.select(knowledge_chunks.c.embedding_model).where(
                    knowledge_chunks.c.project_id == self.project_id,
                    knowledge_chunks.c.chunk_id == "publish-new:0000",
                )
            ).scalar_one()
        self.assertEqual(prepared_model, MODEL_ID)

    def test_publish_rechecks_revocation_after_embedding(self) -> None:
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
        service = self._service(
            audit=self.audit,
            publication=publication,
        )
        with self.assertRaises(ProjectAccessDenied):
            service.publish_source(
                actor=self.editor,
                project_id=self.project_id,
                source_id="publish-source",
            )
        source = self.library.get_source(
            self.project_id,
            "publish-source",
        )
        self.assertEqual(source.current_snapshot_id, "publish-old")
        self.assertEqual(self.audit.events, [])

    def test_publish_rejects_latest_snapshot_drift(self) -> None:
        drifting_provider = DriftingSnapshotEmbeddingProvider(
            self.repository,
            project_id=self.project_id,
            source_id="publish-source",
        )
        publication = KnowledgePublicationService(
            repository=self.repository,
            library=self.library,
            embedding_provider=drifting_provider,
        )
        service = self._service(
            audit=self.audit,
            publication=publication,
        )
        with self.assertRaisesRegex(
            RuntimeError,
            "source snapshot changed during publication",
        ):
            service.publish_source(
                actor=self.editor,
                project_id=self.project_id,
                source_id="publish-source",
            )
        source = self.library.get_source(
            self.project_id,
            "publish-source",
        )
        self.assertEqual(source.current_snapshot_id, "publish-old")
        self.assertEqual(self.audit.events, [])

    def test_publish_and_confirm_retries_do_not_duplicate_audit(self) -> None:
        first = self.service.publish_source(
            actor=self.editor,
            project_id=self.project_id,
            source_id="publish-source",
        )
        second = self.service.publish_source(
            actor=self.editor,
            project_id=self.project_id,
            source_id="publish-source",
        )
        self.assertEqual(first, second)
        self.assertTrue(
            self.service.confirm_product(
                actor=self.editor,
                project_id=self.project_id,
                product_id="confirmed-product",
            )
        )
        self.assertFalse(
            self.service.confirm_product(
                actor=self.editor,
                project_id=self.project_id,
                product_id="confirmed-product",
            )
        )
        actions = [event.action for event in self.audit.events]
        self.assertEqual(
            actions,
            [
                "knowledge.source.published",
                "knowledge.product.confirmed",
            ],
        )
        self.assertEqual(
            self.audit.events[0].details,
            {
                "snapshot_id": "publish-new",
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
                    "review-source/review"
                ),
                json={
                    "source_kind": "knowledge_page",
                    "trust_tier": "hard_fact",
                    "decision": "approve",
                    "reason": "HTTP private review reason.",
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            [event.action for event in self.audit.events],
            ["knowledge.source.reviewed"],
        )


if __name__ == "__main__":
    unittest.main()
