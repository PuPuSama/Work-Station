from __future__ import annotations

from datetime import datetime, timezone

from .repository import PostgresWorkflowAssistantRepository


def prune_expired_assistant_conversations(
    repository: PostgresWorkflowAssistantRepository,
    *,
    before: datetime | None = None,
) -> int:
    """Remove private conversation details without touching plans or Tasks."""

    cutoff = before or datetime.now(timezone.utc)
    return repository.prune_expired(before=cutoff)


__all__ = ["prune_expired_assistant_conversations"]
