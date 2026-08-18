from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

import sqlalchemy as sa


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services.deployment_readiness import (  # noqa: E402
    CURRENT_SERVER_CUTOVER_CAPABILITIES,
    DatabaseReadiness,
    ServerCutoverCapabilities,
    postgres_database_probe,
    run_deployment_preflight,
)
from services.object_store import ObjectStoreError  # noqa: E402
from services.oidc_identity import (  # noqa: E402
    OidcProviderUnavailable,
)


COMPLETE_ENVIRONMENT = {
    "ARTICLE_AGENT_SERVER_MODE": "true",
    "ARTICLE_AGENT_SERVER_SESSION_SECRET": "s" * 32,
    "ARTICLE_AGENT_OIDC_ISSUER": "https://identity.test/tenant",
    "ARTICLE_AGENT_OIDC_CLIENT_ID": "article-agent",
    "ARTICLE_AGENT_OIDC_CLIENT_SECRET": "private-oidc-secret",
    "ARTICLE_AGENT_OIDC_REDIRECT_URI": (
        "https://app.test/api/auth/oidc/callback"
    ),
    "ARTICLE_AGENT_DATABASE_URL": (
        "postgresql+psycopg://user:private-db-password@db.test/app"
    ),
    "EMBEDDING_BASE_URL": "https://embedding.test/v1",
    "EMBEDDING_API_KEY": "private-embedding-key",
    "EMBEDDING_MODEL": "text-embedding-3-small",
    "EMBEDDING_DIMENSIONS": "1536",
    "ARTICLE_AGENT_OBJECT_STORE_BUCKET": "private-bucket",
    "ARTICLE_AGENT_OBJECT_STORE_REGION": "us-east-1",
    "ARTICLE_AGENT_OBJECT_STORE_ENDPOINT": "https://objects.test",
    "ARTICLE_AGENT_OBJECT_STORE_ACCESS_KEY": "private-access-key",
    "ARTICLE_AGENT_OBJECT_STORE_SECRET_KEY": "private-object-secret",
    "ARTICLE_AGENT_OBJECT_STORE_SSE": "AES256",
}


class FakeReadyStore:
    def check_ready(self):
        return None


class FakeFailedStore:
    def check_ready(self):
        raise ObjectStoreError("provider included private-object-secret")


class DeploymentReadinessTests(unittest.TestCase):
    def test_missing_recovery_evidence_fails_without_exposing_secrets(
        self,
    ) -> None:
        report = run_deployment_preflight(
            environment=COMPLETE_ENVIRONMENT,
            database_probe=lambda: DatabaseReadiness(
                revision="20260817_0024",
                vector_extension="0.8.1",
            ),
            object_store_factory=lambda settings: FakeReadyStore(),
            identity_provider_probe=lambda settings: None,
            capabilities=ServerCutoverCapabilities(
                trusted_identity_source=True,
                project_routes_scoped=True,
                postgres_task_single_write=True,
                postgres_job_single_write=True,
                worker_reauthorizes=True,
                object_download_reauthorizes=True,
            ),
        )

        self.assertFalse(report.ready)
        public = str(report.public_values())
        for secret in (
            "private-db-password",
            "private-embedding-key",
            "private-access-key",
            "private-object-secret",
            "private-oidc-secret",
        ):
            self.assertNotIn(secret, public)

    def test_current_capabilities_and_missing_attestation_fail_closed(self) -> None:
        self.assertTrue(
            CURRENT_SERVER_CUTOVER_CAPABILITIES.trusted_identity_source
        )
        self.assertTrue(
            CURRENT_SERVER_CUTOVER_CAPABILITIES.project_routes_scoped
        )
        self.assertTrue(
            CURRENT_SERVER_CUTOVER_CAPABILITIES.postgres_task_single_write
        )
        self.assertTrue(
            CURRENT_SERVER_CUTOVER_CAPABILITIES.postgres_job_single_write
        )
        self.assertTrue(
            CURRENT_SERVER_CUTOVER_CAPABILITIES.worker_reauthorizes
        )
        self.assertTrue(
            CURRENT_SERVER_CUTOVER_CAPABILITIES.object_download_reauthorizes
        )
        report = run_deployment_preflight(
            environment=COMPLETE_ENVIRONMENT,
            database_probe=lambda: DatabaseReadiness(
                revision="20260817_0024",
                vector_extension="0.8.1",
            ),
            object_store_factory=lambda settings: FakeReadyStore(),
            identity_provider_probe=lambda settings: None,
        )

        self.assertFalse(report.ready)
        by_id = {
            check.check_id: check for check in report.checks
        }
        self.assertTrue(by_id["server_cutover"].passed)
        self.assertNotIn(
            "trusted_identity_source",
            by_id["server_cutover"].detail,
        )
        self.assertNotIn(
            "project_routes_scoped",
            by_id["server_cutover"].detail,
        )
        self.assertNotIn(
            "postgres_task_single_write",
            by_id["server_cutover"].detail,
        )
        self.assertNotIn(
            "postgres_job_single_write",
            by_id["server_cutover"].detail,
        )
        self.assertNotIn(
            "worker_reauthorizes",
            by_id["server_cutover"].detail,
        )
        for check_id in (
            "recovery_evidence_identity",
            "database_restore",
            "object_restore",
            "recovery_objectives",
        ):
            self.assertFalse(by_id[check_id].passed)

    def test_probe_failures_and_configuration_errors_are_generic(self) -> None:
        environment = {
            **COMPLETE_ENVIRONMENT,
            "ARTICLE_AGENT_SERVER_SESSION_SECRET": "too-short",
            "ARTICLE_AGENT_OBJECT_STORE_SSE": "none",
        }

        def failed_database():
            raise RuntimeError(
                "database URL contained private-db-password"
            )

        def failed_identity_provider(settings):
            raise OidcProviderUnavailable(
                "provider included private-oidc-secret"
            )

        report = run_deployment_preflight(
            environment=environment,
            database_probe=failed_database,
            object_store_factory=lambda settings: FakeFailedStore(),
            identity_provider_probe=failed_identity_provider,
        )
        public = str(report.public_values())

        self.assertFalse(report.ready)
        self.assertNotIn("private-db-password", public)
        self.assertNotIn("private-object-secret", public)
        self.assertNotIn("private-oidc-secret", public)
        self.assertIn("readiness probe failed", public)

    def test_wrong_schema_revision_blocks_deployment(self) -> None:
        report = run_deployment_preflight(
            environment=COMPLETE_ENVIRONMENT,
            database_probe=lambda: DatabaseReadiness(
                revision="20260730_0008",
                vector_extension="0.8.1",
            ),
            object_store_factory=lambda settings: FakeReadyStore(),
            identity_provider_probe=lambda settings: None,
            capabilities=ServerCutoverCapabilities(
                True,
                True,
                True,
                True,
                True,
                True,
            ),
        )
        self.assertFalse(report.ready)
        database = next(
            check for check in report.checks if check.check_id == "database"
        )
        self.assertFalse(database.passed)

    def test_remote_plain_http_object_endpoint_blocks_deployment(self) -> None:
        report = run_deployment_preflight(
            environment={
                **COMPLETE_ENVIRONMENT,
                "ARTICLE_AGENT_OBJECT_STORE_ENDPOINT": (
                    "http://objects.internal.test"
                ),
            },
            database_probe=lambda: DatabaseReadiness(
                revision="20260817_0024",
                vector_extension="0.8.1",
            ),
            object_store_factory=lambda settings: FakeReadyStore(),
            identity_provider_probe=lambda settings: None,
            capabilities=ServerCutoverCapabilities(
                True,
                True,
                True,
                True,
                True,
                True,
            ),
        )
        transport = next(
            check
            for check in report.checks
            if check.check_id == "object_store_transport"
        )
        self.assertFalse(report.ready)
        self.assertFalse(transport.passed)

    def test_single_label_internal_object_endpoint_is_allowed(self) -> None:
        report = run_deployment_preflight(
            environment={
                **COMPLETE_ENVIRONMENT,
                "ARTICLE_AGENT_OBJECT_STORE_INTERNAL_ENDPOINT": (
                    "http://article-object-store:9000"
                ),
            },
            database_probe=lambda: DatabaseReadiness(
                revision="20260817_0024",
                vector_extension="0.8.1",
            ),
            object_store_factory=lambda settings: FakeReadyStore(),
            identity_provider_probe=lambda settings: None,
            capabilities=ServerCutoverCapabilities(
                True,
                True,
                True,
                True,
                True,
                True,
            ),
        )

        transport = next(
            check
            for check in report.checks
            if check.check_id == "object_store_transport"
        )
        self.assertTrue(transport.passed)

    def test_remote_plain_http_internal_object_endpoint_is_rejected(self) -> None:
        report = run_deployment_preflight(
            environment={
                **COMPLETE_ENVIRONMENT,
                "ARTICLE_AGENT_OBJECT_STORE_INTERNAL_ENDPOINT": (
                    "http://objects.internal.test"
                ),
            },
            database_probe=lambda: DatabaseReadiness(
                revision="20260817_0024",
                vector_extension="0.8.1",
            ),
            object_store_factory=lambda settings: FakeReadyStore(),
            identity_provider_probe=lambda settings: None,
            capabilities=ServerCutoverCapabilities(
                True,
                True,
                True,
                True,
                True,
                True,
            ),
        )

        transport = next(
            check
            for check in report.checks
            if check.check_id == "object_store_transport"
        )
        self.assertFalse(transport.passed)

    @unittest.skipUnless(
        os.environ.get("ARTICLE_AGENT_DATABASE_URL"),
        "ARTICLE_AGENT_DATABASE_URL is required for the PostgreSQL probe",
    )
    def test_real_postgres_probe_reports_head_and_pgvector(self) -> None:
        engine = sa.create_engine(
            os.environ["ARTICLE_AGENT_DATABASE_URL"],
            pool_pre_ping=True,
        )
        try:
            readiness = postgres_database_probe(engine)
        finally:
            engine.dispose()
        self.assertEqual(readiness.revision, "20260817_0024")
        self.assertTrue(readiness.vector_extension)


if __name__ == "__main__":
    unittest.main()
