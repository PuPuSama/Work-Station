from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

from .contracts import (
    EvidenceLink,
    EvidencePack,
    EvidencePackRequest,
    HardFactSentenceTarget,
    KnowledgeCoverageReport,
    ParagraphEvidenceTarget,
    RetrievalHit,
)


def _stable_evidence_pack_id(
    request: EvidencePackRequest,
    hits: Sequence[RetrievalHit],
) -> str:
    identity = {
        "project_id": request.project_id,
        "article_id": request.article_id,
        "outline_version": request.outline_version,
        "retrieval_plan_id": request.retrieval_plan_id,
        "scope_id": request.scope_id,
        "scope_type": request.scope_type,
        "scope_key": request.scope_key,
        "query_variants": request.query_variants,
        "hits": [
            {
                "chunk_id": hit.chunk.chunk_id,
                "source_id": hit.chunk.source_id,
                "snapshot_id": hit.chunk.snapshot_id,
            }
            for hit in hits
        ],
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"ep_{digest}"


class DefaultEvidencePackBuilder:
    """Apply deterministic M3 sufficiency rules to already-scoped retrieval hits."""

    def __init__(
        self,
        *,
        minimum_hits: int = 2,
        minimum_distinct_sources: int = 1,
        require_hard_fact: bool = False,
    ) -> None:
        for name, value in (
            ("minimum_hits", minimum_hits),
            ("minimum_distinct_sources", minimum_distinct_sources),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if not isinstance(require_hard_fact, bool):
            raise ValueError("require_hard_fact must be a boolean")
        self._minimum_hits = minimum_hits
        self._minimum_distinct_sources = minimum_distinct_sources
        self._require_hard_fact = require_hard_fact

    def build(
        self,
        request: EvidencePackRequest,
        hits: Sequence[RetrievalHit],
    ) -> EvidencePack:
        ordered_hits = tuple(hits)
        if any(hit.project_id != request.project_id for hit in ordered_hits):
            raise ValueError("evidence hits must belong to the same project")

        hard_fact_chunk_ids = tuple(
            hit.chunk.chunk_id
            for hit in ordered_hits
            if hit.provenance is not None
            and hit.provenance.trust_tier == "hard_fact"
        )
        public_citation_urls = tuple(
            dict.fromkeys(
                hit.provenance.canonical_url
                for hit in ordered_hits
                if hit.provenance is not None
                and hit.provenance.public_source
                and hit.provenance.canonical_url is not None
            )
        )

        gap_reasons: list[str] = []
        distinct_sources = {
            (hit.chunk.source_id, hit.chunk.snapshot_id) for hit in ordered_hits
        }
        if len(ordered_hits) < self._minimum_hits:
            gap_reasons.append(
                f"requires at least {self._minimum_hits} evidence hits"
            )
        if len(distinct_sources) < self._minimum_distinct_sources:
            gap_reasons.append(
                "requires at least "
                f"{self._minimum_distinct_sources} distinct source snapshots"
            )
        if self._require_hard_fact and not hard_fact_chunk_ids:
            gap_reasons.append("requires hard-fact evidence")

        if not ordered_hits:
            sufficiency = "missing"
        elif gap_reasons:
            sufficiency = "weak"
        else:
            sufficiency = "sufficient"

        return EvidencePack(
            evidence_pack_id=_stable_evidence_pack_id(request, ordered_hits),
            request=request,
            hits=ordered_hits,
            sufficiency=sufficiency,
            gap_reasons=tuple(gap_reasons),
            hard_fact_chunk_ids=hard_fact_chunk_ids,
            public_citation_urls=public_citation_urls,
        )


def calculate_knowledge_coverage(
    *,
    project_id: str,
    article_id: str,
    paragraphs: Sequence[ParagraphEvidenceTarget],
    hard_fact_sentences: Sequence[HardFactSentenceTarget],
    links: Sequence[EvidenceLink],
) -> KnowledgeCoverageReport:
    """Count only current-content links; stale paragraph hashes never carry over."""

    eligible_paragraphs = tuple(
        paragraph
        for paragraph in paragraphs
        if paragraph.eligible and paragraph.visible_words >= 5
    )
    valid_links = tuple(
        link
        for link in links
        if link.project_id == project_id
        and link.article_id == article_id
        and link.validation_status == "valid"
    )

    supported_paragraph_ids = {
        paragraph.paragraph_id
        for paragraph in eligible_paragraphs
        if any(
            link.paragraph_id == paragraph.paragraph_id
            and link.paragraph_hash == paragraph.paragraph_hash
            for link in valid_links
        )
    }
    supported_hard_fact_keys = {
        (target.paragraph_id, target.sentence_id)
        for target in hard_fact_sentences
        if any(
            link.paragraph_id == target.paragraph_id
            and link.sentence_id == target.sentence_id
            and link.paragraph_hash == target.paragraph_hash
            and link.support_scope == "sentence"
            and link.claim_type == "hard_fact"
            for link in valid_links
        )
    }

    return KnowledgeCoverageReport(
        eligible_paragraphs=len(eligible_paragraphs),
        supported_paragraphs=len(supported_paragraph_ids),
        hard_fact_sentences=len(hard_fact_sentences),
        supported_hard_fact_sentences=len(supported_hard_fact_keys),
    )
