from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services.product_assets import (  # noqa: E402
    ProductAssetError,
    extract_product_assets,
    parse_product_page,
    product_asset_directories,
)


PRODUCT_URL = "https://shop.example.com/products/pump-900/"
PRODUCT_HTML = """
<!doctype html>
<html>
  <head>
    <link rel="canonical alternate" href="/products/pump-900/">
    <meta name="description" content="A sanitary pump for controlled liquid transfer.">
    <script type="application/ld+json">
      {
        "@context": "https://schema.org",
        "@graph": [
          {
            "@type": "Product",
            "name": "Pump 900",
            "image": [
              "/media/schema-pump.jpg",
              {
                "@type": "ImageObject",
                "contentUrl": "/media/pump-900-main.jpg",
                "name": "Pump 900 front view",
                "width": 1600,
                "height": 1200
              }
            ]
          },
          {
            "@type": "FAQPage",
            "mainEntity": [{
              "@type": "Question",
              "name": "Can the pump handle warm liquids?",
              "acceptedAnswer": {
                "@type": "Answer",
                "text": "Yes, within the published 80 C operating limit."
              }
            }]
          }
        ]
      }
    </script>
  </head>
  <body>
    <header><img src="/media/header-logo.png" alt="Company logo"></header>
    <nav><img src="/media/search-icon.png" alt="Search"></nav>
    <main id="product-detail">
      <h1>Sanitary Pump 900</h1>
      <div class="woocommerce-product-gallery">
        <figure>
          <a href="/media/pump-900-main.jpg">
            <img src="/media/pump-thumb.jpg" data-large_image="/media/pump-900-main.jpg"
                 alt="Pump 900 front" title="Front view" width="1600" height="1200">
          </a>
          <figcaption>Front connection layout</figcaption>
        </figure>
        <img src="/media/pump-side-small.jpg"
             srcset="/media/pump-side-small.jpg 320w, /media/pump-side-large.webp 1400w"
             alt="Pump 900 side view">
        <a href="/products/pump-800/">
          <img src="/media/pump-800.jpg" alt="A different product">
        </a>
      </div>

      <section class="product-description">
        <p>The 316L stainless-steel housing supports hygienic production lines.</p>
        <ul><li>Maximum flow is 90 litres per minute.</li></ul>
        <figure class="application-photo">
          <img data-src="/media/pump-installed.jpg" alt="Pump installed on a filling line"
               title="Installed pump">
          <figcaption>Installed beside the filling station</figcaption>
        </figure>
        <a href="/products/pump-900/">
          <img src="/media/pump-closeup.jpg" alt="Pump 900 close-up">
        </a>
        <a href="https://other.example.net/products/competitor-pump/">
          <img src="https://other.example.net/media/competitor.jpg" alt="Other product">
        </a>
      </section>

      <table class="product-specifications">
        <caption>Operating specifications</caption>
        <thead><tr><th>Property</th><th>Value</th></tr></thead>
        <tbody>
          <tr><th>Material</th><td>316L stainless steel</td></tr>
          <tr><th>Maximum temperature</th><td>80 C</td></tr>
        </tbody>
      </table>
      <dl class="technical-attributes">
        <dt>Connection</dt><dd>Tri-clamp</dd>
      </dl>

      <section class="faq-list">
        <details>
          <summary>How is the pump cleaned?</summary>
          <p>It supports clean-in-place procedures.</p>
        </details>
      </section>

      <section class="related-products">
        <p>This related card must not become a product fact.</p>
        <img src="/media/related-pump.jpg" alt="Related pump">
      </section>
    </main>
    <aside><img src="/media/sidebar-promo.jpg" alt="Promotion"></aside>
    <footer><img src="/media/footer-logo.jpg" alt="Footer"></footer>
  </body>
</html>
"""


class ProductAssetParsingTests(unittest.TestCase):
    def test_extracts_product_facts_structured_content_and_dom_aware_images(self) -> None:
        parsed = parse_product_page(PRODUCT_URL, PRODUCT_HTML, "pump-900")

        self.assertEqual(parsed.canonical_url, PRODUCT_URL)
        self.assertEqual(parsed.h1, "Sanitary Pump 900")
        self.assertEqual(
            parsed.meta_description,
            "A sanitary pump for controlled liquid transfer.",
        )
        self.assertEqual(
            parsed.main_content_facts,
            [
                "The 316L stainless-steel housing supports hygienic production lines.",
                "Maximum flow is 90 litres per minute.",
            ],
        )
        self.assertEqual(parsed.specification_tables[0]["caption"], "Operating specifications")
        self.assertEqual(parsed.specification_tables[0]["headers"], ["Property", "Value"])
        self.assertIn(["Material", "316L stainless steel"], parsed.specification_tables[0]["rows"])
        self.assertEqual(parsed.specification_tables[1]["rows"], [["Connection", "Tri-clamp"]])
        self.assertEqual(
            [item["question"] for item in parsed.faq],
            ["How is the pump cleaned?", "Can the pump handle warm liquids?"],
        )

        self.assertEqual(
            [asset.source_url for asset in parsed.json_ld_product_images],
            [
                "https://shop.example.com/media/schema-pump.jpg",
                "https://shop.example.com/media/pump-900-main.jpg",
            ],
        )
        self.assertEqual(
            [asset.source_url for asset in parsed.main_gallery],
            [
                "https://shop.example.com/media/pump-900-main.jpg",
                "https://shop.example.com/media/pump-side-large.webp",
            ],
        )
        self.assertEqual(
            [asset.source_url for asset in parsed.body_images],
            [
                "https://shop.example.com/media/pump-installed.jpg",
                "https://shop.example.com/media/pump-closeup.jpg",
            ],
        )
        all_urls = {
            asset.source_url
            for assets in parsed.image_sources.values()
            for asset in assets
        }
        for excluded in (
            "header-logo.png",
            "search-icon.png",
            "pump-800.jpg",
            "competitor.jpg",
            "related-pump.jpg",
            "sidebar-promo.jpg",
            "footer-logo.jpg",
        ):
            self.assertFalse(any(excluded in url for url in all_urls), excluded)

    def test_common_related_product_blocks_do_not_leak_facts_or_images(self) -> None:
        blocked_contexts = [
            "recently-viewed products",
            "widget_recent_products",
            "similar-products",
            "featured_products",
            "product-category",
            "also-bought",
            "frequently-bought-together",
            "other-products",
            "more_products",
            "cross-sells",
            "upsells",
        ]
        blocked_html = "".join(
            f"""
            <section class="{context}">
              <p>Leaked related fact {index}.</p>
              <img src="/media/leaked-{index}.jpg" alt="Related item {index}">
            </section>
            """
            for index, context in enumerate(blocked_contexts, start=1)
        )
        html = f"""
        <html><head><link rel="canonical" href="{PRODUCT_URL}"></head><body>
          <main>
            <h1>Sanitary Pump 900</h1>
            <p>The primary product uses a 316L housing.</p>
            <img src="/media/current-product.jpg" alt="Current product">
            {blocked_html}
          </main>
        </body></html>
        """

        parsed = parse_product_page(PRODUCT_URL, html, "pump-900")

        self.assertEqual(
            parsed.main_content_facts,
            ["The primary product uses a 316L housing."],
        )
        self.assertEqual(
            [asset.source_url for asset in parsed.body_images],
            ["https://shop.example.com/media/current-product.jpg"],
        )

    def test_empty_main_falls_back_to_sibling_product_detail_with_lazy_images(self) -> None:
        html = """
        <html><head><link rel="canonical" href="/self-tapping-screw/"></head><body>
          <main id="theme-shell"></main>
          <div class="content-product-detail fixed">
            <div class="swiper gallery-top">
              <img src="data:image/svg+xml,placeholder"
                   data-lazy-src="/wp-content/uploads/3-37.jpg">
            </div>
            <h1>Stainless Steel Self Tapping Screw</h1>
            <p>The screw cuts its own thread in sheet metal and plastic.</p>
            <div class="document">
              <img src="data:image/svg+xml,placeholder"
                   data-lazy-srcset="/wp-content/uploads/diagram-small.jpg 300w,
                                     /wp-content/uploads/diagram-large.jpg 800w">
            </div>
            <section class="hot-sale">
              <p>A featured product must not become a current-product fact.</p>
              <img data-lazy-src="/wp-content/uploads/hot-sale.jpg">
            </section>
            <section class="related-products">
              <img data-lazy-src="/wp-content/uploads/related.jpg">
            </section>
          </div>
        </body></html>
        """

        parsed = parse_product_page(
            "https://www.qewitfastener.com/self-tapping-screw/",
            html,
            "self-tapping-screw",
        )

        self.assertEqual(parsed.h1, "Stainless Steel Self Tapping Screw")
        self.assertEqual(
            parsed.main_content_facts,
            ["The screw cuts its own thread in sheet metal and plastic."],
        )
        self.assertEqual(
            [asset.source_url for asset in parsed.main_gallery],
            ["https://www.qewitfastener.com/wp-content/uploads/3-37.jpg"],
        )
        self.assertEqual(
            [asset.source_url for asset in parsed.body_images],
            ["https://www.qewitfastener.com/wp-content/uploads/diagram-large.jpg"],
        )

    def test_multiple_json_ld_products_only_contribute_images_when_bound_to_current_page(self) -> None:
        html = """
        <html>
          <head>
            <link rel="canonical" href="/products/pump-900/">
            <script type="application/ld+json">
            {
              "@context": "https://schema.org",
              "@graph": [
                {
                  "@type": "Product",
                  "name": "Acme Sanitary Pump 800",
                  "url": "/products/pump-800/",
                  "image": "/media/related-800.jpg"
                },
                {
                  "@type": "Product",
                  "name": "Acme Sanitary Pump 900",
                  "image": "/media/current-by-name.jpg"
                },
                {
                  "@type": "Product",
                  "name": "Replacement Seal Kit",
                  "@id": "https://shop.example.com/products/pump-900/#product",
                  "image": "/media/current-by-id.jpg"
                },
                {
                  "@type": "Product",
                  "name": "Featured Pump 700",
                  "mainEntityOfPage": {"@id": "/products/pump-700/"},
                  "image": "/media/featured-700.jpg"
                }
              ]
            }
            </script>
          </head>
          <body><main>
            <h1>Acme Sanitary Pump 900</h1>
            <p>Current product details.</p>
          </main></body>
        </html>
        """

        parsed = parse_product_page(PRODUCT_URL, html, "pump-900")

        self.assertEqual(
            [asset.source_url for asset in parsed.json_ld_product_images],
            [
                "https://shop.example.com/media/current-by-name.jpg",
                "https://shop.example.com/media/current-by-id.jpg",
            ],
        )

    def test_manifest_has_stable_candidates_metadata_and_directory_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = extract_product_assets(
                PRODUCT_URL,
                PRODUCT_HTML,
                temporary,
                "pump-900",
            )
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))

            self.assertEqual(result.manifest_path, Path(temporary) / "product_assets" / "pump-900" / "manifest.json")
            self.assertEqual(manifest["schema_version"], 1)
            self.assertEqual(manifest["product_id"], "pump-900")
            self.assertEqual(
                manifest["directory_contract"],
                {
                    "path_base": "task_dir",
                    "product_dir": "product_assets/pump-900",
                    "images_dir": "product_assets/pump-900/images",
                    "manifest_path": "product_assets/pump-900/manifest.json",
                    "download_policy": "injectable_downloader_only",
                },
            )
            candidates = manifest["download_candidates"]
            self.assertEqual([item["id"] for item in candidates], ["A01", "A02", "A03", "A04", "A05"])
            shared = next(
                item for item in candidates if item["source_url"].endswith("pump-900-main.jpg")
            )
            self.assertEqual(
                shared["source_kinds"],
                ["json_ld_product_image", "main_gallery"],
            )
            self.assertEqual(shared["alt"], "Pump 900 front")
            self.assertEqual(shared["caption"], "Front connection layout")
            self.assertTrue(shared["dom_context"]["xpath"])
            for field in (
                "source_url",
                "alt",
                "title",
                "caption",
                "source_kind",
                "dom_context",
                "width",
                "height",
                "sha256",
                "byte_size",
                "content_type",
                "local_path",
            ):
                self.assertIn(field, shared)
            self.assertIsNone(shared["sha256"])
            self.assertIsNone(shared["local_path"])


class ProductAssetDownloadTests(unittest.TestCase):
    def test_only_an_injected_downloader_materializes_candidates_and_updates_manifest(self) -> None:
        calls: list[str] = []

        def fake_downloader(source_url: str) -> dict[str, object]:
            calls.append(source_url)
            return {
                "content": f"downloaded:{source_url}".encode(),
                "content_type": "image/webp",
                "width": 2048,
                "height": 1365,
            }

        with tempfile.TemporaryDirectory() as temporary:
            result = extract_product_assets(
                PRODUCT_URL,
                PRODUCT_HTML,
                temporary,
                "pump-900",
                downloader=fake_downloader,
            )

            self.assertEqual(calls, [item["source_url"] for item in result.download_candidates])
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            for item in manifest["download_candidates"]:
                local_path = Path(temporary) / Path(item["local_path"])
                expected = f"downloaded:{item['source_url']}".encode()
                self.assertEqual(local_path.read_bytes(), expected)
                self.assertEqual(item["sha256"], hashlib.sha256(expected).hexdigest())
                self.assertEqual(item["byte_size"], len(expected))
                self.assertEqual((item["width"], item["height"]), (2048, 1365))

    def test_directory_contract_cannot_escape_through_product_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directories = product_asset_directories(temporary, "../Pump 900")
            self.assertEqual(directories.product_id, "Pump-900")
            self.assertEqual(directories.product_dir.parent, Path(temporary).resolve() / "product_assets")
            with self.assertRaises(ProductAssetError):
                product_asset_directories(temporary, "../..")


if __name__ == "__main__":
    unittest.main()
