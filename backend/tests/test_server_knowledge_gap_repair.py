from __future__ import annotations

import sys
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from knowledge_agent.contracts import RetrievalScope  # noqa: E402
from models import ArticleBrief, TaskRecord  # noqa: E402
from services.server_knowledge_gap_repair import (  # noqa: E402
    KnowledgeGap,
    _requirement_context,
    _targeted_query_variants,
)


class TargetedGapQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scope = RetrievalScope(
            project_id="example.com",
            retrieval_plan_id="plan-a",
            scope_id="scope-selection",
            ordinal=0,
            scope_type="h2_section",
            scope_key="selection",
            title="Select the right industrial enclosure",
            query_variants=("enclosure selection",),
            metadata={
                "claim_requirements": [
                    {
                        "requirement_id": "selection-req-01",
                        "h3_title": "Compare ingress protection and installation",
                        "claim_type": "selection_logic",
                        "query_variants": [
                            "IP rating installation environment",
                            "enclosure mounting conditions",
                        ],
                    },
                ],
            },
        )
        self.task = TaskRecord(
            id="task-a",
            week_folder="server",
            customer="example.com",
            topic_index=1,
            topic="Industrial enclosure selection",
            selected_title="Industrial Enclosure Selection Guide",
            task_dir="/server/task-a",
            article_brief=ArticleBrief(
                brief_id="brief-a",
                knowledge_snapshot_fingerprint="snapshot-a",
                required_capabilities=["IP protection"],
                selection_dimensions=["installation environment"],
            ),
            created_at="2026-08-21T00:00:00+00:00",
            updated_at="2026-08-21T00:00:00+00:00",
        )

    def test_gap_binds_to_h3_requirement_and_brief_context(self) -> None:
        requirement_ids, h3_titles, requirement_queries = _requirement_context(
            "Compare IP rating and installation environment before approval.",
            self.scope,
        )
        self.assertEqual(requirement_ids, ("selection-req-01",))
        self.assertEqual(
            h3_titles,
            ("Compare ingress protection and installation",),
        )
        self.assertIn("IP rating installation environment", requirement_queries)

        queries = _targeted_query_variants(
            sentence_text="Compare IP rating and installation environment before approval.",
            scope=self.scope,
            requirement_queries=requirement_queries,
            task=self.task,
        )
        self.assertEqual(
            queries[0],
            "Compare IP rating and installation environment before approval.",
        )
        self.assertIn("IP protection", queries)
        self.assertLessEqual(len(queries), 8)

    def test_gap_mapping_keeps_repair_identity_and_query_variants(self) -> None:
        gap = KnowledgeGap(
            gap_id="gap-a",
            sentence_id="sentence-a",
            sentence_hash="hash-a",
            text="A sentence with an unsupported claim.",
            claim_type="reference",
            hard_fact=False,
            scope_id="scope-selection",
            scope_title="Selection",
            requirement_ids=("selection-req-01",),
            h3_titles=("Compare ingress protection and installation",),
            query_variants=("sentence query", "requirement query"),
            article_brief_id="brief-a",
            knowledge_snapshot_fingerprint="snapshot-a",
        )
        payload = gap.to_mapping()
        self.assertEqual(payload["query"], "sentence query")
        self.assertEqual(payload["requirement_ids"], ["selection-req-01"])
        self.assertEqual(payload["article_brief_id"], "brief-a")
        self.assertEqual(
            payload["knowledge_snapshot_fingerprint"],
            "snapshot-a",
        )


if __name__ == "__main__":
    unittest.main()
