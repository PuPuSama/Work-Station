from __future__ import annotations

"""Typed bridges from Workflow Assistant actions to existing Server services.

The Assistant owns planning and durable orchestration.  This module is the
only place where an Assistant write step is translated into an existing
project-scoped Task command or Server Job request.  It intentionally returns
public projections only; prompt bodies, queue requests, article content, and
raw provider errors never become Assistant step output.
"""

import hashlib
import uuid
from collections.abc import Callable, Mapping
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from config import AppConfig
from knowledge_agent.research_runs import (
    PostgresResearchRunRepository,
    ResearchGraphRun,
)
from models import (
    STATUS_DRAFT_READY,
    STATUS_HUMANIZED_READY,
    STATUS_INITIAL_AI_CHECKED,
    STATUS_FINAL_AI_CHECKED,
    TaskRecord,
)
from server_schema import background_jobs
from services.access_control import ActorIdentity
from services.server_article_generation import (
    ARTICLE_GENERATION_OPERATION,
    ARTICLE_REWRITE_OPERATION,
)
from services.server_article_images import ServerArticleImagePreparation
from services.server_delivery_package import ServerDeliveryPackage
from services.server_docx_export import ServerArticleDocxExport
from services.server_knowledge_research import KNOWLEDGE_RESEARCH_OPERATION
from services.server_project_tasks import (
    ServerProjectTaskRuntime,
    ServerProjectTaskStoreFactory,
)
from services.server_request_security import AuthorizedProjectRequest
from services.server_outline_update import apply_reviewed_outline
from services.server_seo_review_commands import (
    apply_server_seo_review,
    build_server_seo_review_preview,
    complete_server_seo_review,
    update_server_seo_review_change,
)
from services.server_task_intake import ServerTaskIntakeRow
from services.server_tdk_export import ServerTdkDocxExport
from storage import content_hash
from workflow.state_machine import (
    ACTION_EXPORT_DOCX,
    ACTION_GENERATE_TDK,
    ACTION_PACKAGE_DELIVERY,
    ACTION_PREPARE_IMAGES,
    ACTION_SELECT_TITLE,
    ACTION_UPDATE_OUTLINE,
    ACTION_UPDATE_PRODUCTS,
    ensure_action_allowed,
    invalidate_downstream,
    transition_task,
)

from .context import WorkflowAssistantContextResolver
from .contracts import ActionKind
from .tools import (
    WorkflowToolError,
    WorkflowToolHandler,
    WorkflowToolHumanGateRequired,
    WorkflowToolInvocation,
    WorkflowToolUnavailable,
)


class WorkflowAssistantServiceAdapters:
    """Expose the closed M1 action set over the existing Server services."""

    _QUEUE_ACTIONS: dict[str, tuple[str, str]] = {
        "generate_titles": ("title_generation", "titles"),
        "generate_products": ("product_generation", "products"),
        "generate_outline": ("outline_generation", "outline"),
        "generate_article": ("article_generation", ARTICLE_GENERATION_OPERATION),
        "humanize": ("humanize_generation", "humanize"),
        "review": ("seo_review_generation", "seo_review"),
        "restore_links": ("link_restoration", "restore_links"),
    }

    def __init__(
        self,
        *,
        engine: Engine,
        config: AppConfig,
        task_factory: ServerProjectTaskStoreFactory | None,
        context: WorkflowAssistantContextResolver | None = None,
        plan_status: Callable[[str, ActorIdentity], Mapping[str, Any]] | None = None,
        evidence_chat: Any = None,
        product_selection: Any = None,
        project_catalog: Any = None,
        title_generation: Any = None,
        product_generation: Any = None,
        outline_generation: Any = None,
        article_generation: Any = None,
        humanize_generation: Any = None,
        link_restoration: Any = None,
        seo_review_generation: Any = None,
        knowledge_research: Any = None,
        object_service: Any = None,
    ) -> None:
        self._engine = engine
        self._research_runs = PostgresResearchRunRepository(engine)
        self._config = config
        self._task_factory = task_factory
        self._context = context
        self._plan_status = plan_status
        self._evidence_chat = evidence_chat
        self._product_selection = product_selection
        self._project_catalog = project_catalog
        self._services: dict[str, Any] = {
            "title_generation": title_generation,
            "product_generation": product_generation,
            "outline_generation": outline_generation,
            "article_generation": article_generation,
            "humanize_generation": humanize_generation,
            "link_restoration": link_restoration,
            "seo_review_generation": seo_review_generation,
            "knowledge_research": knowledge_research,
        }
        self._object_service = object_service

    def handlers(self) -> dict[ActionKind, WorkflowToolHandler]:
        """Return the complete closed registry used by the coordinator."""

        handlers: dict[ActionKind, WorkflowToolHandler] = {
            "create_task": self._create_task,
            "generate_titles": self._queue_generation,
            "select_title": self._select_title,
            "generate_products": self._queue_generation,
            "confirm_products": self._confirm_products,
            "generate_outline": self._queue_generation,
            "start_research": self._start_research,
            "generate_article": self._queue_generation,
            "humanize": self._queue_generation,
            "review": self._queue_generation,
            "restore_links": self._queue_generation,
            "prepare_images": self._prepare_images,
            "export_docx": self._export_docx,
            "generate_tdk": self._generate_tdk,
            "package_delivery": self._package_delivery,
        }
        handlers.update(self._read_handlers())
        return handlers

    def job_status(
        self,
        actor: ActorIdentity,
        step: Any,
    ) -> Mapping[str, Any]:
        """Read a safe projection of the existing PostgreSQL Job row."""

        job_id = str(step.background_job_id or "").strip()
        task_id = str(step.article_task_id or "").strip()
        if not job_id or not task_id:
            raise WorkflowToolError("background job identity is missing")
        operation = self._operation_for_step(step)
        with self._engine.connect() as connection:
            row = connection.execute(
                sa.select(
                    background_jobs.c.job_id,
                    background_jobs.c.batch_id,
                    background_jobs.c.task_id,
                    background_jobs.c.operation,
                    background_jobs.c.status,
                    background_jobs.c.source_revision,
                    background_jobs.c.result_revision,
                    background_jobs.c.attempts,
                    background_jobs.c.created_at,
                    background_jobs.c.started_at,
                    background_jobs.c.finished_at,
                    background_jobs.c.updated_at,
                    background_jobs.c.error,
                    background_jobs.c.request,
                ).where(
                    background_jobs.c.organization_id == actor.organization_id,
                    background_jobs.c.project_id == step.project_id,
                    background_jobs.c.task_id == task_id,
                    background_jobs.c.job_id == job_id,
                    background_jobs.c.operation == operation,
                )
            ).mappings().one_or_none()
        if row is None:
            raise WorkflowToolError("background job was not found")
        public = self._public_job(row)
        if str(step.action_kind) == "start_research":
            public = self._research_job_status(
                actor=actor,
                step=step,
                job=public,
                request=dict(row["request"] or {}),
            )
        if (
            public.get("status") == "succeeded"
            and str(step.action_kind) == "generate_outline"
        ):
            public = self._confirm_generated_outline(
                actor=actor,
                step=step,
                job=public,
            )
        if (
            public.get("status") == "succeeded"
            and str(step.action_kind) == "review"
        ):
            public = self._apply_generated_review(
                actor=actor,
                step=step,
                job=public,
            )
        return public

    def _research_job_status(
        self,
        *,
        actor: ActorIdentity,
        step: Any,
        job: Mapping[str, Any],
        request: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Project the graph Run behind a terminal research Job.

        The queue Job can succeed because the graph durably interrupted at its
        candidate-review node.  That is not a completed Assistant research
        action: expose it as a runtime human gate, then let the coordinator
        persist ``waiting_review``.  Only bounded Run metadata is returned;
        candidate URLs and private queue request bodies stay in the Research
        workspace.
        """

        action = str(request.get("action") or "").strip()
        thread_id = str(request.get("thread_id") or "").strip()
        retrieval_plan_id = str(request.get("retrieval_plan_id") or "").strip()
        task_id = str(step.article_task_id or "").strip()
        if (
            action not in {"start", "resume"}
            or not thread_id
            or not retrieval_plan_id
            or not task_id
        ):
            raise WorkflowToolError("knowledge research Job identity is unavailable")
        try:
            run = self._research_runs.get_run(str(step.project_id), thread_id)
        except Exception as exc:
            raise WorkflowToolError("knowledge research Run is unavailable") from exc
        if (
            run is None
            or run.organization_id != actor.organization_id
            or run.project_id != str(step.project_id)
            or run.retrieval_plan_id != retrieval_plan_id
            or str(run.metadata.get("task_id") or "") != task_id
        ):
            raise WorkflowToolError("knowledge research Run identity is invalid")

        projection = self._public_research_run(run)
        job_status = str(job.get("status") or "").strip()
        # An active Resume Job is allowed to leave the Run at its previous
        # review checkpoint until its worker starts.  The Job remains the
        # durable wait identity during that interval.
        if job_status in {"queued", "running", "retry_wait"}:
            return {**dict(job), **projection}
        if job_status in {"failed", "cancelled", "conflict"}:
            return {**dict(job), **projection}
        if job_status != "succeeded":
            return {**dict(job), **projection}

        if run.status == "waiting_for_review":
            return {
                **dict(job),
                **projection,
                "status": "waiting_review",
                "review_required": True,
            }
        if run.status in {"completed", "completed_with_warnings"}:
            return {
                **dict(job),
                **projection,
                "status": "succeeded",
                "review_required": False,
            }
        if run.status == "failed":
            return {
                **dict(job),
                **projection,
                "status": "failed",
                "has_error": True,
                "research_error_code": str(run.error_code or "research_failed")[:120],
            }
        if run.status == "cancelled":
            return {
                **dict(job),
                **projection,
                "status": "cancelled",
            }
        # The graph state is authoritative after a successful queue Job.  A
        # non-terminal Run means an externally queued Resume is still working;
        # keep waiting without replaying the successful Start Job.
        return {
            **dict(job),
            **projection,
            "status": "running",
            "review_required": False,
        }

    @staticmethod
    def _public_research_run(run: ResearchGraphRun) -> dict[str, Any]:
        return {
            "research_thread_id": run.thread_id,
            "retrieval_plan_id": run.retrieval_plan_id,
            "research_status": run.status,
            "current_node": run.current_node,
            "current_scope_id": run.current_scope_id,
            "evidence_pack_ids": list(run.evidence_pack_ids),
            "warning_count": len(run.warnings),
        }

    def _apply_generated_review(
        self,
        *,
        actor: ActorIdentity,
        step: Any,
        job: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Apply only safe, applicable suggestions delegated by the plan.

        The one-time plan confirmation authorizes ordinary review edits, but
        never a second confirmation for number, URL, brand, or product risks.
        Those suggestions and invalid suggestions are rejected explicitly.
        """

        if self._task_factory is None:
            raise WorkflowToolUnavailable(
                "Server project task storage is unavailable"
            )
        task_id = str(step.article_task_id or "").strip()
        result_revision = job.get("result_revision")
        job_id = str(job.get("job_id") or "").strip()
        if (
            not task_id
            or not job_id
            or isinstance(result_revision, bool)
            or not isinstance(result_revision, int)
        ):
            raise WorkflowToolError("generated review identity is unavailable")
        review_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"seo-review\n{job_id}",
        ).hex[:12]
        try:
            runtime = self._task_factory.create(
                AuthorizedProjectRequest(
                    actor=actor,
                    project_id=str(step.project_id),
                    permission="article.edit",
                )
            )
            task = runtime.store.get(task_id)
        except Exception as exc:
            raise WorkflowToolError("generated review task is unavailable") from exc
        review = next(
            (item for item in task.seo_reviews if item.id == review_id),
            None,
        )
        if review is None:
            raise WorkflowToolError("generated review is unavailable")
        if review.status != "open":
            return {
                **dict(job),
                "result_revision": review.applied_revision or task.revision,
                "review_finalized": True,
            }
        if task.revision != result_revision:
            raise WorkflowToolError("generated review task revision changed")
        accepted = 0
        rejected = 0
        try:
            for change in list(review.changes):
                safe = change.applicable and not change.risks
                update_server_seo_review_change(
                    task,
                    review_id=review_id,
                    change_id=change.id,
                    decision="accepted" if safe else "rejected",
                    reviewed_text=(change.model_proposed_text if safe else ""),
                    confirm_risks=False,
                    actor_user_id=actor.user_id,
                )
                accepted += int(safe)
                rejected += int(not safe)
            if accepted:
                preview = build_server_seo_review_preview(
                    task,
                    review_id=review_id,
                )
                summary = apply_server_seo_review(
                    task,
                    review_id=review_id,
                    preview_hash=preview.article_hash,
                    confirm_pending=False,
                    actor_user_id=actor.user_id,
                )
                audit_action = "article.seo_review.applied"
            else:
                summary = complete_server_seo_review(
                    task,
                    review_id=review_id,
                    confirm_pending=False,
                    actor_user_id=actor.user_id,
                )
                audit_action = "article.seo_review.completed"
            saved = runtime.audited_writer.put(
                task,
                expected_revision=result_revision,
                actor=actor,
                action=audit_action,
                details={
                    "accepted_count": summary.accepted_count,
                    "rejected_count": summary.rejected_count,
                    "invalid_count": summary.invalid_count,
                    "pending_count": summary.pending_count,
                },
            )
        except Exception as exc:
            raise WorkflowToolError(
                "generated review could not be safely finalized"
            ) from exc
        return {
            **dict(job),
            "result_revision": saved.revision,
            "review_finalized": True,
            "accepted_count": accepted,
            "rejected_count": rejected,
        }

    def _confirm_generated_outline(
        self,
        *,
        actor: ActorIdentity,
        step: Any,
        job: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Confirm the generated outline delegated by the approved plan.

        Manual workbench generation intentionally leaves ``outline_draft``
        pending review. Workflow Assistant M1 permits the owner to delegate
        that choice in the confirmed plan, so the outline Job reconciliation
        commits the generated draft with the Job result revision. Repeated
        polling is idempotent and returns the already-confirmed revision.
        """

        if self._task_factory is None:
            raise WorkflowToolUnavailable(
                "Server project task storage is unavailable"
            )
        task_id = str(step.article_task_id or "").strip()
        result_revision = job.get("result_revision")
        if (
            not task_id
            or isinstance(result_revision, bool)
            or not isinstance(result_revision, int)
        ):
            raise WorkflowToolError("generated outline identity is unavailable")
        try:
            runtime = self._task_factory.create(
                AuthorizedProjectRequest(
                    actor=actor,
                    project_id=str(step.project_id),
                    permission="article.edit",
                )
            )
            task = runtime.store.get(task_id)
        except Exception as exc:
            raise WorkflowToolError(
                "generated outline task is unavailable"
            ) from exc
        if task.outline and task.outline == task.outline_draft:
            return {
                **dict(job),
                "result_revision": task.revision,
                "outline_confirmed": True,
            }
        if task.revision != result_revision:
            raise WorkflowToolError("generated outline task revision changed")
        try:
            ensure_action_allowed(task, ACTION_UPDATE_OUTLINE)
            outline = apply_reviewed_outline(
                task,
                outline=task.outline_draft,
                confirmed=True,
            )
            saved = runtime.audited_writer.put(
                task,
                expected_revision=result_revision,
                actor=actor,
                action="article.outline.updated",
                details={
                    "confirmed": True,
                    "outline_characters": len(outline),
                },
            )
        except Exception as exc:
            raise WorkflowToolError(
                "generated outline could not be confirmed"
            ) from exc
        return {
            **dict(job),
            "result_revision": saved.revision,
            "outline_confirmed": True,
        }

    def _read_handlers(self) -> dict[ActionKind, WorkflowToolHandler]:
        if self._context is None:
            return {}

        def resolve(invocation: WorkflowToolInvocation):
            return self._context.resolve(
                actor=invocation.actor,
                project_ids=[invocation.project_id],
            )

        def list_projects(invocation: WorkflowToolInvocation) -> Mapping[str, Any]:
            context = self._context.resolve(actor=invocation.actor)
            return {
                "projects": [
                    {
                        "project_id": project.project_id,
                        "customer_name": project.customer_name,
                        "official_domain": project.official_domain,
                    }
                    for project in context.projects
                ]
            }

        def project_summary(invocation: WorkflowToolInvocation) -> Mapping[str, Any]:
            context = resolve(invocation)
            if not context.projects:
                raise WorkflowToolError("project context is unavailable")
            return context.projects[0].public_summary()

        def list_tasks(invocation: WorkflowToolInvocation) -> Mapping[str, Any]:
            summary = project_summary(invocation)
            return {
                "project_id": invocation.project_id,
                "tasks": summary.get("tasks", []),
            }

        def evidence_query(invocation: WorkflowToolInvocation) -> Mapping[str, Any]:
            question = str(
                invocation.input_summary.get("question")
                or invocation.input_summary.get("query")
                or ""
            ).strip()
            if question and self._evidence_chat is not None:
                try:
                    conversation = self._evidence_chat.ask(
                        project_id=invocation.project_id,
                        question=question,
                        request_id=f"assistant-{invocation.plan_id}-{invocation.step_id}",
                        conversation_id=f"assistant_{invocation.plan_id}",
                        article_id=invocation.article_task_id,
                    )
                except Exception as exc:
                    raise WorkflowToolError("published evidence query failed") from exc
                answer = conversation.messages[-1] if conversation.messages else None
                if answer is None:
                    raise WorkflowToolError("published evidence query returned no answer")
                return {
                    "project_id": invocation.project_id,
                    "question": question,
                    "answer": answer.content,
                    "citation_chunk_ids": [
                        citation.chunk_id for citation in answer.citations
                    ],
                }
            summary = project_summary(invocation)
            return {
                "project_id": invocation.project_id,
                "evidence": summary.get("evidence_knowledge", []),
            }

        def plan_status(invocation: WorkflowToolInvocation) -> Mapping[str, Any]:
            if self._plan_status is None:
                raise WorkflowToolUnavailable("plan status is not wired")
            return dict(self._plan_status(invocation.plan_id, invocation.actor))

        return {
            "list_projects": list_projects,
            "list_tasks": list_tasks,
            "read_project_context": project_summary,
            "evidence_query": evidence_query,
            "read_plan_status": plan_status,
        }

    def _runtime(
        self,
        invocation: WorkflowToolInvocation,
    ) -> ServerProjectTaskRuntime:
        self._validate_pins(invocation)
        if self._task_factory is None:
            raise WorkflowToolUnavailable("Server project task storage is unavailable")
        permission = {
            "review": "article.review",
            "start_research": "knowledge.publish",
            "export_docx": "article.deliver",
            "generate_tdk": "article.deliver",
            "package_delivery": "article.deliver",
        }.get(invocation.action_kind, "article.edit")
        authorized = AuthorizedProjectRequest(
            actor=invocation.actor,
            project_id=invocation.project_id,
            permission=permission,  # type: ignore[arg-type]
        )
        try:
            return self._task_factory.create(authorized)
        except Exception as exc:
            raise WorkflowToolError("Server project task storage is unavailable") from exc

    def _validate_pins(self, invocation: WorkflowToolInvocation) -> None:
        """Fail closed when project prompts or published snapshots drifted."""

        if self._context is None:
            return
        pinned_prompt = invocation.pinned_prompt_version
        pinned_knowledge = invocation.pinned_knowledge_snapshot
        if not pinned_prompt and not pinned_knowledge:
            return
        try:
            workspace = self._context.resolve(
                actor=invocation.actor,
                project_ids=[invocation.project_id],
            )
            project = workspace.projects[0]
            summary = project.public_summary()
            if (
                pinned_prompt.get("project_revision") is not None
                and int(pinned_prompt["project_revision"])
                != int(summary["revision"])
            ):
                raise WorkflowToolError("project context changed; revise the plan")
            if (
                pinned_knowledge.get("project_revision") is not None
                and int(pinned_knowledge["project_revision"])
                != int(summary["revision"])
            ):
                raise WorkflowToolError("knowledge context changed; revise the plan")
            expected_prompts = pinned_prompt.get("prompts")
            if isinstance(expected_prompts, list) and expected_prompts != summary.get("prompts", []):
                raise WorkflowToolError("project prompts changed; revise the plan")
            expected_sources = pinned_knowledge.get("sources")
            current_sources = [
                {
                    "source_id": item["source_id"],
                    "snapshot_id": item["snapshot_id"],
                    "trust_tier": item["trust_tier"],
                }
                for item in summary.get("published_knowledge", [])
            ]
            if isinstance(expected_sources, list) and expected_sources != current_sources:
                raise WorkflowToolError("knowledge snapshots changed; revise the plan")
            expected_products = pinned_knowledge.get("products")
            if isinstance(expected_products, list) and expected_products != summary.get("confirmed_products", []):
                raise WorkflowToolError("confirmed products changed; revise the plan")
        except WorkflowToolError:
            raise
        except Exception as exc:
            raise WorkflowToolError("project context is unavailable") from exc

    @staticmethod
    def _task_id(invocation: WorkflowToolInvocation) -> str:
        task_id = str(invocation.article_task_id or "").strip()
        requested = str(invocation.input_summary.get("task_id") or "").strip()
        if requested and task_id and requested != task_id:
            raise WorkflowToolError("step task identity does not match its input")
        task_id = task_id or requested
        if not task_id:
            raise WorkflowToolError("article task is required")
        return task_id

    def _task(
        self,
        invocation: WorkflowToolInvocation,
    ) -> tuple[ServerProjectTaskRuntime, TaskRecord]:
        runtime = self._runtime(invocation)
        task_id = self._task_id(invocation)
        try:
            task = runtime.store.get(task_id)
        except KeyError as exc:
            raise WorkflowToolError("article task was not found") from exc
        if invocation.expected_task_revision is None:
            raise WorkflowToolError("task revision is required")
        if task.revision != invocation.expected_task_revision:
            raise WorkflowToolError("article task revision changed")
        return runtime, task

    @staticmethod
    def _required_text(summary: Mapping[str, Any], key: str) -> str:
        value = summary.get(key)
        if not isinstance(value, str) or not value.strip():
            raise WorkflowToolError(f"{key} is required")
        return value.strip()

    @staticmethod
    def _optional_text(summary: Mapping[str, Any], key: str) -> str:
        value = summary.get(key, "")
        if value is None:
            return ""
        if not isinstance(value, str):
            raise WorkflowToolError(f"{key} must be text")
        return value.strip()

    @staticmethod
    def _bounded_int(
        summary: Mapping[str, Any],
        key: str,
        *,
        default: int | None = None,
        minimum: int = 0,
        maximum: int = 20,
    ) -> int:
        value = summary.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int):
            raise WorkflowToolError(f"{key} must be an integer")
        if value < minimum or value > maximum:
            raise WorkflowToolError(f"{key} is out of range")
        return value

    @staticmethod
    def _bounded_bool(
        summary: Mapping[str, Any],
        key: str,
        *,
        default: bool,
    ) -> bool:
        value = summary.get(key, default)
        if not isinstance(value, bool):
            raise WorkflowToolError(f"{key} must be boolean")
        return value

    def _create_task(self, invocation: WorkflowToolInvocation) -> Mapping[str, Any]:
        runtime = self._runtime(invocation)
        summary = invocation.input_summary
        # Planning policy binds this identity to a Server-published topic.
        # Requiring it again at the write adapter prevents a hand-built plan
        # or a future adapter caller from turning this into arbitrary Task
        # creation.
        self._required_text(summary, "published_topic_id")
        topic = self._required_text(summary, "topic")
        intake_id = self._optional_text(summary, "intake_id")
        if not intake_id:
            digest = hashlib.sha256(
                f"{invocation.plan_id}\n{invocation.step_id}".encode("utf-8")
            ).hexdigest()[:40]
            intake_id = f"assistant_{digest}"
        try:
            result = runtime.intake.create_manual(
                actor=invocation.actor,
                intake_id=intake_id,
                row=ServerTaskIntakeRow(
                    topic=topic,
                    primary_keyword=self._optional_text(summary, "primary_keyword"),
                    competitor_keyword=self._optional_text(summary, "competitor_keyword"),
                    competitor_blog=self._optional_text(summary, "competitor_blog"),
                ),
            )
        except (ValueError, KeyError) as exc:
            raise WorkflowToolError("article task could not be created") from exc
        return {
            "created": bool(result.created),
            "task_ids": [task.id for task in result.tasks],
            "revisions": [task.revision for task in result.tasks],
        }

    def _queue_generation(self, invocation: WorkflowToolInvocation) -> Mapping[str, Any]:
        action = invocation.action_kind
        self._validate_pins(invocation)
        binding = self._QUEUE_ACTIONS.get(action)
        if binding is None:
            raise WorkflowToolError("unsupported queued action")
        service_name, operation = binding
        service = self._services.get(service_name)
        if service is None:
            raise WorkflowToolUnavailable(f"{service_name} is unavailable")
        task_id = self._task_id(invocation)
        source_revision = invocation.expected_task_revision
        if source_revision is None:
            raise WorkflowToolError("task revision is required")
        if action == "humanize":
            runtime, task = self._task(invocation)
            if task.status == STATUS_DRAFT_READY:
                task.initial_ai_check = task.initial_ai_check.model_copy(
                    update={
                        "confirmed": False,
                        "deferred": True,
                        "confirmed_at": "",
                        "article_hash": content_hash(task.initial_article),
                    }
                )
                transition_task(task, STATUS_INITIAL_AI_CHECKED)
                try:
                    task = runtime.audited_writer.put(
                        task,
                        expected_revision=source_revision,
                        actor=invocation.actor,
                        action="article.initial_ai_check.updated",
                        details={
                            "confirmed": False,
                            "deferred": True,
                            "score_recorded": (
                                task.initial_ai_check.score is not None
                            ),
                        },
                    )
                except Exception as exc:
                    raise WorkflowToolError(
                        "initial AI check could not be deferred"
                    ) from exc
                source_revision = task.revision
        elif action == "restore_links":
            runtime, task = self._task(invocation)
            if task.status == STATUS_HUMANIZED_READY:
                task.final_ai_check = task.final_ai_check.model_copy(
                    update={
                        "confirmed": False,
                        "deferred": True,
                        "confirmed_at": "",
                        "article_hash": content_hash(task.humanized_article),
                    }
                )
                transition_task(task, STATUS_FINAL_AI_CHECKED)
                try:
                    task = runtime.audited_writer.put(
                        task,
                        expected_revision=source_revision,
                        actor=invocation.actor,
                        action="article.final_ai_check.updated",
                        details={
                            "confirmed": False,
                            "deferred": True,
                            "score_recorded": (
                                task.final_ai_check.score is not None
                            ),
                        },
                    )
                except Exception as exc:
                    raise WorkflowToolError(
                        "final AI check could not be deferred"
                    ) from exc
                source_revision = task.revision
        kwargs: dict[str, Any] = {
            "actor": invocation.actor,
            "project_id": invocation.project_id,
            "task_id": task_id,
            "source_revision": source_revision,
        }
        if action == "generate_article":
            requested_operation = str(
                invocation.input_summary.get("operation") or operation
            ).strip()
            if requested_operation not in {
                ARTICLE_GENERATION_OPERATION,
                ARTICLE_REWRITE_OPERATION,
            }:
                raise WorkflowToolError("article operation is invalid")
            kwargs["operation"] = requested_operation
            kwargs["use_evidence_pack"] = self._bounded_bool(
                invocation.input_summary,
                "use_evidence_pack",
                default=True,
            )
        try:
            job = service.enqueue(**kwargs)
        except KeyError as exc:
            raise WorkflowToolError("article task was not found") from exc
        except Exception as exc:
            raise WorkflowToolError("Server Job could not be queued") from exc
        return self._waiting_job(job)

    def _select_title(self, invocation: WorkflowToolInvocation) -> Mapping[str, Any]:
        runtime, task = self._task(invocation)
        ensure_action_allowed(task, ACTION_SELECT_TITLE)
        index = self._bounded_int(
            invocation.input_summary,
            "candidate_index",
            default=0,
            minimum=0,
            maximum=max(0, len(task.title_candidates) - 1),
        )
        if index >= len(task.title_candidates):
            raise WorkflowToolError("title candidate index is out of range")
        selected_title = task.title_candidates[index].strip()
        if not selected_title:
            raise WorkflowToolError("title candidate is unavailable")
        task.selected_title = selected_title
        invalidate_downstream(task, "selected_title")
        try:
            saved = runtime.audited_writer.put(
                task,
                expected_revision=cast(int, invocation.expected_task_revision),
                actor=invocation.actor,
                action="article.title.selected",
                details={
                    "candidate_count": len(task.title_candidates),
                    "candidate_index": index,
                },
            )
        except Exception as exc:
            raise WorkflowToolError("title selection could not be saved") from exc
        return {
            "task_id": saved.id,
            "result_revision": saved.revision,
            "candidate_index": index,
        }

    def _confirm_products(self, invocation: WorkflowToolInvocation) -> Mapping[str, Any]:
        if self._product_selection is None:
            raise WorkflowToolUnavailable("confirmed product selection is unavailable")
        runtime, task = self._task(invocation)
        ensure_action_allowed(task, ACTION_UPDATE_PRODUCTS)
        values = invocation.input_summary.get("product_ids")
        if values is None:
            # An explicit confirm_products step represents the user's
            # delegation in the approved plan. Product generation stores its
            # ranked candidates on the Task, so the runtime can safely choose
            # up to three current project-scoped candidates without asking
            # the planner to predict IDs that do not exist yet.
            values = list(task.product_candidate_ids[:3])
        if not isinstance(values, list) or not values or len(values) > 3:
            raise WorkflowToolError("product_ids must contain one to three ids")
        product_ids = [value.strip() for value in values if isinstance(value, str)]
        if len(product_ids) != len(values) or len(set(product_ids)) != len(product_ids):
            raise WorkflowToolError("product_ids are invalid")
        try:
            products = self._product_selection.select(
                invocation.project_id,
                product_ids,
            )
        except Exception as exc:
            raise WorkflowToolError("confirmed products could not be loaded") from exc
        task.products = list(products)
        invalidate_downstream(task, "products")
        try:
            saved = runtime.audited_writer.put(
                task,
                expected_revision=cast(int, invocation.expected_task_revision),
                actor=invocation.actor,
                action="article.products.confirmed",
                details={"product_count": len(task.products)},
            )
        except Exception as exc:
            raise WorkflowToolError("product selection could not be saved") from exc
        return {
            "task_id": saved.id,
            "result_revision": saved.revision,
            "product_count": len(saved.products),
        }

    def _export_docx(self, invocation: WorkflowToolInvocation) -> Mapping[str, Any]:
        if self._object_service is None:
            raise WorkflowToolUnavailable("Server object storage is unavailable")
        runtime, task = self._task(invocation)
        ensure_action_allowed(task, ACTION_EXPORT_DOCX)
        try:
            ServerArticleDocxExport(
                config=self._config,
                objects=self._object_service,
            ).export(
                actor=invocation.actor,
                project_id=invocation.project_id,
                task=task,
            )
            saved = runtime.audited_writer.put(
                task,
                expected_revision=cast(int, invocation.expected_task_revision),
                actor=invocation.actor,
                action="article.docx.exported",
                details={"image_count": len(task.images)},
            )
        except Exception as exc:
            raise WorkflowToolError("Word export could not be completed") from exc
        return {
            "task_id": saved.id,
            "result_revision": saved.revision,
            "asset_id": saved.docx_asset_id,
            "artifact_kind": "docx",
        }

    def _prepare_images(self, invocation: WorkflowToolInvocation) -> Mapping[str, Any]:
        if self._object_service is None or self._project_catalog is None:
            raise WorkflowToolUnavailable("Server image preparation is unavailable")
        runtime, task = self._task(invocation)
        ensure_action_allowed(task, ACTION_PREPARE_IMAGES)
        product_ids = tuple(
            product.product_id for product in task.products if product.product_id
        )
        available = self._project_catalog.image_asset_ids(
            invocation.project_id,
            product_ids,
        )
        hero_asset_id = self._optional_text(
            invocation.input_summary,
            "hero_asset_id",
        )
        if not hero_asset_id:
            hero_asset_id = next(
                (
                    asset_id
                    for product_id in product_ids
                    for asset_id in sorted(available.get(product_id, set()))
                ),
                "",
            )
        if not hero_asset_id:
            raise WorkflowToolError(
                "a published selected-product image is required"
            )
        raw_product_assets = invocation.input_summary.get("product_asset_ids", {})
        if not isinstance(raw_product_assets, Mapping):
            raise WorkflowToolError("product_asset_ids must be an object")
        product_asset_ids = {
            str(product_id).strip(): str(asset_id).strip()
            for product_id, asset_id in raw_product_assets.items()
            if str(product_id).strip() and str(asset_id).strip()
        }
        if (
            hero_asset_id
            not in {
                asset_id
                for values in available.values()
                for asset_id in values
            }
            or not set(product_asset_ids).issubset(product_ids)
            or any(
                asset_id not in available.get(product_id, set())
                for product_id, asset_id in product_asset_ids.items()
            )
        ):
            raise WorkflowToolError(
                "selected images are outside published product evidence"
            )
        try:
            ServerArticleImagePreparation(self._object_service).prepare(
                actor=invocation.actor,
                project_id=invocation.project_id,
                task=task,
                hero_asset_id=hero_asset_id,
                product_asset_ids=product_asset_ids,
                product_anchors=(
                    invocation.input_summary.get("product_anchors")
                    if isinstance(
                        invocation.input_summary.get("product_anchors"),
                        Mapping,
                    )
                    else None
                ),
            )
            saved = runtime.audited_writer.put(
                task,
                expected_revision=cast(int, invocation.expected_task_revision),
                actor=invocation.actor,
                action="article.images.prepared",
                details={"image_count": len(task.images)},
            )
        except Exception as exc:
            raise WorkflowToolError("article images could not be prepared") from exc
        return {
            "task_id": saved.id,
            "result_revision": saved.revision,
            "image_count": len(saved.images),
        }

    def _generate_tdk(self, invocation: WorkflowToolInvocation) -> Mapping[str, Any]:
        if self._object_service is None:
            raise WorkflowToolUnavailable("Server object storage is unavailable")
        runtime, task = self._task(invocation)
        ensure_action_allowed(task, ACTION_GENERATE_TDK)
        try:
            ServerTdkDocxExport(
                config=self._config,
                objects=self._object_service,
            ).generate(
                actor=invocation.actor,
                project_id=invocation.project_id,
                task=task,
            )
            saved = runtime.audited_writer.put(
                task,
                expected_revision=cast(int, invocation.expected_task_revision),
                actor=invocation.actor,
                action="article.tdk.generated",
                details={
                    "description_characters": task.tdk.description_character_count,
                    "keyword_count": len(task.tdk.keywords),
                },
            )
        except Exception as exc:
            raise WorkflowToolError("TDK generation could not be completed") from exc
        return {
            "task_id": saved.id,
            "result_revision": saved.revision,
            "asset_id": saved.tdk_asset_id,
            "artifact_kind": "tdk",
        }

    def _package_delivery(self, invocation: WorkflowToolInvocation) -> Mapping[str, Any]:
        if self._object_service is None:
            raise WorkflowToolUnavailable("Server object storage is unavailable")
        runtime, task = self._task(invocation)
        ensure_action_allowed(task, ACTION_PACKAGE_DELIVERY)
        try:
            ServerDeliveryPackage(objects=self._object_service).package(
                actor=invocation.actor,
                project_id=invocation.project_id,
                task=task,
            )
            saved = runtime.audited_writer.put(
                task,
                expected_revision=cast(int, invocation.expected_task_revision),
                actor=invocation.actor,
                action="article.delivery.packaged",
                details={
                    "file_count": len(task.images)
                    + 2
                    + int(bool(task.final_ai_check.screenshot_asset_id)),
                    "image_count": len(task.images),
                },
            )
        except Exception as exc:
            raise WorkflowToolError("delivery package could not be completed") from exc
        return {
            "task_id": saved.id,
            "result_revision": saved.revision,
            "asset_id": saved.delivery_package_asset_id,
            "artifact_kind": "delivery_package",
            "pending_ai_confirmation": bool(
                saved.final_ai_check.deferred
                and not saved.final_ai_check.confirmed
            ),
        }

    @staticmethod
    def _research_request_id(invocation: WorkflowToolInvocation) -> str:
        return f"assistant-{invocation.plan_id}-{invocation.step_id}"

    def _research_run_for_invocation(
        self,
        *,
        invocation: WorkflowToolInvocation,
        task: TaskRecord,
        request_id: str,
    ) -> ResearchGraphRun | None:
        requested_thread_id = self._optional_text(
            invocation.input_summary,
            "research_thread_id",
        )
        requested_plan_id = self._optional_text(
            invocation.input_summary,
            "retrieval_plan_id",
        )
        try:
            if requested_thread_id:
                run = self._research_runs.get_run(
                    invocation.project_id,
                    requested_thread_id,
                )
                candidates = () if run is None else (run,)
            else:
                candidates = self._research_runs.list_runs(
                    invocation.project_id,
                    article_id=f"topic_{task.topic_index:03d}",
                    limit=200,
                )
        except Exception as exc:
            raise WorkflowToolError("knowledge research Run is unavailable") from exc

        matches = tuple(
            run
            for run in candidates
            if run.organization_id == invocation.actor.organization_id
            and run.project_id == invocation.project_id
            and str(run.metadata.get("task_id") or "") == task.id
            and str(run.metadata.get("request_id") or "") == request_id
            and (not requested_plan_id or run.retrieval_plan_id == requested_plan_id)
        )
        if requested_thread_id and not matches:
            raise WorkflowToolError("knowledge research Run identity is invalid")
        if len(matches) > 1:
            raise WorkflowToolError("knowledge research Run identity is ambiguous")
        return matches[0] if matches else None

    def _active_research_job(
        self,
        *,
        invocation: WorkflowToolInvocation,
        task_id: str,
        thread_id: str,
    ) -> dict[str, Any] | None:
        """Find a manually queued Resume Job without reading its private URLs."""

        try:
            with self._engine.connect() as connection:
                rows = connection.execute(
                    sa.select(
                        background_jobs.c.job_id,
                        background_jobs.c.batch_id,
                        background_jobs.c.task_id,
                        background_jobs.c.operation,
                        background_jobs.c.status,
                        background_jobs.c.source_revision,
                        background_jobs.c.result_revision,
                        background_jobs.c.attempts,
                        background_jobs.c.created_at,
                        background_jobs.c.started_at,
                        background_jobs.c.finished_at,
                        background_jobs.c.updated_at,
                        background_jobs.c.error,
                        background_jobs.c.request,
                    )
                    .where(
                        background_jobs.c.organization_id
                        == invocation.actor.organization_id,
                        background_jobs.c.project_id == invocation.project_id,
                        background_jobs.c.task_id == task_id,
                        background_jobs.c.operation == KNOWLEDGE_RESEARCH_OPERATION,
                        background_jobs.c.status.in_(("queued", "running", "retry_wait")),
                    )
                    .order_by(background_jobs.c.created_at.desc())
                ).mappings()
                for row in rows:
                    request = dict(row["request"] or {})
                    if (
                        str(request.get("thread_id") or "").strip() == thread_id
                        and str(request.get("action") or "").strip() == "resume"
                    ):
                        return self._public_job(row)
        except Exception as exc:
            raise WorkflowToolError("knowledge research Job is unavailable") from exc
        return None

    @staticmethod
    def _approved_research_candidate_ids(
        summary: Mapping[str, Any],
    ) -> tuple[str, ...] | None:
        if "approved_candidate_ids" not in summary:
            return None
        raw = summary.get("approved_candidate_ids")
        if not isinstance(raw, list):
            raise WorkflowToolError("approved_candidate_ids must be a list")
        candidate_ids = tuple(dict.fromkeys(str(item).strip() for item in raw))
        if any(not item or len(item) > 200 for item in candidate_ids):
            raise WorkflowToolError("approved_candidate_ids are invalid")
        return candidate_ids

    def _start_research(self, invocation: WorkflowToolInvocation) -> Mapping[str, Any]:
        research = self._services.get("knowledge_research")
        if research is None:
            raise WorkflowToolUnavailable("knowledge research is unavailable")
        _, task = self._task(invocation)
        task_id = task.id
        request_id = self._research_request_id(invocation)
        run = self._research_run_for_invocation(
            invocation=invocation,
            task=task,
            request_id=request_id,
        )
        if run is not None:
            if run.status in {"completed", "completed_with_warnings"}:
                return {
                    "task_id": task_id,
                    "result_revision": task.revision,
                    **self._public_research_run(run),
                    "review_required": False,
                }
            if run.status in {"failed", "cancelled"}:
                raise WorkflowToolError(
                    f"knowledge research Run is {run.status}"
                )
            if run.status == "waiting_for_review":
                active_resume = self._active_research_job(
                    invocation=invocation,
                    task_id=task_id,
                    thread_id=run.thread_id,
                )
                if active_resume is not None:
                    return {
                        **self._waiting_job(active_resume),
                        **self._public_research_run(run),
                        "review_required": False,
                    }
                approved_candidate_ids = self._approved_research_candidate_ids(
                    invocation.input_summary
                )
                reviewed_thread_id = self._optional_text(
                    invocation.input_summary,
                    "research_thread_id",
                )
                if (
                    not invocation.confirmed
                    or not invocation.human_gate_confirmed
                    or reviewed_thread_id != run.thread_id
                    or approved_candidate_ids is None
                ):
                    raise WorkflowToolHumanGateRequired(
                        "research candidate review is required"
                    )
                resume_identity = "\n".join(
                    (
                        invocation.plan_id,
                        invocation.step_id,
                        run.thread_id,
                        *approved_candidate_ids,
                    )
                )
                resume_request_id = "assistant-resume-" + hashlib.sha256(
                    resume_identity.encode("utf-8")
                ).hexdigest()
                try:
                    queued = research.enqueue_resume(
                        actor=invocation.actor,
                        project_id=invocation.project_id,
                        thread_id=run.thread_id,
                        request_id=resume_request_id,
                        approved_candidate_ids=approved_candidate_ids,
                    )
                except Exception as exc:
                    raise WorkflowToolError(
                        "knowledge research Resume could not be queued"
                    ) from exc
                return {
                    **self._waiting_job(queued),
                    **self._public_research_run(run),
                    "review_required": False,
                }

            # Reuse the idempotent Start receipt while the Run is active. This
            # also lets an externally queued Resume advance the same Assistant
            # step without inventing a second action kind.
            try:
                queued = research.enqueue_start(
                    actor=invocation.actor,
                    project_id=invocation.project_id,
                    retrieval_plan_id=run.retrieval_plan_id,
                    request_id=request_id,
                    max_discovery_queries=run.max_discovery_queries,
                )
            except Exception as exc:
                raise WorkflowToolError(
                    "knowledge research could not be resumed"
                ) from exc
            return {
                **self._waiting_job(queued),
                **self._public_research_run(run),
                "review_required": False,
            }

        retrieval_plan_id = self._optional_text(
            invocation.input_summary,
            "retrieval_plan_id",
        )
        try:
            if not retrieval_plan_id:
                retrieval_plan = research.create_plan_from_task(
                    actor=invocation.actor,
                    project_id=invocation.project_id,
                    task_id=task_id,
                )
                retrieval_plan_id = str(retrieval_plan.retrieval_plan_id)
            max_queries = self._bounded_int(
                invocation.input_summary,
                "max_discovery_queries",
                default=0,
                minimum=0,
                maximum=20,
            )
            queued = research.enqueue_start(
                actor=invocation.actor,
                project_id=invocation.project_id,
                retrieval_plan_id=retrieval_plan_id,
                request_id=request_id,
                max_discovery_queries=max_queries,
            )
        except Exception as exc:
            raise WorkflowToolError("knowledge research could not be queued") from exc
        return self._waiting_job(queued)

    @staticmethod
    def _operation_for_step(step: Any) -> str:
        action = str(step.action_kind)
        if action == "generate_article":
            requested = str(step.input_summary.get("operation") or "").strip()
            return requested or ARTICLE_GENERATION_OPERATION
        if action == "start_research":
            return KNOWLEDGE_RESEARCH_OPERATION
        return {
            "generate_titles": "titles",
            "generate_products": "products",
            "generate_outline": "outline",
            "humanize": "humanize",
            "review": "seo_review",
            "restore_links": "restore_links",
            "prepare_images": "prepare_images",
        }.get(action, action)

    @classmethod
    def _waiting_job(cls, value: Mapping[str, Any]) -> dict[str, Any]:
        job_value = value.get("job") if isinstance(value.get("job"), Mapping) else value
        projected = cls._public_job(job_value)
        projected["_workflow_status"] = "waiting_job"
        return projected

    @staticmethod
    def _public_job(job: Mapping[str, Any]) -> dict[str, Any]:
        job_id = job.get("job_id", job.get("id"))
        if job_id is None:
            raise WorkflowToolError("queued Job has no public identity")

        def optional(value: Any) -> str | None:
            if value is None:
                return None
            normalized = str(value).strip()
            return normalized or None

        return {
            "job_id": str(job_id),
            "batch_id": str(job.get("batch_id") or ""),
            "task_id": str(job.get("task_id") or ""),
            "operation": str(job.get("operation") or ""),
            "status": str(job.get("status") or "queued"),
            "source_revision": int(job.get("source_revision") or 0),
            "result_revision": (
                None
                if job.get("result_revision") is None
                else int(job["result_revision"])
            ),
            "attempts": int(job.get("attempts") or 0),
            "created_at": str(job.get("created_at") or ""),
            "started_at": optional(job.get("started_at")),
            "finished_at": optional(job.get("finished_at")),
            "updated_at": str(job.get("updated_at") or ""),
            "has_error": bool(str(job.get("error") or ""))
            if "has_error" not in job
            else bool(job.get("has_error")),
        }


__all__ = ["WorkflowAssistantServiceAdapters"]
