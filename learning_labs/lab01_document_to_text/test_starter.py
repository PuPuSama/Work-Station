"""Tests define the public behavior of Lab 01."""

import unittest

from learning_labs.lab01_document_to_text.starter import (
    ParsedDocument,
    extract_title,
    normalize_text,
    parse_text_document,
)


class NormalizeTextTests(unittest.TestCase):
    def test_normalizes_line_endings_and_spacing(self) -> None:
        source = "  # Guide  \r\n\r\n\r\n  First line  \rSecond line\t "

        self.assertEqual(
            normalize_text(source),
            "# Guide\n\nFirst line\nSecond line",
        )

    def test_removes_outer_blank_lines(self) -> None:
        self.assertEqual(normalize_text("\n\n  one  \n\n"), "one")

    def test_empty_text_stays_empty(self) -> None:
        self.assertEqual(normalize_text(" \r\n\t\n"), "")

    def test_rejects_non_string_input(self) -> None:
        with self.assertRaises(TypeError):
            normalize_text(None)  # type: ignore[arg-type]


class ExtractTitleTests(unittest.TestCase):
    def test_uses_first_h1(self) -> None:
        text = "Intro\n## Details\n# Product Guide\nBody"
        self.assertEqual(extract_title(text, "folder/source.md"), "Product Guide")

    def test_uses_filename_when_h1_is_missing(self) -> None:
        self.assertEqual(
            extract_title("## Details\nBody", "folder/wood-screws.md"),
            "wood-screws",
        )


class ParseTextDocumentTests(unittest.TestCase):
    def test_builds_normalized_document_and_metadata(self) -> None:
        document = parse_text_document("# Guide\r\n\r\nBody  ", "docs/guide.md")

        self.assertIsInstance(document, ParsedDocument)
        self.assertEqual(document.source_id, "docs/guide.md")
        self.assertEqual(document.title, "Guide")
        self.assertEqual(document.text, "# Guide\n\nBody")
        self.assertEqual(document.metadata["char_count"], len(document.text))
        self.assertEqual(document.metadata["line_count"], 3)


if __name__ == "__main__":
    unittest.main()

