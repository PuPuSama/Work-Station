from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from config import load_config, public_config  # noqa: E402
from knowledge_agent import (  # noqa: E402
    EvidencePack,
    EvidencePackBuilder,
    EvidencePackRequest,
    KnowledgeChunk,
    KnowledgeRepository,
    KnowledgeRetriever,
    ResearchOrchestrator,
    RetrievalHit,
    RetrievalQuery,
    SourceDiscovery,
)


class FeatureFlagTests(unittest.TestCase):
    def test_knowledge_agent_is_disabled_by_default(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KNOWLEDGE_AGENT_ENABLED", None)
            config = load_config()

        self.assertFalse(config.knowledge_agent_enabled)
        self.assertEqual(
            public_config(config)["features"],
            {"knowledge_agent_enabled": False},
        )

    def test_environment_can_enable_the_feature(self) -> None:
        with patch.dict(os.environ, {"KNOWLEDGE_AGENT_ENABLED": "true"}):
            self.assertTrue(load_config().knowledge_agent_enabled)

    def test_invalid_environment_value_is_rejected(self) -> None:
        with patch.dict(os.environ, {"KNOWLEDGE_AGENT_ENABLED": "sometimes"}):
            with self.assertRaisesRegex(ValueError, "must be a boolean"):
                load_config()


class ContractTests(unittest.TestCase):
    def test_project_id_is_required_at_the_retrieval_boundary(self) -> None:
        with self.assertRaisesRegex(ValueError, "project_id is required"):
            RetrievalQuery(project_id=" ", text="wood screws")

    def test_evidence_pack_rejects_cross_project_hits(self) -> None:
        request = EvidencePackRequest(
            project_id="project-a",
            article_id="article-1",
            outline_version=1,
            scope_type="h2_section",
            scope_key="types",
            query_variants=("wood screw types",),
        )
        foreign_chunk = KnowledgeChunk(
            project_id="project-b",
            chunk_id="chunk-1",
            source_id="source-1",
            snapshot_id="snapshot-1",
            text="Evidence",
        )

        with self.assertRaisesRegex(ValueError, "same project"):
            EvidencePack(
                evidence_pack_id="pack-1",
                request=request,
                hits=(RetrievalHit(chunk=foreign_chunk, score=0.9),),
                sufficiency="sufficient",
            )


class ProtocolTests(unittest.TestCase):
    def test_m0_exposes_all_five_formal_boundaries(self) -> None:
        for boundary in (
            KnowledgeRepository,
            KnowledgeRetriever,
            SourceDiscovery,
            EvidencePackBuilder,
            ResearchOrchestrator,
        ):
            self.assertTrue(getattr(boundary, "_is_protocol", False), boundary.__name__)


if __name__ == "__main__":
    unittest.main()
