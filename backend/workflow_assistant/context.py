from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from knowledge_agent.schema import knowledge_products, knowledge_sources, projects
from server_schema import project_prompt_defaults, project_topics
from services.access_control import (
    ActorIdentity,
    PostgresProjectAccessRepository,
    ProjectAccessDenied,
    ProjectAccessService,
)
from services.postgres_task_repository import PostgresTaskRepository
from services.project_directory import (
    AccessibleProject,
    PostgresProjectDirectory,
)


class AssistantContextError(RuntimeError):
    """The project context could not be loaded safely."""


@dataclass(frozen=True, slots=True)
class AssistantTaskContext:
    task_id: str
    topic: str
    primary_keyword: str
    competitor_keyword: str
    status: str
    revision: int
    selected_title: str | None
    manual_completed: bool = False
    title_candidate_count: int = 0
    product_candidate_count: int = 0
    confirmed_product_count: int = 0


@dataclass(frozen=True, slots=True)
class AssistantPromptContext:
    kind: str
    prompt_id: str
    version: int


@dataclass(frozen=True, slots=True)
class AssistantKnowledgeContext:
    source_id: str
    display_name: str
    source_kind: str
    trust_tier: str
    snapshot_id: str


@dataclass(frozen=True, slots=True)
class AssistantProductContext:
    product_id: str
    name: str
    canonical_url: str | None
    category_path: tuple[str, ...]
    description: str
    reference_facts: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AssistantPublishedTopicContext:
    """A topic made available by a Server-side published-topic provider.

    M1 reads only PostgreSQL-backed published rows and never falls back to the
    legacy local workbook path. M2 may add controlled import and revision
    flows, but planning remains bound to this project-scoped business table.
    """

    topic_id: str
    topic: str
    primary_keyword: str
    competitor_keyword: str


@dataclass(frozen=True, slots=True)
class AssistantProjectContext:
    project_id: str
    customer_name: str
    official_domain: str
    project_notes: str
    revision: int
    effective_role: str
    tasks: tuple[AssistantTaskContext, ...]
    prompts: tuple[AssistantPromptContext, ...]
    knowledge: tuple[AssistantKnowledgeContext, ...]
    products: tuple[AssistantProductContext, ...] = ()
    published_topics: tuple[AssistantPublishedTopicContext, ...] = ()

    def public_summary(self) -> dict[str, Any]:
        """Return a planner-safe projection with no prompt/body secrets."""

        published_knowledge = [
            {
                "source_id": item.source_id,
                "display_name": item.display_name,
                "source_kind": item.source_kind,
                "trust_tier": item.trust_tier,
                "snapshot_id": item.snapshot_id,
            }
            for item in self.knowledge
        ]
        evidence_knowledge = [
            item
            for item in published_knowledge
            if item["trust_tier"] == "hard_fact"
            and item["source_kind"] != "official_blog"
        ]
        writing_references = [
            item
            for item in published_knowledge
            if item["source_kind"] == "official_blog"
            or item["trust_tier"] == "reference_material"
        ]

        return {
            "project_id": self.project_id,
            "customer_name": self.customer_name,
            "official_domain": self.official_domain,
            "project_notes": self.project_notes[:3000],
            "revision": self.revision,
            "effective_role": self.effective_role,
            "tasks": [
                {
                    "task_id": task.task_id,
                    "topic": task.topic,
                    "primary_keyword": task.primary_keyword,
                    "competitor_keyword": task.competitor_keyword,
                    "status": task.status,
                    "revision": task.revision,
                    "selected_title": task.selected_title,
                    "manual_completed": task.manual_completed,
                    "title_candidate_count": task.title_candidate_count,
                    "product_candidate_count": task.product_candidate_count,
                    "confirmed_product_count": task.confirmed_product_count,
                }
                for task in self.tasks
            ],
            "prompts": [
                {
                    "kind": prompt.kind,
                    "prompt_id": prompt.prompt_id,
                    "version": prompt.version,
                }
                for prompt in self.prompts
            ],
            "published_knowledge": published_knowledge,
            "evidence_knowledge": evidence_knowledge,
            "writing_references": writing_references,
            "confirmed_products": [
                {
                    "product_id": product.product_id,
                    "name": product.name,
                    "canonical_url": product.canonical_url,
                    "category_path": list(product.category_path),
                    "description": product.description,
                    "reference_facts": list(product.reference_facts),
                }
                for product in self.products
            ],
            "published_topics": [
                {
                    "topic_id": topic.topic_id,
                    "topic": topic.topic,
                    "primary_keyword": topic.primary_keyword,
                    "competitor_keyword": topic.competitor_keyword,
                }
                for topic in self.published_topics
            ],
        }


@dataclass(frozen=True, slots=True)
class AssistantWorkspaceContext:
    projects: tuple[AssistantProjectContext, ...]

    @property
    def project_ids(self) -> tuple[str, ...]:
        return tuple(project.project_id for project in self.projects)

    def public_summary(self) -> list[dict[str, Any]]:
        return [project.public_summary() for project in self.projects]


class WorkflowAssistantContextResolver:
    """Resolve only published, project-scoped context for an active Actor."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._access = ProjectAccessService(PostgresProjectAccessRepository(engine))
        self._directory = PostgresProjectDirectory(engine)

    @property
    def access(self) -> ProjectAccessService:
        """The shared SQL-backed access service for typed tool re-authorization."""

        return self._access

    def accessible_projects(self, actor: ActorIdentity) -> tuple[AccessibleProject, ...]:
        try:
            return self._directory.list_for_actor(actor)
        except Exception as exc:
            raise AssistantContextError("project directory is unavailable") from exc

    def authorize_project_scope(
        self,
        *,
        actor: ActorIdentity,
        project_ids: Sequence[str],
        step_project_ids: Sequence[str] = (),
    ) -> None:
        """Re-authorize a persisted plan without loading article context.

        Attention cards only need the durable plan projection. Resolving every
        project's tasks, prompts, knowledge and products made that lightweight
        inbox depend on unrelated project data and turned one damaged context
        row into a 503 for the entire list.
        """

        normalized_projects = tuple(
            dict.fromkeys(item.strip() for item in project_ids if item.strip())
        )
        if not normalized_projects:
            raise AssistantContextError("project scope is empty")
        normalized_steps = {
            item.strip() for item in step_project_ids if item.strip()
        }
        if not normalized_steps.issubset(set(normalized_projects)):
            raise AssistantContextError("project scope contains an inaccessible project")
        accessible = {
            project.project_id
            for project in self.accessible_projects(actor)
        }
        if not set(normalized_projects).issubset(accessible):
            raise AssistantContextError("project scope contains an inaccessible project")
        try:
            for project_id in normalized_projects:
                self._access.require(actor, project_id, "project.view")
        except ProjectAccessDenied as exc:
            raise AssistantContextError("project access denied") from exc

    def resolve(
        self,
        *,
        actor: ActorIdentity,
        project_ids: list[str] | tuple[str, ...] | None = None,
    ) -> AssistantWorkspaceContext:
        accessible = self.accessible_projects(actor)
        accessible_by_id = {project.project_id: project for project in accessible}
        if project_ids is None:
            selected_ids = tuple(accessible_by_id)
        else:
            selected_ids = tuple(dict.fromkeys(item.strip() for item in project_ids if item.strip()))
            missing = [project_id for project_id in selected_ids if project_id not in accessible_by_id]
            if missing:
                raise AssistantContextError("project scope contains an inaccessible project")
        contexts = tuple(
            self._resolve_project(
                actor=actor,
                project=accessible_by_id[project_id],
            )
            for project_id in selected_ids
        )
        return AssistantWorkspaceContext(projects=contexts)

    def _resolve_project(
        self,
        *,
        actor: ActorIdentity,
        project: AccessibleProject,
    ) -> AssistantProjectContext:
        try:
            self._access.require(actor, project.project_id, "project.view")
            with self._engine.connect() as connection:
                metadata = connection.execute(
                    sa.select(
                        projects.c.project_notes,
                        projects.c.revision,
                    ).where(projects.c.project_id == project.project_id)
                ).mappings().one_or_none()
                if metadata is None:
                    raise AssistantContextError("project context not found")
                prompt_rows = connection.execute(
                    sa.select(
                        project_prompt_defaults.c.kind,
                        project_prompt_defaults.c.prompt_id,
                        project_prompt_defaults.c.version,
                    )
                    .where(
                        project_prompt_defaults.c.organization_id == actor.organization_id,
                        project_prompt_defaults.c.project_id == project.project_id,
                    )
                    .order_by(project_prompt_defaults.c.kind)
                ).mappings().all()
                knowledge_rows = connection.execute(
                    sa.select(
                        knowledge_sources.c.source_id,
                        knowledge_sources.c.display_name,
                        knowledge_sources.c.source_kind,
                        knowledge_sources.c.trust_tier,
                        knowledge_sources.c.current_snapshot_id,
                    )
                    .where(
                        knowledge_sources.c.project_id == project.project_id,
                        knowledge_sources.c.status == "published",
                        knowledge_sources.c.current_snapshot_id.is_not(None),
                    )
                    .order_by(knowledge_sources.c.source_id)
                ).mappings().all()
                product_rows = connection.execute(
                    sa.select(
                        knowledge_products.c.product_id,
                        knowledge_products.c.name,
                        knowledge_products.c.canonical_url,
                        knowledge_products.c.category_path,
                        knowledge_products.c.metadata,
                    )
                    .where(
                        knowledge_products.c.project_id == project.project_id,
                        knowledge_products.c.status == "confirmed",
                    )
                    .order_by(knowledge_products.c.product_id)
                ).mappings().all()
                topic_rows = connection.execute(
                    sa.select(
                        project_topics.c.topic_id,
                        project_topics.c.topic,
                        project_topics.c.primary_keyword,
                        project_topics.c.competitor_keyword,
                    )
                    .where(
                        project_topics.c.organization_id == actor.organization_id,
                        project_topics.c.project_id == project.project_id,
                        project_topics.c.status == "published",
                    )
                    .order_by(project_topics.c.topic_id)
                ).mappings().all()
            task_rows = PostgresTaskRepository(
                self._engine,
                organization_id=actor.organization_id,
                project_id=project.project_id,
            ).load_all()
        except ProjectAccessDenied as exc:
            raise AssistantContextError("project access denied") from exc
        except AssistantContextError:
            raise
        except Exception as exc:
            raise AssistantContextError("project context is unavailable") from exc

        tasks = tuple(
            AssistantTaskContext(
                task_id=str(row.get("id") or ""),
                topic=str(row.get("topic") or ""),
                primary_keyword=str(row.get("primary_keyword") or ""),
                competitor_keyword=str(row.get("competitor_keyword") or ""),
                status=str(row.get("status") or ""),
                revision=int(row.get("revision") or 0),
                selected_title=(
                    str(row.get("selected_title"))
                    if row.get("selected_title")
                    else None
                ),
                manual_completed=bool(row.get("manual_completed", False)),
                title_candidate_count=len(row.get("title_candidates") or ()),
                product_candidate_count=len(
                    row.get("product_candidate_ids") or ()
                ),
                confirmed_product_count=len(row.get("products") or ()),
            )
            for row in task_rows
            if str(row.get("id") or "").strip()
        )
        prompts = tuple(
            AssistantPromptContext(
                kind=str(row["kind"]),
                prompt_id=str(row["prompt_id"]),
                version=int(row["version"]),
            )
            for row in prompt_rows
        )
        knowledge = tuple(
            AssistantKnowledgeContext(
                source_id=str(row["source_id"]),
                display_name=str(row["display_name"]),
                source_kind=str(row["source_kind"]),
                trust_tier=str(row["trust_tier"]),
                snapshot_id=str(row["current_snapshot_id"]),
            )
            for row in knowledge_rows
            if row["current_snapshot_id"]
        )
        products = tuple(
            AssistantProductContext(
                product_id=str(row["product_id"]),
                name=str(row["name"]),
                canonical_url=(
                    None
                    if row["canonical_url"] is None
                    else str(row["canonical_url"])
                ),
                category_path=tuple(
                    str(value).strip()
                    for value in (row["category_path"] or ())
                    if str(value).strip()
                ),
                description=str(
                    (
                        row["metadata"]
                        if isinstance(row["metadata"], Mapping)
                        else {}
                    ).get("description")
                    or ""
                )[:3000],
                reference_facts=tuple(
                    str(value).strip()[:500]
                    for value in (
                        (
                            row["metadata"]
                            if isinstance(row["metadata"], Mapping)
                            else {}
                        ).get("reference_facts")
                        or []
                    )
                    if str(value).strip()
                )[:8],
            )
            for row in product_rows
        )
        published_topics = tuple(
            AssistantPublishedTopicContext(
                topic_id=str(row["topic_id"]),
                topic=str(row["topic"]),
                primary_keyword=str(row["primary_keyword"] or ""),
                competitor_keyword=str(row["competitor_keyword"] or ""),
            )
            for row in topic_rows
        )
        return AssistantProjectContext(
            project_id=project.project_id,
            customer_name=project.customer_name,
            official_domain=project.official_domain,
            project_notes=str(metadata["project_notes"] or ""),
            revision=int(metadata["revision"]),
            effective_role=project.effective_role,
            tasks=tasks,
            prompts=prompts,
            knowledge=knowledge,
            products=products,
            published_topics=published_topics,
        )


__all__ = [
    "AssistantContextError",
    "AssistantKnowledgeContext",
    "AssistantProjectContext",
    "AssistantPromptContext",
    "AssistantProductContext",
    "AssistantPublishedTopicContext",
    "AssistantTaskContext",
    "AssistantWorkspaceContext",
    "WorkflowAssistantContextResolver",
]
