from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from models import TaskRecord, TdkMetadata  # noqa: E402
from services.delivery_metadata import build_delivery_metadata  # noqa: E402


class DeliveryMetadataTests(unittest.TestCase):
    def test_primary_keyword_uses_approximate_variants_when_exact_count_is_low(self) -> None:
        task = TaskRecord(
            id="metadata-task",
            week_folder="server",
            customer="example.com",
            topic_index=1,
            topic="PET preform manufacturing",
            task_dir="/server/metadata-task",
            tdk=TdkMetadata(
                keywords=["PET preform manufacturing", "PET preform mold"]
            ),
            created_at="2026-08-31T00:00:00+00:00",
            updated_at="2026-08-31T00:00:00+00:00",
        )
        article = (
            "# PET Preform Manufacturing\n\n"
            "PET preform production depends on controlled material preparation.\n"
            "The PET preform production line must be reviewed before ordering."
        )

        payload = json.loads(
            build_delivery_metadata(
                task,
                article=article,
                project_id="example.com",
                delivery_filename="example.com-topic_001.zip",
            )
        )

        density = payload["keyword_density"][0]
        self.assertEqual(payload["schema_version"], 2)
        self.assertEqual(payload["primary_keyword"], "PET preform manufacturing")
        self.assertEqual(density["exact_occurrences"], 1)
        self.assertEqual(density["approximate_occurrences"], 3)
        self.assertEqual(density["occurrences"], 3)
        self.assertEqual(density["match_mode"], "approximate")
        self.assertGreater(density["density_percent"], 0)
        self.assertEqual(payload["keyword_density"][1]["match_mode"], "exact")
        self.assertEqual(payload["keyword_density"][1]["approximate_occurrences"], 0)


if __name__ == "__main__":
    unittest.main()
