from __future__ import annotations

import argparse
import json
import os

import sqlalchemy as sa

from services.deployment_readiness import (
    postgres_database_probe,
    run_deployment_preflight,
)
from services.object_store import S3ObjectStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the read-only M7 server deployment gates. Output contains "
            "check IDs and safe status text only."
        )
    )
    parser.add_argument(
        "--backup-restore-drill-passed",
        action="store_true",
        help=(
            "Attest that the dated database and object restore evidence "
            "required by the M7 runbook has been reviewed."
        ),
    )
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    environment = dict(os.environ)

    def database_probe():
        database_url = environment.get(
            "ARTICLE_AGENT_DATABASE_URL",
            "",
        ).strip()
        if not database_url:
            raise RuntimeError("database readiness check failed")
        engine = sa.create_engine(database_url, pool_pre_ping=True)
        try:
            return postgres_database_probe(engine)
        finally:
            engine.dispose()

    report = run_deployment_preflight(
        environment=environment,
        database_probe=database_probe,
        object_store_factory=S3ObjectStore,
        backup_restore_drill_passed=(
            arguments.backup_restore_drill_passed
        ),
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
