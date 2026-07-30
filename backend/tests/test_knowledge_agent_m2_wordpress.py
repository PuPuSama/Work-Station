from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from knowledge_agent.wordpress import (  # noqa: E402
    FetchedResource,
    OfficialSiteFetchError,
    UnsafeOfficialSiteUrl,
    WordPressSiteProbe,
    classify_web_page,
    discover_product_links,
    normalize_official_url,
)


class FakeFetcher:
    def __init__(self, responses: dict[str, tuple[bytes, str]]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def fetch(
        self,
        *,
        site_url: str,
        url: str,
        max_bytes: int,
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


if __name__ == "__main__":
    unittest.main()
