from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from typing import Mapping, Protocol, Sequence

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from .contracts import (
    EMBEDDING_DIMENSIONS,
    SOURCE_KINDS,
    TRUST_TIERS,
    KnowledgeChunk,
    RetrievalHit,
    RetrievalProvenance,
    RetrievalQuery,
)
from .interfaces import EmbeddingProvider
from .repository import _chunk_from_row
from .schema import (
    knowledge_chunks,
    knowledge_product_source_evidence,
    knowledge_sources,
    source_snapshots,
)


class HybridRetrievalConfigurationError(ValueError):
    """Raised when weights, filters, or reranker output break M3 invariants."""


class HybridReranker(Protocol):
    """Optional bounded reranker over already project-filtered candidates."""

    def rerank(
        self,
        query: RetrievalQuery,
        candidates: Sequence[RetrievalHit],
    ) -> Mapping[str, float]: ...


@dataclass(frozen=True, slots=True)
class HybridRetrievalConfig:
    vector_weight: float = 0.55
    lexical_weight: float = 0.45
    reranker_weight: float = 0.25
    rrf_k: int = 60
    candidate_multiplier: int = 8
    minimum_candidates: int = 40
    maximum_candidates: int = 200

    def __post_init__(self) -> None:
        for field_name in (
            "vector_weight",
            "lexical_weight",
            "reranker_weight",
        ):
            value = getattr(self, field_name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(float(value))
                or float(value) < 0
            ):
                raise HybridRetrievalConfigurationError(
                    f"{field_name} must be a finite non-negative number"
                )
        if self.vector_weight + self.lexical_weight <= 0:
            raise HybridRetrievalConfigurationError(
                "vector_weight and lexical_weight must not both be zero"
            )
        if not 0 <= self.reranker_weight <= 1:
            raise HybridRetrievalConfigurationError(
                "reranker_weight must be between 0 and 1"
            )
        for field_name in (
            "rrf_k",
            "candidate_multiplier",
            "minimum_candidates",
            "maximum_candidates",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise HybridRetrievalConfigurationError(
                    f"{field_name} must be a positive integer"
                )
        if self.minimum_candidates > self.maximum_candidates:
            raise HybridRetrievalConfigurationError(
                "minimum_candidates must not exceed maximum_candidates"
            )


_SEQUENCE_FILTERS = {
    "source_ids",
    "source_kinds",
    "trust_tiers",
    "canonical_urls",
    "product_ids",
    "heading_contains",
}
_MAPPING_FILTERS = {"chunk_metadata", "source_metadata"}
_SCALAR_FILTERS = {"public_source", "fetched_after"}
_ALLOWED_FILTERS = _SEQUENCE_FILTERS | _MAPPING_FILTERS | _SCALAR_FILTERS


def _string_tuple(value: object, field_name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise HybridRetrievalConfigurationError(
            f"{field_name} must be a sequence of strings"
        )
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise HybridRetrievalConfigurationError(
                f"{field_name} must contain non-empty strings"
            )
        normalized.append(item.strip())
    if not normalized:
        raise HybridRetrievalConfigurationError(
            f"{field_name} must not be empty"
        )
    return tuple(dict.fromkeys(normalized))


def _filter_clauses(filters: Mapping[str, object]) -> tuple[sa.ColumnElement[bool], ...]:
    unknown = sorted(set(filters) - _ALLOWED_FILTERS)
    if unknown:
        raise HybridRetrievalConfigurationError(
            "unsupported retrieval filters: " + ", ".join(unknown)
        )
    clauses: list[sa.ColumnElement[bool]] = []
    if "source_ids" in filters:
        clauses.append(
            knowledge_chunks.c.source_id.in_(
                _string_tuple(filters["source_ids"], "source_ids")
            )
        )
    if "source_kinds" in filters:
        values = _string_tuple(filters["source_kinds"], "source_kinds")
        invalid = sorted(set(values) - SOURCE_KINDS)
        if invalid:
            raise HybridRetrievalConfigurationError(
                "source_kinds contains unsupported values: " + ", ".join(invalid)
            )
        clauses.append(knowledge_sources.c.source_kind.in_(values))
    if "trust_tiers" in filters:
        values = _string_tuple(filters["trust_tiers"], "trust_tiers")
        invalid = sorted(set(values) - TRUST_TIERS)
        if invalid:
            raise HybridRetrievalConfigurationError(
                "trust_tiers contains unsupported values: " + ", ".join(invalid)
            )
        clauses.append(knowledge_sources.c.trust_tier.in_(values))
    if "canonical_urls" in filters:
        clauses.append(
            knowledge_sources.c.canonical_url.in_(
                _string_tuple(filters["canonical_urls"], "canonical_urls")
            )
        )
    if "public_source" in filters:
        value = filters["public_source"]
        if not isinstance(value, bool):
            raise HybridRetrievalConfigurationError(
                "public_source must be a boolean"
            )
        clauses.append(knowledge_sources.c.public_source.is_(value))
    if "fetched_after" in filters:
        value = filters["fetched_after"]
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() is None
        ):
            raise HybridRetrievalConfigurationError(
                "fetched_after must be a timezone-aware datetime"
            )
        clauses.append(source_snapshots.c.fetched_at >= value)
    if "heading_contains" in filters:
        clauses.append(
            knowledge_chunks.c.heading_path.contains(
                list(
                    _string_tuple(
                        filters["heading_contains"],
                        "heading_contains",
                    )
                )
            )
        )
    for filter_name, column in (
        ("chunk_metadata", knowledge_chunks.c.metadata),
        ("source_metadata", knowledge_sources.c.metadata),
    ):
        if filter_name not in filters:
            continue
        value = filters[filter_name]
        if not isinstance(value, Mapping) or not value:
            raise HybridRetrievalConfigurationError(
                f"{filter_name} must be a non-empty mapping"
            )
        clauses.append(column.contains(dict(value)))
    if "product_ids" in filters:
        product_ids = _string_tuple(filters["product_ids"], "product_ids")
        clauses.append(
            sa.exists(
                sa.select(sa.literal(1)).where(
                    knowledge_product_source_evidence.c.project_id
                    == knowledge_chunks.c.project_id,
                    knowledge_product_source_evidence.c.source_id
                    == knowledge_chunks.c.source_id,
                    knowledge_product_source_evidence.c.snapshot_id
                    == knowledge_chunks.c.snapshot_id,
                    knowledge_product_source_evidence.c.product_id.in_(product_ids),
                )
            )
        )
    return tuple(clauses)


def _provenance_from_row(row: Mapping[str, object]) -> RetrievalProvenance:
    return RetrievalProvenance(
        project_id=str(row["project_id"]),
        source_id=str(row["source_id"]),
        snapshot_id=str(row["snapshot_id"]),
        display_name=str(row["source_display_name"]),
        source_kind=str(row["source_kind"]),  # type: ignore[arg-type]
        trust_tier=str(row["trust_tier"]),  # type: ignore[arg-type]
        public_source=bool(row["public_source"]),
        canonical_url=(
            None if row["canonical_url"] is None else str(row["canonical_url"])
        ),
        fetched_at=row["fetched_at"],  # type: ignore[arg-type]
    )


def _select_fields() -> tuple[sa.ColumnElement[object], ...]:
    return (
        knowledge_chunks.c.project_id,
        knowledge_chunks.c.chunk_id,
        knowledge_chunks.c.source_id,
        knowledge_chunks.c.snapshot_id,
        knowledge_chunks.c.ordinal,
        knowledge_chunks.c.heading_path,
        knowledge_chunks.c.text,
        knowledge_chunks.c.locator,
        knowledge_chunks.c.metadata,
        knowledge_sources.c.display_name.label("source_display_name"),
        knowledge_sources.c.source_kind,
        knowledge_sources.c.trust_tier,
        knowledge_sources.c.public_source,
        knowledge_sources.c.canonical_url,
        source_snapshots.c.fetched_at,
    )


class BasicHybridRetriever:
    """Explainable M3 RRF fusion over PostgreSQL FTS and exact pgvector search."""

    def __init__(
        self,
        engine: Engine,
        embedding_provider: EmbeddingProvider,
        *,
        config: HybridRetrievalConfig | None = None,
        reranker: HybridReranker | None = None,
    ) -> None:
        if embedding_provider.dimensions != EMBEDDING_DIMENSIONS:
            raise HybridRetrievalConfigurationError(
                f"embedding provider dimensions must be {EMBEDDING_DIMENSIONS}"
            )
        self._engine = engine
        self._embedding_provider = embedding_provider
        self._config = config or HybridRetrievalConfig()
        self._reranker = reranker

    def retrieve(self, query: RetrievalQuery) -> tuple[RetrievalHit, ...]:
        filters = _filter_clauses(query.filters)
        embedded = self._embedding_provider.embed((query.text,))
        if embedded.count != 1:
            raise ValueError("embedding provider must return one query vector")
        candidate_limit = min(
            self._config.maximum_candidates,
            max(
                self._config.minimum_candidates,
                query.limit * self._config.candidate_multiplier,
            ),
        )

        current = (
            knowledge_chunks.join(
                knowledge_sources,
                sa.and_(
                    knowledge_sources.c.project_id
                    == knowledge_chunks.c.project_id,
                    knowledge_sources.c.source_id
                    == knowledge_chunks.c.source_id,
                    knowledge_sources.c.current_snapshot_id
                    == knowledge_chunks.c.snapshot_id,
                ),
            )
            .join(
                source_snapshots,
                sa.and_(
                    source_snapshots.c.project_id
                    == knowledge_chunks.c.project_id,
                    source_snapshots.c.source_id
                    == knowledge_chunks.c.source_id,
                    source_snapshots.c.snapshot_id
                    == knowledge_chunks.c.snapshot_id,
                ),
            )
        )
        base_where = (
            knowledge_chunks.c.project_id == query.project_id,
            knowledge_sources.c.status == "published",
            knowledge_chunks.c.embedding.is_not(None),
            knowledge_chunks.c.embedding_model == embedded.model_id,
            *filters,
        )
        distance = knowledge_chunks.c.embedding.cosine_distance(
            list(embedded.vectors[0])
        )
        vector_statement = (
            sa.select(*_select_fields(), distance.label("cosine_distance"))
            .select_from(current)
            .where(
                *base_where,
            )
            .order_by(distance.asc(), knowledge_chunks.c.chunk_id.asc())
            .limit(candidate_limit)
        )

        regconfig = sa.literal_column("'simple'::regconfig")
        search_document = sa.func.to_tsvector(
            regconfig,
            knowledge_chunks.c.text,
        )
        search_query = sa.func.websearch_to_tsquery(regconfig, query.text)
        lexical_rank = sa.func.ts_rank_cd(
            search_document,
            search_query,
            32,
        )
        lexical_statement = (
            sa.select(*_select_fields(), lexical_rank.label("lexical_score"))
            .select_from(current)
            .where(
                *base_where,
                search_document.op("@@")(search_query),
            )
            .order_by(lexical_rank.desc(), knowledge_chunks.c.chunk_id.asc())
            .limit(candidate_limit)
        )

        with self._engine.connect() as connection:
            vector_rows = connection.execute(vector_statement).mappings().all()
            lexical_rows = connection.execute(lexical_statement).mappings().all()

        vector_ranks = {
            str(row["chunk_id"]): index
            for index, row in enumerate(vector_rows, start=1)
        }
        lexical_ranks = {
            str(row["chunk_id"]): index
            for index, row in enumerate(lexical_rows, start=1)
        }
        vector_by_id = {str(row["chunk_id"]): row for row in vector_rows}
        lexical_by_id = {str(row["chunk_id"]): row for row in lexical_rows}
        candidate_ids = sorted(set(vector_by_id) | set(lexical_by_id))
        maximum_rrf = (
            self._config.vector_weight / (self._config.rrf_k + 1)
            + self._config.lexical_weight / (self._config.rrf_k + 1)
        )
        hits: list[RetrievalHit] = []
        for chunk_id in candidate_ids:
            row = vector_by_id.get(chunk_id) or lexical_by_id[chunk_id]
            vector_rank = vector_ranks.get(chunk_id)
            lexical_position = lexical_ranks.get(chunk_id)
            vector_component = (
                0.0
                if vector_rank is None
                else self._config.vector_weight
                / (self._config.rrf_k + vector_rank)
            )
            lexical_component = (
                0.0
                if lexical_position is None
                else self._config.lexical_weight
                / (self._config.rrf_k + lexical_position)
            )
            rrf_score = (vector_component + lexical_component) / maximum_rrf
            vector_similarity = (
                None
                if chunk_id not in vector_by_id
                else 1.0 - float(vector_by_id[chunk_id]["cosine_distance"])
            )
            lexical_score = (
                None
                if chunk_id not in lexical_by_id
                else float(lexical_by_id[chunk_id]["lexical_score"])
            )
            hits.append(
                RetrievalHit(
                    chunk=_chunk_from_row(row),
                    score=rrf_score,
                    provenance=_provenance_from_row(row),
                    explanation={
                        "method": "rrf",
                        "vector_rank": vector_rank,
                        "lexical_rank": lexical_position,
                        "vector_similarity": vector_similarity,
                        "lexical_score": lexical_score,
                        "vector_weight": self._config.vector_weight,
                        "lexical_weight": self._config.lexical_weight,
                        "rrf_k": self._config.rrf_k,
                        "rrf_score": rrf_score,
                    },
                )
            )

        if self._reranker is not None and hits:
            reranked = self._reranker.rerank(query, tuple(hits))
            unknown_ids = sorted(set(reranked) - {hit.chunk.chunk_id for hit in hits})
            if unknown_ids:
                raise HybridRetrievalConfigurationError(
                    "reranker returned unknown chunk IDs"
                )
            adjusted: list[RetrievalHit] = []
            for hit in hits:
                reranker_score = reranked.get(hit.chunk.chunk_id)
                if reranker_score is None:
                    adjusted.append(hit)
                    continue
                if (
                    isinstance(reranker_score, bool)
                    or not isinstance(reranker_score, (int, float))
                    or not isfinite(float(reranker_score))
                    or not 0 <= float(reranker_score) <= 1
                ):
                    raise HybridRetrievalConfigurationError(
                        "reranker scores must be finite numbers between 0 and 1"
                    )
                final_score = (
                    (1 - self._config.reranker_weight) * hit.score
                    + self._config.reranker_weight * float(reranker_score)
                )
                adjusted.append(
                    RetrievalHit(
                        chunk=hit.chunk,
                        score=final_score,
                        provenance=hit.provenance,
                        explanation={
                            **dict(hit.explanation),
                            "reranker_score": float(reranker_score),
                            "reranker_weight": self._config.reranker_weight,
                            "final_score": final_score,
                        },
                    )
                )
            hits = adjusted

        hits.sort(key=lambda item: (-item.score, item.chunk.chunk_id))
        return tuple(hits[: query.limit])


__all__ = [
    "BasicHybridRetriever",
    "HybridReranker",
    "HybridRetrievalConfig",
    "HybridRetrievalConfigurationError",
]
