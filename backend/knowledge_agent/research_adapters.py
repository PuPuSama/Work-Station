from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from hashlib import sha256
from typing import Callable, Protocol

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from .contracts import KnowledgeSource
from .evidence_repository import PostgresRetrievalPlanRepository
from .interfaces import KnowledgeRepository
from .library import PostgresKnowledgeLibrary
from .publication import KnowledgePublicationService
from .research_graph import (
    CandidateIngestionResult,
    ResearchCandidate,
    ScopeEvidenceObservation,
)
from .research_runs import (
    GapFillAttempt,
    PostgresResearchRunRepository,
    ResearchRunConflictError,
)
from .schema import projects
from .scope_evidence import ScopeEvidenceService
from .web_ingestion import OfficialWebPageIngestionService
from .wordpress import (
    UnsafeOfficialSiteUrl,
    WordPressIngestionError,
    normalize_official_url,
)


class OfficialSearchResultPort(Protocol):
    url: str
    score: float | None


class OfficialSearchResponsePort(Protocol):
    results: Sequence[OfficialSearchResultPort]
    request_id: str


class OfficialSearchPort(Protocol):
    def search(
        self,
        query: str,
        host: str,
        max_results: int = 5,
    ) -> OfficialSearchResponsePort: ...


class PostgresProjectDirectory:
    """Resolve project policy data without leaking another project's row."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def official_domain(self, project_id: str) -> str:
        with self._engine.connect() as connection:
            value = connection.execute(
                sa.select(projects.c.official_domain).where(
                    projects.c.project_id == project_id,
                    projects.c.status == "active",
                )
            ).scalar_one_or_none()
        if value is None:
            raise ValueError("active knowledge project was not found")
        return str(value)


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


def _candidate_id(url: str) -> str:
    return "candidate_" + sha256(url.encode("utf-8")).hexdigest()[:32]


def _candidate_from_recorded_url(url: str, *, reused: bool) -> ResearchCandidate:
    return ResearchCandidate(
        candidate_id=_candidate_id(url),
        url=url,
        page_type="unknown",
        needs_review=True,
        evidence={
            "channel": "tavily_discovery",
            "same_site": True,
            "reused_attempt": reused,
        },
    )


class TavilyOfficialDiscoveryAdapter:
    """Discover same-site URLs only; never ingest or publish search snippets."""

    def __init__(
        self,
        *,
        projects: PostgresProjectDirectory,
        plans: PostgresRetrievalPlanRepository,
        search: OfficialSearchPort,
        attempts: PostgresResearchRunRepository,
        max_results: int = 5,
    ) -> None:
        if not 1 <= max_results <= 20:
            raise ValueError("max_results must be between 1 and 20")
        self._projects = projects
        self._plans = plans
        self._search = search
        self._attempts = attempts
        self._max_results = max_results

    def discover(
        self,
        *,
        project_id: str,
        thread_id: str,
        article_id: str,
        retrieval_plan_id: str,
        scope_id: str,
        round_number: int,
        gap_reasons: Sequence[str],
        attempt_id: str,
    ) -> tuple[ResearchCandidate, ...]:
        plan = self._plans.get_retrieval_plan(project_id, retrieval_plan_id)
        if plan is None:
            raise ValueError("retrieval plan was not found")
        if plan.article_id != article_id:
            raise ValueError("retrieval plan does not belong to the article")
        scope = next((item for item in plan.scopes if item.scope_id == scope_id), None)
        if scope is None:
            raise ValueError("retrieval scope was not found")
        query_parts = (
            scope.title,
            *scope.query_variants[:2],
            *tuple(gap_reasons)[:2],
        )
        query = " ".join(dict.fromkeys(part.strip() for part in query_parts if part.strip()))
        reason = "; ".join(reason.strip() for reason in gap_reasons if reason.strip())
        reason = reason or "evidence remained weak"

        existing = self._attempts.get_gap_attempt_by_id(attempt_id)
        if existing is not None:
            self._validate_attempt_identity(
                existing,
                project_id=project_id,
                thread_id=thread_id,
                retrieval_plan_id=retrieval_plan_id,
                scope_id=scope_id,
                round_number=round_number,
                query=query,
            )
            return tuple(
                _candidate_from_recorded_url(url, reused=True)
                for url in existing.discovered_urls
            )

        domain = self._projects.official_domain(project_id)
        site_url = f"https://{domain}"
        response = self._search.search(
            query,
            host=domain,
            max_results=self._max_results,
        )
        urls: list[str] = []
        scores: dict[str, float | None] = {}
        for item in response.results:
            try:
                url = normalize_official_url(site_url, item.url)
            except (UnsafeOfficialSiteUrl, ValueError):
                continue
            if url in scores:
                continue
            urls.append(url)
            scores[url] = item.score
        recorded = self._attempts.record_gap_attempt(
            GapFillAttempt(
                project_id=project_id,
                thread_id=thread_id,
                retrieval_plan_id=retrieval_plan_id,
                scope_id=scope_id,
                round_number=round_number,
                attempt_id=attempt_id,
                reason=reason,
                channel="tavily_discovery",
                query=query,
                discovered_urls=tuple(urls),
                result="pending",
                cost_usage={
                    "queries": 1,
                    "result_count": len(urls),
                    "request_id_present": bool(response.request_id),
                },
            )
        )
        return tuple(
            ResearchCandidate(
                candidate_id=_candidate_id(url),
                url=url,
                page_type="unknown",
                needs_review=True,
                evidence={
                    "channel": "tavily_discovery",
                    "same_site": True,
                    "score": scores[url],
                    "reused_attempt": False,
                },
            )
            for url in recorded.discovered_urls
        )

    @staticmethod
    def _validate_attempt_identity(
        attempt: GapFillAttempt,
        *,
        project_id: str,
        thread_id: str,
        retrieval_plan_id: str,
        scope_id: str,
        round_number: int,
        query: str,
    ) -> None:
        if (
            attempt.project_id != project_id
            or attempt.thread_id != thread_id
            or attempt.retrieval_plan_id != retrieval_plan_id
            or attempt.scope_id != scope_id
            or attempt.round_number != round_number
            or attempt.query != query
        ):
            raise ResearchRunConflictError(
                "gap-fill attempt does not match the requested graph scope"
            )


class OfficialCandidateIngestionAdapter:
    """Fetch approved URLs, apply deterministic classification, then publish."""

    def __init__(
        self,
        *,
        projects: PostgresProjectDirectory,
        web_ingestion: OfficialWebPageIngestionService,
        repository: KnowledgeRepository,
        library: PostgresKnowledgeLibrary,
        publication: KnowledgePublicationService,
        attempts: PostgresResearchRunRepository,
        minimum_auto_publish_confidence: float = 0.75,
        review_source: Callable[[KnowledgeSource, str, str], None] | None = None,
        publish_source: Callable[[KnowledgeSource, str], str] | None = None,
        authorize_candidate: Callable[[], None] | None = None,
    ) -> None:
        if not 0 <= minimum_auto_publish_confidence <= 1:
            raise ValueError("minimum_auto_publish_confidence must be between 0 and 1")
        self._projects = projects
        self._web_ingestion = web_ingestion
        self._repository = repository
        self._library = library
        self._publication = publication
        self._attempts = attempts
        self._minimum_confidence = minimum_auto_publish_confidence
        self._review_source = review_source or self._default_review_source
        self._publish_source = publish_source or self._default_publish_source
        self._authorize_candidate = authorize_candidate or (lambda: None)

    def ingest(
        self,
        *,
        project_id: str,
        thread_id: str,
        retrieval_plan_id: str,
        scope_id: str,
        round_number: int,
        candidates: Sequence[ResearchCandidate],
        approved_urls: Sequence[str],
        attempt_id: str,
    ) -> CandidateIngestionResult:
        attempt = self._attempts.get_gap_attempt_by_id(attempt_id)
        if attempt is None:
            raise ResearchRunConflictError("gap-fill discovery attempt was not recorded")
        if (
            attempt.project_id != project_id
            or attempt.thread_id != thread_id
            or attempt.retrieval_plan_id != retrieval_plan_id
            or attempt.scope_id != scope_id
            or attempt.round_number != round_number
        ):
            raise ResearchRunConflictError(
                "gap-fill attempt does not belong to the requested graph scope"
            )
        if attempt.result != "pending":
            return CandidateIngestionResult(
                published_source_ids=attempt.published_source_ids,
                warnings=(
                    ()
                    if attempt.result == "improved"
                    else ("Recorded gap-fill attempt did not publish new evidence.",)
                ),
            )

        candidate_by_url = {candidate.url: candidate for candidate in candidates}
        approved = tuple(dict.fromkeys(url.strip() for url in approved_urls if url.strip()))
        unknown = sorted(set(approved) - set(candidate_by_url))
        if unknown:
            raise ValueError("approved_urls contains unknown candidates")
        domain = self._projects.official_domain(project_id)
        site_url = f"https://{domain}"
        published: list[str] = []
        needs_review: list[str] = []
        warnings: list[str] = []

        for url in approved:
            candidate = candidate_by_url[url]
            try:
                # Server mode injects a fresh authorization check here so a
                # revoked actor cannot continue fetching later candidates.
                self._authorize_candidate()
                normalized_url = normalize_official_url(site_url, url)
                result = self._web_ingestion.ingest_url(
                    project_id=project_id,
                    site_url=site_url,
                    url=normalized_url,
                    metadata={
                        "research_graph": {
                            "thread_id": thread_id,
                            "scope_id": scope_id,
                            "round_number": round_number,
                            "attempt_id": attempt_id,
                        }
                    },
                )
            except (UnsafeOfficialSiteUrl, WordPressIngestionError):
                needs_review.append(candidate.candidate_id)
                warnings.append(
                    f"Candidate {candidate.candidate_id} could not pass deterministic "
                    "official-page classification."
                )
                continue

            current = self._library.get_source(project_id, result.source.source_id)
            if (
                current is not None
                and current.status == "published"
                and current.current_snapshot_id == result.snapshot.snapshot_id
            ):
                published.append(result.source.source_id)
                continue

            if result.classification.confidence < self._minimum_confidence:
                self._review_source(
                    result.source,
                    "needs_review",
                    "deterministic classification confidence is below the gate",
                )
                needs_review.append(candidate.candidate_id)
                warnings.append(
                    f"Candidate {candidate.candidate_id} remains in Research Inbox "
                    "for classification review."
                )
                continue

            self._review_source(
                result.source,
                "approve",
                (
                    "same-site URL was human-approved and deterministic "
                    "classification passed the automated publication gate"
                ),
            )
            published_source_id = self._publish_source(
                result.source,
                result.snapshot.snapshot_id,
            )
            published.append(published_source_id)

        final_result = (
            "improved"
            if published
            else "blocked"
            if needs_review
            else "no_change"
        )
        completed = self._attempts.record_gap_attempt(
            GapFillAttempt(
                project_id=attempt.project_id,
                thread_id=attempt.thread_id,
                retrieval_plan_id=attempt.retrieval_plan_id,
                scope_id=attempt.scope_id,
                round_number=attempt.round_number,
                attempt_id=attempt.attempt_id,
                reason=attempt.reason,
                channel=attempt.channel,
                query=attempt.query,
                discovered_urls=attempt.discovered_urls,
                published_source_ids=tuple(dict.fromkeys(published)),
                result=final_result,  # type: ignore[arg-type]
                cost_usage=dict(attempt.cost_usage),
            )
        )
        return CandidateIngestionResult(
            published_source_ids=completed.published_source_ids,
            needs_review_candidate_ids=tuple(dict.fromkeys(needs_review)),
            warnings=tuple(warnings),
        )

    def _default_review_source(
        self,
        source: KnowledgeSource,
        decision: str,
        reason: str,
    ) -> None:
        metadata = dict(source.metadata)
        metadata["review"] = {
            "decision": decision,
            "reason": reason,
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
            "actor": "research_graph_publication_gate",
        }
        reviewed = KnowledgeSource(
            project_id=source.project_id,
            source_id=source.source_id,
            display_name=source.display_name,
            source_kind=source.source_kind,
            trust_tier=source.trust_tier,
            status=("inbox" if decision == "approve" else "needs_review"),
            canonical_url=source.canonical_url,
            public_source=source.public_source,
            metadata=metadata,
        )
        self._repository.upsert_source(reviewed)

    def _default_publish_source(
        self,
        source: KnowledgeSource,
        snapshot_id: str,
    ) -> str:
        publication = self._publication.publish(
            project_id=source.project_id,
            source_id=source.source_id,
            snapshot_id=snapshot_id,
        )
        return publication.source_id
