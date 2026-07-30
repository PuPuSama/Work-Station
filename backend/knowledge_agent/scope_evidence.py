from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from .contracts import EvidencePack, RetrievalHit, RetrievalQuery, RetrievalScope
from .evidence import DefaultEvidencePackBuilder
from .evidence_repository import (
    PostgresEvidencePackRepository,
    PostgresRetrievalPlanRepository,
)
from .hybrid_retriever import BasicHybridRetriever


class ScopeEvidenceNotFound(LookupError):
    """Raised when a plan or one of its project-scoped scopes does not exist."""


def normalized_retrieval_filters(scope: RetrievalScope) -> dict[str, object]:
    """Convert persisted JSON filters into the retriever's typed filter values."""

    filters = dict(scope.filters)
    fetched_after = filters.get("fetched_after")
    if isinstance(fetched_after, str):
        try:
            filters["fetched_after"] = datetime.fromisoformat(
                fetched_after.replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise ValueError(
                "fetched_after must be an ISO-8601 timezone-aware datetime"
            ) from exc
    return filters


class ScopeEvidenceService:
    """Run M3 retrieval and persist one immutable outline-scoped Evidence Pack."""

    def __init__(
        self,
        *,
        plans: PostgresRetrievalPlanRepository,
        retriever: BasicHybridRetriever,
        packs: PostgresEvidencePackRepository,
    ) -> None:
        self._plans = plans
        self._retriever = retriever
        self._packs = packs

    def build(
        self,
        *,
        project_id: str,
        retrieval_plan_id: str,
        scope_id: str,
        limit: int = 8,
    ) -> EvidencePack:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        plan = self._plans.get_retrieval_plan(project_id, retrieval_plan_id)
        if plan is None:
            raise ScopeEvidenceNotFound("retrieval plan was not found")
        scope = next((item for item in plan.scopes if item.scope_id == scope_id), None)
        if scope is None:
            raise ScopeEvidenceNotFound("retrieval scope was not found")

        filters = normalized_retrieval_filters(scope)
        candidates: dict[str, tuple[RetrievalHit, list[str]]] = {}
        for query_text in scope.query_variants:
            for hit in self._retriever.retrieve(
                RetrievalQuery(
                    project_id=project_id,
                    text=query_text,
                    limit=limit,
                    filters=filters,
                )
            ):
                existing = candidates.get(hit.chunk.chunk_id)
                if existing is None:
                    candidates[hit.chunk.chunk_id] = (hit, [query_text])
                    continue
                best, matched_queries = existing
                if query_text not in matched_queries:
                    matched_queries.append(query_text)
                if hit.score > best.score:
                    candidates[hit.chunk.chunk_id] = (hit, matched_queries)

        merged_hits = tuple(
            replace(
                hit,
                explanation={
                    **dict(hit.explanation),
                    "matched_query_variants": list(matched_queries),
                },
            )
            for hit, matched_queries in sorted(
                candidates.values(),
                key=lambda item: (-item[0].score, item[0].chunk.chunk_id),
            )[:limit]
        )
        pack = DefaultEvidencePackBuilder(
            minimum_hits=scope.minimum_hits,
            minimum_distinct_sources=scope.minimum_distinct_sources,
            require_hard_fact=scope.require_hard_fact,
        ).build(
            scope.evidence_request(
                article_id=plan.article_id,
                outline_version=plan.outline_version,
            ),
            merged_hits,
        )
        self._packs.save_evidence_pack(pack)
        persisted = self._packs.get_evidence_pack(project_id, pack.evidence_pack_id)
        if persisted is None:
            raise RuntimeError("evidence pack was not persisted")
        return persisted
