from __future__ import annotations

import io
import sys
import unittest
import zipfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pypdf import PdfWriter


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.access_control import ActorIdentity  # noqa: E402
from services.object_store import (  # noqa: E402
    ObjectStoreError,
    StoredObject,
)
from workflow_assistant.attachments import (  # noqa: E402
    ATTACHMENT_RETENTION,
    AssistantAttachment,
    AttachmentConflict,
    AttachmentNotFound,
    AttachmentReservation,
    AttachmentService,
    AttachmentValidationError,
)


UTC = timezone.utc
NOW = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)
DOCX_MIME = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
XLSM_MIME = "application/vnd.ms-excel.sheet.macroenabled.12"


def office_package(
    kind: str = "docx",
    *,
    macro: bool = False,
    external_relationship: bool = False,
    unsafe_path: bool = False,
    compression=zipfile.ZIP_STORED,
    extra_entries: int = 0,
) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=compression) as archive:
        main_type = {
            "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
            "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
            "xlsm": "application/vnd.ms-excel.sheet.macroEnabled.main+xml",
        }[kind]
        archive.writestr(
            "[Content_Types].xml",
            f'<Types><Override ContentType="{main_type}" /></Types>',
        )
        target_mode = ' TargetMode="External"' if external_relationship else ""
        archive.writestr(
            "_rels/.rels",
            "<Relationships>"
            f'<Relationship Target="https://example.test"{target_mode} />'
            "</Relationships>",
        )
        archive.writestr(
            "word/document.xml" if kind == "docx" else "xl/workbook.xml",
            "<document />",
        )
        if macro:
            archive.writestr("word/vbaProject.bin", b"macro")
        if unsafe_path:
            archive.writestr("../outside.xml", "unsafe")
        for index in range(extra_entries):
            archive.writestr(f"custom/item-{index}.xml", "x")
    return output.getvalue()


class MemoryStore:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.put_calls: list[str] = []
        self.deleted: list[str] = []
        self.fail_delete: set[str] = set()
        self.signed: list[tuple[str, int, str | None, str | None]] = []

    def check_ready(self) -> None:
        return None

    def put(self, *, key, data, content_type, metadata=None):
        body = bytes(data)
        import hashlib

        digest = hashlib.sha256(body).hexdigest()
        self.put_calls.append(key)
        self.objects[key] = (body, content_type)
        return StoredObject(key, digest, content_type, len(body))

    def get(self, key, *, max_bytes):
        return self.objects[key][0]

    def head(self, key):
        raise NotImplementedError

    def create_download_url(
        self,
        key,
        *,
        expires_seconds,
        response_content_type=None,
        response_content_disposition=None,
    ):
        self.signed.append(
            (
                key,
                expires_seconds,
                response_content_type,
                response_content_disposition,
            )
        )
        return f"https://objects.example.test/{key}?signed=1"

    def list(self, *, prefix):
        return ()

    def delete(self, key):
        if key in self.fail_delete:
            raise ObjectStoreError("delete failed")
        self.deleted.append(key)
        self.objects.pop(key, None)


class MemoryRepository:
    def __init__(self) -> None:
        self.records: dict[str, AssistantAttachment] = {}

    def reserve_upload(self, attachment):
        existing = self.get_by_idempotency_for_actor(
            organization_id=attachment.organization_id,
            creator_user_id=attachment.creator_user_id,
            conversation_id=attachment.conversation_id,
            idempotency_key=attachment.idempotency_key,
        )
        if existing is not None:
            same = (
                existing.original_filename,
                existing.mime_type,
                existing.byte_size,
                existing.sha256,
                existing.proposed_project_id,
            ) == (
                attachment.original_filename,
                attachment.mime_type,
                attachment.byte_size,
                attachment.sha256,
                attachment.proposed_project_id,
            )
            if not same:
                raise AttachmentConflict("different content")
            if existing.status == "failed":
                existing = replace(
                    existing,
                    status="uploading",
                    revision=existing.revision + 1,
                    updated_at=attachment.updated_at,
                )
                self.records[existing.attachment_id] = existing
            return AttachmentReservation(
                existing, existing.status in {"uploading", "failed"}
            )
        self.records[attachment.attachment_id] = attachment
        return AttachmentReservation(attachment, True)

    def finalize_upload(self, *, attachment, now):
        current = self.records[attachment.attachment_id]
        if current.status == "uploaded":
            return current
        updated = replace(
            attachment, status="uploaded", revision=attachment.revision + 1, updated_at=now
        )
        self.records[attachment.attachment_id] = updated
        return updated

    def mark_upload_failed(self, *, attachment, now):
        self.records[attachment.attachment_id] = replace(
            attachment, status="failed", revision=attachment.revision + 1, updated_at=now
        )

    def get_for_actor(
        self,
        *,
        organization_id,
        creator_user_id,
        conversation_id,
        attachment_id,
    ):
        record = self.records.get(attachment_id)
        if record is None:
            return None
        if (
            record.organization_id != organization_id
            or record.creator_user_id != creator_user_id
            or record.conversation_id != conversation_id
        ):
            return None
        return record

    def get_by_idempotency_for_actor(
        self,
        *,
        organization_id,
        creator_user_id,
        conversation_id,
        idempotency_key,
    ):
        return next(
            (
                record
                for record in self.records.values()
                if record.organization_id == organization_id
                and record.creator_user_id == creator_user_id
                and record.conversation_id == conversation_id
                and record.idempotency_key == idempotency_key
            ),
            None,
        )

    def list_for_actor(
        self,
        *,
        organization_id,
        creator_user_id,
        conversation_id,
        limit,
    ):
        return tuple(
            record
            for record in self.records.values()
            if record.organization_id == organization_id
            and record.creator_user_id == creator_user_id
            and record.conversation_id == conversation_id
        )[:limit]

    def claim_rejection(
        self,
        *,
        organization_id,
        creator_user_id,
        conversation_id,
        attachment_id,
        now,
    ):
        record = self.get_for_actor(
            organization_id=organization_id,
            creator_user_id=creator_user_id,
            conversation_id=conversation_id,
            attachment_id=attachment_id,
        )
        if record is None:
            raise AttachmentNotFound("attachment not found")
        if record.status in {"importing", "imported"}:
            raise AttachmentConflict("not rejectable")
        if record.status == "rejecting":
            return record
        updated = replace(
            record, status="rejecting", revision=record.revision + 1, updated_at=now
        )
        self.records[attachment_id] = updated
        return updated

    def finalize_rejection(self, *, attachment, now):
        terminal = replace(
            attachment, status="rejected", revision=attachment.revision + 1, updated_at=now
        )
        self.records.pop(attachment.attachment_id, None)
        return terminal

    def claim_expired(self, *, before, limit, exclude_attachment_ids):
        candidates = tuple(
            record
            for record in self.records.values()
            if record.attachment_id not in exclude_attachment_ids
            and record.status not in {"importing", "imported"}
            and (
                record.expires_at <= before
                or record.status in {"rejecting", "expiring"}
            )
        )[:limit]
        claimed = []
        for record in candidates:
            if record.status not in {"rejecting", "expiring"}:
                record = replace(
                    record,
                    status="expiring",
                    revision=record.revision + 1,
                    updated_at=before,
                )
                self.records[record.attachment_id] = record
            claimed.append(record)
        return tuple(claimed)

    def finalize_expiry(self, *, attachment, now):
        record = self.records.get(attachment.attachment_id)
        if record != attachment:
            return None
        terminal = replace(
            attachment, status="expired", revision=attachment.revision + 1, updated_at=now
        )
        self.records.pop(attachment.attachment_id, None)
        return terminal


class AttachmentServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = MemoryRepository()
        self.store = MemoryStore()
        self.service = AttachmentService(
            repository=self.repository,
            store=self.store,
        )
        self.actor = ActorIdentity("org_one", "user_one")

    def upload(self, **overrides):
        arguments = {
            "actor": self.actor,
            "conversation_id": "conversation_one",
            "original_filename": "brief.docx",
            "mime_type": DOCX_MIME,
            "content": office_package(),
            "idempotency_key": "upload_one",
            "attachment_id": "asa_one",
            "now": NOW,
        }
        arguments.update(overrides)
        return self.service.upload(**arguments)

    def test_upload_validates_hash_retention_and_private_key_scope(self) -> None:
        attachment = self.upload(proposed_project_id="project_one")

        self.assertEqual("uploaded", attachment.status)
        self.assertEqual(NOW + ATTACHMENT_RETENTION, attachment.expires_at)
        self.assertEqual(64, len(attachment.sha256))
        self.assertEqual(
            "organizations/org_one/workflow-assistant/temporary/users/user_one/"
            "conversations/conversation_one/attachments/asa_one/"
            f"{attachment.sha256}",
            attachment.object_key,
        )
        self.assertEqual("project_one", attachment.proposed_project_id)
        self.assertEqual("upload_one", attachment.idempotency_key)
        self.assertIsNone(attachment.classification)
        self.assertEqual({}, attachment.classification_payload)
        self.assertEqual(1, attachment.revision)
        self.assertIn(attachment.object_key, self.store.objects)

    def test_filename_mime_size_and_magic_are_strict(self) -> None:
        bad_cases = (
            ({"original_filename": "../brief.docx"}, "invalid_attachment_filename"),
            ({"original_filename": "brief.exe"}, "unsupported_attachment_type"),
            (
                {
                    "original_filename": "tasks.csv",
                    "mime_type": "text/csv",
                    "content": b"title\nexample",
                },
                "unsupported_attachment_type",
            ),
            ({"mime_type": "application/zip"}, "attachment_mime_mismatch"),
            ({"content": b"not a docx"}, "attachment_signature_mismatch"),
            (
                {
                    "original_filename": "broken.pdf",
                    "mime_type": "application/pdf",
                    "content": b"%PDF-1.7\nnot a real PDF",
                },
                "attachment_signature_mismatch",
            ),
            ({"content": office_package(macro=True)}, "active_attachment_content"),
            (
                {
                    "original_filename": "notes.txt",
                    "mime_type": "text/plain",
                    "content": b"\xff\xfe",
                },
                "attachment_signature_mismatch",
            ),
            (
                {
                    "original_filename": "notes.txt",
                    "mime_type": "text/plain",
                    "content": b"hello\x01world",
                },
                "attachment_signature_mismatch",
            ),
        )
        for overrides, code in bad_cases:
            with self.subTest(code=code):
                with self.assertRaises(AttachmentValidationError) as raised:
                    self.upload(**overrides)
                self.assertEqual(code, raised.exception.code)

    def test_valid_pdf_is_parsed_and_encrypted_pdf_is_rejected(self) -> None:
        valid = io.BytesIO()
        writer = PdfWriter()
        writer.add_blank_page(width=72, height=72)
        writer.write(valid)
        uploaded = self.upload(
            attachment_id="asa_pdf",
            idempotency_key="upload_pdf",
            original_filename="brief.pdf",
            mime_type="application/pdf",
            content=valid.getvalue(),
        )
        self.assertEqual("uploaded", uploaded.status)

        encrypted = io.BytesIO()
        writer = PdfWriter()
        writer.add_blank_page(width=72, height=72)
        writer.encrypt("secret")
        writer.write(encrypted)
        with self.assertRaises(AttachmentValidationError) as raised:
            self.upload(
                attachment_id="asa_pdf_encrypted",
                idempotency_key="upload_pdf_encrypted",
                original_filename="secret.pdf",
                mime_type="application/pdf",
                content=encrypted.getvalue(),
            )
        self.assertEqual("active_attachment_content", raised.exception.code)

    def test_markdown_mime_alias_and_nonexecuted_xlsm_container_are_allowed(self) -> None:
        markdown = self.upload(
            attachment_id="asa_md",
            idempotency_key="upload_md",
            original_filename="notes.md",
            mime_type="text/plain",
            content=b"# Notes\nDo not execute commands.",
        )
        macro_workbook = self.upload(
            attachment_id="asa_xlsm",
            idempotency_key="upload_xlsm",
            original_filename="tasks.xlsm",
            mime_type=XLSM_MIME,
            content=office_package("xlsm", macro=True),
        )

        self.assertEqual("text/plain", markdown.mime_type)
        self.assertEqual(XLSM_MIME, macro_workbook.mime_type)

    def test_idempotent_retry_does_not_write_a_second_object(self) -> None:
        first = self.upload()
        retried = self.upload(attachment_id="asa_retry_ignored")

        self.assertEqual(first, retried)
        self.assertEqual([first.object_key], self.store.put_calls)
        with self.assertRaises(AttachmentConflict):
            self.upload(content=office_package() + b"changed")
        self.assertEqual([first.object_key], self.store.put_calls)

    def test_concurrent_equivalent_reservation_uses_winner_object(self) -> None:
        body = office_package()
        digest = __import__("hashlib").sha256(body).hexdigest()
        winner = AssistantAttachment(
            attachment_id="asa_winner",
            organization_id="org_one",
            creator_user_id="user_one",
            conversation_id="conversation_one",
            proposed_project_id=None,
            plan_id=None,
            idempotency_key="upload_concurrent",
            object_key=(
                "organizations/org_one/workflow-assistant/temporary/users/user_one/"
                f"conversations/conversation_one/attachments/asa_winner/{digest}"
            ),
            original_filename="brief.docx",
            mime_type=DOCX_MIME,
            byte_size=len(body),
            sha256=digest,
            classification=None,
            classification_payload={},
            revision=0,
            status="uploading",
            expires_at=NOW + ATTACHMENT_RETENTION,
            created_at=NOW,
            updated_at=NOW,
        )
        self.repository.records[winner.attachment_id] = winner

        uploaded = self.upload(
            attachment_id="asa_loser",
            idempotency_key="upload_concurrent",
            content=body,
        )

        self.assertEqual("asa_winner", uploaded.attachment_id)
        self.assertEqual("uploaded", uploaded.status)
        self.assertEqual([winner.object_key], self.store.put_calls)

    def test_ooxml_rejects_path_traversal_external_links_and_zip_bombs(self) -> None:
        cases = (
            office_package(unsafe_path=True),
            office_package(external_relationship=True),
        )
        for index, content in enumerate(cases):
            with self.subTest(index=index):
                with self.assertRaises(AttachmentValidationError):
                    self.upload(
                        attachment_id=f"asa_unsafe_{index}",
                        idempotency_key=f"unsafe_{index}",
                        content=content,
                    )

        with self.assertRaisesRegex(AttachmentValidationError, "too many entries"):
            self.upload(
                attachment_id="asa_entries",
                idempotency_key="unsafe_entries",
                content=office_package(extra_entries=2_046),
            )

        with self.assertRaisesRegex(
            AttachmentValidationError, "compression ratio"
        ):
            self.upload(
                attachment_id="asa_bomb",
                idempotency_key="unsafe_bomb",
                content=self._compressed_bomb(),
            )

    @staticmethod
    def _compressed_bomb() -> bytes:
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", "<Types />")
            archive.writestr("_rels/.rels", "<Relationships />")
            archive.writestr("word/document.xml", "x" * 200_000)
        return output.getvalue()

    def test_actor_and_conversation_scope_cannot_cross(self) -> None:
        attachment = self.upload()
        with self.assertRaises(AttachmentNotFound):
            self.service.get(
                actor=ActorIdentity("org_one", "user_two"),
                conversation_id="conversation_one",
                attachment_id=attachment.attachment_id,
                now=NOW,
            )
        with self.assertRaises(AttachmentNotFound):
            self.service.get(
                actor=self.actor,
                conversation_id="conversation_two",
                attachment_id=attachment.attachment_id,
                now=NOW,
            )

    def test_service_rejects_a_repository_scope_leak(self) -> None:
        attachment = self.upload()

        def leaking_get(**_kwargs):
            return replace(attachment, creator_user_id="user_two")

        self.repository.get_for_actor = leaking_get
        with self.assertRaises(AttachmentNotFound):
            self.service.get(
                actor=self.actor,
                conversation_id="conversation_one",
                attachment_id=attachment.attachment_id,
                now=NOW,
            )

    def test_download_is_short_lived_and_does_not_read_content(self) -> None:
        attachment = self.upload(original_filename="产品 说明.docx")
        result = self.service.create_download(
            actor=self.actor,
            conversation_id="conversation_one",
            attachment_id=attachment.attachment_id,
            now=NOW,
        )

        self.assertEqual(300, result.url_expires_seconds)
        self.assertIn("signed=1", result.download_url)
        self.assertEqual(DOCX_MIME, self.store.signed[0][2])
        self.assertIn("filename*=UTF-8''", self.store.signed[0][3])

    def test_download_rejects_unconfirmed_object_states(self) -> None:
        attachment = self.upload()
        for status in ("uploading", "failed", "rejecting", "expiring"):
            with self.subTest(status=status):
                self.repository.records[attachment.attachment_id] = replace(
                    attachment, status=status
                )
                with self.assertRaises(AttachmentConflict):
                    self.service.create_download(
                        actor=self.actor,
                        conversation_id="conversation_one",
                        attachment_id=attachment.attachment_id,
                        now=NOW,
                    )

    def test_reject_deletes_temporary_object_and_marks_metadata(self) -> None:
        attachment = self.upload()
        rejected = self.service.reject(
            actor=self.actor,
            conversation_id="conversation_one",
            attachment_id=attachment.attachment_id,
            now=NOW + timedelta(hours=1),
        )

        self.assertEqual("rejected", rejected.status)
        self.assertNotIn(attachment.attachment_id, self.repository.records)
        self.assertIn(attachment.object_key, self.store.deleted)
        self.assertNotIn(attachment.object_key, self.store.objects)

    def test_importing_or_imported_attachment_cannot_be_rejected(self) -> None:
        attachment = self.upload()
        self.repository.records[attachment.attachment_id] = replace(
            attachment, status="importing"
        )
        with self.assertRaises(AttachmentConflict):
            self.service.reject(
                actor=self.actor,
                conversation_id="conversation_one",
                attachment_id=attachment.attachment_id,
                now=NOW,
            )

    def test_failed_rejection_claim_is_drained_by_cleanup(self) -> None:
        attachment = self.upload()
        self.store.fail_delete.add(attachment.object_key)
        with self.assertRaises(Exception):
            self.service.reject(
                actor=self.actor,
                conversation_id="conversation_one",
                attachment_id=attachment.attachment_id,
                now=NOW + timedelta(hours=1),
            )
        self.assertEqual(
            "rejecting", self.repository.records[attachment.attachment_id].status
        )

        self.store.fail_delete.clear()
        self.assertEqual(1, self.service.cleanup_expired(before=NOW, limit=1))
        self.assertNotIn(attachment.attachment_id, self.repository.records)

    def test_cleanup_drains_more_than_one_batch_and_skips_imported(self) -> None:
        records = [self.upload()]
        for index in range(1, 3):
            records.append(
                self.upload(
                    attachment_id=f"asa_batch_{index}",
                    idempotency_key=f"upload_batch_{index}",
                )
            )
        imported = self.upload(
            attachment_id="asa_imported",
            idempotency_key="upload_imported",
        )
        cutoff = NOW + ATTACHMENT_RETENTION + timedelta(seconds=1)
        self.repository.records[imported.attachment_id] = replace(
            imported, status="imported"
        )

        self.assertEqual(3, self.service.cleanup_expired(before=cutoff, limit=1))
        self.assertIn(imported.attachment_id, self.repository.records)
        for record in records:
            self.assertNotIn(record.attachment_id, self.repository.records)

    def test_expired_cleanup_is_retryable_and_never_touches_fresh_objects(self) -> None:
        expired = self.upload()
        fresh = self.upload(
            attachment_id="asa_fresh",
            idempotency_key="upload_fresh",
        )
        cutoff = NOW + ATTACHMENT_RETENTION + timedelta(seconds=1)
        self.repository.records[fresh.attachment_id] = replace(
            fresh,
            expires_at=cutoff + timedelta(days=1),
        )
        self.store.fail_delete.add(expired.object_key)

        self.assertEqual(0, self.service.cleanup_expired(before=cutoff))
        self.assertEqual("expiring", self.repository.records[expired.attachment_id].status)
        self.store.fail_delete.clear()
        self.assertEqual(1, self.service.cleanup_expired(before=cutoff))
        self.assertNotIn(expired.attachment_id, self.repository.records)
        self.assertEqual("uploaded", self.repository.records[fresh.attachment_id].status)

    def test_expired_records_are_not_read_or_listed_before_cleanup(self) -> None:
        attachment = self.upload()
        after_expiry = attachment.expires_at + timedelta(seconds=1)
        with self.assertRaises(AttachmentNotFound):
            self.service.get(
                actor=self.actor,
                conversation_id="conversation_one",
                attachment_id=attachment.attachment_id,
                now=after_expiry,
            )
        self.assertEqual(
            (),
            self.service.list(
                actor=self.actor,
                conversation_id="conversation_one",
                now=after_expiry,
            ),
        )


if __name__ == "__main__":
    unittest.main()
