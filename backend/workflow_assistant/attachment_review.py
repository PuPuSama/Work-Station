from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Event, Thread
from typing import Mapping, cast

from models import PromptKind
from services.access_control import ActorIdentity, ProjectAccessService, ProjectPermission
from services.object_store import ObjectStore
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from server_schema import organizations, workspace_users

from .attachment_classifier import AttachmentClassifierLlmFactory, AttachmentClassifierService
from .attachment_job_repository import (
    PostgresAttachmentJobOrganizationDiscovery,
    PostgresAttachmentJobRepository,
)
from .attachment_jobs import (
    AttachmentJob,
    AttachmentJobAuthorizationChanged,
    AttachmentJobConflict,
    AttachmentJobOrganizationDispatcher,
    AttachmentJobResult,
    AttachmentJobRunner,
)
from .attachment_repository import PostgresAttachmentRepository
from .attachments import AssistantAttachment, AttachmentConflict
from .classification import AttachmentClassification
from .import_proposal_repository import PostgresImportProposalRepository
from .import_proposals import (
    ImportProposal,
    ImportProposalService,
    ImportTargetKind,
    normalized_json_object,
)
from .proposal_preview import ProposalPreviewBuilder
from .proposal_target_snapshot import PostgresProposalTargetSnapshotProvider


def _classification(attachment: AssistantAttachment) -> AttachmentClassification:
    envelope = dict(attachment.classification_payload)
    nested = envelope.get("classification")
    if not isinstance(nested, Mapping):
        raise AttachmentConflict("attachment classification payload is invalid")
    result = AttachmentClassification.model_validate(dict(nested))
    if result.classification != attachment.classification:
        raise AttachmentConflict("attachment classification changed")
    return result


def _permission(target_kind: ImportTargetKind, *, confirmation: bool) -> str:
    if not confirmation:
        return "project.view"
    if target_kind == "knowledge_source":
        return "knowledge.edit"
    return "article.edit"


@dataclass(frozen=True, slots=True)
class AttachmentReviewRunnerStopReport:
    stopped: bool
    alive: bool


class AttachmentReviewWorkflowService:
    """Application boundary for classify and review-only proposal jobs."""

    def __init__(
        self,
        engine: Engine,
        *,
        object_store: ObjectStore,
        llm_factory: AttachmentClassifierLlmFactory,
        access: ProjectAccessService,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        if not 0.1 <= poll_interval_seconds <= 60:
            raise ValueError("poll_interval_seconds must be between 0.1 and 60")
        self._engine = engine
        self._access = access
        self._attachments = PostgresAttachmentRepository(engine)
        self._proposals = PostgresImportProposalRepository(engine)
        self._proposal_service = ImportProposalService(
            repository=self._proposals,
            attachment_loader=self._load_attachment,
            authorize_project=self._authorize_proposal,
        )
        self._classifier = AttachmentClassifierService(
            self._attachments,
            object_store,
            llm_factory,
            self._authorize_attachment_project,
        )
        self._preview = ProposalPreviewBuilder(object_store)
        self._targets = PostgresProposalTargetSnapshotProvider(engine, access=access)
        self._discovery = PostgresAttachmentJobOrganizationDiscovery(engine)
        self._dispatcher = AttachmentJobOrganizationDispatcher(
            self._discovery,
            runner_factory=self._runner_for_organization,
        )
        self._poll_interval_seconds = float(poll_interval_seconds)
        self._stop = Event()
        self._wake = Event()
        self._thread: Thread | None = None

    @property
    def running(self) -> bool:
        return bool(self._thread is not None and self._thread.is_alive())

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._wake.set()
        self._thread = Thread(
            target=self._run,
            name="workflow-assistant-attachment-review",
            daemon=True,
        )
        self._thread.start()

    def wake(self) -> None:
        self._wake.set()

    def stop(self, *, timeout_seconds: float = 10.0) -> AttachmentReviewRunnerStopReport:
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative")
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout_seconds)
        return AttachmentReviewRunnerStopReport(
            stopped=True,
            alive=bool(thread is not None and thread.is_alive()),
        )

    def enqueue_classification(
        self,
        *,
        actor: ActorIdentity,
        attachment: AssistantAttachment,
        idempotency_key: str,
        expected_attachment_revision: int,
    ) -> AttachmentJob:
        self._assert_attachment(actor, attachment, expected_attachment_revision)
        if attachment.proposed_project_id is not None:
            self._access.require(actor, attachment.proposed_project_id, "project.view")
        job = self._job_repository(actor.organization_id).enqueue(
            requested_by_user_id=actor.user_id,
            attachment_id=attachment.attachment_id,
            operation="classify_attachment",
            idempotency_key=idempotency_key,
            expected_attachment_revision=expected_attachment_revision,
            project_id=attachment.proposed_project_id,
            request_payload={"conversation_id": attachment.conversation_id},
        )
        self.wake()
        return job

    def enqueue_proposal_preview(
        self,
        *,
        actor: ActorIdentity,
        attachment: AssistantAttachment,
        target_kind: ImportTargetKind,
        target_project_id: str | None,
        plan_id: str | None,
        idempotency_key: str,
        expected_attachment_revision: int,
        prompt_kind: PromptKind | None = None,
    ) -> AttachmentJob:
        self._assert_attachment_scope(actor, attachment)
        if attachment.revision != expected_attachment_revision:
            resolution = attachment.classification_payload.get("resolution")
            replay = (
                isinstance(resolution, Mapping)
                and resolution.get("request_idempotency_key") == idempotency_key
                and resolution.get("source_revision") == expected_attachment_revision
            )
            if not replay:
                raise AttachmentConflict("attachment revision changed")
        project_id = str(target_project_id or "").strip()
        if not project_id:
            raise ValueError("target_project_id is required for proposal preview")
        self._access.require(actor, project_id, "project.view")
        resolved = self._resolve_choice(
            actor=actor,
            attachment=attachment,
            target_kind=target_kind,
            target_project_id=project_id,
            prompt_kind=prompt_kind,
            request_idempotency_key=idempotency_key,
            source_revision=expected_attachment_revision,
        )
        job = self._job_repository(actor.organization_id).enqueue(
            requested_by_user_id=actor.user_id,
            attachment_id=resolved.attachment_id,
            operation="preview_import_proposal",
            idempotency_key=idempotency_key,
            expected_attachment_revision=resolved.revision,
            project_id=project_id,
            request_payload={
                "target_kind": target_kind,
                "target_project_id": project_id,
                "plan_id": plan_id,
            },
        )
        self.wake()
        return job

    def get_proposal(self, *, actor: ActorIdentity, proposal_id: str) -> ImportProposal:
        proposal = self._proposal_service.get(actor=actor, proposal_id=proposal_id)
        if proposal.target_project_id is not None:
            self._access.require(actor, proposal.target_project_id, "project.view")
        return proposal

    def get_job(self, *, actor: ActorIdentity, job_id: str) -> AttachmentJob:
        job = self._job_repository(actor.organization_id).get_for_actor(
            user_id=actor.user_id,
            job_id=job_id,
        )
        if job.project_id is not None:
            self._access.require(actor, job.project_id, "project.view")
        return job

    def revise_proposal(
        self,
        *,
        actor: ActorIdentity,
        proposal_id: str,
        expected_revision: int,
        expected_attachment_revision: int,
        target_kind: ImportTargetKind,
        target_project_id: str | None,
        normalized_diff: Mapping[str, object],
    ) -> ImportProposal:
        current = self.get_proposal(actor=actor, proposal_id=proposal_id)
        self._require_current_attachment_revision(
            actor, current.attachment_id, expected_attachment_revision
        )
        if (
            target_kind != current.target_kind
            or target_project_id != current.target_project_id
        ):
            raise ValueError(
                "changing a proposal target requires a new server preview"
            )
        reviewed_diff = self._reviewed_subset(
            current.normalized_diff,
            normalized_diff,
        )
        return self._proposal_service.revise(
            actor=actor,
            proposal_id=proposal_id,
            expected_revision=expected_revision,
            target_kind=target_kind,
            normalized_diff=reviewed_diff,
            target_project_id=target_project_id,
        )

    @staticmethod
    def _reviewed_subset(
        original: Mapping[str, object],
        requested: Mapping[str, object],
    ) -> dict[str, object]:
        """Allow exclusions while rejecting client-authored import content."""

        current = normalized_json_object(original)
        candidate = normalized_json_object(requested)
        if set(candidate) != set(current):
            raise ValueError("proposal diff fields cannot be added or removed")
        review_lists = {"create", "update", "skip", "conflicts", "invalid"}
        for key, current_value in current.items():
            candidate_value = candidate[key]
            if key not in review_lists:
                if candidate_value != current_value:
                    raise ValueError("proposal diff metadata cannot be changed")
                continue
            if not isinstance(current_value, list) or not isinstance(
                candidate_value, list
            ):
                raise ValueError("proposal diff review buckets must be arrays")
            available = [
                normalized_json_object({"item": item})["item"]
                for item in current_value
            ]
            remaining = list(available)
            for item in candidate_value:
                normalized_item = normalized_json_object({"item": item})["item"]
                try:
                    index = remaining.index(normalized_item)
                except ValueError as exc:
                    raise ValueError(
                        "proposal revisions may only exclude existing entries"
                    ) from exc
                remaining.pop(index)
        return candidate

    def confirm_proposal(
        self,
        *,
        actor: ActorIdentity,
        proposal_id: str,
        target_project_id: str,
        expected_revision: int,
        expected_attachment_revision: int,
    ) -> ImportProposal:
        current = self.get_proposal(actor=actor, proposal_id=proposal_id)
        self._require_current_attachment_revision(
            actor, current.attachment_id, expected_attachment_revision
        )
        return self._proposal_service.confirm(
            actor=actor,
            proposal_id=proposal_id,
            expected_revision=expected_revision,
            target_project_id=target_project_id,
        )

    def cancel_proposal(
        self,
        *,
        actor: ActorIdentity,
        proposal_id: str,
        expected_revision: int,
    ) -> ImportProposal:
        self.get_proposal(actor=actor, proposal_id=proposal_id)
        return self._proposal_service.cancel(
            actor=actor,
            proposal_id=proposal_id,
            expected_revision=expected_revision,
        )

    def _resolve_choice(
        self,
        *,
        actor: ActorIdentity,
        attachment: AssistantAttachment,
        target_kind: ImportTargetKind,
        target_project_id: str,
        prompt_kind: PromptKind | None,
        request_idempotency_key: str,
        source_revision: int,
    ) -> AssistantAttachment:
        current = _classification(attachment)
        if current.classification != "needs_user_choice":
            if current.classification != target_kind:
                raise ValueError("target_kind does not match attachment classification")
            if current.target_project_id != target_project_id:
                raise ValueError("target project does not match attachment classification")
            if current.prompt_kind != prompt_kind and target_kind == "prompt_asset":
                raise ValueError("prompt_kind does not match attachment classification")
            return attachment
        if target_kind not in current.candidate_classifications or target_kind in {
            "needs_user_choice",
            "unsupported",
        }:
            raise ValueError("target_kind is outside the classified choices")
        if not current.structure_compatible:
            raise ValueError("attachment structure is not compatible with this target")
        if target_kind == "prompt_asset" and prompt_kind is None:
            raise ValueError("prompt_kind is required for prompt attachments")
        resolved = AttachmentClassification(
            classification=target_kind,
            reason=current.reason,
            confidence=current.confidence,
            target_project_id=target_project_id,
            prompt_kind=prompt_kind if target_kind == "prompt_asset" else None,
            candidate_classifications=[],
            is_ambiguous=False,
            structure_compatible=True,
            affects_multiple_projects=False,
        )
        envelope = dict(attachment.classification_payload)
        envelope["classification"] = resolved.model_dump(mode="json")
        envelope["resolution"] = {
            "kind": "explicit_user_choice",
            "resolved_by_user_id": actor.user_id,
            "request_idempotency_key": request_idempotency_key,
            "source_revision": source_revision,
        }
        return self._attachments.resolve_classification_choice(
            attachment=attachment,
            expected_revision=attachment.revision,
            classification=target_kind,
            classification_payload=envelope,
            now=datetime.now(timezone.utc),
        )

    def _runner_for_organization(self, organization_id: str) -> AttachmentJobRunner:
        repository = self._job_repository(organization_id)
        return AttachmentJobRunner(
            repository,
            authorize=self._authorize_job,
            handlers={
                "classify_attachment": self._handle_classification,
                "preview_import_proposal": self._handle_preview,
            },
        )

    def _authorize_job(self, job: object, _phase: str) -> None:
        actor = ActorIdentity(
            str(getattr(job, "organization_id")),
            str(getattr(job, "requested_by_user_id")),
        )
        self._require_active_actor(actor)
        project_id = getattr(job, "project_id", None)
        if project_id:
            permission = "project.view"
            if getattr(job, "operation", "") == "execute_import_proposal":
                proposal_id = str(getattr(job, "proposal_id", ""))
                proposal = self._proposal_service.get(
                    actor=actor, proposal_id=proposal_id
                )
                permission = _permission(proposal.target_kind, confirmation=True)
            self._access.require(
                actor,
                str(project_id),
                cast(ProjectPermission, permission),
            )

    def _require_active_actor(self, actor: ActorIdentity) -> None:
        with self._engine.connect() as connection:
            active = connection.execute(
                sa.select(sa.literal(True))
                .select_from(
                    workspace_users.join(
                        organizations,
                        organizations.c.organization_id
                        == workspace_users.c.organization_id,
                    )
                )
                .where(
                    workspace_users.c.organization_id == actor.organization_id,
                    workspace_users.c.user_id == actor.user_id,
                    workspace_users.c.status == "active",
                    organizations.c.status == "active",
                )
            ).scalar_one_or_none()
        if active is not True:
            raise PermissionError("attachment job actor is not active")

    def _handle_classification(
        self,
        job: AttachmentJob,
        cancelled: object,
        commit_guard: object,
    ) -> AttachmentJobResult:
        if callable(cancelled) and cancelled():
            raise AttachmentJobConflict("attachment classification was cancelled")
        actor = ActorIdentity(job.organization_id, job.requested_by_user_id)
        conversation_id = str(job.request_payload.get("conversation_id") or "").strip()
        current = self._load_attachment(actor, job.attachment_id)
        if (
            current is not None
            and current.status in {"proposal_ready", "needs_user_choice"}
            and current.classification_payload.get(
                "classification_job_idempotency_key"
            )
            == job.idempotency_key
        ):
            if callable(commit_guard):
                commit_guard()
            replayed = _classification(current)
            return AttachmentJobResult(
                result_payload={
                    "classification": replayed.model_dump(mode="json"),
                    "model_identity": str(
                        current.classification_payload.get("model_identity") or ""
                    ),
                },
                attachment_revision=current.revision,
            )
        try:
            result = self._classifier.classify(
                actor,
                conversation_id=conversation_id,
                attachment_id=job.attachment_id,
                before_commit=(commit_guard if callable(commit_guard) else None),
                job_idempotency_key=job.idempotency_key,
            )
        except (AttachmentJobAuthorizationChanged, AttachmentJobConflict):
            raise
        except Exception as exc:
            conflict = AttachmentJobConflict("attachment classification failed")
            conflict.code = str(getattr(exc, "code", conflict.code))
            raise conflict from exc
        return AttachmentJobResult(
            result_payload={
                "classification": result.classification.model_dump(mode="json"),
                "model_identity": result.model_identity,
            },
            attachment_revision=result.attachment.revision,
        )

    def _handle_preview(
        self,
        job: AttachmentJob,
        cancelled: object,
        commit_guard: object,
    ) -> AttachmentJobResult:
        actor = ActorIdentity(job.organization_id, job.requested_by_user_id)
        attachment = self._load_attachment(actor, job.attachment_id)
        if attachment is None or attachment.revision != job.expected_attachment_revision:
            raise AttachmentJobConflict("attachment revision changed")
        classification = _classification(attachment)
        target_kind = str(job.request_payload.get("target_kind") or "")
        if classification.classification != target_kind or not job.project_id:
            raise AttachmentJobConflict("proposal preview target changed")
        target = self._targets.load(actor=actor, project_id=job.project_id)
        diff = self._preview.build(
            actor=actor,
            attachment=attachment,
            classification=classification,
            target=target,
        )
        if callable(cancelled) and cancelled():
            raise AttachmentJobConflict("proposal preview was cancelled")
        if callable(commit_guard):
            commit_guard()
        proposal = self._proposal_service.create(
            actor=actor,
            attachment_id=attachment.attachment_id,
            target_kind=cast(ImportTargetKind, target_kind),
            normalized_diff=diff,
            idempotency_key=f"preview-job:{job.job_id}",
            target_project_id=job.project_id,
            plan_id=(str(job.request_payload.get("plan_id")) if job.request_payload.get("plan_id") else None),
        )
        return AttachmentJobResult(
            result_payload={
                "proposal_id": proposal.proposal_id,
                "proposal_revision": proposal.revision,
                "proposal_status": proposal.status,
            },
            attachment_revision=attachment.revision,
            proposal_revision=proposal.revision,
        )

    def _authorize_attachment_project(self, actor: ActorIdentity, project_id: str) -> None:
        self._access.require(actor, project_id, "project.view")

    def _authorize_proposal(
        self,
        actor: ActorIdentity,
        project_id: str,
        target_kind: ImportTargetKind,
        stage: str,
    ) -> None:
        self._access.require(
            actor,
            project_id,
            cast(
                ProjectPermission,
                _permission(target_kind, confirmation=stage == "confirm"),
            ),
        )

    def _load_attachment(
        self, actor: ActorIdentity, attachment_id: str
    ) -> AssistantAttachment | None:
        return self._attachments.get_by_id_for_actor(
            organization_id=actor.organization_id,
            creator_user_id=actor.user_id,
            attachment_id=attachment_id,
        )

    def _require_current_attachment_revision(
        self, actor: ActorIdentity, attachment_id: str, expected_revision: int
    ) -> AssistantAttachment:
        attachment = self._load_attachment(actor, attachment_id)
        if attachment is None or attachment.revision != expected_revision:
            raise AttachmentConflict("attachment revision changed")
        return attachment

    @staticmethod
    def _assert_attachment(
        actor: ActorIdentity,
        attachment: AssistantAttachment,
        expected_revision: int,
    ) -> None:
        if (
            attachment.organization_id != actor.organization_id
            or attachment.creator_user_id != actor.user_id
        ):
            raise AttachmentConflict("attachment scope changed")
        if attachment.revision != expected_revision:
            raise AttachmentConflict("attachment revision changed")

    @staticmethod
    def _assert_attachment_scope(
        actor: ActorIdentity,
        attachment: AssistantAttachment,
    ) -> None:
        if (
            attachment.organization_id != actor.organization_id
            or attachment.creator_user_id != actor.user_id
        ):
            raise AttachmentConflict("attachment scope changed")

    def _job_repository(self, organization_id: str) -> PostgresAttachmentJobRepository:
        return PostgresAttachmentJobRepository(
            self._engine,
            organization_id=organization_id,
        )

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._dispatcher.run_once(
                    organization_limit=25,
                    jobs_per_organization=1,
                )
            except Exception:
                pass
            self._wake.wait(self._poll_interval_seconds)
            self._wake.clear()


__all__ = [
    "AttachmentReviewRunnerStopReport",
    "AttachmentReviewWorkflowService",
]
