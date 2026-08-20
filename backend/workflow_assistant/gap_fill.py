"""Typed Workflow Assistant boundary for precise research gap filling.

The knowledge-agent research graph owns retrieval plans, checkpoints, source
publication, and evidence-pack creation.  This module deliberately owns only
the assistant-facing contract: a bounded, safe review projection and the
explicit approval payload used to release the waiting assistant step.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from knowledge_agent.research_runs import (
    ResearchRunConflictError,
    ResearchRunNotFound,
)
from services.access_control import ActorIdentity

from .contracts import WorkflowPlanResponse


MAX_GAP_FILL_CANDIDATES = 20


class GapFillRequest(BaseModel):
    """Explicit candidate approval for one paused research step.

    An empty candidate list is valid and means that the user rejected all
    currently proposed sources.  The graph may then continue to its bounded
    warning path without treating an unreviewed candidate as evidence.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    revision: int = Field(ge=0)
    step_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    research_thread_id: str = Field(min_length=1, max_length=200)
    request_id: str = Field(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    approved_candidate_ids: list[str] = Field(
        default_factory=list,
        max_length=MAX_GAP_FILL_CANDIDATES,
    )

    @field_validator("approved_candidate_ids")
    @classmethod
    def validate_candidate_ids(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value or len(value) > 200 for value in normalized):
            raise ValueError("approved_candidate_ids are invalid")
        if len(normalized) != len(set(normalized)):
            raise ValueError("approved_candidate_ids must be unique")
        return normalized


class GapFillCandidateEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = None
    channel: Literal["official_site", "tavily_discovery"] | None = None
    same_site: bool | None = None
    score: float | None = None
    reused_attempt: bool | None = None


class GapFillCandidateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    url: str
    page_type: str
    needs_review: bool
    evidence: GapFillCandidateEvidence


class GapFillSnapshotResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    research_thread_id: str
    retrieval_plan_id: str
    status: str
    current_scope_id: str | None
    gap_reasons: list[str]
    gap_fill_round: int
    max_gap_fill_rounds: int
    discovery_queries_used: int
    max_discovery_queries: int
    evidence_pack_ids: list[str]
    review_candidates: list[GapFillCandidateResponse]


class GapFillResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan: WorkflowPlanResponse
    step_id: str
    research_thread_id: str
    queue_job_id: str
    queue_job_status: str
    snapshot: GapFillSnapshotResponse


class GapFillError(RuntimeError):
    """Base error for the assistant gap-fill boundary."""


class GapFillNotFound(GapFillError):
    """The requested research run or checkpoint is not visible."""


class GapFillConflict(GapFillError):
    """The run is not at a candidate-review boundary or has changed."""


class GapFillUnavailable(GapFillError):
    """The existing research service is not configured or is unavailable."""


def _safe_candidate(raw: object) -> dict[str, Any] | None:
    """Keep only same-site, reviewable candidates from the graph checkpoint."""

    if not isinstance(raw, Mapping) or raw.get("needs_review") is not True:
        return None
    candidate_id = str(raw.get("candidate_id") or "").strip()
    url = str(raw.get("url") or "").strip()
    page_type = str(raw.get("page_type") or "unknown").strip() or "unknown"
    if not candidate_id or len(candidate_id) > 200 or not url or len(url) > 4096:
        return None
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
        return None
    evidence_raw = raw.get("evidence")
    evidence = dict(evidence_raw) if isinstance(evidence_raw, Mapping) else {}
    # The discovery adapter records these two fields for every candidate. A
    # missing/false marker fails closed so a blog or third-party URL cannot be
    # presented as an assistant evidence candidate.
    if evidence.get("same_site") is not True:
        return None
    channel = str(evidence.get("channel") or "").strip()
    if channel not in {"official_site", "tavily_discovery"}:
        return None
    safe_evidence: dict[str, Any] = {
        "reason": (
            str(evidence["reason"])[:500]
            if isinstance(evidence.get("reason"), str)
            else None
        ),
        "channel": channel,
        "same_site": True,
        "score": (
            float(evidence["score"])
            if isinstance(evidence.get("score"), int | float)
            and not isinstance(evidence.get("score"), bool)
            else None
        ),
        "reused_attempt": (
            bool(evidence["reused_attempt"])
            if isinstance(evidence.get("reused_attempt"), bool)
            else None
        ),
    }
    return {
        "candidate_id": candidate_id,
        "url": url,
        "page_type": page_type[:200],
        "needs_review": True,
        "evidence": safe_evidence,
    }


class WorkflowAssistantGapFillService:
    """Adapt the existing Server research registry to assistant contracts."""

    def __init__(self, research_registry: Any) -> None:
        self._research = research_registry

    def snapshot(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        thread_id: str,
    ) -> GapFillSnapshotResponse:
        method = getattr(self._research, "gap_fill_snapshot", None)
        if not callable(method):
            raise GapFillUnavailable("knowledge research gap-fill is unavailable")
        try:
            raw = method(
                actor=actor,
                project_id=project_id,
                thread_id=thread_id,
            )
        except ResearchRunNotFound as exc:
            raise GapFillNotFound("research run was not found") from exc
        except ResearchRunConflictError as exc:
            raise GapFillConflict(str(exc)) from exc
        except GapFillError:
            raise
        except Exception as exc:
            raise GapFillUnavailable("research gap-fill snapshot is unavailable") from exc
        if not isinstance(raw, Mapping):
            raise GapFillUnavailable("research gap-fill snapshot is invalid")
        raw_candidates = raw.get("review_candidates") or ()
        candidates = tuple(
            candidate
            for candidate in (_safe_candidate(item) for item in raw_candidates)
            if candidate is not None
        )
        return GapFillSnapshotResponse(
            project_id=str(raw.get("project_id") or project_id),
            research_thread_id=str(raw.get("research_thread_id") or thread_id),
            retrieval_plan_id=str(raw.get("retrieval_plan_id") or ""),
            status=str(raw.get("status") or ""),
            current_scope_id=(
                str(raw["current_scope_id"])
                if raw.get("current_scope_id")
                else None
            ),
            gap_reasons=[
                str(item)[:500]
                for item in (raw.get("gap_reasons") or ())
                if str(item).strip()
            ][:20],
            gap_fill_round=max(0, int(raw.get("gap_fill_round") or 0)),
            max_gap_fill_rounds=max(0, int(raw.get("max_gap_fill_rounds") or 0)),
            discovery_queries_used=max(
                0,
                int(raw.get("discovery_queries_used") or 0),
            ),
            max_discovery_queries=max(
                0,
                int(raw.get("max_discovery_queries") or 0),
            ),
            evidence_pack_ids=[
                str(item) for item in (raw.get("evidence_pack_ids") or ()) if str(item).strip()
            ][:50],
            review_candidates=list(candidates)[:MAX_GAP_FILL_CANDIDATES],
        )

    def enqueue_resume(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        thread_id: str,
        request_id: str,
        approved_candidate_ids: Sequence[str],
    ) -> Mapping[str, Any]:
        method = getattr(self._research, "enqueue_resume", None)
        if not callable(method):
            raise GapFillUnavailable("knowledge research resume is unavailable")
        try:
            result = method(
                actor=actor,
                project_id=project_id,
                thread_id=thread_id,
                request_id=request_id,
                approved_candidate_ids=tuple(approved_candidate_ids),
            )
        except ResearchRunNotFound as exc:
            raise GapFillNotFound("research run was not found") from exc
        except ResearchRunConflictError as exc:
            raise GapFillConflict(str(exc)) from exc
        except GapFillError:
            raise
        except ValueError as exc:
            raise GapFillConflict(str(exc)) from exc
        except Exception as exc:
            raise GapFillUnavailable("research resume could not be queued") from exc
        if not isinstance(result, Mapping):
            raise GapFillUnavailable("research resume response is invalid")
        job = result.get("job") if isinstance(result.get("job"), Mapping) else result
        job_id = str(job.get("job_id", job.get("id", "")) if isinstance(job, Mapping) else "").strip()
        if not job_id:
            raise GapFillUnavailable("research resume Job identity is unavailable")
        return {
            "job_id": job_id,
            "status": str(job.get("status") or "queued") if isinstance(job, Mapping) else "queued",
        }


__all__ = [
    "GapFillCandidateEvidence",
    "GapFillCandidateResponse",
    "GapFillConflict",
    "GapFillError",
    "GapFillNotFound",
    "GapFillRequest",
    "GapFillResponse",
    "GapFillSnapshotResponse",
    "GapFillUnavailable",
    "MAX_GAP_FILL_CANDIDATES",
    "WorkflowAssistantGapFillService",
]
