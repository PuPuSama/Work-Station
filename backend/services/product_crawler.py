from __future__ import annotations

import json
import mimetypes
import re
import ssl
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib import parse, request
from urllib.error import HTTPError, URLError

from config import AppConfig
from models import Product, TaskRecord
from services.generator import primary_keyword
from storage import write_json_artifact


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
MAX_CRAWL_SECONDS = 28
DEFAULT_FETCH_TIMEOUT = 5
IMAGE_FETCH_TIMEOUT = 10
MAX_ENRICH_CANDIDATES = 14
MAX_RAW_CANDIDATES = 50
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
MIN_IMAGE_BYTES = 1500
MIN_IMAGE_WIDTH = 180
MIN_IMAGE_HEIGHT = 120
STOPWORDS = {
    "about",
    "after",
    "again",
    "against",
    "between",
    "choose",
    "choosing",
    "difference",
    "different",
    "does",
    "from",
    "guide",
    "into",
    "made",
    "make",
    "should",
    "their",
    "there",
    "these",
    "this",
    "used",
    "what",
    "when",
    "where",
    "which",
    "while",
    "with",
    "your",
}
PRODUCT_PATH_HINTS = (
    "product",
    "products",
    "solution",
    "solutions",
    "service",
    "services",
    "mould",
    "mold",
    "machine",
    "equipment",
    "attachment",
    "jewelry",
    "floor",
    "belt",
    "fastener",
)
PRODUCT_INDEX_PATHS = (
    "/products/",
    "/product/",
    "/our-products/",
    "/product-category/",
)
BAD_IMAGE_HINTS = (
    "logo",
    "icon",
    "sprite",
    "placeholder",
    "avatar",
    "banner",
    "loading",
    "search",
    "theme",
    "themes",
    "gedunew",
    "magnifier",
    "magnifying",
    "menu",
    "close",
    "arrow",
    "share",
    "favicon",
    "default",
    "blank",
    "transparent",
    "ajax-loader",
    "wechat",
    "whatsapp",
    "facebook",
    "twitter",
    "linkedin",
)


@dataclass
class CrawlCandidate:
    name: str
    url: str
    description: str = ""
    image_url: str = ""
    score: int = 0
    source: str = ""
    debug: list[str] = field(default_factory=list)


class SimpleHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.in_title = False
        self.meta: list[dict[str, str]] = []
        self.links: list[dict[str, str]] = []
        self.images: list[dict[str, str]] = []
        self.text_parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key.lower(): value or "" for key, value in attrs}
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg"}:
            self.skip_depth += 1
        if tag == "title":
            self.in_title = True
        elif tag == "meta":
            self.meta.append(attributes)
        elif tag == "a":
            self.links.append(attributes)
        elif tag == "img":
            self.images.append(attributes)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg"} and self.skip_depth:
            self.skip_depth -= 1
        if tag == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        text = normalize_space(data)
        if not text:
            return
        if self.in_title:
            self.title_parts.append(text)
        elif not self.skip_depth:
            self.text_parts.append(text)

    @property
    def title(self) -> str:
        return normalize_space(" ".join(self.title_parts))

    @property
    def text(self) -> str:
        return normalize_space(" ".join(self.text_parts))


def recommend_products(config: AppConfig, task: TaskRecord, limit: int = 3) -> list[Product]:
    base_url = site_base_url(task.customer)
    deadline = time.monotonic() + MAX_CRAWL_SECONDS
    terms = search_terms(task)
    candidates: list[CrawlCandidate] = []
    seen_urls: set[str] = set()

    for candidate in collect_candidates(base_url, terms, deadline):
        if not candidate.url or candidate.url in seen_urls:
            continue
        seen_urls.add(candidate.url)
        rough_score_candidate(candidate, terms, base_url)
        if candidate.score > 0:
            candidates.append(candidate)
        if len(candidates) >= MAX_RAW_CANDIDATES or expired(deadline):
            break

    candidates.sort(key=lambda item: item.score, reverse=True)
    enriched_candidates: list[CrawlCandidate] = []
    for candidate in candidates[:MAX_ENRICH_CANDIDATES]:
        if expired(deadline):
            break
        enrich_candidate(candidate, terms, deadline)
        score_candidate(candidate, terms, base_url)
        if candidate.score > 0:
            enriched_candidates.append(candidate)

    if enriched_candidates:
        candidates = enriched_candidates
    candidates.sort(key=lambda item: item.score, reverse=True)
    products: list[Product] = []
    fallback_products: list[Product] = []
    for candidate in candidates[: max(limit * 4, limit + 6)]:
        if len(products) >= limit:
            break
        image_path = ""
        if candidate.image_url:
            image_path = download_product_image(task, candidate)
        product = Product(
            name=candidate.name or product_name_from_url(candidate.url),
            url=candidate.url,
            image_path=image_path,
            description=candidate.description[:420],
        )
        if image_path:
            products.append(product)
        else:
            fallback_products.append(product)

    for product in fallback_products:
        if len(products) >= limit:
            break
        products.append(product)

    write_json_artifact(
        task,
        "product_assets/products_auto.json",
        [
            {
                "name": candidate.name,
                "url": candidate.url,
                "description": candidate.description,
                "image_url": candidate.image_url,
                "score": candidate.score,
                "source": candidate.source,
                "debug": candidate.debug,
            }
            for candidate in candidates[:20]
        ],
    )
    return products


def collect_candidates(base_url: str, terms: list[str], deadline: float) -> list[CrawlCandidate]:
    candidates: list[CrawlCandidate] = []
    candidates.extend(candidates_from_product_indexes(base_url, terms, deadline))
    if len(candidates) >= 10 or expired(deadline):
        return candidates

    api_index = fetch_json(urljoin(base_url, "/wp-json/"))
    routes = api_index.get("routes", {}) if isinstance(api_index, dict) else {}
    endpoint_paths = ["/wp/v2/pages", "/wp/v2/posts", "/wp/v2/search"]
    for route in routes:
        if "(?P" in route or "<" in route:
            continue
        if re.search(r"/wp/v2/(product|products|portfolio|service|services)\b", route):
            endpoint_paths.append(route)

    for term in terms[:5]:
        if expired(deadline):
            return candidates
        for endpoint in unique(endpoint_paths):
            if expired(deadline):
                return candidates
            candidates.extend(candidates_from_rest_endpoint(base_url, endpoint, term))
            if len(candidates) >= MAX_RAW_CANDIDATES:
                return candidates
        if expired(deadline):
            return candidates
        candidates.extend(candidates_from_media_search(base_url, term))
        if len(candidates) >= MAX_RAW_CANDIDATES:
            return candidates
        if len(candidates) >= 10:
            return candidates

    if len(candidates) < 5 and not expired(deadline):
        candidates.extend(candidates_from_sitemaps(base_url, terms, deadline))
    if len(candidates) < 5 and not expired(deadline):
        candidates.extend(candidates_from_homepage(base_url, terms))
    return candidates


def candidates_from_rest_endpoint(base_url: str, endpoint: str, term: str) -> list[CrawlCandidate]:
    params: dict[str, str | int] = {"search": term, "per_page": 10}
    if endpoint != "/wp/v2/search":
        params["_embed"] = "1"
    url = urljoin(base_url, "/wp-json" + endpoint) + "?" + parse.urlencode(params)
    data = fetch_json(url)
    if not isinstance(data, list):
        return []

    candidates: list[CrawlCandidate] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        item_url = rest_item_url(item)
        if not item_url:
            continue
        title = clean_html_text(rest_rendered(item.get("title")) or item.get("title") or item_url)
        description = clean_html_text(
            rest_rendered(item.get("excerpt"))
            or rest_rendered(item.get("content"))
            or item.get("subtype", "")
        )
        image_url = embedded_featured_image(item)
        candidates.append(
            CrawlCandidate(
                name=title or product_name_from_url(item_url),
                url=item_url,
                description=description,
                image_url=image_url,
                source=f"rest:{endpoint}",
                debug=[f"search={term}"],
            )
        )
    return candidates


def candidates_from_media_search(base_url: str, term: str) -> list[CrawlCandidate]:
    url = urljoin(base_url, "/wp-json/wp/v2/media")
    url += "?" + parse.urlencode({"search": term, "media_type": "image", "per_page": 10})
    data = fetch_json(url)
    if not isinstance(data, list):
        return []

    candidates: list[CrawlCandidate] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        image_url = best_media_url(item)
        if not image_url or is_bad_image_url(image_url):
            continue
        link = str(item.get("link") or base_url)
        title = clean_html_text(rest_rendered(item.get("title")) or item.get("alt_text") or product_name_from_url(image_url))
        description = clean_html_text(item.get("alt_text") or rest_rendered(item.get("caption")) or title)
        candidates.append(
            CrawlCandidate(
                name=title,
                url=link,
                description=description,
                image_url=image_url,
                source="rest:media",
                debug=[f"search={term}"],
            )
        )
    return candidates


def candidates_from_product_indexes(base_url: str, terms: list[str], deadline: float) -> list[CrawlCandidate]:
    candidates: list[CrawlCandidate] = []
    for index_path in PRODUCT_INDEX_PATHS:
        if expired(deadline):
            break
        index_url = urljoin(base_url, index_path)
        html = fetch_text(index_url)
        if not html:
            continue
        parser = parse_html(html)
        for link in parser.links:
            href = link.get("href", "")
            if not href:
                continue
            url = parse.urljoin(index_url, href)
            if not same_site(base_url, url) or not is_product_index_candidate(index_url, url, terms):
                continue
            if normalize_url(url).rstrip("/") == normalize_url(index_url).rstrip("/"):
                continue
            name = clean_html_text(link.get("title") or link.get("aria-label") or product_name_from_url(url))
            candidates.append(
                CrawlCandidate(
                    name=name,
                    url=url,
                    source="product-index",
                    debug=[index_url],
                )
            )
            if len(candidates) >= MAX_RAW_CANDIDATES:
                return candidates
    return candidates


def candidates_from_sitemaps(base_url: str, terms: list[str], deadline: float) -> list[CrawlCandidate]:
    sitemap_urls = [
        urljoin(base_url, "/sitemap.xml"),
        urljoin(base_url, "/wp-sitemap.xml"),
        urljoin(base_url, "/page-sitemap.xml"),
        urljoin(base_url, "/product-sitemap.xml"),
        urljoin(base_url, "/wp-sitemap-posts-page-1.xml"),
        urljoin(base_url, "/wp-sitemap-posts-product-1.xml"),
    ]
    candidates: list[CrawlCandidate] = []
    for sitemap_url in sitemap_urls:
        if expired(deadline):
            break
        text = fetch_text(sitemap_url)
        if not text:
            continue
        for url in sitemap_locations(text):
            if is_relevant_product_url(url, terms):
                candidates.append(
                    CrawlCandidate(
                        name=product_name_from_url(url),
                        url=url,
                        source="sitemap",
                        debug=[sitemap_url],
                    )
                )
    return candidates


def candidates_from_homepage(base_url: str, terms: list[str]) -> list[CrawlCandidate]:
    html = fetch_text(base_url)
    if not html:
        return []
    parser = parse_html(html)
    candidates: list[CrawlCandidate] = []
    for link in parser.links:
        href = link.get("href", "")
        if not href:
            continue
        url = parse.urljoin(base_url, href)
        if not same_site(base_url, url) or not is_relevant_product_url(url, terms):
            continue
        name = clean_html_text(link.get("title") or link.get("aria-label") or product_name_from_url(url))
        candidates.append(
            CrawlCandidate(
                name=name,
                url=url,
                source="homepage",
                debug=["homepage link"],
            )
        )
    return candidates


def enrich_candidate(candidate: CrawlCandidate, terms: list[str], deadline: float) -> None:
    if expired(deadline):
        return
    html = fetch_text(candidate.url)
    if not html:
        return
    parser = parse_html(html)
    title = first_meta(parser, "og:title") or first_meta(parser, "twitter:title") or parser.title
    description = first_meta(parser, "og:description") or first_meta(parser, "description")
    image_url = first_meta(parser, "og:image") or first_meta(parser, "twitter:image")

    if title:
        candidate.name = clean_title(title)
    if description:
        candidate.description = clean_html_text(description)
    elif not candidate.description:
        candidate.description = parser.text[:420]

    if image_url and is_bad_image_url(parse.urljoin(candidate.url, image_url)):
        image_url = ""
    if not image_url:
        image_url = best_html_image(candidate.url, parser, terms)
    if image_url:
        candidate.image_url = parse.urljoin(candidate.url, image_url)


def rough_score_candidate(candidate: CrawlCandidate, terms: list[str], base_url: str) -> None:
    haystack = " ".join([candidate.name, candidate.url, candidate.description]).lower()
    score = 0
    for term in terms:
        term_lower = term.lower()
        if term_lower and term_lower in haystack:
            score += 8
        for token in tokenize(term):
            if token in haystack:
                score += 2
    path = parse.urlparse(candidate.url).path.lower()
    if any(hint in path for hint in PRODUCT_PATH_HINTS):
        score += 18
    if candidate.image_url:
        score += 10
    if same_site(base_url, candidate.url):
        score += 6
    if candidate.source.startswith("rest"):
        score += 4
    if looks_like_blog(candidate.url):
        score -= 8
    candidate.score = score


def score_candidate(candidate: CrawlCandidate, terms: list[str], base_url: str) -> None:
    haystack = " ".join([candidate.name, candidate.url, candidate.description]).lower()
    score = 0
    for term in terms:
        term_lower = term.lower()
        if term_lower and term_lower in haystack:
            score += 12
        for token in tokenize(term):
            if token in haystack:
                score += 2
    path = parse.urlparse(candidate.url).path.lower()
    if any(hint in path for hint in PRODUCT_PATH_HINTS):
        score += 18
    if candidate.image_url:
        score += 12
    if same_site(base_url, candidate.url):
        score += 8
    if candidate.source.startswith("rest"):
        score += 5
    if candidate.source == "rest:media":
        score -= 8
    if any(bad in haystack for bad in BAD_IMAGE_HINTS):
        score -= 20
    if looks_like_blog(candidate.url):
        score -= 10
    candidate.score = score


def download_product_image(task: TaskRecord, candidate: CrawlCandidate) -> str:
    image_url = candidate.image_url
    if not image_url or is_bad_image_url(image_url):
        return ""
    existing_path = existing_product_image(task, candidate)
    if existing_path:
        return existing_path
    try:
        response = open_url(image_url, timeout=IMAGE_FETCH_TIMEOUT)
        data = response.read(8 * 1024 * 1024)
        content_type = response.headers.get("Content-Type", "").split(";")[0].strip()
    except (HTTPError, URLError, TimeoutError, OSError, UnicodeError):
        return ""

    if not is_valid_product_image(data, image_url, content_type):
        return ""

    extension = Path(parse.urlparse(image_url).path).suffix.lower()
    if extension not in IMAGE_EXTENSIONS:
        extension = mimetypes.guess_extension(content_type) or ".jpg"
    if extension == ".jpe":
        extension = ".jpg"
    if extension not in IMAGE_EXTENSIONS:
        extension = ".jpg"

    images_dir = Path(task.task_dir) / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    filename = safe_filename(candidate.name or product_name_from_url(candidate.url)) + extension
    output = unique_path(images_dir / filename)
    output.write_bytes(data)
    return str(output)


def existing_product_image(task: TaskRecord, candidate: CrawlCandidate) -> str:
    images_dir = Path(task.task_dir) / "images"
    if not images_dir.exists():
        return ""
    stem = safe_filename(candidate.name or product_name_from_url(candidate.url))
    for path in images_dir.iterdir():
        if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        if path.stem != stem and not path.stem.startswith(stem + "-"):
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if is_valid_product_image(data, path.name, "image/" + path.suffix.lower().lstrip(".")):
            return str(path)
    return ""


def search_terms(task: TaskRecord) -> list[str]:
    keyword = primary_keyword(task)
    fields = [
        keyword,
        task.selected_title,
        task.topic,
        task.competitor_keyword,
    ]
    terms: list[str] = []
    keyword_tokens = tokenize(keyword)
    topic_tokens = tokenize(task.topic)
    if keyword_tokens:
        terms.append(" ".join(keyword_tokens[:4]))
    if topic_tokens:
        terms.append(" ".join(topic_tokens[:4]))

    for field in fields:
        value = normalize_space(field)
        if value and not value.lower().startswith("http") and not looks_like_question(value):
            terms.append(value)

    tokens = tokenize(" ".join(fields))
    for size in (3, 2):
        for index in range(0, max(len(tokens) - size + 1, 0)):
            phrase = " ".join(tokens[index : index + size])
            if len(phrase) >= 8:
                terms.append(phrase)
    terms.extend(tokens[:8])
    return unique([term for term in terms if term])


def fetch_json(url: str, timeout: int = DEFAULT_FETCH_TIMEOUT) -> Any:
    text = fetch_text(url, timeout=timeout)
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def fetch_text(url: str, timeout: int = DEFAULT_FETCH_TIMEOUT) -> str:
    try:
        response = open_url(url, timeout=timeout)
        raw = response.read(2 * 1024 * 1024)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError):
        return ""
    content_type = response.headers.get_content_charset() or "utf-8"
    return raw.decode(content_type, errors="ignore")


def open_url(url: str, timeout: int = DEFAULT_FETCH_TIMEOUT):
    safe_url = encode_url(url)
    req = request.Request(
        safe_url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/json,image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    contexts = [None]
    if safe_url.lower().startswith("https://"):
        contexts.extend([ssl._create_unverified_context(), ssl._create_unverified_context()])
    last_error: Exception | None = None
    for context in contexts:
        try:
            if context is None:
                return request.urlopen(req, timeout=timeout)
            return request.urlopen(req, timeout=timeout, context=context)
        except (HTTPError, URLError, TimeoutError, OSError, ssl.SSLError) as error:
            last_error = error
    if last_error:
        raise last_error
    return request.urlopen(req, timeout=timeout)


def encode_url(url: str) -> str:
    parsed = parse.urlsplit(str(url or ""))
    try:
        netloc = parsed.netloc.encode("idna").decode("ascii")
    except UnicodeError:
        netloc = parsed.netloc
    path = parse.quote(parse.unquote(parsed.path), safe="/%:@")
    query = parse.quote(parse.unquote(parsed.query), safe="=&%/:;+?@,")
    fragment = parse.quote(parse.unquote(parsed.fragment), safe="=&%/:;+?@,")
    return parse.urlunsplit((parsed.scheme, netloc, path, query, fragment))


def parse_html(html: str) -> SimpleHTMLParser:
    parser = SimpleHTMLParser()
    parser.feed(html)
    return parser


def first_meta(parser: SimpleHTMLParser, name: str) -> str:
    wanted = name.lower()
    for meta in parser.meta:
        key = (meta.get("property") or meta.get("name") or "").lower()
        if key == wanted:
            return meta.get("content", "")
    return ""


def best_html_image(page_url: str, parser: SimpleHTMLParser, terms: list[str]) -> str:
    best = ""
    best_score = -999
    for image in parser.images:
        source = (
            srcset_best(image.get("data-srcset", ""))
            or srcset_best(image.get("data-lazy-srcset", ""))
            or srcset_best(image.get("srcset", ""))
            or image.get("data-large_image")
            or image.get("data-zoom-image")
            or image.get("data-original")
            or image.get("data-src")
            or image.get("data-lazy-src")
            or image.get("src")
        )
        if not source:
            continue
        alt = image.get("alt", "")
        candidate_url = parse.urljoin(page_url, source)
        if is_bad_image_url(candidate_url):
            continue
        text = f"{candidate_url} {alt}".lower()
        score = 0
        if any(term.lower() in text for term in terms):
            score += 20
        for token in tokenize(" ".join(terms)):
            if token in text:
                score += 2
        if any(bad in text for bad in BAD_IMAGE_HINTS):
            score -= 40
        width = numeric(image.get("width", "0"))
        height = numeric(image.get("height", "0"))
        if width >= 300 or height >= 300:
            score += 8
        if any(candidate_url.lower().endswith(ext) for ext in IMAGE_EXTENSIONS):
            score += 5
        if score > best_score:
            best_score = score
            best = candidate_url
    return best


def is_product_index_candidate(index_url: str, url: str, terms: list[str]) -> bool:
    parsed = parse.urlparse(url)
    path = parsed.path.lower()
    if any(part in path for part in ("/tag/", "/category/", "/author/", "/wp-content/", "/feed/")):
        return False
    if parsed.query and not any(key in parsed.query.lower() for key in ("product", "p=")):
        return False
    index_path = parse.urlparse(index_url).path.rstrip("/").lower()
    if index_path and path.rstrip("/").startswith(index_path + "/"):
        return True
    return is_relevant_product_url(url, terms)


def is_bad_image_url(url: str) -> bool:
    lower = parse.unquote(str(url or "")).lower()
    if not lower:
        return True
    path = parse.urlparse(lower).path
    if any(part in path for part in ("/wp-content/themes/", "/wp-includes/", "/assets/icons/", "/images/icons/")):
        return True
    filename = Path(path).name
    return any(hint in lower or hint in filename for hint in BAD_IMAGE_HINTS)


def is_valid_product_image(data: bytes, image_url: str, content_type: str) -> bool:
    if is_bad_image_url(image_url) or len(data) < MIN_IMAGE_BYTES:
        return False
    content_type = content_type.lower()
    if content_type and not content_type.startswith("image/"):
        return False
    dimensions = image_dimensions(data)
    if not dimensions:
        return len(data) >= 6000
    width, height = dimensions
    return width >= MIN_IMAGE_WIDTH and height >= MIN_IMAGE_HEIGHT


def image_dimensions(data: bytes) -> tuple[int, int] | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data.startswith(b"\xff\xd8"):
        return jpeg_dimensions(data)
    if data.startswith(b"RIFF") and len(data) >= 30 and data[8:12] == b"WEBP":
        return webp_dimensions(data)
    return None


def jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    index = 2
    while index + 9 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        index += 2
        while marker == 0xFF and index < len(data):
            marker = data[index]
            index += 1
        if marker in {0xD8, 0xD9}:
            continue
        if index + 2 > len(data):
            return None
        segment_length = int.from_bytes(data[index : index + 2], "big")
        if segment_length < 2 or index + segment_length > len(data):
            return None
        if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
            height = int.from_bytes(data[index + 3 : index + 5], "big")
            width = int.from_bytes(data[index + 5 : index + 7], "big")
            return width, height
        index += segment_length
    return None


def webp_dimensions(data: bytes) -> tuple[int, int] | None:
    chunk = data[12:16]
    if chunk == b"VP8X" and len(data) >= 30:
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return width, height
    if chunk == b"VP8 " and len(data) >= 30:
        width = int.from_bytes(data[26:28], "little") & 0x3FFF
        height = int.from_bytes(data[28:30], "little") & 0x3FFF
        return width, height
    if chunk == b"VP8L" and len(data) >= 25:
        bits = int.from_bytes(data[21:25], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return width, height
    return None


def embedded_featured_image(item: dict[str, Any]) -> str:
    embedded = item.get("_embedded", {})
    if not isinstance(embedded, dict):
        return ""
    featured = embedded.get("wp:featuredmedia") or embedded.get("wp:attachment")
    if not isinstance(featured, list) or not featured:
        return ""
    first = featured[0]
    if isinstance(first, dict):
        return best_media_url(first)
    return ""


def best_media_url(item: dict[str, Any]) -> str:
    details = item.get("media_details", {})
    if isinstance(details, dict):
        sizes = details.get("sizes", {})
        if isinstance(sizes, dict):
            best_url = ""
            best_area = 0
            for size in sizes.values():
                if not isinstance(size, dict):
                    continue
                url = str(size.get("source_url") or "")
                width = int(size.get("width") or 0)
                height = int(size.get("height") or 0)
                area = width * height
                if url and area > best_area:
                    best_url = url
                    best_area = area
            if best_url:
                return best_url
    return str(item.get("source_url") or "")


def rest_item_url(item: dict[str, Any]) -> str:
    url = str(item.get("link") or item.get("url") or "")
    if url:
        return url
    links = item.get("_links", {})
    if isinstance(links, dict):
        about = links.get("about") or links.get("self")
        if isinstance(about, list) and about and isinstance(about[0], dict):
            return str(about[0].get("href") or "")
    return ""


def rest_rendered(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("rendered") or "")
    if isinstance(value, str):
        return value
    return ""


def sitemap_locations(text: str) -> list[str]:
    locations: list[str] = []
    try:
        root = ET.fromstring(text.encode("utf-8"))
    except ET.ParseError:
        return re.findall(r"<loc>(.*?)</loc>", text, flags=re.IGNORECASE | re.DOTALL)
    for element in root.iter():
        if element.tag.endswith("loc") and element.text:
            locations.append(element.text.strip())
    return locations


def is_relevant_product_url(url: str, terms: list[str]) -> bool:
    parsed = parse.urlparse(url)
    path = parsed.path.lower()
    if any(part in path for part in ("/tag/", "/category/", "/author/", "/wp-content/")):
        return False
    hint_score = any(hint in path for hint in PRODUCT_PATH_HINTS)
    term_score = any(token in path for term in terms for token in tokenize(term))
    return bool(hint_score or term_score)


def site_base_url(customer: str) -> str:
    customer = customer.strip()
    if customer.startswith(("http://", "https://")):
        return customer.rstrip("/")
    return "https://" + customer.strip("/").rstrip("/")


def urljoin(base_url: str, path: str) -> str:
    return parse.urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))


def same_site(base_url: str, url: str) -> bool:
    base_host = parse.urlparse(base_url).netloc.lower().removeprefix("www.")
    host = parse.urlparse(url).netloc.lower().removeprefix("www.")
    return bool(host and (host == base_host or host.endswith("." + base_host)))


def normalize_url(url: str) -> str:
    parsed = parse.urlparse(url)
    return parse.urlunparse(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower().removeprefix("www."),
            parsed.path.rstrip("/"),
            "",
            parsed.query,
            "",
        )
    )


def product_name_from_url(url: str) -> str:
    path = parse.urlparse(url).path.strip("/")
    if not path:
        return parse.urlparse(url).netloc
    last = path.split("/")[-1] or path.split("/")[-2]
    return clean_title(last.replace("-", " ").replace("_", " "))


def clean_title(value: str) -> str:
    value = clean_html_text(value)
    value = re.sub(r"\s+[|-]\s+.*$", "", value)
    return normalize_space(value).strip(" -|")


def clean_html_text(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return normalize_space(unescape(text))


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def tokenize(value: str) -> list[str]:
    tokens = re.findall(r"[a-zA-Z0-9]+", value.lower())
    return [token for token in tokens if len(token) >= 3 and token not in STOPWORDS]


def srcset_best(value: str) -> str:
    best = ""
    best_width = 0
    for part in value.split(","):
        pieces = part.strip().split()
        if not pieces:
            continue
        width = 0
        if len(pieces) > 1 and pieces[1].endswith("w"):
            width = numeric(pieces[1][:-1])
        if not best or width > best_width:
            best = pieces[0]
            best_width = width
    return best


def numeric(value: str) -> int:
    match = re.search(r"\d+", str(value or ""))
    return int(match.group(0)) if match else 0


def looks_like_blog(url: str) -> bool:
    path = parse.urlparse(url).path.lower()
    return any(part in path for part in ("/blog/", "/news/", "/article/", "/articles/"))


def looks_like_question(value: str) -> bool:
    lower = value.strip().lower()
    return lower.endswith("?") or lower.startswith(("what ", "how ", "why ", "which ", "when ", "where "))


def safe_filename(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]+', "_", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:100] or "product"


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(2, 1000):
        candidate = path.with_name(f"{stem}-{index}{suffix}")
        if not candidate.exists():
            return candidate
    return path


def unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        key = value.lower()
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


def expired(deadline: float) -> bool:
    return time.monotonic() >= deadline
