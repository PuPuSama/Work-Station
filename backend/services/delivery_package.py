from __future__ import annotations

import hashlib
import re
import shutil
from io import BytesIO
from pathlib import Path
from typing import Sequence
from urllib.parse import urlsplit
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

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


def _archive_filename(value: str, label: str) -> str:
    normalized = str(value or "").strip()
    if (
        not normalized
        or Path(normalized).name != normalized
        or "/" in normalized
        or "\\" in normalized
        or any(ord(character) < 32 for character in normalized)
    ):
        raise DeliveryPackageError(
            f"{label} has an unsafe filename."
        )
    return normalized


def _unique_archive_filename(
    filename: str,
    used_names: set[str],
) -> str:
    candidate = Path(filename)
    name = candidate.name
    counter = 2
    while name.casefold() in used_names:
        name = f"{candidate.stem}-{counter}{candidate.suffix}"
        counter += 1
    used_names.add(name.casefold())
    return name


def _write_deterministic_zip_entry(
    archive: ZipFile,
    filename: str,
    data: bytes,
) -> None:
    info = ZipInfo(filename, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    info.create_system = 3
    archive.writestr(info, data, compress_type=ZIP_DEFLATED, compresslevel=6)


def build_delivery_zip_bytes(
    *,
    article_docx: bytes,
    article_filename: str,
    tdk_docx: bytes,
    images: Sequence[tuple[str, bytes]],
    final_screenshot: bytes | None = None,
    final_screenshot_filename: str = "final-ai-rate.png",
    metadata: bytes | None = None,
    metadata_filename: str = "metadata.json",
) -> bytes:
    """Build the public delivery layout without filesystem metadata."""

    article_data = bytes(article_docx)
    tdk_data = bytes(tdk_docx)
    screenshot_data = (
        bytes(final_screenshot)
        if final_screenshot is not None
        else b""
    )
    metadata_data = bytes(metadata) if metadata is not None else None
    if not article_data:
        raise DeliveryPackageError("Article Word document is empty.")
    if not tdk_data:
        raise DeliveryPackageError("D document is empty.")
    if metadata_data is not None and not metadata_data:
        raise DeliveryPackageError("Metadata file is empty.")
    if not images:
        raise DeliveryPackageError(
            "No prepared article images are available for delivery."
        )
    if len(images) > MAX_DELIVERY_IMAGES:
        raise DeliveryPackageError(
            f"Article delivery supports at most {MAX_DELIVERY_IMAGES} "
            f"images; received {len(images)}."
        )

    normalized_article_name = _archive_filename(
        article_filename,
        "Article Word document",
    )
    if Path(normalized_article_name).suffix.casefold() != ".docx":
        raise DeliveryPackageError(
            "Article Word document must use a .docx filename."
        )
    if normalized_article_name.casefold() == "d.docx":
        normalized_article_name = "Article.docx"
    normalized_screenshot_name = ""
    if screenshot_data:
        normalized_screenshot_name = _archive_filename(
            final_screenshot_filename,
            "Final AI-rate screenshot",
        )
        if not normalized_screenshot_name.casefold().startswith("final-ai-rate."):
            normalized_screenshot_name = "final-ai-rate.png"
    normalized_metadata_name = ""
    if metadata_data is not None:
        normalized_metadata_name = _archive_filename(
            metadata_filename,
            "Delivery metadata file",
        )
    used_names = {
        normalized_article_name.casefold(),
        "d.docx",
    }
    if normalized_screenshot_name:
        used_names.add(normalized_screenshot_name.casefold())
    if normalized_metadata_name:
        if normalized_metadata_name.casefold() in used_names:
            raise DeliveryPackageError(
                "Delivery metadata file conflicts with another delivery file."
            )
        used_names.add(normalized_metadata_name.casefold())
    normalized_images: list[tuple[str, bytes]] = []
    seen_hashes: set[str] = set()
    for raw_filename, raw_data in images:
        data = bytes(raw_data)
        if not data:
            raise DeliveryPackageError(
                "Prepared article image is empty."
            )
        digest = hashlib.sha256(data).hexdigest()
        if digest in seen_hashes:
            raise DeliveryPackageError(
                "Duplicate article image content is not allowed."
            )
        seen_hashes.add(digest)
        filename = _archive_filename(
            raw_filename,
            "Prepared article image",
        )
        normalized_images.append(
            (
                _unique_archive_filename(filename, used_names),
                data,
            )
        )

    output = BytesIO()
    with ZipFile(
        output,
        "w",
        compression=ZIP_DEFLATED,
        compresslevel=6,
    ) as archive:
        _write_deterministic_zip_entry(
            archive,
            normalized_article_name,
            article_data,
        )
        _write_deterministic_zip_entry(archive, "D.docx", tdk_data)
        if metadata_data is not None:
            _write_deterministic_zip_entry(
                archive,
                normalized_metadata_name,
                metadata_data,
            )
        for filename, data in normalized_images:
            _write_deterministic_zip_entry(archive, filename, data)
        if screenshot_data:
            _write_deterministic_zip_entry(
                archive,
                normalized_screenshot_name,
                screenshot_data,
            )
    return output.getvalue()


def _delivery_ai_screenshot(task: TaskRecord) -> Path | None:
    final_path = str(task.final_ai_check.screenshot_path or "").strip()
    if final_path:
        return _required_file(final_path, "Final AI-rate screenshot")
    if not task.humanization_skipped:
        return _required_file("", "Final AI-rate screenshot")

    initial_path = str(task.initial_ai_check.screenshot_path or "").strip()
    if initial_path:
        return _required_file(initial_path, "Initial AI-rate screenshot")
    return None


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

    final_screenshot = _delivery_ai_screenshot(task)

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

    if final_screenshot is not None:
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
