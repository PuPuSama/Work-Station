from __future__ import annotations

import json
from io import BytesIO
import sys
import unittest
from pathlib import Path
from zipfile import ZipFile

import httpx


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from knowledge_agent.ingestion import (  # noqa: E402
    DocumentInput,
    MinerUDocumentParser,
    MinerUContentListAdapter,
    MinerUSettings,
    ParsedBlock,
    ParsedDocument,
    ParserQualityExpectation,
    compare_parsers,
)
from knowledge_agent.ingestion.mineru import (  # noqa: E402
    document_parser_router_from_environment,
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
        self.assertEqual(
            parsed.blocks[3].text,
            "4 mm | 50 mm || Diameter: 4 mm; Length: 50 mm",
        )
        self.assertEqual(
            parsed.blocks[3].metadata["table_cells"],
            ["4 mm", "50 mm"],
        )
        self.assertEqual(parsed.blocks[3].metadata["table_id"], "mineru-table-3")
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

    def test_table_row_keeps_multi_model_column_mapping(self) -> None:
        original = DocumentInput(filename="matrix.pdf", content=b"%PDF matrix")
        parsed = MinerUContentListAdapter().normalize(
            document_input=original,
            content_list=json.dumps(
                [
                    {
                        "type": "table",
                        "table_body": (
                            "<table>"
                            "<tr><th>Technical Specification</th>"
                            "<th colspan='2'>REVO HESS series</th></tr>"
                            "<tr><th>Parameter</th><th>6000VA/6000W</th>"
                            "<th>8000VA/8000W</th></tr>"
                            "<tr><td>Surge Power</td><td>12000VA</td>"
                            "<td>16000VA</td></tr>"
                            "</table>"
                        ),
                        "page_idx": 0,
                    }
                ]
            ).encode(),
            mineru_version="3.0.0",
        )

        row = parsed.blocks[-1]
        self.assertIn("6000VA/6000W: 12000VA", row.text)
        self.assertIn("8000VA/8000W: 16000VA", row.text)
        self.assertEqual(row.metadata["table_cells"], ["Surge Power", "12000VA", "16000VA"])

    def test_table_without_th_keeps_matrix_model_context(self) -> None:
        original = DocumentInput(filename="matrix.html", content=b"table")
        parsed = MinerUContentListAdapter().normalize(
            document_input=original,
            content_list=json.dumps(
                [
                    {
                        "type": "table",
                        "table_body": (
                            "<table>"
                            "<tr><td>Technical Specification</td>"
                            "<td colspan='2'>REVO HESS series</td></tr>"
                            "<tr><td>Rated Power</td><td>6000VA/6000W</td>"
                            "<td>8000VA/8000W</td></tr>"
                            "<tr><td>Surge Power</td><td>12000VA</td>"
                            "<td>16000VA</td></tr>"
                            "</table>"
                        ),
                        "page_idx": 1,
                    }
                ]
            ).encode(),
            mineru_version="3.0.0",
        )

        row = parsed.blocks[-1]
        self.assertIn("6000VA/6000W: 12000VA", row.text)
        self.assertIn("8000VA/8000W: 16000VA", row.text)
        self.assertEqual(
            row.metadata["table_headers"],
            ["Technical Specification", "6000VA/6000W", "8000VA/8000W"],
        )


def mineru_archive() -> bytes:
    output = BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr(
            "spec/auto/spec_content_list.json",
            json.dumps(
                [
                    {
                        "type": "text",
                        "text": "DIN 985 Specification",
                        "text_level": 1,
                        "page_idx": 0,
                    },
                    {
                        "type": "image",
                        "image_caption": "Product dimensions",
                        "img_path": "images/din985.png",
                        "page_idx": 0,
                    },
                ]
            ),
        )
        archive.writestr("spec/auto/images/din985.png", b"image bytes")
    return output.getvalue()


class MinerUDocumentParserTests(unittest.TestCase):
    def test_precision_api_upload_poll_and_zip_normalization(self) -> None:
        requests: list[httpx.Request] = []
        archive = mineru_archive()

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path == "/api/v4/file-urls/batch":
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "batch_id": "batch-1",
                            "file_urls": ["https://upload.test/signed"],
                        },
                    },
                )
            if request.url == httpx.URL("https://upload.test/signed"):
                self.assertEqual(request.method, "PUT")
                self.assertEqual(request.content, b"%PDF fixture")
                return httpx.Response(200)
            if request.url.path == "/api/v4/extract-results/batch/batch-1":
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "batch_id": "batch-1",
                            "extract_result": [
                                {
                                    "file_name": "spec.pdf",
                                    "data_id": document.content_hash,
                                    "state": "done",
                                    "full_zip_url": "https://download.test/result.zip",
                                }
                            ],
                        },
                    },
                )
            if request.url == httpx.URL("https://download.test/result.zip"):
                return httpx.Response(200, content=archive)
            return httpx.Response(404)

        document = DocumentInput(
            filename="spec.pdf",
            content=b"%PDF fixture",
            content_type="application/pdf",
        )
        client = httpx.Client(transport=httpx.MockTransport(handler))
        parser = MinerUDocumentParser(
            MinerUSettings(
                api_key="mineru-secret",
                base_url="https://mineru.test",
                timeout_seconds=5,
                poll_interval_seconds=0.01,
            ),
            client=client,
            sleeper=lambda _seconds: None,
        )

        parsed = parser.parse(document)

        self.assertEqual(parsed.parser_name, "mineru-content-list")
        self.assertEqual(parsed.parser_version, "api-v4-vlm/adapter-1")
        self.assertIn("DIN 985 Specification", parsed.text)
        self.assertEqual(len(parsed.assets), 1)
        submit = requests[0]
        self.assertEqual(submit.headers["Authorization"], "Bearer mineru-secret")
        self.assertEqual(json.loads(submit.content)["model_version"], "vlm")
        self.assertNotIn(
            "Authorization",
            requests[-1].headers,
        )

    def test_api_error_does_not_expose_api_key(self) -> None:
        client = httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    401,
                    text="mineru-secret must not be repeated",
                )
            )
        )
        parser = MinerUDocumentParser(
            MinerUSettings(
                api_key="mineru-secret",
                base_url="https://mineru.test",
            ),
            client=client,
        )
        with self.assertRaises(Exception) as raised:
            parser.parse(
                DocumentInput(filename="private.pdf", content=b"%PDF")
            )
        self.assertNotIn("mineru-secret", str(raised.exception))

    def test_environment_router_keeps_docx_local_when_mineru_key_exists(self) -> None:
        local = document_parser_router_from_environment({})
        remote = document_parser_router_from_environment(
            {"ARTICLE_AGENT_MINERU_API_KEY": "token"}
        )
        document = DocumentInput(filename="spec.pdf", content=b"%PDF")
        docx = DocumentInput(filename="spec.docx", content=b"PK fixture")
        self.assertEqual(local.select(document).name, "pypdf-lightweight")
        self.assertEqual(remote.select(document).name, "mineru-content-list")
        self.assertEqual(local.select(docx).name, "docx-lightweight")
        self.assertEqual(remote.select(docx).name, "docx-lightweight")
        local.close()
        remote.close()

    def test_archive_rejects_parent_path_without_writing_files(self) -> None:
        output = BytesIO()
        with ZipFile(output, "w") as archive:
            archive.writestr(
                "../private_content_list.json",
                json.dumps([{"type": "text", "text": "unsafe"}]),
            )
        parser = MinerUDocumentParser(
            MinerUSettings(
                api_key="mineru-secret",
                base_url="https://mineru.test",
            ),
            client=httpx.Client(
                transport=httpx.MockTransport(
                    lambda _request: httpx.Response(500)
                )
            ),
        )
        with self.assertRaisesRegex(Exception, "unsafe"):
            parser._read_archive(output.getvalue())

    def test_settings_repr_does_not_expose_key(self) -> None:
        settings = MinerUSettings(api_key="mineru-secret")
        self.assertNotIn("mineru-secret", repr(settings))


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
