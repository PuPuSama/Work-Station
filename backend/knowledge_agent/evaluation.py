from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from math import ceil, isfinite
from pathlib import Path
from time import perf_counter
from typing import Literal
from urllib.parse import urlsplit

from .contracts import SOURCE_KINDS, RetrievalHit, RetrievalQuery
from .interfaces import KnowledgeRetriever


AnnotationStatus = Literal["pending", "approved"]
EvidenceSufficiency = Literal["missing", "weak", "sufficient"]


@dataclass(frozen=True, slots=True)
class RetrievalEvaluationCase:
    case_id: str
    project_id: str
    query: str
    expected_source_ids: tuple[str, ...] = ()
    allowed_source_kinds: tuple[str, ...] = ()
    forbidden_canonical_urls: tuple[str, ...] = ()
    expects_refusal: bool = False
    annotation_status: AnnotationStatus = "pending"
    notes: str = ""

    def __post_init__(self) -> None:
        for name in ("case_id", "project_id", "query"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} is required")
            object.__setattr__(self, name, value.strip())
        for name in (
            "expected_source_ids",
            "allowed_source_kinds",
            "forbidden_canonical_urls",
        ):
            values = getattr(self, name)
            if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
                raise ValueError(f"{name} must be a sequence of strings")
            if any(not isinstance(value, str) or not value.strip() for value in values):
                raise ValueError(f"{name} must contain non-empty strings")
            object.__setattr__(
                self,
                name,
                tuple(dict.fromkeys(value.strip() for value in values)),
            )
        invalid_source_kinds = sorted(
            set(self.allowed_source_kinds) - SOURCE_KINDS
        )
        if invalid_source_kinds:
            raise ValueError(
                "allowed_source_kinds contains unsupported values: "
                + ", ".join(invalid_source_kinds)
            )
        for value in self.forbidden_canonical_urls:
            parsed = urlsplit(value)
            if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
                raise ValueError(
                    "forbidden_canonical_urls must contain absolute HTTP(S) URLs"
                )
        if self.annotation_status not in {"pending", "approved"}:
            raise ValueError("annotation_status must be pending or approved")
        if self.expects_refusal and self.expected_source_ids:
            raise ValueError("refusal cases must not define expected_source_ids")
        if (
            self.annotation_status == "approved"
            and not self.expects_refusal
            and not self.expected_source_ids
        ):
            raise ValueError(
                "approved answerable cases require expected_source_ids"
            )


@dataclass(frozen=True, slots=True)
class RetrievalEvaluationObservation:
    case_id: str
    latency_ms: float
    hit_chunk_ids: tuple[str, ...]
    hit_source_ids: tuple[str, ...]
    hit_source_kinds: tuple[str | None, ...]
    hit_canonical_urls: tuple[str | None, ...]
    hit_scores: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class RetrievalEvaluationMetrics:
    case_count: int
    answerable_case_count: int
    refusal_case_count: int
    recall_at_k: float
    mean_reciprocal_rank: float
    first_hit_source_kind_accuracy: float
    wrong_source_rate: float
    correct_refusal_rate: float
    latency_p50_ms: float
    latency_p95_ms: float


@dataclass(frozen=True, slots=True)
class RetrievalEvaluationReport:
    retriever_name: str
    k: int
    metrics: RetrievalEvaluationMetrics
    observations: tuple[RetrievalEvaluationObservation, ...]
    metadata: Mapping[str, object] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)


@dataclass(frozen=True, slots=True)
class EvidenceImprovementObservation:
    """One scope before and after bounded gap filling."""

    case_id: str
    scope_id: str
    before_sufficiency: EvidenceSufficiency
    after_sufficiency: EvidenceSufficiency
    before_hit_count: int
    after_hit_count: int
    gap_fill_attempts: int
    published_source_ids: tuple[str, ...] = ()
    cost_usd: float = 0.0
    before_hard_fact_coverage: float | None = None
    after_hard_fact_coverage: float | None = None

    def __post_init__(self) -> None:
        for name in ("case_id", "scope_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} is required")
            object.__setattr__(self, name, value.strip())
        for name in ("before_sufficiency", "after_sufficiency"):
            if getattr(self, name) not in {"missing", "weak", "sufficient"}:
                raise ValueError(f"{name} must be missing, weak, or sufficient")
        for name in ("before_hit_count", "after_hit_count", "gap_fill_attempts"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if (
            isinstance(self.cost_usd, bool)
            or not isinstance(self.cost_usd, (int, float))
            or not isfinite(float(self.cost_usd))
            or self.cost_usd < 0
        ):
            raise ValueError("cost_usd must be a finite non-negative number")
        object.__setattr__(self, "cost_usd", float(self.cost_usd))
        sources = tuple(
            source_id.strip()
            for source_id in self.published_source_ids
            if isinstance(source_id, str) and source_id.strip()
        )
        if len(sources) != len(self.published_source_ids):
            raise ValueError("published_source_ids must contain non-empty strings")
        object.__setattr__(
            self,
            "published_source_ids",
            tuple(dict.fromkeys(sources)),
        )
        coverage_values = (
            self.before_hard_fact_coverage,
            self.after_hard_fact_coverage,
        )
        if (coverage_values[0] is None) != (coverage_values[1] is None):
            raise ValueError(
                "before and after hard-fact coverage must be provided together"
            )
        for value in coverage_values:
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(float(value))
                or not 0 <= float(value) <= 1
            ):
                raise ValueError("hard-fact coverage must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class EvidenceImprovementMetrics:
    scope_count: int
    improved_scope_rate: float
    sufficient_before_rate: float
    sufficient_after_rate: float
    mean_hit_delta: float
    mean_hard_fact_coverage_before: float
    mean_hard_fact_coverage_after: float
    total_gap_fill_attempts: int
    published_source_count: int
    total_cost_usd: float


@dataclass(frozen=True, slots=True)
class EvidenceImprovementReport:
    metrics: EvidenceImprovementMetrics
    observations: tuple[EvidenceImprovementObservation, ...]

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)


def evaluate_evidence_improvement(
    observations: Sequence[EvidenceImprovementObservation],
) -> EvidenceImprovementReport:
    """Aggregate M4/M5 telemetry without treating discovery as automatically useful."""

    items = tuple(observations)
    if not items:
        raise ValueError("at least one evidence improvement observation is required")
    sufficiency_score = {"missing": 0, "weak": 1, "sufficient": 2}
    before_coverage = tuple(
        float(item.before_hard_fact_coverage)
        for item in items
        if item.before_hard_fact_coverage is not None
    )
    after_coverage = tuple(
        float(item.after_hard_fact_coverage)
        for item in items
        if item.after_hard_fact_coverage is not None
    )
    published_sources = {
        source_id for item in items for source_id in item.published_source_ids
    }
    metrics = EvidenceImprovementMetrics(
        scope_count=len(items),
        improved_scope_rate=(
            sum(
                sufficiency_score[item.after_sufficiency]
                > sufficiency_score[item.before_sufficiency]
                for item in items
            )
            / len(items)
        ),
        sufficient_before_rate=(
            sum(item.before_sufficiency == "sufficient" for item in items)
            / len(items)
        ),
        sufficient_after_rate=(
            sum(item.after_sufficiency == "sufficient" for item in items)
            / len(items)
        ),
        mean_hit_delta=_mean(
            item.after_hit_count - item.before_hit_count for item in items
        ),
        mean_hard_fact_coverage_before=_mean(before_coverage),
        mean_hard_fact_coverage_after=_mean(after_coverage),
        total_gap_fill_attempts=sum(item.gap_fill_attempts for item in items),
        published_source_count=len(published_sources),
        total_cost_usd=sum(item.cost_usd for item in items),
    )
    return EvidenceImprovementReport(metrics=metrics, observations=items)


def load_evaluation_cases(
    path: Path,
    *,
    approved_only: bool = True,
) -> tuple[RetrievalEvaluationCase, ...]:
    cases: list[RetrievalEvaluationCase] = []
    identities: set[str] = set()
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"invalid evaluation JSON on line {line_number}"
            ) from exc
        if not isinstance(payload, dict):
            raise ValueError(
                f"evaluation line {line_number} must be a JSON object"
            )
        for name in ("case_id", "project_id", "query"):
            if not isinstance(payload.get(name), str):
                raise ValueError(
                    f"evaluation line {line_number} field {name} must be a string"
                )
        sequence_values: dict[str, tuple[str, ...]] = {}
        for name in (
            "expected_source_ids",
            "allowed_source_kinds",
            "forbidden_canonical_urls",
        ):
            value = payload.get(name, [])
            if not isinstance(value, list) or any(
                not isinstance(item, str) for item in value
            ):
                raise ValueError(
                    f"evaluation line {line_number} field {name} "
                    "must be a list of strings"
                )
            sequence_values[name] = tuple(value)
        expects_refusal = payload.get("expects_refusal", False)
        if not isinstance(expects_refusal, bool):
            raise ValueError(
                f"evaluation line {line_number} field expects_refusal "
                "must be a boolean"
            )
        annotation_status = payload.get("annotation_status", "pending")
        if not isinstance(annotation_status, str):
            raise ValueError(
                f"evaluation line {line_number} field annotation_status "
                "must be a string"
            )
        notes = payload.get("notes", "")
        if not isinstance(notes, str):
            raise ValueError(
                f"evaluation line {line_number} field notes must be a string"
            )
        case = RetrievalEvaluationCase(
            case_id=payload["case_id"],
            project_id=payload["project_id"],
            query=payload["query"],
            expected_source_ids=sequence_values["expected_source_ids"],
            allowed_source_kinds=sequence_values["allowed_source_kinds"],
            forbidden_canonical_urls=sequence_values[
                "forbidden_canonical_urls"
            ],
            expects_refusal=expects_refusal,
            annotation_status=annotation_status,  # type: ignore[arg-type]
            notes=notes,
        )
        if case.case_id in identities:
            raise ValueError(f"duplicate evaluation case_id: {case.case_id}")
        identities.add(case.case_id)
        if not approved_only or case.annotation_status == "approved":
            cases.append(case)
    return tuple(cases)


def evaluate_retriever(
    *,
    retriever_name: str,
    retriever: KnowledgeRetriever,
    cases: Sequence[RetrievalEvaluationCase],
    k: int = 5,
    minimum_score: float | None = None,
    clock: Callable[[], float] = perf_counter,
    metadata: Mapping[str, object] | None = None,
) -> RetrievalEvaluationReport:
    if not retriever_name.strip():
        raise ValueError("retriever_name is required")
    if isinstance(k, bool) or not isinstance(k, int) or k <= 0:
        raise ValueError("k must be positive")
    if minimum_score is not None and (
        isinstance(minimum_score, bool)
        or not isinstance(minimum_score, (int, float))
        or not isfinite(float(minimum_score))
    ):
        raise ValueError("minimum_score must be finite")
    approved = tuple(
        case for case in cases if case.annotation_status == "approved"
    )
    if not approved:
        raise ValueError("at least one approved evaluation case is required")
    observations: list[RetrievalEvaluationObservation] = []
    hits_by_case: dict[str, tuple[RetrievalHit, ...]] = {}
    for case in approved:
        started = clock()
        hits = tuple(
            retriever.retrieve(
                RetrievalQuery(
                    project_id=case.project_id,
                    text=case.query,
                    limit=k,
                )
            )
        )
        elapsed_ms = max(0.0, (clock() - started) * 1000)
        if minimum_score is not None:
            hits = tuple(hit for hit in hits if hit.score >= minimum_score)
        hits = hits[:k]
        hits_by_case[case.case_id] = hits
        observations.append(_observation(case, hits, elapsed_ms))
    return RetrievalEvaluationReport(
        retriever_name=retriever_name.strip(),
        k=k,
        metrics=_metrics(approved, hits_by_case, observations),
        observations=tuple(observations),
        metadata=dict(metadata or {}),
    )


def _observation(
    case: RetrievalEvaluationCase,
    hits: Sequence[RetrievalHit],
    latency_ms: float,
) -> RetrievalEvaluationObservation:
    return RetrievalEvaluationObservation(
        case_id=case.case_id,
        latency_ms=latency_ms,
        hit_chunk_ids=tuple(hit.chunk.chunk_id for hit in hits),
        hit_source_ids=tuple(hit.chunk.source_id for hit in hits),
        hit_source_kinds=tuple(
            hit.provenance.source_kind if hit.provenance is not None else None
            for hit in hits
        ),
        hit_canonical_urls=tuple(
            hit.provenance.canonical_url if hit.provenance is not None else None
            for hit in hits
        ),
        hit_scores=tuple(hit.score for hit in hits),
    )


def _metrics(
    cases: Sequence[RetrievalEvaluationCase],
    hits_by_case: Mapping[str, Sequence[RetrievalHit]],
    observations: Sequence[RetrievalEvaluationObservation],
) -> RetrievalEvaluationMetrics:
    answerable = tuple(case for case in cases if not case.expects_refusal)
    refusals = tuple(case for case in cases if case.expects_refusal)
    recalls: list[float] = []
    reciprocal_ranks: list[float] = []
    classification_scores: list[float] = []
    wrong_hits = 0
    total_hits = 0
    for case in answerable:
        hits = hits_by_case[case.case_id]
        returned_ids = [hit.chunk.source_id for hit in hits]
        expected = set(case.expected_source_ids)
        recalls.append(len(expected.intersection(returned_ids)) / len(expected))
        first_rank = next(
            (
                rank
                for rank, source_id in enumerate(returned_ids, start=1)
                if source_id in expected
            ),
            None,
        )
        reciprocal_ranks.append(0.0 if first_rank is None else 1.0 / first_rank)
        if case.allowed_source_kinds:
            first_kind = (
                hits[0].provenance.source_kind
                if hits and hits[0].provenance is not None
                else None
            )
            classification_scores.append(
                1.0 if first_kind in set(case.allowed_source_kinds) else 0.0
            )
        forbidden = set(case.forbidden_canonical_urls)
        allowed_kinds = set(case.allowed_source_kinds)
        for hit in hits:
            total_hits += 1
            provenance = hit.provenance
            if provenance is None:
                wrong_hits += 1
                continue
            if (
                provenance.canonical_url in forbidden
                or (
                    allowed_kinds
                    and provenance.source_kind not in allowed_kinds
                )
            ):
                wrong_hits += 1
    correct_refusals = sum(
        1 for case in refusals if not hits_by_case[case.case_id]
    )
    latencies = sorted(observation.latency_ms for observation in observations)
    return RetrievalEvaluationMetrics(
        case_count=len(cases),
        answerable_case_count=len(answerable),
        refusal_case_count=len(refusals),
        recall_at_k=_mean(recalls),
        mean_reciprocal_rank=_mean(reciprocal_ranks),
        first_hit_source_kind_accuracy=_mean(classification_scores),
        wrong_source_rate=(wrong_hits / total_hits if total_hits else 0.0),
        correct_refusal_rate=(
            correct_refusals / len(refusals) if refusals else 0.0
        ),
        latency_p50_ms=_percentile(latencies, 0.50),
        latency_p95_ms=_percentile(latencies, 0.95),
    )


def _mean(values: Iterable[float]) -> float:
    collected = tuple(values)
    return sum(collected) / len(collected) if collected else 0.0


def _percentile(values: Sequence[float], fraction: float) -> float:
    if not values:
        return 0.0
    index = max(0, ceil(len(values) * fraction) - 1)
    return float(values[index])


__all__ = [
    "EvidenceImprovementMetrics",
    "EvidenceImprovementObservation",
    "EvidenceImprovementReport",
    "RetrievalEvaluationCase",
    "RetrievalEvaluationMetrics",
    "RetrievalEvaluationObservation",
    "RetrievalEvaluationReport",
    "evaluate_evidence_improvement",
    "evaluate_retriever",
    "load_evaluation_cases",
]
