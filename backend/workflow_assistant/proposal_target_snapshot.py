from __future__ import annotations

from typing import Any, Mapping

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from knowledge_agent.schema import source_snapshots
from services.access_control import ActorIdentity, ProjectAccessService
from services.server_project_metadata import PostgresServerProjectMetadata
from server_schema import (
    article_tasks,
    project_prompt_heads,
    project_prompt_versions,
    project_topics,
)

from .proposal_preview import (
    ExistingPrompt,
    ExistingTabularItem,
    ProposalTargetSnapshot,
)


MAX_SNAPSHOT_ITEMS = 5_000
MAX_SNAPSHOT_TEXT_BYTES = 2 * 1024 * 1024


class ProposalTargetSnapshotError(RuntimeError):
    """The authorized Project state could not be bounded for review."""


def _text(value: object, *, limit: int = 2_048) -> str:
    normalized = " ".join(str(value or "").split())
    return normalized[:limit]


def _task_item(payload: Mapping[str, Any]) -> ExistingTabularItem | None:
    topic = _text(payload.get("topic"))
    if not topic:
        return None
    return ExistingTabularItem(
        item_id=_text(payload.get("id"), limit=255),
        topic=topic,
        primary_keyword=_text(payload.get("primary_keyword")),
        competitor_keyword=_text(payload.get("competitor_keyword")),
        competitor_blog=_text(payload.get("competitor_blog"), limit=4_096),
    )


class PostgresProposalTargetSnapshotProvider:
    """Read a bounded, freshly authorized Project comparison snapshot."""

    def __init__(
        self,
        engine: Engine,
        *,
        access: ProjectAccessService,
    ) -> None:
        self._engine = engine
        self._access = access
        self._metadata = PostgresServerProjectMetadata(engine)

    def load(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
    ) -> ProposalTargetSnapshot:
        normalized_project_id = str(project_id or "").strip()
        if not normalized_project_id:
            raise ValueError("project_id is required")
        self._access.require(actor, normalized_project_id, "project.view")
        metadata = self._metadata.get(actor=actor, project_id=normalized_project_id)

        with self._engine.connect() as connection:
            prompt_rows = connection.execute(
                sa.select(
                    project_prompt_heads.c.prompt_id,
                    project_prompt_heads.c.kind,
                    project_prompt_heads.c.status,
                    project_prompt_versions.c.version,
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
                    project_prompt_heads.c.organization_id == actor.organization_id,
                    project_prompt_heads.c.project_id == normalized_project_id,
                    project_prompt_heads.c.status.in_(("active", "archived")),
                )
                .order_by(project_prompt_heads.c.prompt_id)
                .limit(MAX_SNAPSHOT_ITEMS + 1)
            ).mappings().all()
            hash_rows = connection.execute(
                sa.select(source_snapshots.c.content_hash)
                .where(source_snapshots.c.project_id == normalized_project_id)
                .distinct()
                .limit(MAX_SNAPSHOT_ITEMS + 1)
            ).scalars().all()
            topic_rows = connection.execute(
                sa.select(
                    project_topics.c.topic_id,
                    project_topics.c.topic,
                    project_topics.c.primary_keyword,
                    project_topics.c.competitor_keyword,
                )
                .where(
                    project_topics.c.organization_id == actor.organization_id,
                    project_topics.c.project_id == normalized_project_id,
                    project_topics.c.status.in_(("published", "archived")),
                )
                .order_by(project_topics.c.topic_id)
                .limit(MAX_SNAPSHOT_ITEMS + 1)
            ).mappings().all()
            task_rows = connection.execute(
                sa.select(
                    article_tasks.c.task_id.label("id"),
                    article_tasks.c.payload["topic"].astext.label("topic"),
                    article_tasks.c.payload["primary_keyword"].astext.label(
                        "primary_keyword"
                    ),
                    article_tasks.c.payload["competitor_keyword"].astext.label(
                        "competitor_keyword"
                    ),
                    article_tasks.c.payload["competitor_blog"].astext.label(
                        "competitor_blog"
                    ),
                )
                .where(
                    article_tasks.c.organization_id == actor.organization_id,
                    article_tasks.c.project_id == normalized_project_id,
                )
                .order_by(article_tasks.c.position, article_tasks.c.task_id)
                .limit(MAX_SNAPSHOT_ITEMS + 1)
            ).mappings().all()
        if any(
            len(values) > MAX_SNAPSHOT_ITEMS
            for values in (prompt_rows, hash_rows, topic_rows, task_rows)
        ):
            raise ProposalTargetSnapshotError("project comparison snapshot is too large")

        prompt_text_bytes = sum(
            len(str(row["name"] or "").encode("utf-8"))
            + len(str(row["content"] or "").encode("utf-8"))
            for row in prompt_rows
        )
        projected_text_bytes = sum(
            len(str(value or "").encode("utf-8"))
            for rows in (topic_rows, task_rows)
            for row in rows
            for value in row.values()
        )
        if prompt_text_bytes + projected_text_bytes > MAX_SNAPSHOT_TEXT_BYTES:
            raise ProposalTargetSnapshotError("project comparison text is too large")

        prompts = tuple(
            ExistingPrompt(
                prompt_id=str(row["prompt_id"]),
                name=str(row["name"]),
                kind=str(row["kind"]),  # type: ignore[arg-type]
                content=str(row["content"]),
                version=int(row["version"]),
            )
            for row in prompt_rows
        )

        tasks = tuple(
            item
            for payload in task_rows
            if isinstance(payload, Mapping)
            for item in (_task_item(payload),)
            if item is not None
        )
        topics = tuple(
            ExistingTabularItem(
                item_id=str(row["topic_id"]),
                topic=str(row["topic"]),
                primary_keyword=str(row["primary_keyword"] or ""),
                competitor_keyword=str(row["competitor_keyword"] or ""),
            )
            for row in topic_rows
        )
        return ProposalTargetSnapshot(
            project_id=normalized_project_id,
            knowledge_content_hashes=frozenset(str(value) for value in hash_rows),
            prompts=prompts,
            project_notes=metadata.project_notes,
            project_notes_revision=metadata.revision,
            task_rows=tasks,
            topics=topics,
        )


__all__ = [
    "MAX_SNAPSHOT_ITEMS",
    "MAX_SNAPSHOT_TEXT_BYTES",
    "PostgresProposalTargetSnapshotProvider",
    "ProposalTargetSnapshotError",
]
