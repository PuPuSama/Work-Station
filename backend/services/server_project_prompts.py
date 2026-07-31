from __future__ import annotations

import uuid
from datetime import datetime, timezone
from hashlib import sha256
from typing import Literal, cast

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from models import PromptSnapshot
from server_schema import (
    project_prompt_defaults,
    project_prompt_heads,
    project_prompt_versions,
)
from services.access_control import (
    ActorIdentity,
    PostgresProjectAccessRepository,
    ProjectAccessDenied,
    ProjectAccessService,
    decide_project_permission,
)
from services.audit_log import (
    AuditEvent,
    AuditEventWriter,
    PostgresAuditEventWriter,
)


PromptKind = Literal["outline", "article", "review"]
PromptStatus = Literal["active", "archived"]
PromptSource = Literal["system", "project_default", "library"]


class ServerProjectPromptError(ValueError):
    """A prompt command violates the public project prompt contract."""


class ServerProjectPromptConflict(RuntimeError):
    """A prompt command used a stale version or conflicting state."""


class ServerProjectPromptUnavailable(RuntimeError):
    """The prompt command could not commit without exposing internals."""


def _system_snapshot(kind: PromptKind) -> PromptSnapshot:
    return PromptSnapshot(
        kind=kind,
        source="system",
        captured_at=datetime.now(timezone.utc).isoformat(),
    )


def _validate_kind(kind: str) -> PromptKind:
    if kind not in {"outline", "article", "review"}:
        raise ServerProjectPromptError("prompt kind is unsupported")
    return cast(PromptKind, kind)


def _required_text(
    value: str,
    field_name: str,
    *,
    max_length: int,
) -> str:
    normalized = (
        value.replace("\r\n", "\n").replace("\r", "\n").strip()
    )
    if not normalized:
        raise ServerProjectPromptError(f"{field_name} is required")
    if len(normalized) > max_length:
        raise ServerProjectPromptError(f"{field_name} is too long")
    return normalized


class PostgresProjectPromptService:
    """Project-scoped immutable prompt versions and explicit default pointers."""

    def __init__(
        self,
        engine: Engine,
        *,
        organization_id: str,
        project_id: str,
        audit: AuditEventWriter | None = None,
    ) -> None:
        self._engine = engine
        self._access_repository = PostgresProjectAccessRepository(engine)
        self._access = ProjectAccessService(self._access_repository)
        self._audit = audit or PostgresAuditEventWriter()
        self.organization_id = _required_text(
            organization_id,
            "organization_id",
            max_length=255,
        )
        self.project_id = _required_text(
            project_id,
            "project_id",
            max_length=255,
        )

    def _require_read(self, actor: ActorIdentity) -> None:
        if actor.organization_id != self.organization_id:
            raise ProjectAccessDenied("project access denied")
        self._access.require(actor, self.project_id, "project.view")

    def _lock_write_access(
        self,
        connection: Connection,
        actor: ActorIdentity,
    ) -> None:
        if actor.organization_id != self.organization_id:
            raise ProjectAccessDenied("project access denied")
        facts = self._access_repository.lock_project_access_in_connection(
            connection,
            actor,
            self.project_id,
        )
        if not decide_project_permission(
            facts,
            "article.edit",
        ).allowed:
            raise ProjectAccessDenied("project access denied")

    def _append_audit(
        self,
        connection: Connection,
        *,
        actor: ActorIdentity,
        prompt_id: str,
        action: str,
        details: dict[str, object],
    ) -> None:
        self._audit.append(
            connection,
            AuditEvent(
                organization_id=self.organization_id,
                event_id=f"prompt_{uuid.uuid4().hex}",
                actor_user_id=actor.user_id,
                project_id=self.project_id,
                action=action,
                target_type="project_prompt",
                target_id=prompt_id,
                details=details,
            ),
        )

    @staticmethod
    def _snapshot(
        row: sa.RowMapping,
        *,
        source: PromptSource,
    ) -> PromptSnapshot:
        return PromptSnapshot(
            prompt_id=str(row["prompt_id"]),
            name=str(row["name"]),
            kind=str(row["kind"]),
            content=str(row["content"]),
            version=int(row["version"]),
            source=source,
            captured_at=datetime.now(timezone.utc).isoformat(),
        )

    def _current_row(
        self,
        connection: Connection,
        prompt_id: str,
        *,
        lock: bool = False,
    ) -> sa.RowMapping | None:
        query = (
            sa.select(
                project_prompt_heads.c.prompt_id,
                project_prompt_heads.c.kind,
                project_prompt_heads.c.status,
                project_prompt_heads.c.current_version,
                project_prompt_versions.c.version,
                project_prompt_versions.c.name,
                project_prompt_versions.c.content,
                project_prompt_versions.c.content_hash,
                project_prompt_versions.c.created_at,
            )
            .select_from(
                project_prompt_heads.join(
                    project_prompt_versions,
                    sa.and_(
                        project_prompt_versions.c.organization_id
                        == project_prompt_heads.c.organization_id,
                        project_prompt_versions.c.project_id
                        == project_prompt_heads.c.project_id,
                        project_prompt_versions.c.prompt_id
                        == project_prompt_heads.c.prompt_id,
                        project_prompt_versions.c.kind
                        == project_prompt_heads.c.kind,
                        project_prompt_versions.c.version
                        == project_prompt_heads.c.current_version,
                    ),
                )
            )
            .where(
                project_prompt_heads.c.organization_id
                == self.organization_id,
                project_prompt_heads.c.project_id == self.project_id,
                project_prompt_heads.c.prompt_id == prompt_id,
            )
        )
        if lock:
            query = query.with_for_update(of=project_prompt_heads)
        return connection.execute(query).mappings().one_or_none()

    def create(
        self,
        actor: ActorIdentity,
        *,
        name: str,
        kind: PromptKind,
        content: str,
    ) -> PromptSnapshot:
        kind = _validate_kind(kind)
        cleaned_name = _required_text(name, "name", max_length=120)
        cleaned_content = _required_text(
            content,
            "content",
            max_length=40000,
        )
        prompt_id = uuid.uuid4().hex
        try:
            with self._engine.begin() as connection:
                self._lock_write_access(connection, actor)
                connection.execute(
                    project_prompt_heads.insert().values(
                        organization_id=self.organization_id,
                        project_id=self.project_id,
                        prompt_id=prompt_id,
                        kind=kind,
                        current_version=1,
                    )
                )
                connection.execute(
                    project_prompt_versions.insert().values(
                        organization_id=self.organization_id,
                        project_id=self.project_id,
                        prompt_id=prompt_id,
                        kind=kind,
                        version=1,
                        name=cleaned_name,
                        content=cleaned_content,
                        content_hash=sha256(
                            cleaned_content.encode("utf-8")
                        ).hexdigest(),
                        created_by_user_id=actor.user_id,
                    )
                )
                self._append_audit(
                    connection,
                    actor=actor,
                    prompt_id=prompt_id,
                    action="project_prompt.created",
                    details={
                        "content_characters": len(cleaned_content),
                        "kind": kind,
                        "version": 1,
                    },
                )
                row = self._current_row(connection, prompt_id)
                assert row is not None
                return self._snapshot(row, source="library")
        except (ProjectAccessDenied, ServerProjectPromptError):
            raise
        except (SQLAlchemyError, RuntimeError) as exc:
            raise ServerProjectPromptUnavailable(
                "project prompt command is temporarily unavailable"
            ) from exc

    def update(
        self,
        actor: ActorIdentity,
        *,
        prompt_id: str,
        expected_version: int,
        name: str,
        content: str,
    ) -> PromptSnapshot:
        normalized_id = _required_text(
            prompt_id,
            "prompt_id",
            max_length=255,
        )
        cleaned_name = _required_text(name, "name", max_length=120)
        cleaned_content = _required_text(
            content,
            "content",
            max_length=40000,
        )
        if expected_version <= 0:
            raise ServerProjectPromptError(
                "expected_version must be positive"
            )
        try:
            with self._engine.begin() as connection:
                self._lock_write_access(connection, actor)
                current = self._current_row(
                    connection,
                    normalized_id,
                    lock=True,
                )
                if current is None:
                    raise KeyError(normalized_id)
                if current["status"] != "active":
                    raise ServerProjectPromptConflict(
                        "project prompt is not active"
                    )
                if int(current["current_version"]) != expected_version:
                    raise ServerProjectPromptConflict(
                        "project prompt version conflict"
                    )
                next_version = expected_version + 1
                connection.execute(
                    project_prompt_versions.insert().values(
                        organization_id=self.organization_id,
                        project_id=self.project_id,
                        prompt_id=normalized_id,
                        kind=str(current["kind"]),
                        version=next_version,
                        name=cleaned_name,
                        content=cleaned_content,
                        content_hash=sha256(
                            cleaned_content.encode("utf-8")
                        ).hexdigest(),
                        created_by_user_id=actor.user_id,
                    )
                )
                connection.execute(
                    project_prompt_heads.update()
                    .where(
                        project_prompt_heads.c.organization_id
                        == self.organization_id,
                        project_prompt_heads.c.project_id
                        == self.project_id,
                        project_prompt_heads.c.prompt_id == normalized_id,
                    )
                    .values(
                        current_version=next_version,
                        updated_at=sa.func.now(),
                    )
                )
                self._append_audit(
                    connection,
                    actor=actor,
                    prompt_id=normalized_id,
                    action="project_prompt.version.created",
                    details={
                        "content_characters": len(cleaned_content),
                        "from_version": expected_version,
                        "kind": str(current["kind"]),
                        "to_version": next_version,
                    },
                )
                row = self._current_row(connection, normalized_id)
                assert row is not None
                return self._snapshot(row, source="library")
        except (
            KeyError,
            ProjectAccessDenied,
            ServerProjectPromptConflict,
            ServerProjectPromptError,
        ):
            raise
        except (SQLAlchemyError, RuntimeError) as exc:
            raise ServerProjectPromptUnavailable(
                "project prompt command is temporarily unavailable"
            ) from exc

    def set_active(
        self,
        actor: ActorIdentity,
        *,
        prompt_id: str,
        active: bool,
    ) -> PromptStatus:
        normalized_id = _required_text(
            prompt_id,
            "prompt_id",
            max_length=255,
        )
        status: PromptStatus = "active" if active else "archived"
        try:
            with self._engine.begin() as connection:
                self._lock_write_access(connection, actor)
                current = self._current_row(
                    connection,
                    normalized_id,
                    lock=True,
                )
                if current is None:
                    raise KeyError(normalized_id)
                if current["status"] == status:
                    return status
                connection.execute(
                    project_prompt_heads.update()
                    .where(
                        project_prompt_heads.c.organization_id
                        == self.organization_id,
                        project_prompt_heads.c.project_id
                        == self.project_id,
                        project_prompt_heads.c.prompt_id == normalized_id,
                    )
                    .values(status=status, updated_at=sa.func.now())
                )
                if not active:
                    connection.execute(
                        project_prompt_defaults.delete().where(
                            project_prompt_defaults.c.organization_id
                            == self.organization_id,
                            project_prompt_defaults.c.project_id
                            == self.project_id,
                            project_prompt_defaults.c.prompt_id
                            == normalized_id,
                        )
                    )
                self._append_audit(
                    connection,
                    actor=actor,
                    prompt_id=normalized_id,
                    action="project_prompt.status.updated",
                    details={
                        "active": active,
                        "kind": str(current["kind"]),
                        "version": int(current["current_version"]),
                    },
                )
                return status
        except (KeyError, ProjectAccessDenied):
            raise
        except (SQLAlchemyError, RuntimeError) as exc:
            raise ServerProjectPromptUnavailable(
                "project prompt command is temporarily unavailable"
            ) from exc

    def set_default(
        self,
        actor: ActorIdentity,
        *,
        kind: PromptKind,
        prompt_id: str | None,
    ) -> PromptSnapshot:
        kind = _validate_kind(kind)
        normalized_id = (prompt_id or "").strip()
        try:
            with self._engine.begin() as connection:
                self._lock_write_access(connection, actor)
                current = None
                if normalized_id:
                    current = self._current_row(
                        connection,
                        normalized_id,
                        lock=True,
                    )
                    if (
                        current is None
                        or current["status"] != "active"
                        or current["kind"] != kind
                    ):
                        raise ServerProjectPromptError(
                            "default prompt is unavailable"
                        )
                existing_default = connection.execute(
                    sa.select(
                        project_prompt_defaults.c.prompt_id,
                        project_prompt_defaults.c.version,
                    )
                    .where(
                        project_prompt_defaults.c.organization_id
                        == self.organization_id,
                        project_prompt_defaults.c.project_id
                        == self.project_id,
                        project_prompt_defaults.c.kind == kind,
                    )
                    .with_for_update()
                ).one_or_none()
                if not normalized_id:
                    if existing_default is None:
                        return _system_snapshot(kind)
                    connection.execute(
                        project_prompt_defaults.delete().where(
                            project_prompt_defaults.c.organization_id
                            == self.organization_id,
                            project_prompt_defaults.c.project_id
                            == self.project_id,
                            project_prompt_defaults.c.kind == kind,
                        )
                    )
                    self._append_audit(
                        connection,
                        actor=actor,
                        prompt_id=f"default:{kind}",
                        action="project_prompt.default.updated",
                        details={"cleared": True, "kind": kind},
                    )
                    return _system_snapshot(kind)
                assert current is not None
                version = int(current["current_version"])
                if (
                    existing_default is not None
                    and existing_default.prompt_id == normalized_id
                    and int(existing_default.version) == version
                ):
                    return self._snapshot(
                        current,
                        source="project_default",
                    )
                connection.execute(
                    postgresql_insert(project_prompt_defaults)
                    .values(
                        organization_id=self.organization_id,
                        project_id=self.project_id,
                        kind=kind,
                        prompt_id=normalized_id,
                        version=version,
                    )
                    .on_conflict_do_update(
                        index_elements=[
                            project_prompt_defaults.c.organization_id,
                            project_prompt_defaults.c.project_id,
                            project_prompt_defaults.c.kind,
                        ],
                        set_={
                            "prompt_id": normalized_id,
                            "version": version,
                            "updated_at": sa.func.now(),
                        },
                    )
                )
                self._append_audit(
                    connection,
                    actor=actor,
                    prompt_id=normalized_id,
                    action="project_prompt.default.updated",
                    details={
                        "cleared": False,
                        "kind": kind,
                        "version": version,
                    },
                )
                return self._snapshot(
                    current,
                    source="project_default",
                )
        except (ProjectAccessDenied, ServerProjectPromptError):
            raise
        except (SQLAlchemyError, RuntimeError) as exc:
            raise ServerProjectPromptUnavailable(
                "project prompt command is temporarily unavailable"
            ) from exc

    def resolve(
        self,
        actor: ActorIdentity,
        *,
        kind: PromptKind,
        selection: str,
    ) -> PromptSnapshot:
        kind = _validate_kind(kind)
        self._require_read(actor)
        normalized = selection.strip()
        if normalized == "system":
            return _system_snapshot(kind)
        with self._engine.connect() as connection:
            if not normalized or normalized == "project_default":
                row = connection.execute(
                    sa.select(
                        project_prompt_defaults.c.prompt_id,
                        project_prompt_defaults.c.version,
                        project_prompt_heads.c.kind,
                        project_prompt_heads.c.status,
                        project_prompt_versions.c.name,
                        project_prompt_versions.c.content,
                        project_prompt_versions.c.created_at,
                    )
                    .select_from(
                        project_prompt_defaults.join(
                            project_prompt_heads,
                            sa.and_(
                                project_prompt_heads.c.organization_id
                                == project_prompt_defaults.c.organization_id,
                                project_prompt_heads.c.project_id
                                == project_prompt_defaults.c.project_id,
                                project_prompt_heads.c.prompt_id
                                == project_prompt_defaults.c.prompt_id,
                            ),
                        ).join(
                            project_prompt_versions,
                            sa.and_(
                                project_prompt_versions.c.organization_id
                                == project_prompt_defaults.c.organization_id,
                                project_prompt_versions.c.project_id
                                == project_prompt_defaults.c.project_id,
                                project_prompt_versions.c.prompt_id
                                == project_prompt_defaults.c.prompt_id,
                                project_prompt_versions.c.kind
                                == project_prompt_defaults.c.kind,
                                project_prompt_versions.c.version
                                == project_prompt_defaults.c.version,
                            ),
                        )
                    )
                    .where(
                        project_prompt_defaults.c.organization_id
                        == self.organization_id,
                        project_prompt_defaults.c.project_id
                        == self.project_id,
                        project_prompt_defaults.c.kind == kind,
                        project_prompt_heads.c.status == "active",
                    )
                ).mappings().one_or_none()
                if row is None:
                    return _system_snapshot(kind)
                return self._snapshot(
                    row,
                    source="project_default",
                )
            row = self._current_row(connection, normalized)
            if (
                row is None
                or row["status"] != "active"
                or row["kind"] != kind
            ):
                raise ServerProjectPromptError(
                    "selected prompt is unavailable"
                )
            return self._snapshot(row, source="library")
__all__ = [
    "PostgresProjectPromptService",
    "PromptKind",
    "PromptStatus",
    "ServerProjectPromptConflict",
    "ServerProjectPromptError",
    "ServerProjectPromptUnavailable",
]
