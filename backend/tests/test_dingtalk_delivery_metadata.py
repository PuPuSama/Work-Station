"""Check the distributed Skill against the real delivery exporter, using no services."""
from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from models import AICheck, KnowledgeCoverageCheck, TaskRecord, TdkMetadata
from services.delivery_metadata import build_delivery_metadata


SCRIPT = BACKEND_DIR.parent / "skills/dingtalk-sheet-fill/scripts/metadata_to_patch.py"
SPEC = importlib.util.spec_from_file_location("dingtalk_delivery_parser", SCRIPT)
assert SPEC and SPEC.loader
PARSER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PARSER)


class DingTalkDeliveryMetadataTests(unittest.TestCase):
    def setUp(self):
        self.task = TaskRecord(
            id="tsk_demo_001", week_folder="server", customer="example.com",
            topic_index=1, topic="Workshop pump selection", task_dir="/test/task",
            tdk=TdkMetadata(keywords=["workshop pump"]),
            created_at="2026-09-09T00:00:00+00:00",
            updated_at="2026-09-09T00:00:00+00:00",
            final_ai_check=AICheck(score=20, confirmed=True),
            knowledge_coverage=KnowledgeCoverageCheck(
                status="available", sentence_coverage=0.16,
                supported_sentences=4, eligible_sentences=25,
            ),
        )
        self.article = (
            "# Workshop Pump Selection\n\n"
            "Choose a workshop pump after reviewing the workshop pump pressure range. "
            "A workshop pump needs regular maintenance.\n\n"
            "Read the [pump guide](https://example.com/pumps) and "
            "[maintenance checklist](https://example.com/maintenance)."
        )

    def metadata(self):
        return json.loads(build_delivery_metadata(
            self.task, article=self.article, project_id="example.com",
            delivery_filename="example-001.zip",
        ))

    def test_current_exporter_produces_the_expected_sheet_values(self):
        data = self.metadata()
        patch = PARSER.build_patch(data, Path("metadata.json"))
        self.assertEqual(patch["project_id"], "example.com")
        self.assertEqual(patch["cells"]["A"]["value"], "9.9")
        self.assertEqual(patch["cells"]["G"]["value"], "Workshop Pump Selection")
        self.assertEqual(patch["cells"]["E"]["value"], 4)
        self.assertEqual(patch["cells"]["H"]["value"], 20)
        self.assertEqual(patch["cells"]["K"]["value"], 16)
        self.assertEqual(patch["cells"]["I"]["value"], "pump guide\nmaintenance checklist")

    def test_exported_automatic_ai_result_does_not_bypass_confirmation(self):
        self.task.final_ai_check.confirmed = False
        self.task.final_ai_check.provider = "automatic-test"
        with self.assertRaises(PARSER.MetadataError):
            PARSER.build_patch(self.metadata(), Path("metadata.json"))

    def test_exported_stale_citation_does_not_bypass_availability(self):
        self.task.knowledge_coverage.status = "stale"
        with self.assertRaises(PARSER.MetadataError):
            PARSER.build_patch(self.metadata(), Path("metadata.json"))


if __name__ == "__main__":
    unittest.main()
