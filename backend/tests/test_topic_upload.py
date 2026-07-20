from __future__ import annotations

import sys
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from openpyxl import Workbook


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services.topics import TopicWorkbookError, store_topic_workbook  # noqa: E402


def workbook_bytes() -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Topic", "Keyword", "URL"])
    sheet.append(["Example topic", "example", "https://example.com/article"])
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


class TopicUploadTests(unittest.TestCase):
    def test_stores_valid_xlsx_using_only_the_uploaded_basename(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            library = Path(directory) / "topic-library"
            config = SimpleNamespace(topic_library=library)

            saved = store_topic_workbook(config, "../example.com.xlsx", workbook_bytes())

            self.assertEqual(saved, library / "example.com.xlsx")
            self.assertTrue(saved.exists())

    def test_rejects_non_xlsx_and_invalid_workbook_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = SimpleNamespace(topic_library=Path(directory))

            with self.assertRaises(TopicWorkbookError):
                store_topic_workbook(config, "topics.csv", b"topic")
            with self.assertRaises(TopicWorkbookError):
                store_topic_workbook(config, "topics.xlsx", b"not a workbook")


if __name__ == "__main__":
    unittest.main()
