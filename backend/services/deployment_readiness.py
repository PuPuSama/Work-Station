from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from knowledge_agent.settings import (
    KnowledgeAgentConfigurationError,
    KnowledgeAgentSettings,
)
from services.object_store import (
    ObjectStore,
    ObjectStoreError,
    S3ObjectStoreSettings,
)
from services.oidc_identity import (
    OidcConfigurationError,
    OidcProviderSettings,
    OidcProviderUnavailable,
)
from services.recovery_evidence import VerifiedRecoveryEvidence
from services.server_auth import (
    ServerActorSessionError,
    load_server_actor_session_codec,
    server_mode_enabled,
)

# Keep the signed deployment evidence bound to the schema that the Server
# runtime actually requires.  The durable assistant dispatch inbox was added
# in 0033; accepting the old M1 merge head would let a partially migrated
# deployment pass the preflight gate and fail only when a user sends a plan.
EXPECTED_ALEMBIC_HEAD = "20260826_0033"


@dataclass(frozen=True, slots=True)
class DatabaseReadiness:
    revision: str
    vector_extension: str


@dataclass(frozen=True, slots=True)
class ServerCutoverCapabilities:
    """Code capabilities that must exist before server mode may be exposed."""

    trusted_identity_source: bool = False
    project_routes_scoped: bool = False
    postgres_task_single_write: bool = False
    postgres_job_single_write: bool = False
    worker_reauthorizes: bool = False
    object_download_reauthorizes: bool = False

    def missing(self) -> tuple[str, ...]:
        return tuple(
            name
            for name, enabled in (
                ("trusted_identity_source", self.trusted_identity_source),
                ("project_routes_scoped", self.project_routes_scoped),
                (
                    "postgres_task_single_write",
                    self.postgres_task_single_write,
                ),
                (
                    "postgres_job_single_write",
                    self.postgres_job_single_write,
                ),
                ("worker_reauthorizes", self.worker_reauthorizes),
                (
                    "object_download_reauthorizes",
                    self.object_download_reauthorizes,
                ),
            )
            if not enabled
        )


CURRENT_SERVER_CUTOVER_CAPABILITIES = ServerCutoverCapabilities(
    trusted_identity_source=True,
    project_routes_scoped=True,
    postgres_task_single_write=True,
    postgres_job_single_write=True,
    worker_reauthorizes=True,
    object_download_reauthorizes=True,
)


@dataclass(frozen=True, slots=True)
class PreflightCheck:
    check_id: str
    passed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class DeploymentPreflightReport:
    checks: tuple[PreflightCheck, ...]

    @property
    def ready(self) -> bool:
        return all(check.passed for check in self.checks)

    def public_values(self) -> dict[str, object]:
        """Safe report containing no URLs, credentials, or provider bodies."""

        return {
            "ready": self.ready,
            "checks": [
                {
                    "id": check.check_id,
                    "passed": check.passed,
                    "detail": check.detail,
                }
                for check in self.checks
            ],
        }


class DatabaseProbe(Protocol):
    def __call__(self) -> DatabaseReadiness: ...


def postgres_database_probe(engine: Engine) -> DatabaseReadiness:
    """Read-only PostgreSQL/Alembic/pgvector deployment probe."""

    try:
        with engine.connect() as connection:
            connection.execute(sa.text("SELECT 1")).scalar_one()
            revision = str(
                connection.execute(
                    sa.text("SELECT version_num FROM alembic_version")
                ).scalar_one()
            )
            vector_extension = str(
                connection.execute(
                    sa.text(
                        "SELECT extversion FROM pg_extension "
                        "WHERE extname = 'vector'"
                    )
                ).scalar_one()
            )
    except (SQLAlchemyError, ValueError) as exc:
        raise RuntimeError("database readiness check failed") from exc
    return DatabaseReadiness(
        revision=revision,
        vector_extension=vector_extension,
    )


def _configuration_checks(
    environment: Mapping[str, str],
) -> tuple[
    list[PreflightCheck],
    KnowledgeAgentSettings | None,
    S3ObjectStoreSettings | None,
    OidcProviderSettings | None,
]:
    checks: list[PreflightCheck] = []
    try:
        enabled = server_mode_enabled(environment)
        checks.append(
            PreflightCheck(
                "server_mode",
                enabled,
                "enabled" if enabled else "disabled",
            )
        )
    except ServerActorSessionError:
        checks.append(
            PreflightCheck("server_mode", False, "invalid configuration")
        )

    oidc_settings: OidcProviderSettings | None = None
    try:
        oidc_settings = OidcProviderSettings.from_environment(
            environment
        )
        checks.append(
            PreflightCheck(
                "oidc_config",
                oidc_settings is not None,
                "configured"
                if oidc_settings is not None
                else "not ready",
            )
        )
    except OidcConfigurationError:
        checks.append(
            PreflightCheck("oidc_config", False, "not ready")
        )

    try:
        load_server_actor_session_codec(environment)
        checks.append(PreflightCheck("actor_session", True, "configured"))
    except ServerActorSessionError:
        checks.append(
            PreflightCheck("actor_session", False, "not ready")
        )

    knowledge_settings: KnowledgeAgentSettings | None = None
    try:
        knowledge_settings = KnowledgeAgentSettings.from_env(
            enabled=True,
            environ=environment,
        ).require_ready()
        checks.append(PreflightCheck("knowledge_runtime", True, "configured"))
    except KnowledgeAgentConfigurationError:
        checks.append(
            PreflightCheck("knowledge_runtime", False, "not ready")
        )

    object_settings: S3ObjectStoreSettings | None = None
    try:
        object_settings = S3ObjectStoreSettings.from_environment(environment)
        encrypted = bool(object_settings.server_side_encryption)
        checks.append(
            PreflightCheck(
                "object_store_config",
                encrypted,
                "configured with server-side encryption"
                if encrypted
                else "server-side encryption is disabled",
            )
        )
        endpoint = urlsplit(object_settings.endpoint_url)
        public_transport = (
            not object_settings.endpoint_url
            or endpoint.scheme == "https"
            or endpoint.hostname in {"localhost", "127.0.0.1", "::1"}
        )
        internal_endpoint = urlsplit(object_settings.internal_endpoint_url)
        internal_hostname = internal_endpoint.hostname or ""
        internal_transport = (
            not object_settings.internal_endpoint_url
            or internal_endpoint.scheme == "https"
            or internal_hostname in {"localhost", "127.0.0.1", "::1"}
            or (
                internal_endpoint.scheme == "http"
                and "." not in internal_hostname
                and ":" not in internal_hostname
            )
        )
        secure_transport = public_transport and internal_transport
        checks.append(
            PreflightCheck(
                "object_store_transport",
                secure_transport,
                "secure public and private internal transport"
                if secure_transport
                else (
                    "public endpoints must use HTTPS; internal HTTP endpoints "
                    "must use a loopback or single-label service hostname"
                ),
            )
        )
    except ValueError:
        checks.append(
            PreflightCheck("object_store_config", False, "not ready")
        )
        checks.append(
            PreflightCheck("object_store_transport", False, "not ready")
        )
    return checks, knowledge_settings, object_settings, oidc_settings


def run_deployment_preflight(
    *,
    environment: Mapping[str, str],
    database_probe: DatabaseProbe,
    object_store_factory: Callable[[S3ObjectStoreSettings], ObjectStore],
    identity_provider_probe: (
        Callable[[OidcProviderSettings], None] | None
    ) = None,
    capabilities: ServerCutoverCapabilities = CURRENT_SERVER_CUTOVER_CAPABILITIES,
    recovery_evidence: VerifiedRecoveryEvidence | None = None,
) -> DeploymentPreflightReport:
    """Run fail-closed checks without returning sensitive configuration."""

    verified_recovery_evidence = (
        recovery_evidence
        if isinstance(recovery_evidence, VerifiedRecoveryEvidence)
        else None
    )

    (
        checks,
        knowledge_settings,
        object_settings,
        oidc_settings,
    ) = _configuration_checks(environment)

    if knowledge_settings is not None:
        try:
            database = database_probe()
            revision_ready = database.revision == EXPECTED_ALEMBIC_HEAD
            checks.append(
                PreflightCheck(
                    "database",
                    revision_ready and bool(database.vector_extension),
                    "schema and pgvector ready"
                    if revision_ready and database.vector_extension
                    else "schema revision or pgvector is not ready",
                )
            )
        except RuntimeError:
            checks.append(
                PreflightCheck("database", False, "readiness probe failed")
            )
    else:
        checks.append(
            PreflightCheck("database", False, "configuration unavailable")
        )

    if oidc_settings is not None and identity_provider_probe is not None:
        try:
            identity_provider_probe(oidc_settings)
            checks.append(
                PreflightCheck(
                    "identity_provider",
                    True,
                    "metadata and signing keys reachable",
                )
            )
        except OidcProviderUnavailable:
            checks.append(
                PreflightCheck(
                    "identity_provider",
                    False,
                    "readiness probe failed",
                )
            )
    else:
        checks.append(
            PreflightCheck(
                "identity_provider",
                False,
                "configuration unavailable",
            )
        )

    if object_settings is not None:
        try:
            object_store_factory(object_settings).check_ready()
            checks.append(
                PreflightCheck("object_store", True, "bucket reachable")
            )
        except (ObjectStoreError, ValueError):
            checks.append(
                PreflightCheck("object_store", False, "readiness probe failed")
            )
    else:
        checks.append(
            PreflightCheck(
                "object_store",
                False,
                "configuration unavailable",
            )
        )

    missing_capabilities = capabilities.missing()
    checks.append(
        PreflightCheck(
            "server_cutover",
            not missing_capabilities,
            "all code gates ready"
            if not missing_capabilities
            else "missing: " + ", ".join(missing_capabilities),
        )
    )
    recovery_checks = (
        (
            "recovery_evidence_identity",
            verified_recovery_evidence is not None,
        ),
        (
            "database_restore",
            verified_recovery_evidence.database_restore_passed
            if verified_recovery_evidence is not None
            else False,
        ),
        (
            "object_restore",
            verified_recovery_evidence.object_restore_passed
            if verified_recovery_evidence is not None
            else False,
        ),
        (
            "recovery_objectives",
            verified_recovery_evidence.recovery_objectives_passed
            if verified_recovery_evidence is not None
            else False,
        ),
    )
    checks.extend(
        PreflightCheck(
            check_id,
            passed,
            "verified" if passed else "not verified",
        )
        for check_id, passed in recovery_checks
    )
    return DeploymentPreflightReport(tuple(checks))


__all__ = [
    "CURRENT_SERVER_CUTOVER_CAPABILITIES",
    "DatabaseReadiness",
    "DeploymentPreflightReport",
    "EXPECTED_ALEMBIC_HEAD",
    "PreflightCheck",
    "ServerCutoverCapabilities",
    "postgres_database_probe",
    "run_deployment_preflight",
]
