from __future__ import annotations

import json
from collections.abc import Mapping


def encode_sse(
    *,
    event: str,
    data: Mapping[str, object] | str,
    event_id: int | None = None,
) -> str:
    """Encode one browser EventSource frame with compact deterministic JSON."""

    payload = (
        data
        if isinstance(data, str)
        else json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    )
    lines: list[str] = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    lines.extend(f"data: {line}" for line in payload.splitlines() or ("",))
    return "\n".join(lines) + "\n\n"


def resolve_after_sequence(
    query_value: int | None,
    last_event_id: str | None,
) -> int:
    """Prefer an explicit cursor, otherwise accept EventSource Last-Event-ID."""

    if query_value is not None:
        if query_value < 0:
            raise ValueError("after_sequence must be non-negative")
        return query_value
    raw = (last_event_id or "").strip()
    if not raw:
        return 0
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError("Last-Event-ID must be a non-negative integer") from exc
    if value < 0:
        raise ValueError("Last-Event-ID must be a non-negative integer")
    return value
