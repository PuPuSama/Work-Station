from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from models import ProjectPromptLibrary
from server_schema import (
    project_prompt_defaults,
    project_prompt_heads,
    project_prompt_versions,
)
from services.access_control import (
    ActorIdentity,
    PostgresProjectAccessRepository,
    ProjectAccessDenied,
    decide_project_permission,
)
from services.audit_log import (
    AuditEvent,
    AuditEventWriter,
    PostgresAuditEventWriter,
)
from services.project_prompts import ProjectPromptRepository


class ProjectPromptMigrationConflict(RuntimeError):
    """The PostgreSQL target is non-empty and differs from SQLite."""


class ProjectPromptMigrationUnavailable(RuntimeError):
    """The migration transaction failed without exposing private content."""


@dataclass(frozen=True, slots=True)
class ProjectPromptMigrationSummary:
    prompt_count: int
    active_count: int
    default_count: int
    content_digest: str


@dataclass(frozen=True, slots=True)
class ProjectPromptMigrationReport:
    organization_id: str
    project_id: str
    source: ProjectPromptMigrationSummary
    target_before: ProjectPromptMigrationSummary
    target_after: ProjectPromptMigrationSummary
    imported: bool
    already_matched: bool


def _canonical_state(library: ProjectPromptLibrary) -> dict[str, object]:
    prompts = sorted(
        (
            {
                "prompt_id": item.id.strip(),
                "kind": item.kind,
                "name": " ".join(item.name.split()),
                "content": item.content.replace(
                    "\r\n",
                    "\n",
                ).replace("\r", "\n").strip(),
                "version": int(item.version),
                "active": bool(item.active),
            }
            for item in library.prompts
        ),
        key=lambda item: str(item["prompt_id"]),
    )
    ids = [str(item["prompt_id"]) for item in prompts]
    if any(not prompt_id for prompt_id in ids):
        raise ProjectPromptMigrationConflict(
            "SQLite prompt identity is empty"
        )
    if len(ids) != len(set(ids)):
        raise ProjectPromptMigrationConflict(
            "SQLite prompt identities are not unique"
        )
    if any(
        not item["name"]
        or not item["content"]
        or int(item["version"]) <= 0
        for item in prompts
    ):
        raise ProjectPromptMigrationConflict(
            "SQLite prompt snapshot is invalid"
        )
    by_id = {
        str(item["prompt_id"]): item
        for item in prompts
    }
    defaults: dict[str, dict[str, object]] = {}
    for kind, prompt_id in (
        (
            "outline",
            library.defaults.default_outline_prompt_id.strip(),
        ),
        (
            "article",
            library.defaults.default_article_prompt_id.strip(),
        ),
        (
            "review",
            library.defaults.default_review_prompt_id.strip(),
        ),
    ):
        if not prompt_id:
            continue
        item = by_id.get(prompt_id)
        if (
            item is None
            or item["kind"] != kind
            or not item["active"]
        ):
            raise ProjectPromptMigrationConflict(
                "SQLite prompt default is invalid"
            )
        defaults[kind] = {
            "prompt_id": prompt_id,
            "version": int(item["version"]),
        }
    return {"prompts": prompts, "defaults": defaults}


def _summary(state: dict[str, object]) -> ProjectPromptMigrationSummary:
    prompts = list(state["prompts"])
    defaults = dict(state["defaults"])
    canonical = json.dumps(
        state,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return ProjectPromptMigrationSummary(
        prompt_count=len(prompts),
        active_count=sum(
            1 for item in prompts if bool(item["active"])
        ),
        default_count=len(defaults),
        content_digest=hashlib.sha256(
            canonical.encode("utf-8")
        ).hexdigest(),
    )


def _target_state(
    connection,
    *,
    organization_id: str,
    project_id: str,
) -> dict[str, object]:
    rows = connection.execute(
        sa.select(
            project_prompt_heads.c.prompt_id,
            project_prompt_heads.c.kind,
            project_prompt_heads.c.status,
            project_prompt_heads.c.current_version,
            project_prompt_versions.c.name,
            project_prompt_versions.c.content,
        )
        .select_from(
            project_prompt_heads.join(
                project_prompt_versions,
                sa.and_(
                    project_prompt_versions.c.organization_id
                    == project_prompt_heads.c.organization_id,
                    project_prompt_versions.c.project_id
                    == project_prompt_heads.c.project_id,
                    project_prompt_versions.c.prompt_id
                    == project_prompt_heads.c.prompt_id,
                    project_prompt_versions.c.kind
                    == project_prompt_heads.c.kind,
                    project_prompt_versions.c.version
                    == project_prompt_heads.c.current_version,
                ),
            )
        )
        .where(
            project_prompt_heads.c.organization_id == organization_id,
            project_prompt_heads.c.project_id == project_id,
        )
    ).mappings().all()
    defaults = connection.execute(
        sa.select(
            project_prompt_defaults.c.kind,
            project_prompt_defaults.c.prompt_id,
            project_prompt_defaults.c.version,
        ).where(
            project_prompt_defaults.c.organization_id
            == organization_id,
            project_prompt_defaults.c.project_id == project_id,
        )
    ).mappings().all()
    return {
        "prompts": sorted(
            (
                {
                    "prompt_id": str(row["prompt_id"]),
                    "kind": str(row["kind"]),
                    "name": str(row["name"]),
                    "content": str(row["content"]),
                    "version": int(row["current_version"]),
                    "active": row["status"] == "active",
                }
                for row in rows
            ),
            key=lambda item: item["prompt_id"],
        ),
        "defaults": {
            str(row["kind"]): {
                "prompt_id": str(row["prompt_id"]),
                "version": int(row["version"]),
            }
            for row in defaults
        },
    }


def migrate_project_prompts(
    source: ProjectPromptRepository,
    *,
    customer: str,
    engine: Engine,
    actor: ActorIdentity,
    project_id: str,
    audit: AuditEventWriter | None = None,
    dry_run: bool = False,
) -> ProjectPromptMigrationReport:
    """Import one exact current SQLite snapshot without fabricating history."""

    organization_id = actor.organization_id.strip()
    normalized_project_id = project_id.strip()
    source_state = _canonical_state(source.list(customer))
    source_summary = _summary(source_state)
    access = PostgresProjectAccessRepository(engine)
    audit_writer = audit or PostgresAuditEventWriter()
    try:
        with engine.begin() as connection:
            facts = access.lock_project_access_in_connection(
                connection,
                actor,
                normalized_project_id,
            )
            if not decide_project_permission(
                facts,
                "article.edit",
            ).allowed:
                raise ProjectAccessDenied("project access denied")
            target_before_state = _target_state(
                connection,
                organization_id=organization_id,
                project_id=normalized_project_id,
            )
            target_before = _summary(target_before_state)
            if target_before.prompt_count or target_before.default_count:
                if target_before != source_summary:
                    raise ProjectPromptMigrationConflict(
                        "PostgreSQL prompt target differs from SQLite"
                    )
                return ProjectPromptMigrationReport(
                    organization_id=organization_id,
                    project_id=normalized_project_id,
                    source=source_summary,
                    target_before=target_before,
                    target_after=target_before,
                    imported=False,
                    already_matched=True,
                )
            if dry_run:
                return ProjectPromptMigrationReport(
                    organization_id=organization_id,
                    project_id=normalized_project_id,
                    source=source_summary,
                    target_before=target_before,
                    target_after=target_before,
                    imported=False,
                    already_matched=False,
                )
            for item in source_state["prompts"]:
                connection.execute(
                    project_prompt_heads.insert().values(
                        organization_id=organization_id,
                        project_id=normalized_project_id,
                        prompt_id=item["prompt_id"],
                        kind=item["kind"],
                        status=(
                            "active"
                            if item["active"]
                            else "archived"
                        ),
                        current_version=item["version"],
                    )
                )
                connection.execute(
                    project_prompt_versions.insert().values(
                        organization_id=organization_id,
                        project_id=normalized_project_id,
                        prompt_id=item["prompt_id"],
                        kind=item["kind"],
                        version=item["version"],
                        name=item["name"],
                        content=item["content"],
                        content_hash=hashlib.sha256(
                            str(item["content"]).encode("utf-8")
                        ).hexdigest(),
                        created_by_user_id=actor.user_id,
                    )
                )
            for kind, pointer in source_state["defaults"].items():
                connection.execute(
                    project_prompt_defaults.insert().values(
                        organization_id=organization_id,
                        project_id=normalized_project_id,
                        kind=kind,
                        prompt_id=pointer["prompt_id"],
                        version=pointer["version"],
                    )
                )
            audit_writer.append(
                connection,
                AuditEvent(
                    organization_id=organization_id,
                    event_id=f"prompt-import-{uuid.uuid4().hex}",
                    actor_user_id=actor.user_id,
                    project_id=normalized_project_id,
                    action="project_prompt.imported",
                    target_type="project_prompt_library",
                    target_id=normalized_project_id,
                    details={
                        "active_count": source_summary.active_count,
                        "default_count": source_summary.default_count,
                        "prompt_count": source_summary.prompt_count,
                    },
                ),
            )
            target_after = _summary(
                _target_state(
                    connection,
                    organization_id=organization_id,
                    project_id=normalized_project_id,
                )
            )
            if target_after != source_summary:
                raise ProjectPromptMigrationConflict(
                    "PostgreSQL prompt verification differs from SQLite"
                )
            return ProjectPromptMigrationReport(
                organization_id=organization_id,
                project_id=normalized_project_id,
                source=source_summary,
                target_before=target_before,
                target_after=target_after,
                imported=True,
                already_matched=False,
            )
    except (ProjectAccessDenied, ProjectPromptMigrationConflict):
        raise
    except (SQLAlchemyError, RuntimeError) as exc:
        raise ProjectPromptMigrationUnavailable(
            "project prompt migration is temporarily unavailable"
        ) from exc


__all__ = [
    "ProjectPromptMigrationConflict",
    "ProjectPromptMigrationReport",
    "ProjectPromptMigrationSummary",
    "ProjectPromptMigrationUnavailable",
    "migrate_project_prompts",
]
