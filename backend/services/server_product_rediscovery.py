from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from knowledge_agent.assets import PostgresKnowledgeAssetRepository
from knowledge_agent.catalog import PostgresProductCatalogRepository
from knowledge_agent.library import PostgresKnowledgeLibrary
from knowledge_agent.object_storage import ScopedS3ArtifactStore
from knowledge_agent.repository import PostgresKnowledgeRepository
from knowledge_agent.schema import projects
from knowledge_agent.web_ingestion import (
    OfficialWebPageIngestionService,
    WordPressProductSyncService,
)
from knowledge_agent.wordpress import (
    OfficialSiteFetchError,
    SafeOfficialSiteFetcher,
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
from services.authorized_job_queue import (
    AuthorizedPostgresJobQueue,
    ReauthorizingJobHandler,
)
from services.job_queue import (
    ACTIVE_JOB_STATUSES,
    ActiveJobError,
    BatchJobRunner,
    JobCancelled,
    JobConflict,
)
from services.object_store import ObjectStore
from services.postgres_job_queue import PostgresJobQueue
from services.postgres_task_repository import PostgresTaskRepository


PRODUCT_REDISCOVERY_OPERATION = "product_rediscovery"


class ProductRediscoveryUnavailable(RuntimeError):
    """The scoped server rediscovery runner cannot accept work."""


@dataclass(frozen=True, slots=True)
class ProductRediscoveryCommand:
    category_url: str
    max_products: int

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, object],
    ) -> ProductRediscoveryCommand:
        category_url = str(value.get("category_url") or "").strip()
        if not category_url or len(category_url) > 4096:
            raise JobConflict("product rediscovery request is invalid")
        raw_limit = value.get("max_products", 12)
        if isinstance(raw_limit, bool):
            raise JobConflict("product rediscovery request is invalid")
        try:
            max_products = int(raw_limit)
        except (TypeError, ValueError) as exc:
            raise JobConflict(
                "product rediscovery request is invalid"
            ) from exc
        if not 1 <= max_products <= 50:
            raise JobConflict("product rediscovery request is invalid")
        return cls(
            category_url=category_url,
            max_products=max_products,
        )

    def private_values(self) -> dict[str, object]:
        return {
            "category_url": self.category_url,
            "max_products": self.max_products,
        }


ProductSyncFactory = Callable[
    [str, str],
    WordPressProductSyncService,
]
ProductRediscoveryJobHandler = Callable[
    [dict[str, Any], Callable[[], bool]],
    int,
]


class ServerProductRediscoveryHandler:
    """Execute one project-scoped official-site discovery job."""

    def __init__(
        self,
        engine: Engine,
        *,
        sync_factory: ProductSyncFactory,
    ) -> None:
        self._engine = engine
        self._sync_factory = sync_factory

    def __call__(
        self,
        job: dict[str, Any],
        cancelled: Callable[[], bool],
    ) -> int:
        if str(job.get("operation") or "") != (
            PRODUCT_REDISCOVERY_OPERATION
        ):
            raise JobConflict("unsupported server job operation")
        organization_id = str(
            job.get("organization_id") or ""
        ).strip()
        project_id = str(job.get("project_id") or "").strip()
        task_id = str(job.get("task_id") or "").strip()
        source_revision = int(job.get("source_revision") or 0)
        command = ProductRediscoveryCommand.from_mapping(
            dict(job.get("request") or {})
        )
        if cancelled():
            raise JobCancelled(
                "Product rediscovery cancelled before execution."
            )
        task = PostgresTaskRepository(
            self._engine,
            organization_id=organization_id,
            project_id=project_id,
        ).get(task_id)
        if task is None or int(task.get("revision") or 0) != (
            source_revision
        ):
            raise JobConflict("source task revision changed")
        with self._engine.connect() as connection:
            domain = connection.execute(
                sa.select(projects.c.official_domain).where(
                    projects.c.project_id == project_id,
                    projects.c.status == "active",
                )
            ).scalar_one_or_none()
        if domain is None:
            raise JobConflict("active project was not found")
        if cancelled():
            raise JobCancelled(
                "Product rediscovery cancelled before official-site fetch."
            )
        sync = self._sync_factory(organization_id, project_id)
        try:
            sync.sync_category(
                project_id=project_id,
                site_url=f"https://{domain}",
                category_url=command.category_url,
                max_products=command.max_products,
            )
        except OfficialSiteFetchError as exc:
            if "could not be fetched" in str(exc).casefold():
                raise RuntimeError(
                    "official-site fetch is temporarily unavailable"
                ) from exc
            raise
        if cancelled():
            # Immutable Inbox evidence already written before a late cancel is
            # retained; it is not published and never replaces Task products.
            raise JobCancelled(
                "Product rediscovery cancelled after evidence ingestion."
            )
        return source_revision


def create_product_sync_factory(
    engine: Engine,
    *,
    store: ObjectStore,
    bucket: str,
) -> ProductSyncFactory:
    """Build project-bound S3 ingestion without a local artifact fallback."""

    def create(
        organization_id: str,
        project_id: str,
    ) -> WordPressProductSyncService:
        repository = PostgresKnowledgeRepository(engine)
        asset_repository = PostgresKnowledgeAssetRepository(engine)
        catalog_repository = PostgresProductCatalogRepository(engine)
        fetcher = SafeOfficialSiteFetcher()
        ingestion = OfficialWebPageIngestionService(
            repository=repository,
            asset_repository=asset_repository,
            catalog_repository=catalog_repository,
            artifact_store=ScopedS3ArtifactStore(
                store=store,
                bucket=bucket,
                organization_id=organization_id,
                project_id=project_id,
            ),
            fetcher=fetcher,
            snapshot_lookup=PostgresKnowledgeLibrary(engine),
        )
        return WordPressProductSyncService(
            fetcher=fetcher,
            page_ingestion=ingestion,
        )

    return create


@dataclass(slots=True)
class _ProjectRunner:
    queue: PostgresJobQueue
    runner: BatchJobRunner | None


class ServerProductRediscoveryRegistry:
    """Lazily run one strictly scoped queue per active Project.

    This is an explicit M7 migration adapter. A later global dispatcher may
    replace the per-project threads, but it must preserve the fixed
    Organization/Project queue, requester identity, and both authorization
    checks.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        access: ProjectAccessService,
        handler: ProductRediscoveryJobHandler | None,
        access_repository: PostgresProjectAccessRepository | None = None,
        audit: AuditEventWriter | None = None,
    ) -> None:
        self._engine = engine
        self._access = access
        self._access_repository = (
            access_repository or PostgresProjectAccessRepository(engine)
        )
        self._audit = audit or PostgresAuditEventWriter()
        self._handler = handler
        self._lock = threading.Lock()
        self._closed = False
        self._projects: dict[tuple[str, str], _ProjectRunner] = {}

    def _ensure_project(
        self,
        organization_id: str,
        project_id: str,
        *,
        start_runner: bool,
    ) -> _ProjectRunner:
        scope = (organization_id, project_id)
        with self._lock:
            if self._closed:
                raise ProductRediscoveryUnavailable(
                    "product rediscovery runner is stopped"
                )
            current = self._projects.get(scope)
            if current is not None and (
                not start_runner or current.runner is not None
            ):
                return current
            if current is None:
                current = _ProjectRunner(
                    queue=PostgresJobQueue(
                        self._engine,
                        organization_id=organization_id,
                        project_id=project_id,
                    ),
                    runner=None,
                )
                self._projects[scope] = current
            if not start_runner:
                return current
            if self._handler is None:
                raise ProductRediscoveryUnavailable(
                    "product rediscovery runner is not configured"
                )
            queue = current.queue
            authorized_queue = AuthorizedPostgresJobQueue(
                queue,
                access=self._access,
            )
            runner = BatchJobRunner(
                authorized_queue,
                ReauthorizingJobHandler(
                    self._handler,
                    access=self._access,
                ),
                concurrency=1,
                operations=(PRODUCT_REDISCOVERY_OPERATION,),
            )
            current.runner = runner
            try:
                runner.start()
            except Exception:
                current.runner = None
                runner.stop()
                raise
            return current

    def start_existing(self) -> None:
        if self._handler is None:
            return
        with self._engine.connect() as connection:
            scopes = connection.execute(
                sa.select(
                    background_jobs.c.organization_id,
                    background_jobs.c.project_id,
                )
                .where(
                    background_jobs.c.operation
                    == PRODUCT_REDISCOVERY_OPERATION,
                    background_jobs.c.status.in_(
                        ACTIVE_JOB_STATUSES
                    ),
                )
                .distinct()
            ).all()
        for organization_id, project_id in scopes:
            project = self._ensure_project(
                str(organization_id),
                str(project_id),
                start_runner=True,
            )
            if project.runner is None:
                raise ProductRediscoveryUnavailable(
                    "product rediscovery runner did not start"
                )
            project.runner.wake()

    def enqueue(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        task_id: str,
        source_revision: int,
        command: ProductRediscoveryCommand,
    ) -> dict[str, object]:
        # Cheap precheck avoids allocating a runner for an unauthorized scope.
        # The same permission is locked and re-evaluated in the transaction.
        self._access.require(actor, project_id, "knowledge.edit")
        project = self._ensure_project(
            actor.organization_id,
            project_id,
            start_runner=True,
        )
        try:
            with self._engine.begin() as connection:
                facts = (
                    self._access_repository.lock_project_access_in_connection(
                        connection,
                        actor,
                        project_id,
                    )
                )
                if not decide_project_permission(
                    facts,
                    "knowledge.edit",
                ).allowed:
                    raise ProjectAccessDenied("project access denied")
                task = connection.execute(
                    sa.select(
                        article_tasks.c.revision,
                        article_tasks.c.topic_index,
                    )
                    .where(
                        article_tasks.c.organization_id
                        == actor.organization_id,
                        article_tasks.c.project_id == project_id,
                        article_tasks.c.task_id == task_id,
                    )
                    .with_for_update()
                ).one_or_none()
                if task is None:
                    raise KeyError(task_id)
                if int(task.revision) != source_revision:
                    raise JobConflict("source task revision changed")
                batch = project.queue.create_batch_in_transaction(
                    connection,
                    PRODUCT_REDISCOVERY_OPERATION,
                    [
                        {
                            "task_id": task_id,
                            "source_revision": source_revision,
                            "customer": project_id,
                            "topic_index": int(task.topic_index),
                            "request": command.private_values(),
                        }
                    ],
                    customer=project_id,
                    requested_by_user_id=actor.user_id,
                )
                job = batch["jobs"][0]
                job_id = str(job["id"])
                identity = "\n".join(
                    (
                        actor.organization_id,
                        project_id,
                        job_id,
                        PRODUCT_REDISCOVERY_OPERATION,
                    )
                )
                self._audit.append(
                    connection,
                    AuditEvent(
                        organization_id=actor.organization_id,
                        event_id=(
                            "job_"
                            + uuid.uuid5(
                                uuid.NAMESPACE_URL,
                                identity,
                            ).hex
                        ),
                        actor_user_id=actor.user_id,
                        project_id=project_id,
                        action="knowledge.products.rediscovery.queued",
                        target_type="background_job",
                        target_id=job_id,
                        details={
                            "operation": PRODUCT_REDISCOVERY_OPERATION,
                            "source_revision": source_revision,
                            "max_products": command.max_products,
                        },
                    ),
                )
        except (
            ActiveJobError,
            JobConflict,
            KeyError,
            ProjectAccessDenied,
        ):
            raise
        except (RuntimeError, SQLAlchemyError) as exc:
            raise ProductRediscoveryUnavailable(
                "product rediscovery could not be queued"
            ) from exc
        if project.runner is None:
            raise ProductRediscoveryUnavailable(
                "product rediscovery runner did not start"
            )
        project.runner.wake()
        return self._public_job(job)

    def get_job(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        task_id: str,
        job_id: str,
    ) -> dict[str, object]:
        self._access.require(actor, project_id, "project.view")
        project = self._ensure_project(
            actor.organization_id,
            project_id,
            start_runner=False,
        )
        job = project.queue.get_job(job_id)
        if (
            str(job["task_id"]) != task_id
            or str(job["operation"]) != PRODUCT_REDISCOVERY_OPERATION
        ):
            raise KeyError(job_id)
        return self._public_job(job)

    @staticmethod
    def _public_job(job: Mapping[str, object]) -> dict[str, object]:
        def optional_text(value: object) -> str | None:
            normalized = "" if value is None else str(value).strip()
            return normalized or None

        return {
            "job_id": str(job["id"]),
            "batch_id": str(job["batch_id"]),
            "task_id": str(job["task_id"]),
            "operation": str(job["operation"]),
            "status": str(job["status"]),
            "source_revision": int(job["source_revision"]),
            "result_revision": (
                None
                if job.get("result_revision") is None
                else int(job["result_revision"])
            ),
            "attempts": int(job["attempts"]),
            "created_at": str(job["created_at"]),
            "started_at": optional_text(job.get("started_at")),
            "finished_at": optional_text(job.get("finished_at")),
            "updated_at": str(job["updated_at"]),
            "has_error": bool(str(job.get("error") or "")),
        }

    def stop(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            runners = [
                project.runner
                for project in self._projects.values()
                if project.runner is not None
            ]
            self._projects.clear()
        for runner in runners:
            runner.stop()


__all__ = [
    "PRODUCT_REDISCOVERY_OPERATION",
    "ProductRediscoveryCommand",
    "ProductRediscoveryUnavailable",
    "ServerProductRediscoveryHandler",
    "ServerProductRediscoveryRegistry",
    "create_product_sync_factory",
]
