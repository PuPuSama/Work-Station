from __future__ import annotations

import hashlib
import json
import os
import sys
import unittest
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import sqlalchemy as sa
from fastapi import HTTPException, Request, Response


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from knowledge_agent.database import create_knowledge_engine  # noqa: E402
from knowledge_agent.http import (  # noqa: E402
    create_snapshot_raw_download,
    get_snapshot_evidence_manifest,
    preview_snapshot_evidence,
)
from knowledge_agent.schema import (  # noqa: E402
    knowledge_sources,
    projects,
    source_snapshots,
)
from services.access_control import (  # noqa: E402
    ActorIdentity,
    ProjectAccessDenied,
)
from services.object_store import ObjectStoreError, ObjectTooLarge  # noqa: E402
from services.server_snapshot_evidence import (  # noqa: E402
    MAX_NORMALIZED_PREVIEW_BYTES,
    MAX_PREVIEW_CHARACTERS,
    PostgresServerSnapshotEvidenceService,
    RAW_DOWNLOAD_CONTENT_DISPOSITION,
    RAW_DOWNLOAD_CONTENT_TYPE,
    RAW_DOWNLOAD_EXPIRES_SECONDS,
    RAW_DOWNLOAD_FILENAME,
    SnapshotEvidenceNotFound,
    SnapshotEvidenceUnavailable,
)


DATABASE_URL_ENV = "ARTICLE_AGENT_DATABASE_URL"
BUCKET = "snapshot-evidence-test"
FETCHED_AT = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)


@dataclass(frozen=True)
class FakeHead:
    key: str
    byte_size: int
    content_type: str
    sha256: str


class FakeAccess:
    def __init__(self) -> None:
        self.revoked = False
        self.allowed_projects: set[str] | None = None
        self.calls: list[tuple[ActorIdentity, str, str]] = []

    def require(self, actor, project_id, permission):
        self.calls.append((actor, project_id, permission))
        if self.revoked or (
            self.allowed_projects is not None
            and project_id not in self.allowed_projects
        ):
            raise ProjectAccessDenied("project access denied")
        return object()


class FakeStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.content_types: dict[str, str] = {}
        self.head_sizes: dict[str, int] = {}
        self.head_error: Exception | None = None
        self.get_error: Exception | None = None
        self.sign_error: Exception | None = None
        self.signed_url = (
            "https://signed.example.test/download?private=signature"
        )
        self.get_calls: list[tuple[str, int]] = []
        self.sign_calls: list[dict[str, object]] = []

    def add(self, key: str, body: bytes, content_type: str) -> None:
        self.objects[key] = bytes(body)
        self.content_types[key] = content_type

    def head(self, key: str) -> FakeHead:
        if self.head_error is not None:
            raise self.head_error
        if key not in self.objects:
            raise ObjectStoreError("provider secret missing object")
        return FakeHead(
            key=key,
            byte_size=self.head_sizes.get(key, len(self.objects[key])),
            content_type=self.content_types[key],
            sha256=hashlib.sha256(self.objects[key]).hexdigest(),
        )

    def get(self, key: str, *, max_bytes: int) -> bytes:
        self.get_calls.append((key, max_bytes))
        if self.get_error is not None:
            raise self.get_error
        body = self.objects[key]
        if len(body) > max_bytes:
            raise ObjectTooLarge("provider secret oversized response")
        return body

    def create_download_url(
        self,
        key: str,
        *,
        expires_seconds: int,
        response_content_type: str | None = None,
        response_content_disposition: str | None = None,
    ) -> str:
        self.sign_calls.append(
            {
                "key": key,
                "expires_seconds": expires_seconds,
                "response_content_type": response_content_type,
                "response_content_disposition": response_content_disposition,
            }
        )
        if self.sign_error is not None:
            raise self.sign_error
        return self.signed_url


@unittest.skipUnless(
    os.environ.get(DATABASE_URL_ENV),
    f"{DATABASE_URL_ENV} is required for PostgreSQL integration tests",
)
class M7ServerSnapshotEvidenceTests(unittest.TestCase):
    engine: sa.Engine

    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = create_knowledge_engine(os.environ[DATABASE_URL_ENV])
        with cls.engine.connect() as connection:
            connection.execute(sa.text("SELECT 1")).scalar_one()

    @classmethod
    def tearDownClass(cls) -> None:
        if hasattr(cls, "engine"):
            cls.engine.dispose()

    def setUp(self) -> None:
        self.prefix = f"snapshot-evidence-{uuid.uuid4().hex}"
        self.organization_id = f"{self.prefix}-org"
        self.actor = ActorIdentity(
            organization_id=self.organization_id,
            user_id=f"{self.prefix}-user",
        )
        self.project_a = f"{self.prefix}-project-a"
        self.project_b = f"{self.prefix}-project-b"
        self.source_a = f"{self.prefix}-source-a"
        self.source_other = f"{self.prefix}-source-other"
        self.source_rejected = f"{self.prefix}-source-rejected"
        self.current_snapshot = f"{self.prefix}-current"
        self.pending_snapshot = f"{self.prefix}-pending"
        self.historical_snapshot = f"{self.prefix}-historical"
        self.other_snapshot = f"{self.prefix}-other"
        self.rejected_snapshot = f"{self.prefix}-rejected"
        self.project_b_snapshot = f"{self.prefix}-project-b-current"
        self.store = FakeStore()
        self.access = FakeAccess()
        self.service = PostgresServerSnapshotEvidenceService(
            engine=self.engine,
            store=self.store,  # type: ignore[arg-type]
            bucket=BUCKET,
            access=self.access,  # type: ignore[arg-type]
        )
        self._seed_database()
        self._seed_objects()

    def tearDown(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                knowledge_sources.update()
                .where(
                    knowledge_sources.c.project_id.in_(
                        (self.project_a, self.project_b)
                    )
                )
                .values(
                    status="inbox",
                    current_snapshot_id=None,
                    pending_snapshot_id=None,
                )
            )
            connection.execute(
                source_snapshots.delete().where(
                    source_snapshots.c.project_id.in_((self.project_a, self.project_b))
                )
            )
            connection.execute(
                knowledge_sources.delete().where(
                    knowledge_sources.c.project_id.in_((self.project_a, self.project_b))
                )
            )
            connection.execute(
                projects.delete().where(
                    projects.c.project_id.in_((self.project_a, self.project_b))
                )
            )

    def _key(self, project_id: str, name: str) -> str:
        return (
            f"organizations/{self.organization_id}/projects/"
            f"{project_id}/evidence/{name}"
        )

    def _uri(self, project_id: str, name: str) -> str:
        return f"s3://{BUCKET}/{self._key(project_id, name)}"

    def _snapshot_values(
        self,
        project_id: str,
        source_id: str,
        snapshot_id: str,
        artifact_name: str,
    ) -> dict[str, object]:
        return {
            "project_id": project_id,
            "source_id": source_id,
            "snapshot_id": snapshot_id,
            "content_hash": hashlib.sha256(snapshot_id.encode()).hexdigest(),
            "parser_name": "snapshot-evidence-test",
            "parser_version": "1",
            "raw_artifact_uri": self._uri(project_id, f"{artifact_name}.raw"),
            "normalized_artifact_uri": self._uri(
                project_id,
                f"{artifact_name}.json",
            ),
            "fetched_at": FETCHED_AT,
        }

    def _seed_database(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                projects.insert(),
                (
                    {
                        "project_id": self.project_a,
                        "customer_name": "Snapshot Evidence A",
                        "official_domain": f"{self.prefix}-a.example.test",
                    },
                    {
                        "project_id": self.project_b,
                        "customer_name": "Snapshot Evidence B",
                        "official_domain": f"{self.prefix}-b.example.test",
                    },
                ),
            )
            connection.execute(
                knowledge_sources.insert(),
                (
                    {
                        "project_id": self.project_a,
                        "source_id": self.source_a,
                        "display_name": "Source A",
                        "source_kind": "private_file",
                        "trust_tier": "hard_fact",
                        "status": "published",
                        "public_source": False,
                        "current_snapshot_id": self.current_snapshot,
                        "pending_snapshot_id": self.pending_snapshot,
                    },
                    {
                        "project_id": self.project_a,
                        "source_id": self.source_other,
                        "display_name": "Other Source",
                        "source_kind": "private_file",
                        "trust_tier": "hard_fact",
                        "status": "inbox",
                        "public_source": False,
                        "current_snapshot_id": None,
                        "pending_snapshot_id": self.other_snapshot,
                    },
                    {
                        "project_id": self.project_a,
                        "source_id": self.source_rejected,
                        "display_name": "Rejected Source",
                        "source_kind": "private_file",
                        "trust_tier": "hard_fact",
                        "status": "rejected",
                        "public_source": False,
                        "current_snapshot_id": self.rejected_snapshot,
                        "pending_snapshot_id": None,
                    },
                    {
                        "project_id": self.project_b,
                        "source_id": self.source_a,
                        "display_name": "Project B Source",
                        "source_kind": "private_file",
                        "trust_tier": "hard_fact",
                        "status": "published",
                        "public_source": False,
                        "current_snapshot_id": self.project_b_snapshot,
                        "pending_snapshot_id": None,
                    },
                ),
            )
            connection.execute(
                source_snapshots.insert(),
                (
                    self._snapshot_values(
                        self.project_a,
                        self.source_a,
                        self.current_snapshot,
                        "current",
                    ),
                    self._snapshot_values(
                        self.project_a,
                        self.source_a,
                        self.pending_snapshot,
                        "pending",
                    ),
                    self._snapshot_values(
                        self.project_a,
                        self.source_a,
                        self.historical_snapshot,
                        "historical",
                    ),
                    self._snapshot_values(
                        self.project_a,
                        self.source_other,
                        self.other_snapshot,
                        "other",
                    ),
                    self._snapshot_values(
                        self.project_a,
                        self.source_rejected,
                        self.rejected_snapshot,
                        "rejected",
                    ),
                    self._snapshot_values(
                        self.project_b,
                        self.source_a,
                        self.project_b_snapshot,
                        "project-b",
                    ),
                ),
            )

    def _seed_objects(self) -> None:
        snapshots = (
            (self.project_a, "current"),
            (self.project_a, "pending"),
            (self.project_a, "historical"),
            (self.project_a, "other"),
            (self.project_a, "rejected"),
            (self.project_b, "project-b"),
        )
        for project_id, name in snapshots:
            self.store.add(
                self._key(project_id, f"{name}.raw"),
                f"raw-{name}".encode(),
                "text/html",
            )
            self.store.add(
                self._key(project_id, f"{name}.json"),
                json.dumps(
                    {
                        "title": f"Title {name}",
                        "blocks": [
                            {"text": f"Block one {name}"},
                            {"text": f"Block two {name}"},
                        ],
                        "metadata": {"secret_url": "https://private.test"},
                    }
                ).encode(),
                "application/json; charset=utf-8",
            )

    def _manifest(self, snapshot_id: str | None = None):
        return self.service.get_manifest(
            actor=self.actor,
            project_id=self.project_a,
            source_id=self.source_a,
            snapshot_id=snapshot_id or self.current_snapshot,
        )

    def _preview(self, snapshot_id: str | None = None):
        return self.service.get_preview(
            actor=self.actor,
            project_id=self.project_a,
            source_id=self.source_a,
            snapshot_id=snapshot_id or self.current_snapshot,
        )

    def _download(self, snapshot_id: str | None = None):
        return self.service.create_raw_download(
            actor=self.actor,
            project_id=self.project_a,
            source_id=self.source_a,
            snapshot_id=snapshot_id or self.current_snapshot,
        )

    def _http_request(self, *, server_mode: bool = True) -> Request:
        application = SimpleNamespace(
            state=SimpleNamespace(
                knowledge_agent_runtime=object(),
                server_mode_enabled=server_mode,
                server_snapshot_evidence=self.service,
            )
        )
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/knowledge/test",
                "headers": [],
                "query_string": b"",
                "server": ("testserver", 80),
                "client": ("testclient", 50000),
                "scheme": "http",
                "app": application,
            }
        )
        request.state.actor_identity = self.actor
        request.state.project_id = self.project_a
        return request

    def test_manifest_exposes_safe_metadata_without_object_identity(self) -> None:
        manifest = self._manifest()

        self.assertEqual(manifest.slot, "current")
        self.assertTrue(manifest.raw_available)
        self.assertEqual(manifest.raw_content_type, "text/html")
        self.assertEqual(manifest.raw_byte_size, len(b"raw-current"))
        self.assertTrue(manifest.normalized_available)
        self.assertTrue(manifest.preview_supported)
        public = asdict(manifest)
        self.assertFalse(
            any(
                "uri" in key or "key" in key or "hash" in key
                for key in public
            )
        )
        self.assertNotIn("organizations/", repr(manifest))

    def test_pending_snapshot_is_addressed_by_its_exact_identity(self) -> None:
        manifest = self._manifest(self.pending_snapshot)
        preview = self._preview(self.pending_snapshot)

        self.assertEqual(manifest.slot, "pending")
        self.assertEqual(preview.slot, "pending")
        self.assertIn("Title pending", preview.text)
        self.assertNotIn("Title current", preview.text)

    def test_preview_rechecks_pending_pointer_after_manifest(self) -> None:
        manifest = self._manifest(self.pending_snapshot)
        self.assertEqual(manifest.slot, "pending")
        with self.engine.begin() as connection:
            connection.execute(
                knowledge_sources.update()
                .where(
                    knowledge_sources.c.project_id == self.project_a,
                    knowledge_sources.c.source_id == self.source_a,
                )
                .values(pending_snapshot_id=None)
            )

        with self.assertRaises(SnapshotEvidenceNotFound):
            self._preview(self.pending_snapshot)

    def test_historical_and_rejected_current_snapshots_are_not_visible(self) -> None:
        with self.assertRaisesRegex(
            SnapshotEvidenceNotFound,
            "^Snapshot evidence was not found\\.$",
        ):
            self._manifest(self.historical_snapshot)

        with self.assertRaisesRegex(
            SnapshotEvidenceNotFound,
            "^Snapshot evidence was not found\\.$",
        ):
            self.service.get_manifest(
                actor=self.actor,
                project_id=self.project_a,
                source_id=self.source_rejected,
                snapshot_id=self.rejected_snapshot,
            )

        with self.engine.begin() as connection:
            connection.execute(
                knowledge_sources.update()
                .where(
                    knowledge_sources.c.project_id == self.project_a,
                    knowledge_sources.c.source_id == self.source_rejected,
                )
                .values(
                    current_snapshot_id=None,
                    pending_snapshot_id=self.rejected_snapshot,
                )
            )
        with self.assertRaises(SnapshotEvidenceNotFound):
            self.service.get_manifest(
                actor=self.actor,
                project_id=self.project_a,
                source_id=self.source_rejected,
                snapshot_id=self.rejected_snapshot,
            )

    def test_source_and_snapshot_identity_cannot_be_mixed(self) -> None:
        with self.assertRaises(SnapshotEvidenceNotFound):
            self.service.get_manifest(
                actor=self.actor,
                project_id=self.project_a,
                source_id=self.source_other,
                snapshot_id=self.pending_snapshot,
            )

    def test_cross_project_and_revoked_access_are_denied_before_object_io(self) -> None:
        self.access.allowed_projects = {self.project_a}
        with self.assertRaises(ProjectAccessDenied):
            self.service.get_manifest(
                actor=self.actor,
                project_id=self.project_b,
                source_id=self.source_a,
                snapshot_id=self.project_b_snapshot,
            )

        self.access.revoked = True
        with self.assertRaises(ProjectAccessDenied):
            self._manifest()
        self.assertEqual(self.store.sign_calls, [])

    def test_each_public_operation_reauthorizes_project_view(self) -> None:
        self._manifest()
        manifest_calls = len(self.access.calls)
        self._preview()
        preview_calls = len(self.access.calls) - manifest_calls
        self._download()
        download_calls = len(self.access.calls) - manifest_calls - preview_calls

        self.assertGreaterEqual(manifest_calls, 1)
        self.assertGreaterEqual(preview_calls, 2)
        self.assertGreaterEqual(download_calls, 2)
        self.assertTrue(all(call[2] == "project.view" for call in self.access.calls))

    def test_invalid_object_scopes_are_rejected_without_uri_leaks(self) -> None:
        raw_column = source_snapshots.c.raw_artifact_uri
        invalid_uris = (
            f"s3://other-bucket/{self._key(self.project_a, 'current.raw')}",
            f"{self._uri(self.project_a, 'current.raw')}?token=secret",
            f"{self._uri(self.project_a, 'current.raw')}#secret",
            f"s3://{BUCKET}/organizations/other-org/projects/"
            f"{self.project_a}/evidence/current.raw",
            f"s3://{BUCKET}/organizations/{self.organization_id}/projects/"
            f"{self.project_a}-other/evidence/current.raw",
            f"s3://{BUCKET}/organizations/{self.organization_id}/projects/"
            f"{self.project_a}/%2e%2e/private/current.raw",
        )
        original = self._uri(self.project_a, "current.raw")
        for invalid_uri in invalid_uris:
            with self.subTest(invalid_uri=invalid_uri):
                with self.engine.begin() as connection:
                    connection.execute(
                        source_snapshots.update()
                        .where(
                            source_snapshots.c.project_id == self.project_a,
                            source_snapshots.c.snapshot_id == self.current_snapshot,
                        )
                        .values(raw_artifact_uri=invalid_uri)
                    )
                with self.assertRaises(SnapshotEvidenceUnavailable) as raised:
                    self._manifest()
                self.assertNotIn("secret", str(raised.exception))
                self.assertNotIn(BUCKET, str(raised.exception))
        with self.engine.begin() as connection:
            connection.execute(
                source_snapshots.update()
                .where(
                    source_snapshots.c.project_id == self.project_a,
                    source_snapshots.c.snapshot_id == self.current_snapshot,
                )
                .values({raw_column: original})
            )

    def test_manifest_sanitizes_unsafe_provider_content_type(self) -> None:
        raw_key = self._key(self.project_a, "current.raw")
        self.store.content_types[raw_key] = "text/html\r\nX-Secret: value"

        manifest = self._manifest()

        self.assertEqual(manifest.raw_content_type, RAW_DOWNLOAD_CONTENT_TYPE)

    def test_preview_extracts_only_title_and_block_text(self) -> None:
        normalized_key = self._key(self.project_a, "current.json")
        self.store.objects[normalized_key] = json.dumps(
            {
                "page": {
                    "title": "Visible title",
                    "blocks": [
                        {"text": "Visible block"},
                        {"text": "  "},
                        {"locator": {"private": "hidden locator"}},
                    ],
                    "canonical_url": "https://private.example.test/page",
                },
                "source": {"requested_url": "https://private.example.test"},
            }
        ).encode()

        preview = self._preview()

        self.assertEqual(preview.text, "Visible title\n\nVisible block")
        self.assertEqual(preview.block_count, 1)
        self.assertFalse(preview.truncated)
        self.assertNotIn("private.example.test", preview.text)
        self.assertEqual(
            self.store.get_calls[-1][1],
            MAX_NORMALIZED_PREVIEW_BYTES,
        )

    def test_preview_rejects_oversized_or_non_json_artifacts_before_get(self) -> None:
        normalized_key = self._key(self.project_a, "current.json")
        self.store.head_sizes[normalized_key] = MAX_NORMALIZED_PREVIEW_BYTES + 1
        with self.assertRaisesRegex(
            SnapshotEvidenceUnavailable,
            "^Snapshot evidence preview is temporarily unavailable\\.$",
        ):
            self._preview()
        self.assertEqual(self.store.get_calls, [])

        self.store.head_sizes.pop(normalized_key)
        self.store.content_types[normalized_key] = "text/plain"
        with self.assertRaises(SnapshotEvidenceUnavailable):
            self._preview()
        self.assertEqual(self.store.get_calls, [])

    def test_preview_rejects_invalid_json_with_stable_error(self) -> None:
        normalized_key = self._key(self.project_a, "current.json")
        self.store.objects[normalized_key] = b'{"title":"secret",broken'

        with self.assertRaises(SnapshotEvidenceUnavailable) as raised:
            self._preview()

        self.assertEqual(
            str(raised.exception),
            "Snapshot evidence preview is temporarily unavailable.",
        )
        self.assertNotIn("secret", str(raised.exception))

    def test_preview_rejects_object_bytes_that_do_not_match_head_hash(self) -> None:
        normalized_key = self._key(self.project_a, "current.json")
        original_head = self.store.head(normalized_key)
        self.store.objects[normalized_key] = b'{"title":"changed"}'

        def stale_head(key: str) -> FakeHead:
            self.assertEqual(key, normalized_key)
            return original_head

        self.store.head = stale_head  # type: ignore[method-assign]

        with self.assertRaisesRegex(
            SnapshotEvidenceUnavailable,
            "^Snapshot evidence preview is temporarily unavailable\\.$",
        ):
            self._preview()

    def test_preview_is_truncated_at_a_stable_output_bound(self) -> None:
        normalized_key = self._key(self.project_a, "current.json")
        self.store.objects[normalized_key] = json.dumps(
            {"title": "Long", "blocks": [{"text": "x" * 80_000}]}
        ).encode()

        preview = self._preview()

        self.assertTrue(preview.truncated)
        self.assertEqual(len(preview.text), MAX_PREVIEW_CHARACTERS)
        self.assertEqual(preview.block_count, 1)

    def test_provider_head_get_and_sign_errors_are_redacted(self) -> None:
        cases = (
            (
                "head_error",
                ObjectStoreError("provider endpoint access-key secret"),
                self._manifest,
                "Snapshot evidence is temporarily unavailable.",
            ),
            (
                "get_error",
                ObjectStoreError("provider body contained secret"),
                self._preview,
                "Snapshot evidence preview is temporarily unavailable.",
            ),
            (
                "sign_error",
                ObjectStoreError("provider signature secret"),
                self._download,
                "Snapshot evidence download is temporarily unavailable.",
            ),
        )
        for attribute, error, operation, expected in cases:
            with self.subTest(attribute=attribute):
                setattr(self.store, attribute, error)
                with self.assertRaises(SnapshotEvidenceUnavailable) as raised:
                    operation()
                self.assertEqual(str(raised.exception), expected)
                self.assertNotIn("secret", str(raised.exception))
                setattr(self.store, attribute, None)

    def test_raw_download_forces_fixed_expiry_and_attachment_headers(self) -> None:
        download = self._download(self.pending_snapshot)

        self.assertEqual(download.slot, "pending")
        self.assertTrue(
            download.download_url.startswith("https://signed.example.test/")
        )
        self.assertEqual(download.expires_seconds, RAW_DOWNLOAD_EXPIRES_SECONDS)
        self.assertEqual(download.content_type, RAW_DOWNLOAD_CONTENT_TYPE)
        self.assertEqual(download.content_disposition, RAW_DOWNLOAD_CONTENT_DISPOSITION)
        self.assertEqual(download.filename, RAW_DOWNLOAD_FILENAME)
        self.assertEqual(
            self.store.sign_calls[-1],
            {
                "key": self._key(self.project_a, "pending.raw"),
                "expires_seconds": 60,
                "response_content_type": "application/octet-stream",
                "response_content_disposition": (
                    'attachment; filename="snapshot-evidence.bin"'
                ),
            },
        )

    def test_raw_download_rejects_unsafe_signed_url(self) -> None:
        for value in (
            "",
            "javascript:alert(1)",
            "https://user:secret@signed.example.test/download",
            "https://signed.example.test/download#secret",
            "/relative/download",
        ):
            with self.subTest(value=value):
                self.store.signed_url = value
                with self.assertRaisesRegex(
                    SnapshotEvidenceUnavailable,
                    "^Snapshot evidence download is temporarily unavailable\\.$",
                ):
                    self._download()

    def test_http_routes_preserve_exact_identity_and_disable_caching(self) -> None:
        request = self._http_request()

        manifest_headers = Response()
        manifest = get_snapshot_evidence_manifest(
            self.project_a,
            self.source_a,
            self.pending_snapshot,
            request,
            manifest_headers,
        )
        preview_headers = Response()
        preview = preview_snapshot_evidence(
            self.project_a,
            self.source_a,
            self.pending_snapshot,
            request,
            preview_headers,
        )
        download_headers = Response()
        download = create_snapshot_raw_download(
            self.project_a,
            self.source_a,
            self.pending_snapshot,
            request,
            download_headers,
        )

        self.assertEqual(manifest.snapshot_id, self.pending_snapshot)
        self.assertEqual(manifest.slot, "pending")
        self.assertEqual(preview.snapshot_id, self.pending_snapshot)
        self.assertEqual(preview.slot, "pending")
        self.assertEqual(download.snapshot_id, self.pending_snapshot)
        self.assertEqual(download.slot, "pending")
        self.assertEqual(
            download.download_url,
            "https://signed.example.test/download?private=signature",
        )
        for response in (
            manifest_headers,
            preview_headers,
            download_headers,
        ):
            self.assertEqual(response.headers["cache-control"], "no-store")

    def test_http_routes_are_server_only_and_hide_historical_identity(self) -> None:
        with self.assertRaises(HTTPException) as local_error:
            get_snapshot_evidence_manifest(
                self.project_a,
                self.source_a,
                self.current_snapshot,
                self._http_request(server_mode=False),
                Response(),
            )
        self.assertEqual(local_error.exception.status_code, 409)

        with self.assertRaises(HTTPException) as historical_error:
            get_snapshot_evidence_manifest(
                self.project_a,
                self.source_a,
                self.historical_snapshot,
                self._http_request(),
                Response(),
            )
        self.assertEqual(historical_error.exception.status_code, 404)
        self.assertNotIn(
            self.historical_snapshot,
            str(historical_error.exception.detail),
        )


if __name__ == "__main__":
    unittest.main()
