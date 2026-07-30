from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import PurePath
from types import MappingProxyType
from typing import Literal, Mapping


Metadata = Mapping[str, object]
BlockKind = Literal["heading", "paragraph", "table_row", "page_text"]


def _required_text(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} is required")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized


def _metadata(value: Metadata, field_name: str) -> Metadata:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a mapping")
    return MappingProxyType(dict(value))


@dataclass(frozen=True, slots=True)
class DocumentInput:
    """Untrusted file input at the document-parser boundary."""

    filename: str
    content: bytes = field(repr=False)
    content_type: str | None = None

    def __post_init__(self) -> None:
        filename = _required_text(self.filename, "filename")
        if PurePath(filename).name != filename or "/" in filename or "\\" in filename:
            raise ValueError("filename must not contain a directory path")
        if not isinstance(self.content, bytes) or not self.content:
            raise ValueError("content must be non-empty bytes")
        content_type = self.content_type
        if content_type is not None:
            content_type = _required_text(content_type, "content_type").lower()
            content_type = content_type.partition(";")[0].strip()
        object.__setattr__(self, "filename", filename)
        object.__setattr__(self, "content_type", content_type)

    @property
    def suffix(self) -> str:
        return PurePath(self.filename).suffix.lower()

    @property
    def content_hash(self) -> str:
        return sha256(self.content).hexdigest()


@dataclass(frozen=True, slots=True)
class ParsedBlock:
    """One ordered, source-locatable unit produced by a document parser."""

    kind: BlockKind
    ordinal: int
    text: str
    heading_path: tuple[str, ...] = ()
    locator: Metadata = field(default_factory=dict)
    metadata: Metadata = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in {"heading", "paragraph", "table_row", "page_text"}:
            raise ValueError("kind is not a supported parsed block kind")
        if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int):
            raise ValueError("ordinal must be a non-negative integer")
        if self.ordinal < 0:
            raise ValueError("ordinal must be a non-negative integer")
        object.__setattr__(self, "text", _required_text(self.text, "text"))
        if isinstance(self.heading_path, (str, bytes)):
            raise ValueError("heading_path must be a sequence of headings")
        object.__setattr__(
            self,
            "heading_path",
            tuple(_required_text(item, "heading_path") for item in self.heading_path),
        )
        object.__setattr__(self, "locator", _metadata(self.locator, "locator"))
        object.__setattr__(self, "metadata", _metadata(self.metadata, "metadata"))


@dataclass(frozen=True, slots=True)
class ParsedAsset:
    """An immutable embedded asset extracted from a source document."""

    filename: str
    content: bytes = field(repr=False)
    content_type: str
    ordinal: int = 0
    locator: Metadata = field(default_factory=dict)
    metadata: Metadata = field(default_factory=dict)

    def __post_init__(self) -> None:
        filename = _required_text(self.filename, "filename")
        if PurePath(filename).name != filename or "/" in filename or "\\" in filename:
            raise ValueError("asset filename must not contain a directory path")
        if not isinstance(self.content, bytes) or not self.content:
            raise ValueError("asset content must be non-empty bytes")
        if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int):
            raise ValueError("asset ordinal must be a non-negative integer")
        if self.ordinal < 0:
            raise ValueError("asset ordinal must be a non-negative integer")
        object.__setattr__(self, "filename", filename)
        object.__setattr__(
            self, "content_type", _required_text(self.content_type, "content_type").lower()
        )
        object.__setattr__(self, "locator", _metadata(self.locator, "locator"))
        object.__setattr__(self, "metadata", _metadata(self.metadata, "metadata"))

    @property
    def content_hash(self) -> str:
        return sha256(self.content).hexdigest()


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    """Provider-neutral normalized document ready for review and chunking."""

    filename: str
    content_type: str
    content_hash: str
    parser_name: str
    parser_version: str
    blocks: tuple[ParsedBlock, ...]
    assets: tuple[ParsedAsset, ...] = ()
    title: str | None = None
    metadata: Metadata = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in (
            "filename",
            "content_type",
            "content_hash",
            "parser_name",
            "parser_version",
        ):
            object.__setattr__(
                self, field_name, _required_text(getattr(self, field_name), field_name)
            )
        normalized_hash = self.content_hash.lower()
        if len(normalized_hash) != 64 or any(
            character not in "0123456789abcdef" for character in normalized_hash
        ):
            raise ValueError("content_hash must be a 64-character SHA-256 hex digest")
        object.__setattr__(self, "content_hash", normalized_hash)
        object.__setattr__(self, "blocks", tuple(self.blocks))
        object.__setattr__(self, "assets", tuple(self.assets))
        if not self.blocks and not self.assets:
            raise ValueError("parsed document must contain text blocks or assets")
        expected_block_ordinals = tuple(range(len(self.blocks)))
        if tuple(block.ordinal for block in self.blocks) != expected_block_ordinals:
            raise ValueError("parsed block ordinals must be contiguous and zero-based")
        expected_asset_ordinals = tuple(range(len(self.assets)))
        if tuple(asset.ordinal for asset in self.assets) != expected_asset_ordinals:
            raise ValueError("parsed asset ordinals must be contiguous and zero-based")
        title = self.title
        if title is not None:
            title = _required_text(title, "title")
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "metadata", _metadata(self.metadata, "metadata"))

    @property
    def text(self) -> str:
        return "\n\n".join(block.text for block in self.blocks)
