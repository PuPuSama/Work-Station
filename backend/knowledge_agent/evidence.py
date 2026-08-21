from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence

from .contracts import (
    EVIDENCE_SOURCE_KINDS,
    EvidenceLink,
    EvidencePack,
    EvidencePackRequest,
    HardFactSentenceTarget,
    KnowledgeCoverageReport,
    RetrievalHit,
    SentenceEvidenceTarget,
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
        *,
        claim_requirements: Sequence[Mapping[str, object]] = (),
    ) -> EvidencePack:
        ordered_hits = tuple(
            hit
            for hit in hits
            if hit.provenance is None
            or hit.provenance.source_kind in EVIDENCE_SOURCE_KINDS
        )
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

        # Hit count is only a coarse guard. A section can have several nearly
        # duplicate chunks while still missing the specific H3 claim it needs.
        # ScopeEvidenceService supplies the immutable requirement metadata from
        # the confirmed outline plan; direct legacy callers may omit it.
        for requirement in claim_requirements:
            requirement_id = str(
                requirement.get("requirement_id") or "claim"
            ).strip()
            raw_queries = requirement.get("query_variants") or ()
            query_variants = {
                str(value).strip()
                for value in raw_queries
                if isinstance(value, str) and value.strip()
            }
            supported_hits = tuple(
                hit
                for hit in ordered_hits
                if query_variants.intersection(
                    {
                        str(value).strip()
                        for value in (
                            hit.explanation.get("matched_query_variants", ())
                            if isinstance(hit.explanation, Mapping)
                            else ()
                        )
                        if isinstance(value, str) and value.strip()
                    }
                )
            )
            try:
                minimum_support = max(
                    1,
                    int(requirement.get("minimum_support") or 1),
                )
            except (TypeError, ValueError):
                minimum_support = 1
            if len(supported_hits) < minimum_support:
                gap_reasons.append(
                    f"claim requirement {requirement_id} lacks "
                    f"{minimum_support} matched evidence hit(s)"
                )
            if bool(requirement.get("require_hard_fact")) and not any(
                hit.chunk.chunk_id in hard_fact_chunk_ids
                for hit in supported_hits
            ):
                gap_reasons.append(
                    f"claim requirement {requirement_id} requires hard-fact evidence"
                )

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
    sentences: Sequence[SentenceEvidenceTarget],
    hard_fact_sentences: Sequence[HardFactSentenceTarget],
    links: Sequence[EvidenceLink],
) -> KnowledgeCoverageReport:
    """Count only current sentence links; stale sentence hashes never carry over."""

    eligible_sentences = tuple(
        sentence
        for sentence in sentences
        if sentence.eligible and sentence.visible_words >= 5
    )
    valid_links = tuple(
        link
        for link in links
        if link.project_id == project_id
        and link.article_id == article_id
        and link.validation_status == "valid"
    )

    def matches_sentence(
        link: EvidenceLink,
        target: SentenceEvidenceTarget | HardFactSentenceTarget,
    ) -> bool:
        if (
            link.support_scope != "sentence"
            or link.sentence_id != target.sentence_id
        ):
            return False
        linked_sentence_hash = str(
            link.metadata.get("sentence_hash") or ""
        ).lower()
        if len(linked_sentence_hash) == 64:
            return linked_sentence_hash == target.sentence_hash
        # Legacy sentence links did not store a sentence hash. They remain
        # usable only while their complete paragraph is unchanged.
        return (
            link.paragraph_id == target.paragraph_id
            and link.paragraph_hash == target.paragraph_hash
        )

    supported_sentence_ids = {
        sentence.sentence_id
        for sentence in eligible_sentences
        if any(
            matches_sentence(link, sentence)
            for link in valid_links
        )
    }
    supported_hard_fact_keys = {
        (target.paragraph_id, target.sentence_id)
        for target in hard_fact_sentences
        if any(
            matches_sentence(link, target)
            and link.claim_type == "hard_fact"
            for link in valid_links
        )
    }

    return KnowledgeCoverageReport(
        eligible_sentences=len(eligible_sentences),
        supported_sentences=len(supported_sentence_ids),
        hard_fact_sentences=len(hard_fact_sentences),
        supported_hard_fact_sentences=len(supported_hard_fact_keys),
    )
