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
    def test_tavily_latency_does_not_consume_official_site_discovery_budget(self) -> None:
        now = [0.0]
        captured: dict[str, float] = {}

        class SlowEmptyTavily:
            ready = True

            def search(self, _query: str, _host: str, max_results: int = 10):
                now[0] = 20.0
                return SimpleNamespace(results=())

        def collect(_base_url: str, _terms: list[str], deadline: float):
            captured["deadline"] = deadline
            return []

        task = SimpleNamespace(
            customer="www.jadduo.cn",
            task_dir="unused",
            topic="Stool ladder buying guide",
            selected_title="Stool ladder buying guide",
            competitor_keyword="",
        )
        with (
            patch.object(crawler.time, "monotonic", side_effect=lambda: now[0]),
            patch.object(crawler, "collect_candidates", side_effect=collect),
            patch.object(crawler, "write_json_artifact"),
        ):
            products = crawler.recommend_products(
                SimpleNamespace(),
                task,
                tavily_client=SlowEmptyTavily(),
                download_images=False,
            )

        self.assertEqual(products, [])
        self.assertEqual(captured["deadline"], 20.0 + crawler.MAX_DISCOVERY_SECONDS)

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

    def test_woodscrew_article_focuses_search_on_the_product_family(self) -> None:
        task = SimpleNamespace(
            customer="www.qewitfastener.com",
            task_dir="unused",
            topic=(
                "What are some common mistakes to avoid when selecting and using "
                "woodscrews in woodworking projects?"
            ),
            selected_title="Woodscrew Size, Material, and Thread Mistakes B2B Buyers Should Avoid",
            competitor_keyword="",
        )

        terms = crawler.search_terms(task)

        self.assertEqual(terms[0], "wood screws")
        self.assertEqual(crawler.tavily_product_query(terms), "wood screws products")

    def test_stool_ladder_article_focuses_search_on_the_product_family(self) -> None:
        task = SimpleNamespace(
            customer="www.jadduo.cn",
            task_dir="unused",
            topic="Small But Mighty: The Benefits of A Stool Ladder For Everyday Tasks",
            selected_title="Small But Mighty: The Benefits of A Stool Ladder For Everyday Tasks",
            competitor_keyword="",
        )

        terms = crawler.search_terms(task)

        self.assertEqual(terms[0], "stool ladder")
        self.assertEqual(crawler.tavily_product_query(terms), "stool ladder products")

        step_category = crawler.CrawlCandidate(
            name="Step Ladder",
            url="https://www.jadduo.cn/product-category/step-ladder/",
        )
        aluminum_category = crawler.CrawlCandidate(
            name="Aluminum",
            url="https://www.jadduo.cn/product-category/telescopic-ladder/aluminum/",
        )
        self.assertGreater(
            crawler.category_relevance_score(step_category, terms),
            crawler.category_relevance_score(aluminum_category, terms),
        )

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

    def test_single_product_template_wins_over_footer_category_signals(self) -> None:
        related = "".join(
            f'<a href="/product/related-{index}/">Related {index}</a>'
            for index in range(1, 6)
        )
        html = f"""
        <html><head><title>Steel Folding Stool Ladder - Jadduo</title></head>
        <body class="single single-product product-template-default woocommerce">
          <h1>Steel Folding Stool Ladder</h1>
          <img src="/wp-content/uploads/steel-folding-stool-ladder.webp" width="800" height="800">
          <p>This official product detail page describes the materials, folding frame,
          non-slip steps, household applications, packaging options, and purchasing
          information for professional wholesale buyers.</p>
          <footer><h2>Product Categories</h2>{related}</footer>
        </body></html>
        """
        parser = crawler.parse_html(html)
        url = "https://www.jadduo.cn/product/steel-folding-stool-ladder/"

        self.assertFalse(crawler.is_product_listing_page(url, parser, ["stool ladder"]))
        self.assertTrue(crawler.is_product_detail_page(url, parser, ["stool ladder"]))

    def test_uc_post_list_context_excludes_navigation_links(self) -> None:
        parser = crawler.parse_html(
            """
            <nav><a href="/services/">Services</a></nav>
            <div class="uc_post_list uc-items-wrapper">
              <div class="uc_post_list_box">
                <a href="/product/steel-folding-stool-ladder/">Steel Folding Stool Ladder</a>
              </div>
            </div>
            <footer><a href="/contact-us/">Contact</a></footer>
            """
        )

        links = crawler.listing_member_links(parser)

        self.assertEqual(
            [link["href"] for link in links],
            ["/product/steel-folding-stool-ladder/"],
        )

    def test_fallback_listing_members_still_exclude_navigation_and_service_pages(self) -> None:
        parser = crawler.parse_html(
            """
            <nav><a href="/services/">Services</a></nav>
            <main>
              <a href="/product/ladder-a/">Ladder A</a>
              <a href="/product/ladder-b/">Ladder B</a>
            </main>
            <footer><a href="/contact-us/">Contact</a></footer>
            """
        )

        links = crawler.listing_member_links(parser)

        self.assertEqual(
            [link["href"] for link in links],
            ["/product/ladder-a/", "/product/ladder-b/"],
        )

    def test_services_page_is_not_a_product_detail_fallback(self) -> None:
        parser = crawler.parse_html(
            """
            <html><head>
              <title>Professional Ladder Services</title>
              <meta name="description" content="Expert ladder services with business, design, inspection, and after-sales teams.">
              <meta property="og:image" content="https://www.example.com/media/team.jpg">
            </head><body><main>
              <h1>service</h1>
              <p>Our business and design teams provide professional support to industrial buyers throughout every project.</p>
              <img src="/media/team.jpg" width="620" height="340">
            </main></body></html>
            """
        )

        self.assertFalse(
            crawler.is_product_detail_page(
                "https://www.example.com/services/",
                parser,
                ["roof ladder"],
            )
        )

    def test_root_level_editorial_article_is_not_a_product_detail_fallback(self) -> None:
        parser = crawler.parse_html(
            """
            <html><head>
              <title>How to Choose a Roof Ladder</title>
              <meta property="og:type" content="article">
              <meta name="description" content="A detailed guide to choosing and using roof ladders safely for maintenance projects.">
              <meta property="og:image" content="https://www.example.com/media/roof-guide.jpg">
            </head><body><main>
              <h1>How to Choose a Roof Ladder</h1>
              <p>This detailed article compares ladder placement, access, storage, and safety considerations for buyers.</p>
            </main></body></html>
            """
        )

        self.assertFalse(
            crawler.is_product_detail_page(
                "https://www.example.com/how-to-choose-a-roof-ladder/",
                parser,
                ["roof ladder"],
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
        self.assertTrue(all(candidate.source == "product-category" for candidate in candidates))

    def test_relevant_category_excludes_global_hot_sale_products(self) -> None:
        index_url = "https://www.example.com/products/"
        nuts_url = "https://www.example.com/category/fasteners/nuts/"
        screws_url = "https://www.example.com/category/fasteners/screws/"
        woodscrews_url = (
            "https://www.example.com/category/fasteners/screws/"
            "woodscrews-dry-wall-screws/"
        )
        pages = {
            index_url: f"""
                <a href="{nuts_url}">Nuts</a>
                <a href="{screws_url}">Screws</a>
                <a href="{woodscrews_url}">Woodscrews &amp; Dry Wall Screws</a>
            """,
            woodscrews_url: """
                <html><body class="category">
                  <div class="newpro hot-sale">
                    <a href="/metric-nylon-insert-nut/">Metric Nylon Insert Nut</a>
                    <a href="/twist-drill/">Twist Drill</a>
                    <a href="/non-standard-nuts/">Non Standard Nuts</a>
                  </div>
                  <div class="productny-list"><ul class="fixed">
                    <li><div class="p-item"><a href="/drywall-screws/">Dry Wall Screws</a></div></li>
                    <li><div class="p-item"><a href="/chipboard-screws/">Chipboard Screws</a></div></li>
                    <li><div class="p-item"><a href="/coach-screws/">Coach Screws</a></div></li>
                    <li><div class="p-item"><a href="/twin-thread-woodscrews/">Twin Thread Woodscrews</a></div></li>
                  </ul></div>
                </body></html>
            """,
        }
        fetched: list[str] = []

        def fetch(url: str, timeout: int = 5) -> str:
            fetched.append(url)
            return pages.get(url, "")

        with patch.object(crawler, "fetch_text", side_effect=fetch):
            candidates = crawler.candidates_from_product_indexes(
                "https://www.example.com",
                ["wood screws", "woodscrews woodworking"],
                crawler.time.monotonic() + 10,
            )

        self.assertEqual(fetched[:2], [index_url, woodscrews_url])
        self.assertEqual(
            [candidate.name for candidate in candidates],
            ["Dry Wall Screws", "Chipboard Screws", "Coach Screws", "Twin Thread Woodscrews"],
        )
        self.assertTrue(all(candidate.source == "product-category" for candidate in candidates))
        self.assertTrue(all(candidate.category_url == woodscrews_url for candidate in candidates))
        self.assertNotIn("https://www.example.com/non-standard-nuts/", [item.url for item in candidates])


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

    def test_internal_candidate_pool_can_return_reserves_without_changing_public_cap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task = task_at(Path(temporary))
            candidates = self.candidates([""] * 5)
            with (
                patch.object(crawler, "collect_candidates", return_value=candidates),
                patch.object(crawler, "enrich_candidate", side_effect=verified),
            ):
                products = crawler.recommend_products(
                    object(),
                    task,
                    limit=3,
                    candidate_pool_limit=5,
                )

            self.assertEqual(len(products), 5)

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
