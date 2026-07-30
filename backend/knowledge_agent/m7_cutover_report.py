from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from knowledge_agent.database import create_knowledge_engine
from services.postgres_job_queue import PostgresJobQueue
from services.postgres_task_repository import PostgresTaskRepository
from services.server_cutover_report import (
    ReadOnlySQLiteJobSource,
    ReadOnlySQLiteTaskSource,
    build_server_cutover_report,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compare frozen SQLite Task/Job stores with one PostgreSQL "
            "organization/project scope. No records are written."
        )
    )
    parser.add_argument("--organization-id", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument(
        "--task-database",
        required=True,
        type=Path,
        help="Existing SQLite task_records database, usually tasks.sqlite3.",
    )
    parser.add_argument(
        "--job-database",
        required=True,
        type=Path,
        help="Existing SQLite JobQueue database.",
    )
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    database_url = os.environ.get(
        "ARTICLE_AGENT_DATABASE_URL",
        "",
    ).strip()
    if not database_url:
        raise SystemExit("ARTICLE_AGENT_DATABASE_URL is required")
    engine = create_knowledge_engine(database_url)
    try:
        report = build_server_cutover_report(
            task_source=ReadOnlySQLiteTaskSource(
                arguments.task_database
            ),
            task_target=PostgresTaskRepository(
                engine,
                organization_id=arguments.organization_id,
                project_id=arguments.project_id,
            ),
            job_source=ReadOnlySQLiteJobSource(
                arguments.job_database
            ),
            job_target=PostgresJobQueue(
                engine,
                organization_id=arguments.organization_id,
                project_id=arguments.project_id,
            ),
        )
    finally:
        engine.dispose()
    print(
        json.dumps(
            report.public_values(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0 if report.ready_for_single_write else 2


if __name__ == "__main__":
    raise SystemExit(main())
