"""Deterministic admission checks for assets extracted from private sources.

Private document assets are evidence attached to a trusted source snapshot. A
technical drawing may therefore be useful in the Knowledge Library even when
it is not suitable as an article hero image. The checks in this module keep
that distinction explicit: source publication can proceed for clean assets,
while malformed or ambiguous images leave the snapshot in the review queue.
"""

from dataclasses import dataclass
from io import BytesIO
import re
from typing import Literal, Mapping, Sequence

from PIL import Image, UnidentifiedImageError


AssetReviewDecision = Literal["approve", "needs_review", "reject"]
AssetRole = Literal["product_image", "technical_illustration", "attachment"]

MIN_IMAGE_WIDTH = 180
MIN_IMAGE_HEIGHT = 120
MAX_IMAGE_PIXELS = 40_000_000
SUPPORTED_RASTER_FORMATS = frozenset({"JPEG", "PNG", "WEBP"})
VECTOR_CONTENT_TYPES = frozenset(
    {
        "image/emf",
        "image/svg+xml",
        "image/wmf",
        "image/x-emf",
        "image/x-wmf",
    }
)
TECHNICAL_HINT_PATTERN = re.compile(
    r"(?:assembly|blueprint|diagram|dimension|drawing|installation|schematic|"
    r"spec(?:ification)?|structure|technical|安装|尺寸|结构|示意|技术|图纸)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class AssetReview:
    decision: AssetReviewDecision
    role: AssetRole
    reason: str
    width: int | None = None
    height: int | None = None
    image_format: str | None = None

    def to_metadata(self) -> dict[str, object]:
        return {
            "decision": self.decision,
            "role": self.role,
            "reason": self.reason,
            "width": self.width,
            "height": self.height,
            "image_format": self.image_format,
            # Private-source images are evidence-bound. A later product-specific
            # workflow must confirm identity before any of them can become Hero.
            "hero_eligible": False,
            "requires_product_confirmation_for_hero": (
                self.role == "product_image" and self.decision == "approve"
            ),
        }


def _role_for(filename: str, metadata: Mapping[str, object] | None) -> AssetRole:
    values = [filename]
    for value in (metadata or {}).values():
        if isinstance(value, str):
            values.append(value)
    return (
        "technical_illustration"
        if TECHNICAL_HINT_PATTERN.search(" ".join(values))
        else "product_image"
    )


def review_embedded_asset(
    *,
    filename: str,
    content: bytes,
    content_type: str,
    metadata: Mapping[str, object] | None = None,
) -> AssetReview:
    """Return a safe, serializable review result without calling an API.

    Semantic product matching is intentionally not guessed for private
    document images: their source snapshot already supplies the evidence
    boundary. They are never selected as article hero images unless a later
    product-specific pipeline verifies them.
    """

    normalized_type = str(content_type or "").strip().casefold()
    role = _role_for(filename, metadata)
    if not normalized_type.startswith("image/"):
        return AssetReview(
            decision="approve",
            role="attachment",
            reason="非图片附件保留为来源证据，不参与图片候选。",
        )

    if normalized_type in VECTOR_CONTENT_TYPES:
        return AssetReview(
            decision="approve",
            role="technical_illustration",
            reason="矢量技术图保留为来源证据，不参与 Hero 首图候选。",
            image_format=normalized_type.removeprefix("image/").upper(),
        )

    try:
        with Image.open(BytesIO(content)) as image:
            width, height = (int(image.width), int(image.height))
            image_format = str(image.format or "").upper() or None
            if width <= 0 or height <= 0:
                raise ValueError("image dimensions are invalid")
            if width * height > MAX_IMAGE_PIXELS:
                return AssetReview(
                    decision="needs_review",
                    role=role,
                    reason="图片像素过高，需要人工确认后再使用。",
                    width=width,
                    height=height,
                    image_format=image_format,
                )
            image.load()
    except (OSError, UnidentifiedImageError, ValueError, Image.DecompressionBombError):
        return AssetReview(
            decision="reject",
            role=role,
            reason="图片无法安全解码。",
        )

    if image_format not in SUPPORTED_RASTER_FORMATS:
        return AssetReview(
            decision="needs_review",
            role=role,
            reason="图片格式不在自动发布白名单内，需要人工确认。",
            width=width,
            height=height,
            image_format=image_format,
        )
    if width < MIN_IMAGE_WIDTH or height < MIN_IMAGE_HEIGHT:
        return AssetReview(
            decision="needs_review",
            role=role,
            reason="图片尺寸过小，需要人工确认是否仅为图标或装饰图。",
            width=width,
            height=height,
            image_format=image_format,
        )

    return AssetReview(
        decision="approve",
        role=role,
        reason=(
            "产品图片通过基础质量检查。"
            if role == "product_image"
            else "技术图通过基础质量检查，保留为证据且不作为 Hero 首图。"
        ),
        width=width,
        height=height,
        image_format=image_format,
    )


def summarize_asset_reviews(
    assets: Sequence[Mapping[str, object]],
    *,
    chunk_count: int,
) -> tuple[AssetReviewDecision, str]:
    """Choose the source-level automatic review decision.

    A parsed source without text cannot be embedded. Any ambiguous or
    malformed embedded image keeps the source in the manual queue, while a
    clean source can be published immediately. This avoids silently dropping
    useful text because one image needs an operator's decision.
    """

    if chunk_count <= 0:
        return "reject", "自动审核拒绝：资料没有可检索的文本段落。"
    decisions: list[str] = []
    for asset in assets:
        review = asset.get("knowledge_asset_review")
        decisions.append(
            str(review.get("decision") or "")
            if isinstance(review, Mapping)
            else ""
        )
    if "reject" in decisions:
        return "needs_review", "自动审核发现无法安全解码的图片，已转人工复核。"
    if "needs_review" in decisions:
        return (
            "approve",
            "自动审核通过：正文可发布；小尺寸或非白名单图片已限制为证据资产，"
            "不会自动用于 Hero 首图。",
        )
    return "approve", "自动审核通过：文本解析和图片基础质量检查均通过。"


__all__ = [
    "AssetReview",
    "AssetReviewDecision",
    "AssetRole",
    "MAX_IMAGE_PIXELS",
    "MIN_IMAGE_HEIGHT",
    "MIN_IMAGE_WIDTH",
    "review_embedded_asset",
    "summarize_asset_reviews",
]
