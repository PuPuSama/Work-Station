from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from knowledge_agent.ingestion import (  # noqa: E402
    DocumentInput,
    MinerUContentListAdapter,
    ParsedBlock,
    ParsedDocument,
    ParserQualityExpectation,
    compare_parsers,
)


class StaticParser:
    def __init__(self, name: str, *, with_table: bool) -> None:
        self.name = name
        self.with_table = with_table

    def parse(self, document_input: DocumentInput) -> ParsedDocument:
        blocks = [
            ParsedBlock(
                kind="heading",
                ordinal=0,
                text="Wood Screw Specification",
                heading_path=("Wood Screw Specification",),
                locator={"page_number": 1},
            ),
            ParsedBlock(
                kind="paragraph",
                ordinal=1,
                text="Material: carbon steel",
                heading_path=("Wood Screw Specification",),
                locator={"page_number": 1},
            ),
        ]
        if self.with_table:
            blocks.append(
                ParsedBlock(
                    kind="table_row",
                    ordinal=2,
                    text="Diameter | 4 mm",
                    heading_path=("Wood Screw Specification",),
                    locator={"page_number": 2, "bbox_0_1000": [1, 2, 3, 4]},
                )
            )
        return ParsedDocument(
            filename=document_input.filename,
            content_type=document_input.content_type or "application/pdf",
            content_hash=document_input.content_hash,
            parser_name=self.name,
            parser_version="1",
            blocks=tuple(blocks),
        )


class MinerUContentListAdapterTests(unittest.TestCase):
    def test_adapter_preserves_reading_order_page_bbox_table_and_image(self) -> None:
        original = DocumentInput(
            filename="spec.pdf",
            content=b"%PDF representative fixture",
            content_type="application/pdf",
        )
        content_list = json.dumps(
            [
                {
                    "type": "header",
                    "text": "Confidential header",
                    "page_idx": 0,
                    "bbox": [0, 0, 1000, 30],
                },
                {
                    "type": "text",
                    "text": "Wood Screw Specification",
                    "text_level": 1,
                    "page_idx": 0,
                    "bbox": [50, 80, 900, 130],
                },
                {
                    "type": "text",
                    "text": "Material: carbon steel",
                    "page_idx": 0,
                    "bbox": [50, 150, 900, 210],
                },
                {
                    "type": "table",
                    "table_body": (
                        "<table><tr><th>Diameter</th><th>Length</th></tr>"
                        "<tr><td>4 mm</td><td>50 mm</td></tr></table>"
                    ),
                    "page_idx": 1,
                    "bbox": [40, 100, 950, 600],
                    "img_path": "images/spec-table.png",
                },
            ]
        ).encode()

        parsed = MinerUContentListAdapter().normalize(
            document_input=original,
            content_list=content_list,
            mineru_version="3.0.0",
            assets={"images/spec-table.png": b"fake image bytes"},
        )

        self.assertEqual(parsed.parser_name, "mineru-content-list")
        self.assertEqual(parsed.parser_version, "3.0.0/adapter-1")
        self.assertEqual(parsed.title, "Wood Screw Specification")
        self.assertEqual(
            [block.kind for block in parsed.blocks],
            ["heading", "paragraph", "table_row", "table_row"],
        )
        self.assertEqual(parsed.blocks[2].text, "Diameter | Length")
        self.assertEqual(parsed.blocks[2].locator["page_number"], 2)
        self.assertEqual(
            parsed.blocks[2].locator["bbox_0_1000"],
            [40.0, 100.0, 950.0, 600.0],
        )
        self.assertEqual(len(parsed.assets), 1)
        self.assertEqual(parsed.assets[0].filename, "spec-table.png")
        self.assertNotIn("Confidential header", parsed.text)

    def test_adapter_reports_invalid_contract_without_dumping_input(self) -> None:
        original = DocumentInput(filename="private.pdf", content=b"private bytes")
        with self.assertRaisesRegex(Exception, "must be a list") as raised:
            MinerUContentListAdapter().normalize(
                document_input=original,
                content_list=b'{"secret":"must not appear"}',
                mineru_version="3.0.0",
            )
        self.assertNotIn("secret", str(raised.exception))


class ParserBenchmarkTests(unittest.TestCase):
    def test_comparison_uses_same_hash_and_records_quality_and_resource_facts(
        self,
    ) -> None:
        document = DocumentInput(
            filename="representative.pdf",
            content=b"same input bytes for every parser",
            content_type="application/pdf",
        )

        report = compare_parsers(
            document_input=document,
            parsers={
                "lightweight": StaticParser("pdf-lightweight", with_table=False),
                "mineru": StaticParser("mineru-content-list", with_table=True),
            },
            expectation=ParserQualityExpectation(
                required_text=("carbon steel", "Diameter"),
                minimum_blocks=2,
                minimum_headings=1,
                minimum_table_rows=1,
            ),
            repeat_count=2,
        )

        self.assertEqual(report.content_hash, document.content_hash)
        self.assertEqual(report.repeat_count, 2)
        by_name = {item.parser_name: item for item in report.observations}
        self.assertFalse(by_name["pdf-lightweight"].minimums_passed)
        self.assertEqual(
            by_name["pdf-lightweight"].required_text_recall,
            0.5,
        )
        self.assertTrue(by_name["mineru-content-list"].minimums_passed)
        self.assertEqual(by_name["mineru-content-list"].located_page_count, 2)
        self.assertEqual(by_name["mineru-content-list"].bbox_block_count, 1)
        self.assertGreaterEqual(
            by_name["mineru-content-list"].peak_python_bytes_max,
            0,
        )


if __name__ == "__main__":
    unittest.main()
