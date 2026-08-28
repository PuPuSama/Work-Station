from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Literal

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from knowledge_agent.assets import PostgresKnowledgeAssetRepository
from knowledge_agent.catalog import PostgresProductCatalogRepository
from knowledge_agent.contracts import KnowledgeSource, RetrievalPlan
from knowledge_agent.evidence_repository import (
    EvidenceRepositoryError,
    PostgresEvidencePackRepository,
    PostgresRetrievalPlanRepository,
)
from knowledge_agent.interfaces import EmbeddingProvider
from knowledge_agent.library import PostgresKnowledgeLibrary
from knowledge_agent.object_storage import ScopedS3ArtifactStore
from knowledge_agent.publication import KnowledgePublicationService
from knowledge_agent.repository import PostgresKnowledgeRepository
from knowledge_agent.research_adapters import (
    M3ScopeEvidenceAdapter,
    OfficialCandidateIngestionAdapter,
    PostgresProjectDirectory,
    PostgresRetrievalPlanAdapter,
    TavilyOfficialDiscoveryAdapter,
)
from knowledge_agent.research_execution import (
    ResearchExecutionError,
    ResearchGraphExecutionService,
    ResearchGraphSessionFactory,
)
from knowledge_agent.research_graph import (
    CandidateIngestionResult,
    ResearchCandidate,
    ResearchGraphRequest,
)
from knowledge_agent.research_runs import (
    PostgresResearchRunRepository,
    ResearchGraphRun,
    ResearchRunConflictError,
    ResearchRunNotFound,
)
from knowledge_agent.schema import (
    evidence_pack_hits,
    evidence_packs,
    gap_fill_attempts,
    research_graph_events,
    research_graph_runs,
    retrieval_plans,
    retrieval_scopes,
)
from knowledge_agent.research_telemetry import PostgresResearchTelemetry
from knowledge_agent.retrieval_plan_generation import generate_retrieval_plan
from knowledge_agent.scope_evidence import ScopeEvidenceService
from knowledge_agent.web_ingestion import OfficialWebPageIngestionService
from knowledge_agent.wordpress import SafeOfficialSiteFetcher
from models import (
    STATUS_OUTLINE_CONFIRMED,
    TaskRecord,
    WORKFLOW_STATUSES,
)
from server_schema import article_tasks, background_jobs
from services.access_control import (
    ActorIdentity,
    PostgresProjectAccessRepository,
    ProjectAccessDenied,
    ProjectAccessService,
    decide_project_permission,
)
from services.audit_log import (
    AuditEvent,
    AuditEventWriter,
    PostgresAuditEventWriter,
)
from services.authorized_job_queue import DEFAULT_PROJECT_JOB_CONCURRENCY
from services.job_queue import (
    ACTIVE_JOB_STATUSES,
    ActiveJobError,
    JobCancelled,
    JobConflict,
)
from services.object_store import ObjectStore
from services.server_knowledge_commands import (
    PostgresServerKnowledgeCommands,
)
from services.server_knowledge_gap_repair import (
    ServerKnowledgeGapRepairService,
    TargetedGapRepairResult,
)
from services.server_project_job_registry import (
    ProjectJobHandler,
    ServerProjectJobRegistry,
    ServerProjectJobStopReport,
    public_job,
)
from services.server_web_evidence_ingestion import (
    CheckpointingOfficialSiteFetcher,
    PostgresServerWebEvidenceIngestion,
    ServerWebEvidenceContext,
)
from services.tavily import TavilyClient


KNOWLEDGE_RESEARCH_OPERATION = "knowledge_research"
MAX_APPROVED_CANDIDATES = 20


@dataclass(frozen=True, slots=True)
class _ActiveResearchExecution:
    actor: ActorIdentity
    cancelled: Callable[[], bool]


_ACTIVE_RESEARCH_EXECUTION: ContextVar[
    _ActiveResearchExecution | None
] = ContextVar(
    "server_knowledge_research_execution",
    default=None,
)


class ServerKnowledgeResearchUnavailable(RuntimeError):
    """The authorized Server research command could not be completed safely."""


@dataclass(frozen=True, slots=True)
class _PlanReplacement:
    kept_plan_exists: bool = False
    discarded_plan_count: int = 0
    discarded_run_count: int = 0
    discarded_evidence_pack_count: int = 0


def _required_text(value: object, field_name: str, *, max_length: int) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > max_length:
        raise JobConflict(f"{field_name} is invalid")
    return normalized


def _confirmed_outline_version(task: TaskRecord) -> int:
    return max(
        1,
        sum(
            1
            for version in task.article_versions
            if version.kind == "outline"
            and version.source_kind == "manual_confirmed"
        ),
    )


def _confirmed_outline_ready(task: TaskRecord) -> bool:
    try:
        confirmed_or_later = (
            WORKFLOW_STATUSES.index(task.status)
            >= WORKFLOW_STATUSES.index(STATUS_OUTLINE_CONFIRMED)
        )
    except ValueError:
        confirmed_or_later = False
    return confirmed_or_later and bool(task.outline.strip())


def _outline_hash(task: TaskRecord) -> str:
    return hashlib.sha256(task.outline.strip().encode("utf-8")).hexdigest()


def _article_id(task: TaskRecord) -> str:
    return f"topic_{task.topic_index:03d}"


def is_server_generated_retrieval_plan(plan: RetrievalPlan) -> bool:
    """Identify Plans created from the confirmed PostgreSQL Task boundary.

    This marker controls Server read visibility only. Execution still calls
    ``_validate_plan_task`` so a current Task, outline version and outline hash
    must match before a Research Job can be queued.
    """

    metadata = dict(plan.metadata)
    outline_hash = str(metadata.get("outline_hash") or "").strip()
    return (
        metadata.get("generated_from") == "confirmed_task_outline"
        and bool(str(metadata.get("task_id") or "").strip())
        and len(outline_hash) == 64
        and all(character in "0123456789abcdef" for character in outline_hash)
    )


def _validate_plan_task(plan: RetrievalPlan, task: TaskRecord) -> None:
    metadata = dict(plan.metadata)
    if not is_server_generated_retrieval_plan(plan):
        raise JobConflict("retrieval plan is not server-generated")
    if str(metadata.get("task_id") or "") != task.id:
        raise JobConflict("retrieval plan does not match the source task")
    if plan.article_id != _article_id(task):
        raise JobConflict("retrieval plan does not match the source article")
    if (
        not _confirmed_outline_ready(task)
        or plan.outline_version != _confirmed_outline_version(task)
        or str(metadata.get("outline_hash") or "") != _outline_hash(task)
    ):
        raise JobConflict("confirmed outline changed after plan creation")


def _research_scope_selection(
    plan: RetrievalPlan,
    *,
    scope_ids: Sequence[str] = (),
    initial_evidence_pack_ids: Sequence[str] = (),
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Validate the optional targeted-run subset against the immutable Plan."""

    selected = tuple(dict.fromkeys(str(value).strip() for value in scope_ids))
    carried = tuple(
        dict.fromkeys(str(value).strip() for value in initial_evidence_pack_ids)
    )
    if any(not value or len(value) > 200 for value in (*selected, *carried)):
        raise JobConflict("knowledge research scope selection is invalid")
    plan_scope_ids = tuple(scope.scope_id for scope in plan.scopes)
    if not selected:
        selected = tuple(plan_scope_ids)
    if set(selected) - set(plan_scope_ids):
        raise JobConflict("knowledge research scope selection is invalid")
    if len(selected) > 100 or len(carried) > 200:
        raise JobConflict("knowledge research scope selection is invalid")
    return selected, carried


def _discard_replaced_task_plans(
    connection: Connection,
    *,
    project_id: str,
    article_id: str,
    task_id: str,
    keep_plan_id: str,
) -> _PlanReplacement:
    """Delete superseded research artifacts inside the Plan transaction."""

    rows = connection.execute(
        sa.select(
            retrieval_plans.c.retrieval_plan_id,
            retrieval_plans.c.metadata,
        )
        .where(
            retrieval_plans.c.project_id == project_id,
            retrieval_plans.c.article_id == article_id,
        )
        .with_for_update()
    ).mappings()
    task_plan_ids = tuple(
        str(row["retrieval_plan_id"])
        for row in rows
        if str(dict(row["metadata"] or {}).get("task_id") or "") == task_id
    )
    kept_plan_exists = keep_plan_id in task_plan_ids
    discarded_plan_ids = tuple(
        plan_id for plan_id in task_plan_ids if plan_id != keep_plan_id
    )
    if not discarded_plan_ids:
        return _PlanReplacement(kept_plan_exists=kept_plan_exists)

    thread_ids = tuple(
        str(thread_id)
        for thread_id in connection.execute(
            sa.select(research_graph_runs.c.thread_id).where(
                research_graph_runs.c.project_id == project_id,
                research_graph_runs.c.retrieval_plan_id.in_(
                    discarded_plan_ids
                ),
            )
        ).scalars()
    )
    evidence_pack_ids = tuple(
        str(evidence_pack_id)
        for evidence_pack_id in connection.execute(
            sa.select(evidence_packs.c.evidence_pack_id).where(
                evidence_packs.c.project_id == project_id,
                evidence_packs.c.retrieval_plan_id.in_(
                    discarded_plan_ids
                ),
            )
        ).scalars()
    )

    connection.execute(
        gap_fill_attempts.delete().where(
            gap_fill_attempts.c.project_id == project_id,
            gap_fill_attempts.c.retrieval_plan_id.in_(discarded_plan_ids),
        )
    )
    if thread_ids:
        connection.execute(
            research_graph_events.delete().where(
                research_graph_events.c.project_id == project_id,
                research_graph_events.c.thread_id.in_(thread_ids),
            )
        )
        for table_name in (
            "checkpoints",
            "checkpoint_blobs",
            "checkpoint_writes",
        ):
            connection.execute(
                sa.text(
                    f"DELETE FROM {table_name} WHERE thread_id IN :thread_ids"
                ).bindparams(sa.bindparam("thread_ids", expanding=True)),
                {"thread_ids": thread_ids},
            )
    connection.execute(
        research_graph_runs.delete().where(
            research_graph_runs.c.project_id == project_id,
            research_graph_runs.c.retrieval_plan_id.in_(discarded_plan_ids),
        )
    )
    if evidence_pack_ids:
        connection.execute(
            evidence_pack_hits.delete().where(
                evidence_pack_hits.c.project_id == project_id,
                evidence_pack_hits.c.evidence_pack_id.in_(evidence_pack_ids),
            )
        )
    connection.execute(
        evidence_packs.delete().where(
            evidence_packs.c.project_id == project_id,
            evidence_packs.c.retrieval_plan_id.in_(discarded_plan_ids),
        )
    )
    connection.execute(
        retrieval_scopes.delete().where(
            retrieval_scopes.c.project_id == project_id,
            retrieval_scopes.c.retrieval_plan_id.in_(discarded_plan_ids),
        )
    )
    connection.execute(
        retrieval_plans.delete().where(
            retrieval_plans.c.project_id == project_id,
            retrieval_plans.c.retrieval_plan_id.in_(discarded_plan_ids),
        )
    )
    return _PlanReplacement(
        kept_plan_exists=kept_plan_exists,
        discarded_plan_count=len(discarded_plan_ids),
        discarded_run_count=len(thread_ids),
        discarded_evidence_pack_count=len(evidence_pack_ids),
    )


def _server_thread_id(
    *,
    organization_id: str,
    project_id: str,
    retrieval_plan_id: str,
    request_id: str,
) -> str:
    identity = "\n".join(
        (organization_id, project_id, retrieval_plan_id, request_id)
    )
    return "rg_server_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class KnowledgeResearchCommand:
    action: Literal["start", "resume"]
    request_id: str
    thread_id: str
    retrieval_plan_id: str
    outline_version: int
    max_discovery_queries: int
    scope_ids: tuple[str, ...] = ()
    initial_evidence_pack_ids: tuple[str, ...] = ()
    approved_urls: tuple[str, ...] = ()

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, object],
    ) -> KnowledgeResearchCommand:
        action = str(value.get("action") or "").strip()
        if action not in {"start", "resume"}:
            raise JobConflict("knowledge research action is invalid")
        raw_outline_version = value.get("outline_version")
        raw_budget = value.get("max_discovery_queries", 2)
        if isinstance(raw_outline_version, bool) or isinstance(raw_budget, bool):
            raise JobConflict("knowledge research request is invalid")
        try:
            outline_version = int(raw_outline_version)
            max_discovery_queries = int(raw_budget)
        except (TypeError, ValueError) as exc:
            raise JobConflict("knowledge research request is invalid") from exc
        if outline_version <= 0 or not 0 <= max_discovery_queries <= 20:
            raise JobConflict("knowledge research request is invalid")
        def normalized_list(field_name: str, maximum: int) -> tuple[str, ...]:
            raw = value.get(field_name) or []
            if not isinstance(raw, list):
                raise JobConflict("knowledge research request is invalid")
            normalized = tuple(
                dict.fromkeys(str(item).strip() for item in raw)
            )
            if (
                len(normalized) > maximum
                or any(not item or len(item) > 200 for item in normalized)
            ):
                raise JobConflict("knowledge research request is invalid")
            return normalized

        scope_ids = normalized_list("scope_ids", 100)
        initial_evidence_pack_ids = normalized_list(
            "initial_evidence_pack_ids",
            200,
        )
        if action == "resume" and (scope_ids or initial_evidence_pack_ids):
            raise JobConflict("knowledge research request is invalid")
        raw_urls = value.get("approved_urls")
        if raw_urls is None:
            raw_urls = []
        if not isinstance(raw_urls, list):
            raise JobConflict("knowledge research candidates are invalid")
        approved_urls = tuple(
            dict.fromkeys(str(item).strip() for item in raw_urls)
        )
        if (
            len(approved_urls) > MAX_APPROVED_CANDIDATES
            or any(not url or len(url) > 4096 for url in approved_urls)
            or (action == "start" and approved_urls)
        ):
            raise JobConflict("knowledge research candidates are invalid")
        return cls(
            action=action,  # type: ignore[arg-type]
            request_id=_required_text(
                value.get("request_id"),
                "request_id",
                max_length=200,
            ),
            thread_id=_required_text(
                value.get("thread_id"),
                "thread_id",
                max_length=200,
            ),
            retrieval_plan_id=_required_text(
                value.get("retrieval_plan_id"),
                "retrieval_plan_id",
                max_length=200,
            ),
            outline_version=outline_version,
            max_discovery_queries=max_discovery_queries,
            scope_ids=scope_ids,
            initial_evidence_pack_ids=initial_evidence_pack_ids,
            approved_urls=approved_urls,
        )

    def private_values(self) -> dict[str, object]:
        return {
            "action": self.action,
            "request_id": self.request_id,
            "thread_id": self.thread_id,
            "retrieval_plan_id": self.retrieval_plan_id,
            "outline_version": self.outline_version,
            "max_discovery_queries": self.max_discovery_queries,
            "scope_ids": list(self.scope_ids),
            "initial_evidence_pack_ids": list(
                self.initial_evidence_pack_ids
            ),
            "approved_urls": list(self.approved_urls),
        }


class ServerCandidateIngestionAdapter:
    """Bind approved official-page bytes and publication to the active Job actor."""

    def __init__(
        self,
        engine: Engine,
        *,
        store: ObjectStore,
        bucket: str,
        publication: KnowledgePublicationService,
        attempts: PostgresResearchRunRepository,
        access: ProjectAccessService,
    ) -> None:
        self._engine = engine
        self._store = store
        self._bucket = bucket
        self._publication = publication
        self._attempts = attempts
        self._access = access

    def ingest(
        self,
        *,
        project_id: str,
        thread_id: str,
        retrieval_plan_id: str,
        scope_id: str,
        round_number: int,
        candidates: Sequence[ResearchCandidate],
        approved_urls: Sequence[str],
        attempt_id: str,
    ) -> CandidateIngestionResult:
        execution = _ACTIVE_RESEARCH_EXECUTION.get()
        if execution is None:
            raise JobConflict(
                "knowledge research execution context is unavailable"
            )
        actor = execution.actor

        def checkpoint() -> None:
            if execution.cancelled():
                raise JobCancelled("Knowledge research cancelled.")
            self._access.require(
                actor,
                project_id,
                "knowledge.publish",
            )

        checkpoint()
        repository = PostgresKnowledgeRepository(self._engine)
        assets = PostgresKnowledgeAssetRepository(self._engine)
        catalog = PostgresProductCatalogRepository(self._engine)
        library = PostgresKnowledgeLibrary(self._engine)
        fetcher = CheckpointingOfficialSiteFetcher(
            SafeOfficialSiteFetcher(),
            checkpoint=checkpoint,
        )
        commands = PostgresServerKnowledgeCommands(
            self._engine,
            repository=repository,
            catalog=catalog,
            publication=self._publication,
        )
        preparer = OfficialWebPageIngestionService(
            repository=repository,
            asset_repository=assets,
            catalog_repository=catalog,
            artifact_store=ScopedS3ArtifactStore(
                store=self._store,
                bucket=self._bucket,
                organization_id=actor.organization_id,
                project_id=project_id,
                checkpoint=checkpoint,
            ),
            fetcher=fetcher,
            snapshot_lookup=library,
        )
        web_ingestion = PostgresServerWebEvidenceIngestion(
            self._engine,
            preparer=preparer,
            context=ServerWebEvidenceContext(
                actor=actor,
                project_id=project_id,
                operation=KNOWLEDGE_RESEARCH_OPERATION,
                target_type="research_thread",
                target_id=thread_id,
                permission="knowledge.publish",
                cancelled=execution.cancelled,
            ),
            bucket=self._bucket,
            repository=repository,
            assets=assets,
            catalog=catalog,
        )
        def review_source(source, snapshot_id, decision, reason):
            checkpoint()
            receipt_id = "research-review-" + uuid.uuid5(
                uuid.NAMESPACE_URL,
                "\x1f".join(
                    (
                        actor.organization_id,
                        project_id,
                        thread_id,
                        attempt_id,
                        snapshot_id,
                        decision,
                    )
                ),
            ).hex
            return commands.review_snapshot(
                actor=actor,
                project_id=project_id,
                source_id=source.source_id,
                snapshot_id=snapshot_id,
                receipt_id=receipt_id,
                source_kind=source.source_kind,
                trust_tier=source.trust_tier,
                decision=decision,
                reason=reason,
            )

        def publish_source(source, snapshot_id):
            checkpoint()
            return commands.publish_source(
                actor=actor,
                project_id=project_id,
                source_id=source.source_id,
                snapshot_id=snapshot_id,
            ).source_id

        ingestion = OfficialCandidateIngestionAdapter(
            projects=PostgresProjectDirectory(self._engine),
            web_ingestion=web_ingestion,
            repository=repository,
            library=library,
            publication=self._publication,
            attempts=self._attempts,
            authorize_candidate=checkpoint,
            review_source=review_source,
            publish_source=publish_source,
        )
        return ingestion.ingest(
            project_id=project_id,
            thread_id=thread_id,
            retrieval_plan_id=retrieval_plan_id,
            scope_id=scope_id,
            round_number=round_number,
            candidates=candidates,
            approved_urls=approved_urls,
            attempt_id=attempt_id,
        )


def create_server_research_execution(
    *,
    engine: Engine,
    database_url: str,
    embedding_provider: EmbeddingProvider | None,
    store: ObjectStore,
    bucket: str,
    access: ProjectAccessService,
) -> ResearchGraphExecutionService | None:
    """Build the Server graph with S3-only candidate ingestion."""

    if embedding_provider is None:
        return None
    search = TavilyClient()
    if not search.ready:
        return None
    repository = PostgresKnowledgeRepository(engine)
    library = PostgresKnowledgeLibrary(engine)
    plans = PostgresRetrievalPlanRepository(engine)
    packs = PostgresEvidencePackRepository(engine)
    runs = PostgresResearchRunRepository(engine)
    publication = KnowledgePublicationService(
        repository=repository,
        library=library,
        embedding_provider=embedding_provider,
    )
    from knowledge_agent.hybrid_retriever import (
        BasicHybridRetriever,
        ContextualTokenReranker,
    )

    retriever = BasicHybridRetriever(
        engine,
        embedding_provider,
        reranker=ContextualTokenReranker(),
    )
    scope_evidence = ScopeEvidenceService(
        plans=plans,
        retriever=retriever,
        packs=packs,
    )
    return ResearchGraphExecutionService(
        sessions=ResearchGraphSessionFactory(
            database_url=database_url,
            plans=PostgresRetrievalPlanAdapter(plans),
            evidence=M3ScopeEvidenceAdapter(scope_evidence),
            discovery=TavilyOfficialDiscoveryAdapter(
                projects=PostgresProjectDirectory(engine),
                plans=plans,
                search=search,
                attempts=runs,
            ),
            ingestion=ServerCandidateIngestionAdapter(
                engine,
                store=store,
                bucket=bucket,
                publication=publication,
                attempts=runs,
                access=access,
            ),
            telemetry=PostgresResearchTelemetry(runs),
        ),
        runs=runs,
        passthrough_exceptions=(JobCancelled,),
    )


class ServerKnowledgeResearchHandler:
    """Execute a private Start/Resume command after Queue reauthorization."""

    def __init__(
        self,
        engine: Engine,
        *,
        execution: ResearchGraphExecutionService,
    ) -> None:
        self._engine = engine
        self._execution = execution
        self._plans = PostgresRetrievalPlanRepository(engine)
        self._runs = PostgresResearchRunRepository(engine)

    def __call__(
        self,
        job: dict[str, Any],
        cancelled: Callable[[], bool],
    ) -> int:
        if str(job.get("operation") or "") != KNOWLEDGE_RESEARCH_OPERATION:
            raise JobConflict("unsupported server job operation")
        organization_id = _required_text(
            job.get("organization_id"),
            "organization_id",
            max_length=200,
        )
        project_id = _required_text(
            job.get("project_id"),
            "project_id",
            max_length=200,
        )
        task_id = _required_text(
            job.get("task_id"),
            "task_id",
            max_length=200,
        )
        requester = _required_text(
            job.get("requested_by_user_id"),
            "requested_by_user_id",
            max_length=200,
        )
        source_revision = int(job.get("source_revision") or 0)
        command = KnowledgeResearchCommand.from_mapping(
            dict(job.get("request") or {})
        )
        if cancelled():
            raise JobCancelled("Knowledge research cancelled before execution.")
        with self._engine.connect() as connection:
            row = connection.execute(
                sa.select(article_tasks.c.payload).where(
                    article_tasks.c.organization_id == organization_id,
                    article_tasks.c.project_id == project_id,
                    article_tasks.c.task_id == task_id,
                )
            ).scalar_one_or_none()
        if row is None:
            raise JobConflict("source task was not found")
        task = TaskRecord.model_validate(dict(row))
        if task.revision != source_revision:
            raise JobConflict("source task revision changed")
        plan = self._plans.get_retrieval_plan(
            project_id,
            command.retrieval_plan_id,
        )
        if plan is None:
            raise JobConflict("retrieval plan was not found")
        _validate_plan_task(plan, task)
        run = self._runs.get_run(project_id, command.thread_id)
        if (
            run is None
            or run.organization_id != organization_id
            or run.retrieval_plan_id != plan.retrieval_plan_id
            or run.outline_version != command.outline_version
        ):
            raise JobConflict("research run identity is invalid")
        execution_token = _ACTIVE_RESEARCH_EXECUTION.set(
            _ActiveResearchExecution(
                actor=ActorIdentity(organization_id, requester),
                cancelled=cancelled,
            )
        )
        try:
            if command.action == "start":
                self._execution.execute_start(
                    ResearchGraphRequest(
                        organization_id=organization_id,
                        project_id=project_id,
                        article_id=plan.article_id,
                        outline_version=plan.outline_version,
                        retrieval_plan_id=plan.retrieval_plan_id,
                        thread_id=command.thread_id,
                        max_gap_fill_rounds=plan.max_gap_fill_rounds,
                        max_discovery_queries=command.max_discovery_queries,
                        scope_ids=command.scope_ids,
                        initial_evidence_pack_ids=(
                            command.initial_evidence_pack_ids
                        ),
                    )
                )
            else:
                self._execution.validate_resume(
                    project_id=project_id,
                    thread_id=command.thread_id,
                    approved_urls=command.approved_urls,
                )
                self._execution.execute_resume(
                    project_id=project_id,
                    thread_id=command.thread_id,
                    approved_urls=command.approved_urls,
                )
        except (ResearchRunConflictError, ResearchRunNotFound) as exc:
            raise JobConflict("research run state changed") from exc
        except ResearchExecutionError as exc:
            # The graph has already persisted a sanitized terminal Run state.
            # Automatic Job retry would replay a terminal checkpoint, so keep
            # this Job terminal until a domain Resume command is created.
            raise JobConflict("knowledge research execution failed") from exc
        finally:
            _ACTIVE_RESEARCH_EXECUTION.reset(execution_token)
        return source_revision


class ServerKnowledgeResearchRegistry:
    """Atomic Plan/Run commands plus the project-scoped PostgreSQL runner."""

    def __init__(
        self,
        engine: Engine,
        *,
        access: ProjectAccessService,
        execution: ResearchGraphExecutionService | None,
        project_job_concurrency: int = DEFAULT_PROJECT_JOB_CONCURRENCY,
        access_repository: PostgresProjectAccessRepository | None = None,
        audit: AuditEventWriter | None = None,
    ) -> None:
        self._engine = engine
        self._access = access
        self._access_repository = (
            access_repository or PostgresProjectAccessRepository(engine)
        )
        self._audit = audit or PostgresAuditEventWriter()
        self._plans = PostgresRetrievalPlanRepository(engine)
        self._runs = PostgresResearchRunRepository(engine)
        self._gap_repair = ServerKnowledgeGapRepairService(
            engine,
            plans=self._plans,
        )
        handler: ProjectJobHandler | None = (
            None
            if execution is None
            else ServerKnowledgeResearchHandler(
                engine,
                execution=execution,
            )
        )
        self._execution = execution
        self._registry = ServerProjectJobRegistry(
            engine,
            operation=KNOWLEDGE_RESEARCH_OPERATION,
            access=access,
            handler=handler,
            error_type=ServerKnowledgeResearchUnavailable,
            terminal_audit=self._audit,
            project_job_concurrency=project_job_concurrency,
        )

    def start_existing(self) -> None:
        self._registry.start_existing()

    def stop(
        self,
        *,
        timeout_seconds: float = 10.0,
    ) -> ServerProjectJobStopReport:
        return self._registry.stop(timeout_seconds=timeout_seconds)

    def create_plan_from_task(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        task_id: str,
    ) -> RetrievalPlan:
        self._access.require(actor, project_id, "knowledge.edit")
        try:
            with self._engine.begin() as connection:
                self._lock_access(
                    connection,
                    actor,
                    project_id,
                    "knowledge.edit",
                )
                task = self._lock_task(
                    connection,
                    actor=actor,
                    project_id=project_id,
                    task_id=task_id,
                )
                if not _confirmed_outline_ready(task):
                    raise JobConflict(
                        "confirm the outline before creating a retrieval plan"
                    )
                active_job = connection.execute(
                    sa.select(background_jobs.c.job_id)
                    .where(
                        background_jobs.c.organization_id
                        == actor.organization_id,
                        background_jobs.c.project_id == project_id,
                        background_jobs.c.task_id == task.id,
                        background_jobs.c.status.in_(ACTIVE_JOB_STATUSES),
                    )
                    .with_for_update()
                ).first()
                if active_job is not None:
                    raise JobConflict(
                        "wait for the active task job before replacing "
                        "the retrieval plan"
                    )
                plan = generate_retrieval_plan(
                    project_id=project_id,
                    article_id=_article_id(task),
                    task_id=task.id,
                    outline_version=_confirmed_outline_version(task),
                    outline=task.outline,
                    topic=task.topic,
                    products=[
                        product.model_dump() for product in task.products
                    ],
                    article_brief=(
                        task.article_brief.model_dump(mode="json")
                        if task.article_brief is not None
                        else None
                    ),
                )
                replacement = _discard_replaced_task_plans(
                    connection,
                    project_id=project_id,
                    article_id=plan.article_id,
                    task_id=task.id,
                    keep_plan_id=plan.retrieval_plan_id,
                )
                persisted = self._plans.save_retrieval_plan_in_transaction(
                    connection,
                    plan,
                )
                if (
                    replacement.kept_plan_exists
                    and replacement.discarded_plan_count == 0
                ):
                    return persisted
                action = (
                    "knowledge.retrieval_plan.replaced"
                    if replacement.discarded_plan_count
                    else "knowledge.retrieval_plan.created"
                )
                self._append_audit(
                    connection,
                    actor=actor,
                    project_id=project_id,
                    action=action,
                    target_type="retrieval_plan",
                    target_id=persisted.retrieval_plan_id,
                    details={
                        "task_id": task.id,
                        "article_id": persisted.article_id,
                        "outline_version": persisted.outline_version,
                        "scope_count": len(persisted.scopes),
                        "discarded_plan_count": (
                            replacement.discarded_plan_count
                        ),
                        "discarded_run_count": (
                            replacement.discarded_run_count
                        ),
                        "discarded_evidence_pack_count": (
                            replacement.discarded_evidence_pack_count
                        ),
                    },
                )
                return persisted
        except (
            EvidenceRepositoryError,
            IntegrityError,
            JobConflict,
            KeyError,
            ProjectAccessDenied,
            ValueError,
        ):
            raise
        except (RuntimeError, SQLAlchemyError) as exc:
            raise ServerKnowledgeResearchUnavailable(
                "retrieval plan could not be created"
            ) from exc

    def create_targeted_gap_plan(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        task_id: str,
        source_revision: int,
        retrieval_plan_id: str,
        sentence_ids: Sequence[str] = (),
    ) -> TargetedGapRepairResult:
        """Create a new repair Plan while retaining unaffected evidence Packs."""

        self._access.require(actor, project_id, "knowledge.publish")
        normalized_plan_id = _required_text(
            retrieval_plan_id,
            "retrieval_plan_id",
            max_length=200,
        )
        if isinstance(source_revision, bool) or source_revision < 0:
            raise JobConflict("source task revision is invalid")
        requested_sentence_ids = tuple(
            dict.fromkeys(str(value).strip() for value in sentence_ids)
        )
        if len(requested_sentence_ids) > 12 or any(
            not value or len(value) > 200 for value in requested_sentence_ids
        ):
            raise JobConflict("targeted repair sentence identity is invalid")
        try:
            with self._engine.begin() as connection:
                self._lock_access(
                    connection,
                    actor,
                    project_id,
                    "knowledge.publish",
                )
                task = self._lock_task(
                    connection,
                    actor=actor,
                    project_id=project_id,
                    task_id=task_id,
                )
                if task.revision != source_revision:
                    raise JobConflict("source task revision changed")
                if not _confirmed_outline_ready(task):
                    raise JobConflict(
                        "confirm the outline before targeted repair"
                    )
                active_job = connection.execute(
                    sa.select(background_jobs.c.job_id)
                    .where(
                        background_jobs.c.organization_id
                        == actor.organization_id,
                        background_jobs.c.project_id == project_id,
                        background_jobs.c.task_id == task.id,
                        background_jobs.c.status.in_(ACTIVE_JOB_STATUSES),
                    )
                    .with_for_update()
                ).first()
                if active_job is not None:
                    raise JobConflict(
                        "wait for the active task job before targeted repair"
                    )
                base_plan = self._plans.get_retrieval_plan_in_transaction(
                    connection,
                    project_id,
                    normalized_plan_id,
                )
                if base_plan is None:
                    raise KeyError(normalized_plan_id)
                _validate_plan_task(base_plan, task)
                result = self._gap_repair.create_in_transaction(
                    connection,
                    project_id=project_id,
                    task=task,
                    base_plan=base_plan,
                    sentence_ids=requested_sentence_ids,
                )
                self._append_audit(
                    connection,
                    actor=actor,
                    project_id=project_id,
                    action="knowledge.retrieval_plan.targeted_repair_created",
                    target_type="retrieval_plan",
                    target_id=result.plan.retrieval_plan_id,
                    details={
                        "task_id": task.id,
                        "repair_of_plan_id": base_plan.retrieval_plan_id,
                        "gap_count": len(result.gaps),
                        "targeted_scope_count": len(
                            result.targeted_scope_ids
                        ),
                        "carried_evidence_pack_count": len(
                            result.carried_evidence_pack_ids
                        ),
                    },
                )
                return result
        except (
            EvidenceRepositoryError,
            IntegrityError,
            JobConflict,
            KeyError,
            ProjectAccessDenied,
            ValueError,
        ):
            raise
        except (RuntimeError, SQLAlchemyError) as exc:
            raise ServerKnowledgeResearchUnavailable(
                "targeted knowledge repair Plan could not be created"
            ) from exc

    def enqueue_start(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        retrieval_plan_id: str,
        request_id: str,
        max_discovery_queries: int,
        scope_ids: Sequence[str] = (),
        initial_evidence_pack_ids: Sequence[str] = (),
    ) -> dict[str, object]:
        normalized_request_id = _required_text(
            request_id,
            "request_id",
            max_length=200,
        )
        normalized_plan_id = _required_text(
            retrieval_plan_id,
            "retrieval_plan_id",
            max_length=200,
        )
        if (
            isinstance(max_discovery_queries, bool)
            or not 0 <= max_discovery_queries <= 20
        ):
            raise ValueError("max_discovery_queries must be between 0 and 20")
        self._access.require(actor, project_id, "knowledge.publish")
        project = self._registry.project(
            actor.organization_id,
            project_id,
            start_runner=True,
        )
        thread_id = _server_thread_id(
            organization_id=actor.organization_id,
            project_id=project_id,
            retrieval_plan_id=normalized_plan_id,
            request_id=normalized_request_id,
        )
        try:
            with self._engine.begin() as connection:
                self._lock_access(
                    connection,
                    actor,
                    project_id,
                    "knowledge.publish",
                )
                plan = self._plans.get_retrieval_plan_in_transaction(
                    connection,
                    project_id,
                    normalized_plan_id,
                )
                if plan is None:
                    raise KeyError(normalized_plan_id)
                plan_metadata = dict(plan.metadata)
                requested_scope_ids = tuple(scope_ids)
                requested_initial_pack_ids = tuple(initial_evidence_pack_ids)
                if not requested_scope_ids and plan_metadata.get(
                    "targeted_scope_ids"
                ):
                    raw_targeted_scope_ids = plan_metadata.get(
                        "targeted_scope_ids"
                    )
                    if isinstance(raw_targeted_scope_ids, Sequence) and not isinstance(
                        raw_targeted_scope_ids, (str, bytes)
                    ):
                        requested_scope_ids = tuple(
                            str(value) for value in raw_targeted_scope_ids
                        )
                if not requested_initial_pack_ids and plan_metadata.get(
                    "carried_evidence_pack_ids"
                ):
                    raw_carried_pack_ids = plan_metadata.get(
                        "carried_evidence_pack_ids"
                    )
                    if isinstance(raw_carried_pack_ids, Sequence) and not isinstance(
                        raw_carried_pack_ids, (str, bytes)
                    ):
                        requested_initial_pack_ids = tuple(
                            str(value) for value in raw_carried_pack_ids
                        )
                selected_scope_ids, carried_pack_ids = _research_scope_selection(
                    plan,
                    scope_ids=requested_scope_ids,
                    initial_evidence_pack_ids=requested_initial_pack_ids,
                )
                task_id = _required_text(
                    plan.metadata.get("task_id"),
                    "retrieval plan task_id",
                    max_length=200,
                )
                task = self._lock_task(
                    connection,
                    actor=actor,
                    project_id=project_id,
                    task_id=task_id,
                )
                _validate_plan_task(plan, task)
                existing_job = self._matching_job(
                    connection,
                    actor=actor,
                    project_id=project_id,
                    task_id=task_id,
                    action="start",
                    request_id=normalized_request_id,
                    thread_id=thread_id,
                )
                if existing_job is not None:
                    return self._queued_result(
                        connection,
                        project_id=project_id,
                        thread_id=thread_id,
                        job=existing_job,
                    )
                graph_request = ResearchGraphRequest(
                    organization_id=actor.organization_id,
                    project_id=project_id,
                    article_id=plan.article_id,
                    outline_version=plan.outline_version,
                    retrieval_plan_id=plan.retrieval_plan_id,
                    thread_id=thread_id,
                    max_gap_fill_rounds=plan.max_gap_fill_rounds,
                    max_discovery_queries=max_discovery_queries,
                    scope_ids=selected_scope_ids,
                    initial_evidence_pack_ids=carried_pack_ids,
                )
                run = self._runs.create_run_in_transaction(
                    connection,
                    graph_request,
                    metadata={
                        "mode": "server",
                        "task_id": task.id,
                        "request_id": normalized_request_id,
                        "requested_by_user_id": actor.user_id,
                        "generated_from": plan.metadata.get(
                            "generated_from",
                            "confirmed_task_outline",
                        ),
                        "outline_hash": plan.metadata.get(
                            "outline_hash",
                            _outline_hash(task),
                        ),
                        "targeted_repair": plan.metadata.get(
                            "repair_type"
                        )
                        == "targeted_knowledge_gap",
                    },
                )
                self._runs.append_event_in_transaction(
                    connection,
                    project_id=project_id,
                    thread_id=thread_id,
                    event_type="queued",
                    node_name="queued",
                    details={"outline_version": plan.outline_version},
                )
                command = KnowledgeResearchCommand(
                    action="start",
                    request_id=normalized_request_id,
                    thread_id=thread_id,
                    retrieval_plan_id=plan.retrieval_plan_id,
                    outline_version=plan.outline_version,
                    max_discovery_queries=max_discovery_queries,
                    scope_ids=selected_scope_ids,
                    initial_evidence_pack_ids=carried_pack_ids,
                )
                job = self._create_job(
                    connection,
                    project=project,
                    actor=actor,
                    project_id=project_id,
                    task=task,
                    command=command,
                )
                self._append_queued_audit(
                    connection,
                    actor=actor,
                    project_id=project_id,
                    job=job,
                    command=command,
                )
                result = {
                    "run": run,
                    "batch_id": str(job["batch_id"]),
                    "job_id": str(job["id"]),
                    "job": public_job(job),
                }
        except (
            ActiveJobError,
            EvidenceRepositoryError,
            IntegrityError,
            JobConflict,
            KeyError,
            ProjectAccessDenied,
            ResearchRunConflictError,
            ValueError,
        ):
            raise
        except (RuntimeError, SQLAlchemyError) as exc:
            raise ServerKnowledgeResearchUnavailable(
                "knowledge research could not be queued"
            ) from exc
        if project.runner is None:
            raise ServerKnowledgeResearchUnavailable(
                "knowledge research runner did not start"
            )
        project.runner.wake()
        return result

    def enqueue_resume(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        thread_id: str,
        request_id: str,
        approved_candidate_ids: Sequence[str],
    ) -> dict[str, object]:
        if self._execution is None:
            raise ServerKnowledgeResearchUnavailable(
                "knowledge research runner is not configured"
            )
        normalized_thread_id = _required_text(
            thread_id,
            "thread_id",
            max_length=200,
        )
        normalized_request_id = _required_text(
            request_id,
            "request_id",
            max_length=200,
        )
        candidate_ids = tuple(
            dict.fromkeys(str(item).strip() for item in approved_candidate_ids)
        )
        if (
            len(candidate_ids) > MAX_APPROVED_CANDIDATES
            or any(not item or len(item) > 200 for item in candidate_ids)
        ):
            raise ValueError("approved_candidate_ids are invalid")
        self._access.require(actor, project_id, "knowledge.publish")
        project = self._registry.project(
            actor.organization_id,
            project_id,
            start_runner=True,
        )
        try:
            with self._engine.begin() as connection:
                self._lock_access(
                    connection,
                    actor,
                    project_id,
                    "knowledge.publish",
                )
                run = self._runs.lock_run_in_transaction(
                    connection,
                    project_id,
                    normalized_thread_id,
                )
                if run.organization_id != actor.organization_id:
                    raise ResearchRunConflictError(
                        "research run does not belong to the active organization"
                    )
                plan = self._plans.get_retrieval_plan_in_transaction(
                    connection,
                    project_id,
                    run.retrieval_plan_id,
                )
                if plan is None:
                    raise KeyError(run.retrieval_plan_id)
                task_id = _required_text(
                    plan.metadata.get("task_id"),
                    "retrieval plan task_id",
                    max_length=200,
                )
                task = self._lock_task(
                    connection,
                    actor=actor,
                    project_id=project_id,
                    task_id=task_id,
                )
                _validate_plan_task(plan, task)
                existing_job = self._matching_job(
                    connection,
                    actor=actor,
                    project_id=project_id,
                    task_id=task_id,
                    action="resume",
                    request_id=normalized_request_id,
                    thread_id=normalized_thread_id,
                )
                if existing_job is not None:
                    return self._queued_result(
                        connection,
                        project_id=project_id,
                        thread_id=normalized_thread_id,
                        job=existing_job,
                    )
                if run.status != "waiting_for_review":
                    raise ResearchRunConflictError(
                        "research run is not waiting for candidate review"
                    )
                # Resolve private URLs only after checking the durable request
                # receipt. A repeated request can therefore return its first
                # Job even after the graph checkpoint has advanced.
                approved_urls = self._candidate_urls(
                    project_id=project_id,
                    thread_id=normalized_thread_id,
                    approved_candidate_ids=candidate_ids,
                )
                command = KnowledgeResearchCommand(
                    action="resume",
                    request_id=normalized_request_id,
                    thread_id=normalized_thread_id,
                    retrieval_plan_id=plan.retrieval_plan_id,
                    outline_version=plan.outline_version,
                    max_discovery_queries=run.max_discovery_queries,
                    approved_urls=approved_urls,
                )
                job = self._create_job(
                    connection,
                    project=project,
                    actor=actor,
                    project_id=project_id,
                    task=task,
                    command=command,
                )
                self._append_queued_audit(
                    connection,
                    actor=actor,
                    project_id=project_id,
                    job=job,
                    command=command,
                )
                result = {
                    "run": run,
                    "batch_id": str(job["batch_id"]),
                    "job_id": str(job["id"]),
                    "job": public_job(job),
                }
        except (
            ActiveJobError,
            EvidenceRepositoryError,
            IntegrityError,
            JobConflict,
            KeyError,
            ProjectAccessDenied,
            ResearchRunConflictError,
            ResearchRunNotFound,
            ValueError,
        ):
            raise
        except (RuntimeError, SQLAlchemyError) as exc:
            raise ServerKnowledgeResearchUnavailable(
                "knowledge research resume could not be queued"
            ) from exc
        if project.runner is None:
            raise ServerKnowledgeResearchUnavailable(
                "knowledge research runner did not start"
            )
        project.runner.wake()
        return result

    def _candidate_urls(
        self,
        *,
        project_id: str,
        thread_id: str,
        approved_candidate_ids: Sequence[str],
    ) -> tuple[str, ...]:
        if self._execution is None:
            raise ServerKnowledgeResearchUnavailable(
                "knowledge research runner is not configured"
            )
        state = self._execution.checkpoint_state(
            project_id=project_id,
            thread_id=thread_id,
        )
        raw_candidates = state.get("discovered_candidates") or ()
        candidates = {
            str(candidate.get("candidate_id") or ""): str(
                candidate.get("url") or ""
            )
            for candidate in raw_candidates  # type: ignore[union-attr]
            if isinstance(candidate, Mapping)
            and candidate.get("needs_review") is True
            and candidate.get("candidate_id")
            and candidate.get("url")
        }
        if set(approved_candidate_ids) - set(candidates):
            raise ValueError(
                "approved_candidate_ids contain unknown candidates"
            )
        return tuple(candidates[item] for item in approved_candidate_ids)

    def gap_fill_snapshot(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        thread_id: str,
    ) -> dict[str, object]:
        """Return a bounded review projection for an assistant gap-fill gate.

        The graph checkpoint remains the only source for candidate URLs and
        explicit gap reasons.  This method does not expose query receipts,
        snippets, private request bodies, or unreviewed candidates; it merely
        projects the same-site candidates already shown by the knowledge
        workspace so the Assistant can safely bind an approval to a plan.
        """

        normalized_project_id = _required_text(
            project_id,
            "project_id",
            max_length=255,
        )
        normalized_thread_id = _required_text(
            thread_id,
            "thread_id",
            max_length=200,
        )
        self._access.require(actor, normalized_project_id, "knowledge.publish")
        run = self._runs.get_run(normalized_project_id, normalized_thread_id)
        if run is None:
            raise ResearchRunNotFound(normalized_thread_id)
        if run.organization_id != actor.organization_id:
            raise ResearchRunConflictError(
                "research run does not belong to the active organization"
            )
        if self._execution is None:
            raise ServerKnowledgeResearchUnavailable(
                "knowledge research runner is not configured"
            )
        state = self._execution.checkpoint_state(
            project_id=normalized_project_id,
            thread_id=normalized_thread_id,
        )
        raw_candidates = state.get("discovered_candidates") or ()
        candidates: list[dict[str, object]] = []
        for raw in raw_candidates:  # type: ignore[union-attr]
            if not isinstance(raw, Mapping) or raw.get("needs_review") is not True:
                continue
            evidence = raw.get("evidence")
            evidence_map = dict(evidence) if isinstance(evidence, Mapping) else {}
            # The discovery adapters mark every candidate as same-site and
            # route it through one of these two explicit channels. Fail closed
            # if a future adapter omits the marker or reports another channel.
            if evidence_map.get("same_site") is not True:
                continue
            if str(evidence_map.get("channel") or "") not in {
                "official_site",
                "tavily_discovery",
            }:
                continue
            candidate_id = str(raw.get("candidate_id") or "").strip()
            url = str(raw.get("url") or "").strip()
            page_type = str(raw.get("page_type") or "unknown").strip()
            if not candidate_id or not url or not page_type:
                continue
            candidates.append(
                {
                    "candidate_id": candidate_id[:200],
                    "url": url[:4096],
                    "page_type": page_type[:200],
                    "needs_review": True,
                    "evidence": {
                        key: evidence_map[key]
                        for key in (
                            "reason",
                            "channel",
                            "same_site",
                            "score",
                            "reused_attempt",
                        )
                        if key in evidence_map
                    },
                }
            )
            if len(candidates) >= MAX_APPROVED_CANDIDATES:
                break
        raw_reasons = state.get("latest_gap_reasons") or ()
        gap_reasons = tuple(
            str(reason).strip()[:500]
            for reason in raw_reasons  # type: ignore[union-attr]
            if str(reason).strip()
        )
        raw_current_scope = state.get("current_scope_id")
        return {
            "project_id": normalized_project_id,
            "research_thread_id": normalized_thread_id,
            "retrieval_plan_id": run.retrieval_plan_id,
            "status": run.status,
            "current_scope_id": (
                str(raw_current_scope).strip()
                if raw_current_scope
                else run.current_scope_id
            ),
            "gap_reasons": gap_reasons[:20],
            "gap_fill_round": run.gap_fill_round,
            "max_gap_fill_rounds": run.max_gap_fill_rounds,
            "discovery_queries_used": run.discovery_queries_used,
            "max_discovery_queries": run.max_discovery_queries,
            "evidence_pack_ids": list(run.evidence_pack_ids),
            "review_candidates": candidates,
        }

    def _lock_access(
        self,
        connection: Connection,
        actor: ActorIdentity,
        project_id: str,
        permission: Literal["knowledge.edit", "knowledge.publish"],
    ) -> None:
        facts = self._access_repository.lock_project_access_in_connection(
            connection,
            actor,
            project_id,
        )
        if not decide_project_permission(facts, permission).allowed:
            raise ProjectAccessDenied("project access denied")

    @staticmethod
    def _lock_task(
        connection: Connection,
        *,
        actor: ActorIdentity,
        project_id: str,
        task_id: str,
    ) -> TaskRecord:
        payload = connection.execute(
            sa.select(article_tasks.c.payload)
            .where(
                article_tasks.c.organization_id == actor.organization_id,
                article_tasks.c.project_id == project_id,
                article_tasks.c.task_id == task_id,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if payload is None:
            raise KeyError(task_id)
        return TaskRecord.model_validate(dict(payload))

    @staticmethod
    def _matching_job(
        connection: Connection,
        *,
        actor: ActorIdentity,
        project_id: str,
        task_id: str,
        action: str,
        request_id: str,
        thread_id: str,
    ) -> dict[str, object] | None:
        rows = connection.execute(
            sa.select(background_jobs)
            .where(
                background_jobs.c.organization_id == actor.organization_id,
                background_jobs.c.project_id == project_id,
                background_jobs.c.task_id == task_id,
                background_jobs.c.operation == KNOWLEDGE_RESEARCH_OPERATION,
            )
            .order_by(background_jobs.c.created_at.desc())
        ).mappings()
        for row in rows:
            request = dict(row["request"] or {})
            if (
                request.get("action") == action
                and request.get("request_id") == request_id
                and request.get("thread_id") == thread_id
            ):
                return {
                    "id": str(row["job_id"]),
                    "batch_id": str(row["batch_id"]),
                    "task_id": str(row["task_id"]),
                    "operation": str(row["operation"]),
                    "status": str(row["status"]),
                    "source_revision": int(row["source_revision"]),
                    "result_revision": row["result_revision"],
                    "attempts": int(row["attempts"]),
                    "created_at": row["created_at"].isoformat(),
                    "started_at": (
                        None
                        if row["started_at"] is None
                        else row["started_at"].isoformat()
                    ),
                    "finished_at": (
                        None
                        if row["finished_at"] is None
                        else row["finished_at"].isoformat()
                    ),
                    "updated_at": row["updated_at"].isoformat(),
                    "error": str(row["error"] or ""),
                }
        return None

    def _queued_result(
        self,
        connection: Connection,
        *,
        project_id: str,
        thread_id: str,
        job: Mapping[str, object],
    ) -> dict[str, object]:
        run = self._runs.get_run_in_transaction(
            connection,
            project_id,
            thread_id,
        )
        if run is None:
            raise ResearchRunNotFound("research run was not found")
        return {
            "run": run,
            "batch_id": str(job["batch_id"]),
            "job_id": str(job["id"]),
            "job": public_job(job),
        }

    @staticmethod
    def _create_job(
        connection: Connection,
        *,
        project: object,
        actor: ActorIdentity,
        project_id: str,
        task: TaskRecord,
        command: KnowledgeResearchCommand,
    ) -> dict[str, Any]:
        queue = getattr(project, "queue")
        batch = queue.create_batch_in_transaction(
            connection,
            KNOWLEDGE_RESEARCH_OPERATION,
            [
                {
                    "task_id": task.id,
                    "source_revision": task.revision,
                    "customer": project_id,
                    "topic_index": task.topic_index,
                    "request": command.private_values(),
                }
            ],
            customer=project_id,
            requested_by_user_id=actor.user_id,
        )
        return dict(batch["jobs"][0])

    def _append_queued_audit(
        self,
        connection: Connection,
        *,
        actor: ActorIdentity,
        project_id: str,
        job: Mapping[str, object],
        command: KnowledgeResearchCommand,
    ) -> None:
        self._append_audit(
            connection,
            actor=actor,
            project_id=project_id,
            action="knowledge.research.queued",
            target_type="background_job",
            target_id=str(job["id"]),
            details={
                "operation": KNOWLEDGE_RESEARCH_OPERATION,
                "research_action": command.action,
                "thread_id": command.thread_id,
                "retrieval_plan_id": command.retrieval_plan_id,
                "outline_version": command.outline_version,
                "max_discovery_queries": command.max_discovery_queries,
                "approved_candidate_count": len(command.approved_urls),
            },
        )

    def _append_audit(
        self,
        connection: Connection,
        *,
        actor: ActorIdentity,
        project_id: str,
        action: str,
        target_type: str,
        target_id: str,
        details: Mapping[str, object],
    ) -> None:
        identity = "\n".join(
            (
                actor.organization_id,
                project_id,
                action,
                target_type,
                target_id,
            )
        )
        self._audit.append(
            connection,
            AuditEvent(
                organization_id=actor.organization_id,
                event_id="research_"
                + uuid.uuid5(uuid.NAMESPACE_URL, identity).hex,
                actor_user_id=actor.user_id,
                project_id=project_id,
                action=action,
                target_type=target_type,
                target_id=target_id,
                details=dict(details),
            ),
        )


__all__ = [
    "KNOWLEDGE_RESEARCH_OPERATION",
    "KnowledgeResearchCommand",
    "ServerCandidateIngestionAdapter",
    "ServerKnowledgeResearchHandler",
    "ServerKnowledgeResearchRegistry",
    "ServerKnowledgeResearchUnavailable",
    "create_server_research_execution",
    "is_server_generated_retrieval_plan",
]
