from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html import unescape
from types import MappingProxyType
from typing import Literal, Mapping, Protocol, runtime_checkable
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from lxml import etree
from lxml import html as lxml_html

from services.product_assets import ParsedProductPage, ProductAssetError, parse_product_page
from services.product_crawler import (
    is_product_detail_page as crawler_is_product_detail_page,
    is_product_listing_page as crawler_is_product_listing_page,
    open_url,
    parse_html as crawler_parse_html,
)


WebPageType = Literal[
    "product_detail",
    "product_category",
    "official_blog",
    "knowledge_page",
    "unknown",
]

MAX_WEB_RESOURCE_BYTES = 8 * 1024 * 1024
WORDPRESS_PROBE_VERSION = "wordpress-site-probe/1"
WEB_PAGE_PARSER_VERSION = "official-web-page/3"
_NON_HTML_SUFFIXES = frozenset(
    {
        ".7z",
        ".avi",
        ".css",
        ".csv",
        ".doc",
        ".docx",
        ".gif",
        ".gz",
        ".ico",
        ".jpeg",
        ".jpg",
        ".js",
        ".json",
        ".mov",
        ".mp3",
        ".mp4",
        ".pdf",
        ".png",
        ".ppt",
        ".pptx",
        ".rar",
        ".rss",
        ".svg",
        ".tar",
        ".txt",
        ".webm",
        ".webp",
        ".xls",
        ".xlsx",
        ".xml",
        ".zip",
    }
)
_LOW_VALUE_PATH_MARKERS = (
    "/cart",
    "/checkout",
    "/my-account",
    "/login",
    "/register",
    "/search",
    "/feed",
    "/author/",
    "/tag/",
    "/wp-admin",
    "/wp-login",
)
_NON_PRODUCT_CONTENT_TERMS = frozenset(
    {
        "about",
        "contact",
        "contacto",
        "contactos",
        "contactez",
        "contacter",
        "contato",
        "kontakt",
        "kontaktieren",
        "lianxi",
        "nous",
        "ueber",
    }
)
_NON_PRODUCT_CONTENT_PHRASES = (
    "about us",
    "contact us",
    "get in touch",
    "kontaktieren sie uns",
    "nous contacter",
    "contactez nous",
    "pongase en contacto",
    "póngase en contacto",
    "uber uns",
    "über uns",
    "联系我们",
)

_FIXED_NON_PRODUCT_SLUGS = frozenset(
    {
        "home-2",
        "home-3",
        "homepage",
        "oem-odm",
        "politica-de-privacidad",
        "politica-sulla-privacy",
        "politique-de-confidentialite",
        "privacy-policy",
        "privacy-policy-2",
        "datenschutz",
        "datenschutzerklarung",
        "thank-you",
        "thanks",
    }
)


def _looks_like_non_product_content_page(
    *,
    path: str,
    title: str,
    heading: str,
    body_classes: set[str],
) -> bool:
    decoded_path = unquote(path).casefold()
    path_segments = tuple(
        segment for segment in decoded_path.strip("/").split("/") if segment
    )
    if path_segments and path_segments[-1] in _FIXED_NON_PRODUCT_SLUGS:
        return True
    normalized = re.sub(
        r"[^\w\u4e00-\u9fff]+",
        " ",
        f"{decoded_path} {title.casefold()} {heading.casefold()}",
    ).strip()
    tokens = set(normalized.split())
    if tokens & _NON_PRODUCT_CONTENT_TERMS:
        return True
    if any(phrase in normalized for phrase in _NON_PRODUCT_CONTENT_PHRASES):
        return True
    return bool(
        body_classes
        & {
            "page-template-contact",
            "page-template-contact-page",
            "page-template-template-contact",
        }
    )


class WordPressIngestionError(RuntimeError):
    """Base error for official-site probing and deterministic HTML parsing."""


class UnsafeOfficialSiteUrl(WordPressIngestionError):
    """Raised when a requested URL is outside the project official site."""


class OfficialSiteFetchError(WordPressIngestionError):
    """Raised when an official-site resource cannot be fetched safely."""


def _required_text(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} is required")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized


def _normalized_host(url: str) -> str:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").rstrip(".").lower().encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        return ""
    return host.removeprefix("www.")


def same_official_site(site_url: str, candidate_url: str) -> bool:
    """Allow the official host and its subdomains, ignoring a leading ``www``."""

    site_host = _normalized_host(site_url)
    candidate_host = _normalized_host(candidate_url)
    return bool(
        site_host
        and candidate_host
        and (
            candidate_host == site_host
            or candidate_host.endswith(f".{site_host}")
        )
    )


def normalize_site_url(value: str) -> str:
    """Normalize a hostname or HTTP(S) URL to a credential-free site origin."""

    raw = _required_text(value, "site_url")
    if "://" not in raw:
        raw = f"https://{raw}"
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise UnsafeOfficialSiteUrl("site_url must be a valid HTTP(S) URL") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port is not None
        and port not in {80, 443}
    ):
        raise UnsafeOfficialSiteUrl(
            "site_url must be a credential-free HTTP(S) site URL"
        )
    host = parsed.hostname.rstrip(".").lower()
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise UnsafeOfficialSiteUrl("site_url hostname is invalid") from exc
    netloc = host
    if port is not None:
        netloc = f"{host}:{port}"
    return urlunsplit((parsed.scheme.lower(), netloc, "", "", ""))


def normalize_official_url(site_url: str, value: str) -> str:
    """Resolve a URL and prove it remains inside the requested official site."""

    site = normalize_site_url(site_url)
    raw = _required_text(value, "url")
    resolved = urljoin(f"{site}/", raw)
    try:
        parsed = urlsplit(resolved)
        port = parsed.port
    except ValueError as exc:
        raise UnsafeOfficialSiteUrl("url must be a valid HTTP(S) URL") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        and port not in {80, 443}
        or not same_official_site(site, resolved)
    ):
        raise UnsafeOfficialSiteUrl("url must belong to the project official site")
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            path,
            parsed.query,
            "",
        )
    )


@dataclass(frozen=True, slots=True)
class FetchedResource:
    """One bounded network response returned by an injected fetch adapter."""

    requested_url: str
    final_url: str
    content: bytes = field(repr=False)
    content_type: str

    def __post_init__(self) -> None:
        for field_name in ("requested_url", "final_url", "content_type"):
            object.__setattr__(
                self,
                field_name,
                _required_text(getattr(self, field_name), field_name),
            )
        if not isinstance(self.content, bytes) or not self.content:
            raise ValueError("content must be non-empty bytes")

    @property
    def text(self) -> str:
        charset = "utf-8"
        match = re.search(r"charset=([A-Za-z0-9._-]+)", self.content_type)
        if match:
            charset = match.group(1)
        return self.content.decode(charset, errors="replace")


@runtime_checkable
class OfficialSiteFetcher(Protocol):
    """Network boundary used by site probing and product synchronization."""

    def fetch(
        self,
        *,
        site_url: str,
        url: str,
        max_bytes: int = MAX_WEB_RESOURCE_BYTES,
    ) -> FetchedResource: ...


class SafeOfficialSiteFetcher:
    """Production adapter reusing the application's DNS and redirect safeguards."""

    def __init__(self, *, timeout_seconds: int = 10) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._timeout_seconds = timeout_seconds

    def fetch(
        self,
        *,
        site_url: str,
        url: str,
        max_bytes: int = MAX_WEB_RESOURCE_BYTES,
    ) -> FetchedResource:
        site = normalize_site_url(site_url)
        target = normalize_official_url(site, url)
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        try:
            response = open_url(
                target,
                timeout=self._timeout_seconds,
                redirect_validator=lambda redirect: same_official_site(site, redirect),
            )
            content = response.read(max_bytes + 1)
            final_url = str(response.geturl() or target)
            if not same_official_site(site, final_url):
                raise UnsafeOfficialSiteUrl(
                    "redirect left the project official site"
                )
            if len(content) > max_bytes:
                raise OfficialSiteFetchError("official-site resource exceeds size limit")
            content_type = str(
                response.headers.get("Content-Type") or "application/octet-stream"
            ).lower()
        except WordPressIngestionError:
            raise
        except Exception as exc:
            raise OfficialSiteFetchError(
                "official-site resource could not be fetched"
            ) from exc
        return FetchedResource(
            requested_url=target,
            final_url=normalize_official_url(site, final_url),
            content=content,
            content_type=content_type,
        )


@dataclass(frozen=True, slots=True)
class WordPressProbeResult:
    site_url: str
    detected: bool
    rest_api_url: str | None
    namespaces: tuple[str, ...]
    route_count: int
    reason: str
    probe_version: str = WORDPRESS_PROBE_VERSION


class WordPressSiteProbe:
    """Detect WordPress through its public REST index without mutating storage."""

    def __init__(self, fetcher: OfficialSiteFetcher) -> None:
        self._fetcher = fetcher

    def probe(self, site_url: str) -> WordPressProbeResult:
        site = normalize_site_url(site_url)
        candidates = (
            f"{site}/wp-json/",
            f"{site}/?rest_route=/",
        )
        failures: list[str] = []
        for candidate in candidates:
            try:
                resource = self._fetcher.fetch(
                    site_url=site,
                    url=candidate,
                    max_bytes=2 * 1024 * 1024,
                )
                payload = json.loads(resource.text)
            except (OfficialSiteFetchError, UnsafeOfficialSiteUrl, json.JSONDecodeError):
                failures.append(candidate)
                continue
            if not isinstance(payload, Mapping):
                failures.append(candidate)
                continue
            raw_namespaces = payload.get("namespaces")
            namespaces = tuple(
                sorted(
                    {
                        str(value).strip()
                        for value in (
                            raw_namespaces
                            if isinstance(raw_namespaces, list)
                            else ()
                        )
                        if str(value).strip()
                    }
                )
            )
            raw_routes = payload.get("routes")
            routes = raw_routes if isinstance(raw_routes, Mapping) else {}
            wordpress_signal = (
                any(namespace == "wp/v2" for namespace in namespaces)
                or any(str(route).startswith("/wp/v2/") for route in routes)
            )
            if wordpress_signal:
                return WordPressProbeResult(
                    site_url=site,
                    detected=True,
                    rest_api_url=resource.final_url,
                    namespaces=namespaces,
                    route_count=len(routes),
                    reason=(
                        "WordPress REST index exposes the wp/v2 namespace or routes."
                    ),
                )
            failures.append(candidate)
        return WordPressProbeResult(
            site_url=site,
            detected=False,
            rest_api_url=None,
            namespaces=(),
            route_count=0,
            reason=(
                "No WordPress wp/v2 REST index was found at /wp-json/ or "
                "?rest_route=/."
            ),
        )


@dataclass(frozen=True, slots=True)
class ClassifiedWebPage:
    """Deterministic page classification plus the evidence used to reach it."""

    requested_url: str
    canonical_url: str
    page_type: WebPageType
    confidence: float
    reasons: tuple[str, ...]
    title: str
    heading: str
    breadcrumbs: tuple[str, ...]
    text_blocks: tuple[str, ...]
    product_page: ParsedProductPage | None = field(default=None, repr=False)
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


def _clean_text(value: object) -> str:
    return re.sub(r"\s+", " ", unescape(str(value or ""))).strip()


def _schema_types(document: etree._Element) -> set[str]:
    types: set[str] = set()
    for node in document.xpath(
        "//script[translate(@type, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
        "'abcdefghijklmnopqrstuvwxyz')='application/ld+json']"
    ):
        try:
            payload = json.loads(node.text or "")
        except json.JSONDecodeError:
            continue
        stack = [payload]
        while stack:
            value = stack.pop()
            if isinstance(value, Mapping):
                raw_type = value.get("@type")
                if isinstance(raw_type, str):
                    types.add(raw_type.casefold())
                elif isinstance(raw_type, list):
                    types.update(str(item).casefold() for item in raw_type)
                stack.extend(value.values())
            elif isinstance(value, list):
                stack.extend(value)
    return types


def _canonical_url(document: etree._Element, requested_url: str) -> str:
    links = document.xpath(
        "//link[contains(concat(' ', translate(@rel, "
        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), ' '), "
        "' canonical ')]/@href"
    )
    if links:
        candidate = urljoin(requested_url, str(links[0]).strip())
        if same_official_site(requested_url, candidate):
            return candidate.split("#", 1)[0]
    return requested_url.split("#", 1)[0]


def _breadcrumb_labels(document: etree._Element, heading: str) -> tuple[str, ...]:
    nodes = document.xpath(
        "//*[contains(translate(@class, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
        "'abcdefghijklmnopqrstuvwxyz'), 'breadcrumb')]"
    )
    if not nodes:
        return ()
    values: list[str] = []
    for value in nodes[0].xpath(".//a//text() | .//span//text() | .//li//text()"):
        label = _clean_text(value)
        if (
            not label
            or label.casefold() in {"home", "homepage"}
            or heading
            and label.casefold() == heading.casefold()
            or values
            and values[-1].casefold() == label.casefold()
        ):
            continue
        values.append(label)
    return tuple(values[:12])


def _text_blocks(document: etree._Element) -> tuple[str, ...]:
    main_nodes = document.xpath("//main | //*[@role='main']")
    body_nodes = document.xpath("//body")
    content_xpath = ".//h1 | .//h2 | .//h3 | .//p | .//li | .//th | .//td"
    usable_main_nodes = [
        node
        for node in main_nodes
        if len(node.xpath(content_xpath)) >= 2
    ]
    root = (
        max(usable_main_nodes, key=lambda node: len(node.xpath(content_xpath)))
        if usable_main_nodes
        else body_nodes[0]
        if body_nodes
        else document
    )
    values: list[str] = []
    seen: set[str] = set()
    for node in root.xpath(content_xpath):
        if node.xpath("ancestor::nav | ancestor::header | ancestor::footer | ancestor::aside"):
            continue
        value = _clean_text(node.text_content())
        key = value.casefold()
        if len(value) < 3 or key in seen:
            continue
        seen.add(key)
        values.append(value[:4000])
        if len(values) >= 160:
            break
    return tuple(values)


def classify_web_page(
    *,
    requested_url: str,
    html: str | bytes,
) -> ClassifiedWebPage:
    """Classify already-fetched HTML and retain rule-level evidence."""

    if not html:
        raise WordPressIngestionError("html is required")
    source = html if isinstance(html, bytes) else html.encode("utf-8")
    parser = lxml_html.HTMLParser(encoding="utf-8", recover=True)
    try:
        document = lxml_html.fromstring(source, parser=parser, base_url=requested_url)
    except (ValueError, etree.ParserError) as exc:
        raise WordPressIngestionError("official page HTML could not be parsed") from exc

    canonical = _canonical_url(document, requested_url)
    title_values = document.xpath("//title/text()")
    heading_values = document.xpath("//h1[1]//text()")
    title = _clean_text(title_values[0]) if title_values else ""
    heading = _clean_text(" ".join(str(value) for value in heading_values))
    schema_types = _schema_types(document)
    body_classes = {
        token.casefold()
        for value in document.xpath("//body/@class")
        for token in str(value).split()
    }
    path = urlsplit(canonical).path.casefold()
    crawler_terms = list(
        dict.fromkeys(
            token
            for token in re.findall(r"[a-z0-9]+", f"{path} {title.casefold()}")
            if len(token) >= 3
        )
    )[:16]
    crawler_document = crawler_parse_html(source.decode("utf-8", errors="replace"))
    crawler_detail = crawler_is_product_detail_page(
        canonical,
        crawler_document,
        crawler_terms,
    )
    crawler_listing = crawler_is_product_listing_page(
        canonical,
        crawler_document,
        crawler_terms,
    )
    product_schema = "product" in schema_types
    article_schema = bool(schema_types & {"article", "blogposting", "newsarticle"})
    woo_detail = (
        {"single", "product"} <= body_classes
        or "single-product" in body_classes
    )
    add_to_cart = bool(
        document.xpath(
            "//*[contains(translate(@class, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
            "'abcdefghijklmnopqrstuvwxyz'), 'add_to_cart') or "
            "contains(translate(@name, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
            "'abcdefghijklmnopqrstuvwxyz'), 'add-to-cart')]"
        )
    )
    product_listing = bool(
        document.xpath(
            "//*[contains(concat(' ', translate(@class, "
            "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), ' '), "
            "' products ')]"
        )
    )
    product_category_signal = (
        "tax-product_cat" in " ".join(document.xpath("//body/@class")).casefold()
        or "/product-category/" in path
    )
    blog_path = any(marker in path for marker in ("/blog/", "/news/", "/article/"))
    editorial_page = (
        article_schema
        or blog_path
        or "single-post" in body_classes
    )
    home_page = not path.strip("/") or "home" in body_classes
    non_product_content_page = _looks_like_non_product_content_page(
        path=path,
        title=title,
        heading=heading,
        body_classes=body_classes,
    )
    blocks = _text_blocks(document)
    page_type: WebPageType
    confidence: float
    reasons: list[str]

    if editorial_page and not woo_detail:
        page_type = "official_blog"
        confidence = 0.88 if article_schema else 0.72
        reasons = [
            (
                "schema.org Article/BlogPosting data is present"
                if article_schema
                else "URL or WordPress body class identifies an editorial post"
            )
        ]
    elif non_product_content_page:
        page_type = "knowledge_page"
        confidence = 0.9
        reasons = ["URL, title, or page template identifies a non-product company page"]
    elif home_page:
        if product_listing or crawler_listing:
            page_type = "product_category"
            confidence = 0.72
            reasons = ["the homepage contains a product-listing container"]
        elif blocks:
            page_type = "knowledge_page"
            confidence = 0.68
            reasons = ["the canonical homepage is company knowledge, not a product detail"]
        else:
            page_type = "unknown"
            confidence = 0.2
            reasons = ["the homepage did not expose substantive content"]
    elif product_category_signal or (
        (product_listing or crawler_listing)
        and not woo_detail
        and not add_to_cart
    ):
        page_type = "product_category"
        confidence = 0.9 if product_category_signal else 0.84
        reasons = [
            (
                "the page URL or template identifies the page itself as a product category"
                if product_category_signal
                else "a product-listing container identifies the page itself as a listing"
            )
        ]
    elif product_schema or woo_detail or add_to_cart:
        page_type = "product_detail"
        reasons = []
        confidence = 0.72
        if product_schema:
            reasons.append("schema.org Product data is present")
            confidence += 0.18
        if woo_detail:
            reasons.append("WooCommerce single-product markup is present")
            confidence += 0.08
        if add_to_cart:
            reasons.append("an add-to-cart control is present")
            confidence += 0.04
    elif product_listing or crawler_listing:
        page_type = "product_category"
        confidence = 0.84
        reasons = [
            (
                "a product-listing container is present"
                if product_listing
                else "URL or WordPress body class identifies a product category"
            )
        ]
    elif crawler_detail:
        page_type = "product_detail"
        confidence = 0.78
        reasons = [
            "the conservative B2B product-page detector found a "
            "substantive page with product identity and image evidence"
        ]
    else:
        if heading and len(blocks) >= 2:
            page_type = "knowledge_page"
            confidence = 0.55
            reasons = ["the page has a primary heading and substantive main content"]
        else:
            page_type = "unknown"
            confidence = 0.2
            reasons = ["no product, category, or editorial page signals were strong enough"]

    product_page: ParsedProductPage | None = None
    if page_type == "product_detail":
        try:
            product_page = parse_product_page(canonical, source)
        except ProductAssetError as exc:
            reasons.append(f"product detail extraction was partial: {type(exc).__name__}")

    return ClassifiedWebPage(
        requested_url=requested_url,
        canonical_url=canonical,
        page_type=page_type,
        confidence=min(confidence, 0.99),
        reasons=tuple(reasons),
        title=title,
        heading=heading,
        breadcrumbs=_breadcrumb_labels(document, heading),
        text_blocks=blocks,
        product_page=product_page,
        metadata={
            "schema_types": sorted(schema_types),
            "parser_version": WEB_PAGE_PARSER_VERSION,
        },
    )


def discover_product_links(
    *,
    site_url: str,
    category_url: str,
    html: str | bytes,
    limit: int = 24,
) -> tuple[str, ...]:
    """Discover deterministic same-site product candidates from a category page."""

    if limit <= 0:
        raise ValueError("limit must be positive")
    source = html if isinstance(html, bytes) else html.encode("utf-8")
    parser = lxml_html.HTMLParser(encoding="utf-8", recover=True)
    try:
        document = lxml_html.fromstring(source, parser=parser, base_url=category_url)
    except (ValueError, etree.ParserError) as exc:
        raise WordPressIngestionError("category HTML could not be parsed") from exc
    candidates: list[tuple[int, str]] = []
    seen: set[str] = set()
    for anchor in document.xpath("//a[@href]"):
        href = str(anchor.get("href") or "").strip()
        if not href:
            continue
        try:
            url = normalize_official_url(site_url, urljoin(category_url, href))
        except UnsafeOfficialSiteUrl:
            continue
        path = urlsplit(url).path.casefold()
        if (
            url == category_url
            or path.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg"))
            or any(marker in path for marker in ("/blog/", "/news/", "/tag/"))
        ):
            continue
        context = " ".join(
            str(value)
            for node in [anchor, *anchor.iterancestors()]
            for value in (node.get("class"), node.get("rel"))
            if value
        ).casefold()
        score = 0
        if "product" in context:
            score += 3
        if "/product/" in path:
            score += 3
        if anchor.xpath(".//img"):
            score += 1
        if score < 3 or url in seen:
            continue
        seen.add(url)
        candidates.append((score, url))
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return tuple(url for _score, url in candidates[:limit])


def discover_category_pagination_links(
    *,
    site_url: str,
    category_url: str,
    html: str | bytes,
) -> tuple[str, ...]:
    """Discover deterministic same-site pagination links from a category page."""

    source = html if isinstance(html, bytes) else html.encode("utf-8")
    parser = lxml_html.HTMLParser(encoding="utf-8", recover=True)
    try:
        document = lxml_html.fromstring(source, parser=parser, base_url=category_url)
    except (ValueError, etree.ParserError) as exc:
        raise WordPressIngestionError("category HTML could not be parsed") from exc
    candidates: list[tuple[int, str]] = []
    seen: set[str] = set()
    for anchor in document.xpath("//a[@href]"):
        href = str(anchor.get("href") or "").strip()
        if not href:
            continue
        try:
            url = normalize_official_url(site_url, urljoin(category_url, href))
        except UnsafeOfficialSiteUrl:
            continue
        if url == category_url or url in seen:
            continue
        parsed = urlsplit(url)
        rel = str(anchor.get("rel") or "").casefold().split()
        context = " ".join(
            str(value)
            for node in [anchor, *anchor.iterancestors()]
            for value in (node.get("class"), node.get("id"))
            if value
        ).casefold()
        pagination_context = any(
            marker in context
            for marker in (
                "pagination",
                "page-numbers",
                "nav-links",
                "woocommerce-pagination",
            )
        )
        pagination_url = bool(
            re.search(r"/(?:page|paged)/\d+/?$", parsed.path.casefold())
            or re.search(
                r"(?:^|&)(?:page|paged|product-page|product_page)=\d+(?:&|$)",
                parsed.query.casefold(),
            )
        )
        if "next" not in rel and not pagination_context and not pagination_url:
            continue
        seen.add(url)
        candidates.append((0 if "next" in rel else 1, url))
    candidates.sort(key=lambda item: (item[0], item[1]))
    return tuple(url for _rank, url in candidates)


def discover_internal_page_links(
    *,
    site_url: str,
    page_url: str,
    html: str | bytes,
    limit: int = 500,
) -> tuple[str, ...]:
    """Return prioritized same-site HTML page candidates from any page.

    This is deliberately CMS-agnostic. Sitemap and CMS adapters can seed the
    frontier, while ordinary navigation links let the scan continue when a
    site exposes neither WordPress nor a useful sitemap.
    """

    if limit <= 0:
        raise ValueError("limit must be positive")
    source = html if isinstance(html, bytes) else html.encode("utf-8")
    parser = lxml_html.HTMLParser(encoding="utf-8", recover=True)
    try:
        document = lxml_html.fromstring(source, parser=parser, base_url=page_url)
    except (ValueError, etree.ParserError) as exc:
        raise WordPressIngestionError("official page HTML could not be parsed") from exc
    candidates: dict[str, int] = {}
    for anchor in document.xpath("//a[@href]"):
        href = str(anchor.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        try:
            url = normalize_official_url(site_url, urljoin(page_url, href))
        except UnsafeOfficialSiteUrl:
            continue
        parsed = urlsplit(url)
        path = parsed.path.casefold()
        suffix = re.search(r"\.[a-z0-9]{1,8}$", path)
        if (
            url == page_url
            or (suffix is not None and suffix.group(0) in _NON_HTML_SUFFIXES)
            or any(marker in path for marker in _LOW_VALUE_PATH_MARKERS)
        ):
            continue
        text = _clean_text(anchor.text_content()).casefold()
        score = 0
        if anchor.xpath("ancestor::nav | ancestor::header"):
            score += 4
        if any(
            marker in path or marker in text
            for marker in (
                "product",
                "solution",
                "application",
                "about",
                "company",
                "contact",
                "blog",
                "news",
                "article",
                "guide",
                "support",
                "service",
            )
        ):
            score += 3
        depth = len([part for part in path.split("/") if part])
        score += max(0, 3 - depth)
        previous = candidates.get(url)
        if previous is None or score > previous:
            candidates[url] = score
    ranked = sorted(candidates.items(), key=lambda item: (-item[1], item[0]))
    return tuple(url for url, _score in ranked[:limit])


def sitemap_locations(
    *,
    site_url: str,
    payload: str | bytes,
) -> tuple[str, ...]:
    """Read same-site URLs from either a sitemap or a sitemap index."""

    source = payload if isinstance(payload, bytes) else payload.encode("utf-8")
    try:
        document = etree.fromstring(source, parser=etree.XMLParser(recover=True))
    except (ValueError, etree.XMLSyntaxError):
        return ()
    locations: list[str] = []
    seen: set[str] = set()
    for raw in document.xpath("//*[local-name()='loc']/text()"):
        try:
            url = normalize_official_url(site_url, str(raw).strip())
        except (UnsafeOfficialSiteUrl, ValueError):
            continue
        if url in seen:
            continue
        seen.add(url)
        locations.append(url)
    return tuple(locations)


def product_links_from_wordpress_rest(
    *,
    site_url: str,
    payload: str | bytes,
    limit: int,
) -> tuple[str, ...]:
    """Read canonical product links from a public WordPress product route."""

    if limit <= 0:
        raise ValueError("limit must be positive")
    source = payload.decode("utf-8", errors="replace") if isinstance(
        payload,
        bytes,
    ) else payload
    try:
        records = json.loads(source)
    except json.JSONDecodeError:
        return ()
    if not isinstance(records, list):
        return ()
    links: list[str] = []
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, Mapping):
            continue
        raw_link = record.get("link")
        if not isinstance(raw_link, str):
            continue
        try:
            link = normalize_official_url(site_url, raw_link)
        except UnsafeOfficialSiteUrl:
            continue
        if link in seen:
            continue
        seen.add(link)
        links.append(link)
        if len(links) >= limit:
            break
    return tuple(links)


__all__ = [
    "ClassifiedWebPage",
    "FetchedResource",
    "MAX_WEB_RESOURCE_BYTES",
    "OfficialSiteFetchError",
    "OfficialSiteFetcher",
    "SafeOfficialSiteFetcher",
    "UnsafeOfficialSiteUrl",
    "WEB_PAGE_PARSER_VERSION",
    "WORDPRESS_PROBE_VERSION",
    "WebPageType",
    "WordPressIngestionError",
    "WordPressProbeResult",
    "WordPressSiteProbe",
    "classify_web_page",
    "discover_product_links",
    "discover_category_pagination_links",
    "discover_internal_page_links",
    "product_links_from_wordpress_rest",
    "sitemap_locations",
    "normalize_official_url",
    "normalize_site_url",
    "same_official_site",
]
