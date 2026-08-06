from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

from starlette.routing import Route

from services.candidate_inventory import (
    CandidateInventoryError,
    build_candidate_inventory,
    inventory_json,
    verify_release_checkout,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate a stable M7 candidate inventory artifact."
    )
    parser.add_argument("--release-commit", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=Path.cwd(),
    )
    return parser


def _outside_repository(output: Path, repository_root: Path) -> bool:
    try:
        output.resolve().relative_to(repository_root.resolve())
    except ValueError:
        return True
    return False


def run(arguments: argparse.Namespace) -> dict[str, object]:
    repository_root = arguments.repository_root.resolve()
    output = arguments.output.resolve()
    if not _outside_repository(output, repository_root):
        raise CandidateInventoryError("output_must_be_external")
    if output.exists():
        raise CandidateInventoryError("output_already_exists")
    verify_release_checkout(
        repository_root,
        release_commit=arguments.release_commit,
    )

    from app import app

    routes = [
        route for route in app.routes if isinstance(route, Route)
    ]
    inventory = build_candidate_inventory(
        routes,
        release_commit=arguments.release_commit,
    )
    route_inventory = inventory["route_inventory"]
    operation_inventory = inventory["operation_inventory"]
    if not isinstance(route_inventory, Mapping) or not isinstance(
        operation_inventory,
        Mapping,
    ):
        raise CandidateInventoryError("inventory_projection_invalid")
    counts = route_inventory.get("counts")
    if not isinstance(counts, Mapping) or any(
        not isinstance(value, int) for value in counts.values()
    ):
        raise CandidateInventoryError("inventory_projection_invalid")

    verify_release_checkout(
        repository_root,
        release_commit=arguments.release_commit,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    staging_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{output.name}.",
            suffix=".tmp",
            dir=output.parent,
            delete=False,
        ) as handle:
            staging_path = Path(handle.name)
            handle.write(inventory_json(inventory))
            handle.flush()
            os.fsync(handle.fileno())
        verify_release_checkout(
            repository_root,
            release_commit=arguments.release_commit,
        )
        try:
            os.link(staging_path, output)
        except FileExistsError as exc:
            raise CandidateInventoryError("output_already_exists") from exc
    except CandidateInventoryError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise CandidateInventoryError("artifact_write_failed") from exc
    finally:
        if staging_path is not None:
            staging_path.unlink(missing_ok=True)
    return {
        "operation_count": operation_inventory.get("count"),
        "operation_inventory_digest": operation_inventory.get("sha256"),
        "release_commit": inventory["release_commit"],
        "route_count": sum(counts.values()),
        "route_inventory_digest": route_inventory.get("sha256"),
    }


def main(argv: Sequence[str] | None = None) -> int:
    try:
        public = run(_parser().parse_args(argv))
    except CandidateInventoryError as exc:
        print(
            json.dumps({"error": exc.code}, sort_keys=True),
            file=sys.stderr,
        )
        return 2
    except Exception:
        print(
            json.dumps({"error": "inventory_generation_failed"}, sort_keys=True),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(public, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
