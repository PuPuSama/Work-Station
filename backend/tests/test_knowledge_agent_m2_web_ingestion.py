from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
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
    LocalKnowledgeArtifactStore,
    OfficialSiteFetchError,
    OfficialWebPageIngestionService,
    PostgresKnowledgeAssetRepository,
    PostgresKnowledgeLibrary,
    PostgresKnowledgeRepository,
    PostgresProductCatalogRepository,
    WordPressProductSyncService,
    create_knowledge_engine,
)
from knowledge_agent.wordpress import MAX_WEB_RESOURCE_BYTES  # noqa: E402
from knowledge_agent.schema import (  # noqa: E402
    knowledge_product_asset_evidence,
    knowledge_product_source_evidence,
    knowledge_sources,
    snapshot_assets,
)


def png_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (640, 480), (170, 170, 170)).save(output, format="PNG")
    return output.getvalue()


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


@unittest.skipUnless(
    os.environ.get("ARTICLE_AGENT_DATABASE_URL"),
    "ARTICLE_AGENT_DATABASE_URL is required for PostgreSQL integration tests",
)
class OfficialWebIngestionIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = create_knowledge_engine(os.environ["ARTICLE_AGENT_DATABASE_URL"])

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
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
                    sa.text(f"DELETE FROM {table} WHERE project_id = 'example.com'")
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
                    sa.text(f"DELETE FROM {table} WHERE project_id = 'example.com'")
                )
        self.temp_directory.cleanup()

    def _service(self) -> WordPressProductSyncService:
        rest_url = "https://example.com/wp-json/"
        category_url = "https://example.com/category/screws/"
        product_url = "https://example.com/product/wood-screw/"
        blog_url = "https://example.com/blog/wood-screw-guide/"
        image_url = "https://example.com/uploads/wood-screw.png"
        category_html = f"""
        <html><body class="archive tax-product_cat">
          <main>
            <h1>Wood Screws</h1>
            <ul class="products">
              <li class="product"><a href="{product_url}">Wood Screw</a></li>
              <li class="product"><a href="{blog_url}">Selection Guide</a></li>
            </ul>
          </main>
        </body></html>
        """.encode()
        product_html = f"""
        <html>
          <head>
            <link rel="canonical" href="{product_url}" />
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
              <p>Carbon steel wood screw for timber connections.</p>
              <table class="specifications">
                <tr><th>Material</th><td>Carbon steel</td></tr>
              </table>
            </main>
          </body>
        </html>
        """.encode()
        blog_html = b"""
        <html>
          <head>
            <script type="application/ld+json">
              {"@context":"https://schema.org","@type":"BlogPosting"}
            </script>
          </head>
          <body class="single-post"><main>
            <h1>Wood screw selection guide</h1>
            <p>This is editorial guidance.</p>
          </main></body>
        </html>
        """
        fetcher = FakeOfficialSiteFetcher(
            {
                rest_url: (
                    json.dumps(
                        {
                            "namespaces": ["wp/v2"],
                            "routes": {"/wp/v2/pages": {}},
                        }
                    ).encode(),
                    "application/json",
                ),
                category_url: (category_html, "text/html; charset=utf-8"),
                product_url: (product_html, "text/html; charset=utf-8"),
                blog_url: (blog_html, "text/html; charset=utf-8"),
                image_url: (png_bytes(), "image/png"),
            }
        )
        repository = PostgresKnowledgeRepository(self.engine)
        repository.upsert_project(
            KnowledgeProject(
                project_id="example.com",
                customer_name="Example",
                official_domain="example.com",
            )
        )
        asset_repository = PostgresKnowledgeAssetRepository(self.engine)
        catalog_repository = PostgresProductCatalogRepository(self.engine)
        library = PostgresKnowledgeLibrary(self.engine)
        page_ingestion = OfficialWebPageIngestionService(
            repository=repository,
            asset_repository=asset_repository,
            catalog_repository=catalog_repository,
            artifact_store=LocalKnowledgeArtifactStore(
                Path(self.temp_directory.name)
            ),
            fetcher=fetcher,
            snapshot_lookup=library,
        )
        return WordPressProductSyncService(
            fetcher=fetcher,
            page_ingestion=page_ingestion,
        )

    def test_sync_keeps_pages_in_inbox_and_links_product_image_evidence(self) -> None:
        service = self._service()

        result = service.sync_category(
            project_id="example.com",
            site_url="https://example.com",
            category_url="https://example.com/category/screws/",
            max_products=10,
        )

        self.assertTrue(result.probe.detected)
        self.assertEqual(result.category.classification.page_type, "product_category")
        self.assertEqual(result.category.source.status, "inbox")
        self.assertEqual(len(result.products), 1)
        product_result = result.products[0]
        self.assertEqual(product_result.classification.page_type, "product_detail")
        self.assertIsNotNone(product_result.product)
        self.assertEqual(len(product_result.assets), 1)
        self.assertEqual(product_result.assets[0].width, 640)
        self.assertEqual(result.skipped_urls, ())

        with self.engine.connect() as connection:
            sources = connection.execute(
                sa.select(
                    knowledge_sources.c.source_kind,
                    knowledge_sources.c.status,
                )
                .where(knowledge_sources.c.project_id == "example.com")
                .order_by(knowledge_sources.c.source_kind)
            ).all()
            source_evidence_count = connection.execute(
                sa.select(sa.func.count())
                .select_from(knowledge_product_source_evidence)
                .where(
                    knowledge_product_source_evidence.c.project_id
                    == "example.com"
                )
            ).scalar_one()
            asset_link_count = connection.execute(
                sa.select(sa.func.count())
                .select_from(snapshot_assets)
                .where(snapshot_assets.c.project_id == "example.com")
            ).scalar_one()
            product_asset_count = connection.execute(
                sa.select(sa.func.count())
                .select_from(knowledge_product_asset_evidence)
                .where(
                    knowledge_product_asset_evidence.c.project_id
                    == "example.com"
                )
            ).scalar_one()
        self.assertEqual(
            sources,
            [("product_category", "inbox"), ("product_detail", "inbox")],
        )
        self.assertEqual(source_evidence_count, 1)
        self.assertEqual(asset_link_count, 1)
        self.assertEqual(product_asset_count, 1)
        summaries = PostgresKnowledgeLibrary(self.engine).list_sources(
            "example.com"
        )
        self.assertTrue(
            all(item.classification_reason for item in summaries)
        )

    def test_identical_category_sync_is_idempotent(self) -> None:
        service = self._service()
        first = service.sync_category(
            project_id="example.com",
            site_url="https://example.com",
            category_url="https://example.com/category/screws/",
        )
        second = service.sync_category(
            project_id="example.com",
            site_url="https://example.com",
            category_url="https://example.com/category/screws/",
        )

        self.assertEqual(
            first.category.snapshot.snapshot_id,
            second.category.snapshot.snapshot_id,
        )
        self.assertEqual(
            first.products[0].snapshot.snapshot_id,
            second.products[0].snapshot.snapshot_id,
        )
        self.assertEqual(
            first.products[0].product,
            second.products[0].product,
        )


if __name__ == "__main__":
    unittest.main()
