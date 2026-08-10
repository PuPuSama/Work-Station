from __future__ import annotations

import hashlib
import re
import unicodedata
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote


INVALID_WINDOWS_FILENAME_CHARS = '<>:"/\\|?*'
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
IMAGE_SUFFIXES = {".avif", ".bmp", ".gif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
MAX_ARTICLE_IMAGES = 3
_VISUAL_SAMPLE_SIZE = 32
_PERCEPTUAL_HASH_MAX_DISTANCE = 4
_VISUAL_RMS_MAX_DIFFERENCE = 6.0


class ArticleImageError(ValueError):
    """Base error for deterministic article image preparation and placement."""


class ImageDependencyError(ArticleImageError):
    """Raised when Pillow is not installed in the backend runtime."""


class ImageValidationError(ArticleImageError):
    """Raised when an image source or prepared image is invalid."""


class ImageAnchorRequiredError(ArticleImageError):
    """Raised when a product image cannot be placed without a user-selected anchor."""

    def __init__(self, unresolved: list[dict[str, Any]]) -> None:
        self.unresolved = unresolved
        names = ", ".join(
            str(item.get("product_name") or item.get("filename") or item.get("id") or "image")
            for item in unresolved
        )
        super().__init__(
            "无法在正文中可靠定位以下产品图片："
            f"{names}。请从 anchor_candidates 中选择 H2/H3 锚点后再导出。"
        )


@dataclass(frozen=True)
class ImagePlacement:
    image: dict[str, Any]
    position: str
    line_index: int


@dataclass(frozen=True)
class _ImageFingerprint:
    """Exact and visual fingerprints used to prevent repeated article images."""

    sha256: str
    difference_hash: int
    visual_sample: bytes


def _value(item: object, *names: str, default: Any = "") -> Any:
    for name in names:
        if isinstance(item, Mapping) and name in item:
            value = item[name]
        else:
            value = getattr(item, name, None)
        if value is not None and value != "":
            return value
    return default


def sanitize_image_stem(value: str, *, fallback: str = "image", max_length: int = 110) -> str:
    """Return a Windows-safe filename stem while preserving readable article/product text."""

    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    text = re.sub(r"^#{1,6}\s+", "", text)
    if Path(text).suffix.casefold() in IMAGE_SUFFIXES:
        text = text[: -len(Path(text).suffix)]

    cleaned = "".join(
        "_" if char in INVALID_WINDOWS_FILENAME_CHARS or unicodedata.category(char) == "Cc" else char
        for char in text
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    cleaned = re.sub(r"_+", "_", cleaned)
    cleaned = cleaned[:max_length].rstrip(" .") or fallback

    if cleaned.casefold().split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    return cleaned


def extract_article_title(markdown: str) -> str:
    match = re.search(r"(?m)^#\s+(.+?)\s*$", markdown or "")
    return match.group(1).strip() if match else ""


def _visible_markdown(text: str) -> str:
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[`*_~]", "", text)
    text = re.sub(r"^\s{0,3}(?:#{1,6}|[-+*]|\d+[.)])\s+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _normalise_text(text: str) -> str:
    return re.sub(r"\s+", " ", _visible_markdown(text)).strip().casefold()


def _is_faq_heading(text: str) -> bool:
    return _normalise_text(text) == "faq"


def _is_paragraph_boundary(raw_line: str) -> bool:
    stripped = raw_line.strip()
    if not stripped:
        return True
    return bool(
        re.match(
            r"^(?:#{1,6}\s+|[-+*]\s+|\d+[.)]\s+|\||>\s*|```|~~~|---$|\*\*\*$|___$|!\[|img\.)",
            stripped,
            re.IGNORECASE,
        )
    )


def _paragraph_end_index(lines: list[str], start_index: int) -> int:
    """Return the last hard-wrapped line in one Markdown prose paragraph."""

    if start_index < 0 or start_index >= len(lines):
        return start_index
    if _is_paragraph_boundary(lines[start_index]):
        return start_index

    end_index = start_index
    for index in range(start_index + 1, len(lines)):
        if _is_paragraph_boundary(lines[index]):
            break
        end_index = index
    return end_index


def _section_end_index(lines: list[str], anchor_index: int) -> int:
    """Return the last content line in the nearest H2/H3 content block.

    Product matching identifies the relevant sentence, but an image placed
    immediately after that sentence can split the rest of the same subsection.
    Keep non-hero images at the end of the smallest surrounding H2/H3 block:
    before the next heading at the same or a higher level.
    """

    if anchor_index < 0 or anchor_index >= len(lines):
        return anchor_index

    heading_index = -1
    heading_level = 0
    for index in range(anchor_index, -1, -1):
        match = re.match(r"^\s*(#{2,3})\s+", lines[index])
        if match:
            heading_index = index
            heading_level = len(match.group(1))
            break

    if heading_index < 0:
        return _paragraph_end_index(lines, anchor_index)

    boundary = len(lines)
    for index in range(heading_index + 1, len(lines)):
        match = re.match(r"^\s*(#{1,6})\s+", lines[index])
        if match and len(match.group(1)) <= heading_level:
            boundary = index
            break

    for index in range(boundary - 1, heading_index, -1):
        stripped = lines[index].strip()
        if not stripped or stripped in {"---", "***", "___"}:
            continue
        if re.match(r"^(?:#{1,6}\s+|!\[|img\.)", stripped, re.IGNORECASE):
            continue
        return index

    return _paragraph_end_index(lines, anchor_index)


def _anchor_text_at(lines: list[str], line_index: int) -> str:
    """Return stable visible text for the logical block ending at ``line_index``."""

    if line_index < 0 or line_index >= len(lines):
        return ""
    if _is_paragraph_boundary(lines[line_index]):
        return _visible_markdown(lines[line_index])

    start_index = line_index
    while start_index > 0 and not _is_paragraph_boundary(lines[start_index - 1]):
        start_index -= 1
    return _visible_markdown(" ".join(lines[start_index : line_index + 1]))


def article_anchor_candidates(markdown: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for index, raw_line in enumerate((markdown or "").splitlines()):
        match = re.match(r"^\s*(#{2,3})\s+(.+?)\s*$", raw_line)
        if not match:
            continue
        heading = _visible_markdown(match.group(2))
        if _is_faq_heading(heading):
            continue
        candidates.append(
            {
                "id": f"heading-{index}",
                "heading": heading,
                "anchor_heading": heading,
                "level": len(match.group(1)),
                "line_index": index,
            }
        )
    return candidates


def _nearest_heading(lines: list[str], line_index: int) -> str:
    for index in range(line_index, -1, -1):
        match = re.match(r"^\s*#{2,3}\s+(.+?)\s*$", lines[index])
        if match:
            return _visible_markdown(match.group(1))
    return ""


def _first_body_line_after_heading(lines: list[str], heading_index: int) -> int:
    heading_match = re.match(r"^\s*(#{1,6})\s+", lines[heading_index])
    heading_level = len(heading_match.group(1)) if heading_match else 6
    for index in range(heading_index + 1, len(lines)):
        raw = lines[index]
        next_heading = re.match(r"^\s*(#{1,6})\s+", raw)
        if next_heading and len(next_heading.group(1)) <= heading_level:
            break
        stripped = raw.strip()
        if (
            not stripped
            or stripped in {"---", "***", "___"}
            or next_heading
            or _is_paragraph_boundary(raw)
        ):
            continue
        return index
    return heading_index


def _find_manual_anchor(lines: list[str], override: object) -> tuple[int, str] | None:
    raw_anchor_line = _value(override, "anchor_line", default=None)
    try:
        anchor_line = int(raw_anchor_line) if raw_anchor_line is not None else -1
    except (TypeError, ValueError):
        anchor_line = -1
    if 0 <= anchor_line < len(lines) and not re.match(r"^\s*#{1,6}\s+", lines[anchor_line]):
        return _section_end_index(lines, anchor_line), "manual_line"

    anchor_after = str(_value(override, "anchor_after", default="") or "").strip()
    anchor_text = str(_value(override, "anchor_text", default="") or "").strip()
    for requested in (anchor_after, anchor_text):
        if not requested:
            continue
        target = _normalise_text(requested)
        for index, raw_line in enumerate(lines):
            if target and _normalise_text(raw_line) == target:
                return _section_end_index(lines, index), "manual_text"

    anchor_heading = str(_value(override, "anchor_heading", default="") or "").strip()
    if anchor_heading:
        target = _normalise_text(anchor_heading)
        for index, raw_line in enumerate(lines):
            match = re.match(r"^\s*#{2,3}\s+(.+?)\s*$", raw_line)
            if match and _normalise_text(match.group(1)) == target:
                body_index = _first_body_line_after_heading(lines, index)
                if body_index != index:
                    return _section_end_index(lines, body_index), "manual_heading"
    return None


def _find_product_anchor(
    markdown: str,
    product_name: str,
    product_url: str,
    override: object | None = None,
) -> tuple[int, str, str, str] | None:
    lines = (markdown or "").splitlines()
    first_h2_index = next(
        (
            index
            for index, raw_line in enumerate(lines)
            if re.match(r"^\s*##\s+\S", raw_line)
        ),
        len(lines),
    )
    if override is not None:
        manual = _find_manual_anchor(lines, override)
        if manual and manual[0] >= first_h2_index:
            index, match_kind = manual
            return index, _visible_markdown(lines[index]), _nearest_heading(lines, index), match_kind

    url = product_url.strip()
    name = _normalise_text(product_name)
    matches: list[tuple[int, int, str]] = []
    introduction_matches: list[tuple[int, int, str]] = []
    faq_index = next(
        (
            index
            for index, raw_line in enumerate(lines)
            if re.match(r"^\s*##\s+(.+?)\s*$", raw_line)
            and _is_faq_heading(re.sub(r"^\s*##\s+", "", raw_line))
        ),
        len(lines),
    )
    for index, raw_line in enumerate(lines):
        if index >= faq_index:
            break
        if not raw_line.strip():
            continue
        is_heading = bool(re.match(r"^\s*#{1,6}\s+", raw_line))
        target = introduction_matches if index < first_h2_index else matches
        if url and url in raw_line:
            target.append((0 if not is_heading else 2, index, "product_url"))
        elif name and name in _normalise_text(raw_line):
            target.append((1 if not is_heading else 3, index, "product_name"))

    if not matches:
        if not introduction_matches or first_h2_index == len(lines):
            return None
        _, _, match_kind = min(introduction_matches)
        index = _first_body_line_after_heading(lines, first_h2_index)
        if index == first_h2_index:
            return None
    else:
        _, index, match_kind = min(matches)
    if re.match(r"^\s*#{1,6}\s+", lines[index]):
        body_index = _first_body_line_after_heading(lines, index)
        if body_index == index:
            return None
        index = body_index
    index = _section_end_index(lines, index)
    anchor_text = _anchor_text_at(lines, index)
    return index, anchor_text, _nearest_heading(lines, index), match_kind


def _existing_image_overrides(task: object) -> tuple[object | None, list[object]]:
    hero: object | None = None
    products: list[object] = []
    for image in list(_value(task, "images", default=[]) or []):
        role = str(_value(image, "role", "kind", "type", default="product")).casefold()
        if role == "hero" and hero is None:
            hero = image
        elif role == "product":
            products.append(image)
    return hero, products


def _match_product_override(product: object, overrides: list[object], used: set[int]) -> object | None:
    product_url = str(_value(product, "url", "product_url", default="") or "")
    product_name = str(_value(product, "name", "product_name", default="") or "")
    for index, candidate in enumerate(overrides):
        if index in used:
            continue
        candidate_url = str(_value(candidate, "product_url", default="") or "")
        candidate_name = str(_value(candidate, "product_name", default="") or "")
        if product_url and candidate_url == product_url:
            used.add(index)
            return candidate
        if product_name and candidate_name.casefold() == product_name.casefold():
            used.add(index)
            return candidate
    return None


def find_product_image_anchor(
    markdown: str,
    product_name: str,
    product_url: str,
    override: object | None = None,
) -> tuple[int, str, str, str] | None:
    """Resolve one product anchor without requiring a local image path."""

    return _find_product_anchor(
        markdown,
        product_name,
        product_url,
        override,
    )


def _resolve_source(source: str | Path, task_dir: Path) -> Path:
    raw = Path(str(source)).expanduser()
    if raw.is_absolute():
        resolved = raw.resolve()
    else:
        resolved = (task_dir / raw).resolve()
        try:
            resolved.relative_to(task_dir.resolve())
        except ValueError as exc:
            raise ImageValidationError(f"相对图片路径不能越过任务目录：{source}") from exc

    if not resolved.exists() or not resolved.is_file():
        raise ImageValidationError(f"图片文件不存在：{resolved}")
    return resolved


def _unique_webp_filename(stem: str, used: set[str]) -> str:
    candidate = f"{stem}.webp"
    suffix = 2
    while candidate.casefold() in used:
        candidate = f"{stem}-{suffix}.webp"
        suffix += 1
    used.add(candidate.casefold())
    return candidate


def _safe_destination(images_dir: Path, filename: str) -> Path:
    images_dir = images_dir.resolve()
    destination = (images_dir / filename).resolve()
    try:
        destination.relative_to(images_dir)
    except ValueError as exc:
        raise ImageValidationError(f"非法图片输出文件名：{filename}") from exc
    return destination


def _load_pillow():
    try:
        from PIL import Image, ImageOps, UnidentifiedImageError
    except ImportError as exc:
        raise ImageDependencyError(
            "图片准备需要 Pillow。请先安装 backend/requirements.txt 后重试。"
        ) from exc
    return Image, ImageOps, UnidentifiedImageError


def _image_fingerprint(path: str | Path) -> _ImageFingerprint:
    """Return an exact SHA256 plus a small visual fingerprint for ``path``.

    The visual component catches the same picture saved with another encoding or
    pixel size.  It combines a grayscale difference hash with a normalized RGB
    sample so that unrelated flat/white-background images are not treated as
    duplicates merely because their difference hashes look alike.
    """

    Image, ImageOps, UnidentifiedImageError = _load_pillow()
    image_path = Path(path)
    try:
        data = image_path.read_bytes()
        with Image.open(image_path) as opened:
            if getattr(opened, "is_animated", False):
                opened.seek(0)
            frame = ImageOps.exif_transpose(opened)
            frame.load()
            rgb = frame.convert("RGB")
            visual = rgb.resize(
                (_VISUAL_SAMPLE_SIZE, _VISUAL_SAMPLE_SIZE),
                Image.Resampling.LANCZOS,
            )
            grayscale = rgb.resize((9, 8), Image.Resampling.LANCZOS).convert("L")
            grayscale_pixels = list(grayscale.tobytes())
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ImageValidationError(f"图片损坏或格式不受支持：{image_path}") from exc

    difference_hash = 0
    for row in range(8):
        row_start = row * 9
        for column in range(8):
            difference_hash = (difference_hash << 1) | int(
                grayscale_pixels[row_start + column]
                > grayscale_pixels[row_start + column + 1]
            )

    return _ImageFingerprint(
        sha256=hashlib.sha256(data).hexdigest(),
        difference_hash=difference_hash,
        visual_sample=visual.tobytes(),
    )


def _same_image(left: _ImageFingerprint, right: _ImageFingerprint) -> bool:
    if left.sha256 == right.sha256:
        return True
    if (left.difference_hash ^ right.difference_hash).bit_count() > _PERCEPTUAL_HASH_MAX_DISTANCE:
        return False
    if len(left.visual_sample) != len(right.visual_sample) or not left.visual_sample:
        return False

    squared_error = sum(
        (left_value - right_value) ** 2
        for left_value, right_value in zip(left.visual_sample, right.visual_sample)
    )
    mean_squared_error = squared_error / len(left.visual_sample)
    return mean_squared_error <= _VISUAL_RMS_MAX_DIFFERENCE**2


def _matching_fingerprint_index(
    candidate: _ImageFingerprint,
    fingerprints: list[_ImageFingerprint],
) -> int | None:
    for index, existing in enumerate(fingerprints):
        if _same_image(candidate, existing):
            return index
    return None


def convert_to_webp(source: str | Path, destination: str | Path) -> Path:
    """Validate ``source`` with Pillow and atomically write a verified WebP image."""

    Image, ImageOps, UnidentifiedImageError = _load_pillow()
    source_path = Path(source)
    destination_path = Path(destination)
    destination_path.parent.mkdir(parents=True, exist_ok=True)

    temporary = destination_path.with_name(
        f".{destination_path.stem}.{uuid.uuid4().hex}.tmp.webp"
    )
    try:
        try:
            with Image.open(source_path) as probe:
                probe.verify()
            with Image.open(source_path) as opened:
                if getattr(opened, "is_animated", False):
                    opened.seek(0)
                frame = ImageOps.exif_transpose(opened)
                frame.load()
                has_alpha = "A" in frame.getbands()
                converted = frame.convert("RGBA" if has_alpha else "RGB")
                converted.save(
                    temporary,
                    format="WEBP",
                    quality=90,
                    method=6,
                    lossless=has_alpha,
                )
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise ImageValidationError(f"图片损坏或格式不受支持：{source_path}") from exc

        try:
            with Image.open(temporary) as generated:
                if generated.format != "WEBP":
                    raise ImageValidationError(f"WebP 转换验证失败：{destination_path}")
                generated.verify()
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            if isinstance(exc, ImageValidationError):
                raise
            raise ImageValidationError(f"WebP 转换验证失败：{destination_path}") from exc

        temporary.replace(destination_path)
    finally:
        temporary.unlink(missing_ok=True)
    return destination_path


def image_pixel_size(path: str | Path) -> tuple[int, int]:
    """Read a validated image's pixel size through Pillow."""

    Image, _, UnidentifiedImageError = _load_pillow()
    image_path = Path(path)
    try:
        with Image.open(image_path) as opened:
            opened.verify()
        with Image.open(image_path) as opened:
            width, height = opened.size
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ImageValidationError(f"图片损坏或格式不受支持：{image_path}") from exc
    if width <= 0 or height <= 0:
        raise ImageValidationError(f"图片尺寸无效：{image_path}")
    return width, height


def _prepared_image(
    *,
    image_id: str,
    role: str,
    source_path: Path,
    destination: Path,
    product_name: str = "",
    product_url: str = "",
    anchor: tuple[int, str, str, str] | None = None,
    candidates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": image_id,
        "role": role,
        "source_path": str(source_path),
        "prepared_path": str(destination),
        "filename": destination.name,
        "marker": f"img.{destination.name}",
        "product_name": product_name,
        "product_url": product_url,
        "anchor_heading": "",
        "anchor_text": "",
        "anchor_after": "",
        "status": "ready",
        "error": "",
    }
    if role == "hero":
        result["anchor_after"] = "before_first_h2"
        return result

    if anchor:
        line_index, anchor_text, anchor_heading, match_kind = anchor
        result.update(
            {
                "anchor_heading": anchor_heading,
                "anchor_text": anchor_text,
                "anchor_after": anchor_text,
                "anchor_line": line_index,
                "anchor_match": match_kind,
            }
        )
    else:
        result.update(
            {
                "status": "needs_anchor",
                "error": "正文中未找到对应产品名或产品链接，请选择一个 H2/H3 锚点。",
                "anchor_candidates": candidates or [],
            }
        )
    return result


def prepare_task_images(
    task: object,
    article: str | None = None,
    *,
    require_hero: bool = False,
) -> list[dict[str, Any]]:
    """Prepare a task's hero/product images as validated, structured WebP records.

    Product images that cannot be located in the article are returned with
    ``status='needs_anchor'`` and H2/H3 choices. Export remains blocked until a
    caller supplies ``anchor_heading``/``anchor_text``/``anchor_after``.
    """

    task_dir_value = str(_value(task, "task_dir", default="") or "").strip()
    if not task_dir_value:
        raise ImageValidationError("任务缺少 task_dir，无法准备图片。")
    task_dir = Path(task_dir_value).resolve()
    images_dir = task_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    markdown = article if article is not None else str(
        _value(
            task,
            "final_article",
            "linked_article",
            "humanized_article",
            "initial_article",
            "article",
            default="",
        )
        or ""
    )
    selected_title = str(_value(task, "selected_title", default="") or "").strip()
    title = extract_article_title(markdown) or selected_title or str(
        _value(task, "topic", default="Article")
    )

    hero_override, product_overrides = _existing_image_overrides(task)
    hero_source = str(_value(task, "hero_image", default="") or "").strip()
    if not hero_source and hero_override is not None:
        hero_source = str(
            _value(hero_override, "source_path", "prepared_path", "image_path", default="") or ""
        ).strip()

    if require_hero and not hero_source:
        raise ImageValidationError("未设置首图 hero_image，不能进入图片完成或导出阶段。")

    used_filenames: set[str] = set()
    prepared: list[dict[str, Any]] = []
    fingerprints: list[_ImageFingerprint] = []
    candidates = article_anchor_candidates(markdown)

    if hero_source:
        source_path = _resolve_source(hero_source, task_dir)
        source_fingerprint = _image_fingerprint(source_path)
        filename = _unique_webp_filename(sanitize_image_stem(title, fallback="article"), used_filenames)
        destination = _safe_destination(images_dir, filename)
        convert_to_webp(source_path, destination)
        prepared.append(
            _prepared_image(
                image_id="hero",
                role="hero",
                source_path=source_path,
                destination=destination,
            )
        )
        fingerprints.append(source_fingerprint)

    used_overrides: set[int] = set()
    products = list(_value(task, "products", default=[]) or [])
    for index, product in enumerate(products, start=1):
        if len(prepared) >= MAX_ARTICLE_IMAGES:
            break
        override = _match_product_override(product, product_overrides, used_overrides)
        source_value = str(_value(product, "image_path", "source_path", default="") or "").strip()
        if override is not None:
            source_value = str(
                _value(override, "source_path", "prepared_path", "image_path", default=source_value)
                or source_value
            ).strip()
        if not source_value:
            continue

        product_name = str(_value(product, "name", "product_name", default="") or "").strip()
        product_url = str(_value(product, "url", "product_url", default="") or "").strip()
        source_path = _resolve_source(source_value, task_dir)
        source_fingerprint = _image_fingerprint(source_path)
        if _matching_fingerprint_index(source_fingerprint, fingerprints) is not None:
            continue
        stem = sanitize_image_stem(product_name or source_path.stem, fallback=f"product-{index}")
        filename = _unique_webp_filename(stem, used_filenames)
        destination = _safe_destination(images_dir, filename)
        convert_to_webp(source_path, destination)
        anchor = _find_product_anchor(markdown, product_name, product_url, override)
        prepared.append(
            _prepared_image(
                image_id=f"product-{index}",
                role="product",
                source_path=source_path,
                destination=destination,
                product_name=product_name,
                product_url=product_url,
                anchor=anchor,
                candidates=candidates,
            )
        )
        fingerprints.append(source_fingerprint)

    next_index = len(products) + 1
    for override_index, override in enumerate(product_overrides):
        if len(prepared) >= MAX_ARTICLE_IMAGES:
            break
        if override_index in used_overrides:
            continue
        source_value = str(
            _value(override, "source_path", "prepared_path", "image_path", default="") or ""
        ).strip()
        if not source_value:
            continue
        product_name = str(_value(override, "product_name", default="") or "").strip()
        product_url = str(_value(override, "product_url", default="") or "").strip()
        source_path = _resolve_source(source_value, task_dir)
        source_fingerprint = _image_fingerprint(source_path)
        if _matching_fingerprint_index(source_fingerprint, fingerprints) is not None:
            continue
        stem = sanitize_image_stem(
            product_name or source_path.stem,
            fallback=f"product-{next_index}",
        )
        filename = _unique_webp_filename(stem, used_filenames)
        destination = _safe_destination(images_dir, filename)
        convert_to_webp(source_path, destination)
        anchor = _find_product_anchor(markdown, product_name, product_url, override)
        prepared.append(
            _prepared_image(
                image_id=str(_value(override, "id", default=f"product-{next_index}")),
                role="product",
                source_path=source_path,
                destination=destination,
                product_name=product_name,
                product_url=product_url,
                anchor=anchor,
                candidates=candidates,
            )
        )
        fingerprints.append(source_fingerprint)
        next_index += 1

    if product_overrides:
        hero_items = [item for item in prepared if item.get("role") == "hero"]
        body_items = [item for item in prepared if item.get("role") != "hero"]

        def override_slot(item: dict[str, Any]) -> int:
            item_url = str(item.get("product_url") or "").strip()
            item_name = str(item.get("product_name") or "").strip().casefold()
            item_source = str(item.get("source_path") or "").strip().casefold()
            for slot, override in enumerate(product_overrides):
                override_url = str(_value(override, "product_url", default="") or "").strip()
                override_name = str(
                    _value(override, "product_name", default="") or ""
                ).strip().casefold()
                override_source = str(
                    _value(
                        override,
                        "source_path",
                        "prepared_path",
                        "image_path",
                        default="",
                    )
                    or ""
                ).strip().casefold()
                if item_url and override_url == item_url:
                    return slot
                if item_name and override_name == item_name:
                    return slot
                if item_source and override_source == item_source:
                    return slot
            return len(product_overrides) + body_items.index(item)

        prepared = [*hero_items, *sorted(body_items, key=override_slot)]

    return prepared


def _normalise_prepared_image(image: object) -> dict[str, Any]:
    prepared_path = str(
        _value(image, "prepared_path", "webp_path", "path", "image_path", default="") or ""
    ).strip()
    filename = str(_value(image, "filename", default="") or "").strip()
    if prepared_path:
        actual_name = Path(prepared_path).name
        filename = actual_name
    role = str(_value(image, "role", "kind", "type", default="product") or "product").casefold()
    return {
        "id": str(_value(image, "id", default=role) or role),
        "role": role,
        "source_path": str(_value(image, "source_path", default="") or ""),
        "prepared_path": prepared_path,
        "prepared_asset_id": str(
            _value(image, "prepared_asset_id", default="") or ""
        ),
        "prepared_content_hash": str(
            _value(image, "prepared_content_hash", default="") or ""
        ).casefold(),
        "width": _value(image, "width", default=None),
        "height": _value(image, "height", default=None),
        "filename": filename,
        "marker": f"img.{filename}" if filename else "",
        "product_name": str(_value(image, "product_name", default="") or ""),
        "product_url": str(_value(image, "product_url", default="") or ""),
        "anchor_heading": str(_value(image, "anchor_heading", default="") or ""),
        "anchor_text": str(_value(image, "anchor_text", default="") or ""),
        "anchor_after": str(_value(image, "anchor_after", default="") or ""),
        "anchor_line": _value(image, "anchor_line", default=None),
        "status": str(_value(image, "status", default="ready") or "ready"),
        "error": str(_value(image, "error", default="") or ""),
        "anchor_candidates": list(_value(image, "anchor_candidates", default=[]) or []),
    }


def _validate_prepared_image_set(images: list[dict[str, Any]]) -> None:
    """Reject legacy/manual prepared image sets that bypassed preparation rules."""

    if len(images) > MAX_ARTICLE_IMAGES:
        raise ImageValidationError(
            f"每篇文章最多使用 {MAX_ARTICLE_IMAGES} 张图片（包含首图）；当前有 {len(images)} 张。"
        )

    fingerprints: list[_ImageFingerprint] = []
    labels: list[str] = []
    for image in images:
        path = Path(image["prepared_path"])
        label = str(image["filename"] or image["id"] or path.name or "image")
        if not image["prepared_path"] or not path.exists() or not path.is_file():
            raise ImageValidationError(
                f"准备后的图片不存在：{image['prepared_path'] or image['filename'] or image['id']}"
            )
        if path.suffix.casefold() != ".webp" or image["filename"].casefold() != path.name.casefold():
            raise ImageValidationError(f"准备后的图片必须是实际 .webp 文件：{path}")

        fingerprint = _image_fingerprint(path)
        duplicate_index = _matching_fingerprint_index(fingerprint, fingerprints)
        if duplicate_index is not None:
            raise ImageValidationError(
                f"图片内容重复：{label} 与 {labels[duplicate_index]}。每篇文章不能使用重复图片。"
            )
        fingerprints.append(fingerprint)
        labels.append(label)


def _validate_prepared_asset_image_set(
    images: list[dict[str, Any]],
) -> None:
    """Validate Server image identities without accepting local file paths."""

    if not images:
        raise ImageValidationError(
            "Server DOCX export requires prepared article image assets."
        )
    if len(images) > MAX_ARTICLE_IMAGES:
        raise ImageValidationError(
            f"每篇文章最多使用 {MAX_ARTICLE_IMAGES} 张图片（包含首图）；当前有 {len(images)} 张。"
        )

    asset_ids: set[str] = set()
    content_hashes: set[str] = set()
    for image in images:
        asset_id = str(image["prepared_asset_id"] or "").strip()
        content_digest = str(
            image["prepared_content_hash"] or ""
        ).strip()
        filename = str(image["filename"] or "").strip()
        width = image["width"]
        height = image["height"]
        if image["prepared_path"]:
            raise ImageValidationError(
                "Server article images must not contain local prepared paths."
            )
        if (
            not asset_id
            or not re.fullmatch(r"[0-9a-f]{64}", content_digest)
            or not filename
            or Path(filename).name != filename
            or Path(filename).suffix.casefold() != ".webp"
            or not isinstance(width, int)
            or isinstance(width, bool)
            or width <= 0
            or not isinstance(height, int)
            or isinstance(height, bool)
            or height <= 0
        ):
            raise ImageValidationError(
                "Server article image asset metadata is incomplete."
            )
        if asset_id in asset_ids or content_digest in content_hashes:
            raise ImageValidationError(
                "Server article image assets must be unique."
            )
        asset_ids.add(asset_id)
        content_hashes.add(content_digest)


def _has_transition_before_first_h2(markdown: str) -> tuple[int, int]:
    lines = (markdown or "").splitlines()
    h1_indices = [index for index, line in enumerate(lines) if re.match(r"^\s*#\s+\S", line)]
    h2_indices = [index for index, line in enumerate(lines) if re.match(r"^\s*##\s+\S", line)]
    if len(h1_indices) != 1:
        raise ArticleImageError("插入首图前，正文必须包含且只包含一个 H1。")
    if not h2_indices:
        raise ArticleImageError("插入首图前，正文至少需要一个 H2。")
    h1_index = h1_indices[0]
    h2_index = h2_indices[0]
    if h2_index <= h1_index:
        raise ArticleImageError("第一个 H2 必须位于 H1 之后。")

    for raw_line in lines[h1_index + 1 : h2_index]:
        stripped = raw_line.strip()
        if not stripped or stripped in {"---", "***", "___"}:
            continue
        if re.match(r"^(?:#{1,6}\s+|[-+*]\s+|\d+[.)]\s+|\|)", stripped):
            continue
        if re.match(r"^(?:!\[|img\.)", stripped, re.IGNORECASE):
            continue
        return h1_index, h2_index
    raise ArticleImageError(
        "H1 与第一个 H2 之间没有过渡段。请先生成并确认过渡段，再准备首图。"
    )


def validate_hero_image_placement(markdown: str) -> None:
    """Validate the structural slot used by both local and server images."""

    _has_transition_before_first_h2(markdown)


def _resolve_normalised_image_placements(
    markdown: str,
    normalised: list[dict[str, Any]],
) -> list[ImagePlacement]:
    lines = (markdown or "").splitlines()
    placements: list[ImagePlacement] = []
    unresolved: list[dict[str, Any]] = []
    first_h2_index: int | None = None

    for image in normalised:
        if image["role"] == "hero":
            if first_h2_index is None:
                _, first_h2_index = _has_transition_before_first_h2(markdown)
            placements.append(ImagePlacement(image=image, position="before", line_index=first_h2_index))
            continue

        anchor = _find_product_anchor(
            markdown,
            image["product_name"],
            image["product_url"],
            image,
        )
        if anchor is None:
            image["status"] = "needs_anchor"
            image["error"] = "正文中未找到对应产品名或产品链接，请选择一个 H2/H3 锚点。"
            image["anchor_candidates"] = article_anchor_candidates(markdown)
            unresolved.append(image)
            continue
        line_index, anchor_text, anchor_heading, _ = anchor
        image["anchor_text"] = anchor_text
        image["anchor_after"] = anchor_text
        image["anchor_heading"] = anchor_heading
        placements.append(ImagePlacement(image=image, position="after", line_index=line_index))

    if unresolved:
        raise ImageAnchorRequiredError(unresolved)
    return placements


def resolve_image_placements(
    markdown: str,
    images: Iterable[object],
) -> list[ImagePlacement]:
    """Resolve local prepared files to deterministic Markdown positions."""

    normalised = [_normalise_prepared_image(image) for image in images]
    if not normalised:
        return []
    _validate_prepared_image_set(normalised)
    return _resolve_normalised_image_placements(markdown, normalised)


def resolve_asset_image_placements(
    markdown: str,
    images: Iterable[object],
) -> list[ImagePlacement]:
    """Resolve trusted Server Asset identities without filesystem fallback."""

    normalised = [_normalise_prepared_image(image) for image in images]
    _validate_prepared_asset_image_set(normalised)
    return _resolve_normalised_image_placements(markdown, normalised)


def build_image_audit_markdown(markdown: str, images: Iterable[object]) -> str:
    """Return Markdown with deterministic image previews and exact marker lines.

    This is an audit artifact for ``07_final_with_images.md``. DOCX export uses
    the same placement resolver directly, so the audit order and Word order
    cannot silently drift apart.
    """

    placements = resolve_image_placements(markdown, images)
    before: dict[int, list[dict[str, Any]]] = {}
    after: dict[int, list[dict[str, Any]]] = {}
    for placement in placements:
        target = before if placement.position == "before" else after
        target.setdefault(placement.line_index, []).append(placement.image)

    markers = {placement.image["marker"] for placement in placements}
    filenames = {placement.image["filename"] for placement in placements}
    output: list[str] = []

    def append_image_block(image: Mapping[str, Any]) -> None:
        if output and output[-1].strip():
            output.append("")
        alt = str(image.get("product_name") or image.get("filename") or "Article image")
        alt = alt.replace("[", "(").replace("]", ")")
        output.append(f"![{alt}](images/{quote(str(image['filename']))})")
        output.append(str(image["marker"]))
        output.append("")

    for line_index, raw_line in enumerate((markdown or "").splitlines()):
        for image in before.get(line_index, []):
            append_image_block(image)

        stripped = raw_line.strip()
        is_generated_preview = stripped.startswith("![") and any(
            filename in stripped or quote(filename) in stripped for filename in filenames
        )
        if stripped not in markers and not is_generated_preview:
            output.append(raw_line)

        for image in after.get(line_index, []):
            append_image_block(image)

    return "\n".join(output).rstrip() + "\n"


# Backward-friendly descriptive alias for callers added by the workflow API batch.
prepare_article_images = prepare_task_images
