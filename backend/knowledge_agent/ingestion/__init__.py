"""M2 document-ingestion contracts and replaceable lightweight parsers."""

from .contracts import DocumentInput, ParsedAsset, ParsedBlock, ParsedDocument
from .chunking import ParsedDocumentChunker, block_identity
from .parsers import (
    DocumentParseError,
    DocumentParser,
    DocumentParserError,
    DocumentParserRouter,
    DocxDocumentParser,
    ExcelDocumentParser,
    PdfDocumentParser,
    UnsupportedDocumentError,
    default_document_parsers,
)
from .service import IngestionResult, PrivateDocumentIngestionService

__all__ = [
    "DocumentInput",
    "DocumentParseError",
    "DocumentParser",
    "DocumentParserError",
    "DocumentParserRouter",
    "DocxDocumentParser",
    "ExcelDocumentParser",
    "IngestionResult",
    "ParsedAsset",
    "ParsedBlock",
    "ParsedDocument",
    "ParsedDocumentChunker",
    "PdfDocumentParser",
    "UnsupportedDocumentError",
    "PrivateDocumentIngestionService",
    "block_identity",
    "default_document_parsers",
]
