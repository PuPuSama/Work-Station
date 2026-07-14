from __future__ import annotations

import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree

from docx import Document
from PIL import Image


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services.article_images import (  # noqa: E402
    ArticleImageError,
    ImageAnchorRequiredError,
    prepare_task_images,
)
from services.docx_export import export_task_docx  # noqa: E402


WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

FAQ_BLOCK = """## FAQ

**Q: What should buyers check first?**

A: Buyers should check the application requirements first.

**Q: When should buyers request a sample?**

A: Buyers should request one before approval when fit matters.

**Q: Why should buyers compare suppliers?**

A: Buyers should compare capability, quality control, delivery, and support.
"""


def make_png(path: Path, color: tuple[int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (180, 108), color).save(path, format="PNG")


def test_config() -> SimpleNamespace:
    return SimpleNamespace(
        docx_font="Arial",
        title_1_size=20.0,
        title_2_size=16.0,
        title_3_size=13.0,
        body_size=11.0,
    )


def paragraph_text(paragraph) -> str:
    return "".join(node.text or "" for node in paragraph._p.iter() if node.tag == f"{{{WORD_NS}}}t")


class DocxExportTests(unittest.TestCase):
    def test_images_are_inline_marked_and_links_are_real_and_bold(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task_dir = Path(temporary) / "task"
            hero = task_dir / "incoming" / "hero.png"
            product_image = task_dir / "incoming" / "product.png"
            make_png(hero, (30, 60, 90))
            make_png(product_image, (90, 120, 150))
            article = """# Inline Image Export

Read the [Company Website](https://company.example/) before comparing options.

## First Section

This section introduces the decision.

## Product Section

### Product Alpha

Choose [Product Alpha](https://example.com/product-alpha) for this application.

## FAQ

**Q: What should buyers check first?**

A: Buyers should check the application requirements first.

**Q: When should buyers request a sample?**

A: Buyers should request one before approval when fit matters.

**Q: Why should buyers compare suppliers?**

A: Buyers should compare capability, quality control, delivery, and support.
"""
            product = SimpleNamespace(
                name="Product Alpha",
                url="https://example.com/product-alpha",
                image_path=str(product_image),
                description="",
            )
            task = SimpleNamespace(
                task_dir=str(task_dir),
                selected_title="Inline Image Export",
                topic="Topic",
                article=article,
                final_article="",
                linked_article="",
                humanized_article="",
                initial_article="",
                hero_image=str(hero),
                products=[product],
                images=[],
            )
            task.images = prepare_task_images(task, require_hero=True)

            output = export_task_docx(test_config(), task)
            exported = Document(output)
            paragraphs = exported.paragraphs
            display = ["[IMAGE]" if paragraph._p.xpath(".//w:drawing") else paragraph_text(paragraph) for paragraph in paragraphs]

            h1_index = display.index("Inline Image Export")
            transition_index = next(index for index, value in enumerate(display) if value.startswith("Read the Company"))
            hero_index = display.index("[IMAGE]")
            hero_marker_index = display.index("img.Inline Image Export.webp")
            first_h2_index = display.index("First Section")
            self.assertLess(h1_index, transition_index)
            self.assertLess(transition_index, hero_index)
            self.assertEqual(hero_marker_index, hero_index + 1)
            self.assertEqual(first_h2_index, hero_marker_index + 1)

            product_paragraph_index = next(
                index for index, value in enumerate(display) if value.startswith("Choose Product Alpha")
            )
            self.assertEqual(display[product_paragraph_index + 1], "[IMAGE]")
            self.assertEqual(display[product_paragraph_index + 2], "img.Product Alpha.webp")
            self.assertNotIn("Recommended Products", display)
            faq_index = display.index("FAQ")
            self.assertGreater(faq_index, product_paragraph_index)
            self.assertEqual(display[faq_index + 1], "Q: What should buyers check first?")
            self.assertNotIn("**", "\n".join(display))
            question_paragraph = paragraphs[faq_index + 1]
            self.assertTrue(question_paragraph.runs)
            self.assertTrue(all(run.bold for run in question_paragraph.runs if run.text))

            image_indices = [index for index, value in enumerate(display) if value == "[IMAGE]"]
            self.assertEqual(len(image_indices), 2)
            self.assertTrue(all(display[index + 1].startswith("img.") for index in image_indices))

            with zipfile.ZipFile(output) as archive:
                media = [name for name in archive.namelist() if name.startswith("word/media/")]
                self.assertEqual(len(media), 2)
                self.assertTrue(all(name.casefold().endswith(".webp") for name in media))
                content_types = archive.read("[Content_Types].xml").decode("utf-8")
                self.assertIn("image/webp", content_types)

                document_xml = ElementTree.fromstring(archive.read("word/document.xml"))
                for run in document_xml.findall(f".//{{{WORD_NS}}}r"):
                    fonts = run.find(f"{{{WORD_NS}}}rPr/{{{WORD_NS}}}rFonts")
                    self.assertIsNotNone(fonts)
                    self.assertEqual(
                        fonts.attrib[f"{{{WORD_NS}}}ascii"],
                        "Times New Roman",
                    )
                hyperlinks = document_xml.findall(f".//{{{WORD_NS}}}hyperlink")
                self.assertEqual(len(hyperlinks), 2)
                for hyperlink in hyperlinks:
                    self.assertIsNotNone(hyperlink.find(f".//{{{WORD_NS}}}b"))

                relationships = ElementTree.fromstring(
                    archive.read("word/_rels/document.xml.rels")
                )
                external_targets = {
                    relationship.attrib.get("Target")
                    for relationship in relationships.findall(f"{{{REL_NS}}}Relationship")
                    if relationship.attrib.get("TargetMode") == "External"
                }
                self.assertEqual(
                    external_targets,
                    {"https://company.example/", "https://example.com/product-alpha"},
                )

                heading_colors = []
                for paragraph in document_xml.findall(f".//{{{WORD_NS}}}p"):
                    style = paragraph.find(f"{{{WORD_NS}}}pPr/{{{WORD_NS}}}pStyle")
                    if style is None or style.attrib.get(f"{{{WORD_NS}}}val") not in {
                        "Heading1",
                        "Heading2",
                        "Heading3",
                    }:
                        continue
                    heading_colors.extend(
                        color.attrib.get(f"{{{WORD_NS}}}val")
                        for color in paragraph.findall(
                            f".//{{{WORD_NS}}}rPr/{{{WORD_NS}}}color"
                        )
                    )
                self.assertTrue(heading_colors)
                self.assertEqual(set(heading_colors), {"000000"})

    def test_hero_export_rejects_missing_transition(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task_dir = Path(temporary) / "task"
            hero = task_dir / "hero.png"
            make_png(hero, (20, 20, 20))
            task = SimpleNamespace(
                task_dir=str(task_dir),
                selected_title="No Transition",
                topic="Topic",
                article=(
                    "# No Transition\n\n## First Section\n\nBody.\n\n" + FAQ_BLOCK
                ),
                hero_image=str(hero),
                products=[],
                images=[],
            )
            task.images = prepare_task_images(task, require_hero=True)

            with self.assertRaisesRegex(ArticleImageError, "过渡段"):
                export_task_docx(test_config(), task)

    def test_unresolved_product_image_requires_explicit_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task_dir = Path(temporary) / "task"
            hero = task_dir / "hero.png"
            product_image = task_dir / "product.png"
            make_png(hero, (20, 40, 60))
            make_png(product_image, (70, 90, 110))
            task = SimpleNamespace(
                task_dir=str(task_dir),
                selected_title="Anchor Required",
                topic="Topic",
                article=(
                    "# Anchor Required\n\nA transition paragraph.\n\n"
                    "## First Section\n\nBody only.\n\n" + FAQ_BLOCK
                ),
                hero_image=str(hero),
                products=[
                    SimpleNamespace(
                        name="Missing Product",
                        url="https://example.com/missing",
                        image_path=str(product_image),
                    )
                ],
                images=[],
            )
            task.images = prepare_task_images(task, require_hero=True)

            with self.assertRaises(ImageAnchorRequiredError) as raised:
                export_task_docx(test_config(), task)
            self.assertEqual(
                [candidate["heading"] for candidate in raised.exception.unresolved[0]["anchor_candidates"]],
                ["First Section"],
            )


if __name__ == "__main__":
    unittest.main()
