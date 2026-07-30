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
    KnowledgeAssetConflictError,
    KnowledgeAssetNotFound,
    KnowledgeChunk,
    KnowledgeProject,
    KnowledgeSource,
    PostgresKnowledgeAssetRepository,
    PostgresKnowledgeRepository,
    SnapshotAsset,
    SourceSnapshot,
    create_knowledge_engine,
)


def asset(
    project_id: str = "project-a",
    *,
    asset_id: str = "asset-a",
    content_hash: str = "a" * 64,
) -> KnowledgeAsset:
    return KnowledgeAsset(
        project_id=project_id,
        asset_id=asset_id,
        content_hash=content_hash,
        artifact_uri=f"file:///knowledge/{project_id}/{content_hash}.png",
        content_type="image/png",
        byte_size=128,
        width=640,
        height=480,
        metadata={"origin": "test"},
    )


class KnowledgeAssetContractTests(unittest.TestCase):
    def test_asset_validates_hash_uri_size_and_dimensions(self) -> None:
        candidate = asset()
        self.assertEqual(candidate.content_hash, "a" * 64)
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            asset(content_hash="invalid")
        with self.assertRaisesRegex(ValueError, "both be present"):
            KnowledgeAsset(
                project_id="project-a",
                asset_id="asset-a",
                content_hash="a" * 64,
                artifact_uri="file:///knowledge/asset.png",
                content_type="image/png",
                byte_size=10,
                width=100,
            )
        with self.assertRaisesRegex(ValueError, "absolute URI"):
            KnowledgeAsset(
                project_id="project-a",
                asset_id="asset-a",
                content_hash="a" * 64,
                artifact_uri=r"C:\knowledge\asset.png",
                content_type="image/png",
                byte_size=10,
            )

    def test_snapshot_asset_validates_evidence_and_source_url(self) -> None:
        link = SnapshotAsset(
            project_id="project-a",
            source_id="source-a",
            snapshot_id="snapshot-a",
            asset_id="asset-a",
            evidence_kind="gallery",
            ordinal=0,
            source_url="https://example.com/images/a.png",
        )
        self.assertEqual(link.evidence_kind, "gallery")
        with self.assertRaisesRegex(ValueError, "evidence_kind"):
            SnapshotAsset(
                project_id="project-a",
                source_id="source-a",
                snapshot_id="snapshot-a",
                asset_id="asset-a",
                evidence_kind="search_result",  # type: ignore[arg-type]
                ordinal=0,
            )
        with self.assertRaisesRegex(ValueError, "absolute HTTP"):
            SnapshotAsset(
                project_id="project-a",
                source_id="source-a",
                snapshot_id="snapshot-a",
                asset_id="asset-a",
                evidence_kind="gallery",
                ordinal=0,
                source_url="javascript:alert(1)",
            )


@unittest.skipUnless(
    os.environ.get("ARTICLE_AGENT_DATABASE_URL"),
    "ARTICLE_AGENT_DATABASE_URL is required for PostgreSQL integration tests",
)
class KnowledgeAssetRepositoryTests(unittest.TestCase):
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
                    display_name="Product",
                    source_kind="product_detail",
                    trust_tier="hard_fact",
                    canonical_url=f"https://{project_id}.example.com/product",
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
                        text="Official product evidence.",
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

    def test_content_hash_deduplicates_and_snapshot_link_is_idempotent(self) -> None:
        first = self.asset_repository.put_asset(asset())
        deduplicated = self.asset_repository.put_asset(
            asset(asset_id="asset-other")
        )
        self.assertEqual(deduplicated.asset_id, first.asset_id)

        link = SnapshotAsset(
            project_id="project-a",
            source_id="source-a",
            snapshot_id="snapshot-a",
            asset_id=first.asset_id,
            evidence_kind="gallery",
            ordinal=0,
            source_url="https://project-a.example.com/image.png",
            alt_text="Wood screw",
            locator={"selector": ".gallery"},
        )
        self.asset_repository.link_snapshot_asset(link)
        self.asset_repository.link_snapshot_asset(link)
        self.assertEqual(
            self.asset_repository.list_snapshot_assets(
                "project-a", "source-a", "snapshot-a"
            ),
            (link,),
        )

    def test_asset_id_conflict_and_cross_project_link_are_rejected(self) -> None:
        stored = self.asset_repository.put_asset(asset())
        with self.assertRaises(KnowledgeAssetConflictError):
            self.asset_repository.put_asset(
                asset(asset_id=stored.asset_id, content_hash="c" * 64)
            )

        with self.assertRaises(KnowledgeAssetNotFound):
            self.asset_repository.link_snapshot_asset(
                SnapshotAsset(
                    project_id="project-b",
                    source_id="source-a",
                    snapshot_id="snapshot-a",
                    asset_id=stored.asset_id,
                    evidence_kind="gallery",
                    ordinal=0,
                )
            )


if __name__ == "__main__":
    unittest.main()
