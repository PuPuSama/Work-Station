from __future__ import annotations

import sys
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services.server_snapshot_evidence import _block_texts  # noqa: E402


class SnapshotEvidencePreviewTextTests(unittest.TestCase):
    def test_table_rows_render_as_a_preview_table(self) -> None:
        title, blocks = _block_texts(
            {
                "title": "Specification",
                "blocks": [
                    {
                        "kind": "table_row",
                        "metadata": {
                            "table_id": "table-1",
                            "table_cells": ["Model", "Surge Power"],
                            "table_is_header": True,
                        },
                    },
                    {
                        "kind": "table_row",
                        "metadata": {
                            "table_id": "table-1",
                            "table_cells": ["8000W", "16000VA"],
                            "table_is_header": False,
                        },
                    },
                ],
            }
        )

        self.assertEqual(title, "Specification")
        self.assertIn("| Model | Surge Power |", blocks[0])
        self.assertIn("| 8000W | 16000VA |", blocks[0])

    def test_matrix_preview_shows_inferred_model_columns(self) -> None:
        _, blocks = _block_texts(
            {
                "blocks": [
                    {
                        "kind": "table_row",
                        "metadata": {
                            "table_id": "matrix-1",
                            "table_cells": [
                                "Technical Specification",
                                "REVO HESS series",
                                "REVO HESS series",
                            ],
                            "table_headers": [
                                "Technical Specification",
                                "6000VA/6000W",
                                "8000VA/8000W",
                            ],
                        },
                    },
                    {
                        "kind": "table_row",
                        "metadata": {
                            "table_id": "matrix-1",
                            "table_cells": ["Surge Power", "12000VA", "16000VA"],
                            "table_headers": [
                                "Technical Specification",
                                "6000VA/6000W",
                                "8000VA/8000W",
                            ],
                        },
                    },
                ],
            }
        )
        self.assertIn("| Technical Specification | 6000VA/6000W | 8000VA/8000W |", blocks[0])
        self.assertIn("| Surge Power | 12000VA | 16000VA |", blocks[0])


if __name__ == "__main__":
    unittest.main()
