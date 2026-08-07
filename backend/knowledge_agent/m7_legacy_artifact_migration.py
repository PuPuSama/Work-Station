from __future__ import annotations

import argparse
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path

from dotenv import load_dotenv

from knowledge_agent.database import create_knowledge_engine
from services.access_control import ActorIdentity, ProjectAccessDenied
from services.legacy_knowledge_artifact_migration import (
    LegacyKnowledgeArtifactMigrationError,
    LegacyKnowledgeArtifactMigrator,
)
from services.object_store import S3ObjectStore, S3ObjectStoreSettings


ROOT_DIR = Path(__file__).resolve().parents[2]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect or apply the one-time migration from legacy local "
            "Knowledge artifacts to project-scoped object storage."
        )
    )
    parser.add_argument("operation", choices=("inspect", "apply"))
    parser.add_argument("--organization-id", required=True)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument(
        "--confirm-project-id",
        default="",
        help="Required for apply and must exactly match --project-id.",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> int:
    arguments = build_parser().parse_args(argv)
    engine = None
    report = None
    error_code = None
    try:
        if environment is None:
            load_dotenv(ROOT_DIR / ".env")
            load_dotenv(ROOT_DIR / "backend" / ".env")
            source: Mapping[str, str] = os.environ
        else:
            source = environment
        database_url = source.get("ARTICLE_AGENT_DATABASE_URL", "").strip()
        artifact_root = source.get(
            "ARTICLE_AGENT_KNOWLEDGE_ROOT",
            str(ROOT_DIR / "data" / "knowledge-agent"),
        ).strip() or str(ROOT_DIR / "data" / "knowledge-agent")
        if not database_url:
            raise LegacyKnowledgeArtifactMigrationError(
                "legacy_artifact_configuration_invalid"
            )
        settings = S3ObjectStoreSettings.from_environment(source)
        actor = ActorIdentity(
            arguments.organization_id,
            arguments.user_id,
        )
        engine = create_knowledge_engine(database_url)
        migrator = LegacyKnowledgeArtifactMigrator(
            engine,
            S3ObjectStore(settings),
            bucket=settings.bucket,
            local_root=Path(artifact_root),
        )
        if arguments.operation == "inspect":
            report = migrator.inspect(actor, arguments.project_id)
        else:
            if not arguments.confirm_project_id:
                raise ValueError(
                    "--confirm-project-id is required for apply"
                )
            report = migrator.apply(
                actor,
                arguments.project_id,
                confirm_project_id=arguments.confirm_project_id,
            )
    except LegacyKnowledgeArtifactMigrationError as exc:
        error_code = exc.code
    except (ProjectAccessDenied, ValueError):
        error_code = "legacy_artifact_migration_rejected"
    except Exception:
        error_code = "legacy_artifact_migration_unavailable"
    finally:
        if engine is not None:
            try:
                engine.dispose()
            except Exception:
                report = None
                error_code = "legacy_artifact_migration_unavailable"
    if error_code is not None:
        print(
            json.dumps(
                {"ok": False, "error": error_code},
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return 2
    assert report is not None
    print(
        json.dumps(
            {"ok": True, **report.public_values()},
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
