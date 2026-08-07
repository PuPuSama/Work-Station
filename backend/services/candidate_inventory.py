from __future__ import annotations

"""Stable, release-commit-bound M7 route and operation inventories."""

import hashlib
import json
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from starlette.routing import Route

from services.authorized_job_queue import worker_permission_for
from services.server_job_control import (
    SERVER_JOB_CONTROL_OPERATIONS,
    SERVER_JOB_DOMAIN_CONTROL_BLOCKED,
)
from services.server_request_security import (
    knowledge_permission_for,
    server_http_route_available,
    server_knowledge_route_ready,
)


INVENTORY_SCHEMA_VERSION = 1
_RELEASE_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_ROUTE_PARAMETER = re.compile(r"\{([^}:]+)(?::[^}]+)?\}")

_REPRESENTATIVE_ROUTE_VALUES = {
    "article_id": "article-inventory",
    "asset_id": "asset-inventory",
    "batch_id": "batch-inventory",
    "change_id": "change-inventory",
    "conversation_id": "conversation-inventory",
    "customer": "inventory.example",
    "evidence_pack_id": "evidence-pack-inventory",
    "invitation_id": "invitation-inventory",
    "job_id": "job-inventory",
    "kind": "outline",
    "mapping_id": "mapping-inventory",
    "organization_id": "organization-inventory",
    "product_id": "product-inventory",
    "project": "inventory.example",
    "prompt_id": "prompt-inventory",
    "retrieval_plan_id": "plan-inventory",
    "review_id": "review-inventory",
    "scope_id": "scope-inventory",
    "snapshot_id": "snapshot-inventory",
    "source_id": "source-inventory",
    "stage": "initial",
    "task_id": "task-inventory",
    "team_id": "team-inventory",
    "thread_id": "thread-inventory",
    "user_id": "user-inventory",
}

_OPERATION_COMMIT_BOUNDARIES = {
    "article": "postgres_task_cas_and_audit",
    "rewrite_article": "postgres_task_cas_and_audit",
    "humanize": "postgres_task_cas_and_audit",
    "knowledge_research": "postgres_research_checkpoint_and_publication",
    "outline": "postgres_task_cas_and_audit",
    "products": "postgres_task_cas_and_audit",
    "product_rediscovery": "postgres_knowledge_inbox_and_object_store",
    "restore_links": "postgres_task_cas_and_audit",
    "seo_review": "postgres_task_cas_and_audit",
    "titles": "postgres_task_cas_and_audit",
}

_OPERATION_AUDIT_ACTIONS = {
    "article": (
        "article.article_generation.queued",
        "article.draft.generated",
    ),
    "rewrite_article": (
        "article.article_regeneration.queued",
        "article.draft.regenerated",
    ),
    "humanize": (
        "article.humanize.queued",
        "article.humanized.generated",
    ),
    "knowledge_research": (
        "knowledge.retrieval_plan.created",
        "knowledge.research.queued",
        "knowledge.web_snapshot.ingested",
        "knowledge.web_snapshot.reconciled",
    ),
    "outline": (
        "article.outline_generation.queued",
        "article.outline.updated",
    ),
    "products": (
        "article.product_generation.queued",
        "article.products.generated",
    ),
    "product_rediscovery": (
        "knowledge.products.rediscovery.queued",
        "knowledge.web_snapshot.ingested",
        "knowledge.web_snapshot.reconciled",
    ),
    "restore_links": (
        "article.link_restoration.queued",
        "article.links.restored",
    ),
    "seo_review": (
        "article.seo_review.queued",
        "article.seo_review.generated",
    ),
    "titles": (
        "article.title_generation.queued",
        "article.titles.generated",
    ),
}


class CandidateInventoryError(RuntimeError):
    """A candidate inventory could not be produced without ambiguity."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def validate_release_commit(value: str) -> str:
    if value != value.strip() or not _RELEASE_COMMIT.fullmatch(value):
        raise CandidateInventoryError("invalid_release_commit")
    return value


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def representative_route_path(path: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        try:
            return _REPRESENTATIVE_ROUTE_VALUES[name]
        except KeyError as exc:
            raise CandidateInventoryError(
                "route_parameter_unsupported"
            ) from exc

    return _ROUTE_PARAMETER.sub(replace, path)


def _intentionally_unsupported(path: str) -> bool:
    if path == "/api/batches" or path.startswith("/api/batches/"):
        return True
    if path.startswith("/api/batch-jobs/"):
        return True
    return path in {
        "/api/projects/{customer}",
        "/api/projects/{customer}/brand",
        "/api/projects/{customer}/context",
        "/api/projects/{customer}/domain",
    }


def _route_state(method: str, path: str) -> tuple[str, str]:
    concrete_path = representative_route_path(path)
    available = server_http_route_available(method, concrete_path)
    gate = "server_http"
    if path.startswith("/api/knowledge/"):
        available = available and server_knowledge_route_ready(
            method,
            concrete_path,
        )
        gate = "server_http_and_knowledge"
    if available:
        return "server_ready", gate
    if _intentionally_unsupported(path):
        return "intentionally_unsupported", gate
    return "local_only_fail_closed", gate


_PROJECT_MEMBERS_MANAGE_ROUTES = frozenset(
    {
        "grant_project_membership",
        "list_project_membership_candidates",
        "list_project_memberships",
        "revoke_project_membership",
        "update_project_metadata",
    }
)
_PROJECT_VIEW_ROUTES = frozenset(
    {
        "create_project_asset_download",
        "create_project_task_final_ai_screenshot_download",
        "create_project_task_initial_ai_screenshot_download",
        "get_project_metadata",
        "list_project_batches",
        "list_project_tasks",
        "list_server_project_prompts",
        "read_project_batch",
        "read_project_catalog",
        "read_project_product_rediscovery_job",
        "read_project_task",
        "read_project_task_article_generation_job",
        "read_project_task_article_rewrite_job",
        "read_project_task_humanize_job",
        "read_project_task_link_restoration_job",
        "read_project_task_outline_generation_job",
        "read_project_task_product_generation_job",
        "read_project_task_seo_review_job",
        "read_project_task_title_generation_job",
    }
)
_ARTICLE_EDIT_ROUTES = frozenset(
    {
        "create_project_task",
        "create_server_project_prompt",
        "enqueue_project_task_article_generation",
        "enqueue_project_task_article_rewrite",
        "enqueue_project_task_humanize",
        "enqueue_project_task_link_restoration",
        "enqueue_project_task_outline_generation",
        "enqueue_project_task_product_generation",
        "enqueue_project_task_title_generation",
        "import_project_tasks",
        "preview_project_task_writing_settings",
        "replace_project_task_products",
        "restore_project_task_outline_version",
        "rewrite_project_task_article_section",
        "rewrite_project_task_from_scratch",
        "save_project_task_humanized_article",
        "select_project_task_title",
        "set_server_project_prompt_active",
        "set_server_project_prompt_default",
        "update_project_task_outline",
        "update_project_task_writing_settings",
        "update_server_project_prompt",
    }
)
_ARTICLE_REVIEW_ROUTES = frozenset(
    {
        "apply_project_task_seo_review",
        "complete_project_task_seo_review",
        "confirm_project_task_final_ai",
        "confirm_project_task_initial_ai",
        "enqueue_project_task_seo_review",
        "preview_project_task_seo_review",
        "update_project_task_seo_review_change",
        "update_project_task_seo_review_settings",
        "upload_project_task_final_ai_screenshot",
        "upload_project_task_initial_ai_screenshot",
    }
)
_ARTICLE_DELIVER_ROUTES = frozenset(
    {
        "create_project_task_delivery_download",
        "create_project_task_docx_download",
        "create_project_task_tdk_download",
        "export_project_task_docx",
        "generate_project_task_tdk",
        "package_project_task_delivery",
        "prepare_project_task_images",
    }
)
_DYNAMIC_JOB_CONTROL_ROUTES = frozenset(
    {
        "cancel_project_batch",
        "cancel_project_job",
        "retry_project_job",
    }
)
_WORKER_ENQUEUE_ROUTES = frozenset(
    {
        "enqueue_project_product_rediscovery",
        "enqueue_project_task_article_generation",
        "enqueue_project_task_article_rewrite",
        "enqueue_project_task_humanize",
        "enqueue_project_task_link_restoration",
        "enqueue_project_task_outline_generation",
        "enqueue_project_task_product_generation",
        "enqueue_project_task_seo_review",
        "enqueue_project_task_title_generation",
    }
)


def _route_scope(path: str, state: str) -> str:
    if state != "server_ready":
        return "not_applicable_fail_closed"
    if path.startswith("/api/knowledge/") or path.startswith("/api/projects/"):
        return "project"
    if path.startswith("/api/organizations/"):
        return "organization"
    if path.startswith("/api/auth/"):
        return "actor_session"
    return "global"


def _route_permission(
    method: str,
    path: str,
    name: str,
    state: str,
) -> str:
    if state != "server_ready":
        return "not_applicable_fail_closed"
    concrete_path = representative_route_path(path)
    if path == "/api/health" or path.startswith("/api/auth/"):
        return "public_or_session_boundary"
    if path == "/api/projects":
        return "authenticated_actor"
    if path.startswith("/api/knowledge/"):
        return knowledge_permission_for(method, concrete_path)
    if path.startswith("/api/organizations/"):
        return "organization.admin"
    if name in _PROJECT_MEMBERS_MANAGE_ROUTES:
        return "project.members.manage"
    if name in _DYNAMIC_JOB_CONTROL_ROUTES:
        return "operation_worker_permission"
    if name in _ARTICLE_REVIEW_ROUTES:
        return "article.review"
    if name in _ARTICLE_DELIVER_ROUTES:
        return "article.deliver"
    if name == "enqueue_project_product_rediscovery":
        return "knowledge.edit"
    if name in _PROJECT_VIEW_ROUTES:
        return "project.view"
    if name in _ARTICLE_EDIT_ROUTES:
        return "article.edit"
    if path.startswith("/api/projects/"):
        raise CandidateInventoryError("route_metadata_incomplete")
    raise CandidateInventoryError("route_metadata_incomplete")


def _route_storage(path: str, name: str, state: str) -> str:
    if state != "server_ready":
        return "not_applicable_fail_closed"
    if path == "/api/health":
        return "process"
    if path.startswith("/api/auth/"):
        return "postgresql_and_oidc"
    if any(
        marker in path
        for marker in (
            "/assets/",
            "/download",
            "/screenshot",
            "/sources/upload",
        )
    ) or name in {
        "enqueue_project_product_rediscovery",
        "package_project_task_delivery",
        "prepare_project_task_images",
    }:
        return "postgresql_and_scoped_object_store"
    return "postgresql"


def _route_reauthorization(
    method: str,
    path: str,
    name: str,
    state: str,
) -> str:
    if state != "server_ready":
        return "fail_closed_before_handler"
    if path == "/api/health" or path.startswith("/api/auth/"):
        return "public_or_session_boundary"
    if name in _WORKER_ENQUEUE_ROUTES:
        return "request_enqueue_claim_handler_commit"
    if name in _DYNAMIC_JOB_CONTROL_ROUTES:
        return "request_and_queue_mutation"
    if "/download" in path or "/assets/" in path:
        return "request_and_object_access"
    if method in {"POST", "PUT", "PATCH", "DELETE"}:
        return "request_and_commit"
    return "request"


def _route_evidence_id(method: str, path: str) -> str:
    identity = f"{method}\n{path}".encode("utf-8")
    return "route_" + hashlib.sha256(identity).hexdigest()[:24]


def build_route_inventory(
    routes: Sequence[Route],
    *,
    release_commit: str,
) -> dict[str, object]:
    commit = validate_release_commit(release_commit)
    entries: list[dict[str, str]] = []
    identities: set[tuple[str, str]] = set()
    for route in routes:
        if not route.methods:
            raise CandidateInventoryError("route_methods_missing")
        for method in sorted(route.methods):
            if method == "HEAD":
                continue
            identity = (method, route.path)
            if identity in identities:
                raise CandidateInventoryError("duplicate_route_identity")
            identities.add(identity)
            name = str(route.name or "")
            if not name:
                raise CandidateInventoryError("route_name_missing")
            state, gate = _route_state(method, route.path)
            entries.append(
                {
                    "evidence_id": _route_evidence_id(method, route.path),
                    "gate": gate,
                    "method": method,
                    "name": name,
                    "path": route.path,
                    "permission": _route_permission(
                        method,
                        route.path,
                        name,
                        state,
                    ),
                    "reauthorization": _route_reauthorization(
                        method,
                        route.path,
                        name,
                        state,
                    ),
                    "scope": _route_scope(route.path, state),
                    "state": state,
                    "storage": _route_storage(route.path, name, state),
                }
            )
    if not entries:
        raise CandidateInventoryError("route_inventory_empty")
    entries.sort(key=lambda entry: (entry["path"], entry["method"]))
    counts = {
        state: sum(entry["state"] == state for entry in entries)
        for state in (
            "server_ready",
            "local_only_fail_closed",
            "intentionally_unsupported",
        )
    }
    digest_input = {
        "entries": entries,
        "release_commit": commit,
        "schema_version": INVENTORY_SCHEMA_VERSION,
    }
    return {
        "counts": counts,
        "entries": entries,
        "sha256": _digest(digest_input),
    }


def build_operation_inventory(*, release_commit: str) -> dict[str, object]:
    commit = validate_release_commit(release_commit)
    expected = set(SERVER_JOB_CONTROL_OPERATIONS)
    if (
        set(_OPERATION_COMMIT_BOUNDARIES) != expected
        or set(_OPERATION_AUDIT_ACTIONS) != expected
        or not set(SERVER_JOB_DOMAIN_CONTROL_BLOCKED).issubset(expected)
    ):
        raise CandidateInventoryError("operation_inventory_incomplete")
    entries: list[dict[str, object]] = []
    for operation in sorted(expected):
        permission = worker_permission_for(operation)
        domain_controlled = operation in SERVER_JOB_DOMAIN_CONTROL_BLOCKED
        entries.append(
            {
                "audit_actions": list(_OPERATION_AUDIT_ACTIONS[operation]),
                "cancel": (
                    "domain_controlled_only"
                    if domain_controlled
                    else "project_job_control"
                ),
                "claim_authorization": permission,
                "commit_boundary": _OPERATION_COMMIT_BOUNDARIES[operation],
                "drain": "bounded_stop_report",
                "enqueue_authorization": permission,
                "enqueue_transaction": "job_batch_audit_atomic",
                "handler_authorization": permission,
                "operation": operation,
                "queue_store": "postgresql",
                "retry": (
                    "domain_controlled_only"
                    if domain_controlled
                    else "project_job_control"
                ),
                "state": "server_ready",
            }
        )
    digest_input = {
        "entries": entries,
        "release_commit": commit,
        "schema_version": INVENTORY_SCHEMA_VERSION,
    }
    return {
        "count": len(entries),
        "entries": entries,
        "sha256": _digest(digest_input),
    }


def build_candidate_inventory(
    routes: Sequence[Route],
    *,
    release_commit: str,
) -> dict[str, object]:
    commit = validate_release_commit(release_commit)
    return {
        "operation_inventory": build_operation_inventory(
            release_commit=commit
        ),
        "release_commit": commit,
        "route_inventory": build_route_inventory(
            routes,
            release_commit=commit,
        ),
        "schema_version": INVENTORY_SCHEMA_VERSION,
    }


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


def verify_release_checkout(
    repository_root: Path,
    *,
    release_commit: str,
    runner: CommandRunner = subprocess.run,
) -> None:
    commit = validate_release_commit(release_commit)
    root = repository_root.resolve()
    safe_directory = f"safe.directory={root.as_posix()}"
    try:
        top_level = runner(
            [
                "git",
                "-c",
                safe_directory,
                "rev-parse",
                "--show-toplevel",
            ],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        head = runner(
            [
                "git",
                "-c",
                safe_directory,
                "rev-parse",
                "HEAD",
            ],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        status = runner(
            [
                "git",
                "-c",
                safe_directory,
                "status",
                "--porcelain",
                "--untracked-files=normal",
            ],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise CandidateInventoryError("repository_check_failed") from exc
    if Path(top_level).resolve() != root:
        raise CandidateInventoryError("repository_root_mismatch")
    if head != commit:
        raise CandidateInventoryError("release_commit_mismatch")
    if status:
        raise CandidateInventoryError("release_checkout_not_clean")


def inventory_json(inventory: Mapping[str, Any]) -> str:
    """Return deterministic artifact bytes with one trailing newline."""

    return json.dumps(
        dict(inventory),
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    ) + "\n"


__all__ = [
    "CandidateInventoryError",
    "INVENTORY_SCHEMA_VERSION",
    "build_candidate_inventory",
    "build_operation_inventory",
    "build_route_inventory",
    "inventory_json",
    "representative_route_path",
    "validate_release_commit",
    "verify_release_checkout",
]
