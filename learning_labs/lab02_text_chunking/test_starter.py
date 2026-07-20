import unittest

from learning_labs.lab02_text_chunking.starter import (
    MarkdownSection,
    build_chunks,
    split_markdown_sections,
)


class SplitMarkdownSectionsTests(unittest.TestCase):
    def test_preserves_heading_path(self) -> None:
        text = (
            "# Fasteners\nIntro\n\n"
            "## Screws\nScrew overview.\n\n"
            "### Woodscrews\nFor wood assembly."
        )

        sections = split_markdown_sections(text)

        self.assertEqual(
            sections,
            [
                MarkdownSection(("Fasteners",), "Intro"),
                MarkdownSection(("Fasteners", "Screws"), "Screw overview."),
                MarkdownSection(
                    ("Fasteners", "Screws", "Woodscrews"),
                    "For wood assembly.",
                ),
            ],
        )


class BuildChunksTests(unittest.TestCase):
    def test_keeps_short_section_as_one_chunk(self) -> None:
        chunks = build_chunks(
            [MarkdownSection(("Fasteners", "Screws"), "One paragraph.")],
            source_id="product.md",
            max_chars=100,
        )

        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].chunk_id, "product.md:0000")
        self.assertEqual(chunks[0].heading_path, ("Fasteners", "Screws"))
        self.assertEqual(chunks[0].text, "One paragraph.")
        self.assertEqual(chunks[0].position, 0)

    def test_rejects_non_positive_max_chars(self) -> None:
        with self.assertRaises(ValueError):
            build_chunks([], source_id="product.md", max_chars=0)


if __name__ == "__main__":
    unittest.main()

