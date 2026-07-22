"""Stable boundaries for the optional knowledge and research agent."""

from .contracts import (
    DiscoveryRequest,
    EvidencePack,
    EvidencePackRequest,
    KnowledgeChunk,
    ResearchRequest,
    ResearchResult,
    RetrievalHit,
    RetrievalQuery,
    SourceCandidate,
)
from .interfaces import (
    EvidencePackBuilder,
    KnowledgeRepository,
    KnowledgeRetriever,
    ResearchOrchestrator,
    SourceDiscovery,
)

__all__ = [
    "DiscoveryRequest",
    "EvidencePack",
    "EvidencePackBuilder",
    "EvidencePackRequest",
    "KnowledgeChunk",
    "KnowledgeRepository",
    "KnowledgeRetriever",
    "ResearchOrchestrator",
    "ResearchRequest",
    "ResearchResult",
    "RetrievalHit",
    "RetrievalQuery",
    "SourceCandidate",
    "SourceDiscovery",
]
