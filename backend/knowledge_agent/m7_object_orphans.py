from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone
from typing import Mapping, Sequence

from config import initialize_environment
from knowledge_agent.database import create_knowledge_engine
from services.access_control import ActorIdentity
from services.object_orphan_reconciliation import ProjectObjectOrphanReconciler
from services.object_store import S3ObjectStore, S3ObjectStoreSettings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Observe or explicitly clean delayed project object orphans.",
    )
    parser.add_argument("operation", choices=("observe", "cleanup"))
    parser.add_argument("--organization-id", required=True)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument(
        "--confirm-project-id",
        default="",
        help="Required for cleanup and must exactly match --project-id.",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    if environment is None:
        initialize_environment()
        source: Mapping[str, str] = os.environ
    else:
        loaded_environment = dict(environment)
        initialize_environment(loaded_environment)
        source = loaded_environment
    database_url = source.get("ARTICLE_AGENT_DATABASE_URL", "").strip()
    if not database_url:
        raise SystemExit("ARTICLE_AGENT_DATABASE_URL is required")
    settings = S3ObjectStoreSettings.from_environment(source)
    engine = create_knowledge_engine(database_url)
    try:
        reconciler = ProjectObjectOrphanReconciler(
            engine,
            S3ObjectStore(settings),
            bucket=settings.bucket,
        )
        actor = ActorIdentity(args.organization_id, args.user_id)
        now = datetime.now(timezone.utc)
        if args.operation == "observe":
            inventory = reconciler.observe(
                actor,
                args.project_id,
                observed_at=now,
            )
            print(
                " ".join(
                    (
                        f"project={inventory.project_id}",
                        f"scanned={inventory.scanned_object_count}",
                        f"live={inventory.live_object_count}",
                        f"orphans={len(inventory.candidates)}",
                        f"eligible={inventory.eligible_count}",
                    )
                )
            )
            return 0
        if not args.confirm_project_id:
            raise SystemExit("--confirm-project-id is required for cleanup")
        report = reconciler.cleanup(
            actor,
            args.project_id,
            confirm_project_id=args.confirm_project_id,
            observed_at=now,
        )
        print(
            " ".join(
                (
                    f"project={report.project_id}",
                    f"eligible={report.eligible_count}",
                    (
                        "retired_assets="
                        f"{report.retired_registered_asset_count}"
                    ),
                    f"deleted={report.deleted_object_count}",
                    f"delete_failures={report.object_delete_failure_count}",
                )
            )
        )
        return 0 if report.object_delete_failure_count == 0 else 2
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
