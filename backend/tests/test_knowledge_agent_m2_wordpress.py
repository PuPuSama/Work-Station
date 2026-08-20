from __future__ import annotations

import json
import sys
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from PIL import Image


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from knowledge_agent.wordpress import (  # noqa: E402
    FetchedResource,
    OfficialSiteFetchError,
    UnsafeOfficialSiteUrl,
    WordPressSiteProbe,
    classify_web_page,
    discover_category_pagination_links,
    discover_internal_page_links,
    discover_product_links,
    product_links_from_wordpress_rest,
    sitemap_locations,
    normalize_official_url,
)
from knowledge_agent.web_ingestion import (  # noqa: E402
    WebPageIngestionConflict,
    WordPressProductSyncService,
    _image_dimensions,
)
from knowledge_agent.wordpress import WordPressIngestionError  # noqa: E402


class FakeFetcher:
    def __init__(self, responses: dict[str, tuple[bytes, str]]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def fetch(
        self,
        *,
        site_url: str,
        url: str,
        max_bytes: int = 8 * 1024 * 1024,
    ) -> FetchedResource:
        del site_url, max_bytes
        self.calls.append(url)
        response = self.responses.get(url)
        if response is None:
            raise OfficialSiteFetchError("not found")
        content, content_type = response
        return FetchedResource(
            requested_url=url,
            final_url=url,
            content=content,
            content_type=content_type,
        )


class WordPressSiteProbeTests(unittest.TestCase):
    def test_probe_uses_rest_route_fallback_and_keeps_detection_evidence(self) -> None:
        fallback = "https://www.example.com/?rest_route=/"
        payload = json.dumps(
            {
                "namespaces": ["oembed/1.0", "wp/v2"],
                "routes": {
                    "/": {},
                    "/wp/v2/pages": {},
                    "/wp/v2/posts": {},
                },
            }
        ).encode()
        fetcher = FakeFetcher(
            {fallback: (payload, "application/json; charset=utf-8")}
        )

        result = WordPressSiteProbe(fetcher).probe("www.example.com")

        self.assertTrue(result.detected)
        self.assertEqual(result.site_url, "https://www.example.com")
        self.assertEqual(result.rest_api_url, fallback)
        self.assertEqual(result.namespaces, ("oembed/1.0", "wp/v2"))
        self.assertEqual(result.route_count, 3)
        self.assertEqual(
            fetcher.calls,
            [
                "https://www.example.com/wp-json/",
                "https://www.example.com/?rest_route=/",
            ],
        )

    def test_probe_does_not_treat_arbitrary_json_as_wordpress(self) -> None:
        url = "https://example.com/wp-json/"
        fetcher = FakeFetcher(
            {url: (b'{"name":"ordinary api"}', "application/json")}
        )

        result = WordPressSiteProbe(fetcher).probe("https://example.com")

        self.assertFalse(result.detected)
        self.assertIsNone(result.rest_api_url)
        self.assertIn("No WordPress", result.reason)


class OfficialPageClassificationTests(unittest.TestCase):
    def test_product_detail_extracts_identity_facts_and_image_candidates(self) -> None:
        html = """
        <html>
          <head>
            <title>Official Wood Screw</title>
            <link rel="canonical" href="https://example.com/product/wood-screw/" />
            <script type="application/ld+json">
              {
                "@context": "https://schema.org",
                "@type": "Product",
                "name": "Official Wood Screw",
                "image": ["https://example.com/uploads/wood-screw.webp"]
              }
            </script>
          </head>
          <body class="single-product">
            <nav class="woocommerce-breadcrumb">
              <a href="/">Home</a>
              <a href="/fasteners/">Fasteners</a>
              <a href="/fasteners/screws/">Screws</a>
              Wood Screws
            </nav>
            <main>
              <h1>Official Wood Screw</h1>
              <div class="woocommerce-product-gallery">
                <img src="/uploads/wood-screw.webp" alt="Wood screw side view" />
              </div>
              <p>Carbon steel fastener for timber connections.</p>
              <table class="specifications">
                <tr><th>Material</th><td>Carbon steel</td></tr>
              </table>
              <button class="add_to_cart_button">Add to cart</button>
            </main>
          </body>
        </html>
        """

        result = classify_web_page(
            requested_url="https://example.com/product/wood-screw/?tracking=1",
            html=html,
        )

        self.assertEqual(result.page_type, "product_detail")
        self.assertGreaterEqual(result.confidence, 0.9)
        self.assertEqual(
            result.canonical_url,
            "https://example.com/product/wood-screw/",
        )
        self.assertEqual(result.heading, "Official Wood Screw")
        self.assertIn("Fasteners", result.breadcrumbs)
        self.assertIsNotNone(result.product_page)
        assert result.product_page is not None
        self.assertEqual(len(result.product_page.json_ld_product_images), 1)
        self.assertEqual(len(result.product_page.main_gallery), 1)

    def test_blog_schema_is_never_promoted_to_product_by_category_path(self) -> None:
        html = """
        <html>
          <head>
            <script type="application/ld+json">
              {"@context":"https://schema.org","@type":"BlogPosting"}
            </script>
          </head>
          <body class="single-post">
            <main>
              <h1>How to choose a wood screw</h1>
              <p>This guide discusses several screw families.</p>
              <p>It is editorial guidance, not a product detail record.</p>
            </main>
          </body>
        </html>
        """

        result = classify_web_page(
            requested_url="https://example.com/category/guides/wood-screw/",
            html=html,
        )

        self.assertEqual(result.page_type, "official_blog")
        self.assertIsNone(result.product_page)
        self.assertTrue(any("Article" in reason for reason in result.reasons))

    def test_root_level_wordpress_post_wins_over_generic_product_fallback(self) -> None:
        html = """
        <html>
          <head>
            <title>Top Features in a High Quality Aluminum Ladder</title>
            <meta name="description" content="A detailed editorial guide comparing ladder features, materials, placement, and safety for buyers.">
            <meta property="og:image" content="https://example.com/media/guide.jpg">
          </head>
          <body class="post-template-default single single-post postid-5875">
            <main>
              <h1>Top Features in a High Quality Aluminum Ladder</h1>
              <p>This article compares product features and explains how buyers can evaluate different ladder designs safely.</p>
            </main>
          </body>
        </html>
        """

        result = classify_web_page(
            requested_url=(
                "https://example.com/"
                "top-features-in-a-high-quality-aluminum-ladder/"
            ),
            html=html,
        )

        self.assertEqual(result.page_type, "official_blog")
        self.assertIsNone(result.product_page)
        self.assertEqual(result.metadata["parser_version"], "official-web-page/3")

    def test_homepage_with_shared_product_copy_is_not_a_product_detail(self) -> None:
        result = classify_web_page(
            requested_url="https://example.com/",
            html="""
            <html><head>
              <title>Automatic PET Blow Molding Machine Manufacturer</title>
              <meta property="og:image" content="https://example.com/product.jpg">
              <meta name="description" content="Official manufacturer of automatic PET blow molding machines and filling equipment for global beverage factories.">
            </head><body class="home page"><main>
              <h1>Automatic PET Blow Molding Machine Manufacturer</h1>
              <p>Explore complete bottle manufacturing systems, specifications, applications, and after-sales service from our factory.</p>
            </main></body></html>
            """,
        )

        self.assertEqual(result.page_type, "knowledge_page")
        self.assertIsNone(result.product_page)

    def test_multilingual_contact_page_is_not_a_product_detail(self) -> None:
        result = classify_web_page(
            requested_url="https://example.com/de/kontaktieren-sie-uns/",
            html="""
            <html><head>
              <title>Kontaktieren Sie uns - PET Machine</title>
              <meta property="og:image" content="https://example.com/product.jpg">
              <meta name="description" content="Contact our PET blow molding machine factory for specifications, prices, installation, and technical support.">
            </head><body class="page"><main>
              <h1>Kontaktieren Sie uns</h1>
              <p>Send the official sales team your bottle size, output, and factory requirements for a quotation.</p>
            </main></body></html>
            """,
        )

        self.assertEqual(result.page_type, "knowledge_page")
        self.assertIsNone(result.product_page)

    def test_editorial_template_wins_over_embedded_related_product_schema(self) -> None:
        result = classify_web_page(
            requested_url="https://example.com/5-reasons-to-use-a-roof-ladder/",
            html="""
            <html><head><title>5 Reasons to Use a Roof Ladder</title>
              <script type="application/ld+json">
                {"@type":"Product","name":"Related Roof Ladder"}
              </script>
              <meta property="og:image" content="https://example.com/guide.jpg">
            </head><body class="single single-post"><main>
              <h1>5 Reasons to Use a Roof Ladder</h1>
              <p>This editorial guide explains access planning, positioning, inspection, and safe work practices for roof maintenance.</p>
            </main></body></html>
            """,
        )

        self.assertEqual(result.page_type, "official_blog")
        self.assertIsNone(result.product_page)

    def test_category_page_wins_over_nested_child_product_schema(self) -> None:
        result = classify_web_page(
            requested_url="https://example.com/product-category/roof-ladders/",
            html="""
            <html><head><title>Roof Ladders</title>
              <script type="application/ld+json">
                {"@type":"Product","name":"Child Roof Ladder"}
              </script>
            </head><body class="archive tax-product_cat"><main>
              <h1>Roof Ladders</h1>
              <ul class="products"><li class="product">Child product card</li></ul>
            </main></body></html>
            """,
        )

        self.assertEqual(result.page_type, "product_category")
        self.assertIsNone(result.product_page)
        self.assertEqual(result.metadata["parser_version"], "official-web-page/3")

    def test_fixed_company_legal_and_service_pages_are_not_products(self) -> None:
        for slug in (
            "home-2",
            "home-3",
            "oem-odm",
            "privacy-policy",
            "thanks",
        ):
            with self.subTest(slug=slug):
                result = classify_web_page(
                    requested_url=f"https://example.com/{slug}/",
                    html=f"""
                    <html><head><title>{slug} - Machine Manufacturer</title>
                      <meta property="og:image" content="https://example.com/machine.jpg">
                      <meta name="description" content="Official factory services, machine specifications, applications, installation and support for global buyers.">
                    </head><body class="page"><main>
                      <h1>{slug}</h1>
                      <p>Our factory provides engineering, customization, installation and support services for industrial buyers worldwide.</p>
                    </main></body></html>
                    """,
                )

                self.assertEqual(result.page_type, "knowledge_page")
                self.assertIsNone(result.product_page)

    def test_localized_service_and_privacy_pages_are_not_products(self) -> None:
        for path in (
            "/fr/oem-odm/",
            "/fr/politique-de-confidentialite/",
            "/de/datenschutzerklarung/",
        ):
            with self.subTest(path=path):
                result = classify_web_page(
                    requested_url=f"https://example.com{path}",
                    html="""
                    <html><head><title>Industrial Machine Services</title>
                      <meta property="og:image" content="https://example.com/machine.jpg">
                      <meta name="description" content="Official factory services, machine specifications, applications, installation and support for global buyers.">
                    </head><body class="page"><main>
                      <h1>Industrial Machine Services</h1>
                      <p>Our factory provides engineering, customization, installation and support services for industrial buyers worldwide.</p>
                    </main></body></html>
                    """,
                )

                self.assertEqual(result.page_type, "knowledge_page")
                self.assertIsNone(result.product_page)


class GeneralOfficialSiteDiscoveryTests(unittest.TestCase):
    def test_oversized_product_image_is_a_skippable_ingestion_error(self) -> None:
        image = BytesIO()
        Image.new("RGB", (10, 10), color=(20, 40, 60)).save(image, format="PNG")
        original_limit = Image.MAX_IMAGE_PIXELS
        try:
            Image.MAX_IMAGE_PIXELS = 1
            with self.assertRaisesRegex(
                WordPressIngestionError,
                "not a supported raster image",
            ):
                _image_dimensions(image.getvalue())
        finally:
            Image.MAX_IMAGE_PIXELS = original_limit

    def test_sitemap_index_and_urlset_keep_only_same_site_urls(self) -> None:
        index = b"""
        <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <sitemap><loc>https://example.com/pages.xml</loc></sitemap>
          <sitemap><loc>https://cdn.example.net/foreign.xml</loc></sitemap>
        </sitemapindex>
        """
        pages = b"""
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url><loc>https://example.com/about/</loc></url>
          <url><loc>https://example.com/blog/news/</loc></url>
        </urlset>
        """

        self.assertEqual(
            sitemap_locations(site_url="https://example.com", payload=index),
            ("https://example.com/pages.xml",),
        )
        self.assertEqual(
            sitemap_locations(site_url="https://example.com", payload=pages),
            (
                "https://example.com/about/",
                "https://example.com/blog/news/",
            ),
        )

    def test_sitemap_locations_ignores_html_fallback_and_invalid_roots(self) -> None:
        html_fallback = b"<!doctype html><html><body><h1>Not found</h1></body></html>"
        self.assertEqual(
            sitemap_locations(
                site_url="https://example.com",
                payload=html_fallback,
            ),
            (),
        )
        self.assertEqual(
            sitemap_locations(
                site_url="https://example.com",
                payload=b"<html><loc>https://example.com/fake.xml</loc></html>",
            ),
            (),
        )

    def test_internal_link_discovery_is_cms_agnostic_and_filters_assets(self) -> None:
        links = discover_internal_page_links(
            site_url="https://example.com",
            page_url="https://example.com/",
            html="""
            <html><body>
              <nav>
                <a href="/about/">About us</a>
                <a href="/contact/">Contact</a>
                <a href="/products/widget/">Widget</a>
              </nav>
              <a href="/files/catalog.pdf">Catalog</a>
              <a href="https://other.example/blog/">Foreign blog</a>
              <a href="mailto:sales@example.com">Email</a>
            </body></html>
            """,
        )

        self.assertEqual(
            links,
            (
                "https://example.com/about/",
                "https://example.com/contact/",
                "https://example.com/products/widget/",
            ),
        )

    def test_custom_b2b_product_page_without_schema_uses_conservative_fallback(
        self,
    ) -> None:
        html = """
        <html>
          <head>
            <title>Carbon Steel Coach Screws DIN 571</title>
            <meta property="og:title" content="Carbon Steel Coach Screws DIN 571" />
            <meta property="og:description"
                  content="Official dimensional and material information for carbon steel coach screws supplied to industrial buyers." />
            <meta property="og:image" content="https://example.com/uploads/coach-screw.jpg" />
          </head>
          <body>
            <main>
              <h1>Carbon Steel Coach Screws DIN 571</h1>
              <div class="product-gallery">
                <img src="/uploads/coach-screw.jpg" alt="Coach screw" />
              </div>
              <p>Available in zinc plated and stainless steel configurations.</p>
              <p>Contact the official sales team for dimensions and packaging.</p>
            </main>
          </body>
        </html>
        """

        result = classify_web_page(
            requested_url=(
                "https://example.com/"
                "carbon-steel-hexagon-head-coach-screws-din-571/"
            ),
            html=html,
        )

        self.assertEqual(result.page_type, "product_detail")
        self.assertIsNotNone(result.product_page)
        self.assertTrue(any("B2B" in reason for reason in result.reasons))

    def test_category_discovery_keeps_only_same_site_product_context_links(self) -> None:
        html = """
        <html><body>
          <ul class="products">
            <li class="product">
              <a href="/product/wood-screw/"><img src="/wood.jpg"/>Wood Screw</a>
            </li>
            <li class="product">
              <a href="https://shop.example.com/product/drywall-screw/">Drywall</a>
            </li>
            <li class="product"><a href="https://other.test/product/fake/">Fake</a></li>
          </ul>
          <a href="/blog/how-to-install/">Guide</a>
        </body></html>
        """

        links = discover_product_links(
            site_url="https://example.com",
            category_url="https://example.com/category/screws/",
            html=html,
        )

        self.assertEqual(
            links,
            (
                "https://example.com/product/wood-screw/",
                "https://shop.example.com/product/drywall-screw/",
            ),
        )

    def test_category_pagination_discovers_same_site_next_and_numbered_pages(self) -> None:
        html = """
        <html><body>
          <nav class="woocommerce-pagination">
            <a class="page-numbers" href="/products/page/2/">2</a>
            <a rel="next" href="/products/page/3/">Next</a>
            <a href="https://other.test/products/page/4/">4</a>
          </nav>
          <a href="/product/revo-hess/">Product</a>
        </body></html>
        """

        links = discover_category_pagination_links(
            site_url="https://example.com",
            category_url="https://example.com/products/",
            html=html,
        )

        self.assertEqual(
            links,
            (
                "https://example.com/products/page/3/",
                "https://example.com/products/page/2/",
            ),
        )

    def test_product_cards_do_not_turn_products_index_into_detail_page(self) -> None:
        html = """
        <html>
          <head><title>Products - Example</title></head>
          <body class="page woocommerce">
            <main>
              <h1>Products</h1>
              <div class="products">
                <article class="single product">
                  <a href="/product/revo-hess/">REVO HESS</a>
                </article>
                <article class="product">
                  <a href="/product/revo-vm/">REVO VM</a>
                </article>
              </div>
            </main>
          </body>
        </html>
        """

        result = classify_web_page(
            requested_url="https://example.com/products/",
            html=html,
        )

        self.assertEqual(result.page_type, "product_category")

    def test_wordpress_product_route_returns_complete_deduplicated_links(self) -> None:
        links = product_links_from_wordpress_rest(
            site_url="https://example.com",
            payload=json.dumps(
                [
                    {"link": "https://example.com/product/revo-hess/"},
                    {"link": "https://example.com/product/revo-vm/"},
                    {"link": "https://example.com/product/revo-hess/"},
                    {"link": "https://other.test/product/fake/"},
                ]
            ),
            limit=50,
        )

        self.assertEqual(
            links,
            (
                "https://example.com/product/revo-hess/",
                "https://example.com/product/revo-vm/",
            ),
        )

    def test_page_without_main_does_not_treat_first_product_card_as_document_root(
        self,
    ) -> None:
        html = """
        <html>
          <head><title>Wood Screws - Example</title></head>
          <body class="archive tax-product_cat">
            <header><p>Navigation text must be ignored.</p></header>
            <h1>Products</h1>
            <article class="product"><h2>Wood Screw A</h2></article>
            <article class="product"><h2>Wood Screw B</h2></article>
            <p>Official category introduction.</p>
          </body>
        </html>
        """

        result = classify_web_page(
            requested_url="https://example.com/category/wood-screws/",
            html=html,
        )

        self.assertEqual(result.page_type, "product_category")
        self.assertIn("Products", result.text_blocks)
        self.assertIn("Wood Screw B", result.text_blocks)
        self.assertNotIn("Navigation text must be ignored.", result.text_blocks)

    def test_official_url_rejects_cross_site_and_credentials(self) -> None:
        with self.assertRaises(UnsafeOfficialSiteUrl):
            normalize_official_url(
                "https://example.com",
                "https://other.test/product/wood-screw",
            )
        with self.assertRaises(UnsafeOfficialSiteUrl):
            normalize_official_url(
                "https://example.com",
                "https://user:secret@example.com/product/wood-screw",
            )


class WordPressPaginationSyncTests(unittest.TestCase):
    @staticmethod
    def _product_html(name: str) -> bytes:
        return f"""
        <html><head>
          <script type="application/ld+json">
            {{"@context":"https://schema.org","@type":"Product","name":"{name}"}}
          </script>
        </head><body class="single-product"><main>
          <h1>{name}</h1><p>Official product specification information.</p>
        </main></body></html>
        """.encode()

    @staticmethod
    def _page_ingestion():
        class RecordingPageIngestion:
            def __init__(self) -> None:
                self.urls: list[str] = []

            def ingest_resource(self, *, resource, classification, **kwargs):
                del kwargs
                self.urls.append(resource.final_url)
                return SimpleNamespace(
                    classification=classification,
                    product=(
                        SimpleNamespace(product_id="product")
                        if classification.page_type == "product_detail"
                        else None
                    ),
                )

        return RecordingPageIngestion()

    def test_general_site_scan_ingests_product_about_contact_and_blog(self) -> None:
        site = "https://example.com"
        start = f"{site}/"
        pages = {
            start: b"""
                <html><body><main><h1>Example Manufacturing</h1>
                <p>Official manufacturer of industrial components.</p></main>
                <nav><a href="/about/">About</a><a href="/contact/">Contact</a>
                <a href="/blog/launch/">News</a><a href="/product/widget/">Widget</a></nav>
                </body></html>
            """,
            f"{site}/about/": b"<html><main><h1>About us</h1><p>Founded in 1999.</p></main></html>",
            f"{site}/contact/": b"<html><main><h1>Contact</h1><p>Email sales@example.com.</p></main></html>",
            f"{site}/blog/launch/": b"""
                <html><head><script type="application/ld+json">
                {"@type":"BlogPosting"}</script></head><main>
                <h1>New factory</h1><p>Our new factory is open.</p></main></html>
            """,
            f"{site}/product/widget/": self._product_html("Widget"),
        }
        fetcher = FakeFetcher({url: (html, "text/html") for url, html in pages.items()})
        ingestion = self._page_ingestion()

        result = WordPressProductSyncService(
            fetcher=fetcher,
            page_ingestion=ingestion,
        ).sync_site(
            project_id="example.com",
            site_url=site,
            start_url=start,
            max_pages=10,
        )

        self.assertEqual(len(result.pages), 5)
        self.assertEqual(len(result.products), 1)
        self.assertEqual(
            {page.classification.page_type for page in result.pages},
            {"knowledge_page", "official_blog", "product_detail"},
        )

    def test_site_scan_runs_callback_after_each_committed_page(self) -> None:
        site = "https://example.com"
        start = f"{site}/"
        ingestion = self._page_ingestion()
        service = WordPressProductSyncService(
            fetcher=FakeFetcher(
                {
                    start: (
                        b"<html><body class='home'><main><h1>Example</h1>"
                        b"<p>Official manufacturer information.</p></main></body></html>",
                        "text/html",
                    )
                }
            ),
            page_ingestion=ingestion,
        )
        published: list[str] = []
        service.set_page_ingested_callback(
            lambda page: published.append(page.classification.canonical_url)
        )

        result = service.sync_site(
            project_id="example.com",
            site_url=site,
            start_url=start,
            max_pages=1,
        )

        self.assertEqual(published, [start])
        self.assertEqual(len(result.pages), 1)

    def test_one_pending_page_conflict_does_not_abort_the_site_scan(self) -> None:
        site = "https://example.com"
        start = f"{site}/"
        blocked = f"{site}/contact/"
        good = f"{site}/capabilities/"

        class ConflictingPageIngestion:
            def ingest_resource(self, *, resource, classification, **kwargs):
                del kwargs
                if resource.final_url == blocked:
                    raise WebPageIngestionConflict("source already has a pending snapshot")
                return SimpleNamespace(
                    classification=classification,
                    product=None,
                )

        result = WordPressProductSyncService(
            fetcher=FakeFetcher(
                {
                    start: (
                        (
                            "<html><body class='home'><main><h1>Example</h1>"
                            "<p>Official manufacturer information.</p></main>"
                            f"<a href='{blocked}'>Contact</a>"
                            f"<a href='{good}'>Capabilities</a></body></html>"
                        ).encode(),
                        "text/html",
                    ),
                    blocked: (
                        b"<html><main><h1>Contact</h1><p>Official sales contact.</p></main></html>",
                        "text/html",
                    ),
                    good: (
                        b"<html><main><h1>Capabilities</h1>"
                        b"<p>Official manufacturing capabilities.</p></main></html>",
                        "text/html",
                    ),
                }
            ),
            page_ingestion=ConflictingPageIngestion(),
        ).sync_site(
            project_id="example.com",
            site_url=site,
            start_url=start,
            max_pages=5,
        )

        self.assertEqual(len(result.pages), 2)
        self.assertIn(blocked, result.skipped_urls)
        self.assertTrue(any("pending snapshot" in warning for warning in result.warnings))

    def test_bounded_rescan_prioritizes_urls_not_seen_before(self) -> None:
        site = "https://example.com"
        old = f"{site}/about/"
        new = f"{site}/contact/"
        start = f"{site}/"
        fetcher = FakeFetcher(
            {
                f"{site}/sitemap.xml": (
                    (
                        "<urlset>"
                        f"<url><loc>{old}</loc></url>"
                        f"<url><loc>{new}</loc></url>"
                        "</urlset>"
                    ).encode(),
                    "application/xml",
                ),
                start: (
                    (
                        f"<html><main><h1>Home</h1><p>Official home.</p></main>"
                        f'<a href="{old}">About</a><a href="{new}">Contact</a></html>'
                    ).encode(),
                    "text/html",
                ),
                old: (
                    b"<html><main><h1>About</h1><p>Old page.</p></main></html>",
                    "text/html",
                ),
                new: (
                    b"<html><main><h1>Contact</h1><p>New page.</p></main></html>",
                    "text/html",
                ),
            }
        )

        result = WordPressProductSyncService(
            fetcher=fetcher,
            page_ingestion=self._page_ingestion(),
        ).sync_site(
            project_id="example.com",
            site_url=site,
            start_url=start,
            max_pages=1,
            known_urls=(start, old),
        )

        self.assertEqual(len(result.pages), 1)
        self.assertEqual(result.pages[0].classification.canonical_url, new)

    def test_bounded_scan_prioritizes_contact_page_over_large_blog_archive(self) -> None:
        site = "https://example.com"
        contact = f"{site}/contact-us/"
        blog_urls = [f"{site}/news/post-{index}/" for index in range(120)]
        sitemap = (
            "<urlset>"
            + "".join(f"<url><loc>{url}</loc></url>" for url in blog_urls)
            + f"<url><loc>{contact}</loc></url>"
            + "</urlset>"
        ).encode()
        fetcher = FakeFetcher(
            {
                f"{site}/sitemap.xml": (sitemap, "application/xml"),
                contact: (
                    b"<html><main><h1>Contact Us</h1>"
                    b"<p>Email sales@example.com.</p></main></html>",
                    "text/html",
                ),
            }
        )

        result = WordPressProductSyncService(
            fetcher=fetcher,
            page_ingestion=self._page_ingestion(),
        ).sync_site(
            project_id="example.com",
            site_url=site,
            start_url=f"{site}/",
            max_pages=1,
        )

        self.assertEqual(len(result.pages), 1)
        self.assertEqual(result.pages[0].classification.canonical_url, contact)

    def test_product_sitemap_is_complete_outside_ordinary_page_budget(self) -> None:
        site = "https://example.com"
        blogs = [f"{site}/post-{index}/" for index in range(3)]
        products = [f"{site}/product/ladder-{index}/" for index in range(2)]
        sitemap_index = (
            "<sitemapindex>"
            f"<sitemap><loc>{site}/sitemap-post.xml</loc></sitemap>"
            f"<sitemap><loc>{site}/sitemap-product-2026.xml</loc></sitemap>"
            "</sitemapindex>"
        ).encode()
        post_sitemap = (
            "<urlset>"
            + "".join(f"<url><loc>{url}</loc></url>" for url in blogs)
            + "</urlset>"
        ).encode()
        product_sitemap = (
            "<urlset>"
            + "".join(f"<url><loc>{url}</loc></url>" for url in products)
            + "</urlset>"
        ).encode()
        fetcher = FakeFetcher(
            {
                f"{site}/sitemap.xml": (sitemap_index, "application/xml"),
                f"{site}/sitemap-post.xml": (post_sitemap, "application/xml"),
                f"{site}/sitemap-product-2026.xml": (
                    product_sitemap,
                    "application/xml",
                ),
                blogs[0]: (
                    b"<html><body class='single-post'><main><h1>Post</h1>"
                    b"<p>Editorial article.</p></main></body></html>",
                    "text/html",
                ),
                **{
                    url: (self._product_html(f"Ladder {index}"), "text/html")
                    for index, url in enumerate(products)
                },
            }
        )

        result = WordPressProductSyncService(
            fetcher=fetcher,
            page_ingestion=self._page_ingestion(),
        ).sync_site(
            project_id="example.com",
            site_url=site,
            start_url=f"{site}/",
            max_pages=1,
        )

        self.assertEqual(len(result.products), 2)
        self.assertTrue(all(url in fetcher.calls for url in products))
        self.assertIn(f"{site}/", fetcher.calls)
        self.assertTrue(all(url not in fetcher.calls for url in blogs))

    def test_product_sitemap_skips_localized_product_mirrors(self) -> None:
        site = "https://example.com"
        primary = f"{site}/product/oak-flooring/"
        localized = f"{site}/de/produkt/eichenboden/"
        fetcher = FakeFetcher(
            {
                f"{site}/sitemap.xml": (
                    (
                        "<sitemapindex>"
                        f"<sitemap><loc>{site}/product-sitemap.xml</loc></sitemap>"
                        "</sitemapindex>"
                    ).encode(),
                    "application/xml",
                ),
                f"{site}/product-sitemap.xml": (
                    (
                        "<urlset>"
                        f"<url><loc>{primary}</loc></url>"
                        f"<url><loc>{localized}</loc></url>"
                        "</urlset>"
                    ).encode(),
                    "application/xml",
                ),
                primary: (self._product_html("Oak Flooring"), "text/html"),
            }
        )

        result = WordPressProductSyncService(
            fetcher=fetcher,
            page_ingestion=self._page_ingestion(),
        ).sync_site(
            project_id="example.com",
            site_url=site,
            start_url=f"{site}/",
            max_pages=1,
        )

        self.assertEqual(len(result.products), 1)
        self.assertIn(localized, result.skipped_urls)
        self.assertNotIn(localized, fetcher.calls)

    def test_explicit_single_page_scan_skips_sitewide_discovery(self) -> None:
        site = "https://example.com"
        contact = f"{site}/contact-us/"
        fetcher = FakeFetcher(
            {
                contact: (
                    b"<html><main><h1>Contact Us</h1>"
                    b"<p>Email sales@example.com for a quotation.</p></main></html>",
                    "text/html",
                ),
            }
        )

        result = WordPressProductSyncService(
            fetcher=fetcher,
            page_ingestion=self._page_ingestion(),
        ).sync_site(
            project_id="example.com",
            site_url=site,
            start_url=contact,
            max_pages=1,
        )

        self.assertEqual(len(result.pages), 1)
        self.assertEqual(result.pages[0].classification.canonical_url, contact)
        self.assertEqual(
            fetcher.calls,
            [f"{site}/robots.txt", contact],
        )
        self.assertIn("single-page scan", result.probe.reason)

    def test_general_site_scan_respects_robots_txt(self) -> None:
        site = "https://example.com"
        start = f"{site}/"
        private = f"{site}/private/"
        fetcher = FakeFetcher(
            {
                f"{site}/robots.txt": (
                    b"User-agent: ArticleAgentKnowledgeBot\nDisallow: /private/\n",
                    "text/plain",
                ),
                start: (
                    (
                        "<html><main><h1>Home</h1><p>Official home.</p></main>"
                        f'<a href="{private}">Private</a></html>'
                    ).encode(),
                    "text/html",
                ),
                private: (
                    b"<html><main><h1>Private</h1><p>Do not crawl.</p></main></html>",
                    "text/html",
                ),
            }
        )

        result = WordPressProductSyncService(
            fetcher=fetcher,
            page_ingestion=self._page_ingestion(),
        ).sync_site(
            project_id="example.com",
            site_url=site,
            start_url=start,
            max_pages=10,
        )

        self.assertEqual(len(result.pages), 1)
        self.assertIn(private, result.skipped_urls)
        self.assertNotIn(private, fetcher.calls)

    def test_wordpress_rest_continues_until_short_page(self) -> None:
        site = "https://example.com"
        category = f"{site}/products/"
        products = [f"{site}/product/p{index}/" for index in range(1, 4)]
        rest_root = json.dumps(
            {"namespaces": ["wp/v2"], "routes": {"/wp/v2/product": {}}}
        ).encode()
        category_html = b"""
        <html><body class="archive post-type-archive-product"><main>
          <h1>Products</h1><ul class="products"></ul>
        </main></body></html>
        """
        fetcher = FakeFetcher(
            {
                f"{site}/wp-json/": (rest_root, "application/json"),
                category: (category_html, "text/html"),
                f"{site}/wp-json/wp/v2/product?per_page=2&_fields=link": (
                    json.dumps([{"link": products[0]}, {"link": products[1]}]).encode(),
                    "application/json",
                ),
                f"{site}/wp-json/wp/v2/product?per_page=2&_fields=link&page=2": (
                    json.dumps([{"link": products[2]}]).encode(),
                    "application/json",
                ),
                **{
                    url: (self._product_html(f"Product {index}"), "text/html")
                    for index, url in enumerate(products, start=1)
                },
            }
        )
        page_ingestion = self._page_ingestion()
        service = WordPressProductSyncService(
            fetcher=fetcher,
            page_ingestion=page_ingestion,
        )

        result = service.sync_category(
            project_id="example.com",
            site_url=site,
            category_url=category,
            max_products=2,
        )

        self.assertEqual(len(result.products), 3)
        self.assertIn(
            f"{site}/wp-json/wp/v2/product?per_page=2&_fields=link&page=2",
            fetcher.calls,
        )

    def test_html_category_follows_numbered_page(self) -> None:
        site = "https://example.com"
        first_page = f"{site}/products/"
        second_page = f"{site}/products/page/2/"
        products = [f"{site}/product/p1/", f"{site}/product/p2/"]
        page_one_html = f"""
        <html><body class="archive post-type-archive-product"><main>
          <h1>Products</h1><ul class="products">
            <li class="product"><a href="{products[0]}">P1</a></li>
          </ul>
          <nav class="pagination"><a rel="next" href="{second_page}">Next</a></nav>
        </main></body></html>
        """.encode()
        page_two_html = f"""
        <html><body class="archive post-type-archive-product"><main>
          <h1>Products</h1><ul class="products">
            <li class="product"><a href="{products[1]}">P2</a></li>
          </ul>
        </main></body></html>
        """.encode()
        fetcher = FakeFetcher(
            {
                first_page: (page_one_html, "text/html"),
                second_page: (page_two_html, "text/html"),
                products[0]: (self._product_html("Product 1"), "text/html"),
                products[1]: (self._product_html("Product 2"), "text/html"),
            }
        )
        page_ingestion = self._page_ingestion()
        service = WordPressProductSyncService(
            fetcher=fetcher,
            page_ingestion=page_ingestion,
        )

        result = service.sync_category(
            project_id="example.com",
            site_url=site,
            category_url=first_page,
            max_products=50,
        )

        self.assertEqual(len(result.products), 2)
        self.assertIn(second_page, fetcher.calls)


if __name__ == "__main__":
    unittest.main()
