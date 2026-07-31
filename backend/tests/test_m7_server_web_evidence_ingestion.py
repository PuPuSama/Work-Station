from __future__ import annotations

import hashlib
import os
import sys
import unittest
import uuid
from dataclasses import replace
from datetime import timedelta
from io import BytesIO
from pathlib import Path

import sqlalchemy as sa
from PIL import Image


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from knowledge_agent import (  # noqa: E402
    FetchedResource,
    KnowledgeProject,
    OfficialSiteFetchError,
    OfficialWebPageIngestionService,
    PostgresKnowledgeAssetRepository,
    PostgresKnowledgeLibrary,
    PostgresKnowledgeRepository,
    PostgresProductCatalogRepository,
    create_knowledge_engine,
)
from knowledge_agent.object_storage import ScopedS3ArtifactStore  # noqa: E402
from knowledge_agent.schema import (  # noqa: E402
    knowledge_assets,
    knowledge_chunks,
    knowledge_product_asset_evidence,
    knowledge_product_source_evidence,
    knowledge_products,
    knowledge_sources,
    snapshot_assets,
    source_snapshots,
)
from knowledge_agent.wordpress import MAX_WEB_RESOURCE_BYTES  # noqa: E402
from server_schema import (  # noqa: E402
    organizations,
    project_memberships,
    project_ownership,
    workspace_users,
)
from services.access_control import (  # noqa: E402
    ActorIdentity,
    ProjectAccessDenied,
)
from services.job_queue import JobCancelled  # noqa: E402
from services.object_store import (  # noqa: E402
    StoredObject,
    build_project_object_key,
)
from services.server_web_evidence_ingestion import (  # noqa: E402
    CheckpointingOfficialSiteFetcher,
    PostgresServerWebEvidenceIngestion,
    ServerWebEvidenceConflict,
    ServerWebEvidenceContext,
    ServerWebEvidenceUnavailable,
)


DATABASE_URL_ENV = "ARTICLE_AGENT_DATABASE_URL"
BUCKET = "web-evidence-test-bucket"


def product_image_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (640, 480), color=(130, 100, 70)).save(
        output,
        format="PNG",
    )
    return output.getvalue()


def product_html(
    *,
    canonical_url: str,
    image_url: str,
    description: str = "Carbon steel wood screw for timber connections.",
) -> bytes:
    return f"""
    <html>
      <head>
        <title>Official Wood Screw</title>
        <link rel="canonical" href="{canonical_url}" />
        <script type="application/ld+json">
          {{
            "@context":"https://schema.org",
            "@type":"Product",
            "name":"Official Wood Screw",
            "image":["{image_url}"]
          }}
        </script>
      </head>
      <body class="single-product">
        <nav class="woocommerce-breadcrumb">
          <a href="/">Home</a><a href="/fasteners/">Fasteners</a>
          <span>Wood Screws</span>
        </nav>
        <main>
          <h1>Official Wood Screw</h1>
          <div class="woocommerce-product-gallery">
            <img src="{image_url}" alt="Official wood screw" />
          </div>
          <p>{description}</p>
          <table class="specifications">
            <tr><th>Material</th><td>Carbon steel</td></tr>
          </table>
        </main>
      </body>
    </html>
    """.encode()


class FakeOfficialSiteFetcher:
    def __init__(self, responses: dict[str, tuple[bytes, str]]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def fetch(
        self,
        *,
        site_url: str,
        url: str,
        max_bytes: int = MAX_WEB_RESOURCE_BYTES,
    ) -> FetchedResource:
        del site_url
        self.calls.append(url)
        response = self.responses.get(url)
        if response is None:
            raise OfficialSiteFetchError("fake response was not found")
        content, content_type = response
        if len(content) > max_bytes:
            raise OfficialSiteFetchError("fake response exceeds limit")
        return FetchedResource(
            requested_url=url,
            final_url=url,
            content=content,
            content_type=content_type,
        )


class RecordingObjectStore:
    def __init__(self) -> None:
        self.put_calls: list[dict[str, object]] = []
        self.objects: dict[str, bytes] = {}

    def put(
        self,
        *,
        key: str,
        data: bytes,
        content_type: str,
        metadata=None,
    ) -> StoredObject:
        body = bytes(data)
        digest = hashlib.sha256(body).hexdigest()
        self.objects[key] = body
        self.put_calls.append(
            {
                "key": key,
                "content_type": content_type,
                "metadata": dict(metadata or {}),
            }
        )
        return StoredObject(
            key=key,
            content_hash=digest,
            content_type=content_type,
            byte_size=len(body),
            etag="web-evidence-test-etag",
        )


class RecordingAuditWriter:
    def __init__(self) -> None:
        self.events: list[object] = []

    def append(self, connection, event) -> None:
        if not connection.in_transaction():
            raise AssertionError("audit must share the evidence transaction")
        self.events.append(event)


class UniqueRecordingAuditWriter(RecordingAuditWriter):
    def append(self, connection, event) -> None:
        if any(
            existing.event_id == event.event_id
            for existing in self.events
        ):
            raise RuntimeError("duplicate audit event identity")
        super().append(connection, event)


class FailingAuditWriter:
    def append(self, connection, event) -> None:
        del connection, event
        raise RuntimeError(
            "audit failed with https://private-audit.example/secret"
        )


class FailingAfterProductCatalog:
    def __init__(self, delegate: PostgresProductCatalogRepository) -> None:
        self._delegate = delegate

    def upsert_product_in_transaction(self, connection, product) -> bool:
        self._delegate.upsert_product_in_transaction(connection, product)
        raise RuntimeError("catalog failed with catalog-private-token")


class FailingDuringAssetRepository:
    def __init__(self, delegate: PostgresKnowledgeAssetRepository) -> None:
        self._delegate = delegate

    def put_asset_in_transaction(self, connection, asset):
        self._delegate.put_asset_in_transaction(connection, asset)
        raise RuntimeError("asset failed with asset-private-token")


@unittest.skipUnless(
    os.environ.get(DATABASE_URL_ENV),
    f"{DATABASE_URL_ENV} is required for PostgreSQL integration tests",
)
class ServerWebEvidenceIngestionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = create_knowledge_engine(
            os.environ[DATABASE_URL_ENV]
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    def setUp(self) -> None:
        prefix = f"m7-web-evidence-{uuid.uuid4().hex}"
        self.organization_id = f"{prefix}-org"
        self.project_id = f"{prefix}.example.test"
        self.editor_id = f"{prefix}-editor"
        self.site_url = f"https://{self.project_id}"
        self.page_url = f"{self.site_url}/product/wood-screw/"
        self.image_url = f"{self.site_url}/uploads/wood-screw.png"

        self.repository = PostgresKnowledgeRepository(self.engine)
        self.repository.upsert_project(
            KnowledgeProject(
                project_id=self.project_id,
                customer_name="Web Evidence Test Customer",
                official_domain=self.project_id,
            )
        )
        with self.engine.begin() as connection:
            connection.execute(
                organizations.insert().values(
                    organization_id=self.organization_id,
                    name="Web Evidence Test Organization",
                )
            )
            connection.execute(
                workspace_users.insert().values(
                    organization_id=self.organization_id,
                    user_id=self.editor_id,
                    display_name="Knowledge Editor",
                )
            )
            connection.execute(
                project_ownership.insert().values(
                    organization_id=self.organization_id,
                    project_id=self.project_id,
                )
            )
            connection.execute(
                project_memberships.insert().values(
                    organization_id=self.organization_id,
                    project_id=self.project_id,
                    user_id=self.editor_id,
                    role="editor",
                    granted_by_user_id=self.editor_id,
                )
            )

        self.actor = ActorIdentity(
            self.organization_id,
            self.editor_id,
        )
        self.context = ServerWebEvidenceContext(
            actor=self.actor,
            project_id=self.project_id,
            operation="knowledge_research",
            target_type="research_attempt",
            target_id=f"attempt-{uuid.uuid4().hex}",
            permission="knowledge.edit",
        )
        self.store = RecordingObjectStore()
        self.fetcher = FakeOfficialSiteFetcher(
            {
                self.page_url: (
                    product_html(
                        canonical_url=self.page_url,
                        image_url=self.image_url,
                    ),
                    "text/html; charset=utf-8",
                ),
                self.image_url: (product_image_bytes(), "image/png"),
            }
        )
        self.audit = RecordingAuditWriter()
        self.preparer = self._preparer()
        self.service = self._service()

    def tearDown(self) -> None:
        with self.engine.begin() as connection:
            for table in (
                knowledge_product_asset_evidence,
                knowledge_product_source_evidence,
                knowledge_products,
                snapshot_assets,
                knowledge_assets,
                knowledge_chunks,
                source_snapshots,
                knowledge_sources,
            ):
                connection.execute(
                    table.delete().where(
                        table.c.project_id == self.project_id
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
                sa.text("DELETE FROM projects WHERE project_id = :project_id"),
                {"project_id": self.project_id},
            )

    def _preparer(self) -> OfficialWebPageIngestionService:
        return OfficialWebPageIngestionService(
            repository=self.repository,
            asset_repository=PostgresKnowledgeAssetRepository(self.engine),
            catalog_repository=PostgresProductCatalogRepository(self.engine),
            artifact_store=ScopedS3ArtifactStore(
                store=self.store,
                bucket=BUCKET,
                organization_id=self.organization_id,
                project_id=self.project_id,
            ),
            fetcher=self.fetcher,
            snapshot_lookup=PostgresKnowledgeLibrary(self.engine),
        )

    def _service(
        self,
        *,
        audit=None,
        context: ServerWebEvidenceContext | None = None,
        assets=None,
        catalog=None,
    ) -> PostgresServerWebEvidenceIngestion:
        return PostgresServerWebEvidenceIngestion(
            self.engine,
            preparer=self.preparer,
            context=self.context if context is None else context,
            bucket=BUCKET,
            assets=assets,
            catalog=catalog,
            audit=self.audit if audit is None else audit,
        )

    def _prepare(self, *, metadata=None):
        return self.preparer.prepare_url(
            project_id=self.project_id,
            site_url=self.site_url,
            url=self.page_url,
            metadata=metadata,
        )

    def _ingest(self, *, metadata=None):
        return self.service.ingest_url(
            project_id=self.project_id,
            site_url=self.site_url,
            url=self.page_url,
            metadata=metadata,
        )

    def _row_counts(self) -> dict[str, int]:
        tables = {
            "sources": knowledge_sources,
            "snapshots": source_snapshots,
            "chunks": knowledge_chunks,
            "assets": knowledge_assets,
            "snapshot_assets": snapshot_assets,
            "products": knowledge_products,
            "source_evidence": knowledge_product_source_evidence,
            "asset_evidence": knowledge_product_asset_evidence,
        }
        with self.engine.connect() as connection:
            return {
                name: int(
                    connection.execute(
                        sa.select(sa.func.count())
                        .select_from(table)
                        .where(table.c.project_id == self.project_id)
                    ).scalar_one()
                )
                for name, table in tables.items()
            }

    def _empty_counts(self) -> dict[str, int]:
        return {
            "sources": 0,
            "snapshots": 0,
            "chunks": 0,
            "assets": 0,
            "snapshot_assets": 0,
            "products": 0,
            "source_evidence": 0,
            "asset_evidence": 0,
        }

    def _product_updated_at(self, product_id: str):
        with self.engine.connect() as connection:
            return connection.execute(
                sa.select(knowledge_products.c.updated_at).where(
                    knowledge_products.c.project_id == self.project_id,
                    knowledge_products.c.product_id == product_id,
                )
            ).scalar_one()

    def test_prepare_writes_objects_but_no_database_rows(self) -> None:
        prepared = self._prepare()

        self.assertEqual(prepared.classification.page_type, "product_detail")
        self.assertIsNotNone(prepared.product)
        self.assertGreaterEqual(len(self.store.put_calls), 3)
        self.assertEqual(self._row_counts(), self._empty_counts())
        self.assertEqual(self.audit.events, [])

    def test_success_commits_complete_graph_and_redacted_audit(self) -> None:
        secret = "web-evidence-api-key-must-not-leak"
        result = self._ingest(
            metadata={
                "api_key": secret,
                "operator_note": "private ingestion note",
            }
        )

        counts = self._row_counts()
        self.assertEqual(counts["sources"], 1)
        self.assertEqual(counts["snapshots"], 1)
        self.assertGreater(counts["chunks"], 0)
        self.assertEqual(counts["assets"], 1)
        self.assertEqual(counts["snapshot_assets"], 1)
        self.assertEqual(counts["products"], 1)
        self.assertEqual(counts["source_evidence"], 1)
        self.assertEqual(counts["asset_evidence"], 1)
        self.assertIsNotNone(result.product)
        self.assertEqual(len(result.assets), 1)

        self.assertEqual(len(self.audit.events), 1)
        event = self.audit.events[0]
        self.assertEqual(event.action, "knowledge.web_snapshot.ingested")
        self.assertEqual(event.project_id, self.project_id)
        self.assertEqual(event.target_id, result.source.source_id)
        self.assertEqual(
            set(event.details),
            {
                "operation",
                "context_type",
                "context_id",
                "source_kind",
                "page_type",
                "chunk_count",
                "asset_count",
                "product_evidence_count",
                "warning_count",
            },
        )
        serialized_audit = str(event).casefold()
        self.assertNotIn(secret.casefold(), serialized_audit)
        self.assertNotIn(self.page_url.casefold(), serialized_audit)
        self.assertNotIn("official wood screw", serialized_audit)
        self.assertNotIn("s3://", serialized_audit)
        self.assertNotIn("content_hash", serialized_audit)

    def test_audit_failure_rolls_back_complete_database_graph(self) -> None:
        service = self._service(audit=FailingAuditWriter())

        with self.assertRaisesRegex(
            ServerWebEvidenceUnavailable,
            "^web evidence ingestion is temporarily unavailable$",
        ) as captured:
            service.ingest_url(
                project_id=self.project_id,
                site_url=self.site_url,
                url=self.page_url,
            )

        self.assertNotIn("private-audit", str(captured.exception))
        self.assertTrue(self.store.put_calls)
        self.assertEqual(self._row_counts(), self._empty_counts())

    def test_permission_revocation_after_prepare_rejects_commit(self) -> None:
        prepared = self._prepare()
        with self.engine.begin() as connection:
            connection.execute(
                project_memberships.delete().where(
                    project_memberships.c.organization_id
                    == self.organization_id,
                    project_memberships.c.project_id == self.project_id,
                    project_memberships.c.user_id == self.editor_id,
                )
            )

        with self.assertRaisesRegex(
            ProjectAccessDenied,
            "^project access denied$",
        ):
            self.service.commit(prepared)

        self.assertTrue(self.store.put_calls)
        self.assertEqual(self._row_counts(), self._empty_counts())
        self.assertEqual(self.audit.events, [])

    def test_cancellation_after_prepare_rejects_commit(self) -> None:
        cancellation = {"requested": False}
        context = replace(
            self.context,
            cancelled=lambda: cancellation["requested"],
        )
        service = self._service(context=context)
        prepared = self._prepare()
        cancellation["requested"] = True

        with self.assertRaisesRegex(
            JobCancelled,
            "^Web evidence ingestion cancelled[.]$",
        ):
            service.commit(prepared)

        self.assertTrue(self.store.put_calls)
        self.assertEqual(self._row_counts(), self._empty_counts())
        self.assertEqual(self.audit.events, [])

    def test_checkpointing_fetcher_checks_before_and_after_delegate(self) -> None:
        events: list[str] = []

        class OrderedDelegate:
            def fetch(self, **kwargs):
                events.append("fetch")
                return FetchedResource(
                    requested_url=str(kwargs["url"]),
                    final_url=str(kwargs["url"]),
                    content=b"<html><body>Evidence</body></html>",
                    content_type="text/html",
                )

        fetcher = CheckpointingOfficialSiteFetcher(
            OrderedDelegate(),
            checkpoint=lambda: events.append("checkpoint"),
        )

        resource = fetcher.fetch(
            site_url=self.site_url,
            url=self.page_url,
        )

        self.assertEqual(resource.final_url, self.page_url)
        self.assertEqual(events, ["checkpoint", "fetch", "checkpoint"])

    def test_checkpointing_fetcher_propagates_post_fetch_failures(self) -> None:
        for error in (
            ProjectAccessDenied("project access denied"),
            JobCancelled("fetch cancelled"),
        ):
            with self.subTest(error_type=type(error).__name__):
                events: list[str] = []

                class OrderedDelegate:
                    def fetch(self, **kwargs):
                        events.append("fetch")
                        return FetchedResource(
                            requested_url=str(kwargs["url"]),
                            final_url=str(kwargs["url"]),
                            content=b"<html><body>Evidence</body></html>",
                            content_type="text/html",
                        )

                def checkpoint() -> None:
                    events.append("checkpoint")
                    if events == ["checkpoint", "fetch", "checkpoint"]:
                        raise error

                fetcher = CheckpointingOfficialSiteFetcher(
                    OrderedDelegate(),
                    checkpoint=checkpoint,
                )

                with self.assertRaises(type(error)) as captured:
                    fetcher.fetch(
                        site_url=self.site_url,
                        url=self.page_url,
                    )

                self.assertIs(captured.exception, error)
                self.assertEqual(
                    events,
                    ["checkpoint", "fetch", "checkpoint"],
                )

    def test_identical_retry_does_not_duplicate_rows_or_audit(self) -> None:
        first = self._ingest()
        first_counts = self._row_counts()

        second = self._ingest()

        self.assertEqual(
            second.snapshot.snapshot_id,
            first.snapshot.snapshot_id,
        )
        self.assertEqual(self._row_counts(), first_counts)
        self.assertEqual(len(self.audit.events), 1)

    def test_prepared_same_content_canonicalizes_to_first_snapshot(self) -> None:
        first_prepared = self._prepare()
        second_prepared = self._prepare()
        second_prepared = replace(
            second_prepared,
            snapshot=replace(
                second_prepared.snapshot,
                fetched_at=(
                    first_prepared.snapshot.fetched_at
                    + timedelta(seconds=1)
                ),
            ),
        )
        self.assertNotEqual(
            second_prepared.snapshot.fetched_at,
            first_prepared.snapshot.fetched_at,
        )

        first = self.service.commit(first_prepared)
        first_counts = self._row_counts()
        second = self.service.commit(second_prepared)

        self.assertEqual(
            second.snapshot.snapshot_id,
            first.snapshot.snapshot_id,
        )
        self.assertEqual(
            second.snapshot.fetched_at,
            first.snapshot.fetched_at,
        )
        self.assertNotEqual(
            second.snapshot.fetched_at,
            second_prepared.snapshot.fetched_at,
        )
        self.assertEqual(self._row_counts(), first_counts)
        self.assertEqual(
            [event.action for event in self.audit.events],
            ["knowledge.web_snapshot.ingested"],
        )

    def test_deduplicated_asset_identity_is_used_by_all_evidence(self) -> None:
        prepared = self._prepare()
        self.assertEqual(len(prepared.assets), 1)
        existing_asset_id = f"existing-{uuid.uuid4().hex}"
        PostgresKnowledgeAssetRepository(self.engine).put_asset(
            replace(
                prepared.assets[0],
                asset_id=existing_asset_id,
            )
        )

        result = self.service.commit(prepared)

        self.assertEqual(
            [asset.asset_id for asset in result.assets],
            [existing_asset_id],
        )
        with self.engine.connect() as connection:
            linked_asset_id = connection.execute(
                sa.select(snapshot_assets.c.asset_id).where(
                    snapshot_assets.c.project_id == self.project_id,
                    snapshot_assets.c.source_id == result.source.source_id,
                    snapshot_assets.c.snapshot_id
                    == result.snapshot.snapshot_id,
                )
            ).scalar_one()
            evidence_asset_id = connection.execute(
                sa.select(
                    knowledge_product_asset_evidence.c.asset_id
                ).where(
                    knowledge_product_asset_evidence.c.project_id
                    == self.project_id,
                    knowledge_product_asset_evidence.c.source_id
                    == result.source.source_id,
                    knowledge_product_asset_evidence.c.snapshot_id
                    == result.snapshot.snapshot_id,
                )
            ).scalar_one()
        self.assertEqual(linked_asset_id, existing_asset_id)
        self.assertEqual(evidence_asset_id, existing_asset_id)
        self.assertEqual(self._row_counts()["assets"], 1)
        self.assertEqual(len(self.audit.events), 1)

    def test_mid_transaction_repository_failures_roll_back_all_rows(
        self,
    ) -> None:
        cases = (
            (
                "catalog-after-product",
                {
                    "catalog": FailingAfterProductCatalog(
                        PostgresProductCatalogRepository(self.engine)
                    )
                },
                "catalog-private-token",
            ),
            (
                "asset-during-put",
                {
                    "assets": FailingDuringAssetRepository(
                        PostgresKnowledgeAssetRepository(self.engine)
                    )
                },
                "asset-private-token",
            ),
        )
        for name, service_arguments, private_message in cases:
            with self.subTest(name=name):
                service = self._service(**service_arguments)

                with self.assertRaisesRegex(
                    ServerWebEvidenceUnavailable,
                    "^web evidence ingestion is temporarily unavailable$",
                ) as captured:
                    service.ingest_url(
                        project_id=self.project_id,
                        site_url=self.site_url,
                        url=self.page_url,
                    )

                self.assertNotIn(
                    private_message,
                    str(captured.exception),
                )
                self.assertEqual(self._row_counts(), self._empty_counts())
                self.assertEqual(self.audit.events, [])

    def test_identical_retry_does_not_touch_product_updated_at(self) -> None:
        first = self._ingest()
        product = first.product
        self.assertIsNotNone(product)
        assert product is not None
        first_updated_at = self._product_updated_at(product.product_id)

        self._ingest()

        self.assertEqual(
            self._product_updated_at(product.product_id),
            first_updated_at,
        )

    def test_retry_returns_published_and_confirmed_stored_aggregates(
        self,
    ) -> None:
        first = self._ingest()
        product = first.product
        self.assertIsNotNone(product)
        assert product is not None
        with self.engine.begin() as connection:
            connection.execute(
                knowledge_sources.update()
                .where(
                    knowledge_sources.c.project_id == self.project_id,
                    knowledge_sources.c.source_id == first.source.source_id,
                )
                .values(
                    status="published",
                    current_snapshot_id=first.snapshot.snapshot_id,
                )
            )
        PostgresProductCatalogRepository(self.engine).confirm_product(
            self.project_id,
            product.product_id,
        )

        retried = self._ingest()

        self.assertEqual(retried.source.status, "published")
        self.assertEqual(
            retried.source.current_snapshot_id,
            first.snapshot.snapshot_id,
        )
        self.assertIsNotNone(retried.product)
        assert retried.product is not None
        self.assertEqual(retried.product.status, "confirmed")
        self.assertEqual(len(self.audit.events), 1)

    def test_retry_repairs_missing_asset_evidence_and_audits_reconcile(
        self,
    ) -> None:
        service = PostgresServerWebEvidenceIngestion(
            self.engine,
            preparer=self.preparer,
            context=self.context,
            bucket=BUCKET,
            audit=(repair_audit := UniqueRecordingAuditWriter()),
        )
        first = service.ingest_url(
            project_id=self.project_id,
            site_url=self.site_url,
            url=self.page_url,
        )
        first_counts = self._row_counts()
        with self.engine.begin() as connection:
            deleted = connection.execute(
                knowledge_product_asset_evidence.delete().where(
                    knowledge_product_asset_evidence.c.project_id
                    == self.project_id,
                    knowledge_product_asset_evidence.c.source_id
                    == first.source.source_id,
                    knowledge_product_asset_evidence.c.snapshot_id
                    == first.snapshot.snapshot_id,
                )
            ).rowcount
        self.assertEqual(deleted, 1)
        self.assertEqual(self._row_counts()["asset_evidence"], 0)

        retried = service.ingest_url(
            project_id=self.project_id,
            site_url=self.site_url,
            url=self.page_url,
        )

        self.assertEqual(
            retried.snapshot.snapshot_id,
            first.snapshot.snapshot_id,
        )
        self.assertEqual(self._row_counts(), first_counts)
        self.assertCountEqual(
            [event.action for event in repair_audit.events],
            [
                "knowledge.web_snapshot.ingested",
                "knowledge.web_snapshot.reconciled",
            ],
        )

    def test_wrong_object_scope_is_rejected_without_database_rows(self) -> None:
        prepared = self._prepare()
        invalid = replace(
            prepared,
            snapshot=replace(
                prepared.snapshot,
                raw_artifact_uri=prepared.snapshot.raw_artifact_uri.replace(
                    f"s3://{BUCKET}/",
                    "s3://wrong-bucket/",
                    1,
                ),
            ),
        )

        with self.assertRaisesRegex(
            ServerWebEvidenceConflict,
            "^web artifacts are outside the server scope$",
        ):
            self.service.commit(invalid)

        self.assertEqual(self._row_counts(), self._empty_counts())
        self.assertEqual(self.audit.events, [])

    def test_wrong_normalized_digest_in_same_project_is_rejected(self) -> None:
        prepared = self._prepare()
        wrong_digest = (
            "0" * 64
            if prepared.normalized_content_hash != "0" * 64
            else "1" * 64
        )
        wrong_key = build_project_object_key(
            self.organization_id,
            self.project_id,
            wrong_digest,
        )
        invalid = replace(
            prepared,
            snapshot=replace(
                prepared.snapshot,
                normalized_artifact_uri=f"s3://{BUCKET}/{wrong_key}",
            ),
        )

        with self.assertRaisesRegex(
            ServerWebEvidenceConflict,
            "^web artifacts are outside the server scope$",
        ):
            self.service.commit(invalid)

        self.assertEqual(self._row_counts(), self._empty_counts())
        self.assertEqual(self.audit.events, [])

    def test_prepared_webpage_rejects_an_unlinked_asset(self) -> None:
        prepared = self._prepare()
        self.assertEqual(len(prepared.assets), 1)

        with self.assertRaisesRegex(
            ValueError,
            "^every prepared asset must have one snapshot link$",
        ):
            replace(prepared, snapshot_assets=())

        self.assertEqual(self._row_counts(), self._empty_counts())
        self.assertEqual(self.audit.events, [])

    def test_changed_content_for_same_source_requires_snapshot_review(self) -> None:
        first = self._ingest()
        first_counts = self._row_counts()
        self.fetcher.responses[self.page_url] = (
            product_html(
                canonical_url=self.page_url,
                image_url=self.image_url,
                description=(
                    "Changed authoritative product facts require a new review."
                ),
            ),
            "text/html; charset=utf-8",
        )
        changed = self._prepare()
        self.assertNotEqual(
            changed.snapshot.content_hash,
            first.snapshot.content_hash,
        )

        with self.assertRaisesRegex(
            ServerWebEvidenceConflict,
            "^web source refresh requires snapshot-bound review$",
        ):
            self.service.commit(changed)

        self.assertEqual(self._row_counts(), first_counts)
        self.assertEqual(len(self.audit.events), 1)


if __name__ == "__main__":
    unittest.main()
