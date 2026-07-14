from __future__ import annotations

import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services import product_crawler as crawler  # noqa: E402


def jpeg_bytes(seed: int) -> bytes:
    image = Image.new("RGB", (320, 200))
    image.putdata(
        [
            (
                (index * (seed + 3)) % 256,
                (index * (seed + 7) + seed) % 256,
                (index * (seed + 11) + seed * 3) % 256,
            )
            for index in range(320 * 200)
        ]
    )
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=90)
    return output.getvalue()


class ImageResponse:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.headers = {"Content-Type": "image/jpeg"}

    def read(self, _maximum: int = -1) -> bytes:
        return self.data


def task_at(task_dir: Path) -> SimpleNamespace:
    return SimpleNamespace(
        customer="www.example.com",
        task_dir=str(task_dir),
        topic="PET bottle mold",
        selected_title="PET Bottle Mold Buying Guide",
        competitor_keyword="PET bottle mold",
    )


def verified(candidate: crawler.CrawlCandidate, _terms: list[str], _deadline: float) -> None:
    candidate.detail_verified = True
    candidate.description = "A detailed PET bottle mold for industrial production applications."


class ProductPageClassificationTests(unittest.TestCase):
    def test_tavily_is_only_used_to_discover_same_site_urls(self) -> None:
        class FakeTavily:
            ready = True

            def search(self, query: str, host: str, max_results: int = 10):
                self.call = (query, host, max_results)
                return SimpleNamespace(
                    results=(
                        SimpleNamespace(
                            title="Official Product",
                            url="https://www.example.com/products/official-product/",
                            content="Untrusted search snippet",
                            score=0.92,
                        ),
                        SimpleNamespace(
                            title="Outside Result",
                            url="https://outside.example.net/product/",
                            content="Outside",
                            score=0.99,
                        ),
                        SimpleNamespace(
                            title="Official Blog Article",
                            url="https://www.example.com/blog/pet-bottle-mold-guide/",
                            content="Blog",
                            score=0.91,
                        ),
                    )
                )

        client = FakeTavily()
        candidates, audit = crawler.candidates_from_tavily(
            client,
            "https://www.example.com",
            ["pet bottle mold", "buying guide"],
        )

        self.assertEqual(client.call[1], "www.example.com")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].source, "tavily")
        self.assertEqual(candidates[0].description, "")
        self.assertEqual(audit["status"], "ok")
        self.assertFalse(audit["results"][1]["same_site"])
        self.assertTrue(audit["results"][2]["same_site"])
        self.assertFalse(audit["results"][2]["eligible_product_url"])

    def test_article_question_becomes_a_product_oriented_tavily_query(self) -> None:
        task = SimpleNamespace(
            customer="www.qewitfastener.com",
            task_dir="unused",
            topic="How does the material of the self-tapper affect its performance in different environments?",
            selected_title="How to Choose Self-Tapper Materials for Marine, Construction, and Equipment Assembly",
            competitor_keyword="",
        )

        query = crawler.tavily_product_query(crawler.search_terms(task))

        self.assertEqual(query, "self tapping screw products")

    def test_bad_image_hints_use_token_boundaries(self) -> None:
        self.assertTrue(crawler.is_bad_image_url("https://www.example.com/assets/share.png"))
        self.assertFalse(
            crawler.is_bad_image_url("https://www.example.com/products/shared-product.jpg")
        )

    def test_filter_and_pagination_listing_is_rejected(self) -> None:
        links = "".join(
            f'<a href="/products/model-{index}/">Model {index}</a>' for index in range(1, 6)
        )
        html = f"""
        <html><head>
          <title>Professional PET Blowing Molds</title>
          <meta property="og:image" content="/category.jpg">
        </head><body>
          <h1>PET Blowing Mold</h1><p>Filter</p>{links}
          <a href="/pet-blowing-mold/page/2/">2</a>
        </body></html>
        """
        parser = crawler.parse_html(html)

        self.assertTrue(
            crawler.is_product_listing_page(
                "https://www.example.com/pet-blowing-mold/",
                parser,
                ["pet blowing mold"],
            )
        )
        self.assertFalse(
            crawler.is_product_detail_page(
                "https://www.example.com/pet-blowing-mold/",
                parser,
                ["pet blowing mold"],
            )
        )

    def test_product_schema_detail_with_related_category_elements_is_accepted(self) -> None:
        related = "".join(
            f'<a href="/products/related-{index}/">Related {index}</a>' for index in range(1, 6)
        )
        html = f"""
        <html><head>
          <title>330ml PET Blow Mould</title>
          <meta property="og:image" content="/330ml-pet-mould.jpg">
          <script type="application/ld+json">
            {{"@context":"https://schema.org","@type":"Product","name":"330ml PET Blow Mould"}}
          </script>
        </head><body class="single-product">
          <h1>330ml PET Blow Mould</h1>
          <p>This product page contains dimensions, materials, applications and inquiry details.</p>
          <div class="product-category related-products">Related Products {related}</div>
        </body></html>
        """
        parser = crawler.parse_html(html)

        self.assertFalse(
            crawler.is_product_listing_page(
                "https://www.example.com/products/330ml-pet-blow-mould/",
                parser,
                ["pet blow mould"],
            )
        )
        self.assertTrue(
            crawler.is_product_detail_page(
                "https://www.example.com/products/330ml-pet-blow-mould/",
                parser,
                ["pet blow mould"],
            )
        )

    def test_product_index_expands_listing_container_to_detail_links(self) -> None:
        index_url = "https://www.example.com/products/"
        category_url = "https://www.example.com/pet-molds/"
        pages = {
            index_url: '<a href="/pet-molds/">PET Molds</a>',
            category_url: """
                <html><body><p>Filter</p>
                  <a href="/products/model-a/">Model A</a>
                  <a href="/products/model-b/">Model B</a>
                  <a href="/products/model-c/">Model C</a>
                  <a href="/products/model-d/">Model D</a>
                  <a href="/pet-molds/page/2/">2</a>
                </body></html>
            """,
        }

        with patch.object(crawler, "fetch_text", side_effect=lambda url, timeout=5: pages.get(url, "")):
            candidates = crawler.candidates_from_product_indexes(
                "https://www.example.com",
                ["pet mold"],
                crawler.time.monotonic() + 10,
            )

        urls = [candidate.url for candidate in candidates]
        self.assertNotIn(category_url, urls)
        self.assertEqual(
            urls,
            [
                "https://www.example.com/products/model-a/",
                "https://www.example.com/products/model-b/",
                "https://www.example.com/products/model-c/",
                "https://www.example.com/products/model-d/",
            ],
        )
        self.assertTrue(all(candidate.source == "product-container" for candidate in candidates))


class OutboundRequestSafetyTests(unittest.TestCase):
    @staticmethod
    def dns_record(address: str):
        family = crawler.socket.AF_INET6 if ":" in address else crawler.socket.AF_INET
        sockaddr = (address, 443, 0, 0) if family == crawler.socket.AF_INET6 else (address, 443)
        return (family, crawler.socket.SOCK_STREAM, 6, "", sockaddr)

    def test_private_or_special_dns_answer_is_rejected_before_http(self) -> None:
        unsafe_addresses = (
            "10.20.30.40",
            "127.0.0.1",
            "169.254.10.20",
            "0.0.0.0",
            "240.0.0.1",
            "::1",
            "fe80::1234",
        )
        for address in unsafe_addresses:
            with self.subTest(address=address):
                with (
                    patch.object(
                        crawler.socket,
                        "getaddrinfo",
                        return_value=[self.dns_record(address)],
                    ),
                    patch.object(crawler.request, "build_opener") as build_opener,
                ):
                    with self.assertRaises(crawler.UnsafeOutboundURLError):
                        crawler.open_url("https://www.example.com/products/widget/")
                build_opener.assert_not_called()

    def test_mixed_public_and_private_dns_answers_are_rejected(self) -> None:
        records = [
            self.dns_record("93.184.216.34"),
            self.dns_record("::1"),
        ]
        with (
            patch.object(crawler.socket, "getaddrinfo", return_value=records),
            patch.object(crawler.request, "build_opener") as build_opener,
        ):
            with self.assertRaises(crawler.UnsafeOutboundURLError):
                crawler.open_url("https://www.example.com/products/widget/")
        build_opener.assert_not_called()

    def test_windows_proxy_fake_ip_is_allowed_only_for_a_hostname(self) -> None:
        fake_ip_record = [self.dns_record("198.18.0.102")]
        with patch.object(crawler.socket, "getaddrinfo", return_value=fake_ip_record):
            addresses = crawler._validate_outbound_url(
                "https://www.example.com/products/widget/"
            )
            self.assertEqual(str(addresses[0]), "198.18.0.102")

            with self.assertRaises(crawler.UnsafeOutboundURLError):
                crawler._validate_outbound_url("https://198.18.0.102/private")

    def test_redirect_policy_blocks_offsite_target_before_following(self) -> None:
        initial_url = "https://www.example.com/products/widget/"
        redirect_url = "https://attacker.invalid/collect"
        public_record = [self.dns_record("93.184.216.34")]

        class RedirectingOpener:
            def __init__(self, handler) -> None:
                self.handler = handler

            def open(self, req, timeout=5):
                return self.handler.redirect_request(
                    req,
                    None,
                    302,
                    "Found",
                    {},
                    redirect_url,
                )

        def build_opener(*handlers):
            redirect_handler = next(
                item for item in handlers if isinstance(item, crawler._ValidatingRedirectHandler)
            )
            return RedirectingOpener(redirect_handler)

        with (
            patch.object(crawler.socket, "getaddrinfo", return_value=public_record),
            patch.object(crawler.request, "build_opener", side_effect=build_opener),
        ):
            with self.assertRaises(crawler.UnsafeOutboundURLError):
                crawler.open_url(
                    initial_url,
                    redirect_validator=lambda target: crawler.same_site(initial_url, target),
                )

    def test_redirect_target_is_dns_checked_before_following(self) -> None:
        initial_url = "https://www.example.com/products/widget/"
        redirect_url = "https://media.example.com/private.jpg"

        def resolve(host: str, port: int, **_kwargs):
            address = "127.0.0.1" if host == "media.example.com" else "93.184.216.34"
            return [self.dns_record(address)]

        class RedirectingOpener:
            def __init__(self, handler) -> None:
                self.handler = handler

            def open(self, req, timeout=5):
                return self.handler.redirect_request(
                    req,
                    None,
                    302,
                    "Found",
                    {},
                    redirect_url,
                )

        def build_opener(*handlers):
            redirect_handler = next(
                item for item in handlers if isinstance(item, crawler._ValidatingRedirectHandler)
            )
            return RedirectingOpener(redirect_handler)

        with (
            patch.object(crawler.socket, "getaddrinfo", side_effect=resolve),
            patch.object(crawler.request, "build_opener", side_effect=build_opener),
        ):
            with self.assertRaises(crawler.UnsafeOutboundURLError):
                crawler.open_url(
                    initial_url,
                    redirect_validator=lambda target: crawler.same_site(initial_url, target),
                )


class ProductRecommendationDeduplicationTests(unittest.TestCase):
    def candidates(self, image_urls: list[str]) -> list[crawler.CrawlCandidate]:
        return [
            crawler.CrawlCandidate(
                name=f"PET Mold {index}",
                url=f"https://www.example.com/products/pet-mold-{index}/",
                image_url=image_url,
                source="test",
            )
            for index, image_url in enumerate(image_urls, start=1)
        ]

    def test_duplicate_image_url_is_skipped_and_next_candidate_fills_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task = task_at(Path(temporary))
            shared = "https://www.example.com/media/shared.jpg"
            unique = "https://www.example.com/media/unique.jpg"
            candidates = self.candidates([shared, shared + "?utm_source=test", unique])
            payloads = {
                crawler.normalize_url(shared): jpeg_bytes(1),
                crawler.normalize_url(unique): jpeg_bytes(2),
            }
            opened: list[str] = []

            def open_image(url: str, timeout: int = 10) -> ImageResponse:
                opened.append(url)
                return ImageResponse(payloads[crawler.normalize_url(url)])

            with (
                patch.object(crawler, "collect_candidates", return_value=candidates),
                patch.object(crawler, "enrich_candidate", side_effect=verified),
                patch.object(crawler, "open_url", side_effect=open_image),
            ):
                products = crawler.recommend_products(object(), task, limit=2)

            self.assertEqual({product.name for product in products}, {"PET Mold 1", "PET Mold 3"})
            self.assertEqual(len(opened), 2)
            self.assertEqual(
                {crawler.normalize_url(url) for url in opened},
                {crawler.normalize_url(shared), crawler.normalize_url(unique)},
            )

    def test_duplicate_image_bytes_are_skipped_and_limit_is_capped_at_three(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task = task_at(Path(temporary))
            image_urls = [
                f"https://www.example.com/media/image-{index}.jpg" for index in range(1, 5)
            ]
            candidates = self.candidates(image_urls)
            duplicate = jpeg_bytes(3)
            payloads = {
                image_urls[0]: duplicate,
                image_urls[1]: duplicate,
                image_urls[2]: jpeg_bytes(4),
                image_urls[3]: jpeg_bytes(5),
            }

            with (
                patch.object(crawler, "collect_candidates", return_value=candidates),
                patch.object(crawler, "enrich_candidate", side_effect=verified),
                patch.object(
                    crawler,
                    "open_url",
                    side_effect=lambda url, timeout=10: ImageResponse(payloads[url]),
                ),
            ):
                products = crawler.recommend_products(object(), task, limit=6)

            self.assertEqual(
                [product.name for product in products],
                ["PET Mold 1", "PET Mold 3", "PET Mold 4"],
            )
            self.assertEqual(len(list((Path(temporary) / "images").glob("*"))), 3)
            self.assertIn("duplicate-image-bytes", candidates[1].debug)

    def test_slow_discovery_keeps_a_separate_detail_validation_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task = task_at(Path(temporary))
            detail = crawler.CrawlCandidate(
                name="PET Bottle Mold Detail",
                url="https://www.example.com/products/pet-bottle-mold-detail/",
                description="A detailed product page for PET bottle mold buyers.",
                source="test",
            )
            clock = {"now": 100.0}
            captured: dict[str, float] = {}

            def slow_collect(
                _base_url: str,
                _terms: list[str],
                discovery_deadline: float,
            ) -> list[crawler.CrawlCandidate]:
                captured["discovery"] = discovery_deadline
                clock["now"] += 16.0
                return [detail]

            def validate_detail(
                item: crawler.CrawlCandidate,
                _terms: list[str],
                detail_deadline: float,
            ) -> None:
                captured["detail"] = detail_deadline
                verified(item, _terms, detail_deadline)

            with (
                patch.object(crawler.time, "monotonic", side_effect=lambda: clock["now"]),
                patch.object(crawler, "collect_candidates", side_effect=slow_collect),
                patch.object(crawler, "enrich_candidate", side_effect=validate_detail),
            ):
                products = crawler.recommend_products(object(), task, limit=3)

            self.assertEqual(len(products), 1)
            self.assertEqual(captured["discovery"], 112.0)
            self.assertEqual(captured["detail"], 128.0)

    def test_unverified_candidates_are_never_used_as_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task = task_at(Path(temporary))
            candidates = self.candidates(["https://www.example.com/media/image.jpg"])
            with (
                patch.object(crawler, "collect_candidates", return_value=candidates),
                patch.object(crawler, "enrich_candidate", return_value=None),
            ):
                products = crawler.recommend_products(object(), task, limit=3)

            self.assertEqual(products, [])


if __name__ == "__main__":
    unittest.main()
