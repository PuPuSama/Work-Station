from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from knowledge_agent import (  # noqa: E402
    EMBEDDING_DIMENSIONS,
    ChunkEmbedding,
    EmbeddingProvider,
    EvidencePackRepository,
    KnowledgeAgentConfigurationError,
    KnowledgeAgentSettings,
    KnowledgeChunk,
    KnowledgeProject,
    KnowledgeRepository,
    KnowledgeSource,
    SourceSnapshot,
    load_knowledge_agent_settings,
    require_project_scope,
)


def vector(first_value: float = 1.0) -> tuple[float, ...]:
    return (first_value,) + (0.0,) * (EMBEDDING_DIMENSIONS - 1)


class KnowledgeDomainContractTests(unittest.TestCase):
    def test_project_normalizes_domain_and_validates_status(self) -> None:
        project = KnowledgeProject(
            project_id=" project-a ",
            customer_name=" Example Customer ",
            official_domain="WWW.Example.COM.",
        )

        self.assertEqual(project.project_id, "project-a")
        self.assertEqual(project.customer_name, "Example Customer")
        self.assertEqual(project.official_domain, "www.example.com")
        self.assertEqual(project.status, "active")

        with self.assertRaisesRegex(ValueError, "active or archived"):
            KnowledgeProject(
                "project-a",
                "Customer",
                "example.com",
                status="deleted",  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(ValueError, "without a URL scheme"):
            KnowledgeProject("project-a", "Customer", "https://example.com")

    def test_source_enforces_enums_and_publication_boundaries(self) -> None:
        with self.assertRaisesRegex(ValueError, "public_source requires canonical_url"):
            KnowledgeSource(
                project_id="project-a",
                source_id="source-1",
                display_name="Product",
                source_kind="product_detail",
                trust_tier="hard_fact",
                public_source=True,
            )

        with self.assertRaisesRegex(ValueError, "published source requires"):
            KnowledgeSource(
                project_id="project-a",
                source_id="source-1",
                display_name="Product",
                source_kind="product_detail",
                trust_tier="hard_fact",
                status="published",
                canonical_url="https://example.com/product",
                public_source=True,
            )

        source = KnowledgeSource(
            project_id="project-a",
            source_id="source-1",
            display_name="Product",
            source_kind="product_detail",
            trust_tier="hard_fact",
            status="published",
            canonical_url="https://example.com/product",
            public_source=True,
            current_snapshot_id="snapshot-1",
            metadata={"language": "en"},
        )
        self.assertEqual(source.current_snapshot_id, "snapshot-1")
        with self.assertRaises(TypeError):
            source.metadata["language"] = "zh"  # type: ignore[index]

        with self.assertRaisesRegex(ValueError, "absolute HTTP"):
            KnowledgeSource(
                project_id="project-a",
                source_id="source-2",
                display_name="Bad URL",
                source_kind="knowledge_page",
                trust_tier="reference_material",
                canonical_url="https://example.com/page#fragment",
            )

    def test_snapshot_requires_sha256_aware_time_and_absolute_artifact_uri(self) -> None:
        snapshot = SourceSnapshot(
            project_id="project-a",
            source_id="source-1",
            snapshot_id="snapshot-1",
            content_hash="A" * 64,
            fetched_at=datetime(2026, 7, 28, tzinfo=timezone.utc),
            parser_name="html",
            parser_version="1.0",
            raw_artifact_uri="s3://knowledge/raw/source-1.html",
            normalized_artifact_uri="file:///tmp/source-1.json",
        )
        self.assertEqual(snapshot.content_hash, "a" * 64)

        with self.assertRaisesRegex(ValueError, "SHA-256"):
            SourceSnapshot(
                project_id="project-a",
                source_id="source-1",
                snapshot_id="snapshot-1",
                content_hash="not-a-sha256",
                fetched_at=datetime.now(timezone.utc),
                parser_name="html",
                parser_version="1.0",
            )
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            SourceSnapshot(
                project_id="project-a",
                source_id="source-1",
                snapshot_id="snapshot-1",
                content_hash="a" * 64,
                fetched_at=datetime(2026, 7, 28),
                parser_name="html",
                parser_version="1.0",
            )
        with self.assertRaisesRegex(ValueError, "absolute URI"):
            SourceSnapshot(
                project_id="project-a",
                source_id="source-1",
                snapshot_id="snapshot-1",
                content_hash="a" * 64,
                fetched_at=datetime.now(timezone.utc),
                parser_name="html",
                parser_version="1.0",
                raw_artifact_uri=r"C:\knowledge\raw.txt",
            )

    def test_chunk_and_embedding_validate_shape_and_project_scope(self) -> None:
        chunk = KnowledgeChunk(
            project_id="project-a",
            chunk_id="snapshot-1:0000",
            source_id="source-1",
            snapshot_id="snapshot-1",
            text="Evidence",
            ordinal=0,
            heading_path=("Products", "Fasteners"),
            locator={"page_number": 2},
        )
        embedding = ChunkEmbedding(
            project_id="project-a",
            chunk_id=chunk.chunk_id,
            snapshot_id=chunk.snapshot_id,
            embedding_model="text-embedding-3-small",
            vector=vector(),
        )
        self.assertEqual(embedding.project_id, "project-a")
        self.assertEqual(
            require_project_scope(" project-a ", (chunk, embedding)), "project-a"
        )

        foreign_chunk = KnowledgeChunk(
            project_id="project-b",
            chunk_id="snapshot-2:0000",
            source_id="source-2",
            snapshot_id="snapshot-2",
            text="Foreign",
        )
        with self.assertRaisesRegex(ValueError, "requested project"):
            require_project_scope("project-a", (chunk, foreign_chunk))
        with self.assertRaisesRegex(ValueError, "non-negative"):
            KnowledgeChunk(
                project_id="project-a",
                chunk_id="chunk-1",
                source_id="source-1",
                snapshot_id="snapshot-1",
                text="Evidence",
                ordinal=-1,
            )
        with self.assertRaisesRegex(ValueError, "sequence of headings"):
            KnowledgeChunk(
                project_id="project-a",
                chunk_id="chunk-1",
                source_id="source-1",
                snapshot_id="snapshot-1",
                text="Evidence",
                heading_path="Products",  # type: ignore[arg-type]
            )

    def test_embedding_rejects_wrong_dimension_nonfinite_and_zero_vectors(self) -> None:
        invalid_vectors = (
            (1.0,),
            (float("nan"),) + (0.0,) * (EMBEDDING_DIMENSIONS - 1),
            (float("inf"),) + (0.0,) * (EMBEDDING_DIMENSIONS - 1),
            (0.0,) * EMBEDDING_DIMENSIONS,
        )

        for invalid_vector in invalid_vectors:
            with self.subTest(first=invalid_vector[0], length=len(invalid_vector)):
                with self.assertRaises(ValueError):
                    ChunkEmbedding(
                        project_id="project-a",
                        chunk_id="chunk-1",
                        snapshot_id="snapshot-1",
                        embedding_model="text-embedding-3-small",
                        vector=invalid_vector,
                    )


class KnowledgeAgentSettingsTests(unittest.TestCase):
    def test_disabled_feature_does_not_require_database_or_embedding_secrets(self) -> None:
        settings = load_knowledge_agent_settings(enabled=False, environ={})

        self.assertFalse(settings.enabled)
        self.assertFalse(settings.ready)
        self.assertEqual(settings.embedding_dimensions, EMBEDDING_DIMENSIONS)
        self.assertEqual(settings.embedding_model, "text-embedding-3-small")

    def test_enabled_loader_and_explicit_gate_require_runtime_configuration(self) -> None:
        incomplete = KnowledgeAgentSettings.from_env(environ={})
        with self.assertRaisesRegex(
            KnowledgeAgentConfigurationError, "ARTICLE_AGENT_DATABASE_URL"
        ):
            incomplete.require_ready()
        with self.assertRaises(KnowledgeAgentConfigurationError):
            load_knowledge_agent_settings(enabled=True, environ={})

    def test_reads_only_dedicated_embedding_variables(self) -> None:
        settings = KnowledgeAgentSettings.from_env(
            enabled=True,
            environ={
                "KNOWLEDGE_AGENT_ENABLED": "false",
                "LLM_BASE_URL": "https://llm.example/v1",
                "LLM_API_KEY": "llm-secret",
                "LLM_MODEL": "chat-model",
            },
        )

        self.assertTrue(settings.enabled)
        self.assertIsNone(settings.embedding_base_url)
        self.assertIsNone(settings.embedding_api_key)
        self.assertEqual(settings.embedding_model, "text-embedding-3-small")
        with self.assertRaises(KnowledgeAgentConfigurationError):
            settings.require_ready()

    def test_ready_settings_hide_secrets_from_repr_and_public_values(self) -> None:
        api_key = "unit-test-secret"
        database_password = "database-secret"
        settings = KnowledgeAgentSettings.from_env(
            enabled=True,
            environ={
                "ARTICLE_AGENT_DATABASE_URL": (
                    f"postgresql+psycopg://user:{database_password}"
                    "@127.0.0.1:55433/article_agent"
                ),
                "EMBEDDING_BASE_URL": "https://gateway.example/v1",
                "EMBEDDING_API_KEY": api_key,
                "EMBEDDING_MODEL": "text-embedding-3-small",
                "EMBEDDING_DIMENSIONS": "1536",
            },
        ).require_ready()

        self.assertTrue(settings.ready)
        self.assertNotIn(api_key, repr(settings))
        self.assertNotIn(database_password, repr(settings))
        self.assertNotIn(api_key, repr(settings.public_values()))
        self.assertNotIn("database_url", settings.public_values())
        self.assertNotIn("embedding_api_key", settings.public_values())

    def test_m1_rejects_any_other_dimension(self) -> None:
        with self.assertRaisesRegex(
            KnowledgeAgentConfigurationError, "must be 1536"
        ):
            KnowledgeAgentSettings.from_env(
                environ={"EMBEDDING_DIMENSIONS": "3072"}
            )

    def test_database_url_requires_the_psycopg3_sqlalchemy_driver(self) -> None:
        with self.assertRaisesRegex(
            KnowledgeAgentConfigurationError, "postgresql\\+psycopg"
        ):
            KnowledgeAgentSettings.from_env(
                environ={
                    "ARTICLE_AGENT_DATABASE_URL": (
                        "postgresql://user:password@127.0.0.1:55433/article_agent"
                    )
                }
            )


class M1ProtocolTests(unittest.TestCase):
    def test_exposes_repository_and_provider_boundaries(self) -> None:
        self.assertTrue(getattr(KnowledgeRepository, "_is_protocol", False))
        self.assertTrue(getattr(EvidencePackRepository, "_is_protocol", False))
        self.assertTrue(getattr(EmbeddingProvider, "_is_protocol", False))

        repository_methods = {
            "upsert_project",
            "upsert_source",
            "store_snapshot",
            "store_embeddings",
            "activate_snapshot",
            "get_chunks",
        }
        self.assertTrue(repository_methods.issubset(KnowledgeRepository.__dict__))
        self.assertNotIn("save_evidence_pack", KnowledgeRepository.__dict__)
        self.assertIn("save_evidence_pack", EvidencePackRepository.__dict__)


if __name__ == "__main__":
    unittest.main()
