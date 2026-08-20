from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException
from starlette.datastructures import Headers, UploadFile


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.access_control import ActorIdentity  # noqa: E402
from services.server_auth import SERVER_AUTH_COOKIE_NAME  # noqa: E402
from workflow_assistant.attachment_http import (  # noqa: E402
    create_attachment_download,
    get_attachment,
    list_attachments,
    reject_attachment,
    upload_attachment,
)
from workflow_assistant.attachments import (  # noqa: E402
    AttachmentDownload,
    AttachmentNotFound,
    AssistantAttachment,
)
from workflow_assistant.repository import WorkflowAssistantNotFound  # noqa: E402


UTC = timezone.utc
NOW = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)
ACTOR = ActorIdentity("org-a", "user-a")


def attachment(*, attachment_id: str = "asa-a", project: str | None = None) -> AssistantAttachment:
    return AssistantAttachment(
        attachment_id=attachment_id,
        organization_id="org-a",
        creator_user_id="user-a",
        conversation_id="conv-a",
        proposed_project_id=project,
        plan_id=None,
        idempotency_key="upload-a",
        object_key="organizations/org-a/workflow-assistant/users/user-a/conversations/conv-a/attachments/asa-a/hash",
        original_filename="notes.txt",
        mime_type="text/plain",
        byte_size=5,
        sha256="a" * 64,
        classification=None,
        classification_payload={},
        revision=0,
        status="uploaded",
        expires_at=NOW + timedelta(days=7),
        created_at=NOW,
        updated_at=NOW,
    )


class Service:
    def __init__(self) -> None:
        self.record = attachment()
        self.calls: list[tuple[str, object]] = []

    def upload(self, **kwargs):
        self.calls.append(("upload", kwargs))
        self.record = attachment(project=kwargs.get("proposed_project_id"))
        return self.record

    def list(self, **kwargs):
        self.calls.append(("list", kwargs))
        return (self.record,)

    def get(self, **kwargs):
        self.calls.append(("get", kwargs))
        if kwargs["attachment_id"] != self.record.attachment_id:
            raise AttachmentNotFound("not found")
        return self.record

    def create_download(self, **kwargs):
        self.calls.append(("download", kwargs))
        item = self.get(**kwargs)
        return AttachmentDownload(item, "https://objects.test/signed", 300)

    def reject(self, **kwargs):
        self.calls.append(("reject", kwargs))
        return self.record


class Repository:
    def __init__(self, *, visible: bool = True) -> None:
        self.visible = visible
        self.calls: list[tuple[ActorIdentity, str]] = []

    def get_conversation(self, *, actor, conversation_id, include_messages=False):
        del include_messages
        self.calls.append((actor, conversation_id))
        if not self.visible or conversation_id != "conv-a":
            raise WorkflowAssistantNotFound("conversation missing")
        return object()


class Security:
    def __init__(self, *, allowed: bool = True, returned_actor: ActorIdentity = ACTOR) -> None:
        self.allowed = allowed
        self.returned_actor = returned_actor
        self.calls: list[tuple[str, str, str]] = []

    def authorize_project(self, *, token, project, permission):
        self.calls.append((token, project, permission))
        if not self.allowed:
            from services.server_request_security import ServerRequestForbidden

            raise ServerRequestForbidden("denied")
        return SimpleNamespace(actor=self.returned_actor, project_id=project)


def request(*, service=None, repository=None, security=None, enabled=True, attachments=True):
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                article_agent_config=SimpleNamespace(
                    workflow_assistant_enabled=enabled,
                    workflow_assistant_attachments_enabled=attachments,
                ),
                workflow_assistant_attachment_service=service or Service(),
                workflow_assistant_repository=repository or Repository(),
                server_request_security=security,
            )
        ),
        cookies={SERVER_AUTH_COOKIE_NAME: "session-token"},
    )


def upload_file() -> UploadFile:
    import io

    return UploadFile(
        io.BytesIO(b"hello"),
        filename="notes.txt",
        headers=Headers({"content-type": "text/plain"}),
    )


class AttachmentHttpTests(unittest.TestCase):
    def test_upload_checks_conversation_project_and_returns_download_url(self) -> None:
        service = Service()
        security = Security()
        result = asyncio.run(
            upload_attachment(
                "conv-a",
                request=request(service=service, security=security),  # type: ignore[arg-type]
                file=upload_file(),
                idempotency_key="upload-a",
                proposed_project_id="project-a",
                actor=ACTOR,
            )
        )
        self.assertEqual(result.download_url, "https://objects.test/signed")
        self.assertEqual(security.calls, [("session-token", "project-a", "project.view")])
        upload = next(payload for name, payload in service.calls if name == "upload")
        self.assertEqual(upload["proposed_project_id"], "project-a")
        self.assertEqual(upload["content"], b"hello")

    def test_upload_rejects_preselected_project_without_current_access(self) -> None:
        with self.assertRaises(HTTPException) as captured:
            asyncio.run(
                upload_attachment(
                    "conv-a",
                    request=request(security=Security(allowed=False)),  # type: ignore[arg-type]
                    file=upload_file(),
                    idempotency_key="upload-a",
                    proposed_project_id="project-a",
                    actor=ACTOR,
                )
            )
        self.assertEqual(captured.exception.status_code, 403)

    def test_upload_fails_closed_without_project_security(self) -> None:
        with self.assertRaises(HTTPException) as captured:
            asyncio.run(
                upload_attachment(
                    "conv-a",
                    request=request(security=None),  # type: ignore[arg-type]
                    file=upload_file(),
                    idempotency_key="upload-a",
                    proposed_project_id="project-a",
                    actor=ACTOR,
                )
            )
        self.assertEqual(captured.exception.status_code, 503)

    def test_list_and_metadata_require_conversation_scope(self) -> None:
        hidden = request(repository=Repository(visible=False))
        for call in (
            lambda: list_attachments("conv-a", hidden, actor=ACTOR),  # type: ignore[arg-type]
            lambda: get_attachment("asa-a", hidden, conversation_id="conv-a", actor=ACTOR),  # type: ignore[arg-type]
        ):
            with self.assertRaises(HTTPException) as captured:
                call()
            self.assertEqual(captured.exception.status_code, 404)

    def test_metadata_download_and_reject_are_temporary_attachment_actions(self) -> None:
        service = Service()
        current = request(service=service)
        metadata = get_attachment("asa-a", current, conversation_id="conv-a", actor=ACTOR)  # type: ignore[arg-type]
        signed = create_attachment_download("asa-a", current, conversation_id="conv-a", actor=ACTOR)  # type: ignore[arg-type]
        rejected = reject_attachment("asa-a", current, conversation_id="conv-a", actor=ACTOR)  # type: ignore[arg-type]
        self.assertIsNone(metadata.download_url)
        self.assertEqual(signed.download_url, "https://objects.test/signed")
        self.assertEqual(rejected.status, "uploaded")
        self.assertNotIn("classify", [name for name, _ in service.calls])
        self.assertNotIn("import", [name for name, _ in service.calls])

    def test_both_feature_gates_apply(self) -> None:
        for enabled, attachments in ((False, True), (True, False)):
            with self.assertRaises(HTTPException) as captured:
                list_attachments(
                    "conv-a",
                    request=request(enabled=enabled, attachments=attachments),  # type: ignore[arg-type]
                    actor=ACTOR,
                )
            self.assertEqual(captured.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
