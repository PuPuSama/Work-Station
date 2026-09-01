from __future__ import annotations

import argparse
import json
import os

import sqlalchemy as sa

from config import initialize_environment
from services.deployment_readiness import (
    EXPECTED_ALEMBIC_HEAD,
    postgres_database_probe,
    run_deployment_preflight,
)
from services.object_store import S3ObjectStore
from services.project_time import postgres_connect_args
from services.oidc_identity import (
    OidcProviderClient,
    OidcProviderSettings,
)
from services.recovery_evidence import (
    RecoveryEvidenceError,
    VerifiedRecoveryEvidence,
    load_verified_recovery_evidence,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the read-only M7 server deployment gates. Output contains "
            "check IDs and safe status text only."
        )
    )
    parser.add_argument(
        "--recovery-evidence",
        help="Path to the signed recovery evidence JSON envelope.",
    )
    parser.add_argument(
        "--release-commit",
        help="Full 40-character release commit bound by the evidence.",
    )
    return parser


def _load_recovery_evidence(
    arguments: argparse.Namespace,
    environment: dict[str, str],
) -> VerifiedRecoveryEvidence | None:
    evidence_path = arguments.recovery_evidence
    release_commit = arguments.release_commit
    trusted_key = environment.get(
        "ARTICLE_AGENT_RECOVERY_EVIDENCE_PUBLIC_KEY",
        "",
    )
    if not evidence_path or not release_commit or not trusted_key:
        return None
    try:
        return load_verified_recovery_evidence(
            evidence_path,
            trusted_public_key_base64=trusted_key,
            expected_release_commit=release_commit,
            expected_alembic_head=EXPECTED_ALEMBIC_HEAD,
        )
    except RecoveryEvidenceError:
        return None


def main() -> int:
    initialize_environment()
    arguments = _parser().parse_args()
    environment = dict(os.environ)
    recovery_evidence = _load_recovery_evidence(arguments, environment)

    def database_probe():
        database_url = environment.get(
            "ARTICLE_AGENT_DATABASE_URL",
            "",
        ).strip()
        if not database_url:
            raise RuntimeError("database readiness check failed")
        engine = sa.create_engine(
            database_url,
            pool_pre_ping=True,
            connect_args=postgres_connect_args(),
        )
        try:
            return postgres_database_probe(engine)
        finally:
            engine.dispose()

    def identity_provider_probe(
        settings: OidcProviderSettings,
    ) -> None:
        provider = OidcProviderClient(settings)
        try:
            provider.check_ready()
        finally:
            provider.close()

    report = run_deployment_preflight(
        environment=environment,
        database_probe=database_probe,
        object_store_factory=S3ObjectStore,
        identity_provider_probe=identity_provider_probe,
        recovery_evidence=recovery_evidence,
    )
    print(
        json.dumps(
            report.public_values(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0 if report.ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
