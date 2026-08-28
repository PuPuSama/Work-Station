from __future__ import annotations

import hashlib
import ipaddress
import json
import mimetypes
import re
import socket
import ssl
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib import parse, request
from urllib.error import HTTPError, URLError

from config import AppConfig
from models import Product, TaskRecord
from services.generator import primary_keyword
from services.tavily import TavilyClient, TavilyError
from storage import write_json_artifact


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
# Some Windows proxy/VPN clients use RFC 2544 benchmark addresses as fake-IP
# placeholders for public hostnames. Permit that range only after resolving a
# hostname; a literal http://198.18.x.x URL remains blocked.
PROXY_FAKE_IP_NETWORKS = (ipaddress.ip_network("198.18.0.0/15"),)
MAX_CRAWL_SECONDS = 28
MAX_DISCOVERY_SECONDS = 12
MIN_POST_DISCOVERY_SECONDS = 12
DEFAULT_FETCH_TIMEOUT = 5
DISCOVERY_FETCH_TIMEOUT = 2
IMAGE_FETCH_TIMEOUT = 10
MAX_ENRICH_CANDIDATES = 14
MAX_RAW_CANDIDATES = 50
MAX_CONTAINER_PROBES = 8
DISCOVERY_TARGET_CANDIDATES = MAX_ENRICH_CANDIDATES
MIN_PREFERRED_CATEGORY_MEMBERS = 3
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
TAVILY_QUERY_STOPWORDS = STOPWORDS | {
    "a",
    "affect",
    "affects",
    "and",
    "an",
    "are",
    "best",
    "benefit",
    "benefits",
    "can",
    "common",
    "complete",
    "environment",
    "environments",
    "everyday",
    "example",
    "examples",
    "for",
    "how",
    "impact",
    "impacts",
    "its",
    "key",
    "guide",
    "material",
    "materials",
    "mighty",
    "of",
    "performance",
    "practical",
    "select",
    "selecting",
    "small",
    "task",
    "tasks",
    "the",
    "to",
    "type",
    "types",
    "why",
}
PRODUCT_FOCUS_HEADS = {
    "anchor",
    "anchors",
    "attachment",
    "attachments",
    "belt",
    "belts",
    "bolt",
    "bolts",
    "drill",
    "drills",
    "equipment",
    "fastener",
    "fasteners",
    "insert",
    "inserts",
    "jewelry",
    "jewellery",
    "ladder",
    "ladders",
    "lamp",
    "lamps",
    "light",
    "lights",
    "machine",
    "machines",
    "mold",
    "molds",
    "mould",
    "moulds",
    "nut",
    "nuts",
    "screw",
    "screws",
    "washer",
    "washers",
}
CATEGORY_GENERIC_TOKENS = TAVILY_QUERY_STOPWORDS | {
    "avoid",
    "avoiding",
    "buyer",
    "buyers",
    "b2b",
    "common",
    "installation",
    "mistake",
    "mistakes",
    "project",
    "projects",
    "selecting",
    "selection",
    "size",
    "thread",
    "using",
}
PRODUCT_PATH_HINTS = (
    "product",
    "products",
    "solution",
    "solutions",
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
NON_PRODUCT_PATH_SEGMENTS = {
    "about",
    "about-us",
    "contact",
    "contact-us",
    "faq",
    "faqs",
    "our-team",
    "service",
    "services",
    "team",
}
NON_PRODUCT_ENTRY_SLUGS = {
    "live",
    "odm",
    "oem",
    "oem-odm",
}
PRODUCT_ROUTE_SLUGS = {
    "product",
    "products",
    "produkt",
    "produkte",
    "produit",
    "produits",
    "producto",
    "productos",
    "prodotto",
    "prodotti",
    "product-category",
}
PRODUCT_INDEX_PATHS = (
    "/products/",
    "/product/",
    "/our-products/",
    "/product-category/",
)
CATEGORY_PATH_HINTS = (
    "/category/",
    "/product-category/",
    "/product-categories/",
    "/collection/",
    "/collections/",
    "/archive/",
    "/tag/",
    "/author/",
    "/feed/",
)
LISTING_SCHEMA_TYPES = {
    "collectionpage",
    "itemlist",
    "productcollection",
    "archivepage",
}
LISTING_CLASS_HINTS = {
    "archive",
    "category",
    "product-category",
    "product-archive",
    "post-type-archive-product",
    "tax-product_cat",
}
LISTING_PRODUCT_CONTEXT_HINTS = (
    "catalog-grid",
    "collection-grid",
    "p-item",
    "product-card",
    "product-grid",
    "product-item",
    "product-list",
    "productny-list",
    "products-grid",
    "shop-loop",
    "uc-items-wrapper",
    "uc-post-list",
    "uc_post_list",
    "woocommerce",
)
LISTING_EXCLUDED_CONTEXT_HINTS = (
    "cross-sell",
    "featured-product",
    "hot-sale",
    "hot_sale",
    "hotsale",
    "main-menu",
    "navigation",
    "newpro",
    "recommend",
    "related-product",
    "site-footer",
    "site-header",
    "upsell",
)
DETAIL_CLASS_HINTS = {
    "single-product",
    "product-template-default",
    "single_product",
}
TRACKING_QUERY_PREFIXES = ("utm_",)
TRACKING_QUERY_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid"}
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
    detail_verified: bool = False
    category_url: str = ""


class SimpleHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.in_title = False
        self.meta: list[dict[str, str]] = []
        self.links: list[dict[str, str]] = []
        self.images: list[dict[str, str]] = []
        self.text_parts: list[str] = []
        self.body_class_tokens: set[str] = set()
        self.json_ld_parts: list[str] = []
        self._json_ld_buffer: list[str] = []
        self.in_json_ld = False
        self.skip_depth = 0
        self._element_stack: list[tuple[str, str]] = []
        self._link_stack: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key.lower(): value or "" for key, value in attrs}
        tag = tag.lower()
        context = normalize_space(
            " ".join(
                value
                for _, value in self._element_stack
                if value
            )
        )
        if tag == "body":
            self.body_class_tokens.update(
                token.casefold()
                for token in re.split(r"\s+", attributes.get("class", "").strip())
                if token
            )
        if tag == "script" and attributes.get("type", "").split(";", 1)[0].strip().casefold() == "application/ld+json":
            self.in_json_ld = True
            self._json_ld_buffer = []
        if tag in {"script", "style", "noscript", "svg"}:
            self.skip_depth += 1
        if tag == "title":
            self.in_title = True
        elif tag == "meta":
            self.meta.append(attributes)
        elif tag == "a":
            link = dict(attributes)
            link["_context"] = normalize_space(
                " ".join(
                    [
                        context,
                        attributes.get("id", ""),
                        attributes.get("class", ""),
                        attributes.get("role", ""),
                    ]
                )
            )
            link["_text"] = ""
            self.links.append(link)
            self._link_stack.append(link)
        elif tag == "img":
            self.images.append(attributes)
        if tag not in {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }:
            descriptor = normalize_space(
                " ".join(
                    [
                        tag,
                        attributes.get("id", ""),
                        attributes.get("class", ""),
                        attributes.get("role", ""),
                    ]
                )
            )
            self._element_stack.append((tag, descriptor))

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "script" and self.in_json_ld:
            payload = "".join(self._json_ld_buffer).strip()
            if payload:
                self.json_ld_parts.append(payload)
            self._json_ld_buffer = []
            self.in_json_ld = False
        if tag in {"script", "style", "noscript", "svg"} and self.skip_depth:
            self.skip_depth -= 1
        if tag == "title":
            self.in_title = False
        if tag == "a" and self._link_stack:
            self._link_stack.pop()
        for index in range(len(self._element_stack) - 1, -1, -1):
            if self._element_stack[index][0] == tag:
                del self._element_stack[index:]
                break

    def handle_data(self, data: str) -> None:
        if self.in_json_ld:
            self._json_ld_buffer.append(data)
            return
        text = normalize_space(data)
        if not text:
            return
        if self.in_title:
            self.title_parts.append(text)
        elif not self.skip_depth:
            self.text_parts.append(text)
            if self._link_stack:
                link = self._link_stack[-1]
                link["_text"] = normalize_space(f'{link.get("_text", "")} {text}')

    @property
    def title(self) -> str:
        return normalize_space(" ".join(self.title_parts))

    @property
    def text(self) -> str:
        return normalize_space(" ".join(self.text_parts))


def recommend_products(
    config: AppConfig,
    task: TaskRecord,
    limit: int = 3,
    *,
    tavily_client: TavilyClient | None = None,
    download_images: bool = True,
    candidate_pool_limit: int | None = None,
) -> list[Product]:
    # Product recommendations feed the article image workflow, whose public
    # contract allows at most three automatic recommendations.
    limit = max(1, min(int(limit), 3))
    result_limit = limit
    if candidate_pool_limit is not None:
        result_limit = max(
            limit,
            min(int(candidate_pool_limit), MAX_ENRICH_CANDIDATES),
        )
    base_url = site_base_url(task.customer)
    terms = search_terms(task)
    candidates: list[CrawlCandidate] = []
    seen_urls: set[str] = set()

    tavily_audit: dict[str, Any] = {"status": "disabled", "query": "", "results": []}
    if tavily_client is not None and tavily_client.ready:
        tavily_candidates, tavily_audit = candidates_from_tavily(
            tavily_client,
            base_url,
            terms,
            max_results=min(MAX_ENRICH_CANDIDATES, 10),
        )
        for candidate in tavily_candidates:
            normalized_candidate_url = normalize_url(candidate.url)
            if not normalized_candidate_url or normalized_candidate_url in seen_urls:
                continue
            seen_urls.add(normalized_candidate_url)
            rough_score_candidate(candidate, terms, base_url)
            if candidate.score > 0:
                candidates.append(candidate)

    # Tavily has its own request timeout and is only a supplementary URL
    # discovery source. Start the official-site crawl budget afterwards so a
    # slow search response cannot consume the time needed to expand category
    # pages and verify product details.
    started_at = time.monotonic()
    deadline = started_at + MAX_CRAWL_SECONDS
    discovery_deadline = min(
        started_at + MAX_DISCOVERY_SECONDS,
        deadline - MIN_POST_DISCOVERY_SECONDS,
    )

    for candidate in collect_candidates(base_url, terms, discovery_deadline):
        normalized_candidate_url = normalize_url(candidate.url)
        if not normalized_candidate_url or normalized_candidate_url in seen_urls:
            continue
        seen_urls.add(normalized_candidate_url)
        rough_score_candidate(candidate, terms, base_url)
        if candidate.score > 0:
            candidates.append(candidate)
        if len(candidates) >= MAX_RAW_CANDIDATES or expired(deadline):
            break

    # Tavily is a URL discovery layer only. Every result still passes the same
    # local same-site and product-detail verification below before it can be used.
    write_json_artifact(task, "product_assets/tavily_search.json", tavily_audit)

    candidates.sort(key=lambda item: item.score, reverse=True)
    enriched_candidates: list[CrawlCandidate] = []
    for candidate in candidates[:MAX_ENRICH_CANDIDATES]:
        if expired(deadline):
            break
        enrich_candidate(candidate, terms, deadline)
        if not candidate.detail_verified:
            continue
        score_candidate(candidate, terms, base_url)
        if candidate.score > 0:
            enriched_candidates.append(candidate)

    # Never fall back to rough, unverified URLs. A relevant-looking path is not
    # sufficient evidence that a page is an individual product detail page.
    candidates = enriched_candidates
    candidates.sort(key=lambda item: item.score, reverse=True)
    products: list[Product] = []
    fallback_products: list[Product] = []
    seen_product_urls: set[str] = set()
    seen_image_urls: set[str] = set()
    seen_image_hashes: set[str] = set()
    for candidate in candidates:
        if len(products) >= result_limit:
            break
        normalized_product_url = normalize_url(candidate.url)
        if not normalized_product_url or normalized_product_url in seen_product_urls:
            continue
        image_path = ""
        if download_images and candidate.image_url:
            normalized_image_url = normalize_url(candidate.image_url)
            if normalized_image_url and normalized_image_url in seen_image_urls:
                candidate.debug.append("duplicate-image-url")
                continue
            image_path = download_product_image(
                task,
                candidate,
                seen_hashes=seen_image_hashes,
            )
            if not image_path and "duplicate-image-bytes" in candidate.debug:
                continue
            if image_path and normalized_image_url:
                seen_image_urls.add(normalized_image_url)
        product = Product(
            name=candidate.name or product_name_from_url(candidate.url),
            url=candidate.url,
            canonical_url=candidate.url,
            image_path=image_path,
            description=candidate.description[:420],
            discovery_source=candidate.source,
            detail_page_verified=candidate.detail_verified,
        )
        seen_product_urls.add(normalized_product_url)
        if image_path:
            products.append(product)
        else:
            fallback_products.append(product)

    for product in fallback_products:
        if len(products) >= result_limit:
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
                "category_url": candidate.category_url,
                "debug": candidate.debug,
            }
            for candidate in candidates[:20]
        ],
    )
    return products


def tavily_product_query(terms: list[str]) -> str:
    """Collapse overlapping article phrases into one product-oriented query."""

    focus = product_focus_phrase(terms)
    if focus:
        return f"{focus} products"

    tokens: list[str] = []
    seen: set[str] = set()
    for term in unique(terms)[:6]:
        if not term or term.lower().startswith("http"):
            continue
        for token in re.findall(r"[a-zA-Z0-9]+", term.casefold()):
            if len(token) < 3 or token in TAVILY_QUERY_STOPWORDS or token in seen:
                continue
            seen.add(token)
            tokens.append(token)
            if len(tokens) >= 5:
                break
        if len(tokens) >= 5:
            break

    query = " ".join(tokens)
    query = re.sub(r"\bself\s+tapper(?:s)?\b", "self tapping screw", query)
    query = normalize_space(query)
    if not query:
        return "products"
    if not re.search(r"\bproducts?\b", query, flags=re.IGNORECASE):
        query = f"{query} products"
    return query


def product_focus_phrase(terms: list[str]) -> str:
    """Extract the product family and drop article-angle wording from search."""

    for term in unique(terms):
        value = normalize_space(term).casefold()
        if not value or value.startswith("http"):
            continue
        if re.search(r"\bself[\s-]+tappers?\b", value):
            return "self tapping screw"
        if re.search(r"\bwoodscrews?\b|\bwood[\s-]+screws?\b", value):
            return "wood screws"
        if re.search(r"\bdrywall[\s-]*screws?\b|\bdry[\s-]+wall[\s-]+screws?\b", value):
            return "drywall screws"

        raw_tokens = re.findall(r"[a-z0-9]+", value)
        for index, token in enumerate(raw_tokens):
            if token not in PRODUCT_FOCUS_HEADS:
                continue
            start = max(0, index - 3)
            phrase_tokens = [
                item
                for item in raw_tokens[start : index + 1]
                if item not in CATEGORY_GENERIC_TOKENS
            ]
            if phrase_tokens:
                return " ".join(phrase_tokens)
    return ""


def taxonomy_tokens(value: str) -> set[str]:
    """Normalize simple singular/plural and compound product-family variants."""

    normalized: set[str] = set()
    for token in re.findall(r"[a-z0-9]+", parse.unquote(str(value or "")).casefold()):
        normalized.add(token)
        if len(token) > 4 and token.endswith("ies"):
            normalized.add(token[:-3] + "y")
        elif len(token) > 4 and token.endswith("s") and not token.endswith("ss"):
            normalized.add(token[:-1])
        if re.fullmatch(r"woodscrews?", token):
            normalized.update({"wood", "screw", "woodscrew"})
        if token == "drywall":
            normalized.update({"dry", "wall"})
        if token == "stool":
            # A stool ladder is normally catalogued as a compact step ladder,
            # not under a generic telescopic-ladder branch.
            normalized.add("step")
    return normalized


def category_relevance_score(candidate: CrawlCandidate, terms: list[str]) -> int:
    """Prefer the narrowest official category that overlaps the article product family."""

    topic_tokens = taxonomy_tokens(" ".join(terms)) - CATEGORY_GENERIC_TOKENS
    category_tokens = taxonomy_tokens(f"{candidate.name} {candidate.url}")
    overlap = topic_tokens.intersection(category_tokens)
    if not overlap:
        return 0
    score = sum(2 if len(token) >= 5 else 1 for token in overlap)
    path_depth = len([part for part in parse.urlparse(candidate.url).path.split("/") if part])
    # Specific taxonomy overlap must dominate path depth. Otherwise a deeper
    # but unrelated branch (for example telescopic/aluminum) can beat the
    # correct Step Ladder category merely because it has one extra segment.
    return score * 10 + min(path_depth, 5)


def link_has_excluded_listing_context(link: dict[str, str]) -> bool:
    context = link.get("_context", "").casefold()
    context_tokens = set(re.findall(r"[a-z0-9_-]+", context))
    return bool(
        context_tokens.intersection({"footer", "header", "nav"})
        or any(hint in context for hint in LISTING_EXCLUDED_CONTEXT_HINTS)
    )


def link_has_product_listing_context(link: dict[str, str]) -> bool:
    context = link.get("_context", "").casefold()
    return any(hint in context for hint in LISTING_PRODUCT_CONTEXT_HINTS)


def listing_member_links(parser: SimpleHTMLParser) -> list[dict[str, str]]:
    """Keep product-card links and discard navigation, related, and Hot Sale modules."""

    allowed = [
        link
        for link in parser.links
        if link.get("href")
        and not link_has_excluded_listing_context(link)
        and not looks_like_non_product_page(link["href"])
        and not looks_like_blog(link["href"])
    ]
    scoped = [link for link in allowed if link_has_product_listing_context(link)]
    return scoped or allowed


def candidates_from_tavily(
    client: TavilyClient,
    base_url: str,
    terms: list[str],
    *,
    max_results: int = 10,
) -> tuple[list[CrawlCandidate], dict[str, Any]]:
    """Discover official URLs; never accept Tavily snippets or images as proof."""

    host = parse.urlparse(base_url).netloc
    query = tavily_product_query(terms)
    audit: dict[str, Any] = {"status": "ok", "query": query, "results": []}
    try:
        response = client.search(query, host, max_results=max(1, min(max_results, 20)))
    except TavilyError as exc:
        audit.update({"status": "error", "error": str(exc)})
        return [], audit

    candidates: list[CrawlCandidate] = []
    for result in response.results:
        url = strip_url_fragment(result.url)
        same_site_result = bool(url and same_site(base_url, url))
        accepted = bool(same_site_result and not looks_like_blog(url))
        audit["results"].append(
            {
                "title": result.title,
                "url": url,
                "score": result.score,
                "same_site": same_site_result,
                "eligible_product_url": accepted,
            }
        )
        if not accepted:
            continue
        candidates.append(
            CrawlCandidate(
                name=clean_title(result.title),
                url=url,
                # Search snippets are not authoritative product facts. The
                # official detail page fetched below supplies the description.
                description="",
                source="tavily",
                debug=["official-domain Tavily discovery; pending local detail verification"],
            )
        )
    return candidates, audit


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
        title = clean_html_text(
            rest_rendered(item.get("title"))
            or item.get("alt_text")
            or product_name_from_url(image_url)
        )
        description = clean_html_text(
            item.get("alt_text") or rest_rendered(item.get("caption")) or title
        )
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
    seen: set[str] = set()

    def append_links(
        parser: SimpleHTMLParser,
        page_url: str,
        *,
        source: str,
        debug: list[str],
        category_members_only: bool = False,
        category_url: str = "",
    ) -> list[CrawlCandidate]:
        added: list[CrawlCandidate] = []
        links = listing_member_links(parser) if category_members_only else parser.links
        for link in links:
            href = link.get("href", "")
            if not href:
                continue
            url = strip_url_fragment(parse.urljoin(page_url, href))
            normalized = normalize_url(url)
            if (
                not normalized
                or normalized in seen
                or not same_site(base_url, url)
                or not is_product_index_candidate(page_url, url, terms)
            ):
                continue
            if category_members_only and (
                is_known_listing_url(url)
                or looks_like_blog(url)
                or link_has_excluded_listing_context(link)
            ):
                continue
            if normalize_url(url).rstrip("/") == normalize_url(page_url).rstrip("/"):
                continue
            seen.add(normalized)
            name = clean_html_text(
                link.get("_text")
                or link.get("title")
                or link.get("aria-label")
                or product_name_from_url(url)
            )
            candidate = CrawlCandidate(
                name=name,
                url=url,
                source=source,
                debug=list(debug),
                category_url=category_url,
            )
            candidates.append(candidate)
            added.append(candidate)
            if len(candidates) >= MAX_RAW_CANDIDATES:
                break
        return added

    for index_path in PRODUCT_INDEX_PATHS:
        if expired(deadline):
            break
        index_url = urljoin(base_url, index_path)
        html = fetch_text(index_url, timeout=discovery_fetch_timeout(deadline))
        if not html:
            continue
        parser = parse_html(html)
        direct = append_links(
            parser,
            index_url,
            source="product-index",
            debug=[index_url],
        )

        # Many B2B sites expose only category tiles on /products/. Rank those
        # containers against the article's product family before probing them,
        # so a woodscrew article reaches the woodscrew category before broad
        # Hot Sale, drill-bit, or nut pages.
        ranked_containers = sorted(
            enumerate(direct),
            key=lambda item: (category_relevance_score(item[1], terms), -item[0]),
            reverse=True,
        )
        preferred_members: list[CrawlCandidate] = []
        preferred_score = 0
        for _, container in ranked_containers[:MAX_CONTAINER_PROBES]:
            if expired(deadline):
                break
            relevance = category_relevance_score(container, terms)
            fetch_timeout = discovery_fetch_timeout(deadline)
            if relevance > 0:
                remaining = max(1, int(deadline - time.monotonic()))
                fetch_timeout = max(fetch_timeout, min(DEFAULT_FETCH_TIMEOUT, remaining))
            container_html = fetch_text(
                container.url,
                timeout=fetch_timeout,
            )
            if not container_html:
                continue
            container_parser = parse_html(container_html)
            if not is_product_listing_page(container.url, container_parser, terms):
                continue
            previous_candidates = list(candidates)
            previous_seen = set(seen)
            container.debug.append("listing-container")
            candidates.remove(container)
            source = "product-category" if relevance > 0 else "product-container"
            if relevance > 0:
                # A full generic index must not prevent the closest taxonomy
                # category from supplying its own product-detail candidates.
                candidates.clear()
                seen.clear()
            added = append_links(
                container_parser,
                container.url,
                source=source,
                debug=[index_url, f"category={container.url}"],
                category_members_only=True,
                category_url=container.url,
            )
            if relevance > 0 and not added:
                candidates[:] = previous_candidates
                seen.clear()
                seen.update(previous_seen)
            if relevance > preferred_score and added:
                preferred_score = relevance
                preferred_members = added
            if relevance > 0 and len(added) >= MIN_PREFERRED_CATEGORY_MEMBERS:
                return added
            if len(candidates) >= DISCOVERY_TARGET_CANDIDATES:
                return candidates
        if preferred_members:
            preferred_urls = {normalize_url(item.url) for item in preferred_members}
            return preferred_members + [
                item for item in candidates if normalize_url(item.url) not in preferred_urls
            ]
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
    candidate.detail_verified = False
    if expired(deadline):
        return
    html, final_url = fetch_page(candidate.url)
    if not html:
        return
    if final_url:
        if not same_site(candidate.url, final_url):
            candidate.debug.append(f"rejected-cross-site-redirect={final_url}")
            return
        candidate.url = strip_url_fragment(final_url)
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

    if not is_product_detail_page(candidate.url, parser, terms):
        candidate.debug.append("rejected-non-detail-page")
        return
    candidate.detail_verified = True


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
    if candidate.source == "tavily":
        score += 8
    if candidate.source == "product-category":
        score += 80
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
    if candidate.source == "tavily":
        score += 8
    if candidate.source == "product-category":
        score += 100
    if candidate.source == "rest:media":
        score -= 8
    if contains_bad_image_hint(haystack):
        score -= 20
    if looks_like_blog(candidate.url):
        score -= 10
    candidate.score = score


def download_product_image(
    task: TaskRecord,
    candidate: CrawlCandidate,
    *,
    seen_hashes: set[str] | None = None,
) -> str:
    image_url = candidate.image_url
    if not image_url or is_bad_image_url(image_url):
        return ""
    existing_path = existing_product_image(task, candidate)
    if existing_path:
        if seen_hashes is not None:
            try:
                digest = hashlib.sha256(Path(existing_path).read_bytes()).hexdigest()
            except OSError:
                return ""
            if digest in seen_hashes:
                candidate.debug.append("duplicate-image-bytes")
                return ""
            seen_hashes.add(digest)
        return existing_path
    try:
        response = open_url(image_url, timeout=IMAGE_FETCH_TIMEOUT)
        data = response.read(8 * 1024 * 1024)
        content_type = response.headers.get("Content-Type", "").split(";")[0].strip()
    except (HTTPError, URLError, TimeoutError, OSError, UnicodeError):
        return ""

    if not is_valid_product_image(data, image_url, content_type):
        return ""

    digest = hashlib.sha256(data).hexdigest()
    if seen_hashes is not None:
        if digest in seen_hashes:
            candidate.debug.append("duplicate-image-bytes")
            return ""
        seen_hashes.add(digest)

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
    for field in (task.competitor_keyword, task.selected_title, task.topic):
        for compound in re.findall(r"\b[a-zA-Z0-9]+(?:-[a-zA-Z0-9]+)+\b", field or ""):
            phrase = " ".join(tokenize(compound.replace("-", " ")))
            if phrase:
                terms.append(phrase)
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
    terms = unique([term for term in terms if term])
    focus = product_focus_phrase(terms)
    if focus:
        terms = [focus] + [term for term in terms if term.casefold() != focus.casefold()]
    return terms


RedirectValidator = Callable[[str], bool | None]


class UnsafeOutboundURLError(ValueError):
    """Raised before a request when its host or redirect target is unsafe."""


class _ValidatingRedirectHandler(request.HTTPRedirectHandler):
    def __init__(self, validator: RedirectValidator | None = None) -> None:
        super().__init__()
        self.validator = validator

    def redirect_request(
        self,
        req,
        fp,
        code,
        msg,
        headers,
        newurl,
    ):
        _validate_outbound_url(str(newurl), redirect_validator=self.validator)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_json(
    url: str,
    timeout: int = DEFAULT_FETCH_TIMEOUT,
    *,
    redirect_validator: RedirectValidator | None = None,
) -> Any:
    text = fetch_text(url, timeout=timeout, redirect_validator=redirect_validator)
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def fetch_text(
    url: str,
    timeout: int = DEFAULT_FETCH_TIMEOUT,
    *,
    redirect_validator: RedirectValidator | None = None,
) -> str:
    text, _ = fetch_page(url, timeout=timeout, redirect_validator=redirect_validator)
    return text


def fetch_page(
    url: str,
    timeout: int = DEFAULT_FETCH_TIMEOUT,
    *,
    redirect_validator: RedirectValidator | None = None,
) -> tuple[str, str]:
    """Fetch one text page and retain the final URL after redirects."""

    try:
        response = open_url(
            url,
            timeout=timeout,
            redirect_validator=redirect_validator,
        )
        raw = response.read(2 * 1024 * 1024)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError):
        return "", ""
    get_charset = getattr(response.headers, "get_content_charset", None)
    content_type = get_charset() if callable(get_charset) else None
    final_url_getter = getattr(response, "geturl", None)
    final_url = final_url_getter() if callable(final_url_getter) else url
    return raw.decode(content_type or "utf-8", errors="ignore"), str(final_url or url)


def open_url(
    url: str,
    timeout: int = DEFAULT_FETCH_TIMEOUT,
    *,
    redirect_validator: RedirectValidator | None = None,
):
    safe_url = encode_url(url)
    _validate_outbound_url(safe_url, redirect_validator=redirect_validator)
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
        contexts.append(ssl._create_unverified_context())
    last_error: Exception | None = None
    for context in contexts:
        try:
            handlers: list[object] = [_ValidatingRedirectHandler(redirect_validator)]
            if context is not None:
                handlers.append(request.HTTPSHandler(context=context))
            opener = request.build_opener(*handlers)
            return opener.open(req, timeout=timeout)
        except (HTTPError, URLError, TimeoutError, OSError, ssl.SSLError) as error:
            last_error = error
    if last_error:
        raise last_error
    opener = request.build_opener(_ValidatingRedirectHandler(redirect_validator))
    return opener.open(req, timeout=timeout)


def _validate_outbound_url(
    url: str,
    *,
    redirect_validator: RedirectValidator | None = None,
) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
    parsed = parse.urlsplit(str(url or ""))
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        raise UnsafeOutboundURLError("Outbound URL must use HTTP(S) and include a hostname.")
    if parsed.username or parsed.password:
        raise UnsafeOutboundURLError("Outbound URL credentials are not allowed.")
    if redirect_validator is not None:
        try:
            allowed = redirect_validator(str(url))
        except Exception as exc:
            raise UnsafeOutboundURLError("Outbound URL failed its redirect policy.") from exc
        if allowed is False:
            raise UnsafeOutboundURLError("Outbound URL was rejected by its redirect policy.")

    host = parsed.hostname.rstrip(".")
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise UnsafeOutboundURLError("Outbound URL hostname is invalid.") from exc
    try:
        literal_host = ipaddress.ip_address(ascii_host)
    except ValueError:
        literal_host = None
    try:
        port = parsed.port or (443 if parsed.scheme.casefold() == "https" else 80)
    except ValueError as exc:
        raise UnsafeOutboundURLError("Outbound URL port is invalid.") from exc
    try:
        records = socket.getaddrinfo(
            ascii_host,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise UnsafeOutboundURLError("Outbound URL hostname could not be resolved.") from exc

    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for record in records:
        sockaddr = record[4]
        if not sockaddr:
            continue
        raw_address = str(sockaddr[0]).split("%", 1)[0]
        try:
            address = ipaddress.ip_address(raw_address)
        except ValueError as exc:
            raise UnsafeOutboundURLError("DNS returned an invalid address.") from exc
        mapped = getattr(address, "ipv4_mapped", None)
        checked = mapped or address
        unsafe = (
            checked.is_private
            or checked.is_loopback
            or checked.is_link_local
            or checked.is_multicast
            or checked.is_reserved
            or checked.is_unspecified
        )
        proxy_fake_ip = literal_host is None and any(
            checked in network for network in PROXY_FAKE_IP_NETWORKS
        )
        if unsafe and not proxy_fake_ip:
            raise UnsafeOutboundURLError("Outbound URL resolved to a non-public address.")
        addresses.append(address)
    if not addresses:
        raise UnsafeOutboundURLError("Outbound URL hostname returned no usable A/AAAA records.")
    return tuple(addresses)


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


def json_ld_types(parser: SimpleHTMLParser) -> set[str]:
    """Return every Schema.org ``@type`` found in JSON-LD blocks."""

    types: set[str] = set()

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            raw_type = value.get("@type")
            values = raw_type if isinstance(raw_type, list) else [raw_type]
            for item in values:
                if item:
                    types.add(str(item).rsplit("/", 1)[-1].casefold())
            for nested in value.values():
                collect(nested)
        elif isinstance(value, list):
            for nested in value:
                collect(nested)

    for raw in parser.json_ld_parts:
        payload: Any = None
        for candidate in (raw, unescape(raw)):
            try:
                payload = json.loads(candidate)
                break
            except (json.JSONDecodeError, TypeError):
                continue
        if payload is not None:
            collect(payload)
    return types


def product_page_links(page_url: str, parser: SimpleHTMLParser, terms: list[str]) -> list[str]:
    """Collect unique same-site links that could lead to products or product containers."""

    links: list[str] = []
    seen: set[str] = set()
    current = normalize_url(page_url)
    for link in parser.links:
        href = str(link.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        url = strip_url_fragment(parse.urljoin(page_url, href))
        normalized = normalize_url(url)
        if (
            not normalized
            or normalized == current
            or normalized in seen
            or not same_site(page_url, url)
            or not is_relevant_product_url(url, terms)
        ):
            continue
        seen.add(normalized)
        links.append(url)
    return links


def _has_listing_pagination(parser: SimpleHTMLParser) -> bool:
    for link in parser.links:
        href = str(link.get("href") or "").casefold()
        if re.search(r"(?:/page/\d+/?(?:[?#]|$)|[?&](?:page|paged)=\d+)", href):
            return True
    return False


def _has_class_hint(tokens: set[str], hints: set[str]) -> bool:
    return any(hint == token or hint in token for token in tokens for hint in hints)


def is_known_listing_url(url: str) -> bool:
    parsed = parse.urlparse(url)
    path = re.sub(r"/{2,}", "/", parse.unquote(parsed.path)).casefold()
    stripped = path.rstrip("/") or "/"
    if stripped in {item.rstrip("/") or "/" for item in PRODUCT_INDEX_PATHS}:
        return True
    return any(hint in path for hint in CATEGORY_PATH_HINTS)


def is_pagination_url(url: str) -> bool:
    parsed = parse.urlparse(url)
    if re.search(r"/page/\d+/?$", parsed.path, flags=re.IGNORECASE):
        return True
    return any(
        key.casefold() in {"page", "paged"} and str(value).isdigit()
        for key, value in parse.parse_qsl(parsed.query, keep_blank_values=True)
    )


def is_product_listing_page(
    page_url: str,
    parser: SimpleHTMLParser,
    terms: list[str],
) -> bool:
    """Identify containers/archives without treating ordinary related products as a listing."""

    schema_types = json_ld_types(parser)
    if is_known_listing_url(page_url):
        return True
    og_type = first_meta(parser, "og:type").strip().casefold()
    if (
        "product" in schema_types
        or og_type == "product"
        or _has_class_hint(parser.body_class_tokens, DETAIL_CLASS_HINTS)
    ):
        # Product detail templates often repeat a category menu and several
        # related products in their footer. Explicit detail-page evidence must
        # win over those generic listing signals (for example jadduo.cn).
        return False
    if _has_class_hint(parser.body_class_tokens, LISTING_CLASS_HINTS):
        return True
    if schema_types.intersection(LISTING_SCHEMA_TYPES - {"itemlist"}):
        return True

    outgoing_products = product_page_links(page_url, parser, terms)
    text = parser.text.casefold()
    has_filter = bool(
        re.search(
            r"\bfilter\b|\ball products\b|\bproduct categor(?:y|ies)\b|\bshop by categor(?:y|ies)\b",
            text,
        )
    )
    has_pagination = _has_listing_pagination(parser)
    if len(outgoing_products) >= 4 and (has_filter or has_pagination):
        return True
    if "itemlist" in schema_types and len(outgoing_products) >= 4 and "product" not in schema_types:
        return True
    return False


def is_product_detail_page(
    page_url: str,
    parser: SimpleHTMLParser,
    terms: list[str],
) -> bool:
    """Require strong product evidence, with a conservative generic fallback."""

    if (
        not page_url
        or looks_like_non_product_page(page_url)
        or looks_like_blog(page_url)
        or is_product_listing_page(page_url, parser, terms)
    ):
        return False

    schema_types = json_ld_types(parser)
    og_type = first_meta(parser, "og:type").strip().casefold()
    if (
        "product" in schema_types
        or og_type == "product"
        or _has_class_hint(parser.body_class_tokens, DETAIL_CLASS_HINTS)
    ):
        return True
    if og_type == "article" or schema_types.intersection(
        {"article", "blogposting", "newsarticle"}
    ):
        return False

    # Custom B2B sites sometimes publish product pages as ordinary WordPress
    # pages. Accept that shape only when the page is relevant, substantive and
    # has a plausible product image. Listing signals above still take priority.
    title = first_meta(parser, "og:title") or first_meta(parser, "twitter:title") or parser.title
    description = first_meta(parser, "og:description") or first_meta(parser, "description")
    haystack = " ".join((page_url, title, description)).casefold()
    relevant = is_relevant_product_url(page_url, terms) and any(
        token in haystack for term in terms for token in tokenize(term)
    )
    if not relevant:
        relevant = any(hint in haystack for hint in PRODUCT_PATH_HINTS)
    has_image = bool(
        first_meta(parser, "og:image")
        or first_meta(parser, "twitter:image")
        or best_html_image(page_url, parser, terms)
    )
    substantive = len(clean_html_text(description)) >= 40 or len(parser.text) >= 220
    return bool(relevant and has_image and substantive)


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
        if contains_bad_image_hint(text):
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
    if is_pagination_url(url):
        return False
    if any(part in path for part in ("/tag/", "/author/", "/wp-content/", "/feed/")):
        return False
    if parsed.query and not any(key in parsed.query.lower() for key in ("product", "p=")):
        return False
    index_path = parse.urlparse(index_url).path.rstrip("/").lower()
    if index_path and path.rstrip("/").startswith(index_path + "/"):
        return True
    return is_known_listing_url(url) or is_relevant_product_url(url, terms)


def is_bad_image_url(url: str) -> bool:
    lower = parse.unquote(str(url or "")).lower()
    if not lower:
        return True
    path = parse.urlparse(lower).path
    if any(part in path for part in ("/wp-content/themes/", "/wp-includes/", "/assets/icons/", "/images/icons/")):
        return True
    return contains_bad_image_hint(lower)


def contains_bad_image_hint(value: str) -> bool:
    """Match UI/decorative image hints on token boundaries.

    A substring check incorrectly rejected legitimate names such as
    ``shared-product.jpg`` because ``share`` appeared inside ``shared``.
    """

    tokens = re.findall(r"[a-z0-9]+", parse.unquote(str(value or "")).casefold())
    for hint in BAD_IMAGE_HINTS:
        hint_tokens = re.findall(r"[a-z0-9]+", hint.casefold())
        if not hint_tokens:
            continue
        if len(hint_tokens) == 1:
            word = hint_tokens[0]
            if any(token in {word, word + "s", word + "es"} for token in tokens):
                return True
            continue
        size = len(hint_tokens)
        if any(tokens[index : index + size] == hint_tokens for index in range(len(tokens) - size + 1)):
            return True
    return False


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
    if (
        is_known_listing_url(url)
        or is_pagination_url(url)
        or looks_like_non_product_page(url)
        or looks_like_blog(url)
        or any(part in path for part in ("/author/", "/wp-content/"))
    ):
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
    if not parsed.netloc:
        return ""
    scheme = parsed.scheme.casefold()
    if scheme in {"http", "https"}:
        scheme = "https"
    netloc = parsed.netloc.casefold().removeprefix("www.")
    if netloc.endswith(":80") and scheme == "https":
        netloc = netloc[:-3]
    if netloc.endswith(":443") and scheme == "https":
        netloc = netloc[:-4]
    path = re.sub(r"/{2,}", "/", parse.unquote(parsed.path)).rstrip("/").casefold()
    query_items = []
    for key, value in parse.parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.casefold()
        if lowered in TRACKING_QUERY_KEYS or lowered.startswith(TRACKING_QUERY_PREFIXES):
            continue
        query_items.append((key, value))
    query = parse.urlencode(sorted(query_items), doseq=True)
    return parse.urlunparse(
        (
            scheme,
            netloc,
            path,
            "",
            query,
            "",
        )
    )


def strip_url_fragment(url: str) -> str:
    parsed = parse.urlsplit(str(url or ""))
    return parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


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
    if any(part in path for part in ("/blog/", "/news/", "/article/", "/articles/")):
        return True
    segments = [segment for segment in path.strip("/").split("/") if segment]
    if len(segments) != 1:
        return False
    slug = segments[0]
    return bool(
        re.match(r"^(?:how|why|what|when|where|which|can|should)-", slug)
        or re.search(r"(?:^|-)(?:guide|ideas|mistakes|tips)(?:-|$)", slug)
    )


def looks_like_non_product_page(url: str) -> bool:
    path_segments = [
        segment.casefold()
        for segment in parse.unquote(parse.urlparse(url).path).strip("/").split("/")
        if segment
    ]
    if path_segments and path_segments[-1] in NON_PRODUCT_ENTRY_SLUGS:
        if not set(path_segments[:2]).intersection(PRODUCT_ROUTE_SLUGS):
            return True
    return bool(set(path_segments).intersection(NON_PRODUCT_PATH_SEGMENTS))


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


def discovery_fetch_timeout(deadline: float) -> int:
    """Bound one discovery request so retries cannot consume the full crawl budget."""

    remaining = max(0.0, deadline - time.monotonic())
    return max(1, min(DISCOVERY_FETCH_TIMEOUT, int(max(1.0, remaining / 2))))
