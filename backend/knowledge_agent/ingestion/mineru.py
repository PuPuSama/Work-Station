from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from io import BytesIO
import json
import mimetypes
import os
import re
from pathlib import PurePosixPath
from time import monotonic, sleep
from typing import Mapping, Sequence
from urllib.parse import urlsplit
from zipfile import BadZipFile, ZipFile, ZipInfo

import httpx

from lxml import html as lxml_html

from .contracts import DocumentInput, ParsedAsset, ParsedBlock, ParsedDocument
from .parsers import (
    DocxDocumentParser,
    DocumentParseError,
    DocumentParserRouter,
    ExcelDocumentParser,
)


MINERU_CONTENT_LIST_ADAPTER_VERSION = "1"
DEFAULT_MINERU_BASE_URL = "https://mineru.net"
DEFAULT_MINERU_MODEL_VERSION = "vlm"
DEFAULT_MINERU_LANGUAGE = "en"
DEFAULT_MINERU_TIMEOUT_SECONDS = 300.0
DEFAULT_MINERU_POLL_INTERVAL_SECONDS = 3.0
MAX_MINERU_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_MINERU_UNCOMPRESSED_BYTES = 250 * 1024 * 1024
MAX_MINERU_ARCHIVE_ENTRIES = 2_000
MINERU_API_KEY_ENV = "ARTICLE_AGENT_MINERU_API_KEY"
MINERU_BASE_URL_ENV = "ARTICLE_AGENT_MINERU_BASE_URL"
MINERU_MODEL_VERSION_ENV = "ARTICLE_AGENT_MINERU_MODEL_VERSION"
MINERU_LANGUAGE_ENV = "ARTICLE_AGENT_MINERU_LANGUAGE"
MINERU_TIMEOUT_ENV = "ARTICLE_AGENT_MINERU_TIMEOUT_SECONDS"
MINERU_POLL_INTERVAL_ENV = "ARTICLE_AGENT_MINERU_POLL_INTERVAL_SECONDS"
_AUXILIARY_TYPES = {
    "header",
    "footer",
    "page_number",
    "aside_text",
    "page_footnote",
}


class MinerUConfigurationError(ValueError):
    """Raised when an explicitly configured MinerU client is invalid."""


@dataclass(frozen=True, slots=True)
class MinerUSettings:
    api_key: str = field(repr=False)
    base_url: str = DEFAULT_MINERU_BASE_URL
    model_version: str = DEFAULT_MINERU_MODEL_VERSION
    language: str = DEFAULT_MINERU_LANGUAGE
    timeout_seconds: float = DEFAULT_MINERU_TIMEOUT_SECONDS
    poll_interval_seconds: float = DEFAULT_MINERU_POLL_INTERVAL_SECONDS

    def __post_init__(self) -> None:
        api_key = str(self.api_key or "").strip()
        if not api_key:
            raise MinerUConfigurationError("MinerU API key is required")
        object.__setattr__(self, "api_key", api_key)
        base_url = str(self.base_url or "").strip().rstrip("/")
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise MinerUConfigurationError(
                "MinerU base URL must be an absolute HTTP(S) URL"
            )
        object.__setattr__(self, "base_url", base_url)
        model_version = str(self.model_version or "").strip()
        if model_version not in {"pipeline", "vlm"}:
            raise MinerUConfigurationError(
                "MinerU model version must be pipeline or vlm"
            )
        object.__setattr__(self, "model_version", model_version)
        language = str(self.language or "").strip()
        if not language:
            raise MinerUConfigurationError("MinerU language is required")
        object.__setattr__(self, "language", language)
        for field_name in ("timeout_seconds", "poll_interval_seconds"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise MinerUConfigurationError(
                    f"MinerU {field_name} must be a positive number"
                )
            normalized = float(value)
            if normalized <= 0:
                raise MinerUConfigurationError(
                    f"MinerU {field_name} must be a positive number"
                )
            object.__setattr__(self, field_name, normalized)

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> MinerUSettings | None:
        source = os.environ if environ is None else environ
        api_key = str(source.get(MINERU_API_KEY_ENV, "") or "").strip()
        if not api_key:
            return None

        def number(name: str, default: float) -> float:
            raw = str(source.get(name, "") or "").strip()
            if not raw:
                return default
            try:
                return float(raw)
            except ValueError as exc:
                raise MinerUConfigurationError(
                    f"{name} must be a positive number"
                ) from exc

        return cls(
            api_key=api_key,
            base_url=(
                str(source.get(MINERU_BASE_URL_ENV, "") or "").strip()
                or DEFAULT_MINERU_BASE_URL
            ),
            model_version=(
                str(source.get(MINERU_MODEL_VERSION_ENV, "") or "").strip()
                or DEFAULT_MINERU_MODEL_VERSION
            ),
            language=(
                str(source.get(MINERU_LANGUAGE_ENV, "") or "").strip()
                or DEFAULT_MINERU_LANGUAGE
            ),
            timeout_seconds=number(
                MINERU_TIMEOUT_ENV,
                DEFAULT_MINERU_TIMEOUT_SECONDS,
            ),
            poll_interval_seconds=number(
                MINERU_POLL_INTERVAL_ENV,
                DEFAULT_MINERU_POLL_INTERVAL_SECONDS,
            ),
        )


def _text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _bbox(value: object) -> list[float] | None:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or len(value) != 4
    ):
        return None
    result: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return None
        result.append(float(item))
    return result


@dataclass(frozen=True, slots=True)
class _MinerUTableRow:
    cells: tuple[str, ...]
    is_header: bool


def _table_rows(value: object) -> tuple[_MinerUTableRow, ...]:
    source = _text(value)
    if not source:
        return ()
    try:
        document = lxml_html.fromstring(source)
    except (TypeError, ValueError):
        return (_MinerUTableRow((source,), False),)
    rows: list[_MinerUTableRow] = []
    for row in document.xpath("//tr"):
        cells: list[str] = []
        for cell in row.xpath("./th | ./td"):
            text = _text(cell.text_content())
            try:
                colspan = max(1, int(cell.get("colspan", "1")))
            except (TypeError, ValueError):
                colspan = 1
            cells.extend([text] * colspan)
        if any(cells):
            rows.append(
                _MinerUTableRow(
                    cells=tuple(cells),
                    is_header=bool(row.xpath("./th"))
                    or bool(row.xpath("ancestor::thead")),
                )
            )
    return tuple(rows) or (_MinerUTableRow((source,), False),)


def _table_headers(rows: Sequence[_MinerUTableRow]) -> tuple[str, ...]:
    header_rows = [row.cells for row in rows if row.is_header]
    if not header_rows:
        if not rows:
            return ()
        first_row = rows[0].cells
        # MinerU commonly emits every cell as ``td`` even when the source
        # table has a real header. Preserve a simple first-row header, and
        # prefer the first power/model row when a two-level product matrix
        # makes the model columns explicit there.
        first_label = first_row[0].casefold() if first_row else ""
        grouped_header = (
            "technical specification" in first_label
            or "specification" in first_label
            or "参数" in first_row[0]
            or any("series" in cell.casefold() for cell in first_row[1:])
        )
        if grouped_header and len(rows) > 1 and len(first_row) > 1:
            model_row = rows[1].cells
            if all(_looks_like_model_value(value) for value in model_row[1:]):
                return (first_row[0] or "Parameter",) + tuple(model_row[1:])
        return first_row
    width = max(len(row) for row in header_rows)
    headers: list[str] = []
    for column_index in range(width):
        labels: list[str] = []
        for row in header_rows:
            if column_index < len(row):
                label = row[column_index]
                if label and label not in labels:
                    labels.append(label)
        headers.append(" / ".join(labels))
    return tuple(headers)


def _looks_like_model_value(value: str) -> bool:
    normalized = value.strip()
    if not normalized:
        return False
    if "/" in normalized or "series" in normalized.casefold():
        return True
    return bool(
        re.search(r"\d", normalized)
        and re.search(r"[A-Za-z\u4e00-\u9fff]", normalized)
        and not re.fullmatch(r"\d+(?:\.\d+)?\s*(?:mm|cm|m|kg|g|hz|%|°c)", normalized, re.I)
    )


def _table_row_text(
    row: _MinerUTableRow,
    headers: Sequence[str],
) -> str:
    raw = " | ".join(row.cells)
    pairs = [
        f"{header}: {value}"
        for header, value in zip(headers, row.cells)
        if header and value
    ]
    return f"{raw} || {'; '.join(pairs)}" if pairs and not row.is_header else raw


def _list_text(value: object) -> str:
    if isinstance(value, str):
        return _text(value)
    if not isinstance(value, Sequence) or isinstance(value, bytes):
        return ""
    items: list[str] = []
    for item in value:
        if isinstance(item, Mapping):
            candidate = (
                item.get("text")
                or item.get("content")
                or item.get("list_item")
            )
        else:
            candidate = item
        normalized = _text(candidate)
        if normalized:
            items.append(normalized)
    return "\n".join(f"- {item}" for item in items)


class MinerUContentListAdapter:
    """Normalize MinerU's stable legacy ``content_list.json`` into M2 contracts.

    Running MinerU remains an external-service responsibility. This adapter
    intentionally accepts already-produced structured output and referenced
    asset bytes so the FastAPI process never imports MinerU model dependencies.
    """

    def normalize(
        self,
        *,
        document_input: DocumentInput,
        content_list: bytes,
        mineru_version: str,
        assets: Mapping[str, bytes] | None = None,
    ) -> ParsedDocument:
        try:
            payload = json.loads(content_list)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DocumentParseError(
                "MinerU content_list.json is not valid JSON"
            ) from exc
        if not isinstance(payload, list):
            raise DocumentParseError("MinerU content_list.json must be a list")
        version = _text(mineru_version)
        if not version:
            raise DocumentParseError("MinerU version is required")

        blocks: list[ParsedBlock] = []
        parsed_assets: list[ParsedAsset] = []
        headings: list[str] = []
        available_assets = dict(assets or {})
        for item_index, raw_item in enumerate(payload):
            if not isinstance(raw_item, Mapping):
                raise DocumentParseError(
                    f"MinerU content item {item_index} must be an object"
                )
            item_type = _text(raw_item.get("type")).casefold()
            if not item_type:
                raise DocumentParseError(
                    f"MinerU content item {item_index} has no type"
                )
            page_index = raw_item.get("page_idx")
            if isinstance(page_index, bool) or not isinstance(page_index, int):
                page_index = None
            locator: dict[str, object] = {
                "mineru_content_index": item_index,
            }
            if page_index is not None:
                locator["page_index"] = page_index
                locator["page_number"] = page_index + 1
            bbox = _bbox(raw_item.get("bbox"))
            if bbox is not None:
                locator["bbox_0_1000"] = bbox

            if item_type in _AUXILIARY_TYPES:
                continue
            image_path = _text(raw_item.get("img_path"))
            if image_path:
                content = available_assets.get(image_path)
                if content is not None:
                    filename = PurePosixPath(image_path).name
                    if not filename:
                        raise DocumentParseError(
                            f"MinerU image path is invalid at item {item_index}"
                        )
                    content_type = (
                        mimetypes.guess_type(filename)[0]
                        or "application/octet-stream"
                    )
                    parsed_assets.append(
                        ParsedAsset(
                            filename=filename,
                            content=content,
                            content_type=content_type,
                            ordinal=len(parsed_assets),
                            locator=locator,
                            metadata={
                                "mineru_type": item_type,
                                "mineru_path": image_path,
                            },
                        )
                    )
            if item_type == "text":
                value = _text(raw_item.get("text"))
                if not value:
                    continue
                level = raw_item.get("text_level")
                if isinstance(level, int) and not isinstance(level, bool) and level > 0:
                    headings = headings[: level - 1]
                    headings.append(value)
                    block_kind = "heading"
                else:
                    block_kind = "paragraph"
                blocks.append(
                    ParsedBlock(
                        kind=block_kind,
                        ordinal=len(blocks),
                        text=value,
                        heading_path=tuple(headings),
                        locator=locator,
                        metadata={"mineru_type": item_type},
                    )
                )
                continue
            if item_type == "table":
                table_id = f"mineru-table-{item_index}"
                rows = _table_rows(raw_item.get("table_body"))
                headers = _table_headers(rows)
                for row_index, row in enumerate(rows):
                    row_locator = {
                        **locator,
                        "table_id": table_id,
                        "table_row_index": row_index,
                    }
                    blocks.append(
                        ParsedBlock(
                            kind="table_row",
                            ordinal=len(blocks),
                            text=_table_row_text(row, headers),
                            heading_path=tuple(headings),
                            locator=row_locator,
                            metadata={
                                "mineru_type": item_type,
                                "table_id": table_id,
                                "table_row_index": row_index,
                                "table_cells": list(row.cells),
                                "table_headers": list(headers),
                                "table_is_header": row.is_header,
                            },
                        )
                    )
                continue
            if item_type == "list":
                value = _list_text(
                    raw_item.get("list_items") or raw_item.get("text")
                )
            elif item_type == "code":
                value = _text(
                    raw_item.get("code_body") or raw_item.get("text")
                )
            else:
                value = _text(
                    raw_item.get("text")
                    or raw_item.get("equation")
                    or raw_item.get("image_caption")
                    or raw_item.get("chart_caption")
                )
            if value:
                blocks.append(
                    ParsedBlock(
                        kind="paragraph",
                        ordinal=len(blocks),
                        text=value,
                        heading_path=tuple(headings),
                        locator=locator,
                        metadata={"mineru_type": item_type},
                    )
                )

        title = next(
            (block.text for block in blocks if block.kind == "heading"),
            None,
        )
        try:
            return ParsedDocument(
                filename=document_input.filename,
                content_type=document_input.content_type
                or "application/octet-stream",
                content_hash=document_input.content_hash,
                parser_name="mineru-content-list",
                parser_version=(
                    f"{version}/adapter-{MINERU_CONTENT_LIST_ADAPTER_VERSION}"
                ),
                blocks=tuple(blocks),
                assets=tuple(parsed_assets),
                title=title,
                metadata={
                    "mineru_version": version,
                    "content_item_count": len(payload),
                    "missing_asset_count": sum(
                        1
                        for item in payload
                        if isinstance(item, Mapping)
                        and _text(item.get("img_path"))
                        and _text(item.get("img_path")) not in available_assets
                    ),
                },
            )
        except ValueError as exc:
            raise DocumentParseError(
                f"MinerU output could not satisfy ParsedDocument: {exc}"
            ) from exc


class MinerUDocumentParser:
    """Use MinerU Precision Extract API v4 for PDF parsing.

    The API token is sent only to the configured MinerU API base URL. Signed
    upload and result URLs returned by MinerU never appear in exceptions or
    logs. The downloaded archive is bounded and validated before extraction.
    """

    name = "mineru-content-list"
    _suffixes = frozenset({".pdf"})
    _content_types = frozenset(
        {
            "application/pdf",
        }
    )

    def __init__(
        self,
        settings: MinerUSettings,
        *,
        client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
        sleeper: Callable[[float], None] = sleep,
    ) -> None:
        self._settings = settings
        self._client = client or httpx.Client(
            transport=transport,
            timeout=settings.timeout_seconds,
            follow_redirects=True,
        )
        self._owns_client = client is None
        self._sleeper = sleeper
        self._adapter = MinerUContentListAdapter()

    @property
    def version(self) -> str:
        return (
            f"api-v4-{self._settings.model_version}/"
            f"adapter-{MINERU_CONTENT_LIST_ADAPTER_VERSION}"
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def supports(self, document: DocumentInput) -> bool:
        return (
            document.suffix in self._suffixes
            or document.content_type in self._content_types
        )

    def parse(self, document: DocumentInput) -> ParsedDocument:
        if not self.supports(document):
            raise DocumentParseError(
                "MinerU parser only supports PDF documents"
            )
        try:
            batch_id, upload_url = self._create_upload(document)
            upload_response = self._client.put(upload_url, content=document.content)
            if upload_response.status_code not in {200, 201, 204}:
                raise DocumentParseError("MinerU file upload failed")
            archive_url = self._wait_for_archive(batch_id, document)
            archive = self._download_archive(archive_url)
            content_list, assets = self._read_archive(archive)
            parsed = self._adapter.normalize(
                document_input=document,
                content_list=content_list,
                mineru_version=f"api-v4-{self._settings.model_version}",
                assets=assets,
            )
            return replace(
                parsed,
                metadata={
                    **dict(parsed.metadata),
                    "mineru_batch_id": batch_id,
                    "mineru_model_version": self._settings.model_version,
                },
            )
        except DocumentParseError:
            raise
        except httpx.HTTPError:
            raise DocumentParseError(
                "MinerU parsing service is temporarily unavailable"
            ) from None
        except (BadZipFile, OSError, ValueError) as exc:
            raise DocumentParseError(
                "MinerU returned an invalid extraction archive"
            ) from exc

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._settings.api_key}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _json(response: httpx.Response) -> Mapping[str, object]:
        try:
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, json.JSONDecodeError):
            raise DocumentParseError(
                "MinerU API returned an invalid response"
            ) from None
        if not isinstance(payload, Mapping) or payload.get("code") != 0:
            raise DocumentParseError("MinerU API rejected the parsing request")
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise DocumentParseError("MinerU API returned an invalid response")
        return data

    def _create_upload(self, document: DocumentInput) -> tuple[str, str]:
        response = self._client.post(
            f"{self._settings.base_url}/api/v4/file-urls/batch",
            headers=self._headers(),
            json={
                "files": [
                    {
                        "name": document.filename,
                        "data_id": document.content_hash,
                    }
                ],
                "model_version": self._settings.model_version,
                "language": self._settings.language,
                "enable_table": True,
                "enable_formula": True,
            },
        )
        data = self._json(response)
        batch_id = _text(data.get("batch_id"))
        upload_urls = data.get("file_urls")
        if (
            not batch_id
            or not isinstance(upload_urls, Sequence)
            or isinstance(upload_urls, (str, bytes))
            or len(upload_urls) != 1
        ):
            raise DocumentParseError("MinerU API returned an invalid upload task")
        upload_url = _text(upload_urls[0])
        if not self._valid_remote_url(upload_url):
            raise DocumentParseError("MinerU API returned an invalid upload task")
        return batch_id, upload_url

    @staticmethod
    def _valid_remote_url(value: str) -> bool:
        try:
            parsed = urlsplit(value)
            return (
                parsed.scheme in {"http", "https"}
                and bool(parsed.hostname)
                and parsed.username is None
                and parsed.password is None
            )
        except ValueError:
            return False

    def _wait_for_archive(
        self,
        batch_id: str,
        document: DocumentInput,
    ) -> str:
        deadline = monotonic() + self._settings.timeout_seconds
        while monotonic() < deadline:
            response = self._client.get(
                f"{self._settings.base_url}/api/v4/extract-results/batch/{batch_id}",
                headers=self._headers(),
            )
            data = self._json(response)
            results = data.get("extract_result")
            if not isinstance(results, Sequence) or isinstance(results, (str, bytes)):
                raise DocumentParseError("MinerU API returned an invalid task result")
            match = next(
                (
                    item
                    for item in results
                    if isinstance(item, Mapping)
                    and (
                        _text(item.get("data_id")) == document.content_hash
                        or _text(item.get("file_name")) == document.filename
                    )
                ),
                None,
            )
            if match is None:
                self._sleeper(self._settings.poll_interval_seconds)
                continue
            state = _text(match.get("state")).casefold()
            if state == "done":
                archive_url = _text(match.get("full_zip_url"))
                if not self._valid_remote_url(archive_url):
                    raise DocumentParseError(
                        "MinerU API returned an incomplete task result"
                    )
                return archive_url
            if state == "failed":
                raise DocumentParseError("MinerU could not parse this document")
            if state not in {"waiting-file", "pending", "running", "converting"}:
                raise DocumentParseError("MinerU API returned an unknown task state")
            self._sleeper(self._settings.poll_interval_seconds)
        raise DocumentParseError("MinerU parsing timed out")

    def _download_archive(self, archive_url: str) -> bytes:
        chunks: list[bytes] = []
        byte_count = 0
        with self._client.stream("GET", archive_url) as response:
            response.raise_for_status()
            for chunk in response.iter_bytes():
                byte_count += len(chunk)
                if byte_count > MAX_MINERU_ARCHIVE_BYTES:
                    raise DocumentParseError("MinerU extraction archive is too large")
                chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    def _safe_archive_name(value: str) -> str:
        normalized = value.replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        path = PurePosixPath(normalized)
        if not normalized or path.is_absolute() or ".." in path.parts:
            raise DocumentParseError("MinerU extraction archive is unsafe")
        return str(path)

    def _read_archive(self, archive: bytes) -> tuple[bytes, dict[str, bytes]]:
        with ZipFile(BytesIO(archive)) as bundle:
            infos = [item for item in bundle.infolist() if not item.is_dir()]
            if len(infos) > MAX_MINERU_ARCHIVE_ENTRIES:
                raise DocumentParseError("MinerU extraction archive has too many files")
            names: dict[str, ZipInfo] = {}
            total_size = 0
            for info in infos:
                name = self._safe_archive_name(info.filename)
                if name in names:
                    raise DocumentParseError(
                        "MinerU extraction archive contains duplicate paths"
                    )
                if info.flag_bits & 0x1:
                    raise DocumentParseError("MinerU extraction archive is encrypted")
                total_size += int(info.file_size)
                if total_size > MAX_MINERU_UNCOMPRESSED_BYTES:
                    raise DocumentParseError(
                        "MinerU extraction archive expands beyond the safe limit"
                    )
                names[name] = info
            candidates = sorted(
                (
                    name
                    for name in names
                    if name.casefold().endswith("content_list.json")
                ),
                key=lambda name: (name.count("/"), len(name), name),
            )
            if not candidates:
                raise DocumentParseError(
                    "MinerU extraction archive has no content_list.json"
                )
            content_name = candidates[0]
            content_list = bundle.read(names[content_name])
            try:
                payload = json.loads(content_list)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise DocumentParseError(
                    "MinerU content_list.json is not valid JSON"
                ) from exc
            if not isinstance(payload, list):
                raise DocumentParseError(
                    "MinerU content_list.json must be a list"
                )
            requested_paths = {
                _text(item.get("img_path"))
                for item in payload
                if isinstance(item, Mapping)
                and _text(item.get("img_path"))
            }
            content_parent = str(PurePosixPath(content_name).parent)
            assets: dict[str, bytes] = {}
            for requested in sorted(requested_paths):
                safe_requested = self._safe_archive_name(requested)
                possible = [safe_requested]
                if content_parent != ".":
                    possible.append(f"{content_parent}/{safe_requested}")
                possible.extend(
                    name
                    for name in names
                    if name.endswith(f"/{safe_requested}")
                )
                resolved = next((name for name in possible if name in names), None)
                if resolved is not None:
                    assets[requested] = bundle.read(names[resolved])
            return content_list, assets


def document_parser_router_from_environment(
    environ: Mapping[str, str] | None = None,
) -> DocumentParserRouter:
    """Use local DOCX parsing and select MinerU only for configured PDFs."""

    settings = MinerUSettings.from_environment(environ)
    if settings is None:
        return DocumentParserRouter()
    return DocumentParserRouter(
        (
            DocxDocumentParser(),
            MinerUDocumentParser(settings),
            ExcelDocumentParser(),
        )
    )


__all__ = [
    "DEFAULT_MINERU_BASE_URL",
    "DEFAULT_MINERU_LANGUAGE",
    "DEFAULT_MINERU_MODEL_VERSION",
    "MINERU_API_KEY_ENV",
    "MINERU_CONTENT_LIST_ADAPTER_VERSION",
    "MinerUConfigurationError",
    "MinerUContentListAdapter",
    "MinerUDocumentParser",
    "MinerUSettings",
    "document_parser_router_from_environment",
]
