from __future__ import annotations

import warnings
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from models import TaskRecord


AI_CHECK_STAGES = {"initial", "final"}
MAX_AI_SCREENSHOT_PIXELS = 40_000_000


class AIScreenshotError(ValueError):
    """Raised when an AI-rate screenshot cannot be validated or saved."""


def build_ai_rate_screenshot_png(content: bytes) -> tuple[bytes, int, int]:
    """Validate untrusted image bytes and return a metadata-free PNG."""

    if not content:
        raise AIScreenshotError("The pasted AI-rate screenshot is empty.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter(
                "error",
                Image.DecompressionBombWarning,
            )
            with Image.open(BytesIO(content)) as source:
                source.load()
                image = ImageOps.exif_transpose(source)
                if (
                    image.width * image.height
                    > MAX_AI_SCREENSHOT_PIXELS
                ):
                    raise AIScreenshotError(
                        "The AI-rate screenshot exceeds the pixel limit."
                    )
                if image.mode not in {"RGB", "RGBA"}:
                    image = image.convert(
                        (
                            "RGBA"
                            if "transparency" in source.info
                            else "RGB"
                        )
                    )
                output = BytesIO()
                image.save(output, format="PNG", optimize=True)
                return output.getvalue(), image.width, image.height
    except AIScreenshotError:
        raise
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        OSError,
        ValueError,
    ) as exc:
        raise AIScreenshotError(
            "The pasted file is not a valid image."
        ) from exc


def save_ai_rate_screenshot(
    task: TaskRecord,
    stage: str,
    content: bytes,
) -> Path:
    normalized_stage = stage.strip().lower()
    if normalized_stage not in AI_CHECK_STAGES:
        raise AIScreenshotError("AI check stage must be 'initial' or 'final'.")
    data, _width, _height = build_ai_rate_screenshot_png(content)
    output_dir = Path(task.task_dir) / "ai-rate-screenshots"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{normalized_stage}-ai-rate.png"
    output_path.write_bytes(data)

    return output_path
