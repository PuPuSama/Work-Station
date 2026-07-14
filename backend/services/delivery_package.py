from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from urllib.parse import urlsplit

from models import TaskRecord
from storage import now_iso


INVALID_WINDOWS_CHARS = '<>:"/\\|?*'
MAX_DELIVERY_IMAGES = 3


class DeliveryPackageError(ValueError):
    """Raised when a complete delivery folder cannot be assembled."""


def official_website_folder_name(customer: str) -> str:
    raw = str(customer or "").strip()
    if not raw:
        raise DeliveryPackageError("The project official website is empty.")

    parsed = urlsplit(raw if "://" in raw else f"https://{raw}")
    host = (parsed.hostname or raw).strip().lower()
    cleaned = "".join("_" if char in INVALID_WINDOWS_CHARS else char for char in host)
    cleaned = re.sub(r"\s+", "-", cleaned).strip(" .-")
    if not cleaned:
        raise DeliveryPackageError("Unable to derive a folder name from the official website.")
    return cleaned


def _required_file(path_value: str, label: str) -> Path:
    path = Path(str(path_value or ""))
    if not path.is_file():
        raise DeliveryPackageError(f"{label} is missing: {path}")
    return path


def _unique_destination(directory: Path, filename: str, used_names: set[str]) -> Path:
    candidate = Path(filename)
    name = candidate.name
    suffix = candidate.suffix
    stem = candidate.stem
    counter = 2
    while name.casefold() in used_names:
        name = f"{stem}-{counter}{suffix}"
        counter += 1
    used_names.add(name.casefold())
    return directory / name


def _copy_file(source: Path, destination: Path) -> Path:
    if source.resolve() == destination.resolve():
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return destination


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_delivery_images(image_sources: list[Path]) -> None:
    if len(image_sources) > MAX_DELIVERY_IMAGES:
        raise DeliveryPackageError(
            f"Article delivery supports at most {MAX_DELIVERY_IMAGES} images; "
            f"received {len(image_sources)}."
        )

    seen_hashes: dict[str, Path] = {}
    for source in image_sources:
        content_hash = _sha256_file(source)
        duplicate = seen_hashes.get(content_hash)
        if duplicate is not None:
            raise DeliveryPackageError(
                "Duplicate article image content is not allowed: "
                f"{source.name} duplicates {duplicate.name}."
            )
        seen_hashes[content_hash] = source


def _remove_previous_delivery_files(destination: Path) -> None:
    manifest_path = destination / "delivery_manifest.json"
    if not manifest_path.is_file():
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return

    destination_root = destination.resolve()
    for item in manifest.get("files", []):
        if not isinstance(item, dict) or not item.get("delivery"):
            continue
        candidate = Path(str(item["delivery"]))
        try:
            resolved = candidate.resolve()
            resolved.relative_to(destination_root)
        except (OSError, ValueError):
            continue
        if resolved.is_file():
            resolved.unlink()

    for legacy_directory in (
        destination / "images",
        destination / "AI rate screenshots",
    ):
        if legacy_directory.is_dir() and not any(legacy_directory.iterdir()):
            legacy_directory.rmdir()


def package_delivery(task: TaskRecord) -> Path:
    article_docx = _required_file(task.docx_path, "Article Word document")
    tdk_docx = _required_file(task.tdk_path, "D document")

    image_sources: list[Path] = []
    for image in task.images:
        prepared = str(image.prepared_path or "").strip()
        if prepared:
            image_sources.append(_required_file(prepared, "Prepared article image"))
    if not image_sources:
        raise DeliveryPackageError("No prepared article images are available for delivery.")
    _validate_delivery_images(image_sources)

    final_screenshot = _required_file(
        task.final_ai_check.screenshot_path,
        "Final AI-rate screenshot",
    )

    destination = Path(task.task_dir) / official_website_folder_name(task.customer)
    destination.mkdir(parents=True, exist_ok=True)
    _remove_previous_delivery_files(destination)

    copied: list[dict[str, str]] = []
    article_filename = (
        article_docx.name
        if article_docx.name.casefold() != "d.docx"
        else "Article.docx"
    )
    for source, target in (
        (article_docx, destination / article_filename),
        (tdk_docx, destination / "D.docx"),
    ):
        output = _copy_file(source, target)
        copied.append({"source": str(source), "delivery": str(output)})

    used_image_names: set[str] = {
        article_filename.casefold(),
        "d.docx",
        "final-ai-rate.png",
        "delivery_manifest.json",
    }
    for source in image_sources:
        output = _copy_file(
            source,
            _unique_destination(destination, source.name, used_image_names),
        )
        copied.append({"source": str(source), "delivery": str(output)})

    output = _copy_file(final_screenshot, destination / "final-ai-rate.png")
    copied.append({"source": str(final_screenshot), "delivery": str(output)})

    manifest = {
        "official_website": task.customer,
        "task_id": task.id,
        "created_at": now_iso(),
        "files": copied,
    }
    (destination / "delivery_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return destination
