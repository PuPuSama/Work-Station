from __future__ import annotations

import hashlib
import warnings
from collections.abc import Mapping
from dataclasses import dataclass
from io import BytesIO
from typing import Protocol

from PIL import Image, ImageOps, UnidentifiedImageError

from knowledge_agent.assets import KnowledgeAsset
from knowledge_agent.object_storage import ProjectKnowledgeObject
from models import (
    STATUS_IMAGES_READY,
    ArticleImage,
    ArticleVersion,
    TaskRecord,
)
from services.access_control import ActorIdentity
from services.article_images import (
    MAX_ARTICLE_IMAGES,
    ArticleImageError,
    article_anchor_candidates,
    find_product_image_anchor,
    sanitize_image_stem,
    validate_hero_image_placement,
)
from services.article_validation import visible_word_count
from storage import content_hash, now_iso
from workflow.state_machine import invalidate_downstream, transition_task


MAX_SERVER_SOURCE_IMAGE_BYTES = 12 * 1024 * 1024
MAX_SERVER_IMAGE_PIXELS = 40_000_000
_PERCEPTUAL_HASH_MAX_DISTANCE = 4
_VISUAL_SAMPLE_SIZE = 32
_VISUAL_RMS_MAX_DIFFERENCE = 6.0


class ServerArticleImageError(ValueError):
    """A private asset cannot safely become an article image."""


class ServerArticleImageAnchorRequired(ServerArticleImageError):
    """One or more selected product images need an explicit article anchor."""

    def __init__(self, unresolved: list[dict[str, object]]) -> None:
        self.unresolved = tuple(dict(item) for item in unresolved)
        super().__init__(
            "one or more product images require an explicit article anchor"
        )


@dataclass(frozen=True, slots=True)
class DerivedWebp:
    data: bytes
    content_hash: str
    width: int
    height: int
    difference_hash: int
    visual_sample: bytes


@dataclass(frozen=True, slots=True)
class _Candidate:
    role: str
    source_asset_id: str
    derived: DerivedWebp
    product_id: str = ""
    product_name: str = ""
    product_url: str = ""
    anchor: tuple[int, str, str, str] | None = None


class ServerArticleImageObjectService(Protocol):
    """Private-object operations required by server image preparation."""

    def read_for_article_edit(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        asset_id: str,
        max_bytes: int,
    ) -> ProjectKnowledgeObject: ...

    def upload_article_derivative(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        asset_id: str,
        data: bytes,
        width: int,
        height: int,
        metadata: dict[str, object] | None = None,
    ) -> KnowledgeAsset: ...


def _difference_hash(image: Image.Image) -> int:
    sample = image.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    flattened = getattr(sample, "get_flattened_data", None)
    pixels = list(flattened() if callable(flattened) else sample.getdata())
    value = 0
    for row in range(8):
        offset = row * 9
        for column in range(8):
            value = (value << 1) | int(
                pixels[offset + column]
                > pixels[offset + column + 1]
            )
    return value


def _visual_sample(image: Image.Image) -> bytes:
    return image.convert("RGB").resize(
        (_VISUAL_SAMPLE_SIZE, _VISUAL_SAMPLE_SIZE),
        Image.Resampling.LANCZOS,
    ).tobytes()


def derive_webp(source: bytes) -> DerivedWebp:
    """Validate raster bytes and deterministically create one metadata-free WebP."""

    body = bytes(source)
    if not body or len(body) > MAX_SERVER_SOURCE_IMAGE_BYTES:
        raise ServerArticleImageError(
            "source asset is not a supported article image"
        )
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(body)) as probe:
                probe.verify()
            with Image.open(BytesIO(body)) as opened:
                source_width, source_height = opened.size
                if (
                    source_width <= 0
                    or source_height <= 0
                    or source_width * source_height
                    > MAX_SERVER_IMAGE_PIXELS
                ):
                    raise ValueError("invalid dimensions")
                if getattr(opened, "is_animated", False):
                    opened.seek(0)
                frame = ImageOps.exif_transpose(opened)
                frame.load()
                width, height = frame.size
                if (
                    width <= 0
                    or height <= 0
                    or width * height > MAX_SERVER_IMAGE_PIXELS
                ):
                    raise ValueError("invalid dimensions")
                difference_hash = _difference_hash(frame)
                visual_sample = _visual_sample(frame)
                has_alpha = "A" in frame.getbands()
                converted = frame.convert(
                    "RGBA" if has_alpha else "RGB"
                )
                output = BytesIO()
                converted.save(
                    output,
                    format="WEBP",
                    quality=90,
                    method=6,
                    lossless=has_alpha,
                )
        webp = output.getvalue()
        with Image.open(BytesIO(webp)) as generated:
            if generated.format != "WEBP":
                raise ValueError("unexpected derivative format")
            generated.verify()
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        OSError,
        ValueError,
    ) as exc:
        raise ServerArticleImageError(
            "source asset is not a supported article image"
        ) from exc
    return DerivedWebp(
        data=webp,
        content_hash=hashlib.sha256(webp).hexdigest(),
        width=int(width),
        height=int(height),
        difference_hash=difference_hash,
        visual_sample=visual_sample,
    )


def _is_duplicate(
    derived: DerivedWebp,
    selected: list[_Candidate],
) -> bool:
    for item in selected:
        if derived.content_hash == item.derived.content_hash:
            return True
        if (
            derived.difference_hash ^ item.derived.difference_hash
        ).bit_count() > _PERCEPTUAL_HASH_MAX_DISTANCE:
            continue
        differences = (
            int(left) - int(right)
            for left, right in zip(
                derived.visual_sample,
                item.derived.visual_sample,
                strict=True,
            )
        )
        mean_square = sum(value * value for value in differences) / len(
            derived.visual_sample
        )
        if mean_square**0.5 <= _VISUAL_RMS_MAX_DIFFERENCE:
            return True
    return False


def _unique_filename(stem: str, used: set[str]) -> str:
    base = sanitize_image_stem(stem, fallback="article-image")
    candidate = f"{base}.webp"
    suffix = 2
    while candidate.casefold() in used:
        candidate = f"{base}-{suffix}.webp"
        suffix += 1
    used.add(candidate.casefold())
    return candidate


class ServerArticleImagePreparation:
    """Prepare private source assets without creating server-local task files."""

    def __init__(self, objects: ServerArticleImageObjectService) -> None:
        self._objects = objects

    def _read_derived(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        asset_id: str,
        cache: dict[str, DerivedWebp],
    ) -> DerivedWebp:
        cached = cache.get(asset_id)
        if cached is not None:
            return cached
        source = self._objects.read_for_article_edit(
            actor=actor,
            project_id=project_id,
            asset_id=asset_id,
            max_bytes=MAX_SERVER_SOURCE_IMAGE_BYTES,
        )
        if not source.asset.content_type.casefold().startswith("image/"):
            raise ServerArticleImageError(
                "source asset is not a supported article image"
            )
        derived = derive_webp(source.data)
        cache[asset_id] = derived
        return derived

    def prepare(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        task: TaskRecord,
        hero_asset_id: str,
        product_anchors: Mapping[str, str] | None = None,
    ) -> TaskRecord:
        article = task.linked_article or task.humanized_article
        source_hero_id = hero_asset_id.strip()
        if not article.strip() or not source_hero_id:
            raise ServerArticleImageError(
                "article and hero asset are required"
            )
        try:
            validate_hero_image_placement(article)
        except ArticleImageError as exc:
            raise ServerArticleImageError(str(exc)) from exc

        selected: list[_Candidate] = []
        cache: dict[str, DerivedWebp] = {}
        manual_anchors = {
            str(product_id).strip(): str(heading).strip()
            for product_id, heading in dict(
                product_anchors or {}
            ).items()
        }
        selectable_product_ids = {
            product.product_id for product in task.products
        }
        if (
            any(
                not product_id or not heading
                for product_id, heading in manual_anchors.items()
            )
            or not set(manual_anchors).issubset(
                selectable_product_ids
            )
        ):
            raise ServerArticleImageError(
                "product image anchors do not match the selected products"
            )
        hero = self._read_derived(
            actor=actor,
            project_id=project_id,
            asset_id=source_hero_id,
            cache=cache,
        )
        selected.append(
            _Candidate(
                role="hero",
                source_asset_id=source_hero_id,
                derived=hero,
            )
        )

        for product in task.products:
            if len(selected) >= MAX_ARTICLE_IMAGES:
                break
            source_asset_id = product.selected_asset_id.strip()
            if not source_asset_id:
                continue
            derived = self._read_derived(
                actor=actor,
                project_id=project_id,
                asset_id=source_asset_id,
                cache=cache,
            )
            if _is_duplicate(derived, selected):
                continue
            selected.append(
                _Candidate(
                    role="product",
                    source_asset_id=source_asset_id,
                    derived=derived,
                    product_id=product.product_id,
                    product_name=product.name,
                    product_url=product.url or product.canonical_url,
                )
            )

        unresolved: list[dict[str, object]] = []
        anchored: list[_Candidate] = []
        anchors = article_anchor_candidates(article)
        for candidate in selected:
            if candidate.role == "hero":
                anchored.append(candidate)
                continue
            anchor = find_product_image_anchor(
                article,
                candidate.product_name,
                candidate.product_url,
                (
                    {
                        "anchor_heading": manual_anchors[
                            candidate.product_id
                        ]
                    }
                    if candidate.product_id in manual_anchors
                    else None
                ),
            )
            if anchor is None:
                unresolved.append(
                    {
                        "product_id": candidate.product_id,
                        "product_name": candidate.product_name,
                        "anchor_candidates": anchors,
                    }
                )
                continue
            anchored.append(
                _Candidate(
                    role=candidate.role,
                    source_asset_id=candidate.source_asset_id,
                    derived=candidate.derived,
                    product_id=candidate.product_id,
                    product_name=candidate.product_name,
                    product_url=candidate.product_url,
                    anchor=anchor,
                )
            )
        if unresolved:
            raise ServerArticleImageAnchorRequired(unresolved)

        filenames: set[str] = set()
        images: list[ArticleImage] = []
        for index, candidate in enumerate(anchored):
            if candidate.role == "hero":
                filename_stem = task.selected_title or task.topic or "article"
            else:
                filename_stem = (
                    candidate.product_name or f"product-{index}"
                )
            filename = _unique_filename(
                filename_stem,
                filenames,
            )
            derived_asset = self._objects.upload_article_derivative(
                actor=actor,
                project_id=project_id,
                asset_id=f"asset_{candidate.derived.content_hash}",
                data=candidate.derived.data,
                width=candidate.derived.width,
                height=candidate.derived.height,
                metadata={
                    "difference_hash": (
                        f"{candidate.derived.difference_hash:016x}"
                    ),
                },
            )
            anchor = candidate.anchor
            images.append(
                ArticleImage(
                    id=(
                        "hero"
                        if candidate.role == "hero"
                        else f"product-{index}"
                    ),
                    role=candidate.role,
                    source_asset_id=candidate.source_asset_id,
                    prepared_asset_id=derived_asset.asset_id,
                    prepared_content_hash=derived_asset.content_hash,
                    width=candidate.derived.width,
                    height=candidate.derived.height,
                    filename=filename,
                    marker=f"img.{filename}",
                    product_name=candidate.product_name,
                    product_url=candidate.product_url,
                    anchor_heading="" if anchor is None else anchor[2],
                    anchor_text="" if anchor is None else anchor[1],
                    anchor_after=(
                        "before_first_h2"
                        if candidate.role == "hero"
                        else "" if anchor is None else anchor[1]
                    ),
                    status="ready",
                    anchor_line=None if anchor is None else anchor[0],
                    anchor_match="" if anchor is None else anchor[3],
                )
            )

        invalidate_downstream(task, "images")
        task.hero_image = ""
        task.images = images
        task.final_article = article
        task.final_article_word_count = visible_word_count(article)
        task.final_article_hash = content_hash(article)
        task.article = article
        version = ArticleVersion(
            kind="final",
            content=article,
            word_count=task.final_article_word_count,
            content_hash=task.final_article_hash,
            created_at=now_iso(),
            source_kind="server_asset_derivative",
        )
        if (
            not task.article_versions
            or task.article_versions[-1].kind != version.kind
            or task.article_versions[-1].content_hash
            != version.content_hash
            or task.article_versions[-1].source_kind
            != version.source_kind
        ):
            task.article_versions.append(version)
        transition_task(task, STATUS_IMAGES_READY)
        return task


__all__ = [
    "DerivedWebp",
    "MAX_SERVER_SOURCE_IMAGE_BYTES",
    "ServerArticleImageAnchorRequired",
    "ServerArticleImageError",
    "ServerArticleImageObjectService",
    "ServerArticleImagePreparation",
    "derive_webp",
]
