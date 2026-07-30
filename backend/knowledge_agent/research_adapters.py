from __future__ import annotations

from .evidence_repository import PostgresRetrievalPlanRepository
from .research_graph import ScopeEvidenceObservation
from .scope_evidence import ScopeEvidenceService


class PostgresRetrievalPlanAdapter:
    """Resolve the exact ordered scopes for an article outline version."""

    def __init__(self, plans: PostgresRetrievalPlanRepository) -> None:
        self._plans = plans

    def scope_ids(
        self,
        *,
        project_id: str,
        retrieval_plan_id: str,
        article_id: str,
        outline_version: int,
    ) -> tuple[str, ...]:
        plan = self._plans.get_retrieval_plan(project_id, retrieval_plan_id)
        if plan is None:
            raise ValueError("retrieval plan was not found")
        if plan.article_id != article_id or plan.outline_version != outline_version:
            raise ValueError(
                "retrieval plan does not match the requested article outline version"
            )
        return tuple(scope.scope_id for scope in plan.scopes)


class M3ScopeEvidenceAdapter:
    """Expose the deterministic M3 scope service through the M4 graph port."""

    def __init__(self, service: ScopeEvidenceService, *, limit: int = 8) -> None:
        self._service = service
        self._limit = limit

    def retrieve_scope(
        self,
        *,
        project_id: str,
        retrieval_plan_id: str,
        scope_id: str,
    ) -> ScopeEvidenceObservation:
        pack = self._service.build(
            project_id=project_id,
            retrieval_plan_id=retrieval_plan_id,
            scope_id=scope_id,
            limit=self._limit,
        )
        return ScopeEvidenceObservation(
            evidence_pack_id=pack.evidence_pack_id,
            sufficiency=pack.sufficiency,
            gap_reasons=pack.gap_reasons,
            chunk_ids=tuple(hit.chunk.chunk_id for hit in pack.hits),
        )
