from __future__ import annotations

import copy
import hashlib
import json
import mimetypes
import re
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from html import unescape
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit, urlunsplit

try:
    from lxml import etree
    from lxml import html as lxml_html
except ImportError:  # pragma: no cover - the application runtime includes lxml.
    etree = None  # type: ignore[assignment]
    lxml_html = None  # type: ignore[assignment]


MANIFEST_SCHEMA_VERSION = 1
ASSET_ROOT_NAME = "product_assets"
MANIFEST_FILENAME = "manifest.json"
IMAGE_DIRECTORY_NAME = "images"
SOURCE_KIND_JSON_LD = "json_ld_product_image"
SOURCE_KIND_GALLERY = "main_gallery"
SOURCE_KIND_BODY = "body_image"

_BLOCKED_TAGS = {"header", "nav", "footer", "aside", "script", "style", "noscript", "template"}
_ELEMENTOR_ALL_DEVICE_HIDDEN_CLASSES = frozenset(
    {
        "elementor-hidden-widescreen",
        "elementor-hidden-desktop",
        "elementor-hidden-laptop",
        "elementor-hidden-tablet",
        "elementor-hidden-mobile",
    }
)
_HIDDEN_STYLE_PATTERN = re.compile(
    r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*hidden)\s*(?:!important\s*)?(?:;|$)",
    re.IGNORECASE,
)
_BLOCKED_CONTEXT_PATTERN = re.compile(
    r"(?:^|[\s_-])(?:"
    r"related(?:[\s_-]?products?)?|"
    r"productny[\s_-]?list|"
    r"recently[\s_-]?viewed(?:[\s_-]?products?)?|recent[\s_-]?products?|"
    r"similar[\s_-]?products?|featured[\s_-]?products?|"
    r"product[\s_-]?categor(?:y|ies)|"
    r"also[\s_-]?bought|frequently[\s_-]?bought|"
    r"hot[\s_-]?(?:sale|products?)?|best[\s_-]?sellers?|"
    r"prodeta[\s_-]?formbox|"
    r"other[\s_-]?products?|more[\s_-]?products?|"
    r"upsells?|cross[\s_-]?sells?|"
    r"recommend(?:ed|ation|ations)?|you[\s_-]?may[\s_-]?also[\s_-]?like"
    r")(?:$|[\s_-])",
    re.IGNORECASE,
)
_FAQ_CONTEXT_PATTERN = re.compile(
    r"(?:^|[\s_-])(?:faq|faqs|frequently[\s_-]?asked)(?:$|[\s_-])",
    re.IGNORECASE,
)
_GALLERY_CONTEXT_PATTERN = re.compile(
    r"(?:woocommerce[\s_-]?product[\s_-]?gallery|product[\s_-]?(?:image|images|gallery|"
    r"thumbnails?|slider)|(?:^|[\s_-])gallery(?:$|[\s_-])|flex[\s_-]?viewport)",
    re.IGNORECASE,
)
_SPEC_CONTEXT_PATTERN = re.compile(
    r"(?:^|[\s_-])(?:spec|specification|specifications|technical|attribute|attributes|"
    r"parameter|parameters)(?:$|[\s_-])",
    re.IGNORECASE,
)
_PRODUCT_CONTAINER_PATTERN = re.compile(
    r"(?:^|[\s_-])(?:single[\s_-]?product|product[\s_-]?(?:detail|details|summary|content|"
    r"information|info)|woocommerce[\s_-]?product)(?:$|[\s_-])",
    re.IGNORECASE,
)
_BAD_IMAGE_TOKENS = {
    "avatar",
    "blank",
    "favicon",
    "icon",
    "logo",
    "placeholder",
    "search",
    "share",
    "sprite",
    "spinner",
    "tracking",
    "transparent",
}
_IMAGE_SUFFIXES = {".avif", ".gif", ".jpeg", ".jpg", ".png", ".webp"}


class ProductAssetError(RuntimeError):
    """Raised when a product page cannot be parsed or its asset contract cannot be written."""


class AssetDownloader(Protocol):
    """Network-agnostic downloader injected by the caller.

    The downloader receives exactly one source URL. It may return bytes, a
    ``(bytes, content_type)`` tuple, or a mapping with ``content``/``data`` and
    optional ``content_type``, ``width`` and ``height`` values.
    """

    def __call__(self, source_url: str) -> object: ...


@dataclass(frozen=True)
class ProductAssetDirectories:
    task_dir: Path
    asset_root: Path
    product_dir: Path
    images_dir: Path
    manifest_path: Path
    product_id: str

    def contract(self) -> dict[str, str]:
        """Return paths relative to the task directory, which is the contract base."""

        return {
            "path_base": "task_dir",
            "product_dir": self.product_dir.relative_to(self.task_dir).as_posix(),
            "images_dir": self.images_dir.relative_to(self.task_dir).as_posix(),
            "manifest_path": self.manifest_path.relative_to(self.task_dir).as_posix(),
            "download_policy": "injectable_downloader_only",
        }


@dataclass
class ProductImageAsset:
    source_url: str
    source_kind: str
    alt: str = ""
    title: str = ""
    caption: str = ""
    dom_context: dict[str, Any] = field(default_factory=dict)
    width: int | None = None
    height: int | None = None
    asset_id: str = ""
    source_kinds: list[str] = field(default_factory=list)
    dom_contexts: list[dict[str, Any]] = field(default_factory=list)
    sha256: str | None = None
    byte_size: int | None = None
    content_type: str | None = None
    local_path: str | None = None
    download_error: str | None = None

    def __post_init__(self) -> None:
        if not self.source_kinds:
            self.source_kinds = [self.source_kind]
        if not self.dom_contexts and self.dom_context:
            self.dom_contexts = [self.dom_context]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.asset_id,
            "source_url": self.source_url,
            "alt": self.alt,
            "title": self.title,
            "caption": self.caption,
            "source_kind": self.source_kind,
            "source_kinds": list(self.source_kinds),
            "dom_context": dict(self.dom_context),
            "dom_contexts": [dict(item) for item in self.dom_contexts],
            # Width, height and hashes intentionally remain present before a
            # download so later pipeline stages can enrich the same record.
            "width": self.width,
            "height": self.height,
            "sha256": self.sha256,
            "byte_size": self.byte_size,
            "content_type": self.content_type,
            "local_path": self.local_path,
            "download_error": self.download_error,
        }


@dataclass
class ParsedProductPage:
    product_id: str
    source_url: str
    canonical_url: str
    h1: str
    meta_description: str
    main_content_facts: list[str]
    specification_tables: list[dict[str, Any]]
    faq: list[dict[str, str]]
    json_ld_product_images: list[ProductImageAsset]
    main_gallery: list[ProductImageAsset]
    body_images: list[ProductImageAsset]

    @property
    def image_sources(self) -> dict[str, list[ProductImageAsset]]:
        return {
            "json_ld_product_images": self.json_ld_product_images,
            "main_gallery": self.main_gallery,
            "body_images": self.body_images,
        }


@dataclass
class ProductAssetResult:
    parsed: ParsedProductPage
    directories: ProductAssetDirectories
    candidates: list[ProductImageAsset]

    @property
    def download_candidates(self) -> list[dict[str, Any]]:
        return [candidate.to_dict() for candidate in self.candidates]

    @property
    def manifest_path(self) -> Path:
        return self.directories.manifest_path

    def to_manifest(self) -> dict[str, Any]:
        image_sources = {
            name: [asset.to_dict() for asset in assets]
            for name, assets in self.parsed.image_sources.items()
        }
        return {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "product_id": self.parsed.product_id,
            "source_url": self.parsed.source_url,
            "page": {
                "canonical_url": self.parsed.canonical_url,
                "h1": self.parsed.h1,
                "meta_description": self.parsed.meta_description,
                "main_content_facts": list(self.parsed.main_content_facts),
                "specification_tables": copy.deepcopy(self.parsed.specification_tables),
                "faq": copy.deepcopy(self.parsed.faq),
                "image_sources": image_sources,
            },
            "directory_contract": self.directories.contract(),
            "download_candidates": self.download_candidates,
        }

    def as_dict(self) -> dict[str, Any]:
        return self.to_manifest()

    def __getitem__(self, key: str) -> Any:
        """Allow light-weight dict-style pipeline integration without hiding the dataclass API."""

        return self.to_manifest()[key]


def _normalise_space(value: object) -> str:
    return re.sub(r"\s+", " ", unescape(str(value or ""))).strip()


def _safe_product_id(product_id: object) -> str:
    value = unicodedata.normalize("NFKC", str(product_id or "")).strip()
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(" .-_")
    value = value[:80].rstrip(" .-_")
    if not value or value in {".", ".."}:
        raise ProductAssetError("product_id must contain at least one safe letter or number")
    return value


def product_asset_directories(
    task_dir: str | Path,
    product_id: str,
    *,
    create: bool = True,
) -> ProductAssetDirectories:
    task_path = Path(task_dir).expanduser().resolve()
    safe_product_id = _safe_product_id(product_id)
    asset_root = task_path / ASSET_ROOT_NAME
    product_dir = asset_root / safe_product_id
    images_dir = product_dir / IMAGE_DIRECTORY_NAME
    if create:
        images_dir.mkdir(parents=True, exist_ok=True)
    return ProductAssetDirectories(
        task_dir=task_path,
        asset_root=asset_root,
        product_dir=product_dir,
        images_dir=images_dir,
        manifest_path=product_dir / MANIFEST_FILENAME,
        product_id=safe_product_id,
    )


def _tag(element: Any) -> str:
    tag = getattr(element, "tag", "")
    return tag.casefold() if isinstance(tag, str) else ""


def _context_text(element: Any) -> str:
    if element is None:
        return ""
    attributes = getattr(element, "attrib", {})
    return " ".join(
        _normalise_space(attributes.get(name, ""))
        for name in ("id", "class", "role", "aria-label", "data-section")
    )


def _element_and_ancestors(element: Any):
    yield element
    yield from element.iterancestors()


def _is_explicitly_hidden(element: Any) -> bool:
    attributes = getattr(element, "attrib", {})
    if "hidden" in attributes:
        return True
    if _normalise_space(attributes.get("aria-hidden", "")).casefold() == "true":
        return True
    if _HIDDEN_STYLE_PATTERN.search(str(attributes.get("style") or "")):
        return True
    classes = frozenset(
        value.casefold()
        for value in _normalise_space(attributes.get("class", "")).split()
    )
    return _ELEMENTOR_ALL_DEVICE_HIDDEN_CLASSES.issubset(classes)


def _is_blocked(element: Any) -> bool:
    for node in _element_and_ancestors(element):
        if _tag(node) in _BLOCKED_TAGS or _is_explicitly_hidden(node):
            return True
        if _BLOCKED_CONTEXT_PATTERN.search(_context_text(node)):
            return True
    return False


def _is_faq_context(element: Any) -> bool:
    return any(_FAQ_CONTEXT_PATTERN.search(_context_text(node)) for node in _element_and_ancestors(element))


def _has_ancestor_tag(element: Any, tags: set[str]) -> bool:
    return any(_tag(node) in tags for node in element.iterancestors())


def _normalise_url(value: object, base_url: str) -> str:
    raw = _normalise_space(value)
    if not raw or raw.startswith(("data:", "blob:", "javascript:", "mailto:", "tel:")):
        return ""
    joined = urljoin(base_url, raw)
    parsed = urlsplit(joined)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        return ""
    return urlunsplit((parsed.scheme.casefold(), parsed.netloc.casefold(), parsed.path, parsed.query, ""))


def _comparison_url(value: str) -> str:
    parsed = urlsplit(value)
    path = re.sub(r"/+", "/", parsed.path or "/").rstrip("/") or "/"
    return urlunsplit((parsed.scheme.casefold(), parsed.netloc.casefold(), path, "", ""))


def _is_image_link(value: str) -> bool:
    return Path(urlsplit(value).path).suffix.casefold() in _IMAGE_SUFFIXES


def _looks_like_other_product_link(
    image: Any,
    product_url: str,
    canonical_url: str,
) -> bool:
    current_urls = {
        _comparison_url(url)
        for url in (product_url, canonical_url)
        if url
    }
    for ancestor in image.iterancestors("a"):
        linked_url = _normalise_url(ancestor.get("href"), product_url)
        if not linked_url or _is_image_link(linked_url):
            continue
        if _comparison_url(linked_url) in current_urls:
            continue
        # A product image linked to the current page or directly to its image
        # file is allowed above.  Any other navigational card belongs to a
        # different page, even when the site uses flat top-level product URLs
        # instead of a conventional /products/ path.
        return True
    return False


def _looks_like_ui_image(source_url: str) -> bool:
    parsed = urlsplit(source_url)
    tokens = {
        token.casefold()
        for token in re.split(r"[^A-Za-z0-9]+", parsed.path)
        if token
    }
    if tokens & _BAD_IMAGE_TOKENS:
        return True
    return any(
        token.startswith(("favicon", "icon", "logo", "placeholder", "spinner", "sprite"))
        for token in tokens
    )


def _parse_dimension(value: object) -> int | None:
    match = re.search(r"\d+", str(value or ""))
    if not match:
        return None
    number = int(match.group(0))
    return number if number > 0 else None


def _srcset_url(srcset: object, base_url: str) -> str:
    ranked: list[tuple[float, str]] = []
    for index, raw_candidate in enumerate(str(srcset or "").split(",")):
        parts = raw_candidate.strip().split()
        if not parts:
            continue
        url = _normalise_url(parts[0], base_url)
        if not url:
            continue
        score = float(index)
        if len(parts) > 1:
            descriptor = parts[-1].casefold()
            try:
                if descriptor.endswith("w"):
                    score = float(descriptor[:-1])
                elif descriptor.endswith("x"):
                    score = float(descriptor[:-1]) * 10_000
            except ValueError:
                pass
        ranked.append((score, url))
    return max(ranked, default=(0, ""), key=lambda item: item[0])[1]


def _image_source_url(image: Any, base_url: str) -> str:
    for attribute in ("data-large_image", "data-large-image", "data-zoom-image"):
        source_url = _normalise_url(image.get(attribute), base_url)
        if source_url:
            return source_url

    srcset = image.get("srcset") or image.get("data-srcset") or image.get("data-lazy-srcset")
    source_url = _srcset_url(srcset, base_url)
    if source_url:
        return source_url

    for attribute in (
        "data-src",
        "data-lazy-src",
        "data-original",
        "data-lazy",
        "src",
    ):
        source_url = _normalise_url(image.get(attribute), base_url)
        if source_url:
            return source_url
    return ""


def _caption_for_image(image: Any) -> str:
    for ancestor in image.iterancestors():
        if _tag(ancestor) == "figure":
            captions = ancestor.xpath(".//figcaption")
            if captions:
                return _normalise_space(captions[0].text_content())
        if "wp-caption" in _context_text(ancestor).casefold():
            captions = [
                node
                for node in ancestor.iterdescendants()
                if "caption" in _context_text(node).casefold() and node is not image
            ]
            if captions:
                return _normalise_space(captions[0].text_content())
    sibling = image.getnext()
    if sibling is not None and "caption" in _context_text(sibling).casefold():
        return _normalise_space(sibling.text_content())
    return ""


def _nearest_section_heading(element: Any) -> str:
    for ancestor in _element_and_ancestors(element):
        siblings = list(ancestor.itersiblings(preceding=True))
        for sibling in reversed(siblings):
            if _tag(sibling) in {"h1", "h2", "h3", "h4"}:
                return _normalise_space(sibling.text_content())
        if _tag(ancestor) in {"section", "article", "main"}:
            headings = [
                node
                for node in ancestor.iterchildren()
                if _tag(node) in {"h1", "h2", "h3", "h4"}
            ]
            if headings:
                return _normalise_space(headings[0].text_content())
    return ""


def _dom_context(document: Any, image: Any, *, linked_url: str = "") -> dict[str, Any]:
    tree = image.getroottree()
    parent = image.getparent()
    return {
        "tag": _tag(image),
        "xpath": tree.getpath(image) if tree is not None else "",
        "id": image.get("id", ""),
        "class": image.get("class", ""),
        "parent_tag": _tag(parent),
        "parent_id": parent.get("id", "") if parent is not None else "",
        "parent_class": parent.get("class", "") if parent is not None else "",
        "section_heading": _nearest_section_heading(image),
        "linked_url": linked_url,
    }


def _asset_from_image(
    document: Any,
    image: Any,
    *,
    source_kind: str,
    product_url: str,
) -> ProductImageAsset | None:
    source_url = _image_source_url(image, product_url)
    if not source_url or _looks_like_ui_image(source_url):
        return None
    link = next(image.iterancestors("a"), None)
    linked_url = _normalise_url(link.get("href"), product_url) if link is not None else ""
    return ProductImageAsset(
        source_url=source_url,
        source_kind=source_kind,
        alt=_normalise_space(image.get("alt")),
        title=_normalise_space(image.get("title")),
        caption=_caption_for_image(image),
        dom_context=_dom_context(document, image, linked_url=linked_url),
        width=_parse_dimension(image.get("width") or image.get("data-width")),
        height=_parse_dimension(image.get("height") or image.get("data-height")),
    )


def _canonical_url(document: Any, product_url: str) -> str:
    for link in document.xpath("//link[@href]"):
        rels = {part.casefold() for part in re.split(r"\s+", link.get("rel", "").strip()) if part}
        if "canonical" in rels:
            canonical = _normalise_url(link.get("href"), product_url)
            if canonical:
                return canonical
    return _normalise_url(product_url, product_url) or product_url


def _meta_description(document: Any) -> str:
    fallback = ""
    for meta in document.xpath("//meta[@content]"):
        name = (meta.get("name") or "").casefold().strip()
        prop = (meta.get("property") or "").casefold().strip()
        content = _normalise_space(meta.get("content"))
        if name == "description" and content:
            return content
        if prop == "og:description" and content and not fallback:
            fallback = content
    return fallback


def _content_candidate_score(node: Any) -> tuple[float, int]:
    """Rank roots by actual product evidence instead of tag name alone.

    Some WordPress themes emit an empty ``<main>`` shell while rendering the
    product detail in a sibling ``div``.  A tag-only preference therefore
    hides every real image from downstream extraction.  The score deliberately
    caps broad signals so a huge page wrapper does not beat a focused product
    container merely because it contains navigation or related content.
    """

    headings = [item for item in node.iter("h1") if not _is_blocked(item)]
    images = [
        item
        for item in node.iter("img")
        if not _is_blocked(item)
        and any(
            _normalise_space(item.get(attribute))
            for attribute in (
                "data-large_image",
                "data-large-image",
                "data-zoom-image",
                "srcset",
                "data-srcset",
                "data-lazy-srcset",
                "data-src",
                "data-lazy-src",
                "data-original",
                "data-lazy",
                "src",
            )
        )
    ]
    tables = [item for item in node.iter("table") if not _is_blocked(item)]
    text_length = len(_normalise_space(node.text_content()))
    score = min(len(headings), 2) * 100.0
    score += min(len(images), 8) * 15.0
    score += min(text_length, 1_600) / 80.0
    score += min(len(tables), 4) * 10.0
    if _tag(node) == "main":
        score += 10.0
    elif _tag(node) == "article":
        score += 5.0

    depth = sum(1 for _ in node.iterancestors())
    return score, depth


def _main_content_root(document: Any) -> Any:
    candidates: list[Any] = []
    seen: set[int] = set()

    def add(node: Any) -> None:
        identity = id(node)
        if identity in seen or _is_blocked(node):
            return
        seen.add(identity)
        candidates.append(node)

    for node in document.xpath("//main | //article"):
        add(node)
    for heading in document.xpath("//h1"):
        if _is_blocked(heading):
            continue
        add(heading)
        for ancestor in heading.iterancestors():
            if _tag(ancestor) in {"html", "body"}:
                break
            add(ancestor)
    for node in document.xpath("//*"):
        if _PRODUCT_CONTAINER_PATTERN.search(_context_text(node)) or "schema.org/product" in (
            node.get("itemtype") or ""
        ).casefold():
            add(node)

    if candidates:
        best = max(candidates, key=_content_candidate_score)
        # An empty semantic shell scores only its small tag bonus.  Requiring
        # one additional point ensures we fall back to <body> when every
        # candidate is structurally empty.
        if _content_candidate_score(best)[0] > 10.0:
            return best

    body = document.find("body")
    return body if body is not None else document


def _extract_main_content_facts(main: Any) -> list[str]:
    facts: list[str] = []
    seen: set[str] = set()
    for element in main.iter():
        tag = _tag(element)
        if tag not in {"p", "li", "dd"}:
            continue
        if _is_blocked(element) or _is_faq_context(element):
            continue
        if _has_ancestor_tag(element, {"table", "dl"}):
            continue
        if tag == "p" and _has_ancestor_tag(element, {"li"}):
            continue
        text = _normalise_space(element.text_content())
        key = text.casefold()
        if len(text) < 3 or key in seen:
            continue
        seen.add(key)
        facts.append(text)
    return facts


def _extract_specification_tables(main: Any) -> list[dict[str, Any]]:
    tables: list[dict[str, Any]] = []
    for table in main.iter("table"):
        if _is_blocked(table) or _is_faq_context(table):
            continue
        headers: list[str] = []
        rows: list[list[str]] = []
        for row in table.iter("tr"):
            cells = [cell for cell in row.iterchildren() if _tag(cell) in {"th", "td"}]
            values = [_normalise_space(cell.text_content()) for cell in cells]
            if not any(values):
                continue
            if not headers and cells and all(_tag(cell) == "th" for cell in cells):
                headers = values
            else:
                rows.append(values)
        if headers or rows:
            captions = list(table.iter("caption"))
            tables.append(
                {
                    "source_kind": "html_table",
                    "caption": _normalise_space(captions[0].text_content()) if captions else "",
                    "headers": headers,
                    "rows": rows,
                }
            )

    for definition_list in main.iter("dl"):
        if _is_blocked(definition_list) or _is_faq_context(definition_list):
            continue
        if not _SPEC_CONTEXT_PATTERN.search(_context_text(definition_list)):
            continue
        rows: list[list[str]] = []
        current_term = ""
        for child in definition_list.iterchildren():
            if _tag(child) == "dt":
                current_term = _normalise_space(child.text_content())
            elif _tag(child) == "dd" and current_term:
                rows.append([current_term, _normalise_space(child.text_content())])
        if rows:
            tables.append(
                {
                    "source_kind": "definition_list",
                    "caption": "",
                    "headers": ["Property", "Value"],
                    "rows": rows,
                }
            )
    return tables


def _schema_types(node: Mapping[str, Any]) -> set[str]:
    raw_types = node.get("@type", [])
    if isinstance(raw_types, str):
        raw_types = [raw_types]
    return {
        str(raw_type).rstrip("/").rsplit("/", 1)[-1].casefold()
        for raw_type in raw_types
    }


def _walk_json(value: Any):
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _json_text(value: Any) -> str:
    if isinstance(value, Mapping):
        value = value.get("text") or value.get("name") or value.get("@value") or ""
    text = _normalise_space(value)
    if "<" in text and ">" in text:
        text = _normalise_space(re.sub(r"<[^>]+>", " ", text))
    return text


def _json_ld_payloads(document: Any) -> list[Any]:
    payloads: list[Any] = []
    for script in document.xpath("//script"):
        content_type = (script.get("type") or "").split(";", 1)[0].strip().casefold()
        if content_type != "application/ld+json":
            continue
        source = (script.text or "").strip()
        source = re.sub(r"^\s*<!--|-->\s*$", "", source).strip().rstrip(";")
        if not source:
            continue
        try:
            payloads.append(json.loads(source))
        except (TypeError, ValueError):
            continue
    return payloads


def _json_ld_image_values(value: Any):
    if isinstance(value, str):
        yield value, {}, "", "", ""
        return
    if isinstance(value, list):
        for child in value:
            yield from _json_ld_image_values(child)
        return
    if not isinstance(value, Mapping):
        return
    source = value.get("contentUrl") or value.get("url") or value.get("@id")
    if source:
        yield (
            source,
            value,
            _json_text(value.get("alternateName") or value.get("name")),
            _json_text(value.get("name")),
            _json_text(value.get("caption")),
        )


def _normalise_product_name(value: Any) -> str:
    text = unicodedata.normalize("NFKC", _json_text(value)).casefold()
    return re.sub(r"[\W_]+", " ", text, flags=re.UNICODE).strip()


def _product_name_matches_h1(product_name: Any, h1: str) -> bool:
    """Return true only for a strong product-name/H1 match.

    Product names are commonly a shorter substring of a descriptive H1. Model
    tokens containing digits are treated as identity-bearing, so a related
    Pump 800 node cannot match a Pump 900 page merely because most words agree.
    """

    name = _normalise_product_name(product_name)
    heading = _normalise_product_name(h1)
    if not name or not heading:
        return False
    if name == heading:
        return True

    name_tokens = name.split()
    heading_tokens = heading.split()
    name_models = {token for token in name_tokens if any(char.isdigit() for char in token)}
    heading_models = {token for token in heading_tokens if any(char.isdigit() for char in token)}
    if name_models and not name_models.issubset(heading_models):
        return False

    # Avoid accepting a generic one-word Product node such as "Pump" merely
    # because that word appears somewhere in a detailed page heading.
    if (name in heading or heading in name) and (
        len(name_tokens) >= 2 or len(name) >= 12 or bool(name_models)
    ):
        return True

    overlap = len(set(name_tokens) & set(heading_tokens))
    if not overlap:
        return False
    precision = overlap / len(set(name_tokens))
    recall = overlap / len(set(heading_tokens))
    return overlap >= 2 and precision >= 0.9 and recall >= 0.6


def _json_ld_url_values(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for child in value:
            yield from _json_ld_url_values(child)
    elif isinstance(value, Mapping):
        for key in ("url", "@id"):
            if key in value:
                yield from _json_ld_url_values(value[key])


def _product_node_matches_page(
    node: Mapping[str, Any],
    *,
    h1: str,
    canonical_url: str,
    product_url: str,
) -> bool:
    if _product_name_matches_h1(node.get("name"), h1):
        return True

    page_urls = {
        _comparison_url(url)
        for url in (canonical_url, product_url)
        if _normalise_url(url, product_url)
    }
    for field in ("url", "@id", "mainEntityOfPage"):
        for raw_url in _json_ld_url_values(node.get(field)):
            candidate_url = _normalise_url(raw_url, product_url)
            if candidate_url and _comparison_url(candidate_url) in page_urls:
                return True
    return False


def _extract_json_ld(
    document: Any,
    product_url: str,
    *,
    h1: str,
    canonical_url: str,
) -> tuple[list[ProductImageAsset], list[dict[str, str]]]:
    images: list[ProductImageAsset] = []
    faq: list[dict[str, str]] = []
    faq_seen: set[tuple[str, str]] = set()

    walked_nodes = [
        (payload_index, node_index, node)
        for payload_index, payload in enumerate(_json_ld_payloads(document))
        for node_index, node in enumerate(_walk_json(payload))
    ]
    product_nodes = [
        (payload_index, node_index, node)
        for payload_index, node_index, node in walked_nodes
        if "product" in _schema_types(node)
    ]
    accepted_products = {
        (payload_index, node_index)
        for payload_index, node_index, node in product_nodes
        if len(product_nodes) == 1
        or _product_node_matches_page(
            node,
            h1=h1,
            canonical_url=canonical_url,
            product_url=product_url,
        )
    }

    for payload_index, node_index, node in walked_nodes:
        types = _schema_types(node)
        if "product" in types and (payload_index, node_index) in accepted_products:
            for source, metadata, alt, title, caption in _json_ld_image_values(node.get("image")):
                source_url = _normalise_url(source, product_url)
                if not source_url or _looks_like_ui_image(source_url):
                    continue
                images.append(
                    ProductImageAsset(
                        source_url=source_url,
                        source_kind=SOURCE_KIND_JSON_LD,
                        alt=alt,
                        title=title,
                        caption=caption,
                        dom_context={
                            "schema_type": "Product",
                            "payload_index": payload_index,
                            "node_index": node_index,
                            "schema_image_type": next(iter(_schema_types(metadata)), "")
                            if isinstance(metadata, Mapping)
                            else "",
                        },
                        width=_parse_dimension(metadata.get("width"))
                        if isinstance(metadata, Mapping)
                        else None,
                        height=_parse_dimension(metadata.get("height"))
                        if isinstance(metadata, Mapping)
                        else None,
                    )
                )

        if "question" in types:
            question = _json_text(node.get("name"))
            answer_value = node.get("acceptedAnswer") or node.get("suggestedAnswer")
            answers = answer_value if isinstance(answer_value, list) else [answer_value]
            answer = next((_json_text(item) for item in answers if _json_text(item)), "")
            key = (question.casefold(), answer.casefold())
            if question and answer and key not in faq_seen:
                faq_seen.add(key)
                faq.append({"question": question, "answer": answer, "source_kind": "json_ld"})
    return images, faq


def _text_without_descendant(element: Any, excluded: Any) -> str:
    clone = copy.deepcopy(element)
    clone_excluded = clone.xpath(".//summary") if _tag(excluded) == "summary" else []
    for child in clone_excluded:
        child.getparent().remove(child)
    return _normalise_space(clone.text_content())


def _extract_dom_faq(main: Any) -> list[dict[str, str]]:
    faq: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(question: str, answer: str, source_kind: str) -> None:
        question = _normalise_space(question)
        answer = _normalise_space(answer)
        key = (question.casefold(), answer.casefold())
        if question and answer and key not in seen:
            seen.add(key)
            faq.append({"question": question, "answer": answer, "source_kind": source_kind})

    for details in main.iter("details"):
        if _is_blocked(details):
            continue
        summaries = list(details.iter("summary"))
        if not summaries:
            continue
        add(summaries[0].text_content(), _text_without_descendant(details, summaries[0]), "details")

    for question_node in main.xpath('.//*[@itemprop="mainEntity"]'):
        if _is_blocked(question_node):
            continue
        names = question_node.xpath('.//*[@itemprop="name"]')
        answers = question_node.xpath('.//*[@itemprop="acceptedAnswer"]')
        if names and answers:
            answer_texts = answers[0].xpath('.//*[@itemprop="text"]')
            answer_node = answer_texts[0] if answer_texts else answers[0]
            add(names[0].text_content(), answer_node.text_content(), "microdata")

    return faq


def _dedupe_faq(*groups: list[dict[str, str]]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for group in groups:
        for item in group:
            key = (item["question"].casefold(), item["answer"].casefold())
            if key not in seen:
                seen.add(key)
                result.append(item)
    return result


def _is_gallery_image(image: Any) -> bool:
    return any(
        _GALLERY_CONTEXT_PATTERN.search(_context_text(node))
        for node in _element_and_ancestors(image)
    )


def _extract_dom_images(
    document: Any,
    main: Any,
    product_url: str,
    canonical_url: str,
) -> tuple[list[ProductImageAsset], list[ProductImageAsset]]:
    gallery: list[ProductImageAsset] = []
    body: list[ProductImageAsset] = []
    for image in main.iter("img"):
        if _is_blocked(image) or _is_faq_context(image):
            continue
        if _looks_like_other_product_link(image, product_url, canonical_url):
            continue
        source_kind = SOURCE_KIND_GALLERY if _is_gallery_image(image) else SOURCE_KIND_BODY
        asset = _asset_from_image(
            document,
            image,
            source_kind=source_kind,
            product_url=product_url,
        )
        if asset is None:
            continue
        (gallery if source_kind == SOURCE_KIND_GALLERY else body).append(asset)
    return gallery, body


def _dedupe_source_group(assets: list[ProductImageAsset]) -> list[ProductImageAsset]:
    result: list[ProductImageAsset] = []
    seen: set[str] = set()
    for asset in assets:
        key = _comparison_url(asset.source_url)
        if key in seen:
            continue
        seen.add(key)
        result.append(asset)
    return result


def _consolidate_candidates(parsed: ParsedProductPage) -> list[ProductImageAsset]:
    ordered = [
        *parsed.json_ld_product_images,
        *parsed.main_gallery,
        *parsed.body_images,
    ]
    consolidated: list[ProductImageAsset] = []
    by_url: dict[str, ProductImageAsset] = {}
    raw_by_url: dict[str, list[ProductImageAsset]] = {}
    for asset in ordered:
        key = _comparison_url(asset.source_url)
        raw_by_url.setdefault(key, []).append(asset)
        existing = by_url.get(key)
        if existing is None:
            merged = copy.deepcopy(asset)
            consolidated.append(merged)
            by_url[key] = merged
            continue
        if asset.source_kind not in existing.source_kinds:
            existing.source_kinds.append(asset.source_kind)
        if asset.dom_context and asset.dom_context not in existing.dom_contexts:
            existing.dom_contexts.append(asset.dom_context)
        prefer_dom_metadata = bool(asset.dom_context.get("xpath")) and not bool(
            existing.dom_context.get("xpath")
        )
        if asset.alt and (not existing.alt or prefer_dom_metadata):
            existing.alt = asset.alt
        if asset.title and (not existing.title or prefer_dom_metadata):
            existing.title = asset.title
        if asset.caption and (not existing.caption or prefer_dom_metadata):
            existing.caption = asset.caption
        if existing.width is None and asset.width is not None:
            existing.width = asset.width
        if existing.height is None and asset.height is not None:
            existing.height = asset.height
        if not existing.dom_context.get("xpath") and asset.dom_context.get("xpath"):
            existing.dom_context = asset.dom_context

    for index, asset in enumerate(consolidated, start=1):
        asset.asset_id = f"A{index:02d}"
        for raw in raw_by_url[_comparison_url(asset.source_url)]:
            raw.asset_id = asset.asset_id
    return consolidated


def parse_product_page(
    product_url: str,
    html: str | bytes,
    product_id: str = "",
) -> ParsedProductPage:
    """Parse one already-fetched product detail page without performing network I/O."""

    if lxml_html is None or etree is None:  # pragma: no cover - guarded for clear deployment errors.
        raise ProductAssetError("lxml is required for DOM-aware product asset extraction")
    if not _normalise_space(product_url):
        raise ProductAssetError("product_url is required")
    if not html:
        raise ProductAssetError("html is required")

    source = html if isinstance(html, bytes) else html.encode("utf-8")
    parser = lxml_html.HTMLParser(encoding="utf-8", recover=True, remove_comments=False)
    try:
        document = lxml_html.fromstring(source, parser=parser, base_url=product_url)
    except (ValueError, etree.ParserError) as exc:
        raise ProductAssetError(f"Unable to parse product HTML: {exc}") from exc

    canonical_url = _canonical_url(document, product_url)
    main = _main_content_root(document)
    headings = [
        node
        for node in main.iter("h1")
        if not _is_blocked(node)
    ] or [node for node in document.xpath("//h1") if not _is_blocked(node)]
    h1 = _normalise_space(headings[0].text_content()) if headings else ""

    json_ld_images, json_ld_faq = _extract_json_ld(
        document,
        product_url,
        h1=h1,
        canonical_url=canonical_url,
    )
    gallery, body = _extract_dom_images(document, main, product_url, canonical_url)
    parsed = ParsedProductPage(
        product_id=_safe_product_id(product_id) if product_id else "",
        source_url=_normalise_url(product_url, product_url) or product_url,
        canonical_url=canonical_url,
        h1=h1,
        meta_description=_meta_description(document),
        main_content_facts=_extract_main_content_facts(main),
        specification_tables=_extract_specification_tables(main),
        faq=_dedupe_faq(_extract_dom_faq(main), json_ld_faq),
        json_ld_product_images=_dedupe_source_group(json_ld_images),
        main_gallery=_dedupe_source_group(gallery),
        body_images=_dedupe_source_group(body),
    )
    _consolidate_candidates(parsed)
    return parsed


def save_product_asset_manifest(result: ProductAssetResult) -> Path:
    result.directories.images_dir.mkdir(parents=True, exist_ok=True)
    destination = result.directories.manifest_path
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    try:
        temporary.write_text(
            json.dumps(result.to_manifest(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(destination)
    except OSError as exc:
        raise ProductAssetError(f"Unable to write product asset manifest: {destination}") from exc
    return destination


def _coerce_download_result(response: object) -> tuple[bytes, str | None, int | None, int | None]:
    content_type: str | None = None
    width: int | None = None
    height: int | None = None
    content: object = response

    if isinstance(response, Mapping):
        content = response.get("content", response.get("data", response.get("body")))
        content_type = _normalise_space(response.get("content_type") or response.get("mime_type")) or None
        width = _parse_dimension(response.get("width"))
        height = _parse_dimension(response.get("height"))
    elif isinstance(response, tuple) and len(response) >= 2:
        content = response[0]
        content_type = _normalise_space(response[1]) or None
    elif not isinstance(response, (bytes, bytearray, memoryview)):
        content = getattr(response, "content", getattr(response, "data", None))
        headers = getattr(response, "headers", {}) or {}
        if isinstance(headers, Mapping):
            content_type = _normalise_space(
                headers.get("content-type") or headers.get("Content-Type")
            ).split(";", 1)[0] or None

    if not isinstance(content, (bytes, bytearray, memoryview)):
        raise ProductAssetError("Injected downloader must return bytes or a supported bytes wrapper")
    data = bytes(content)
    if not data:
        raise ProductAssetError("Injected downloader returned an empty asset")
    return data, content_type, width, height


def _image_suffix(source_url: str, content_type: str | None) -> str:
    suffix = Path(urlsplit(source_url).path).suffix.casefold()
    if suffix in _IMAGE_SUFFIXES:
        return ".jpg" if suffix == ".jpeg" else suffix
    guessed = mimetypes.guess_extension((content_type or "").split(";", 1)[0].strip())
    if guessed in _IMAGE_SUFFIXES:
        return ".jpg" if guessed == ".jpeg" else guessed
    return ".bin"


def download_product_asset_candidates(
    result: ProductAssetResult,
    downloader: AssetDownloader | Callable[[str], object],
) -> ProductAssetResult:
    """Materialize candidates with an injected downloader; this module never opens the network."""

    if not callable(downloader):
        raise ProductAssetError("downloader must be callable")
    result.directories.images_dir.mkdir(parents=True, exist_ok=True)
    for candidate in result.candidates:
        try:
            response = downloader(candidate.source_url)
            data, content_type, width, height = _coerce_download_result(response)
            suffix = _image_suffix(candidate.source_url, content_type)
            destination = result.directories.images_dir / f"{candidate.asset_id}{suffix}"
            destination.write_bytes(data)
            candidate.sha256 = hashlib.sha256(data).hexdigest()
            candidate.byte_size = len(data)
            candidate.content_type = content_type or mimetypes.guess_type(destination.name)[0]
            candidate.local_path = destination.relative_to(result.directories.task_dir).as_posix()
            candidate.download_error = None
            if width is not None:
                candidate.width = width
            if height is not None:
                candidate.height = height
        except Exception as exc:  # One bad candidate must not discard the page's other assets.
            candidate.download_error = f"{type(exc).__name__}: {exc}"

    # Copy materialized metadata back into each source bucket so both manifest
    # views are consistent for downstream selectors.
    materialized = {candidate.asset_id: candidate for candidate in result.candidates}
    for assets in result.parsed.image_sources.values():
        for source_asset in assets:
            downloaded = materialized.get(source_asset.asset_id)
            if downloaded is None:
                continue
            source_asset.sha256 = downloaded.sha256
            source_asset.byte_size = downloaded.byte_size
            source_asset.content_type = downloaded.content_type
            source_asset.local_path = downloaded.local_path
            source_asset.download_error = downloaded.download_error
            if source_asset.width is None:
                source_asset.width = downloaded.width
            if source_asset.height is None:
                source_asset.height = downloaded.height

    save_product_asset_manifest(result)
    return result


def extract_product_assets(
    product_url: str,
    html: str | bytes,
    task_dir: str | Path,
    product_id: str,
    *,
    downloader: AssetDownloader | Callable[[str], object] | None = None,
) -> ProductAssetResult:
    """Parse a product page, expose candidates, and persist its manifest.

    HTML must be supplied by the caller. No default downloader exists, so this
    service cannot perform network I/O unless a caller explicitly injects one.
    """

    directories = product_asset_directories(task_dir, product_id)
    parsed = parse_product_page(product_url, html, directories.product_id)
    result = ProductAssetResult(
        parsed=parsed,
        directories=directories,
        candidates=_consolidate_candidates(parsed),
    )
    if downloader is not None:
        return download_product_asset_candidates(result, downloader)
    save_product_asset_manifest(result)
    return result


# A descriptive alias for callers that name the operation after its input.
extract_product_page_assets = extract_product_assets


__all__ = [
    "AssetDownloader",
    "MANIFEST_SCHEMA_VERSION",
    "ParsedProductPage",
    "ProductAssetDirectories",
    "ProductAssetError",
    "ProductAssetResult",
    "ProductImageAsset",
    "download_product_asset_candidates",
    "extract_product_assets",
    "extract_product_page_assets",
    "parse_product_page",
    "product_asset_directories",
    "save_product_asset_manifest",
]
