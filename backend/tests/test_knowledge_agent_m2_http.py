from __future__ import annotations

import os
import sys
import tempfile
import unittest
from io import BytesIO
from pathlib import Path

import sqlalchemy as sa
from docx import Document
from fastapi import FastAPI
from fastapi.testclient import TestClient


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from knowledge_agent.http import router  # noqa: E402
from knowledge_agent.runtime import create_knowledge_runtime  # noqa: E402
from knowledge_agent import EMBEDDING_DIMENSIONS, EmbeddingBatch  # noqa: E402


class FakeEmbeddingProvider:
    model_id = "test-embedding-model"
    dimensions = EMBEDDING_DIMENSIONS

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def embed(self, texts: tuple[str, ...]) -> EmbeddingBatch:
        self.calls.append(tuple(texts))
        vectors = tuple(
            (float(index + 1),) + (0.0,) * (EMBEDDING_DIMENSIONS - 1)
            for index, _text in enumerate(texts)
        )
        return EmbeddingBatch(vectors=vectors, model=self.model_id)


def docx_bytes() -> bytes:
    document = Document()
    document.core_properties.title = "Private Fastener Specification"
    document.add_heading("Wood Screw", level=1)
    document.add_paragraph("Material: carbon steel.")
    output = BytesIO()
    document.save(output)
    return output.getvalue()


class DisabledKnowledgeHttpTests(unittest.TestCase):
    def test_routes_return_not_found_without_enabled_runtime(self) -> None:
        app = FastAPI()
        app.include_router(router)
        with TestClient(app) as client:
            response = client.get("/api/knowledge/example.com")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "Knowledge Agent is disabled.")


@unittest.skipUnless(
    os.environ.get("ARTICLE_AGENT_DATABASE_URL"),
    "ARTICLE_AGENT_DATABASE_URL is required for PostgreSQL integration tests",
)
class KnowledgeHttpIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_directory = tempfile.TemporaryDirectory()
        cls.embedding_provider = FakeEmbeddingProvider()
        cls.runtime = create_knowledge_runtime(
            database_url=os.environ["ARTICLE_AGENT_DATABASE_URL"],
            artifact_root=Path(cls.temp_directory.name),
            embedding_provider=cls.embedding_provider,
        )
        app = FastAPI()
        app.state.knowledge_agent_runtime = cls.runtime
        app.include_router(router)
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()
        cls.runtime.close()
        cls.temp_directory.cleanup()

    def setUp(self) -> None:
        with self.runtime.engine.begin() as connection:
            for table in (
                "knowledge_product_asset_evidence",
                "knowledge_product_source_evidence",
                "knowledge_products",
                "snapshot_assets",
                "knowledge_assets",
                "knowledge_chunks",
                "source_snapshots",
                "knowledge_sources",
                "projects",
            ):
                connection.execute(sa.text(f"DELETE FROM {table}"))

    def tearDown(self) -> None:
        self.setUp()

    def test_upload_list_and_open_original_evidence(self) -> None:
        content = docx_bytes()
        response = self.client.post(
            "/api/knowledge/www.example.com/sources/upload",
            files={
                "file": (
                    "private-spec.docx",
                    content,
                    (
                        "application/vnd.openxmlformats-officedocument."
                        "wordprocessingml.document"
                    ),
                )
            },
            data={
                "display_name": "Private specification",
                "trust_tier": "hard_fact",
            },
        )

        self.assertEqual(response.status_code, 201, response.text)
        uploaded = response.json()
        self.assertEqual(uploaded["project_id"], "example.com")
        self.assertEqual(uploaded["status"], "inbox")
        self.assertEqual(uploaded["parser_name"], "docx-lightweight")
        self.assertGreater(uploaded["chunk_count"], 0)

        repeated = self.client.post(
            "/api/knowledge/example.com/sources/upload",
            files={"file": ("private-spec.docx", content)},
            data={
                "display_name": "Private specification",
                "trust_tier": "hard_fact",
            },
        )
        self.assertEqual(repeated.status_code, 201, repeated.text)
        self.assertEqual(
            repeated.json()["snapshot_id"],
            uploaded["snapshot_id"],
        )

        listing = self.client.get("/api/knowledge/example.com")
        self.assertEqual(listing.status_code, 200, listing.text)
        library = listing.json()
        self.assertEqual(library["source_count"], 1)
        self.assertEqual(library["inbox_count"], 1)
        self.assertEqual(library["published_count"], 0)
        self.assertEqual(len(library["sources"]), 1)
        source = library["sources"][0]
        self.assertEqual(
            source["classification_reason"],
            "operator uploaded private document",
        )
        self.assertTrue(source["raw_evidence_url"])

        evidence = self.client.get(source["raw_evidence_url"])
        self.assertEqual(evidence.status_code, 200, evidence.text)
        self.assertEqual(evidence.content, content)

    def test_upload_rejects_unsupported_format_without_creating_source(self) -> None:
        response = self.client.post(
            "/api/knowledge/example.com/sources/upload",
            files={"file": ("notes.txt", b"plain text")},
        )

        self.assertEqual(response.status_code, 422)
        listing = self.client.get("/api/knowledge/example.com")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.json()["source_count"], 0)

    def test_source_requires_review_before_embedding_and_atomic_publication(self) -> None:
        uploaded = self.client.post(
            "/api/knowledge/example.com/sources/upload",
            files={"file": ("private-spec.docx", docx_bytes())},
        ).json()
        source_id = uploaded["source_id"]

        premature = self.client.post(
            f"/api/knowledge/example.com/sources/{source_id}/publish"
        )
        self.assertEqual(premature.status_code, 409)
        self.assertIn("approved", premature.json()["detail"])

        reviewed = self.client.put(
            f"/api/knowledge/example.com/sources/{source_id}/review",
            json={
                "source_kind": "private_file",
                "trust_tier": "hard_fact",
                "decision": "approve",
                "reason": "Operator verified the supplied product specification.",
            },
        )
        self.assertEqual(reviewed.status_code, 200, reviewed.text)
        self.assertEqual(reviewed.json()["decision"], "approve")

        published = self.client.post(
            f"/api/knowledge/example.com/sources/{source_id}/publish"
        )
        self.assertEqual(published.status_code, 200, published.text)
        self.assertEqual(published.json()["status"], "published")
        self.assertEqual(
            published.json()["embedding_model"],
            self.embedding_provider.model_id,
        )
        self.assertGreater(published.json()["chunk_count"], 0)

        library = self.client.get("/api/knowledge/example.com").json()
        self.assertEqual(library["published_count"], 1)
        self.assertEqual(library["inbox_count"], 0)
        self.assertEqual(
            library["sources"][0]["current_snapshot_id"],
            uploaded["snapshot_id"],
        )


if __name__ == "__main__":
    unittest.main()
