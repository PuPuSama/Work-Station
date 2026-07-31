from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import threading
import time
import unittest
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

import sqlalchemy as sa
from fastapi.testclient import TestClient
from docx import Document
from PIL import Image


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from knowledge_agent.database import create_knowledge_engine  # noqa: E402
from knowledge_agent.assets import (  # noqa: E402
    KnowledgeAsset,
    PostgresKnowledgeAssetRepository,
)
from knowledge_agent.object_storage import (  # noqa: E402
    ProjectKnowledgeObjectService,
)
from knowledge_agent.schema import (  # noqa: E402
    knowledge_assets,
    knowledge_chunks,
    knowledge_product_asset_evidence,
    knowledge_product_source_evidence,
    knowledge_products,
    knowledge_sources,
    projects,
    snapshot_assets,
    source_snapshots,
)
from models import TaskRecord  # noqa: E402
from server_schema import (  # noqa: E402
    article_tasks,
    background_jobs,
    job_batches,
    organizations,
    project_memberships,
    project_ownership,
    task_store_state,
    workspace_users,
)
from services.postgres_task_repository import (  # noqa: E402
    PostgresTaskRepository,
)
from services.server_auth import (  # noqa: E402
    SERVER_AUTH_COOKIE_NAME,
    ServerActorSessionCodec,
)
from services.access_control import (  # noqa: E402
    ActorIdentity,
    PostgresProjectAccessRepository,
    ProjectAccessService,
)
from services.object_store import (  # noqa: E402
    StoredObject,
    build_project_object_key,
)
from services.job_queue import JobCancelled, JobConflict  # noqa: E402
from services.postgres_job_queue import PostgresJobQueue  # noqa: E402
from services.server_product_rediscovery import (  # noqa: E402
    ProductRediscoveryCommand,
    ProductRediscoveryUnavailable,
    ServerProductRediscoveryHandler,
    ServerProductRediscoveryRegistry,
)
from services.server_outline_generation import (  # noqa: E402
    PostgresPublishedOutlineContext,
    ServerOutlineGenerationHandler,
    ServerOutlineGenerationRegistry,
)
from services.server_title_generation import (  # noqa: E402
    ServerTitleGenerationHandler,
    ServerTitleGenerationRegistry,
    TitleTemplateReference,
)
from services.server_article_generation import (  # noqa: E402
    ServerArticleGenerationHandler,
    ServerArticleGenerationRegistry,
)
from services.server_project_tasks import (  # noqa: E402
    ServerProjectTaskStoreFactory,
)


SERVER_ARTICLE = """# Example Buyer Guide

This introduction points readers to [example.com](https://example.com/) before the detailed guidance.

## Buyer Checks

### Confirm the application

Keep the original application guidance.

### Compare evidence

Keep the original evidence guidance.

## FAQ

**Q: What should buyers send?**

A: Send requirements and quantities.

**Q: When should buyers request samples?**

A: Request samples before approval.

**Q: Why compare supplier capability?**

A: Capability affects quality and support.
"""

SERVER_TDK_RESPONSE = """{
  "description": "Compare buyer requirements, product evidence, and supplier capability for a more reliable B2B sourcing decision.",
  "keywords": [
    "buyer requirements",
    "product evidence",
    "supplier capability",
    "B2B sourcing",
    "quality checks",
    "purchase planning"
  ]
}"""


class StubServerTdkLlm:
    ready = True

    def __init__(self, *args, **kwargs) -> None:
        del args, kwargs

    def chat(self, messages, temperature=0.7, max_tokens=1800):
        del messages, temperature, max_tokens
        return SERVER_TDK_RESPONSE


class RecordingOutlineProvider:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def generate(
        self,
        task,
        *,
        prompt_snapshot,
        context_chunks,
    ):
        self.calls.append(
            {
                "task_id": task.id,
                "prompt_id": prompt_snapshot.prompt_id,
                "prompt_version": prompt_snapshot.version,
                "prompt_source": prompt_snapshot.source,
                "chunk_ids": [
                    chunk.chunk_id for chunk in context_chunks
                ],
                "chunk_text": [
                    chunk.text for chunk in context_chunks
                ],
            }
        )
        return "## Generated server outline\n\n### Evidence-led section"


class RecordingTitleProvider:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def generate(
        self,
        task,
        *,
        title_count,
        context_chunks,
    ):
        self.calls.append(
            {
                "task_id": task.id,
                "title_count": title_count,
                "chunk_ids": [
                    chunk.chunk_id for chunk in context_chunks
                ],
            }
        )
        return tuple(
            f"Server candidate title {index}"
            for index in range(1, title_count + 1)
        )


class RecordingArticleProvider:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def generate(
        self,
        task,
        *,
        target_words,
        prompt_snapshot,
        context_chunks,
    ):
        self.calls.append(
            {
                "task_id": task.id,
                "target_words": target_words,
                "prompt_source": prompt_snapshot.source,
                "prompt_version": prompt_snapshot.version,
                "chunk_ids": [
                    chunk.chunk_id for chunk in context_chunks
                ],
                "chunk_text": [
                    chunk.text for chunk in context_chunks
                ],
            }
        )
        return SERVER_ARTICLE


class FakeDownloadStore:
    def __init__(self) -> None:
        self.signed: list[tuple[str, int]] = []
        self.objects: dict[str, bytes] = {}
        self.put_calls: list[str] = []

    def check_ready(self):
        return None

    def put(self, **kwargs):
        key = str(kwargs["key"])
        data = bytes(kwargs["data"])
        content_type = str(kwargs["content_type"])
        self.objects[key] = data
        self.put_calls.append(key)
        return StoredObject(
            key=key,
            content_hash=hashlib.sha256(data).hexdigest(),
            content_type=content_type,
            byte_size=len(data),
            etag="etag",
        )

    def get(self, key, *, max_bytes):
        return self.objects[key][: max_bytes + 1]

    def create_download_url(self, key, *, expires_seconds):
        self.signed.append((key, expires_seconds))
        return f"https://signed.example.test/{key}"

    def delete(self, key):
        raise AssertionError("not used")


class RecordingAuditWriter:
    def __init__(self) -> None:
        self.events: list[object] = []

    def append(self, connection, event) -> None:
        if not connection.in_transaction():
            raise AssertionError("audit must share the Task transaction")
        self.events.append(event)


@unittest.skipUnless(
    os.environ.get("ARTICLE_AGENT_DATABASE_URL"),
    "ARTICLE_AGENT_DATABASE_URL is required for PostgreSQL integration tests",
)
class ServerProjectTaskApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = create_knowledge_engine(
            os.environ["ARTICLE_AGENT_DATABASE_URL"]
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    def setUp(self) -> None:
        prefix = f"m7-server-tasks-{uuid.uuid4().hex}"
        self.org_a = f"{prefix}-org-a"
        self.org_b = f"{prefix}-org-b"
        self.user_a = f"{prefix}-user-a"
        self.user_b = f"{prefix}-user-b"
        self.project_a = f"{prefix}.a.example.test"
        self.project_b = f"{prefix}.b.example.test"
        self.task_a = f"{prefix}-task-a"
        self.task_b = f"{prefix}-task-b"
        self.asset_a = f"{prefix}-asset-a"
        self.bad_asset = f"{prefix}-bad-asset"
        self.product_a = f"{prefix}-product-a"
        self.product_b = f"{prefix}-product-b"
        self.inbox_product = f"{prefix}-inbox-product"
        self.unpublished_product = f"{prefix}-unpublished-product"
        with self.engine.begin() as connection:
            connection.execute(
                organizations.insert(),
                (
                    {"organization_id": self.org_a, "name": "Org A"},
                    {"organization_id": self.org_b, "name": "Org B"},
                ),
            )
            connection.execute(
                workspace_users.insert(),
                (
                    {
                        "organization_id": self.org_a,
                        "user_id": self.user_a,
                        "display_name": "User A",
                    },
                    {
                        "organization_id": self.org_b,
                        "user_id": self.user_b,
                        "display_name": "User B",
                    },
                ),
            )
            connection.execute(
                projects.insert(),
                (
                    {
                        "project_id": self.project_a,
                        "customer_name": "Project A",
                        "official_domain": self.project_a,
                    },
                    {
                        "project_id": self.project_b,
                        "customer_name": "Project B",
                        "official_domain": self.project_b,
                    },
                ),
            )
            connection.execute(
                project_ownership.insert(),
                (
                    {
                        "project_id": self.project_a,
                        "organization_id": self.org_a,
                    },
                    {
                        "project_id": self.project_b,
                        "organization_id": self.org_b,
                    },
                ),
            )
            connection.execute(
                project_memberships.insert(),
                (
                    {
                        "organization_id": self.org_a,
                        "project_id": self.project_a,
                        "user_id": self.user_a,
                        "role": "viewer",
                        "granted_by_user_id": self.user_a,
                    },
                    {
                        "organization_id": self.org_b,
                        "project_id": self.project_b,
                        "user_id": self.user_b,
                        "role": "viewer",
                        "granted_by_user_id": self.user_b,
                    },
                ),
            )
        self._task_repository(
            organization_id=self.org_a,
            project_id=self.project_a,
        ).upsert(self._task(self.task_a, self.project_a, 2))
        self._task_repository(
            organization_id=self.org_b,
            project_id=self.project_b,
        ).upsert(self._task(self.task_b, self.project_b, 1))
        asset_repository = PostgresKnowledgeAssetRepository(self.engine)
        content_hash = hashlib.sha256(b"asset-a").hexdigest()
        object_key = build_project_object_key(
            self.org_a,
            self.project_a,
            content_hash,
        )
        asset_repository.put_asset(
            KnowledgeAsset(
                project_id=self.project_a,
                asset_id=self.asset_a,
                content_hash=content_hash,
                artifact_uri=f"s3://private-bucket/{object_key}",
                content_type="image/webp",
                byte_size=7,
            )
        )
        bad_hash = hashlib.sha256(b"bad-asset").hexdigest()
        asset_repository.put_asset(
            KnowledgeAsset(
                project_id=self.project_a,
                asset_id=self.bad_asset,
                content_hash=bad_hash,
                artifact_uri=(
                    "s3://private-bucket/organizations/"
                    f"{self.org_b}/projects/{self.project_a}/"
                    f"blobs/{bad_hash[:2]}/{bad_hash}"
                ),
                content_type="image/webp",
                byte_size=9,
            )
        )

    def tearDown(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                knowledge_chunks.delete().where(
                    knowledge_chunks.c.project_id.in_(
                        (self.project_a, self.project_b)
                    )
                )
            )
            connection.execute(
                knowledge_product_asset_evidence.delete().where(
                    knowledge_product_asset_evidence.c.project_id.in_(
                        (self.project_a, self.project_b)
                    )
                )
            )
            connection.execute(
                knowledge_product_source_evidence.delete().where(
                    knowledge_product_source_evidence.c.project_id.in_(
                        (self.project_a, self.project_b)
                    )
                )
            )
            connection.execute(
                knowledge_products.delete().where(
                    knowledge_products.c.project_id.in_(
                        (self.project_a, self.project_b)
                    )
                )
            )
            connection.execute(
                snapshot_assets.delete().where(
                    snapshot_assets.c.project_id.in_(
                        (self.project_a, self.project_b)
                    )
                )
            )
            connection.execute(
                knowledge_assets.delete().where(
                    knowledge_assets.c.project_id.in_(
                        (self.project_a, self.project_b)
                    )
                )
            )
            connection.execute(
                source_snapshots.delete().where(
                    source_snapshots.c.project_id.in_(
                        (self.project_a, self.project_b)
                    )
                )
            )
            connection.execute(
                knowledge_sources.delete().where(
                    knowledge_sources.c.project_id.in_(
                        (self.project_a, self.project_b)
                    )
                )
            )
            connection.execute(
                background_jobs.delete().where(
                    background_jobs.c.organization_id.in_(
                        (self.org_a, self.org_b)
                    )
                )
            )
            connection.execute(
                job_batches.delete().where(
                    job_batches.c.organization_id.in_(
                        (self.org_a, self.org_b)
                    )
                )
            )
            connection.execute(
                article_tasks.delete().where(
                    article_tasks.c.organization_id.in_(
                        (self.org_a, self.org_b)
                    )
                )
            )
            connection.execute(
                task_store_state.delete().where(
                    task_store_state.c.organization_id.in_(
                        (self.org_a, self.org_b)
                    )
                )
            )
            connection.execute(
                project_memberships.delete().where(
                    project_memberships.c.organization_id.in_(
                        (self.org_a, self.org_b)
                    )
                )
            )
            connection.execute(
                project_ownership.delete().where(
                    project_ownership.c.project_id.in_(
                        (self.project_a, self.project_b)
                    )
                )
            )
            connection.execute(
                workspace_users.delete().where(
                    workspace_users.c.organization_id.in_(
                        (self.org_a, self.org_b)
                    )
                )
            )
            connection.execute(
                projects.delete().where(
                    projects.c.project_id.in_(
                        (self.project_a, self.project_b)
                    )
                )
            )
            connection.execute(
                organizations.delete().where(
                    organizations.c.organization_id.in_(
                        (self.org_a, self.org_b)
                    )
                )
            )

    def _task_repository(
        self,
        *,
        organization_id: str,
        project_id: str,
    ) -> PostgresTaskRepository:
        return PostgresTaskRepository(
            self.engine,
            organization_id=organization_id,
            project_id=project_id,
        )

    def _install_recording_audit(
        self,
        application,
        config,
    ) -> RecordingAuditWriter:
        audit = RecordingAuditWriter()
        application.state.server_project_task_store_factory = (
            ServerProjectTaskStoreFactory(
                self.engine,
                config,
                audit=audit,
            )
        )
        return audit

    @staticmethod
    def _task(task_id: str, project_id: str, topic_index: int) -> dict:
        return TaskRecord(
            id=task_id,
            week_folder="server",
            customer=project_id,
            topic_index=topic_index,
            topic=f"Topic {topic_index}",
            status="title_selected",
            selected_title=f"Selected topic {topic_index}",
            task_dir=f"/server/{task_id}",
            created_at="2026-07-30T00:00:00+00:00",
            updated_at="2026-07-30T00:00:00+00:00",
        ).model_dump(mode="json")

    @staticmethod
    def _image_bytes(color: str) -> bytes:
        output = BytesIO()
        Image.new("RGB", (320, 240), color).save(
            output,
            format="PNG",
        )
        return output.getvalue()

    def _store_outline_context(
        self,
        *,
        project_id: str,
        suffix: str,
        text: str,
        status: str = "published",
    ) -> str:
        source_id = f"{suffix}-source"
        snapshot_id = f"{suffix}-snapshot"
        chunk_id = f"{snapshot_id}:chunk-0"
        with self.engine.begin() as connection:
            connection.execute(
                knowledge_sources.insert().values(
                    project_id=project_id,
                    source_id=source_id,
                    display_name=f"{suffix} source",
                    source_kind="knowledge_page",
                    trust_tier="hard_fact",
                    status=status,
                    public_source=True,
                    canonical_url=f"https://{project_id}/{suffix}",
                    current_snapshot_id=(
                        snapshot_id if status == "published" else None
                    ),
                )
            )
            connection.execute(
                source_snapshots.insert().values(
                    project_id=project_id,
                    snapshot_id=snapshot_id,
                    source_id=source_id,
                    content_hash=hashlib.sha256(
                        text.encode("utf-8")
                    ).hexdigest(),
                    parser_name="m7-outline-test",
                    parser_version="1",
                    fetched_at=datetime.now(timezone.utc),
                )
            )
            connection.execute(
                knowledge_chunks.insert().values(
                    project_id=project_id,
                    chunk_id=chunk_id,
                    source_id=source_id,
                    snapshot_id=snapshot_id,
                    ordinal=0,
                    heading_path=["Published facts"],
                    text=text,
                    locator={"section": "facts"},
                    metadata={},
                )
            )
        return chunk_id

    def _store_selectable_product(
        self,
        *,
        project_id: str,
        product_id: str,
        asset_id: str | None = None,
        status: str = "confirmed",
        source_status: str = "published",
    ) -> None:
        source_id = f"{product_id}-source"
        snapshot_id = f"{product_id}-snapshot"
        canonical_url = f"https://{project_id}/products/{product_id}"
        with self.engine.begin() as connection:
            connection.execute(
                knowledge_sources.insert().values(
                    project_id=project_id,
                    source_id=source_id,
                    display_name=f"{product_id} source",
                    source_kind="product_detail",
                    trust_tier="hard_fact",
                    status=source_status,
                    public_source=True,
                    canonical_url=canonical_url,
                    current_snapshot_id=snapshot_id,
                )
            )
            connection.execute(
                source_snapshots.insert().values(
                    project_id=project_id,
                    snapshot_id=snapshot_id,
                    source_id=source_id,
                    content_hash=hashlib.sha256(
                        product_id.encode("utf-8")
                    ).hexdigest(),
                    parser_name="m7-test",
                    parser_version="1",
                    fetched_at=datetime.now(timezone.utc),
                )
            )
            connection.execute(
                knowledge_products.insert().values(
                    project_id=project_id,
                    product_id=product_id,
                    name=f"Product {product_id}",
                    status=status,
                    canonical_url=canonical_url,
                    category_path=["Fasteners", "Anchors"],
                    metadata={
                        "description": "Published product description.",
                        "main_content_facts": [
                            "Fact one.",
                            "Fact two.",
                        ],
                        "specification_tables": [
                            {
                                "headers": ["Property", "Value"],
                                "rows": [
                                    ["Material", "316 stainless steel"],
                                    ["Length", "50 mm"],
                                ],
                            }
                        ],
                    },
                )
            )
            connection.execute(
                knowledge_product_source_evidence.insert().values(
                    project_id=project_id,
                    product_id=product_id,
                    source_id=source_id,
                    snapshot_id=snapshot_id,
                    relation="primary_detail",
                    confidence=0.99,
                    reason="M7 server selection test evidence",
                    metadata={
                        "selection_projection": {
                            "schema_version": 1,
                            "name": f"Product {product_id}",
                            "canonical_url": canonical_url,
                            "description": "Published product description.",
                            "reference_facts": [
                                "Fact one.",
                                "Fact two.",
                            ],
                            "specification_tables": [
                                {
                                    "headers": ["Property", "Value"],
                                    "rows": [
                                        [
                                            "Material",
                                            "316 stainless steel",
                                        ],
                                        ["Length", "50 mm"],
                                    ],
                                }
                            ],
                        }
                    },
                )
            )
            if asset_id is not None:
                connection.execute(
                    snapshot_assets.insert().values(
                        project_id=project_id,
                        source_id=source_id,
                        snapshot_id=snapshot_id,
                        asset_id=asset_id,
                        evidence_kind="gallery",
                        ordinal=0,
                        source_url=f"{canonical_url}/image.webp",
                    )
                )
                connection.execute(
                    knowledge_product_asset_evidence.insert().values(
                        project_id=project_id,
                        product_id=product_id,
                        source_id=source_id,
                        snapshot_id=snapshot_id,
                        asset_id=asset_id,
                        role="primary",
                        confidence=0.95,
                        reason="Official product gallery primary image",
                    )
                )

    def test_server_task_reads_are_authorized_and_project_scoped(
        self,
    ) -> None:
        import app as app_module

        codec = ServerActorSessionCodec(b"z" * 32)
        actor = ActorIdentity(self.org_a, self.user_a)
        base_config = app_module.config()
        with tempfile.TemporaryDirectory() as directory:
            local_state = Path(directory) / "must-not-exist"
            isolated = replace(
                base_config,
                data_file=local_state / "tasks.json",
                knowledge_agent_enabled=False,
            )
            with (
                patch.object(app_module, "config", return_value=isolated),
                patch.dict(
                    os.environ,
                    {
                        "ARTICLE_AGENT_SERVER_MODE": "true",
                        "ARTICLE_AGENT_SERVER_SESSION_SECRET": "z" * 32,
                    },
                    clear=False,
                ),
                TestClient(app_module.app) as client,
            ):
                audit = self._install_recording_audit(
                    client.app,
                    isolated,
                )
                self.assertEqual(
                    client.get("/api/projects").status_code,
                    401,
                )
                self.assertEqual(
                    client.get(
                        f"/api/projects/{self.project_a}/tasks"
                    ).status_code,
                    401,
                )
                client.cookies.set(
                    SERVER_AUTH_COOKIE_NAME,
                    codec.create(actor),
                )
                download_store = FakeDownloadStore()
                app_module.app.state.server_project_object_service = (
                    ProjectKnowledgeObjectService(
                        store=download_store,  # type: ignore[arg-type]
                        bucket="private-bucket",
                        repository=PostgresKnowledgeAssetRepository(
                            self.engine
                        ),
                        access=ProjectAccessService(
                            PostgresProjectAccessRepository(self.engine)
                        ),
                    )
                )
                directory = client.get("/api/projects")
                self.assertEqual(directory.status_code, 200)
                self.assertEqual(
                    directory.json(),
                    [
                        {
                            "project_id": self.project_a,
                            "customer_name": "Project A",
                            "official_domain": self.project_a,
                            "effective_role": "viewer",
                        }
                    ],
                )
                response = client.get(
                    f"/api/projects/{self.project_a}/tasks"
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    [item["id"] for item in response.json()],
                    [self.task_a],
                )
                self.assertEqual(
                    client.get(
                        f"/api/projects/{self.project_a}/tasks/{self.task_a}"
                    ).json()["id"],
                    self.task_a,
                )
                self.assertEqual(
                    client.get(
                        f"/api/projects/{self.project_a}/tasks/{self.task_b}"
                    ).status_code,
                    404,
                )
                self.assertEqual(
                    client.get(
                        f"/api/projects/{self.project_b}/tasks"
                    ).status_code,
                    403,
                )
                self.assertEqual(
                    client.get("/api/tasks").status_code,
                    503,
                )
                download = client.get(
                    f"/api/projects/{self.project_a}/assets/"
                    f"{self.asset_a}/download",
                    params={"expires_seconds": 120},
                )
                self.assertEqual(download.status_code, 200)
                self.assertEqual(
                    download.json()["asset_id"],
                    self.asset_a,
                )
                self.assertEqual(
                    download_store.signed,
                    [
                        (
                            build_project_object_key(
                                self.org_a,
                                self.project_a,
                                hashlib.sha256(b"asset-a").hexdigest(),
                            ),
                            120,
                        )
                    ],
                )
                self.assertEqual(
                    client.get(
                        f"/api/projects/{self.project_a}/assets/"
                        "missing/download"
                    ).status_code,
                    404,
                )
                self.assertEqual(
                    client.get(
                        f"/api/projects/{self.project_a}/assets/"
                        f"{self.bad_asset}/download"
                    ).status_code,
                    404,
                )
                self.assertEqual(
                    client.get(
                        f"/api/projects/{self.project_b}/assets/"
                        f"{self.asset_a}/download"
                    ).status_code,
                    403,
                )
                rewrite_path = (
                    f"/api/projects/{self.project_a}/tasks/"
                    f"{self.task_a}/rewrite-from-scratch"
                )
                self.assertEqual(
                    client.post(
                        rewrite_path,
                        json={"revision": 0},
                    ).status_code,
                    403,
                )
                with self.engine.begin() as connection:
                    connection.execute(
                        project_memberships.update()
                        .where(
                            project_memberships.c.organization_id
                            == self.org_a,
                            project_memberships.c.project_id
                            == self.project_a,
                            project_memberships.c.user_id == self.user_a,
                        )
                        .values(role="editor")
                    )
                rewritten = client.post(
                    rewrite_path,
                    json={"revision": 0},
                )
                self.assertEqual(rewritten.status_code, 200)
                self.assertEqual(rewritten.json()["revision"], 1)
                self.assertEqual(
                    [event.action for event in audit.events],
                    ["article.task.rewritten"],
                )
                self.assertEqual(
                    client.post(
                        rewrite_path,
                        json={"revision": 0},
                    ).status_code,
                    409,
                )
                self.assertFalse(local_state.exists())

                with self.engine.begin() as connection:
                    connection.execute(
                        projects.update()
                        .where(projects.c.project_id == self.project_a)
                        .values(status="archived")
                    )
                self.assertEqual(
                    client.get("/api/projects").json(),
                    [],
                )
                self.assertEqual(
                    client.get(
                        f"/api/projects/{self.project_a}/tasks"
                    ).status_code,
                    403,
                )

                with self.engine.begin() as connection:
                    connection.execute(
                        projects.update()
                        .where(projects.c.project_id == self.project_a)
                        .values(status="active")
                    )
                    connection.execute(
                        workspace_users.update()
                        .where(
                            workspace_users.c.organization_id
                            == self.org_a,
                            workspace_users.c.user_id == self.user_a,
                        )
                        .values(status="disabled")
                    )
                self.assertEqual(
                    client.get("/api/projects").status_code,
                    401,
                )

    def test_server_selects_only_current_title_candidate_with_cas(
        self,
    ) -> None:
        import app as app_module

        repository = self._task_repository(
            organization_id=self.org_a,
            project_id=self.project_a,
        )
        record = repository.get(self.task_a)
        assert record is not None
        record.update(
            {
                "revision": 0,
                "status": "titles_ready",
                "title_candidates": [
                    "First candidate",
                    "Second candidate",
                ],
                "selected_title": "",
                "outline": "stale outline",
                "outline_draft": "stale outline draft",
                "article": "stale article",
            }
        )
        repository.upsert(record)
        codec = ServerActorSessionCodec(b"q" * 32)
        actor = ActorIdentity(self.org_a, self.user_a)
        base_config = app_module.config()
        with tempfile.TemporaryDirectory() as directory:
            local_state = Path(directory) / "must-not-exist"
            isolated = replace(
                base_config,
                data_file=local_state / "tasks.json",
                knowledge_agent_enabled=False,
            )
            with (
                patch.object(app_module, "config", return_value=isolated),
                patch.dict(
                    os.environ,
                    {
                        "ARTICLE_AGENT_SERVER_MODE": "true",
                        "ARTICLE_AGENT_SERVER_SESSION_SECRET": "q" * 32,
                        "ARTICLE_AGENT_OBJECT_STORE_BUCKET": "",
                    },
                    clear=False,
                ),
                TestClient(app_module.app) as client,
            ):
                audit = self._install_recording_audit(
                    client.app,
                    isolated,
                )
                client.cookies.set(
                    SERVER_AUTH_COOKIE_NAME,
                    codec.create(actor),
                )
                path = (
                    f"/api/projects/{self.project_a}/tasks/"
                    f"{self.task_a}/selected-title"
                )
                self.assertEqual(
                    client.put(
                        path,
                        json={"revision": 0, "candidate_index": 1},
                    ).status_code,
                    403,
                )
                with self.engine.begin() as connection:
                    connection.execute(
                        project_memberships.update()
                        .where(
                            project_memberships.c.organization_id
                            == self.org_a,
                            project_memberships.c.project_id
                            == self.project_a,
                            project_memberships.c.user_id == self.user_a,
                        )
                        .values(role="editor")
                    )
                self.assertEqual(
                    client.put(
                        path,
                        json={
                            "revision": 0,
                            "candidate_index": 1,
                            "title": "Caller replacement",
                        },
                    ).status_code,
                    422,
                )
                self.assertEqual(
                    client.put(
                        path,
                        json={"revision": 0, "candidate_index": 9},
                    ).status_code,
                    422,
                )
                self.assertEqual(
                    client.put(
                        (
                            f"/api/projects/{self.project_b}/tasks/"
                            f"{self.task_a}/selected-title"
                        ),
                        json={"revision": 0, "candidate_index": 1},
                    ).status_code,
                    403,
                )
                selected = client.put(
                    path,
                    json={"revision": 0, "candidate_index": 1},
                )
                self.assertEqual(selected.status_code, 200, selected.text)
                response = selected.json()
                self.assertEqual(
                    response["selected_title"],
                    "Second candidate",
                )
                self.assertEqual(response["status"], "title_selected")
                self.assertEqual(response["revision"], 1)
                self.assertEqual(response["outline"], "")
                self.assertEqual(response["outline_draft"], "")
                self.assertEqual(response["article"], "")
                self.assertEqual(
                    [event.action for event in audit.events],
                    ["article.title.selected"],
                )
                self.assertEqual(
                    audit.events[0].details,
                    {
                        "from_revision": 0,
                        "to_revision": 1,
                        "status": "title_selected",
                        "candidate_count": 2,
                        "candidate_index": 1,
                    },
                )
                self.assertNotIn(
                    "Second candidate",
                    str(audit.events[0].details),
                )
                stale = client.put(
                    path,
                    json={"revision": 0, "candidate_index": 0},
                )
                self.assertEqual(stale.status_code, 409)
                self.assertEqual(len(audit.events), 1)
                stored = repository.get(self.task_a)
                assert stored is not None
                self.assertEqual(
                    stored["selected_title"],
                    "Second candidate",
                )
                self.assertEqual(stored["revision"], 1)
                self.assertFalse(local_state.exists())

    def test_server_saves_outline_draft_and_confirmation_with_cas(
        self,
    ) -> None:
        import app as app_module

        repository = self._task_repository(
            organization_id=self.org_a,
            project_id=self.project_a,
        )
        record = repository.get(self.task_a)
        assert record is not None
        record.update(
            {
                "revision": 0,
                "status": "outline_confirmed",
                "selected_title": "Current title",
                "outline": "## Confirmed outline",
                "outline_draft": "## Confirmed outline",
                "article": "stale article",
                "initial_article": "stale initial article",
                "article_versions": [],
            }
        )
        repository.upsert(record)
        codec = ServerActorSessionCodec(b"r" * 32)
        actor = ActorIdentity(self.org_a, self.user_a)
        base_config = app_module.config()
        with tempfile.TemporaryDirectory() as directory:
            local_state = Path(directory) / "must-not-exist"
            isolated = replace(
                base_config,
                data_file=local_state / "tasks.json",
                knowledge_agent_enabled=False,
            )
            with (
                patch.object(app_module, "config", return_value=isolated),
                patch.dict(
                    os.environ,
                    {
                        "ARTICLE_AGENT_SERVER_MODE": "true",
                        "ARTICLE_AGENT_SERVER_SESSION_SECRET": "r" * 32,
                        "ARTICLE_AGENT_OBJECT_STORE_BUCKET": "",
                    },
                    clear=False,
                ),
                TestClient(app_module.app) as client,
            ):
                audit = self._install_recording_audit(
                    client.app,
                    isolated,
                )
                client.cookies.set(
                    SERVER_AUTH_COOKIE_NAME,
                    codec.create(actor),
                )
                path = (
                    f"/api/projects/{self.project_a}/tasks/"
                    f"{self.task_a}/outline"
                )
                self.assertEqual(
                    client.put(
                        path,
                        json={
                            "revision": 0,
                            "outline": "## Working draft",
                            "confirmed": False,
                        },
                    ).status_code,
                    403,
                )
                with self.engine.begin() as connection:
                    connection.execute(
                        project_memberships.update()
                        .where(
                            project_memberships.c.organization_id
                            == self.org_a,
                            project_memberships.c.project_id
                            == self.project_a,
                            project_memberships.c.user_id == self.user_a,
                        )
                        .values(role="editor")
                    )
                self.assertEqual(
                    client.put(
                        path,
                        json={
                            "revision": 0,
                            "outline": "   ",
                            "confirmed": False,
                        },
                    ).status_code,
                    422,
                )
                self.assertEqual(
                    client.put(
                        path,
                        json={
                            "revision": 0,
                            "outline": "## Working draft",
                            "confirmed": False,
                            "status": "caller-controlled",
                        },
                    ).status_code,
                    422,
                )
                self.assertEqual(
                    client.put(
                        (
                            f"/api/projects/{self.project_b}/tasks/"
                            f"{self.task_a}/outline"
                        ),
                        json={
                            "revision": 0,
                            "outline": "## Cross-project draft",
                            "confirmed": False,
                        },
                    ).status_code,
                    403,
                )
                draft = client.put(
                    path,
                    json={
                        "revision": 0,
                        "outline": "## Working draft",
                        "confirmed": False,
                    },
                )
                self.assertEqual(draft.status_code, 200, draft.text)
                draft_body = draft.json()
                self.assertEqual(draft_body["revision"], 1)
                self.assertEqual(
                    draft_body["outline"],
                    "## Confirmed outline",
                )
                self.assertEqual(
                    draft_body["outline_draft"],
                    "## Working draft",
                )
                self.assertEqual(
                    draft_body["article"],
                    "stale article",
                )
                self.assertEqual(
                    draft_body["article_versions"][-1]["kind"],
                    "outline_draft",
                )
                confirmed = client.put(
                    path,
                    json={
                        "revision": 1,
                        "outline": "## Final reviewed outline",
                        "confirmed": True,
                    },
                )
                self.assertEqual(
                    confirmed.status_code,
                    200,
                    confirmed.text,
                )
                confirmed_body = confirmed.json()
                self.assertEqual(confirmed_body["revision"], 2)
                self.assertEqual(
                    confirmed_body["outline"],
                    "## Final reviewed outline",
                )
                self.assertEqual(
                    confirmed_body["outline_draft"],
                    "## Final reviewed outline",
                )
                self.assertEqual(
                    confirmed_body["status"],
                    "outline_confirmed",
                )
                self.assertEqual(confirmed_body["article"], "")
                self.assertEqual(
                    confirmed_body["initial_article"],
                    "",
                )
                self.assertEqual(
                    confirmed_body["article_versions"][-1]["kind"],
                    "outline",
                )
                self.assertEqual(
                    [event.action for event in audit.events],
                    [
                        "article.outline.updated",
                        "article.outline.updated",
                    ],
                )
                self.assertEqual(
                    audit.events[0].details,
                    {
                        "from_revision": 0,
                        "to_revision": 1,
                        "status": "outline_confirmed",
                        "confirmed": False,
                        "outline_characters": 16,
                    },
                )
                self.assertEqual(
                    audit.events[1].details,
                    {
                        "from_revision": 1,
                        "to_revision": 2,
                        "status": "outline_confirmed",
                        "confirmed": True,
                        "outline_characters": 25,
                    },
                )
                self.assertNotIn(
                    "Working draft",
                    str(audit.events),
                )
                self.assertNotIn(
                    "Final reviewed outline",
                    str(audit.events),
                )
                stale = client.put(
                    path,
                    json={
                        "revision": 1,
                        "outline": "## Stale outline",
                        "confirmed": True,
                    },
                )
                self.assertEqual(stale.status_code, 409)
                self.assertEqual(len(audit.events), 2)
                stored = repository.get(self.task_a)
                assert stored is not None
                self.assertEqual(stored["revision"], 2)
                self.assertEqual(
                    stored["outline"],
                    "## Final reviewed outline",
                )
                self.assertFalse(local_state.exists())

    def test_server_restores_only_owned_outline_version_to_draft(
        self,
    ) -> None:
        import app as app_module

        repository = self._task_repository(
            organization_id=self.org_a,
            project_id=self.project_a,
        )
        record = repository.get(self.task_a)
        assert record is not None
        record.update(
            {
                "revision": 0,
                "status": "outline_confirmed",
                "selected_title": "Current title",
                "outline": "## Current confirmed outline",
                "outline_draft": "## Current confirmed outline",
                "article": "current downstream article",
                "article_versions": [
                    {
                        "kind": "outline",
                        "content": "## Earlier outline",
                        "source_kind": "manual_confirmed",
                    },
                    {
                        "kind": "article",
                        "content": "private article version",
                        "source_kind": "first_version",
                    },
                ],
            }
        )
        repository.upsert(record)
        codec = ServerActorSessionCodec(b"s" * 32)
        actor = ActorIdentity(self.org_a, self.user_a)
        base_config = app_module.config()
        with tempfile.TemporaryDirectory() as directory:
            local_state = Path(directory) / "must-not-exist"
            isolated = replace(
                base_config,
                data_file=local_state / "tasks.json",
                knowledge_agent_enabled=False,
            )
            with (
                patch.object(app_module, "config", return_value=isolated),
                patch.dict(
                    os.environ,
                    {
                        "ARTICLE_AGENT_SERVER_MODE": "true",
                        "ARTICLE_AGENT_SERVER_SESSION_SECRET": "s" * 32,
                        "ARTICLE_AGENT_OBJECT_STORE_BUCKET": "",
                    },
                    clear=False,
                ),
                TestClient(app_module.app) as client,
            ):
                audit = self._install_recording_audit(
                    client.app,
                    isolated,
                )
                client.cookies.set(
                    SERVER_AUTH_COOKIE_NAME,
                    codec.create(actor),
                )
                path = (
                    f"/api/projects/{self.project_a}/tasks/"
                    f"{self.task_a}/outline/restore-version"
                )
                self.assertEqual(
                    client.post(
                        path,
                        json={"revision": 0, "version_index": 0},
                    ).status_code,
                    403,
                )
                with self.engine.begin() as connection:
                    connection.execute(
                        project_memberships.update()
                        .where(
                            project_memberships.c.organization_id
                            == self.org_a,
                            project_memberships.c.project_id
                            == self.project_a,
                            project_memberships.c.user_id == self.user_a,
                        )
                        .values(role="editor")
                    )
                self.assertEqual(
                    client.post(
                        path,
                        json={
                            "revision": 0,
                            "version_index": 0,
                            "outline": "caller replacement",
                        },
                    ).status_code,
                    422,
                )
                self.assertEqual(
                    client.post(
                        path,
                        json={"revision": 0, "version_index": 99},
                    ).status_code,
                    404,
                )
                self.assertEqual(
                    client.post(
                        path,
                        json={"revision": 0, "version_index": 1},
                    ).status_code,
                    422,
                )
                self.assertEqual(
                    client.post(
                        (
                            f"/api/projects/{self.project_b}/tasks/"
                            f"{self.task_a}/outline/restore-version"
                        ),
                        json={"revision": 0, "version_index": 0},
                    ).status_code,
                    403,
                )
                restored = client.post(
                    path,
                    json={"revision": 0, "version_index": 0},
                )
                self.assertEqual(
                    restored.status_code,
                    200,
                    restored.text,
                )
                body = restored.json()
                self.assertEqual(body["revision"], 1)
                self.assertEqual(
                    body["outline"],
                    "## Current confirmed outline",
                )
                self.assertEqual(
                    body["outline_draft"],
                    "## Earlier outline",
                )
                self.assertEqual(
                    body["article"],
                    "current downstream article",
                )
                self.assertEqual(
                    body["status"],
                    "outline_confirmed",
                )
                self.assertEqual(
                    body["article_versions"][-1]["kind"],
                    "outline_draft",
                )
                self.assertEqual(
                    body["article_versions"][-1]["source_kind"],
                    "restored",
                )
                self.assertEqual(
                    [event.action for event in audit.events],
                    ["article.outline_version.restored"],
                )
                self.assertEqual(
                    audit.events[0].details,
                    {
                        "from_revision": 0,
                        "to_revision": 1,
                        "status": "outline_confirmed",
                        "restored_from": "outline",
                        "version_index": 0,
                    },
                )
                self.assertNotIn(
                    "Earlier outline",
                    str(audit.events),
                )
                stale = client.post(
                    path,
                    json={"revision": 0, "version_index": 0},
                )
                self.assertEqual(stale.status_code, 409)
                self.assertEqual(len(audit.events), 1)
                stored = repository.get(self.task_a)
                assert stored is not None
                self.assertEqual(stored["revision"], 1)
                self.assertEqual(
                    stored["outline_draft"],
                    "## Earlier outline",
                )
                self.assertFalse(local_state.exists())

    def test_server_task_api_is_not_added_to_local_mode(self) -> None:
        import app as app_module

        previous_mode = getattr(
            app_module.app.state,
            "server_mode_enabled",
            None,
        )
        app_module.app.state.server_mode_enabled = False
        try:
            response = TestClient(app_module.app).get(
                f"/api/projects/{self.project_a}/tasks"
            )
            self.assertEqual(response.status_code, 404)
            self.assertEqual(
                TestClient(app_module.app).get(
                    "/api/projects"
                ).status_code,
                404,
            )
            self.assertEqual(
                TestClient(app_module.app).get(
                    f"/api/projects/{self.project_a}/assets/"
                    f"{self.asset_a}/download"
                ).status_code,
                404,
            )
            self.assertEqual(
                TestClient(app_module.app).post(
                    f"/api/projects/{self.project_a}/tasks/"
                    f"{self.task_a}/rewrite-from-scratch",
                    json={"revision": 0},
                ).status_code,
                404,
            )
            self.assertEqual(
                TestClient(app_module.app).put(
                    f"/api/projects/{self.project_a}/tasks/"
                    f"{self.task_a}/selected-title",
                    json={"revision": 0, "candidate_index": 0},
                ).status_code,
                404,
            )
            self.assertEqual(
                TestClient(app_module.app).post(
                    f"/api/projects/{self.project_a}/tasks/"
                    f"{self.task_a}/outline/restore-version",
                    json={"revision": 0, "version_index": 0},
                ).status_code,
                404,
            )
            self.assertEqual(
                TestClient(app_module.app).put(
                    f"/api/projects/{self.project_a}/tasks/"
                    f"{self.task_a}/outline",
                    json={
                        "revision": 0,
                        "outline": "## Reviewed outline",
                        "confirmed": True,
                    },
                ).status_code,
                404,
            )
            self.assertEqual(
                TestClient(app_module.app).post(
                    f"/api/projects/{self.project_a}/tasks/"
                    f"{self.task_a}/outline",
                    json={"revision": 0},
                ).status_code,
                404,
            )
            self.assertEqual(
                TestClient(app_module.app).get(
                    f"/api/projects/{self.project_a}/tasks/"
                    f"{self.task_a}/outline/jobs/job-a",
                ).status_code,
                404,
            )
            self.assertEqual(
                TestClient(app_module.app).post(
                    f"/api/projects/{self.project_a}/tasks/"
                    f"{self.task_a}/titles",
                    json={"revision": 0},
                ).status_code,
                404,
            )
            self.assertEqual(
                TestClient(app_module.app).get(
                    f"/api/projects/{self.project_a}/tasks/"
                    f"{self.task_a}/titles/jobs/job-a",
                ).status_code,
                404,
            )
            self.assertEqual(
                TestClient(app_module.app).post(
                    f"/api/projects/{self.project_a}/tasks/"
                    f"{self.task_a}/article",
                    json={"revision": 0},
                ).status_code,
                404,
            )
            self.assertEqual(
                TestClient(app_module.app).get(
                    f"/api/projects/{self.project_a}/tasks/"
                    f"{self.task_a}/article/jobs/job-a",
                ).status_code,
                404,
            )
            self.assertEqual(
                TestClient(app_module.app).put(
                    f"/api/projects/{self.project_a}/tasks/"
                    f"{self.task_a}/products",
                    json={
                        "revision": 0,
                        "product_ids": [self.product_a],
                    },
                ).status_code,
                404,
            )
            self.assertEqual(
                TestClient(app_module.app).put(
                    f"/api/projects/{self.project_a}/tasks/"
                    f"{self.task_a}/article/sections",
                    json={
                        "revision": 0,
                        "heading_path": ["Buyer Checks"],
                        "replacement_body": "Replacement.",
                    },
                ).status_code,
                404,
            )
            self.assertEqual(
                TestClient(app_module.app).post(
                    f"/api/projects/{self.project_a}/tasks/"
                    f"{self.task_a}/product-rediscovery",
                    json={
                        "revision": 0,
                        "category_url": (
                            f"https://{self.project_a}/products"
                        ),
                    },
                ).status_code,
                404,
            )
            self.assertEqual(
                TestClient(app_module.app).post(
                    f"/api/projects/{self.project_a}/tasks/"
                    f"{self.task_a}/export-docx",
                    json={"revision": 0},
                ).status_code,
                404,
            )
            self.assertEqual(
                TestClient(app_module.app).get(
                    f"/api/projects/{self.project_a}/tasks/"
                    f"{self.task_a}/docx/download",
                ).status_code,
                404,
            )
        finally:
            app_module.app.state.server_mode_enabled = previous_mode

    def test_server_replaces_products_only_from_confirmed_project_catalog(
        self,
    ) -> None:
        import app as app_module

        self._store_selectable_product(
            project_id=self.project_a,
            product_id=self.product_a,
            asset_id=self.asset_a,
        )
        self._store_selectable_product(
            project_id=self.project_a,
            product_id=self.inbox_product,
            status="inbox",
        )
        self._store_selectable_product(
            project_id=self.project_b,
            product_id=self.product_b,
        )
        self._store_selectable_product(
            project_id=self.project_a,
            product_id=self.unpublished_product,
            source_status="needs_review",
        )
        with self.engine.begin() as connection:
            connection.execute(
                knowledge_products.update()
                .where(
                    knowledge_products.c.project_id == self.project_a,
                    knowledge_products.c.product_id == self.product_a,
                )
                .values(
                    name="Unreviewed replacement name",
                    canonical_url="https://unreviewed.invalid/product",
                    metadata={
                        "description": "Unreviewed replacement description.",
                        "main_content_facts": ["Unreviewed fact."],
                    },
                )
            )
        codec = ServerActorSessionCodec(b"p" * 32)
        actor = ActorIdentity(self.org_a, self.user_a)
        base_config = app_module.config()
        with tempfile.TemporaryDirectory() as directory:
            local_state = Path(directory) / "must-not-exist"
            isolated = replace(
                base_config,
                data_file=local_state / "tasks.json",
                knowledge_agent_enabled=False,
            )
            with (
                patch.object(app_module, "config", return_value=isolated),
                patch.dict(
                    os.environ,
                    {
                        "ARTICLE_AGENT_SERVER_MODE": "true",
                        "ARTICLE_AGENT_SERVER_SESSION_SECRET": "p" * 32,
                    },
                    clear=False,
                ),
                TestClient(app_module.app) as client,
            ):
                audit = self._install_recording_audit(
                    client.app,
                    isolated,
                )
                client.cookies.set(
                    SERVER_AUTH_COOKIE_NAME,
                    codec.create(actor),
                )
                path = (
                    f"/api/projects/{self.project_a}/tasks/"
                    f"{self.task_a}/products"
                )
                self.assertEqual(
                    client.put(
                        path,
                        json={
                            "revision": 0,
                            "product_ids": [self.product_a],
                        },
                    ).status_code,
                    403,
                )
                with self.engine.begin() as connection:
                    connection.execute(
                        project_memberships.update()
                        .where(
                            project_memberships.c.organization_id
                            == self.org_a,
                            project_memberships.c.project_id
                            == self.project_a,
                            project_memberships.c.user_id == self.user_a,
                        )
                        .values(role="editor")
                    )
                self.assertEqual(
                    client.put(
                        path,
                        json={
                            "revision": 0,
                            "product_ids": [self.product_b],
                        },
                    ).status_code,
                    409,
                )
                self.assertEqual(
                    client.put(
                        path,
                        json={
                            "revision": 0,
                            "product_ids": [self.unpublished_product],
                        },
                    ).status_code,
                    409,
                )
                self.assertEqual(
                    client.put(
                        path,
                        json={
                            "revision": 0,
                            "product_ids": [self.inbox_product],
                        },
                    ).status_code,
                    409,
                )
                self.assertEqual(
                    client.put(
                        path,
                        json={
                            "revision": 0,
                            "product_ids": [
                                self.product_a,
                                self.product_a,
                            ],
                        },
                    ).status_code,
                    422,
                )
                self.assertEqual(
                    client.put(
                        path,
                        json={
                            "revision": 0,
                            "product_ids": [self.product_a],
                            "products": [
                                {
                                    "product_id": self.product_a,
                                    "canonical_url": "https://attacker.invalid",
                                }
                            ],
                        },
                    ).status_code,
                    422,
                )

                response = client.put(
                    path,
                    json={
                        "revision": 0,
                        "product_ids": [self.product_a],
                    },
                )
                self.assertEqual(response.status_code, 200, response.text)
                saved = response.json()
                self.assertEqual(saved["revision"], 1)
                self.assertEqual(
                    [event.action for event in audit.events],
                    ["article.products.confirmed"],
                )
                self.assertEqual(
                    [item["product_id"] for item in saved["products"]],
                    [self.product_a],
                )
                product = saved["products"][0]
                self.assertEqual(
                    product["name"],
                    f"Product {self.product_a}",
                )
                self.assertEqual(
                    product["canonical_url"],
                    f"https://{self.project_a}/products/{self.product_a}",
                )
                self.assertEqual(
                    product["description"],
                    "Published product description.",
                )
                self.assertEqual(product["selected_asset_id"], self.asset_a)
                self.assertEqual(product["image_path"], "")
                self.assertEqual(product["asset_count"], 1)
                self.assertEqual(
                    product["reference_facts"],
                    ["Fact one.", "Fact two."],
                )
                self.assertEqual(
                    product["specifications"],
                    {
                        "Material": "316 stainless steel",
                        "Length": "50 mm",
                    },
                )
                self.assertNotIn("artifact_uri", product)
                self.assertNotIn("source_url", product)
                self.assertEqual(
                    client.put(
                        path,
                        json={
                            "revision": 0,
                            "product_ids": [self.product_a],
                        },
                    ).status_code,
                    409,
                )
                self.assertFalse(local_state.exists())

    def test_server_prepares_private_asset_ids_without_local_paths(
        self,
    ) -> None:
        import app as app_module

        repository = self._task_repository(
            organization_id=self.org_a,
            project_id=self.project_a,
        )
        record = repository.get(self.task_a)
        assert record is not None
        article = SERVER_ARTICLE.replace(
            "Keep the original application guidance.",
            (
                f"Product {self.product_a} supports the application "
                "described in this section."
            ),
        )
        hero_asset_id = f"{self.asset_a}-hero-source"
        product_asset_id = f"{self.asset_a}-product-source"
        record.update(
            {
                "status": "links_verified",
                "selected_title": "Example Buyer Guide",
                "linked_article": article,
                "article": article,
                "products": [
                    {
                        "product_id": self.product_a,
                        "name": f"Product {self.product_a}",
                        "url": (
                            f"https://{self.project_a}/products/"
                            f"{self.product_a}"
                        ),
                        "canonical_url": (
                            f"https://{self.project_a}/products/"
                            f"{self.product_a}"
                        ),
                        "selected_asset_id": product_asset_id,
                        "asset_status": "ready",
                    }
                ],
                "article_versions": [],
            }
        )
        repository.upsert(record)

        codec = ServerActorSessionCodec(b"i" * 32)
        actor = ActorIdentity(self.org_a, self.user_a)
        base_config = app_module.config()
        private_store = FakeDownloadStore()
        access = ProjectAccessService(
            PostgresProjectAccessRepository(self.engine)
        )
        object_service = ProjectKnowledgeObjectService(
            store=private_store,
            bucket="private-bucket",
            repository=PostgresKnowledgeAssetRepository(self.engine),
            access=access,
        )
        with tempfile.TemporaryDirectory() as directory:
            local_state = Path(directory) / "must-not-exist"
            isolated = replace(
                base_config,
                data_file=local_state / "tasks.json",
                knowledge_agent_enabled=False,
            )
            with (
                patch.object(app_module, "config", return_value=isolated),
                patch.dict(
                    os.environ,
                    {
                        "ARTICLE_AGENT_SERVER_MODE": "true",
                        "ARTICLE_AGENT_SERVER_SESSION_SECRET": "i" * 32,
                        "ARTICLE_AGENT_OBJECT_STORE_BUCKET": "",
                    },
                    clear=False,
                ),
                TestClient(app_module.app) as client,
            ):
                audit = self._install_recording_audit(
                    client.app,
                    isolated,
                )
                client.cookies.set(
                    SERVER_AUTH_COOKIE_NAME,
                    codec.create(actor),
                )
                path = (
                    f"/api/projects/{self.project_a}/tasks/"
                    f"{self.task_a}/prepare-images"
                )
                payload = {
                    "revision": 0,
                    "hero_asset_id": hero_asset_id,
                }
                self.assertEqual(
                    client.post(path, json=payload).status_code,
                    403,
                )
                with self.engine.begin() as connection:
                    connection.execute(
                        project_memberships.update()
                        .where(
                            project_memberships.c.organization_id
                            == self.org_a,
                            project_memberships.c.project_id
                            == self.project_a,
                            project_memberships.c.user_id == self.user_a,
                        )
                        .values(role="editor")
                    )
                object_service.upload(
                    actor=actor,
                    project_id=self.project_a,
                    asset_id=hero_asset_id,
                    data=self._image_bytes("navy"),
                    content_type="image/png",
                    width=320,
                    height=240,
                )
                object_service.upload(
                    actor=actor,
                    project_id=self.project_a,
                    asset_id=product_asset_id,
                    data=self._image_bytes("orange"),
                    content_type="image/png",
                    width=320,
                    height=240,
                )
                client.app.state.server_project_object_service = (
                    object_service
                )

                prepared = client.post(path, json=payload)

                self.assertEqual(
                    prepared.status_code,
                    200,
                    prepared.text,
                )
                saved = prepared.json()
                self.assertEqual(saved["revision"], 1)
                self.assertEqual(saved["status"], "images_ready")
                self.assertEqual(
                    [event.action for event in audit.events],
                    ["article.images.prepared"],
                )
                self.assertEqual(len(saved["images"]), 2)
                self.assertEqual(
                    [
                        item["source_asset_id"]
                        for item in saved["images"]
                    ],
                    [hero_asset_id, product_asset_id],
                )
                for image in saved["images"]:
                    self.assertEqual(image["source_path"], "")
                    self.assertEqual(image["prepared_path"], "")
                    self.assertTrue(image["prepared_asset_id"])
                    self.assertNotIn("artifact_uri", image)
                    self.assertNotIn("source_url", image)
                self.assertEqual(
                    saved["images"][0]["anchor_after"],
                    "before_first_h2",
                )
                self.assertEqual(
                    saved["images"][1]["anchor_heading"],
                    "Confirm the application",
                )
                put_count = len(private_store.put_calls)
                self.assertEqual(
                    client.post(path, json=payload).status_code,
                    409,
                )
                self.assertEqual(
                    len(private_store.put_calls),
                    put_count,
                )

                derived_asset_id = saved["images"][1][
                    "prepared_asset_id"
                ]
                downloaded = client.get(
                    (
                        f"/api/projects/{self.project_a}/assets/"
                        f"{derived_asset_id}/download"
                    )
                )
                self.assertEqual(
                    downloaded.status_code,
                    200,
                    downloaded.text,
                )
                self.assertEqual(
                    downloaded.json()["asset_id"],
                    derived_asset_id,
                )

                export_path = (
                    f"/api/projects/{self.project_a}/tasks/"
                    f"{self.task_a}/export-docx"
                )
                with self.engine.begin() as connection:
                    connection.execute(
                        project_memberships.update()
                        .where(
                            project_memberships.c.organization_id
                            == self.org_a,
                            project_memberships.c.project_id
                            == self.project_a,
                            project_memberships.c.user_id == self.user_a,
                        )
                        .values(role="viewer")
                    )
                self.assertEqual(
                    client.post(
                        export_path,
                        json={"revision": 1},
                    ).status_code,
                    403,
                )
                with self.engine.begin() as connection:
                    connection.execute(
                        project_memberships.update()
                        .where(
                            project_memberships.c.organization_id
                            == self.org_a,
                            project_memberships.c.project_id
                            == self.project_a,
                            project_memberships.c.user_id == self.user_a,
                        )
                        .values(role="editor")
                    )
                put_count = len(private_store.put_calls)
                exported = client.post(
                    export_path,
                    json={"revision": 1},
                )
                self.assertEqual(
                    exported.status_code,
                    200,
                    exported.text,
                )
                delivered = exported.json()
                self.assertEqual(delivered["revision"], 2)
                self.assertEqual(
                    delivered["status"],
                    "docx_exported",
                )
                self.assertEqual(
                    [event.action for event in audit.events],
                    [
                        "article.images.prepared",
                        "article.docx.exported",
                    ],
                )
                self.assertEqual(delivered["docx_path"], "")
                self.assertTrue(delivered["docx_asset_id"])
                self.assertEqual(
                    len(delivered["docx_content_hash"]),
                    64,
                )
                self.assertTrue(
                    delivered["docx_filename"].endswith(".docx")
                )
                self.assertEqual(
                    len(private_store.put_calls),
                    put_count + 1,
                )
                docx_asset = (
                    PostgresKnowledgeAssetRepository(
                        self.engine
                    ).get_asset(
                        self.project_a,
                        delivered["docx_asset_id"],
                    )
                )
                assert docx_asset is not None
                docx_key = str(docx_asset.metadata["object_key"])
                with ZipFile(
                    BytesIO(private_store.objects[docx_key])
                ) as archive:
                    self.assertIn(
                        "word/document.xml",
                        archive.namelist(),
                    )
                    self.assertEqual(
                        len(
                            [
                                name
                                for name in archive.namelist()
                                if name.startswith("word/media/")
                            ]
                        ),
                        2,
                    )
                self.assertEqual(
                    client.get(
                        (
                            f"/api/projects/{self.project_a}/assets/"
                            f"{delivered['docx_asset_id']}/download"
                        )
                    ).status_code,
                    404,
                )
                docx_download_path = (
                    f"/api/projects/{self.project_a}/tasks/"
                    f"{self.task_a}/docx/download"
                )
                docx_download = client.get(docx_download_path)
                self.assertEqual(
                    docx_download.status_code,
                    200,
                    docx_download.text,
                )
                self.assertEqual(
                    docx_download.json()["asset_id"],
                    delivered["docx_asset_id"],
                )
                tdk_path = (
                    f"/api/projects/{self.project_a}/tasks/"
                    f"{self.task_a}/generate-tdk"
                )
                with patch(
                    "services.server_tdk_export.LLMClient",
                    StubServerTdkLlm,
                ):
                    tdk_response = client.post(
                        tdk_path,
                        json={"revision": 2},
                    )
                self.assertEqual(
                    tdk_response.status_code,
                    200,
                    tdk_response.text,
                )
                tdk_delivered = tdk_response.json()
                self.assertEqual(tdk_delivered["revision"], 3)
                self.assertEqual(tdk_delivered["tdk_path"], "")
                self.assertTrue(tdk_delivered["tdk_asset_id"])
                self.assertEqual(
                    len(tdk_delivered["tdk_content_hash"]),
                    64,
                )
                self.assertEqual(
                    tdk_delivered["tdk_filename"],
                    "D.docx",
                )
                self.assertEqual(
                    [event.action for event in audit.events],
                    [
                        "article.images.prepared",
                        "article.docx.exported",
                        "article.tdk.generated",
                    ],
                )
                tdk_asset = (
                    PostgresKnowledgeAssetRepository(
                        self.engine
                    ).get_asset(
                        self.project_a,
                        tdk_delivered["tdk_asset_id"],
                    )
                )
                assert tdk_asset is not None
                tdk_key = str(tdk_asset.metadata["object_key"])
                tdk_document = Document(
                    BytesIO(private_store.objects[tdk_key])
                )
                self.assertEqual(
                    [paragraph.text[:3] for paragraph in tdk_document.paragraphs],
                    ["T: ", "D: ", "K: "],
                )
                self.assertEqual(
                    client.get(
                        (
                            f"/api/projects/{self.project_a}/assets/"
                            f"{tdk_delivered['tdk_asset_id']}/download"
                        )
                    ).status_code,
                    404,
                )
                tdk_download_path = (
                    f"/api/projects/{self.project_a}/tasks/"
                    f"{self.task_a}/tdk/download"
                )
                tdk_download = client.get(tdk_download_path)
                self.assertEqual(
                    tdk_download.status_code,
                    200,
                    tdk_download.text,
                )
                self.assertEqual(
                    tdk_download.json()["asset_id"],
                    tdk_delivered["tdk_asset_id"],
                )
                screenshot_bytes = self._image_bytes("white")
                screenshot_asset = (
                    object_service.upload_final_ai_screenshot(
                        actor=actor,
                        project_id=self.project_a,
                        asset_id=(
                            "asset_"
                            + hashlib.sha256(
                                screenshot_bytes
                            ).hexdigest()
                        ),
                        data=screenshot_bytes,
                        width=320,
                        height=240,
                    )
                )
                complete = repository.get(self.task_a)
                assert complete is not None
                complete["humanized_article"] = complete["linked_article"]
                complete["final_ai_check"] = {
                    "confirmed": True,
                    "screenshot_asset_id": screenshot_asset.asset_id,
                    "screenshot_content_hash": (
                        screenshot_asset.content_hash
                    ),
                    "screenshot_filename": "final-ai-rate.png",
                    "screenshot_width": 320,
                    "screenshot_height": 240,
                    "article_hash": hashlib.sha256(
                        complete["linked_article"].encode("utf-8")
                    ).hexdigest(),
                }
                repository.upsert(complete)
                package_path = (
                    f"/api/projects/{self.project_a}/tasks/"
                    f"{self.task_a}/package-delivery"
                )
                packaged = client.post(
                    package_path,
                    json={"revision": 3},
                )
                self.assertEqual(
                    packaged.status_code,
                    200,
                    packaged.text,
                )
                package_task = packaged.json()
                self.assertEqual(package_task["revision"], 4)
                self.assertEqual(
                    package_task["delivery_package_path"],
                    "",
                )
                self.assertTrue(
                    package_task["delivery_package_asset_id"]
                )
                self.assertEqual(
                    package_task["delivery_package_filename"],
                    (
                        f"{self.project_a}-topic_"
                        f"{package_task['topic_index']:03d}.zip"
                    ),
                )
                self.assertEqual(
                    [event.action for event in audit.events],
                    [
                        "article.images.prepared",
                        "article.docx.exported",
                        "article.tdk.generated",
                        "article.delivery.packaged",
                    ],
                )
                package_asset = (
                    PostgresKnowledgeAssetRepository(
                        self.engine
                    ).get_asset(
                        self.project_a,
                        package_task[
                            "delivery_package_asset_id"
                        ],
                    )
                )
                assert package_asset is not None
                package_key = str(
                    package_asset.metadata["object_key"]
                )
                with ZipFile(
                    BytesIO(private_store.objects[package_key])
                ) as archive:
                    self.assertEqual(
                        set(archive.namelist()),
                        {
                            delivered["docx_filename"],
                            "D.docx",
                            *[
                                image["filename"]
                                for image in delivered["images"]
                            ],
                            "final-ai-rate.png",
                        },
                    )
                self.assertEqual(
                    client.get(
                        (
                            f"/api/projects/{self.project_a}/assets/"
                            f"{package_task['delivery_package_asset_id']}/"
                            "download"
                        )
                    ).status_code,
                    404,
                )
                package_download_path = (
                    f"/api/projects/{self.project_a}/tasks/"
                    f"{self.task_a}/delivery-package/download"
                )
                self.assertEqual(
                    client.get(package_download_path).status_code,
                    200,
                )
                put_count = len(private_store.put_calls)
                self.assertEqual(
                    client.post(
                        export_path,
                        json={"revision": 1},
                    ).status_code,
                    409,
                )
                self.assertEqual(
                    len(private_store.put_calls),
                    put_count,
                )
                with patch(
                    "services.server_tdk_export.LLMClient",
                    StubServerTdkLlm,
                ):
                    self.assertEqual(
                        client.post(
                            tdk_path,
                            json={"revision": 2},
                        ).status_code,
                        409,
                    )
                self.assertEqual(
                    len(private_store.put_calls),
                    put_count,
                )
                with self.engine.begin() as connection:
                    connection.execute(
                        project_memberships.update()
                        .where(
                            project_memberships.c.organization_id
                            == self.org_a,
                            project_memberships.c.project_id
                            == self.project_a,
                            project_memberships.c.user_id == self.user_a,
                        )
                        .values(role="viewer")
                    )
                self.assertEqual(
                    client.get(docx_download_path).status_code,
                    403,
                )
                self.assertEqual(
                    client.get(tdk_download_path).status_code,
                    403,
                )
                self.assertEqual(
                    client.get(package_download_path).status_code,
                    403,
                )
                self.assertFalse(local_state.exists())

    def test_server_final_ai_review_uses_private_screenshot_asset(
        self,
    ) -> None:
        import app as app_module

        repository = self._task_repository(
            organization_id=self.org_a,
            project_id=self.project_a,
        )
        record = repository.get(self.task_a)
        assert record is not None
        record.update(
            {
                "status": "humanized_ready",
                "humanized_article": SERVER_ARTICLE,
                "article": SERVER_ARTICLE,
                "final_ai_check": {},
            }
        )
        repository.upsert(record)

        codec = ServerActorSessionCodec(b"v" * 32)
        actor = ActorIdentity(self.org_a, self.user_a)
        base_config = app_module.config()
        private_store = FakeDownloadStore()
        object_service = ProjectKnowledgeObjectService(
            store=private_store,
            bucket="private-bucket",
            repository=PostgresKnowledgeAssetRepository(self.engine),
            access=ProjectAccessService(
                PostgresProjectAccessRepository(self.engine)
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            local_state = Path(directory) / "must-not-exist"
            isolated = replace(
                base_config,
                data_file=local_state / "tasks.json",
                knowledge_agent_enabled=False,
            )
            with (
                patch.object(app_module, "config", return_value=isolated),
                patch.dict(
                    os.environ,
                    {
                        "ARTICLE_AGENT_SERVER_MODE": "true",
                        "ARTICLE_AGENT_SERVER_SESSION_SECRET": "v" * 32,
                        "ARTICLE_AGENT_OBJECT_STORE_BUCKET": "",
                    },
                    clear=False,
                ),
                TestClient(app_module.app) as client,
            ):
                audit = self._install_recording_audit(
                    client.app,
                    isolated,
                )
                client.app.state.server_project_object_service = (
                    object_service
                )
                client.cookies.set(
                    SERVER_AUTH_COOKIE_NAME,
                    codec.create(actor),
                )
                upload_path = (
                    f"/api/projects/{self.project_a}/tasks/"
                    f"{self.task_a}/checks/final-ai/screenshot"
                )
                confirm_path = (
                    f"/api/projects/{self.project_a}/tasks/"
                    f"{self.task_a}/checks/final-ai"
                )
                self.assertEqual(
                    client.post(
                        upload_path,
                        params={"revision": 0},
                        files={
                            "file": (
                                "final.png",
                                self._image_bytes("white"),
                                "image/png",
                            )
                        },
                    ).status_code,
                    403,
                )
                with self.engine.begin() as connection:
                    connection.execute(
                        project_memberships.update()
                        .where(
                            project_memberships.c.organization_id
                            == self.org_a,
                            project_memberships.c.project_id
                            == self.project_a,
                            project_memberships.c.user_id == self.user_a,
                        )
                        .values(role="reviewer")
                    )
                self.assertEqual(
                    client.post(
                        (
                            f"/api/projects/{self.project_b}/tasks/"
                            f"{self.task_b}/checks/final-ai/screenshot"
                        ),
                        params={"revision": 0},
                        files={
                            "file": (
                                "cross-project.png",
                                self._image_bytes("red"),
                                "image/png",
                            )
                        },
                    ).status_code,
                    403,
                )
                self.assertEqual(
                    client.put(
                        confirm_path,
                        json={
                            "revision": 0,
                            "score": 14.2,
                            "report": "Reviewed final AI result.",
                        },
                    ).status_code,
                    409,
                )

                uploaded = client.post(
                    upload_path,
                    params={"revision": 0},
                    files={
                        "file": (
                            "final.png",
                            self._image_bytes("white"),
                            "image/png",
                        )
                    },
                )

                self.assertEqual(
                    uploaded.status_code,
                    200,
                    uploaded.text,
                )
                screenshot_task = uploaded.json()
                self.assertEqual(screenshot_task["revision"], 1)
                self.assertEqual(
                    screenshot_task["status"],
                    "humanized_ready",
                )
                check = screenshot_task["final_ai_check"]
                self.assertEqual(check["screenshot_path"], "")
                self.assertTrue(check["screenshot_asset_id"])
                self.assertEqual(
                    len(check["screenshot_content_hash"]),
                    64,
                )
                self.assertEqual(
                    check["screenshot_filename"],
                    "final-ai-rate.png",
                )
                self.assertEqual(
                    (
                        check["screenshot_width"],
                        check["screenshot_height"],
                    ),
                    (320, 240),
                )
                self.assertEqual(
                    [event.action for event in audit.events],
                    ["article.final_ai_screenshot.uploaded"],
                )
                self.assertNotIn(
                    "report",
                    audit.events[0].details,
                )
                asset_id = check["screenshot_asset_id"]
                self.assertEqual(
                    client.get(
                        (
                            f"/api/projects/{self.project_a}/assets/"
                            f"{asset_id}/download"
                        )
                    ).status_code,
                    404,
                )
                download_path = (
                    f"{upload_path}/download"
                )
                download = client.get(download_path)
                self.assertEqual(
                    download.status_code,
                    200,
                    download.text,
                )
                self.assertEqual(
                    download.json()["asset_id"],
                    asset_id,
                )

                confirmed = client.put(
                    confirm_path,
                    json={
                        "revision": 1,
                        "score": 14.2,
                        "report": "Reviewed final AI result.",
                    },
                )
                self.assertEqual(
                    confirmed.status_code,
                    200,
                    confirmed.text,
                )
                confirmed_task = confirmed.json()
                self.assertEqual(confirmed_task["revision"], 2)
                self.assertEqual(
                    confirmed_task["status"],
                    "final_ai_checked",
                )
                self.assertTrue(
                    confirmed_task["final_ai_check"]["confirmed"]
                )
                self.assertEqual(
                    confirmed_task["final_ai_check"]["report"],
                    "Reviewed final AI result.",
                )
                self.assertEqual(
                    [event.action for event in audit.events],
                    [
                        "article.final_ai_screenshot.uploaded",
                        "article.final_ai_check.updated",
                    ],
                )
                self.assertNotIn(
                    "report",
                    audit.events[1].details,
                )
                put_count = len(private_store.put_calls)
                self.assertEqual(
                    client.post(
                        upload_path,
                        params={"revision": 0},
                        files={
                            "file": (
                                "stale.png",
                                self._image_bytes("black"),
                                "image/png",
                            )
                        },
                    ).status_code,
                    409,
                )
                self.assertEqual(
                    len(private_store.put_calls),
                    put_count,
                )
                with self.engine.begin() as connection:
                    connection.execute(
                        project_memberships.update()
                        .where(
                            project_memberships.c.organization_id
                            == self.org_a,
                            project_memberships.c.project_id
                            == self.project_a,
                            project_memberships.c.user_id == self.user_a,
                        )
                        .values(role="viewer")
                    )
                self.assertEqual(
                    client.get(download_path).status_code,
                    403,
                )
                self.assertFalse(local_state.exists())

    def test_server_title_generation_uses_system_template_and_published_scope(
        self,
    ) -> None:
        import app as app_module

        repository = self._task_repository(
            organization_id=self.org_a,
            project_id=self.project_a,
        )
        record = repository.get(self.task_a)
        assert record is not None
        record.update(
            {
                "status": "new",
                "selected_title": "",
                "title_candidates": [],
                "outline": "stale confirmed outline",
                "outline_draft": "stale outline draft",
                "article": "stale article",
            }
        )
        repository.upsert(record)
        published_chunk = self._store_outline_context(
            project_id=self.project_a,
            suffix=f"{self.task_a}-title-published",
            text="Topic 2 has one verified published positioning fact.",
        )
        self._store_outline_context(
            project_id=self.project_b,
            suffix=f"{self.task_b}-title-published",
            text="Topic 2 repeated is a cross-project positioning fact.",
        )
        provider = RecordingTitleProvider()
        audit = RecordingAuditWriter()
        access = ProjectAccessService(
            PostgresProjectAccessRepository(self.engine)
        )
        handler = ServerTitleGenerationHandler(
            self.engine,
            provider=provider,
            audit=audit,
        )
        codec = ServerActorSessionCodec(b"t" * 32)
        actor = ActorIdentity(self.org_a, self.user_a)
        base_config = app_module.config()
        with tempfile.TemporaryDirectory() as directory:
            local_state = Path(directory) / "must-not-exist"
            isolated = replace(
                base_config,
                data_file=local_state / "tasks.json",
                knowledge_agent_enabled=False,
            )
            registry = ServerTitleGenerationRegistry(
                self.engine,
                config=isolated,
                access=access,
                handler=handler,
                audit=audit,
            )
            self.addCleanup(registry.stop)
            with (
                patch.object(app_module, "config", return_value=isolated),
                patch.dict(
                    os.environ,
                    {
                        "ARTICLE_AGENT_SERVER_MODE": "true",
                        "ARTICLE_AGENT_SERVER_SESSION_SECRET": "t" * 32,
                    },
                    clear=False,
                ),
                TestClient(app_module.app) as client,
            ):
                app_module.app.state.server_title_generation = registry
                client.cookies.set(
                    SERVER_AUTH_COOKIE_NAME,
                    codec.create(actor),
                )
                path = (
                    f"/api/projects/{self.project_a}/tasks/"
                    f"{self.task_a}/titles"
                )
                self.assertEqual(
                    client.post(path, json={"revision": 0}).status_code,
                    403,
                )
                with self.engine.begin() as connection:
                    connection.execute(
                        project_memberships.update()
                        .where(
                            project_memberships.c.organization_id
                            == self.org_a,
                            project_memberships.c.project_id
                            == self.project_a,
                            project_memberships.c.user_id == self.user_a,
                        )
                        .values(role="editor")
                    )
                self.assertEqual(
                    client.post(
                        path,
                        json={
                            "revision": 0,
                            "instruction": "client override",
                        },
                    ).status_code,
                    422,
                )
                self.assertEqual(
                    client.post(
                        (
                            f"/api/projects/{self.project_b}/tasks/"
                            f"{self.task_b}/titles"
                        ),
                        json={"revision": 0},
                    ).status_code,
                    403,
                )
                queued = client.post(path, json={"revision": 0})
                self.assertEqual(queued.status_code, 200, queued.text)
                public_job = queued.json()
                self.assertEqual(public_job["operation"], "titles")
                self.assertNotIn("request", public_job)
                self.assertNotIn("requested_by_user_id", public_job)
                status_path = (
                    f"{path}/jobs/{public_job['job_id']}"
                )
                terminal = None
                for _attempt in range(100):
                    response = client.get(status_path)
                    self.assertEqual(
                        response.status_code,
                        200,
                        response.text,
                    )
                    terminal = response.json()
                    if terminal["status"] in {
                        "succeeded",
                        "failed",
                        "conflict",
                        "cancelled",
                    }:
                        break
                    time.sleep(0.02)
                assert terminal is not None
                self.assertEqual(terminal["status"], "succeeded")
                self.assertEqual(terminal["result_revision"], 1)
                self.assertEqual(len(provider.calls), 1)
                self.assertEqual(
                    provider.calls[0]["chunk_ids"],
                    [published_chunk],
                )
                stored_payload = repository.get(self.task_a)
                assert stored_payload is not None
                stored = TaskRecord.model_validate(stored_payload)
                self.assertEqual(stored.revision, 1)
                self.assertEqual(stored.status, "titles_ready")
                self.assertEqual(len(stored.title_candidates), 10)
                self.assertEqual(stored.selected_title, "")
                self.assertEqual(stored.outline, "")
                self.assertEqual(stored.outline_draft, "")
                self.assertEqual(stored.article, "")
                self.assertNotIn(
                    "Server candidate title",
                    str(audit.events),
                )
                self.assertFalse(local_state.exists())

    def test_server_outline_generation_uses_pinned_prompt_and_published_scope(
        self,
    ) -> None:
        import app as app_module

        published_chunk = self._store_outline_context(
            project_id=self.project_a,
            suffix=f"{self.task_a}-published",
            text=(
                "Selected topic 2 uses one verified published project fact."
            ),
        )
        self._store_outline_context(
            project_id=self.project_a,
            suffix=f"{self.task_a}-inbox",
            text=(
                "Selected topic 2 must not use this unpublished project fact."
            ),
            status="inbox",
        )
        self._store_outline_context(
            project_id=self.project_b,
            suffix=f"{self.task_b}-published",
            text=(
                "Selected topic 2 repeated repeated is a cross-project fact."
            ),
        )
        provider = RecordingOutlineProvider()
        audit = RecordingAuditWriter()
        access = ProjectAccessService(
            PostgresProjectAccessRepository(self.engine)
        )
        handler = ServerOutlineGenerationHandler(
            self.engine,
            provider=provider,
            audit=audit,
        )
        registry = ServerOutlineGenerationRegistry(
            self.engine,
            access=access,
            handler=handler,
            audit=audit,
        )
        self.addCleanup(registry.stop)
        codec = ServerActorSessionCodec(b"o" * 32)
        actor = ActorIdentity(self.org_a, self.user_a)
        base_config = app_module.config()
        with tempfile.TemporaryDirectory() as directory:
            local_state = Path(directory) / "must-not-exist"
            isolated = replace(
                base_config,
                data_file=local_state / "tasks.json",
                knowledge_agent_enabled=False,
            )
            with (
                patch.object(app_module, "config", return_value=isolated),
                patch.dict(
                    os.environ,
                    {
                        "ARTICLE_AGENT_SERVER_MODE": "true",
                        "ARTICLE_AGENT_SERVER_SESSION_SECRET": "o" * 32,
                    },
                    clear=False,
                ),
                TestClient(app_module.app) as client,
            ):
                app_module.app.state.server_outline_generation = registry
                client.cookies.set(
                    SERVER_AUTH_COOKIE_NAME,
                    codec.create(actor),
                )
                path = (
                    f"/api/projects/{self.project_a}/tasks/"
                    f"{self.task_a}/outline"
                )
                self.assertEqual(
                    client.post(path, json={"revision": 0}).status_code,
                    403,
                )
                with self.engine.begin() as connection:
                    connection.execute(
                        project_memberships.update()
                        .where(
                            project_memberships.c.organization_id
                            == self.org_a,
                            project_memberships.c.project_id
                            == self.project_a,
                            project_memberships.c.user_id == self.user_a,
                        )
                        .values(role="editor")
                    )
                self.assertEqual(
                    client.post(
                        path,
                        json={
                            "revision": 0,
                            "prompt_id": "client-controlled",
                        },
                    ).status_code,
                    422,
                )
                self.assertEqual(
                    client.post(
                        (
                            f"/api/projects/{self.project_b}/tasks/"
                            f"{self.task_b}/outline"
                        ),
                        json={"revision": 0},
                    ).status_code,
                    403,
                )
                queued = client.post(path, json={"revision": 0})
                self.assertEqual(queued.status_code, 200, queued.text)
                public_job = queued.json()
                self.assertEqual(public_job["operation"], "outline")
                self.assertNotIn("request", public_job)
                self.assertNotIn("requested_by_user_id", public_job)
                status_path = (
                    f"{path}/jobs/{public_job['job_id']}"
                )
                terminal = None
                for _attempt in range(100):
                    response = client.get(status_path)
                    self.assertEqual(
                        response.status_code,
                        200,
                        response.text,
                    )
                    terminal = response.json()
                    if terminal["status"] in {
                        "succeeded",
                        "failed",
                        "conflict",
                        "cancelled",
                    }:
                        break
                    time.sleep(0.02)
                assert terminal is not None
                self.assertEqual(terminal["status"], "succeeded")
                self.assertEqual(terminal["result_revision"], 1)
                self.assertEqual(len(provider.calls), 1)
                self.assertEqual(
                    provider.calls[0]["chunk_ids"],
                    [published_chunk],
                )
                self.assertEqual(
                    provider.calls[0]["prompt_source"],
                    "system",
                )
                stored_payload = self._task_repository(
                    organization_id=self.org_a,
                    project_id=self.project_a,
                ).get(self.task_a)
                assert stored_payload is not None
                stored = TaskRecord.model_validate(stored_payload)
                self.assertEqual(stored.revision, 1)
                self.assertEqual(stored.outline, "")
                self.assertEqual(
                    stored.outline_draft,
                    "## Generated server outline\n\n"
                    "### Evidence-led section",
                )
                self.assertEqual(
                    stored.article_versions[-1].kind,
                    "outline_draft",
                )
                self.assertEqual(
                    stored.article_versions[-1].source_kind,
                    "generated",
                )
                assert stored.last_outline_prompt_snapshot is not None
                self.assertEqual(
                    stored.last_outline_prompt_snapshot.source,
                    "system",
                )
                self.assertNotIn(
                    "Generated server outline",
                    str(audit.events),
                )
                self.assertFalse(local_state.exists())

    def test_server_article_generation_uses_pinned_prompt_and_published_scope(
        self,
    ) -> None:
        import app as app_module

        repository = self._task_repository(
            organization_id=self.org_a,
            project_id=self.project_a,
        )
        record = repository.get(self.task_a)
        assert record is not None
        record.update(
            {
                "status": "outline_confirmed",
                "selected_title": "Example Buyer Guide",
                "outline": (
                    "## Buyer Checks\n\n"
                    "### Confirm the application\n\n"
                    "### Compare evidence\n\n"
                    "## FAQ"
                ),
                "outline_draft": (
                    "## Buyer Checks\n\n"
                    "### Confirm the application\n\n"
                    "### Compare evidence\n\n"
                    "## FAQ"
                ),
                "raw_draft_article": "",
                "initial_article": "",
                "article": "",
                "humanized_article": "stale downstream article",
                "article_versions": [],
            }
        )
        repository.upsert(record)
        published_chunk = self._store_outline_context(
            project_id=self.project_a,
            suffix=f"{self.task_a}-article-published",
            text=(
                "Example Buyer Guide for Topic 2 uses one verified "
                "published project fact."
            ),
        )
        self._store_outline_context(
            project_id=self.project_a,
            suffix=f"{self.task_a}-article-inbox",
            text=(
                "Example Buyer Guide for Topic 2 must not use this "
                "unpublished fact."
            ),
            status="inbox",
        )
        self._store_outline_context(
            project_id=self.project_b,
            suffix=f"{self.task_b}-article-published",
            text=(
                "Example Buyer Guide for Topic 2 repeated repeated "
                "is a cross-project fact."
            ),
        )
        provider = RecordingArticleProvider()
        audit = RecordingAuditWriter()
        access = ProjectAccessService(
            PostgresProjectAccessRepository(self.engine)
        )
        handler = ServerArticleGenerationHandler(
            self.engine,
            provider=provider,
            audit=audit,
        )
        codec = ServerActorSessionCodec(b"a" * 32)
        actor = ActorIdentity(self.org_a, self.user_a)
        base_config = app_module.config()
        with tempfile.TemporaryDirectory() as directory:
            local_state = Path(directory) / "must-not-exist"
            isolated = replace(
                base_config,
                data_file=local_state / "tasks.json",
                knowledge_agent_enabled=False,
            )
            registry = ServerArticleGenerationRegistry(
                self.engine,
                config=isolated,
                access=access,
                handler=handler,
                audit=audit,
            )
            self.addCleanup(registry.stop)
            with (
                patch.object(app_module, "config", return_value=isolated),
                patch.dict(
                    os.environ,
                    {
                        "ARTICLE_AGENT_SERVER_MODE": "true",
                        "ARTICLE_AGENT_SERVER_SESSION_SECRET": "a" * 32,
                    },
                    clear=False,
                ),
                TestClient(app_module.app) as client,
            ):
                app_module.app.state.server_article_generation = registry
                client.cookies.set(
                    SERVER_AUTH_COOKIE_NAME,
                    codec.create(actor),
                )
                path = (
                    f"/api/projects/{self.project_a}/tasks/"
                    f"{self.task_a}/article"
                )
                self.assertEqual(
                    client.post(path, json={"revision": 0}).status_code,
                    403,
                )
                with self.engine.begin() as connection:
                    connection.execute(
                        project_memberships.update()
                        .where(
                            project_memberships.c.organization_id
                            == self.org_a,
                            project_memberships.c.project_id
                            == self.project_a,
                            project_memberships.c.user_id == self.user_a,
                        )
                        .values(role="editor")
                    )
                self.assertEqual(
                    client.post(
                        path,
                        json={
                            "revision": 0,
                            "word_count": 1000,
                        },
                    ).status_code,
                    422,
                )
                self.assertEqual(
                    client.post(
                        (
                            f"/api/projects/{self.project_b}/tasks/"
                            f"{self.task_b}/article"
                        ),
                        json={"revision": 0},
                    ).status_code,
                    403,
                )
                queued = client.post(path, json={"revision": 0})
                self.assertEqual(queued.status_code, 200, queued.text)
                public_job = queued.json()
                self.assertEqual(public_job["operation"], "article")
                self.assertNotIn("request", public_job)
                self.assertNotIn("requested_by_user_id", public_job)
                status_path = f"{path}/jobs/{public_job['job_id']}"
                terminal = None
                for _attempt in range(100):
                    response = client.get(status_path)
                    self.assertEqual(
                        response.status_code,
                        200,
                        response.text,
                    )
                    terminal = response.json()
                    if terminal["status"] in {
                        "succeeded",
                        "failed",
                        "conflict",
                        "cancelled",
                    }:
                        break
                    time.sleep(0.02)
                assert terminal is not None
                self.assertEqual(terminal["status"], "succeeded")
                self.assertEqual(terminal["result_revision"], 1)
                self.assertEqual(len(provider.calls), 1)
                self.assertEqual(
                    provider.calls[0]["chunk_ids"],
                    [published_chunk],
                )
                self.assertEqual(
                    provider.calls[0]["prompt_source"],
                    "system",
                )
                self.assertEqual(
                    provider.calls[0]["target_words"],
                    1200,
                )
                stored_payload = repository.get(self.task_a)
                assert stored_payload is not None
                stored = TaskRecord.model_validate(stored_payload)
                self.assertEqual(stored.revision, 1)
                self.assertEqual(stored.status, "draft_ready")
                self.assertEqual(
                    stored.raw_draft_article,
                    SERVER_ARTICLE.strip(),
                )
                self.assertEqual(
                    stored.initial_article,
                    SERVER_ARTICLE.strip(),
                )
                self.assertEqual(stored.article, stored.initial_article)
                self.assertEqual(stored.humanized_article, "")
                self.assertEqual(
                    [item.kind for item in stored.article_versions],
                    ["raw_draft", "initial"],
                )
                assert stored.last_article_prompt_snapshot is not None
                self.assertEqual(
                    stored.last_article_prompt_snapshot.source,
                    "system",
                )
                self.assertNotIn(
                    "Example Buyer Guide",
                    str(audit.events),
                )
                self.assertFalse(local_state.exists())

    def test_outline_context_rejects_unpublished_pinned_chunk(
        self,
    ) -> None:
        suffix = f"{self.task_a}-context-revoked"
        chunk_id = self._store_outline_context(
            project_id=self.project_a,
            suffix=suffix,
            text="Selected topic 2 published context.",
        )
        context = PostgresPublishedOutlineContext(self.engine)
        selected = context.select(
            project_id=self.project_a,
            query="Selected topic 2",
        )
        self.assertEqual(
            [chunk.chunk_id for chunk in selected],
            [chunk_id],
        )
        with self.engine.begin() as connection:
            connection.execute(
                knowledge_sources.update()
                .where(
                    knowledge_sources.c.project_id == self.project_a,
                    knowledge_sources.c.source_id == f"{suffix}-source",
                )
                .values(
                    status="stale",
                    current_snapshot_id=None,
                )
            )
        with self.assertRaisesRegex(
            JobConflict,
            "^published outline context changed$",
        ):
            context.load_current(
                project_id=self.project_a,
                chunk_ids=(chunk_id,),
            )

    def test_server_product_rediscovery_uses_requested_actor_and_pg_worker(
        self,
    ) -> None:
        import app as app_module

        calls: list[dict[str, object]] = []

        def handler(job, cancelled):
            self.assertFalse(cancelled())
            calls.append(
                {
                    "organization_id": job["organization_id"],
                    "project_id": job["project_id"],
                    "task_id": job["task_id"],
                    "requested_by_user_id": job[
                        "requested_by_user_id"
                    ],
                    "request": dict(job["request"]),
                }
            )
            return int(job["source_revision"])

        access = ProjectAccessService(
            PostgresProjectAccessRepository(self.engine)
        )
        rediscovery_audit = RecordingAuditWriter()
        registry = ServerProductRediscoveryRegistry(
            self.engine,
            access=access,
            handler=handler,
            audit=rediscovery_audit,
        )
        self.addCleanup(registry.stop)
        codec = ServerActorSessionCodec(b"r" * 32)
        actor = ActorIdentity(self.org_a, self.user_a)
        base_config = app_module.config()
        task_repository = self._task_repository(
            organization_id=self.org_a,
            project_id=self.project_a,
        )
        task_before = task_repository.get(self.task_a)
        with tempfile.TemporaryDirectory() as directory:
            local_state = Path(directory) / "must-not-exist"
            isolated = replace(
                base_config,
                data_file=local_state / "tasks.json",
                knowledge_agent_enabled=False,
            )
            with (
                patch.object(app_module, "config", return_value=isolated),
                patch.dict(
                    os.environ,
                    {
                        "ARTICLE_AGENT_SERVER_MODE": "true",
                        "ARTICLE_AGENT_SERVER_SESSION_SECRET": "r" * 32,
                    },
                    clear=False,
                ),
                TestClient(app_module.app) as client,
            ):
                app_module.app.state.server_product_rediscovery = registry
                client.cookies.set(
                    SERVER_AUTH_COOKIE_NAME,
                    codec.create(actor),
                )
                path = (
                    f"/api/projects/{self.project_a}/tasks/"
                    f"{self.task_a}/product-rediscovery"
                )
                payload = {
                    "revision": 0,
                    "category_url": (
                        f"https://{self.project_a}/products"
                    ),
                    "max_products": 3,
                }
                self.assertEqual(
                    client.post(path, json=payload).status_code,
                    403,
                )
                with self.engine.begin() as connection:
                    connection.execute(
                        project_memberships.update()
                        .where(
                            project_memberships.c.organization_id
                            == self.org_a,
                            project_memberships.c.project_id
                            == self.project_a,
                            project_memberships.c.user_id == self.user_a,
                        )
                        .values(role="editor")
                    )
                self.assertEqual(
                    client.post(
                        (
                            f"/api/projects/{self.project_b}/tasks/"
                            f"{self.task_b}/product-rediscovery"
                        ),
                        json=payload,
                    ).status_code,
                    403,
                )
                queued = client.post(path, json=payload)
                self.assertEqual(queued.status_code, 200, queued.text)
                public_job = queued.json()
                self.assertEqual(
                    public_job["operation"],
                    "product_rediscovery",
                )
                self.assertNotIn("request", public_job)
                self.assertNotIn(
                    "requested_by_user_id",
                    public_job,
                )
                self.assertNotIn("error", public_job)

                status_path = (
                    f"{path}/jobs/{public_job['job_id']}"
                )
                terminal = None
                for _attempt in range(100):
                    current = client.get(status_path)
                    self.assertEqual(
                        current.status_code,
                        200,
                        current.text,
                    )
                    terminal = current.json()
                    if terminal["status"] in {
                        "succeeded",
                        "failed",
                        "conflict",
                        "cancelled",
                    }:
                        break
                    time.sleep(0.02)
                assert terminal is not None
                self.assertEqual(terminal["status"], "succeeded")
                self.assertEqual(terminal["result_revision"], 0)
                self.assertEqual(
                    client.get(
                        (
                            f"/api/projects/{self.project_a}/tasks/"
                            f"{self.task_b}/product-rediscovery/jobs/"
                            f"{public_job['job_id']}"
                        )
                    ).status_code,
                    404,
                )
                self.assertEqual(len(calls), 1)
                self.assertEqual(
                    calls[0],
                    {
                        "organization_id": self.org_a,
                        "project_id": self.project_a,
                        "task_id": self.task_a,
                        "requested_by_user_id": self.user_a,
                        "request": {
                            "category_url": (
                                f"https://{self.project_a}/products"
                            ),
                            "max_products": 3,
                        },
                    },
                )
                with self.engine.connect() as connection:
                    requested_by = connection.execute(
                        sa.select(
                            background_jobs.c.requested_by_user_id
                        ).where(
                            background_jobs.c.organization_id
                            == self.org_a,
                            background_jobs.c.project_id
                            == self.project_a,
                            background_jobs.c.job_id
                            == public_job["job_id"],
                        )
                    ).scalar_one()
                self.assertEqual(requested_by, self.user_a)
                self.assertEqual(len(rediscovery_audit.events), 2)
                audit_event = rediscovery_audit.events[0]
                self.assertEqual(
                    audit_event.action,
                    "knowledge.products.rediscovery.queued",
                )
                self.assertEqual(
                    audit_event.target_id,
                    public_job["job_id"],
                )
                self.assertEqual(
                    audit_event.details,
                    {
                        "operation": "product_rediscovery",
                        "source_revision": 0,
                        "max_products": 3,
                    },
                )
                self.assertNotIn(
                    payload["category_url"],
                    str(audit_event.details),
                )
                terminal_audit = rediscovery_audit.events[1]
                self.assertEqual(
                    terminal_audit.action,
                    "background_job.terminal",
                )
                self.assertEqual(
                    terminal_audit.target_id,
                    public_job["job_id"],
                )
                self.assertEqual(
                    terminal_audit.details,
                    {
                        "operation": "product_rediscovery",
                        "status": "succeeded",
                        "attempts": 1,
                        "source_revision": 0,
                        "result_revision": 0,
                    },
                )
                self.assertNotIn(
                    payload["category_url"],
                    str(terminal_audit.details),
                )
                self.assertEqual(
                    client.post(
                        path,
                        json={**payload, "revision": 99},
                    ).status_code,
                    409,
                )
                self.assertEqual(
                    task_repository.get(self.task_a),
                    task_before,
                )
                self.assertFalse(local_state.exists())
            registry.stop()

    def test_product_rediscovery_audit_failure_rolls_back_job(self) -> None:
        class FailingAudit:
            def append(self, connection, event):
                raise RuntimeError(
                    "audit provider included https://secret.example.test"
                )

        with self.engine.begin() as connection:
            connection.execute(
                project_memberships.update()
                .where(
                    project_memberships.c.organization_id == self.org_a,
                    project_memberships.c.project_id == self.project_a,
                    project_memberships.c.user_id == self.user_a,
                )
                .values(role="editor")
            )
        registry = ServerProductRediscoveryRegistry(
            self.engine,
            access=ProjectAccessService(
                PostgresProjectAccessRepository(self.engine)
            ),
            handler=lambda job, cancelled: 0,
            audit=FailingAudit(),
        )
        self.addCleanup(registry.stop)
        command = ProductRediscoveryCommand(
            category_url="https://secret.example.test/products",
            max_products=3,
        )

        with self.assertRaisesRegex(
            ProductRediscoveryUnavailable,
            "^product rediscovery could not be queued$",
        ) as caught:
            registry.enqueue(
                actor=ActorIdentity(self.org_a, self.user_a),
                project_id=self.project_a,
                task_id=self.task_a,
                source_revision=0,
                command=command,
            )

        self.assertNotIn("secret.example.test", str(caught.exception))
        with self.engine.connect() as connection:
            job_count = connection.execute(
                sa.select(sa.func.count())
                .select_from(background_jobs)
                .where(
                    background_jobs.c.organization_id == self.org_a,
                    background_jobs.c.project_id == self.project_a,
                    background_jobs.c.task_id == self.task_a,
                )
            ).scalar_one()
            batch_count = connection.execute(
                sa.select(sa.func.count())
                .select_from(job_batches)
                .where(
                    job_batches.c.organization_id == self.org_a,
                    job_batches.c.project_id == self.project_a,
                )
            ).scalar_one()
        self.assertEqual(job_count, 0)
        self.assertEqual(batch_count, 0)

    def test_product_rediscovery_stop_requeues_and_reports_drain(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                project_memberships.update()
                .where(
                    project_memberships.c.organization_id == self.org_a,
                    project_memberships.c.project_id == self.project_a,
                    project_memberships.c.user_id == self.user_a,
                )
                .values(role="editor")
            )
        entered = threading.Event()

        def cooperative_handler(job, cancelled):
            entered.set()
            while not cancelled():
                time.sleep(0.005)
            raise JobCancelled("server is stopping")

        registry = ServerProductRediscoveryRegistry(
            self.engine,
            access=ProjectAccessService(
                PostgresProjectAccessRepository(self.engine)
            ),
            handler=cooperative_handler,
            audit=RecordingAuditWriter(),
        )
        job = registry.enqueue(
            actor=ActorIdentity(self.org_a, self.user_a),
            project_id=self.project_a,
            task_id=self.task_a,
            source_revision=0,
            command=ProductRediscoveryCommand(
                category_url=f"https://{self.project_a}/products",
                max_products=3,
            ),
        )
        self.assertTrue(entered.wait(timeout=2))

        report = registry.stop(timeout_seconds=2)

        self.assertTrue(report.drained)
        self.assertEqual(report.project_runner_count, 1)
        self.assertEqual(report.remaining_jobs, 0)
        self.assertEqual(registry.stop(), report)
        raw_queue = PostgresJobQueue(
            self.engine,
            organization_id=self.org_a,
            project_id=self.project_a,
        )
        interrupted = raw_queue.get_job(str(job["job_id"]))
        self.assertEqual(interrupted["status"], "queued")
        self.assertFalse(interrupted["cancel_requested"])

        replacement = ServerProductRediscoveryRegistry(
            self.engine,
            access=ProjectAccessService(
                PostgresProjectAccessRepository(self.engine)
            ),
            handler=lambda queued, cancelled: int(
                queued["source_revision"]
            ),
            audit=RecordingAuditWriter(),
        )
        self.addCleanup(replacement.stop)
        replacement.start_existing()
        deadline = time.time() + 2
        while time.time() < deadline:
            resumed = raw_queue.get_job(str(job["job_id"]))
            if resumed["status"] == "succeeded":
                break
            time.sleep(0.01)
        else:
            self.fail("Interrupted product rediscovery did not resume.")

    def test_product_rediscovery_handler_uses_active_project_domain(
        self,
    ) -> None:
        calls: list[dict[str, object]] = []

        class FakeSync:
            def sync_category(self, **kwargs):
                calls.append(dict(kwargs))
                return object()

        handler = ServerProductRediscoveryHandler(
            self.engine,
            sync_factory=lambda organization_id, project_id: (
                calls.append(
                    {
                        "factory_organization_id": organization_id,
                        "factory_project_id": project_id,
                    }
                )
                or FakeSync()
            ),
        )
        job = {
            "operation": "product_rediscovery",
            "organization_id": self.org_a,
            "project_id": self.project_a,
            "task_id": self.task_a,
            "source_revision": 0,
            "request": {
                "category_url": (
                    f"https://{self.project_a}/products"
                ),
                "max_products": 4,
            },
        }

        result = handler(job, lambda: False)

        self.assertEqual(result, 0)
        self.assertEqual(
            calls,
            [
                {
                    "factory_organization_id": self.org_a,
                    "factory_project_id": self.project_a,
                },
                {
                    "project_id": self.project_a,
                    "site_url": f"https://{self.project_a}",
                    "category_url": (
                        f"https://{self.project_a}/products"
                    ),
                    "max_products": 4,
                },
            ],
        )
        stale = dict(job)
        stale["source_revision"] = 99
        with self.assertRaisesRegex(
            JobConflict,
            "source task revision changed",
        ):
            handler(stale, lambda: False)
        self.assertEqual(len(calls), 2)

    def test_product_rediscovery_status_survives_unconfigured_runner(
        self,
    ) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                project_memberships.update()
                .where(
                    project_memberships.c.organization_id == self.org_a,
                    project_memberships.c.project_id == self.project_a,
                    project_memberships.c.user_id == self.user_a,
                )
                .values(role="editor")
            )
        raw_queue = PostgresJobQueue(
            self.engine,
            organization_id=self.org_a,
            project_id=self.project_a,
        )
        batch = raw_queue.create_batch(
            "product_rediscovery",
            [
                {
                    "task_id": self.task_a,
                    "source_revision": 0,
                    "customer": self.project_a,
                    "topic_index": 2,
                    "request": {
                        "category_url": (
                            f"https://{self.project_a}/products"
                        ),
                        "max_products": 3,
                    },
                }
            ],
            customer=self.project_a,
            requested_by_user_id=self.user_a,
        )
        job_id = str(batch["jobs"][0]["id"])
        registry = ServerProductRediscoveryRegistry(
            self.engine,
            access=ProjectAccessService(
                PostgresProjectAccessRepository(self.engine)
            ),
            handler=None,
        )
        self.addCleanup(registry.stop)
        actor = ActorIdentity(self.org_a, self.user_a)

        current = registry.get_job(
            actor=actor,
            project_id=self.project_a,
            task_id=self.task_a,
            job_id=job_id,
        )

        self.assertEqual(current["status"], "queued")
        self.assertIsNone(current["started_at"])
        with self.assertRaisesRegex(
            ProductRediscoveryUnavailable,
            "not configured",
        ):
            registry.enqueue(
                actor=actor,
                project_id=self.project_a,
                task_id=self.task_a,
                source_revision=0,
                command=ProductRediscoveryCommand(
                    category_url=(
                        f"https://{self.project_a}/products"
                    ),
                    max_products=3,
                ),
            )
        with self.engine.begin() as connection:
            connection.execute(
                background_jobs.update()
                .where(
                    background_jobs.c.organization_id == self.org_a,
                    background_jobs.c.project_id == self.project_a,
                    background_jobs.c.job_id == job_id,
                )
                .values(operation="outline")
            )
        with self.assertRaises(KeyError):
            registry.get_job(
                actor=actor,
                project_id=self.project_a,
                task_id=self.task_a,
                job_id=job_id,
            )

    def test_product_rediscovery_worker_rejects_revoked_requester(
        self,
    ) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                project_memberships.update()
                .where(
                    project_memberships.c.organization_id == self.org_a,
                    project_memberships.c.project_id == self.project_a,
                    project_memberships.c.user_id == self.user_a,
                )
                .values(role="editor")
            )
        raw_queue = PostgresJobQueue(
            self.engine,
            organization_id=self.org_a,
            project_id=self.project_a,
        )
        batch = raw_queue.create_batch(
            "product_rediscovery",
            [
                {
                    "task_id": self.task_a,
                    "source_revision": 0,
                    "customer": self.project_a,
                    "topic_index": 2,
                    "request": {
                        "category_url": (
                            f"https://{self.project_a}/products"
                        ),
                        "max_products": 3,
                    },
                }
            ],
            customer=self.project_a,
            requested_by_user_id=self.user_a,
        )
        job_id = str(batch["jobs"][0]["id"])
        with self.engine.begin() as connection:
            connection.execute(
                project_memberships.update()
                .where(
                    project_memberships.c.organization_id == self.org_a,
                    project_memberships.c.project_id == self.project_a,
                    project_memberships.c.user_id == self.user_a,
                )
                .values(role="viewer")
            )
        calls: list[str] = []
        registry = ServerProductRediscoveryRegistry(
            self.engine,
            access=ProjectAccessService(
                PostgresProjectAccessRepository(self.engine)
            ),
            handler=lambda job, cancelled: (
                calls.append(str(job["id"])) or 0
            ),
            audit=RecordingAuditWriter(),
        )
        self.addCleanup(registry.stop)

        registry.start_existing()

        current = None
        for _attempt in range(100):
            current = raw_queue.get_job(job_id)
            if current["status"] == "conflict":
                break
            time.sleep(0.02)
        assert current is not None
        self.assertEqual(current["status"], "conflict")
        self.assertEqual(
            current["error"],
            "job actor is not authorized",
        )
        self.assertEqual(calls, [])

    def test_server_rewrites_only_one_snapshotted_article_section(
        self,
    ) -> None:
        import app as app_module

        repository = self._task_repository(
            organization_id=self.org_a,
            project_id=self.project_a,
        )
        record = repository.get(self.task_a)
        assert record is not None
        record.update(
            {
                "status": "humanized_ready",
                "selected_title": "Example Buyer Guide",
                "initial_article": SERVER_ARTICLE,
                "humanized_article": "downstream copy",
                "article": "downstream copy",
                "article_versions": [],
            }
        )
        repository.upsert(record)

        codec = ServerActorSessionCodec(b"q" * 32)
        actor = ActorIdentity(self.org_a, self.user_a)
        base_config = app_module.config()
        with tempfile.TemporaryDirectory() as directory:
            local_state = Path(directory) / "must-not-exist"
            isolated = replace(
                base_config,
                data_file=local_state / "tasks.json",
                knowledge_agent_enabled=False,
            )
            with (
                patch.object(app_module, "config", return_value=isolated),
                patch.dict(
                    os.environ,
                    {
                        "ARTICLE_AGENT_SERVER_MODE": "true",
                        "ARTICLE_AGENT_SERVER_SESSION_SECRET": "q" * 32,
                    },
                    clear=False,
                ),
                TestClient(app_module.app) as client,
            ):
                audit = self._install_recording_audit(
                    client.app,
                    isolated,
                )
                client.cookies.set(
                    SERVER_AUTH_COOKIE_NAME,
                    codec.create(actor),
                )
                path = (
                    f"/api/projects/{self.project_a}/tasks/"
                    f"{self.task_a}/article/sections"
                )
                request = {
                    "revision": 0,
                    "heading_path": ["Buyer Checks"],
                    "replacement_body": (
                        "### Confirm the application\n\n"
                        "Use revised application guidance.\n\n"
                        "### Compare evidence\n\n"
                        "Use revised evidence guidance."
                    ),
                }
                self.assertEqual(
                    client.put(path, json=request).status_code,
                    403,
                )
                with self.engine.begin() as connection:
                    connection.execute(
                        project_memberships.update()
                        .where(
                            project_memberships.c.organization_id
                            == self.org_a,
                            project_memberships.c.project_id
                            == self.project_a,
                            project_memberships.c.user_id == self.user_a,
                        )
                        .values(role="editor")
                    )
                escaped = dict(request)
                escaped["replacement_body"] = (
                    "## FAQ\n\nAttempted sibling replacement."
                )
                self.assertEqual(
                    client.put(path, json=escaped).status_code,
                    422,
                )
                self.assertEqual(
                    repository.get(self.task_a)["revision"],  # type: ignore[index]
                    0,
                )
                self.assertEqual(
                    client.put(
                        (
                            f"/api/projects/{self.project_a}/tasks/"
                            f"{self.task_b}/article/sections"
                        ),
                        json=request,
                    ).status_code,
                    404,
                )

                response = client.put(path, json=request)
                self.assertEqual(response.status_code, 200, response.text)
                saved = response.json()
                self.assertEqual(saved["revision"], 1)
                self.assertEqual(saved["status"], "draft_ready")
                self.assertEqual(saved["humanized_article"], "")
                self.assertIn(
                    "Use revised application guidance.",
                    saved["initial_article"],
                )
                self.assertNotIn(
                    "Keep the original application guidance.",
                    saved["initial_article"],
                )
                original_prefix = SERVER_ARTICLE.split(
                    "## Buyer Checks",
                    1,
                )[0]
                original_suffix = (
                    "## FAQ" + SERVER_ARTICLE.split("## FAQ", 1)[1]
                )
                self.assertTrue(
                    saved["initial_article"].startswith(original_prefix)
                )
                self.assertTrue(
                    saved["initial_article"].endswith(original_suffix)
                )
                self.assertEqual(
                    [
                        item["source_kind"]
                        for item in saved["article_versions"]
                    ],
                    [
                        "before_section_rewrite",
                        "section_rewrite",
                    ],
                )
                self.assertEqual(
                    saved["article_versions"][0]["content"],
                    SERVER_ARTICLE,
                )
                self.assertEqual(
                    [event.action for event in audit.events],
                    ["article.section.replaced"],
                )
                self.assertEqual(
                    client.put(path, json=request).status_code,
                    409,
                )
                persisted = repository.get(self.task_a)
                self.assertEqual(
                    len(persisted["article_versions"]),  # type: ignore[index]
                    2,
                )
                self.assertFalse(local_state.exists())


if __name__ == "__main__":
    unittest.main()
