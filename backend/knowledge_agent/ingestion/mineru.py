from __future__ import annotations

import json
import mimetypes
import re
from pathlib import PurePosixPath
from typing import Mapping, Sequence

from lxml import html as lxml_html

from .contracts import DocumentInput, ParsedAsset, ParsedBlock, ParsedDocument
from .parsers import DocumentParseError


MINERU_CONTENT_LIST_ADAPTER_VERSION = "1"
_AUXILIARY_TYPES = {
    "header",
    "footer",
    "page_number",
    "aside_text",
    "page_footnote",
}


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


def _table_rows(value: object) -> tuple[str, ...]:
    source = _text(value)
    if not source:
        return ()
    try:
        document = lxml_html.fromstring(source)
    except (TypeError, ValueError):
        return (source,)
    rows: list[str] = []
    for row in document.xpath("//tr"):
        cells = [
            _text(cell.text_content())
            for cell in row.xpath("./th | ./td")
        ]
        cells = [cell for cell in cells if cell]
        if cells:
            rows.append(" | ".join(cells))
    return tuple(rows) or (source,)


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
                for row_index, value in enumerate(
                    _table_rows(raw_item.get("table_body"))
                ):
                    blocks.append(
                        ParsedBlock(
                            kind="table_row",
                            ordinal=len(blocks),
                            text=value,
                            heading_path=tuple(headings),
                            locator={**locator, "table_row_index": row_index},
                            metadata={"mineru_type": item_type},
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


__all__ = [
    "MINERU_CONTENT_LIST_ADAPTER_VERSION",
    "MinerUContentListAdapter",
]
