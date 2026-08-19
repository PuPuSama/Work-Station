from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from config import AppConfig
from server_schema import organization_llm_settings, workspace_users
from services.access_control import ActorIdentity
from services.audit_log import AuditEvent, AuditEventWriter, PostgresAuditEventWriter
from services.llm import LLMClient


class ServerLlmSettingsDenied(PermissionError):
    """The actor cannot read or change the organization model settings."""


class ServerLlmSettingsConflict(RuntimeError):
    """The organization model settings changed since the client loaded them."""


class ServerLlmSettingsUnavailable(RuntimeError):
    """The PostgreSQL-backed model settings are unavailable."""


@dataclass(frozen=True, slots=True)
class ServerLlmSettings:
    model: str
    reasoning_effort: str
    revision: int
    updated_at: str | None
    can_edit: bool = False


def _timestamp(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    normalized = str(value).strip()
    return normalized or None


def _required_text(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized


class PostgresServerLlmSettings:
    """Read and update organization-scoped runtime LLM choices."""

    def __init__(
        self,
        engine: Engine,
        *,
        audit: AuditEventWriter | None = None,
    ) -> None:
        self._engine = engine
        self._audit = audit or PostgresAuditEventWriter()

    @staticmethod
    def _record(
        row: Mapping[str, object] | None,
        *,
        fallback_model: str,
        fallback_reasoning_effort: str,
        can_edit: bool,
    ) -> ServerLlmSettings:
        if row is None:
            return ServerLlmSettings(
                model=fallback_model,
                reasoning_effort=fallback_reasoning_effort,
                revision=0,
                updated_at=None,
                can_edit=can_edit,
            )
        return ServerLlmSettings(
            model=str(row["model"]),
            reasoning_effort=str(row["reasoning_effort"]),
            revision=int(row["revision"]),
            updated_at=_timestamp(row["updated_at"]),
            can_edit=can_edit,
        )

    def _organization_role(
        self,
        connection: Connection,
        organization_id: str,
        user_id: str | None = None,
    ) -> str | None:
        statement = sa.select(workspace_users.c.organization_role).where(
            workspace_users.c.organization_id == organization_id,
            workspace_users.c.status == "active",
        )
        if user_id is not None:
            statement = statement.where(workspace_users.c.user_id == user_id)
        return connection.execute(statement).scalar_one_or_none()

    def get_for_organization(
        self,
        organization_id: str,
        *,
        fallback_model: str,
        fallback_reasoning_effort: str,
    ) -> ServerLlmSettings:
        organization_id = _required_text(organization_id, "organization_id")
        try:
            with self._engine.connect() as connection:
                row = connection.execute(
                    sa.select(
                        organization_llm_settings.c.model,
                        organization_llm_settings.c.reasoning_effort,
                        organization_llm_settings.c.revision,
                        organization_llm_settings.c.updated_at,
                    ).where(
                        organization_llm_settings.c.organization_id
                        == organization_id
                    )
                ).mappings().one_or_none()
        except (SQLAlchemyError, RuntimeError) as exc:
            raise ServerLlmSettingsUnavailable(
                "organization model settings are unavailable"
            ) from exc
        return self._record(
            row,
            fallback_model=_required_text(fallback_model, "fallback_model"),
            fallback_reasoning_effort=_required_text(
                fallback_reasoning_effort,
                "fallback_reasoning_effort",
            ),
            can_edit=False,
        )

    def get(
        self,
        *,
        actor: ActorIdentity,
        fallback_model: str,
        fallback_reasoning_effort: str,
    ) -> ServerLlmSettings:
        try:
            with self._engine.connect() as connection:
                role = self._organization_role(
                    connection,
                    actor.organization_id,
                    actor.user_id,
                )
                if role is None:
                    raise ServerLlmSettingsDenied(
                        "organization model settings read denied"
                    )
                row = connection.execute(
                    sa.select(
                        organization_llm_settings.c.model,
                        organization_llm_settings.c.reasoning_effort,
                        organization_llm_settings.c.revision,
                        organization_llm_settings.c.updated_at,
                    ).where(
                        organization_llm_settings.c.organization_id
                        == actor.organization_id
                    )
                ).mappings().one_or_none()
        except ServerLlmSettingsDenied:
            raise
        except (SQLAlchemyError, RuntimeError) as exc:
            raise ServerLlmSettingsUnavailable(
                "organization model settings are unavailable"
            ) from exc
        return self._record(
            row,
            fallback_model=_required_text(fallback_model, "fallback_model"),
            fallback_reasoning_effort=_required_text(
                fallback_reasoning_effort,
                "fallback_reasoning_effort",
            ),
            # Every active workspace member may change the shared setting.
            # The organization membership check above remains the boundary;
            # this is not exposed to unauthenticated or inactive users.
            can_edit=True,
        )

    def update(
        self,
        *,
        actor: ActorIdentity,
        expected_revision: int,
        model: str,
        reasoning_effort: str,
        allowed_models: Sequence[str],
        allowed_reasoning_efforts: Sequence[str],
        event_id: str | None = None,
    ) -> ServerLlmSettings:
        if expected_revision < 0:
            raise ValueError("revision must not be negative")
        normalized_model = _required_text(model, "model")
        normalized_effort = _required_text(
            reasoning_effort,
            "reasoning_effort",
        )
        if normalized_model not in set(allowed_models):
            raise ValueError("selected model is not available")
        if normalized_effort not in set(allowed_reasoning_efforts):
            raise ValueError("selected reasoning effort is not available")
        try:
            with self._engine.begin() as connection:
                role = self._organization_role(
                    connection,
                    actor.organization_id,
                    actor.user_id,
                )
                if role is None:
                    raise ServerLlmSettingsDenied(
                        "organization model settings update denied"
                    )
                current = connection.execute(
                    sa.select(
                        organization_llm_settings.c.model,
                        organization_llm_settings.c.reasoning_effort,
                        organization_llm_settings.c.revision,
                        organization_llm_settings.c.updated_at,
                    )
                    .where(
                        organization_llm_settings.c.organization_id
                        == actor.organization_id
                    )
                    .with_for_update()
                ).mappings().one_or_none()
                current_revision = 0 if current is None else int(current["revision"])
                if current_revision != expected_revision:
                    raise ServerLlmSettingsConflict(
                        "organization model settings revision changed"
                    )
                next_revision = expected_revision + 1
                if current is None:
                    connection.execute(
                        organization_llm_settings.insert().values(
                            organization_id=actor.organization_id,
                            model=normalized_model,
                            reasoning_effort=normalized_effort,
                            revision=next_revision,
                        )
                    )
                else:
                    result = connection.execute(
                        organization_llm_settings.update()
                        .where(
                            organization_llm_settings.c.organization_id
                            == actor.organization_id,
                            organization_llm_settings.c.revision
                            == expected_revision,
                        )
                        .values(
                            model=normalized_model,
                            reasoning_effort=normalized_effort,
                            revision=next_revision,
                            updated_at=sa.func.now(),
                        )
                    )
                    if result.rowcount != 1:
                        raise ServerLlmSettingsConflict(
                            "organization model settings revision changed"
                        )
                self._audit.append(
                    connection,
                    AuditEvent(
                        organization_id=actor.organization_id,
                        event_id=(
                            event_id or "llm_settings_" + uuid.uuid4().hex
                        ),
                        actor_user_id=actor.user_id,
                        action="workspace.llm_settings.updated",
                        target_type="organization_llm_settings",
                        target_id=actor.organization_id,
                        details={
                            "model": normalized_model,
                            "reasoning_effort": normalized_effort,
                            "revision": next_revision,
                        },
                    ),
                )
                updated = connection.execute(
                    sa.select(
                        organization_llm_settings.c.model,
                        organization_llm_settings.c.reasoning_effort,
                        organization_llm_settings.c.revision,
                        organization_llm_settings.c.updated_at,
                    ).where(
                        organization_llm_settings.c.organization_id
                        == actor.organization_id
                    )
                ).mappings().one()
        except (ServerLlmSettingsDenied, ServerLlmSettingsConflict, ValueError):
            raise
        except (SQLAlchemyError, RuntimeError) as exc:
            raise ServerLlmSettingsUnavailable(
                "organization model settings could not be updated"
            ) from exc
        return self._record(
            updated,
            fallback_model=normalized_model,
            fallback_reasoning_effort=normalized_effort,
            can_edit=True,
        )


class ServerLlmClientFactory:
    """Create per-job clients using the latest organization setting."""

    def __init__(
        self,
        config: AppConfig,
        settings: PostgresServerLlmSettings,
    ) -> None:
        self._config = config
        self._settings = settings
        self._base_client = LLMClient(config)

    @property
    def ready(self) -> bool:
        return self._base_client.ready

    def client(self, organization_id: str, *, title: bool = False) -> LLMClient:
        selected = self._settings.get_for_organization(
            organization_id,
            fallback_model=self._config.llm_model,
            fallback_reasoning_effort=self._config.llm_reasoning_effort,
        )
        selected_config = replace(
            self._config,
            llm_model=selected.model,
            llm_reasoning_effort=("low" if title else selected.reasoning_effort),
            llm_runtime_override=True,
        )
        return LLMClient(selected_config)


__all__ = [
    "PostgresServerLlmSettings",
    "ServerLlmClientFactory",
    "ServerLlmSettings",
    "ServerLlmSettingsConflict",
    "ServerLlmSettingsDenied",
    "ServerLlmSettingsUnavailable",
]
