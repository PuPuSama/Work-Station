from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Protocol

from sqlalchemy.engine import Connection

from server_schema import audit_events


def _required_text(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized


def _optional_text(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field_name)


@dataclass(frozen=True)
class AuditEvent:
    """Append-only audit record with caller-supplied stable identity."""

    organization_id: str
    event_id: str
    action: str
    target_type: str
    target_id: str
    actor_user_id: str | None = None
    project_id: str | None = None
    details: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in (
            "organization_id",
            "event_id",
            "action",
            "target_type",
            "target_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _required_text(str(getattr(self, field_name)), field_name),
            )
        object.__setattr__(
            self,
            "actor_user_id",
            _optional_text(self.actor_user_id, "actor_user_id"),
        )
        object.__setattr__(
            self,
            "project_id",
            _optional_text(self.project_id, "project_id"),
        )
        object.__setattr__(self, "details", dict(self.details))


class AuditEventWriter(Protocol):
    def append(self, connection: Connection, event: AuditEvent) -> None:
        """Append inside the caller's business transaction."""


class PostgresAuditEventWriter:
    """SQLAlchemy Core writer; PostgreSQL enforces immutable audit rows."""

    def append(self, connection: Connection, event: AuditEvent) -> None:
        if not connection.in_transaction():
            raise ValueError(
                "audit events must be appended inside a business transaction"
            )
        connection.execute(
            audit_events.insert().values(
                organization_id=event.organization_id,
                event_id=event.event_id,
                actor_user_id=event.actor_user_id,
                project_id=event.project_id,
                action=event.action,
                target_type=event.target_type,
                target_id=event.target_id,
                details=dict(event.details),
            )
        )


__all__ = [
    "AuditEvent",
    "AuditEventWriter",
    "PostgresAuditEventWriter",
]
