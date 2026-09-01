from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from knowledge_agent.assets import PostgresKnowledgeAssetRepository
from knowledge_agent.catalog import PostgresProductCatalogRepository
from knowledge_agent.embedding import EmbeddingProviderError
from knowledge_agent.library import PostgresKnowledgeLibrary
from knowledge_agent.object_storage import ScopedS3ArtifactStore
from knowledge_agent.publication import KnowledgePublicationError
from knowledge_agent.repository import PostgresKnowledgeRepository
from knowledge_agent.schema import (
    knowledge_products,
    knowledge_sources,
    projects,
)
from knowledge_agent.web_ingestion import (
    OfficialSiteScanResult,
    OfficialSiteSyncService,
    OfficialWebPageIngestionService,
    WebPageIngestionConflict,
    WordPressCategorySyncResult,
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
    DEFAULT_PROJECT_JOB_CONCURRENCY,
    authorized_batch_runner,
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
from services.server_web_evidence_ingestion import (
    CheckpointingOfficialSiteFetcher,
    PostgresServerWebEvidenceIngestion,
    ServerWebEvidenceContext,
)
from services.server_knowledge_commands import PostgresServerKnowledgeCommands
from services.project_time import project_now_iso


PRODUCT_REDISCOVERY_OPERATION = "product_rediscovery"
PUBLICATION_RETRY_DELAYS_SECONDS = (1.0, 3.0)


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


@dataclass(frozen=True, slots=True)
class OfficialSiteScanCommand:
    start_url: str
    max_pages: int

    def __post_init__(self) -> None:
        if len(self.start_url) > 4096:
            raise ValueError("start_url is too long")
        if not 1 <= self.max_pages <= 500:
            raise ValueError("max_pages must be between 1 and 500")


@dataclass(frozen=True, slots=True)
class ProductRediscoveryStopReport:
    """Aggregate controlled-shutdown evidence for project runners."""

    project_runner_count: int
    dispatcher_stopped: bool
    remaining_jobs: int

    @property
    def drained(self) -> bool:
        return self.dispatcher_stopped and self.remaining_jobs == 0


@dataclass(frozen=True, slots=True)
class ManualProductScanStatus:
    scan_id: str
    project_id: str
    status: str
    started_at: str
    finished_at: str | None = None
    processed_pages: int = 0
    skipped_pages: int = 0
    processed_products: int = 0
    skipped_products: int = 0
    source_count: int = 0
    product_count: int = 0
    error: str = ""


ProductSyncFactory = Callable[
    [ActorIdentity, str, str, Callable[[], bool]],
    OfficialSiteSyncService,
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
        commands: PostgresServerKnowledgeCommands | None = None,
    ) -> None:
        self._engine = engine
        self._sync_factory = sync_factory
        self._commands = commands

    def _publish(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        result: WordPressCategorySyncResult | OfficialSiteScanResult,
    ) -> None:
        if self._commands is None:
            return
        for page in result.pages:
            self._publish_page(
                actor=actor,
                project_id=project_id,
                page=page,
            )

    def _publish_page(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        page: object,
    ) -> None:
        if self._commands is None:
            return
        receipt_id = "review_" + uuid.uuid5(
                uuid.NAMESPACE_URL,
                "\n".join(
                    (
                        project_id,
                        page.source.source_id,
                        page.snapshot.snapshot_id,
                        "official_site_auto_publish_v1",
                    )
                ),
            ).hex
        try:
            self._commands.review_snapshot(
                    actor=actor,
                    project_id=project_id,
                    source_id=page.source.source_id,
                    snapshot_id=page.snapshot.snapshot_id,
                    receipt_id=receipt_id,
                    source_kind=page.source.source_kind,
                    trust_tier=page.source.trust_tier,
                    decision="approve",
                    reason="Official-site scan completed automatically.",
                    reviewer_kind="automation",
                    reviewer_id="official_site_scan_v1",
            )
        except KnowledgePublicationError:
            # Publication is intentionally idempotent. A stale browser tab
            # or another scan worker may have activated this same immutable
            # snapshot after ingestion but before this review call.
            if not self._snapshot_is_active(
                project_id=project_id,
                source_id=page.source.source_id,
                snapshot_id=page.snapshot.snapshot_id,
            ):
                raise
        else:
            for attempt in range(len(PUBLICATION_RETRY_DELAYS_SECONDS) + 1):
                try:
                    self._commands.publish_source(
                        actor=actor,
                        project_id=project_id,
                        source_id=page.source.source_id,
                        snapshot_id=page.snapshot.snapshot_id,
                    )
                    break
                except EmbeddingProviderError:
                    if attempt >= len(PUBLICATION_RETRY_DELAYS_SECONDS):
                        self._reject_unpublished_page(
                            actor=actor,
                            project_id=project_id,
                            page=page,
                        )
                        raise WebPageIngestionConflict(
                            "page publication skipped after embedding retries"
                        )
                    time.sleep(PUBLICATION_RETRY_DELAYS_SECONDS[attempt])
                except KnowledgePublicationError:
                    if not self._snapshot_is_active(
                        project_id=project_id,
                        source_id=page.source.source_id,
                        snapshot_id=page.snapshot.snapshot_id,
                    ):
                        raise
                    break
            else:  # pragma: no cover - the bounded loop always breaks or raises.
                raise RuntimeError("official-site publication retry exhausted")
        if self._product_is_auto_confirmable(page):
            self._commands.confirm_product(
                actor=actor,
                project_id=project_id,
                product_id=page.product.product_id,
            )

    def _reject_unpublished_page(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        page: object,
    ) -> None:
        assert self._commands is not None
        receipt_id = "review_" + uuid.uuid5(
            uuid.NAMESPACE_URL,
            "\n".join(
                (
                    project_id,
                    page.source.source_id,
                    page.snapshot.snapshot_id,
                    "official_site_publication_failed_v1",
                )
            ),
        ).hex
        self._commands.review_snapshot(
            actor=actor,
            project_id=project_id,
            source_id=page.source.source_id,
            snapshot_id=page.snapshot.snapshot_id,
            receipt_id=receipt_id,
            source_kind=page.source.source_kind,
            trust_tier=page.source.trust_tier,
            decision="reject",
            reason="Automatic publication exhausted transient embedding retries.",
            reviewer_kind="automation",
            reviewer_id="official_site_scan_v1",
        )

    @staticmethod
    def _product_is_auto_confirmable(page: object) -> bool:
        product = getattr(page, "product", None)
        if product is None:
            return False
        classification = getattr(page, "classification", None)
        if classification is None:
            # Compatibility for older internal callers that predate classifier
            # evidence on the result object.
            return True
        if getattr(classification, "page_type", "") != "product_detail":
            return False
        path = urlsplit(str(getattr(classification, "canonical_url", ""))).path
        normalized_path = unquote(path).casefold().rstrip("/")
        segments = [
            unquote(segment).casefold()
            for segment in path.split("/")
            if segment
        ]
        if (
            len(segments) >= 2
            and len(segments[0]) in {2, 3, 5}
            and segments[1]
            in {
                "product",
                "products",
                "produkt",
                "produkte",
                "produit",
                "produits",
                "producto",
                "productos",
                "prodotto",
                "prodotti",
            }
        ):
            return False
        if normalized_path.startswith(("/product/", "/products/")):
            return True
        fixed_non_product_slugs = {
            "home-2",
            "home-3",
            "homepage",
            "live",
            "odm",
            "oem",
            "oem-odm",
            "politica-de-privacidad",
            "politica-sulla-privacy",
            "politique-de-confidentialite",
            "privacy-policy",
            "privacy-policy-2",
            "datenschutz",
            "datenschutzerklarung",
            "thank-you",
            "thanks",
        }
        if normalized_path.startswith("/product-category/") or (
            segments and segments[-1] in fixed_non_product_slugs
        ):
            return False
        # Root-level BJY mould pages do not expose specification tables or a
        # conventional /product/ path. Keep them usable only when the
        # conservative content detector supplied explicit classifier evidence;
        # a generic Product schema alone is not enough for automatic approval.
        reasons = tuple(
            str(reason).casefold()
            for reason in getattr(classification, "reasons", ())
        )
        return any("conservative b2b product-page detector" in reason for reason in reasons)

    def _snapshot_is_active(
        self,
        *,
        project_id: str,
        source_id: str,
        snapshot_id: str,
    ) -> bool:
        with self._engine.connect() as connection:
            row = connection.execute(
                sa.select(
                    knowledge_sources.c.status,
                    knowledge_sources.c.current_snapshot_id,
                    knowledge_sources.c.pending_snapshot_id,
                ).where(
                    knowledge_sources.c.project_id == project_id,
                    knowledge_sources.c.source_id == source_id,
                )
            ).mappings().one_or_none()
        return bool(
            row is not None
            and row["status"] == "published"
            and row["current_snapshot_id"] == snapshot_id
            and row["pending_snapshot_id"] is None
        )

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
        job_id = str(job.get("id") or job.get("job_id") or "").strip()
        requested_by_user_id = str(
            job.get("requested_by_user_id") or ""
        ).strip()
        task_id = str(job.get("task_id") or "").strip()
        source_revision = int(job.get("source_revision") or 0)
        command = ProductRediscoveryCommand.from_mapping(
            dict(job.get("request") or {})
        )
        if not job_id or not requested_by_user_id:
            raise JobConflict("product rediscovery job identity is invalid")
        actor = ActorIdentity(
            organization_id=organization_id,
            user_id=requested_by_user_id,
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
        self.scan(
            actor=actor,
            project_id=project_id,
            scan_id=job_id,
            command=command,
            cancelled=cancelled,
        )
        return source_revision

    def scan(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        scan_id: str,
        command: ProductRediscoveryCommand | OfficialSiteScanCommand,
        cancelled: Callable[[], bool],
    ) -> WordPressCategorySyncResult | OfficialSiteScanResult:
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
        sync = self._sync_factory(actor, project_id, scan_id, cancelled)
        incremental_publish = self._commands is not None and hasattr(
            sync,
            "set_page_ingested_callback",
        )
        if incremental_publish:
            sync.set_page_ingested_callback(
                lambda page: self._publish_page(
                    actor=actor,
                    project_id=project_id,
                    page=page,
                )
            )
        try:
            if isinstance(command, OfficialSiteScanCommand):
                result = sync.sync_site(
                    project_id=project_id,
                    site_url=f"https://{domain}",
                    start_url=(command.start_url or f"https://{domain}/"),
                    max_pages=command.max_pages,
                    known_urls=self._known_official_urls(project_id),
                )
            else:
                result = sync.sync_category(
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
            raise JobCancelled(
                "Product rediscovery cancelled after evidence ingestion."
            )
        if not incremental_publish:
            self._publish(
                actor=actor,
                project_id=project_id,
                result=result,
            )
        return result

    def _known_official_urls(self, project_id: str) -> tuple[str, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(knowledge_sources.c.canonical_url).where(
                    knowledge_sources.c.project_id == project_id,
                    knowledge_sources.c.public_source.is_(True),
                    knowledge_sources.c.canonical_url.is_not(None),
                )
            ).scalars()
            return tuple(str(url) for url in rows if url)


def create_product_sync_factory(
    engine: Engine,
    *,
    store: ObjectStore,
    bucket: str,
) -> ProductSyncFactory:
    """Build project-bound S3 ingestion without a local artifact fallback."""

    def create(
        actor: ActorIdentity,
        project_id: str,
        job_id: str,
        cancelled: Callable[[], bool],
    ) -> WordPressProductSyncService:
        access = ProjectAccessService(
            PostgresProjectAccessRepository(engine)
        )

        def checkpoint() -> None:
            if cancelled():
                raise JobCancelled("Product rediscovery cancelled.")
            access.require(actor, project_id, "knowledge.edit")

        repository = PostgresKnowledgeRepository(engine)
        asset_repository = PostgresKnowledgeAssetRepository(engine)
        catalog_repository = PostgresProductCatalogRepository(engine)
        fetcher = CheckpointingOfficialSiteFetcher(
            SafeOfficialSiteFetcher(),
            checkpoint=checkpoint,
        )
        preparer = OfficialWebPageIngestionService(
            repository=repository,
            asset_repository=asset_repository,
            catalog_repository=catalog_repository,
            artifact_store=ScopedS3ArtifactStore(
                store=store,
                bucket=bucket,
                organization_id=actor.organization_id,
                project_id=project_id,
                checkpoint=checkpoint,
            ),
            fetcher=fetcher,
            snapshot_lookup=PostgresKnowledgeLibrary(engine),
        )
        ingestion = PostgresServerWebEvidenceIngestion(
            engine,
            preparer=preparer,
            context=ServerWebEvidenceContext(
                actor=actor,
                project_id=project_id,
                operation=PRODUCT_REDISCOVERY_OPERATION,
                target_type="background_job",
                target_id=job_id,
                permission="knowledge.edit",
                cancelled=cancelled,
            ),
            bucket=bucket,
            repository=repository,
            assets=asset_repository,
            catalog=catalog_repository,
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
        project_job_concurrency: int = DEFAULT_PROJECT_JOB_CONCURRENCY,
        access_repository: PostgresProjectAccessRepository | None = None,
        audit: AuditEventWriter | None = None,
    ) -> None:
        self._engine = engine
        self._access = access
        self._project_job_concurrency = project_job_concurrency
        self._access_repository = (
            access_repository or PostgresProjectAccessRepository(engine)
        )
        self._audit = audit or PostgresAuditEventWriter()
        self._handler = handler
        self._lock = threading.Lock()
        self._closed = False
        self._projects: dict[tuple[str, str], _ProjectRunner] = {}
        self._manual_scans: dict[tuple[str, str], ManualProductScanStatus] = {}
        self._stop_report: ProductRediscoveryStopReport | None = None

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
                        terminal_audit=self._audit,
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
            runner = authorized_batch_runner(
                queue,
                self._handler,
                access=self._access,
                operations=(PRODUCT_REDISCOVERY_OPERATION,),
                concurrency=self._project_job_concurrency,
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

    def begin_manual_scan(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
    ) -> ManualProductScanStatus:
        self.require_scan(actor=actor, project_id=project_id)
        scope = (actor.organization_id, project_id)
        with self._lock:
            current = self._manual_scans.get(scope)
            if current is not None and current.status == "running":
                raise ActiveJobError(f"knowledge-scan:{project_id}")
            status = ManualProductScanStatus(
                scan_id=f"manual_{uuid.uuid4().hex}",
                project_id=project_id,
                status="running",
                started_at=project_now_iso(),
                product_count=self._confirmed_product_count(project_id),
            )
            self._manual_scans[scope] = status
            return status

    def run_manual_scan(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        scan_id: str,
        command: OfficialSiteScanCommand,
    ) -> ManualProductScanStatus:
        handler = self._handler
        assert isinstance(handler, ServerProductRediscoveryHandler)
        scope = (actor.organization_id, project_id)
        try:
            result = handler.scan(
                actor=actor,
                project_id=project_id,
                scan_id=scan_id,
                command=command,
                cancelled=lambda: False,
            )
            finished = ManualProductScanStatus(
                scan_id=scan_id,
                project_id=project_id,
                status="succeeded",
                started_at=self._scan_started_at(scope, scan_id),
                finished_at=project_now_iso(),
                processed_pages=len(result.pages),
                skipped_pages=len(result.skipped_urls),
                processed_products=len(result.products),
                skipped_products=sum(
                    1
                    for url in result.skipped_urls
                    if "/product" in url.casefold()
                ),
                source_count=self._published_source_count(project_id),
                product_count=self._confirmed_product_count(project_id),
            )
        except Exception as exc:
            finished = ManualProductScanStatus(
                scan_id=scan_id,
                project_id=project_id,
                status="failed",
                started_at=self._scan_started_at(scope, scan_id),
                finished_at=project_now_iso(),
                source_count=self._published_source_count(project_id),
                product_count=self._confirmed_product_count(project_id),
                error=str(exc)[:500],
            )
            with self._lock:
                self._manual_scans[scope] = finished
            raise
        with self._lock:
            self._manual_scans[scope] = finished
        return finished

    def manual_scan_status(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
    ) -> ManualProductScanStatus | None:
        self._access.require(actor, project_id, "project.view")
        with self._lock:
            return self._manual_scans.get((actor.organization_id, project_id))

    def _scan_started_at(self, scope: tuple[str, str], scan_id: str) -> str:
        with self._lock:
            current = self._manual_scans.get(scope)
            if current is not None and current.scan_id == scan_id:
                return current.started_at
        return project_now_iso()

    def _confirmed_product_count(self, project_id: str) -> int:
        with self._engine.connect() as connection:
            return int(
                connection.execute(
                    sa.select(sa.func.count())
                    .select_from(knowledge_products)
                    .where(
                        knowledge_products.c.project_id == project_id,
                        knowledge_products.c.status == "confirmed",
                    )
                ).scalar_one()
            )

    def _published_source_count(self, project_id: str) -> int:
        with self._engine.connect() as connection:
            return int(
                connection.execute(
                    sa.select(sa.func.count())
                    .select_from(knowledge_sources)
                    .where(
                        knowledge_sources.c.project_id == project_id,
                        knowledge_sources.c.status == "published",
                    )
                ).scalar_one()
            )

    def require_scan(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
    ) -> None:
        self._access.require(actor, project_id, "knowledge.publish")
        if self._closed or not isinstance(
            self._handler,
            ServerProductRediscoveryHandler,
        ):
            raise ProductRediscoveryUnavailable(
                "product rediscovery runner is not configured"
            )

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

    def stop(
        self,
        *,
        timeout_seconds: float = 10.0,
    ) -> ProductRediscoveryStopReport:
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative")
        with self._lock:
            if self._closed:
                return self._stop_report or ProductRediscoveryStopReport(
                    project_runner_count=0,
                    dispatcher_stopped=True,
                    remaining_jobs=0,
                )
            self._closed = True
            runners = [
                project.runner
                for project in self._projects.values()
                if project.runner is not None
            ]
            self._projects.clear()
        deadline = time.monotonic() + timeout_seconds
        dispatcher_stopped = True
        remaining_jobs = 0
        for runner in runners:
            report = runner.stop(
                timeout_seconds=max(
                    0.0,
                    deadline - time.monotonic(),
                )
            )
            dispatcher_stopped = (
                dispatcher_stopped and report.dispatcher_stopped
            )
            remaining_jobs += report.remaining_jobs
        result = ProductRediscoveryStopReport(
            project_runner_count=len(runners),
            dispatcher_stopped=dispatcher_stopped,
            remaining_jobs=remaining_jobs,
        )
        with self._lock:
            self._stop_report = result
        return result


__all__ = [
    "PRODUCT_REDISCOVERY_OPERATION",
    "OfficialSiteScanCommand",
    "ProductRediscoveryCommand",
    "ManualProductScanStatus",
    "ProductRediscoveryStopReport",
    "ProductRediscoveryUnavailable",
    "ServerProductRediscoveryHandler",
    "ServerProductRediscoveryRegistry",
    "create_product_sync_factory",
]
