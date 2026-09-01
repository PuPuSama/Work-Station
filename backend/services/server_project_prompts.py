from __future__ import annotations

import uuid
from dataclasses import dataclass
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
from services.server_request_security import AuthorizedProjectRequest
from services.project_time import project_now_iso


PromptKind = Literal["outline", "article", "review", "humanize"]
PromptStatus = Literal["active", "archived"]
PromptSource = Literal["system", "project_default", "library"]


def _prompt_scope_lock_identity(
    organization_id: str,
    project_id: str,
) -> str:
    """Stable namespace for serializing one Project's Prompt pointer graph."""

    return "\n".join(("server_project_prompts_v1", organization_id, project_id))


@dataclass(frozen=True, slots=True)
class ServerProjectPromptItem:
    snapshot: PromptSnapshot
    status: PromptStatus


@dataclass(frozen=True, slots=True)
class ServerProjectPromptDirectory:
    prompts: tuple[ServerProjectPromptItem, ...]
    defaults: dict[PromptKind, PromptSnapshot]


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
        captured_at=project_now_iso(),
    )


def _validate_kind(kind: str) -> PromptKind:
    if kind not in {"outline", "article", "review", "humanize"}:
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


def _validate_content_contract(
    kind: PromptKind,
    content: str,
) -> None:
    if kind == "humanize" and content.count("{{ARTICLE}}") != 1:
        raise ServerProjectPromptError(
            "humanize prompt must contain exactly one {{ARTICLE}} placeholder"
        )


class PostgresProjectPromptService:
    """Project-scoped prompts with explicit default pointers."""

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
        # The Default row may not exist yet, so a row lock cannot protect the
        # absence case. Every Prompt mutation and writing-settings validation
        # takes this transaction-scoped lock before reading a Default pointer.
        connection.execute(
            sa.select(
                sa.func.pg_advisory_xact_lock(
                    sa.func.hashtextextended(
                        _prompt_scope_lock_identity(
                            self.organization_id,
                            self.project_id,
                        ),
                        0,
                    )
                )
            )
        ).scalar_one()

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
            captured_at=project_now_iso(),
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

    def _resolve_in_connection(
        self,
        connection: Connection,
        *,
        kind: PromptKind,
        selection: str,
        lock: bool,
    ) -> PromptSnapshot:
        normalized = selection.strip()
        if normalized == "system":
            return _system_snapshot(kind)
        if not normalized or normalized == "project_default":
            query = (
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
                    project_prompt_defaults.c.project_id == self.project_id,
                    project_prompt_defaults.c.kind == kind,
                    project_prompt_heads.c.status == "active",
                )
            )
            if lock:
                query = query.with_for_update(of=project_prompt_heads)
            row = connection.execute(query).mappings().one_or_none()
            if row is None:
                return _system_snapshot(kind)
            return self._snapshot(row, source="project_default")
        row = self._current_row(connection, normalized, lock=lock)
        if (
            row is None
            or row["status"] != "active"
            or row["kind"] != kind
        ):
            raise ServerProjectPromptError(
                "selected prompt is unavailable"
            )
        return self._snapshot(row, source="library")

    def resolve_for_update_in_transaction(
        self,
        connection: Connection,
        actor: ActorIdentity,
        *,
        kind: PromptKind,
        selection: str,
    ) -> PromptSnapshot:
        """Resolve and lock one selection in the caller's write transaction."""

        if not connection.in_transaction():
            raise ValueError(
                "project prompt resolution requires a business transaction"
            )
        validated_kind = _validate_kind(kind)
        self._lock_write_access(connection, actor)
        return self._resolve_in_connection(
            connection,
            kind=validated_kind,
            selection=selection,
            lock=True,
        )

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
        _validate_content_contract(kind, cleaned_content)
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

    def create_imported(
        self,
        actor: ActorIdentity,
        *,
        prompt_id: str,
        name: str,
        kind: PromptKind,
        content: str,
    ) -> PromptSnapshot:
        """Create one import-owned Prompt with a retry-stable identity.

        The regular ``create`` command intentionally allocates a random ID
        for interactive use. Imports need a deterministic ID so a worker
        crash after the business write can safely replay the same Proposal.
        """

        normalized_id = _required_text(prompt_id, "prompt_id", max_length=255)
        validated_kind = _validate_kind(kind)
        cleaned_name = _required_text(name, "name", max_length=120)
        cleaned_content = _required_text(content, "content", max_length=40000)
        _validate_content_contract(validated_kind, cleaned_content)
        digest = sha256(cleaned_content.encode("utf-8")).hexdigest()
        try:
            with self._engine.begin() as connection:
                self._lock_write_access(connection, actor)
                current = self._current_row(connection, normalized_id, lock=True)
                if current is not None:
                    if (
                        str(current["kind"]) != validated_kind
                        or str(current["name"]) != cleaned_name
                        or str(current["content_hash"]) != digest
                        or str(current["content"]) != cleaned_content
                    ):
                        raise ServerProjectPromptConflict(
                            "imported prompt identity has different content"
                        )
                    return self._snapshot(current, source="library")
                connection.execute(
                    project_prompt_heads.insert().values(
                        organization_id=self.organization_id,
                        project_id=self.project_id,
                        prompt_id=normalized_id,
                        kind=validated_kind,
                        current_version=1,
                    )
                )
                connection.execute(
                    project_prompt_versions.insert().values(
                        organization_id=self.organization_id,
                        project_id=self.project_id,
                        prompt_id=normalized_id,
                        kind=validated_kind,
                        version=1,
                        name=cleaned_name,
                        content=cleaned_content,
                        content_hash=digest,
                        created_by_user_id=actor.user_id,
                    )
                )
                self._append_audit(
                    connection,
                    actor=actor,
                    prompt_id=normalized_id,
                    action="project_prompt.imported",
                    details={
                        "content_characters": len(cleaned_content),
                        "kind": validated_kind,
                        "version": 1,
                    },
                )
                row = self._current_row(connection, normalized_id)
                assert row is not None
                return self._snapshot(row, source="library")
        except (
            ProjectAccessDenied,
            ServerProjectPromptConflict,
            ServerProjectPromptError,
            ValueError,
        ):
            raise
        except (SQLAlchemyError, RuntimeError) as exc:
            raise ServerProjectPromptUnavailable(
                "project prompt import is temporarily unavailable"
            ) from exc

    def update(
        self,
        actor: ActorIdentity,
        *,
        prompt_id: str,
        expected_version: int,
        name: str,
        kind: PromptKind | None = None,
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
        requested_kind = _validate_kind(kind) if kind is not None else None
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
                current_kind = _validate_kind(str(current["kind"]))
                target_kind = requested_kind or current_kind
                _validate_content_contract(target_kind, cleaned_content)
                if target_kind != current_kind:
                    replacement_id = uuid.uuid4().hex
                    connection.execute(
                        project_prompt_heads.insert().values(
                            organization_id=self.organization_id,
                            project_id=self.project_id,
                            prompt_id=replacement_id,
                            kind=target_kind,
                            current_version=1,
                        )
                    )
                    connection.execute(
                        project_prompt_versions.insert().values(
                            organization_id=self.organization_id,
                            project_id=self.project_id,
                            prompt_id=replacement_id,
                            kind=target_kind,
                            version=1,
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
                        .values(status="archived", updated_at=sa.func.now())
                    )
                    connection.execute(
                        project_prompt_defaults.delete().where(
                            project_prompt_defaults.c.organization_id
                            == self.organization_id,
                            project_prompt_defaults.c.project_id
                            == self.project_id,
                            project_prompt_defaults.c.prompt_id == normalized_id,
                        )
                    )
                    self._append_audit(
                        connection,
                        actor=actor,
                        prompt_id=replacement_id,
                        action="project_prompt.reclassified",
                        details={
                            "from_kind": current_kind,
                            "from_prompt_id": normalized_id,
                            "to_kind": target_kind,
                        },
                    )
                    replacement = self._current_row(
                        connection,
                        replacement_id,
                    )
                    assert replacement is not None
                    return self._snapshot(replacement, source="library")
                current_version = int(current["current_version"])
                connection.execute(
                    project_prompt_versions.update()
                    .where(
                        project_prompt_versions.c.organization_id
                        == self.organization_id,
                        project_prompt_versions.c.project_id
                        == self.project_id,
                        project_prompt_versions.c.prompt_id == normalized_id,
                        project_prompt_versions.c.kind == str(current["kind"]),
                        project_prompt_versions.c.version == current_version,
                    )
                    .values(
                        name=cleaned_name,
                        content=cleaned_content,
                        content_hash=sha256(
                            cleaned_content.encode("utf-8")
                        ).hexdigest(),
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
                    .values(updated_at=sa.func.now())
                )
                self._append_audit(
                    connection,
                    actor=actor,
                    prompt_id=normalized_id,
                    action="project_prompt.updated",
                    details={
                        "content_characters": len(cleaned_content),
                        "version": current_version,
                        "kind": str(current["kind"]),
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

    def delete(
        self,
        actor: ActorIdentity,
        *,
        prompt_id: str,
    ) -> None:
        normalized_id = _required_text(
            prompt_id,
            "prompt_id",
            max_length=255,
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

                connection.execute(
                    project_prompt_defaults.delete().where(
                        project_prompt_defaults.c.organization_id
                        == self.organization_id,
                        project_prompt_defaults.c.project_id
                        == self.project_id,
                        project_prompt_defaults.c.prompt_id == normalized_id,
                    )
                )
                # The prompt head owns its version rows. The delete cascade
                # removes the current and historical rows in one transaction.
                connection.execute(
                    project_prompt_heads.delete().where(
                        project_prompt_heads.c.organization_id
                        == self.organization_id,
                        project_prompt_heads.c.project_id
                        == self.project_id,
                        project_prompt_heads.c.prompt_id == normalized_id,
                    )
                )
                self._append_audit(
                    connection,
                    actor=actor,
                    prompt_id=normalized_id,
                    action="project_prompt.deleted",
                    details={
                        "kind": str(current["kind"]),
                        "version": int(current["current_version"]),
                    },
                )
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
        expected_version: int,
        active: bool,
    ) -> ServerProjectPromptItem:
        normalized_id = _required_text(
            prompt_id,
            "prompt_id",
            max_length=255,
        )
        if expected_version <= 0:
            raise ServerProjectPromptError(
                "expected_version must be positive"
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
                if int(current["current_version"]) != expected_version:
                    raise ServerProjectPromptConflict(
                        "project prompt version conflict"
                    )
                if current["status"] == status:
                    return ServerProjectPromptItem(
                        snapshot=self._snapshot(
                            current,
                            source="library",
                        ),
                        status=status,
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
                return ServerProjectPromptItem(
                    snapshot=self._snapshot(
                        current,
                        source="library",
                    ),
                    status=status,
                )
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
        with self._engine.connect() as connection:
            return self._resolve_in_connection(
                connection,
                kind=kind,
                selection=selection,
                lock=False,
            )

    def list(
        self,
        actor: ActorIdentity,
    ) -> ServerProjectPromptDirectory:
        self._require_read(actor)
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(
                    project_prompt_heads.c.prompt_id,
                    project_prompt_heads.c.kind,
                    project_prompt_heads.c.status,
                    project_prompt_versions.c.version,
                    project_prompt_versions.c.name,
                    project_prompt_versions.c.content,
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
                )
                .order_by(
                    project_prompt_heads.c.status,
                    project_prompt_heads.c.kind,
                    sa.func.lower(project_prompt_versions.c.name),
                    project_prompt_heads.c.prompt_id,
                )
            ).mappings().all()
            default_rows = connection.execute(
                sa.select(
                    project_prompt_defaults.c.kind,
                    project_prompt_defaults.c.prompt_id,
                    project_prompt_defaults.c.version,
                    project_prompt_versions.c.name,
                    project_prompt_versions.c.content,
                    project_prompt_versions.c.created_at,
                )
                .select_from(
                    project_prompt_defaults.join(
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
                )
            ).mappings().all()
        prompts = tuple(
            ServerProjectPromptItem(
                snapshot=self._snapshot(row, source="library"),
                status=cast(PromptStatus, row["status"]),
            )
            for row in rows
        )
        defaults = {
            cast(PromptKind, row["kind"]): self._snapshot(
                row,
                source="project_default",
            )
            for row in default_rows
        }
        return ServerProjectPromptDirectory(
            prompts=prompts,
            defaults=defaults,
        )


class ServerProjectPromptServiceFactory:
    """Construct prompt services only from an already authorized scope."""

    def __init__(
        self,
        engine: Engine,
        *,
        audit: AuditEventWriter | None = None,
    ) -> None:
        self._engine = engine
        self._audit = audit

    def create(
        self,
        authorized: AuthorizedProjectRequest,
    ) -> PostgresProjectPromptService:
        return PostgresProjectPromptService(
            self._engine,
            organization_id=authorized.actor.organization_id,
            project_id=authorized.project_id,
            audit=self._audit,
        )
__all__ = [
    "PostgresProjectPromptService",
    "PromptKind",
    "PromptStatus",
    "ServerProjectPromptConflict",
    "ServerProjectPromptError",
    "ServerProjectPromptUnavailable",
    "ServerProjectPromptDirectory",
    "ServerProjectPromptItem",
    "ServerProjectPromptServiceFactory",
]
