from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from typing import Callable, Literal, Mapping, Protocol, cast, get_args

from sqlalchemy.engine import Engine

from knowledge_agent.ingestion import DocumentInput
from models import PromptKind
from services.access_control import ActorIdentity, ProjectAccessService
from services.object_store import ObjectStore, ObjectStoreError
from services.server_private_document_ingestion import (
    PostgresServerPrivateDocumentIngestion,
)
from services.server_project_metadata import PostgresServerProjectMetadata
from services.server_project_prompts import PostgresProjectPromptService
from services.server_project_topics import (
    PostgresServerProjectTopicService,
    ServerProjectTopicRow,
)
from services.server_task_intake import (
    PostgresServerTaskIntakeService,
    ServerTaskIntakeRow,
)
from workflow_assistant.attachments import AssistantAttachment, MAX_ATTACHMENT_BYTES
from workflow_assistant.import_proposals import (
    ImportProposal,
    ImportTargetKind,
    normalized_json_object,
)


ImportCompletionStatus = Literal["completed", "waiting_publication"]
ImportEntityAction = Literal["create", "update", "skip"]


class TypedImportError(RuntimeError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ImportEntityRef:
    entity_type: str
    entity_id: str
    action: ImportEntityAction
    revision: int | None = None
    reason: str | None = None

    def public_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "action": self.action,
        }
        if self.revision is not None:
            value["revision"] = self.revision
        if self.reason is not None:
            value["reason"] = self.reason
        return value


@dataclass(frozen=True, slots=True)
class TypedImportResult:
    status: ImportCompletionStatus
    entity_refs: tuple[ImportEntityRef, ...]

    @property
    def created_count(self) -> int:
        return sum(item.action == "create" for item in self.entity_refs)

    @property
    def updated_count(self) -> int:
        return sum(item.action == "update" for item in self.entity_refs)

    @property
    def skipped_count(self) -> int:
        return sum(item.action == "skip" for item in self.entity_refs)


@dataclass(frozen=True, slots=True)
class TypedImportRequest:
    actor: ActorIdentity
    proposal: ImportProposal
    attachment: AssistantAttachment
    expected_proposal_revision: int
    idempotency_key: str


class TypedImportAdapter(Protocol):
    target_kind: ImportTargetKind

    def execute(self, request: TypedImportRequest) -> TypedImportResult: ...


def _objects(diff: Mapping[str, object], name: str) -> tuple[dict[str, object], ...]:
    value = diff.get(name)
    if not isinstance(value, list):
        raise TypedImportError(
            f"proposal {name} bucket is invalid",
            code="invalid_import_diff",
        )
    items: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise TypedImportError(
                f"proposal {name} entry is invalid",
                code="invalid_import_diff",
            )
        items.append(normalized_json_object(item))
    return tuple(items)


def _required(value: object, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise TypedImportError(
            f"{field_name} is required",
            code="invalid_import_diff",
        )
    return normalized


def _stable_key(prefix: str, request: TypedImportRequest) -> str:
    # A confirmed Proposal revision is the business operation identity. The
    # caller key deduplicates enqueue attempts, but changing that transport key
    # must never create a second set of formal records for the same Proposal.
    identity = "\n".join(
        (
            request.actor.organization_id,
            request.proposal.proposal_id,
            str(request.proposal.revision),
        )
    )
    return f"{prefix}_{uuid.uuid5(uuid.NAMESPACE_URL, identity).hex}"


def _skip_refs(
    items: tuple[dict[str, object], ...],
    *,
    entity_type: str,
    fallback_id: str,
) -> tuple[ImportEntityRef, ...]:
    return tuple(
        ImportEntityRef(
            entity_type=entity_type,
            entity_id=str(item.get("existing_id") or fallback_id),
            action="skip",
            reason=str(item.get("reason") or "excluded_or_existing"),
        )
        for item in items
    )


def _reject_nonempty_update(diff: Mapping[str, object]) -> None:
    if _objects(diff, "update"):
        raise TypedImportError(
            "this import target does not support update entries",
            code="unsupported_import_update",
        )


def _reject_nonempty_bucket(
    diff: Mapping[str, object],
    bucket: str,
    *,
    code: str = "unsupported_import_shape",
) -> None:
    if _objects(diff, bucket):
        raise TypedImportError(
            f"this import target does not support {bucket} entries",
            code=code,
        )


def build_default_import_executor(
    *,
    engine: Engine,
    access: ProjectAccessService,
    object_store: ObjectStore,
    ingestion: PostgresServerPrivateDocumentIngestion,
) -> TypedImportExecutor:
    """Build the Server-only adapters from existing project services."""

    def prompt_factory(
        organization_id: str,
        project_id: str,
    ) -> PostgresProjectPromptService:
        return PostgresProjectPromptService(
            engine,
            organization_id=organization_id,
            project_id=project_id,
        )

    def task_factory(
        organization_id: str,
        project_id: str,
    ) -> PostgresServerTaskIntakeService:
        return PostgresServerTaskIntakeService(
            engine,
            organization_id=organization_id,
            project_id=project_id,
        )

    return TypedImportExecutor(
        access=access,
        adapters=(
            KnowledgeSourceImportAdapter(
                object_store=object_store,
                ingestion=ingestion,
            ),
            PromptAssetImportAdapter(prompt_factory),
            TaskWorkbookImportAdapter(task_factory),
            ProjectNotesImportAdapter(PostgresServerProjectMetadata(engine)),
            TopicLibraryImportAdapter(PostgresServerProjectTopicService(engine)),
        ),
    )


class TypedImportExecutor:
    """Validate one confirmed Proposal and route it to one typed adapter.

    Every concrete adapter delegates writes to a Server service whose business
    transaction reauthorizes the actor and appends its Audit event. The
    executor never interprets attachment text as commands.
    """

    def __init__(
        self,
        *,
        access: ProjectAccessService,
        adapters: tuple[TypedImportAdapter, ...],
    ) -> None:
        self._access = access
        self._adapters = {adapter.target_kind: adapter for adapter in adapters}
        if len(self._adapters) != len(adapters):
            raise ValueError("typed import adapters must have unique target kinds")

    def execute(self, request: TypedImportRequest) -> TypedImportResult:
        proposal = request.proposal
        actor = request.actor
        key = str(request.idempotency_key or "").strip()
        if not key or len(key) > 255:
            raise TypedImportError(
                "idempotency_key is required and must not exceed 255 characters",
                code="invalid_import_request",
            )
        if (
            proposal.organization_id != actor.organization_id
            or proposal.creator_user_id != actor.user_id
            or request.attachment.organization_id != actor.organization_id
            or request.attachment.creator_user_id != actor.user_id
            or proposal.attachment_id != request.attachment.attachment_id
        ):
            raise TypedImportError(
                "import proposal is outside the actor scope",
                code="import_scope_conflict",
            )
        if proposal.revision != request.expected_proposal_revision:
            raise TypedImportError(
                "import proposal revision changed",
                code="import_proposal_revision_conflict",
            )
        if proposal.status not in {"confirmed", "running"} or proposal.target_project_id is None:
            raise TypedImportError(
                "import proposal is not confirmed",
                code="import_proposal_status_conflict",
            )
        diff = proposal.normalized_diff
        allowed_fields = {
            "schema_version",
            "target_project_id",
            "target_kind",
            "source",
            "requires_publication_review",
            "prompt_kind",
            "workbook",
            "create",
            "update",
            "skip",
            "conflicts",
            "invalid",
        }
        if set(diff).difference(allowed_fields):
            raise TypedImportError(
                "import proposal contains unknown fields",
                code="invalid_import_diff",
            )
        if (
            diff.get("schema_version") != 1
            or diff.get("target_project_id") != proposal.target_project_id
            or diff.get("target_kind") != proposal.target_kind
        ):
            raise TypedImportError(
                "import proposal target metadata changed",
                code="invalid_import_diff",
            )
        source = diff.get("source")
        if (
            not isinstance(source, Mapping)
            or source.get("attachment_id") != request.attachment.attachment_id
            or (
                source.get("sha256") is not None
                and source.get("sha256") != request.attachment.sha256
            )
        ):
            raise TypedImportError(
                "import proposal source metadata changed",
                code="invalid_import_diff",
            )
        if _objects(diff, "conflicts") or _objects(diff, "invalid"):
            raise TypedImportError(
                "unresolved proposal entries cannot be imported",
                code="unresolved_import_diff",
            )
        workbook = diff.get("workbook")
        if isinstance(workbook, Mapping) and bool(workbook.get("truncated")):
            raise TypedImportError(
                "truncated workbook previews cannot be imported",
                code="truncated_import_diff",
            )
        adapter = self._adapters.get(proposal.target_kind)
        if adapter is None:
            raise TypedImportError(
                "typed import adapter is unavailable",
                code="import_adapter_unavailable",
            )
        permission = (
            "knowledge.edit"
            if proposal.target_kind == "knowledge_source"
            else "article.edit"
        )
        self._access.require(actor, proposal.target_project_id, permission)  # type: ignore[arg-type]
        return adapter.execute(request)


class KnowledgeSourceImportAdapter:
    target_kind: ImportTargetKind = "knowledge_source"

    def __init__(
        self,
        *,
        object_store: ObjectStore,
        ingestion: PostgresServerPrivateDocumentIngestion,
    ) -> None:
        self._store = object_store
        self._ingestion = ingestion

    def execute(self, request: TypedImportRequest) -> TypedImportResult:
        _reject_nonempty_update(request.proposal.normalized_diff)
        create = _objects(request.proposal.normalized_diff, "create")
        if _objects(request.proposal.normalized_diff, "update"):
            raise TypedImportError(
                "knowledge imports do not accept update entries",
                code="invalid_import_diff",
            )
        skip = _objects(request.proposal.normalized_diff, "skip")
        if not create:
            return TypedImportResult(
                status="waiting_publication",
                entity_refs=_skip_refs(
                    skip,
                    entity_type="knowledge_source",
                    fallback_id=request.attachment.sha256,
                ),
            )
        if len(create) != 1:
            raise TypedImportError(
                "knowledge proposal must contain one source",
                code="invalid_import_diff",
            )
        try:
            content = self._store.get(
                request.attachment.object_key,
                max_bytes=MAX_ATTACHMENT_BYTES,
            )
        except ObjectStoreError as exc:
            raise TypedImportError(
                "knowledge attachment is unavailable",
                code="attachment_object_unavailable",
            ) from exc
        if (
            len(content) != request.attachment.byte_size
            or hashlib.sha256(content).hexdigest() != request.attachment.sha256
        ):
            raise TypedImportError(
                "knowledge attachment integrity check failed",
                code="attachment_integrity_failed",
            )
        project_id = cast(str, request.proposal.target_project_id)
        uploaded = self._ingestion.upload(
            actor=request.actor,
            project_id=project_id,
            source_id=_stable_key("assistant", request),
            display_name=_required(create[0].get("title"), "title"),
            document_input=DocumentInput(
                filename=request.attachment.original_filename,
                content=content,
                content_type=request.attachment.mime_type,
            ),
            trust_tier="reference_material",
        )
        result = uploaded.result
        return TypedImportResult(
            status="waiting_publication",
            entity_refs=(
                ImportEntityRef(
                    entity_type="knowledge_source",
                    entity_id=result.source.source_id,
                    action="create" if uploaded.created else "skip",
                    reason=None if uploaded.created else "immutable_source_exists",
                ),
                ImportEntityRef(
                    entity_type="knowledge_snapshot",
                    entity_id=result.snapshot.snapshot_id,
                    action="create" if uploaded.created else "skip",
                    reason=None if uploaded.created else "immutable_snapshot_exists",
                ),
                *_skip_refs(
                    skip,
                    entity_type="knowledge_source",
                    fallback_id=request.attachment.sha256,
                ),
            ),
        )


PromptServiceFactory = Callable[[str, str], PostgresProjectPromptService]


class PromptAssetImportAdapter:
    target_kind: ImportTargetKind = "prompt_asset"

    def __init__(self, factory: PromptServiceFactory) -> None:
        self._factory = factory

    def execute(self, request: TypedImportRequest) -> TypedImportResult:
        diff = request.proposal.normalized_diff
        _reject_nonempty_update(diff)
        create = _objects(diff, "create")
        if _objects(diff, "update"):
            raise TypedImportError(
                "prompt imports require a new prompt entry",
                code="invalid_import_diff",
            )
        skip = _objects(diff, "skip")
        if len(create) > 1:
            raise TypedImportError(
                "prompt imports accept one prompt at a time",
                code="invalid_import_diff",
            )
        declared_kind = diff.get("prompt_kind")
        if declared_kind not in get_args(PromptKind):
            raise TypedImportError(
                "prompt imports require an explicit prompt kind",
                code="invalid_import_diff",
            )
        project_id = cast(str, request.proposal.target_project_id)
        service = self._factory(request.actor.organization_id, project_id)
        refs = list(
            _skip_refs(skip, entity_type="project_prompt", fallback_id="existing")
        )
        for item in create:
            item_kind = _required(item.get("prompt_kind"), "prompt_kind")
            if item_kind not in get_args(PromptKind) or item_kind != declared_kind:
                raise TypedImportError(
                    "prompt entry kind does not match the proposal",
                    code="invalid_import_diff",
                )
            kind = cast(PromptKind, item_kind)
            name = _required(item.get("name"), "name")
            content = _required(item.get("content"), "content")
            exact = next(
                (
                    candidate.snapshot
                    for candidate in service.list(request.actor).prompts
                    if candidate.snapshot.kind == kind
                    and candidate.snapshot.content.strip() == content
                ),
                None,
            )
            snapshot = exact or service.create_imported(
                request.actor,
                prompt_id=_stable_key("assistant_prompt", request),
                name=name,
                kind=kind,
                content=content,
            )
            refs.append(
                ImportEntityRef(
                    entity_type="project_prompt",
                    entity_id=snapshot.prompt_id,
                    action="skip" if exact is not None else "create",
                    revision=snapshot.version,
                    reason=("identical_prompt_exists" if exact is not None else None),
                )
            )
        return TypedImportResult(status="completed", entity_refs=tuple(refs))


TaskIntakeFactory = Callable[[str, str], PostgresServerTaskIntakeService]


class TaskWorkbookImportAdapter:
    target_kind: ImportTargetKind = "task_workbook"

    def __init__(self, factory: TaskIntakeFactory) -> None:
        self._factory = factory

    def execute(self, request: TypedImportRequest) -> TypedImportResult:
        _reject_nonempty_update(request.proposal.normalized_diff)
        create = _objects(request.proposal.normalized_diff, "create")
        if _objects(request.proposal.normalized_diff, "update"):
            raise TypedImportError(
                "task workbook imports do not accept update entries",
                code="invalid_import_diff",
            )
        skip = _objects(request.proposal.normalized_diff, "skip")
        refs = list(_skip_refs(skip, entity_type="article_task", fallback_id="existing"))
        if create:
            rows = tuple(
                ServerTaskIntakeRow(
                    topic=_required(item.get("topic"), "topic"),
                    primary_keyword=str(item.get("primary_keyword") or ""),
                    competitor_keyword=str(item.get("competitor_keyword") or ""),
                    competitor_blog=str(item.get("competitor_blog") or ""),
                )
                for item in create
            )
            project_id = cast(str, request.proposal.target_project_id)
            result = self._factory(
                request.actor.organization_id,
                project_id,
            ).import_rows(
                actor=request.actor,
                intake_id=_stable_key("assistant_import", request),
                source_name=request.attachment.original_filename,
                rows=rows,
            )
            refs.extend(
                ImportEntityRef(
                    entity_type="article_task",
                    entity_id=task.id,
                    action="create" if result.created else "skip",
                    revision=int(getattr(task, "revision", 0)),
                    reason=None if result.created else "task_intake_exists",
                )
                for task in result.tasks
            )
        return TypedImportResult(status="completed", entity_refs=tuple(refs))


class ProjectNotesImportAdapter:
    target_kind: ImportTargetKind = "project_notes"

    def __init__(self, service: PostgresServerProjectMetadata) -> None:
        self._service = service

    def execute(self, request: TypedImportRequest) -> TypedImportResult:
        _reject_nonempty_bucket(request.proposal.normalized_diff, "create")
        update = _objects(request.proposal.normalized_diff, "update")
        if _objects(request.proposal.normalized_diff, "create"):
            raise TypedImportError(
                "project notes imports do not accept create entries",
                code="invalid_import_diff",
            )
        skip = _objects(request.proposal.normalized_diff, "skip")
        project_id = cast(str, request.proposal.target_project_id)
        if not update:
            return TypedImportResult(
                status="completed",
                entity_refs=_skip_refs(
                    skip,
                    entity_type="project_notes",
                    fallback_id=project_id,
                ),
            )
        if len(update) != 1 or update[0].get("field") != "project_notes":
            raise TypedImportError(
                "project notes proposal must contain one notes update",
                code="invalid_import_diff",
            )
        item = update[0]
        expected = item.get("expected_revision")
        if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
            raise TypedImportError(
                "project notes expected revision is invalid",
                code="invalid_import_diff",
            )
        after = str(item.get("after") or "").strip()
        current = self._service.get(actor=request.actor, project_id=project_id)
        if current.project_notes.strip() == after:
            return TypedImportResult(
                status="completed",
                entity_refs=(
                    ImportEntityRef(
                        entity_type="project_notes",
                        entity_id=project_id,
                        action="skip",
                        revision=current.revision,
                        reason="project_notes_unchanged",
                    ),
                ),
            )
        if current.revision != expected:
            raise TypedImportError(
                "project metadata revision changed",
                code="target_revision_conflict",
            )
        updated = self._service.update(
            actor=request.actor,
            project_id=project_id,
            expected_revision=expected,
            customer_name=current.customer_name,
            official_domain=current.official_domain,
            project_notes=after,
        )
        return TypedImportResult(
            status="completed",
            entity_refs=(
                ImportEntityRef(
                    entity_type="project_notes",
                    entity_id=project_id,
                    action="update",
                    revision=updated.revision,
                ),
                *_skip_refs(
                    skip,
                    entity_type="project_notes",
                    fallback_id=project_id,
                ),
            ),
        )


class TopicLibraryImportAdapter:
    target_kind: ImportTargetKind = "topic_library"

    def __init__(self, service: PostgresServerProjectTopicService) -> None:
        self._service = service

    def execute(self, request: TypedImportRequest) -> TypedImportResult:
        _reject_nonempty_update(request.proposal.normalized_diff)
        create = _objects(request.proposal.normalized_diff, "create")
        if _objects(request.proposal.normalized_diff, "update"):
            raise TypedImportError(
                "topic library imports do not accept update entries",
                code="invalid_import_diff",
            )
        skip = _objects(request.proposal.normalized_diff, "skip")
        refs = list(_skip_refs(skip, entity_type="project_topic", fallback_id="existing"))
        if create:
            result = self._service.import_rows(
                actor=request.actor,
                project_id=cast(str, request.proposal.target_project_id),
                idempotency_key=_stable_key("assistant_import", request),
                rows=tuple(
                    ServerProjectTopicRow(
                        topic=_required(item.get("topic"), "topic"),
                        primary_keyword=str(item.get("primary_keyword") or ""),
                        competitor_keyword=str(item.get("competitor_keyword") or ""),
                    )
                    for item in create
                ),
            )
            refs.extend(
                ImportEntityRef(
                    entity_type="project_topic",
                    entity_id=item.topic_id,
                    action="create" if item.created else "skip",
                    revision=0,
                    reason=None if item.created else "topic_already_exists",
                )
                for item in result.items
            )
        return TypedImportResult(status="completed", entity_refs=tuple(refs))


__all__ = [
    "ImportEntityRef",
    "KnowledgeSourceImportAdapter",
    "ProjectNotesImportAdapter",
    "PromptAssetImportAdapter",
    "TaskWorkbookImportAdapter",
    "TopicLibraryImportAdapter",
    "TypedImportAdapter",
    "TypedImportError",
    "TypedImportExecutor",
    "TypedImportRequest",
    "TypedImportResult",
    "build_default_import_executor",
]
