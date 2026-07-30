from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

import sqlalchemy as sa


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from knowledge_agent import (  # noqa: E402
    KnowledgeAsset,
    KnowledgeChunk,
    KnowledgeProduct,
    KnowledgeProject,
    KnowledgeSource,
    PostgresKnowledgeAssetRepository,
    PostgresKnowledgeRepository,
    PostgresProductCatalogRepository,
    ProductAssetEvidence,
    ProductCatalogNotFound,
    ProductConfirmationError,
    ProductSourceEvidence,
    SnapshotAsset,
    SourceSnapshot,
    create_knowledge_engine,
)


class ProductCatalogContractTests(unittest.TestCase):
    def test_product_and_evidence_validate_business_identity(self) -> None:
        product = KnowledgeProduct(
            project_id="project-a",
            product_id="wood-screw",
            name="Wood Screw",
            canonical_url="https://example.com/products/wood-screw",
            category_path=("Fasteners", "Screws", "Wood Screws"),
        )
        self.assertEqual(product.status, "inbox")
        self.assertEqual(product.category_path[-1], "Wood Screws")

        with self.assertRaisesRegex(ValueError, "between 0 and 1"):
            ProductSourceEvidence(
                project_id="project-a",
                product_id="wood-screw",
                source_id="source-a",
                snapshot_id="snapshot-a",
                relation="primary_detail",
                confidence=1.5,
                reason="Product H1 and schema.org Product agree.",
            )
        with self.assertRaisesRegex(ValueError, "role"):
            ProductAssetEvidence(
                project_id="project-a",
                product_id="wood-screw",
                source_id="source-a",
                snapshot_id="snapshot-a",
                asset_id="asset-a",
                role="logo",  # type: ignore[arg-type]
                confidence=0.8,
                reason="Invalid role.",
            )


@unittest.skipUnless(
    os.environ.get("ARTICLE_AGENT_DATABASE_URL"),
    "ARTICLE_AGENT_DATABASE_URL is required for PostgreSQL integration tests",
)
class ProductCatalogRepositoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = create_knowledge_engine(os.environ["ARTICLE_AGENT_DATABASE_URL"])

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    def setUp(self) -> None:
        with self.engine.begin() as connection:
            for table in (
                "evidence_links",
                "evidence_pack_hits",
                "evidence_packs",
                "retrieval_scopes",
                "retrieval_plans",
                "knowledge_product_asset_evidence",
                "knowledge_product_source_evidence",
                "knowledge_products",
                "snapshot_assets",
                "knowledge_assets",
                "knowledge_chunks",
                "source_snapshots",
                "knowledge_sources",
                "projects",
            ):
                connection.execute(
                    sa.text(
                        f"DELETE FROM {table} "
                        "WHERE project_id IN ('project-a', 'project-b')"
                    )
                )
        self.knowledge_repository = PostgresKnowledgeRepository(self.engine)
        self.asset_repository = PostgresKnowledgeAssetRepository(self.engine)
        self.catalog_repository = PostgresProductCatalogRepository(self.engine)
        for project_id in ("project-a", "project-b"):
            self.knowledge_repository.upsert_project(
                KnowledgeProject(
                    project_id=project_id,
                    customer_name=project_id,
                    official_domain=f"{project_id}.example.com",
                )
            )
            self.knowledge_repository.upsert_source(
                KnowledgeSource(
                    project_id=project_id,
                    source_id="source-a",
                    display_name="Wood Screw",
                    source_kind="product_detail",
                    trust_tier="hard_fact",
                    canonical_url=f"https://{project_id}.example.com/wood-screw",
                    public_source=True,
                )
            )
            self.knowledge_repository.store_snapshot(
                project_id,
                SourceSnapshot(
                    project_id=project_id,
                    source_id="source-a",
                    snapshot_id="snapshot-a",
                    content_hash="b" * 64,
                    fetched_at=datetime.now(timezone.utc),
                    parser_name="html",
                    parser_version="1",
                ),
                (
                    KnowledgeChunk(
                        project_id=project_id,
                        chunk_id="snapshot-a:0000",
                        source_id="source-a",
                        snapshot_id="snapshot-a",
                        text="Official wood screw product detail.",
                    ),
                ),
            )

    def tearDown(self) -> None:
        with self.engine.begin() as connection:
            for table in (
                "evidence_links",
                "evidence_pack_hits",
                "evidence_packs",
                "retrieval_scopes",
                "retrieval_plans",
                "knowledge_product_asset_evidence",
                "knowledge_product_source_evidence",
                "knowledge_products",
                "snapshot_assets",
                "knowledge_assets",
                "knowledge_chunks",
                "source_snapshots",
                "knowledge_sources",
                "projects",
            ):
                connection.execute(
                    sa.text(
                        f"DELETE FROM {table} "
                        "WHERE project_id IN ('project-a', 'project-b')"
                    )
                )

    def _store_product_and_asset(self) -> None:
        self.catalog_repository.upsert_product(
            KnowledgeProduct(
                project_id="project-a",
                product_id="wood-screw",
                name="Wood Screw",
                canonical_url="https://project-a.example.com/wood-screw",
                category_path=("Fasteners", "Wood Screws"),
            )
        )
        stored_asset = self.asset_repository.put_asset(
            KnowledgeAsset(
                project_id="project-a",
                asset_id="asset-a",
                content_hash="a" * 64,
                artifact_uri="file:///knowledge/project-a/asset-a.png",
                content_type="image/png",
                byte_size=100,
                width=640,
                height=480,
            )
        )
        self.asset_repository.link_snapshot_asset(
            SnapshotAsset(
                project_id="project-a",
                source_id="source-a",
                snapshot_id="snapshot-a",
                asset_id=stored_asset.asset_id,
                evidence_kind="gallery",
                ordinal=0,
                source_url="https://project-a.example.com/wood-screw.png",
            )
        )

    def test_confirmation_requires_primary_detail_and_preserves_evidence(self) -> None:
        self._store_product_and_asset()
        with self.assertRaises(ProductConfirmationError):
            self.catalog_repository.confirm_product("project-a", "wood-screw")

        source_evidence = ProductSourceEvidence(
            project_id="project-a",
            product_id="wood-screw",
            source_id="source-a",
            snapshot_id="snapshot-a",
            relation="primary_detail",
            confidence=0.98,
            reason="Canonical product URL, H1, and Product schema agree.",
        )
        asset_evidence = ProductAssetEvidence(
            project_id="project-a",
            product_id="wood-screw",
            source_id="source-a",
            snapshot_id="snapshot-a",
            asset_id="asset-a",
            role="primary",
            confidence=0.95,
            reason="Image belongs to the confirmed product gallery.",
        )
        self.catalog_repository.store_source_evidence(source_evidence)
        self.catalog_repository.store_source_evidence(source_evidence)
        self.catalog_repository.store_asset_evidence(asset_evidence)
        self.catalog_repository.store_asset_evidence(asset_evidence)
        self.catalog_repository.confirm_product("project-a", "wood-screw")

        confirmed = self.catalog_repository.get_product("project-a", "wood-screw")
        self.assertIsNotNone(confirmed)
        self.assertEqual(confirmed.status, "confirmed")  # type: ignore[union-attr]
        self.assertEqual(
            self.catalog_repository.list_products(
                "project-a", status="confirmed"
            ),
            (confirmed,),
        )

    def test_cross_project_product_evidence_is_rejected(self) -> None:
        self._store_product_and_asset()
        with self.assertRaises(ProductCatalogNotFound):
            self.catalog_repository.store_source_evidence(
                ProductSourceEvidence(
                    project_id="project-b",
                    product_id="wood-screw",
                    source_id="source-a",
                    snapshot_id="snapshot-a",
                    relation="primary_detail",
                    confidence=0.9,
                    reason="Must not cross project boundaries.",
                )
            )


if __name__ == "__main__":
    unittest.main()
