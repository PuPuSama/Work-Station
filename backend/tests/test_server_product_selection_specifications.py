from __future__ import annotations

import sys
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services.server_product_selection import _specifications  # noqa: E402
from models import Product  # noqa: E402
from services.generator import products_for_prompt  # noqa: E402


class ProductSelectionSpecificationTests(unittest.TestCase):
    def test_multi_model_table_keeps_each_model_label(self) -> None:
        specifications = _specifications(
            {
                "specification_tables": [
                    {
                        "headers": [
                            "Technical specification",
                            "REVO HESS 6000W",
                            "REVO HESS 8000W",
                        ],
                        "rows": [
                            ["Rated Power", "6000W", "8000W"],
                            ["Surge Power", "12000VA", "16000VA"],
                        ],
                    }
                ]
            }
        )

        self.assertEqual(
            specifications["Surge Power [REVO HESS 8000W]"],
            "16000VA",
        )
        self.assertEqual(
            specifications["Surge Power [REVO HESS 6000W]"],
            "12000VA",
        )

    def test_rated_power_row_can_supply_model_labels(self) -> None:
        specifications = _specifications(
            {
                "specification_tables": [
                    {
                        "headers": ["Specification", "Value", "Value"],
                        "rows": [
                            ["Rated Power", "6000VA/6000W", "8000VA/8000W"],
                            ["Surge Power", "12000VA", "16000VA"],
                        ],
                    }
                ]
            }
        )

        self.assertEqual(
            specifications["Surge Power [8000VA/8000W]"],
            "16000VA",
        )

    def test_prompt_marks_manual_specifications_as_authoritative(self) -> None:
        context = products_for_prompt(
            [
                Product(
                    product_id="revo-hess",
                    name="REVO HESS",
                    canonical_url="https://example.com/revo-hess",
                    specifications={
                        "Surge Power [8000W]": "16000VA",
                    },
                    specifications_overridden=True,
                )
            ]
        )

        self.assertIn("Operator-corrected specifications", context)
        self.assertIn("Surge Power [8000W]: 16000VA", context)


if __name__ == "__main__":
    unittest.main()
