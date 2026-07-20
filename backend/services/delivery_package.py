from __future__ import annotations

import hashlib
import re
import shutil
from zipfile import ZIP_DEFLATED, ZipFile
from pathlib import Path
from urllib.parse import urlsplit

from models import TaskRecord


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


def _reset_delivery_directory(task_directory: Path, destination: Path) -> None:
    """Rebuild the generated delivery folder without shipping internal metadata."""

    task_root = task_directory.resolve()
    destination_root = destination.resolve()
    try:
        destination_root.relative_to(task_root)
    except ValueError as exc:
        raise DeliveryPackageError(
            f"Delivery folder must stay inside the task directory: {destination}"
        ) from exc
    if destination_root == task_root:
        raise DeliveryPackageError("Delivery folder cannot replace the task directory.")

    if destination.exists():
        if not destination.is_dir():
            raise DeliveryPackageError(
                f"Delivery destination is not a folder: {destination}"
            )
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)


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

    task_directory = Path(task.task_dir)
    destination = task_directory / official_website_folder_name(task.customer)
    _reset_delivery_directory(task_directory, destination)

    article_filename = (
        article_docx.name
        if article_docx.name.casefold() != "d.docx"
        else "Article.docx"
    )
    for source, target in (
        (article_docx, destination / article_filename),
        (tdk_docx, destination / "D.docx"),
    ):
        _copy_file(source, target)

    used_image_names: set[str] = {
        article_filename.casefold(),
        "d.docx",
        "final-ai-rate.png",
    }
    for source in image_sources:
        _copy_file(
            source,
            _unique_destination(destination, source.name, used_image_names),
        )

    _copy_file(final_screenshot, destination / "final-ai-rate.png")

    return destination


def build_delivery_zip(task: TaskRecord) -> Path:
    package_value = str(task.delivery_package_path or "").strip()
    if not package_value:
        raise DeliveryPackageError("The delivery package has not been generated yet.")
    package_path = Path(package_value)
    if not package_path.is_dir():
        raise DeliveryPackageError("The delivery package folder is missing. Please package it again.")

    task_root = Path(task.task_dir).resolve()
    package_root = package_path.resolve()
    try:
        package_root.relative_to(task_root)
    except ValueError as exc:
        raise DeliveryPackageError("The delivery package folder is outside the task directory.") from exc
    if package_root == task_root:
        raise DeliveryPackageError("The task directory itself cannot be downloaded as a delivery package.")

    archive = task_root / f"{package_root.name}-topic_{task.topic_index:03d}.zip"
    temporary = archive.with_name(f".{archive.name}.tmp")
    temporary.unlink(missing_ok=True)
    file_count = 0
    try:
        with ZipFile(temporary, "w", compression=ZIP_DEFLATED, compresslevel=6) as output:
            for source in sorted(package_root.rglob("*")):
                if source.is_symlink():
                    raise DeliveryPackageError("The delivery package cannot contain symbolic links.")
                if not source.is_file():
                    continue
                resolved = source.resolve()
                try:
                    relative = resolved.relative_to(package_root)
                except ValueError as exc:
                    raise DeliveryPackageError("A delivery file is outside the package folder.") from exc
                output.write(resolved, arcname=relative.as_posix())
                file_count += 1
        if not file_count:
            raise DeliveryPackageError("The delivery package folder is empty.")
        temporary.replace(archive)
    finally:
        temporary.unlink(missing_ok=True)
    return archive
