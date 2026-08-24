from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from time import perf_counter
from typing import Callable, Literal, Protocol, TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, RetryPolicy, interrupt

from .contracts import Sufficiency


ResearchRunStatus = Literal[
    "running",
    "waiting_for_review",
    "completed",
    "completed_with_warnings",
]
GapFillChannel = Literal["official_site", "tavily_discovery"]


class ResearchGraphState(TypedDict):
    """Small checkpoint-safe state; customer content stays in domain tables."""

    organization_id: str
    project_id: str
    article_id: str
    outline_version: int
    retrieval_plan_id: str
    thread_id: str
    scope_ids: list[str]
    scope_index: int
    current_scope_id: str
    current_node: str
    gap_fill_round: int
    max_gap_fill_rounds: int
    max_discovery_queries: int
    discovery_queries_used: int
    latest_evidence_pack_id: str
    latest_sufficiency: Sufficiency
    latest_gap_reasons: list[str]
    latest_chunk_ids: list[str]
    discovered_candidates: list[dict[str, object]]
    approved_candidate_urls: list[str]
    published_source_ids: list[str]
    evidence_pack_ids: list[str]
    warnings: list[str]
    status: ResearchRunStatus


@dataclass(frozen=True, slots=True)
class ResearchGraphRequest:
    organization_id: str
    project_id: str
    article_id: str
    outline_version: int
    retrieval_plan_id: str
    thread_id: str
    max_gap_fill_rounds: int = 2
    max_discovery_queries: int = 2
    scope_ids: tuple[str, ...] = ()
    initial_evidence_pack_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "organization_id",
            "project_id",
            "article_id",
            "retrieval_plan_id",
            "thread_id",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} is required")
            object.__setattr__(self, name, value.strip())
        if (
            isinstance(self.outline_version, bool)
            or not isinstance(self.outline_version, int)
            or self.outline_version <= 0
        ):
            raise ValueError("outline_version must be positive")
        if (
            isinstance(self.max_gap_fill_rounds, bool)
            or not isinstance(self.max_gap_fill_rounds, int)
            or not 0 <= self.max_gap_fill_rounds <= 2
        ):
            raise ValueError("max_gap_fill_rounds must be between 0 and 2")
        if (
            isinstance(self.max_discovery_queries, bool)
            or not isinstance(self.max_discovery_queries, int)
            or self.max_discovery_queries < 0
        ):
            raise ValueError("max_discovery_queries must be non-negative")
        for name in ("scope_ids", "initial_evidence_pack_ids"):
            values = tuple(getattr(self, name))
            if any(
                not isinstance(value, str) or not value.strip()
                for value in values
            ):
                raise ValueError(f"{name} must contain non-empty strings")
            if len(set(values)) != len(values):
                raise ValueError(f"{name} must contain unique values")
            object.__setattr__(self, name, values)


@dataclass(frozen=True, slots=True)
class ScopeEvidenceObservation:
    evidence_pack_id: str
    sufficiency: Sufficiency
    gap_reasons: tuple[str, ...]
    chunk_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResearchCandidate:
    candidate_id: str
    url: str
    page_type: str
    needs_review: bool
    evidence: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class CandidateIngestionResult:
    published_source_ids: tuple[str, ...] = ()
    needs_review_candidate_ids: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


class RetrievalPlanPort(Protocol):
    def scope_ids(
        self,
        *,
        project_id: str,
        retrieval_plan_id: str,
        article_id: str,
        outline_version: int,
    ) -> Sequence[str]: ...


class ScopeEvidencePort(Protocol):
    def retrieve_scope(
        self,
        *,
        project_id: str,
        retrieval_plan_id: str,
        scope_id: str,
    ) -> ScopeEvidenceObservation: ...


class OfficialDiscoveryPort(Protocol):
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
    ) -> Sequence[ResearchCandidate]: ...


class CandidateIngestionPort(Protocol):
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
    ) -> CandidateIngestionResult: ...


class ResearchTelemetryPort(Protocol):
    def record_node_attempt(
        self,
        *,
        project_id: str,
        thread_id: str,
        node_name: str,
        scope_id: str | None,
        operation_id: str,
        duration_ms: float,
        outcome: Literal["succeeded", "failed"],
        error_code: str | None,
    ) -> None: ...


def new_research_thread_id(
    project_id: str,
    article_id: str,
    outline_version: int,
) -> str:
    identity = f"{project_id.strip()}:{article_id.strip()}:v{outline_version}"
    prefix = sha256(identity.encode("utf-8")).hexdigest()[:16]
    return f"rg_{prefix}_{uuid4().hex}"


def _attempt_id(state: ResearchGraphState) -> str:
    raw = (
        f"{state['thread_id']}:{state['current_scope_id']}:"
        f"{state['gap_fill_round'] + 1}"
    )
    return "gap_" + sha256(raw.encode("utf-8")).hexdigest()


def _candidate_payload(candidate: ResearchCandidate) -> dict[str, object]:
    return {
        "candidate_id": candidate.candidate_id,
        "url": candidate.url,
        "page_type": candidate.page_type,
        "needs_review": candidate.needs_review,
        "evidence": dict(candidate.evidence),
    }


def _candidate_from_payload(payload: Mapping[str, object]) -> ResearchCandidate:
    return ResearchCandidate(
        candidate_id=str(payload["candidate_id"]),
        url=str(payload["url"]),
        page_type=str(payload["page_type"]),
        needs_review=bool(payload["needs_review"]),
        evidence=dict(payload.get("evidence") or {}),  # type: ignore[arg-type]
    )


class BoundedResearchGraph:
    """LangGraph orchestration shell around deterministic research ports."""

    def __init__(
        self,
        *,
        plans: RetrievalPlanPort,
        evidence: ScopeEvidencePort,
        discovery: OfficialDiscoveryPort,
        ingestion: CandidateIngestionPort,
        checkpointer: object,
        telemetry: ResearchTelemetryPort | None = None,
    ) -> None:
        self._plans = plans
        self._evidence = evidence
        self._discovery = discovery
        self._ingestion = ingestion
        self._telemetry = telemetry
        self._graph = self._compile(checkpointer)

    def _compile(self, checkpointer: object):
        builder = StateGraph(ResearchGraphState)
        network_retry = RetryPolicy(
            initial_interval=0.1,
            backoff_factor=2,
            max_interval=1,
            max_attempts=3,
            jitter=False,
            retry_on=(ConnectionError, TimeoutError, RuntimeError),
        )

        builder.add_node(
            "plan_scopes",
            self._instrument("plan_scopes", self._plan_scopes),
        )
        builder.add_node(
            "retrieve_knowledge",
            self._instrument("retrieve_knowledge", self._retrieve_knowledge),
            retry_policy=network_retry,
        )
        builder.add_node(
            "assess_evidence",
            self._instrument("assess_evidence", self._assess_evidence),
        )
        builder.add_node(
            "discover_official_sources",
            self._instrument(
                "discover_official_sources",
                self._discover_official_sources,
            ),
            retry_policy=network_retry,
        )
        builder.add_node(
            "ingest_candidates",
            self._instrument("ingest_candidates", self._ingest_candidates),
            retry_policy=network_retry,
        )
        builder.add_node(
            "await_human_review",
            self._instrument("await_human_review", self._await_human_review),
        )
        builder.add_node(
            "build_evidence_pack",
            self._instrument("build_evidence_pack", self._build_evidence_pack),
        )
        builder.add_node(
            "finish_with_warning",
            self._instrument("finish_with_warning", self._finish_with_warning),
        )

        builder.add_edge(START, "plan_scopes")
        builder.add_edge("plan_scopes", "retrieve_knowledge")
        builder.add_edge("retrieve_knowledge", "assess_evidence")
        builder.add_conditional_edges(
            "assess_evidence",
            self._route_after_assessment,
            {
                "build": "build_evidence_pack",
                "discover": "discover_official_sources",
                "warning": "finish_with_warning",
            },
        )
        builder.add_conditional_edges(
            "discover_official_sources",
            self._route_after_discovery,
            {
                "review": "await_human_review",
                "ingest": "ingest_candidates",
                "retry": "retrieve_knowledge",
                "warning": "finish_with_warning",
            },
        )
        builder.add_edge("await_human_review", "ingest_candidates")
        builder.add_edge("ingest_candidates", "retrieve_knowledge")
        builder.add_conditional_edges(
            "build_evidence_pack",
            self._route_after_scope,
            {"next": "retrieve_knowledge", "done": END},
        )
        builder.add_conditional_edges(
            "finish_with_warning",
            self._route_after_scope,
            {"next": "retrieve_knowledge", "done": END},
        )
        return builder.compile(checkpointer=checkpointer)

    def _instrument(
        self,
        node_name: str,
        node: Callable[[ResearchGraphState], Mapping[str, object]],
    ) -> Callable[[ResearchGraphState], Mapping[str, object]]:
        def instrumented(state: ResearchGraphState) -> Mapping[str, object]:
            started = perf_counter()
            try:
                result = node(state)
            except Exception as exc:
                self._record_node_attempt(
                    state,
                    node_name=node_name,
                    started=started,
                    outcome="failed",
                    error_code=type(exc).__name__,
                )
                raise
            self._record_node_attempt(
                state,
                node_name=node_name,
                started=started,
                outcome="succeeded",
                error_code=None,
            )
            return result

        return instrumented

    def _record_node_attempt(
        self,
        state: ResearchGraphState,
        *,
        node_name: str,
        started: float,
        outcome: Literal["succeeded", "failed"],
        error_code: str | None,
    ) -> None:
        if self._telemetry is None:
            return
        raw_operation = (
            f"{state['thread_id']}:{node_name}:{state['current_scope_id']}:"
            f"{state['gap_fill_round']}:{state['discovery_queries_used']}"
        )
        operation_id = "node_" + sha256(
            raw_operation.encode("utf-8")
        ).hexdigest()[:32]
        try:
            self._telemetry.record_node_attempt(
                project_id=state["project_id"],
                thread_id=state["thread_id"],
                node_name=node_name,
                scope_id=state["current_scope_id"] or None,
                operation_id=operation_id,
                duration_ms=round((perf_counter() - started) * 1000, 3),
                outcome=outcome,
                error_code=error_code,
            )
        except Exception:
            # Telemetry must not replay a completed external side effect.
            return

    @staticmethod
    def _config(thread_id: str) -> dict[str, dict[str, str]]:
        return {"configurable": {"thread_id": thread_id}}

    def start(self, request: ResearchGraphRequest) -> dict[str, object]:
        initial: ResearchGraphState = {
            "organization_id": request.organization_id,
            "project_id": request.project_id,
            "article_id": request.article_id,
            "outline_version": request.outline_version,
            "retrieval_plan_id": request.retrieval_plan_id,
            "thread_id": request.thread_id,
            "scope_ids": list(request.scope_ids),
            "scope_index": 0,
            "current_scope_id": "",
            "current_node": "start",
            "gap_fill_round": 0,
            "max_gap_fill_rounds": request.max_gap_fill_rounds,
            "max_discovery_queries": request.max_discovery_queries,
            "discovery_queries_used": 0,
            "latest_evidence_pack_id": "",
            "latest_sufficiency": "missing",
            "latest_gap_reasons": [],
            "latest_chunk_ids": [],
            "discovered_candidates": [],
            "approved_candidate_urls": [],
            "published_source_ids": [],
            "evidence_pack_ids": list(request.initial_evidence_pack_ids),
            "warnings": [],
            "status": "running",
        }
        return dict(
            self._graph.invoke(initial, config=self._config(request.thread_id))
        )

    def resume(
        self,
        thread_id: str,
        *,
        approved_urls: Sequence[str],
    ) -> dict[str, object]:
        payload = {"approved_urls": list(approved_urls)}
        return dict(
            self._graph.invoke(
                Command(resume=payload),
                config=self._config(thread_id),
            )
        )

    def continue_run(self, thread_id: str) -> dict[str, object]:
        """Resume the next checkpointed node after a worker/process failure."""

        return dict(
            self._graph.invoke(
                None,
                config=self._config(thread_id),
            )
        )

    def state(self, thread_id: str) -> dict[str, object]:
        snapshot = self._graph.get_state(self._config(thread_id))
        return dict(snapshot.values)

    def history(self, thread_id: str) -> tuple[dict[str, object], ...]:
        return tuple(
            {
                "values": dict(snapshot.values),
                "next": tuple(snapshot.next),
                "metadata": dict(snapshot.metadata),
                "created_at": snapshot.created_at,
            }
            for snapshot in self._graph.get_state_history(
                self._config(thread_id)
            )
        )

    def _plan_scopes(
        self,
        state: ResearchGraphState,
    ) -> Mapping[str, object]:
        all_scope_ids = tuple(
            self._plans.scope_ids(
                project_id=state["project_id"],
                retrieval_plan_id=state["retrieval_plan_id"],
                article_id=state["article_id"],
                outline_version=state["outline_version"],
            )
        )
        if not all_scope_ids:
            raise ValueError("retrieval plan must contain at least one scope")
        requested_scope_ids = tuple(state["scope_ids"])
        if requested_scope_ids:
            if set(requested_scope_ids) - set(all_scope_ids):
                raise ValueError("research scope selection is invalid")
            scope_ids = list(requested_scope_ids)
        else:
            scope_ids = list(all_scope_ids)
        return {
            "scope_ids": scope_ids,
            "scope_index": 0,
            "current_scope_id": scope_ids[0],
            "current_node": "plan_scopes",
            "status": "running",
        }

    def _retrieve_knowledge(
        self,
        state: ResearchGraphState,
    ) -> Mapping[str, object]:
        observation = self._evidence.retrieve_scope(
            project_id=state["project_id"],
            retrieval_plan_id=state["retrieval_plan_id"],
            scope_id=state["current_scope_id"],
        )
        return {
            "current_node": "retrieve_knowledge",
            "latest_evidence_pack_id": observation.evidence_pack_id,
            "latest_sufficiency": observation.sufficiency,
            "latest_gap_reasons": list(observation.gap_reasons),
            "latest_chunk_ids": list(observation.chunk_ids),
            "discovered_candidates": [],
            "approved_candidate_urls": [],
            "status": "running",
        }

    @staticmethod
    def _assess_evidence(
        state: ResearchGraphState,
    ) -> Mapping[str, object]:
        return {"current_node": "assess_evidence"}

    @staticmethod
    def _route_after_assessment(
        state: ResearchGraphState,
    ) -> Literal["build", "discover", "warning"]:
        if state["latest_sufficiency"] == "sufficient":
            return "build"
        if (
            state["gap_fill_round"] < state["max_gap_fill_rounds"]
            and state["discovery_queries_used"] < state["max_discovery_queries"]
        ):
            return "discover"
        return "warning"

    def _discover_official_sources(
        self,
        state: ResearchGraphState,
    ) -> Mapping[str, object]:
        round_number = state["gap_fill_round"] + 1
        candidates = tuple(
            self._discovery.discover(
                project_id=state["project_id"],
                thread_id=state["thread_id"],
                article_id=state["article_id"],
                retrieval_plan_id=state["retrieval_plan_id"],
                scope_id=state["current_scope_id"],
                round_number=round_number,
                gap_reasons=state["latest_gap_reasons"],
                attempt_id=_attempt_id(state),
            )
        )
        return {
            "current_node": "discover_official_sources",
            "discovery_queries_used": state["discovery_queries_used"] + 1,
            "discovered_candidates": [
                _candidate_payload(candidate) for candidate in candidates
            ],
            "approved_candidate_urls": [
                candidate.url for candidate in candidates if not candidate.needs_review
            ],
            "status": (
                "waiting_for_review"
                if any(candidate.needs_review for candidate in candidates)
                else "running"
            ),
        }

    @staticmethod
    def _route_after_discovery(
        state: ResearchGraphState,
    ) -> Literal["review", "ingest", "retry", "warning"]:
        candidates = state["discovered_candidates"]
        if any(bool(candidate.get("needs_review")) for candidate in candidates):
            return "review"
        return "ingest"

    @staticmethod
    def _await_human_review(
        state: ResearchGraphState,
    ) -> Mapping[str, object]:
        review_candidates = [
            candidate
            for candidate in state["discovered_candidates"]
            if bool(candidate.get("needs_review"))
        ]
        decision = interrupt(
            {
                "type": "research_candidate_review",
                "project_id": state["project_id"],
                "article_id": state["article_id"],
                "scope_id": state["current_scope_id"],
                "round_number": state["gap_fill_round"] + 1,
                "candidates": review_candidates,
            }
        )
        if not isinstance(decision, Mapping):
            raise ValueError("review decision must be a mapping")
        requested = decision.get("approved_urls", [])
        if isinstance(requested, (str, bytes)) or not isinstance(
            requested, Sequence
        ):
            raise ValueError("approved_urls must be a sequence")
        known_urls = {
            str(candidate["url"]) for candidate in state["discovered_candidates"]
        }
        approved = [str(url) for url in requested]
        unknown = sorted(set(approved) - known_urls)
        if unknown:
            raise ValueError("approved_urls contains unknown candidates")
        automatic = [
            str(candidate["url"])
            for candidate in state["discovered_candidates"]
            if not bool(candidate.get("needs_review"))
        ]
        return {
            "current_node": "await_human_review",
            "approved_candidate_urls": list(dict.fromkeys((*automatic, *approved))),
            "status": "running",
        }

    def _ingest_candidates(
        self,
        state: ResearchGraphState,
    ) -> Mapping[str, object]:
        round_number = state["gap_fill_round"] + 1
        result = self._ingestion.ingest(
            project_id=state["project_id"],
            thread_id=state["thread_id"],
            retrieval_plan_id=state["retrieval_plan_id"],
            scope_id=state["current_scope_id"],
            round_number=round_number,
            candidates=tuple(
                _candidate_from_payload(candidate)
                for candidate in state["discovered_candidates"]
            ),
            approved_urls=state["approved_candidate_urls"],
            attempt_id=_attempt_id(state),
        )
        return {
            "current_node": "ingest_candidates",
            "gap_fill_round": round_number,
            "published_source_ids": list(
                dict.fromkeys(
                    (*state["published_source_ids"], *result.published_source_ids)
                )
            ),
            "warnings": [*state["warnings"], *result.warnings],
            "discovered_candidates": [],
            "approved_candidate_urls": [],
            "status": "running",
        }

    @staticmethod
    def _advance_scope(
        state: ResearchGraphState,
        *,
        warning: str | None = None,
    ) -> Mapping[str, object]:
        next_index = state["scope_index"] + 1
        is_done = next_index >= len(state["scope_ids"])
        warnings = list(state["warnings"])
        if warning is not None:
            warnings.append(warning)
        return {
            "scope_index": next_index,
            "current_scope_id": (
                state["current_scope_id"]
                if is_done
                else state["scope_ids"][next_index]
            ),
            "gap_fill_round": 0,
            "latest_evidence_pack_id": "",
            "latest_sufficiency": "missing",
            "latest_gap_reasons": [],
            "latest_chunk_ids": [],
            "discovered_candidates": [],
            "approved_candidate_urls": [],
            "warnings": warnings,
            "status": (
                "completed_with_warnings"
                if is_done and warnings
                else "completed"
                if is_done
                else "running"
            ),
        }

    def _build_evidence_pack(
        self,
        state: ResearchGraphState,
    ) -> Mapping[str, object]:
        update = dict(self._advance_scope(state))
        update.update(
            {
                "current_node": "build_evidence_pack",
                "evidence_pack_ids": list(
                    dict.fromkeys(
                        (
                            *state["evidence_pack_ids"],
                            state["latest_evidence_pack_id"],
                        )
                    )
                ),
            }
        )
        return update

    def _finish_with_warning(
        self,
        state: ResearchGraphState,
    ) -> Mapping[str, object]:
        reasons = "; ".join(state["latest_gap_reasons"]) or "evidence remained weak"
        warning = (
            f"scope {state['current_scope_id']} finished after "
            f"{state['gap_fill_round']} gap-fill rounds: {reasons}"
        )
        update = dict(self._advance_scope(state, warning=warning))
        pack_ids = list(state["evidence_pack_ids"])
        if state["latest_evidence_pack_id"]:
            pack_ids.append(state["latest_evidence_pack_id"])
        update.update(
            {
                "current_node": "finish_with_warning",
                "evidence_pack_ids": list(dict.fromkeys(pack_ids)),
            }
        )
        return update

    @staticmethod
    def _route_after_scope(
        state: ResearchGraphState,
    ) -> Literal["next", "done"]:
        if state["scope_index"] >= len(state["scope_ids"]):
            return "done"
        return "next"
