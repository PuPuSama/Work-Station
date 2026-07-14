from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from models import TaskRecord


AI_CHECK_STAGES = {"initial", "final"}


class AIScreenshotError(ValueError):
    """Raised when an AI-rate screenshot cannot be validated or saved."""


def save_ai_rate_screenshot(
    task: TaskRecord,
    stage: str,
    content: bytes,
) -> Path:
    normalized_stage = stage.strip().lower()
    if normalized_stage not in AI_CHECK_STAGES:
        raise AIScreenshotError("AI check stage must be 'initial' or 'final'.")
    if not content:
        raise AIScreenshotError("The pasted AI-rate screenshot is empty.")

    try:
        with Image.open(BytesIO(content)) as source:
            source.load()
            image = ImageOps.exif_transpose(source)
            if image.mode not in {"RGB", "RGBA"}:
                image = image.convert("RGBA" if "transparency" in source.info else "RGB")
            output_dir = Path(task.task_dir) / "ai-rate-screenshots"
            output_dir.mkdir(parents=True, exist_ok=True)
            output_path = output_dir / f"{normalized_stage}-ai-rate.png"
            image.save(output_path, format="PNG", optimize=True)
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise AIScreenshotError("The pasted file is not a valid image.") from exc

    return output_path
