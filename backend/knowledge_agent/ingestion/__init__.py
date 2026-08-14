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
from .benchmark import (
    BenchmarkParser,
    ParserBenchmarkObservation,
    ParserBenchmarkReport,
    ParserQualityExpectation,
    compare_parsers,
)
from .mineru import (
    MINERU_API_KEY_ENV,
    MINERU_CONTENT_LIST_ADAPTER_VERSION,
    MinerUConfigurationError,
    MinerUContentListAdapter,
    MinerUDocumentParser,
    MinerUSettings,
    document_parser_router_from_environment,
)

__all__ = [
    "DocumentInput",
    "DocumentParseError",
    "DocumentParser",
    "DocumentParserError",
    "DocumentParserRouter",
    "DocxDocumentParser",
    "ExcelDocumentParser",
    "IngestionResult",
    "BenchmarkParser",
    "MINERU_CONTENT_LIST_ADAPTER_VERSION",
    "MINERU_API_KEY_ENV",
    "MinerUConfigurationError",
    "MinerUContentListAdapter",
    "MinerUDocumentParser",
    "MinerUSettings",
    "ParsedAsset",
    "ParsedBlock",
    "ParsedDocument",
    "ParsedDocumentChunker",
    "ParserBenchmarkObservation",
    "ParserBenchmarkReport",
    "ParserQualityExpectation",
    "PdfDocumentParser",
    "UnsupportedDocumentError",
    "PrivateDocumentIngestionService",
    "block_identity",
    "compare_parsers",
    "default_document_parsers",
    "document_parser_router_from_environment",
]
