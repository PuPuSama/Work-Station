from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import unittest
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import sqlalchemy as sa
from fastapi.testclient import TestClient


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
from services.object_store import build_project_object_key  # noqa: E402


class FakeDownloadStore:
    def __init__(self) -> None:
        self.signed: list[tuple[str, int]] = []

    def check_ready(self):
        return None

    def put(self, **kwargs):
        raise AssertionError("not used")

    def get(self, key, *, max_bytes):
        raise AssertionError("not used")

    def create_download_url(self, key, *, expires_seconds):
        self.signed.append((key, expires_seconds))
        return f"https://signed.example.test/{key}"

    def delete(self, key):
        raise AssertionError("not used")


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
                    403,
                )

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
                    f"{self.task_a}/products",
                    json={
                        "revision": 0,
                        "product_ids": [self.product_a],
                    },
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


if __name__ == "__main__":
    unittest.main()
