from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .runtime import KnowledgeAgentRuntime


RESEARCH_DETAIL_RETENTION_DAYS = 30


def prune_expired_research_details(
    runtime: KnowledgeAgentRuntime,
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    """Apply the M5 30-day detail policy without deleting run summaries."""

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    before = current - timedelta(days=RESEARCH_DETAIL_RETENTION_DAYS)
    prune_runs = getattr(
        runtime.research_run_repository,
        "prune_expired_details",
        None,
    )
    counts = (
        prune_runs(before=before)
        if callable(prune_runs)
        else {
            "research_graph_events": 0,
            "gap_fill_attempts": 0,
        }
    )
    chat_repository = getattr(runtime, "research_chat_repository", None)
    prune_chats = getattr(chat_repository, "prune_expired", None)
    counts["research_conversations"] = (
        int(prune_chats(before=current)) if callable(prune_chats) else 0
    )
    return counts
