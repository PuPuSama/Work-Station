from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .contracts import EMBEDDING_DIMENSIONS


DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"


class KnowledgeAgentConfigurationError(ValueError):
    """Raised when the optional knowledge-agent runtime is not ready."""


def _optional_environment_value(
    environment: Mapping[str, str], name: str
) -> str | None:
    value = environment.get(name)
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _embedding_dimensions(environment: Mapping[str, str]) -> int:
    raw_value = _optional_environment_value(environment, "EMBEDDING_DIMENSIONS")
    if raw_value is None:
        return EMBEDDING_DIMENSIONS
    try:
        dimensions = int(raw_value)
    except ValueError as exc:
        raise KnowledgeAgentConfigurationError(
            "EMBEDDING_DIMENSIONS must be an integer"
        ) from exc
    if dimensions != EMBEDDING_DIMENSIONS:
        raise KnowledgeAgentConfigurationError(
            f"EMBEDDING_DIMENSIONS must be {EMBEDDING_DIMENSIONS} for M1"
        )
    return dimensions


def _validate_database_url(value: str) -> None:
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise KnowledgeAgentConfigurationError(
            "ARTICLE_AGENT_DATABASE_URL must be a PostgreSQL URL"
        ) from exc
    if parsed.scheme != "postgresql+psycopg" or not parsed.hostname:
        raise KnowledgeAgentConfigurationError(
            "ARTICLE_AGENT_DATABASE_URL must use the postgresql+psycopg driver"
        )


def _validate_embedding_base_url(value: str) -> None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise KnowledgeAgentConfigurationError(
            "EMBEDDING_BASE_URL must be an absolute HTTP(S) URL"
        ) from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port is not None
        and not 0 < port < 65536
    ):
        raise KnowledgeAgentConfigurationError(
            "EMBEDDING_BASE_URL must be an absolute HTTP(S) URL"
        )


@dataclass(frozen=True, slots=True)
class KnowledgeAgentSettings:
    enabled: bool = False
    database_url: str | None = field(default=None, repr=False)
    embedding_base_url: str | None = None
    embedding_api_key: str | None = field(default=None, repr=False)
    embedding_model: str = DEFAULT_EMBEDDING_MODEL
    embedding_dimensions: int = EMBEDDING_DIMENSIONS

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise KnowledgeAgentConfigurationError("enabled must be a boolean")
        if self.embedding_dimensions != EMBEDDING_DIMENSIONS:
            raise KnowledgeAgentConfigurationError(
                f"EMBEDDING_DIMENSIONS must be {EMBEDDING_DIMENSIONS} for M1"
            )

        for name in (
            "database_url",
            "embedding_base_url",
            "embedding_api_key",
        ):
            value = getattr(self, name)
            if value is not None:
                if not isinstance(value, str):
                    raise KnowledgeAgentConfigurationError(f"{name} must be a string")
                object.__setattr__(self, name, value.strip() or None)

        if not isinstance(self.embedding_model, str):
            raise KnowledgeAgentConfigurationError("EMBEDDING_MODEL must be a string")
        object.__setattr__(
            self,
            "embedding_model",
            self.embedding_model.strip() or DEFAULT_EMBEDDING_MODEL,
        )

        if self.database_url is not None:
            _validate_database_url(self.database_url)
        if self.embedding_base_url is not None:
            _validate_embedding_base_url(self.embedding_base_url)

    @classmethod
    def from_env(
        cls,
        *,
        enabled: bool = False,
        environ: Mapping[str, str] | None = None,
    ) -> KnowledgeAgentSettings:
        """Read only dedicated database/embedding variables.

        ``enabled`` deliberately comes from the application's existing
        ``AppConfig.knowledge_agent_enabled`` flag. This layer does not parse a
        second feature-flag source.
        """

        environment = os.environ if environ is None else environ
        return cls(
            enabled=enabled,
            database_url=_optional_environment_value(
                environment, "ARTICLE_AGENT_DATABASE_URL"
            ),
            embedding_base_url=_optional_environment_value(
                environment, "EMBEDDING_BASE_URL"
            ),
            embedding_api_key=_optional_environment_value(
                environment, "EMBEDDING_API_KEY"
            ),
            embedding_model=(
                _optional_environment_value(environment, "EMBEDDING_MODEL")
                or DEFAULT_EMBEDDING_MODEL
            ),
            embedding_dimensions=_embedding_dimensions(environment),
        )

    @property
    def ready(self) -> bool:
        return all(
            (
                self.database_url,
                self.embedding_base_url,
                self.embedding_api_key,
                self.embedding_model,
            )
        )

    def require_ready(self) -> KnowledgeAgentSettings:
        missing = [
            environment_name
            for environment_name, value in (
                ("ARTICLE_AGENT_DATABASE_URL", self.database_url),
                ("EMBEDDING_BASE_URL", self.embedding_base_url),
                ("EMBEDDING_API_KEY", self.embedding_api_key),
                ("EMBEDDING_MODEL", self.embedding_model),
            )
            if not value
        ]
        if missing:
            raise KnowledgeAgentConfigurationError(
                "knowledge agent configuration is incomplete; missing "
                + ", ".join(missing)
            )
        return self

    def public_values(self) -> dict[str, object]:
        """Return readiness metadata without database credentials or API keys."""

        return {
            "enabled": self.enabled,
            "ready": self.ready,
            "embedding_model": self.embedding_model,
            "embedding_dimensions": self.embedding_dimensions,
        }


def load_knowledge_agent_settings(
    *,
    enabled: bool,
    environ: Mapping[str, str] | None = None,
    require_ready: bool | None = None,
) -> KnowledgeAgentSettings:
    """Load M1 settings using the feature flag supplied by ``AppConfig``."""

    settings = KnowledgeAgentSettings.from_env(enabled=enabled, environ=environ)
    should_require_ready = settings.enabled if require_ready is None else require_ready
    return settings.require_ready() if should_require_ready else settings
