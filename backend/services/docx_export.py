from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.oxml.shape import CT_Inline
from docx.opc.constants import RELATIONSHIP_TYPE
from docx.opc.part import Part
from docx.shared import Inches, Pt, RGBColor

from config import AppConfig
from models import TaskRecord
from services.article_images import (
    ImagePlacement,
    image_pixel_size,
    prepare_task_images,
    resolve_image_placements,
    sanitize_image_stem,
)
from services.article_validation import validate_article_layout


MARKDOWN_LINK_PATTERN = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
URL_PATTERN = re.compile(r"(https?://[^\s)]+)")
FAQ_BOLD_QUESTION_PATTERN = re.compile(r"^\s*\*\*(Q:\s+.+)\*\*\s*$")
WORD_FONT_NAME = "Times New Roman"
BLACK_HEX = "000000"


def _value(item: object, *names: str, default: Any = "") -> Any:
    for name in names:
        if isinstance(item, Mapping) and name in item:
            value = item[name]
        else:
            value = getattr(item, name, None)
        if value is not None and value != "":
            return value
    return default


def safe_filename(value: str) -> str:
    return sanitize_image_stem(value, fallback="article", max_length=120)


def _current_article(task: object, title: str) -> str:
    article = str(
        _value(
            task,
            "final_article",
            "linked_article",
            "humanized_article",
            "initial_article",
            "article",
            default="",
        )
        or ""
    )
    return article or f"# {title}\n\n"


def _has_image_sources(task: object) -> bool:
    if str(_value(task, "hero_image", default="") or "").strip():
        return True
    if list(_value(task, "images", default=[]) or []):
        return True
    return any(
        str(_value(product, "image_path", "source_path", default="") or "").strip()
        for product in list(_value(task, "products", default=[]) or [])
    )


def _prepared_images(task: object, markdown: str) -> list[object]:
    existing = list(_value(task, "images", default=[]) or [])
    if existing:
        paths = [
            str(_value(image, "prepared_path", "webp_path", default="") or "").strip()
            for image in existing
        ]
        has_hero = any(
            str(_value(image, "role", "kind", "type", default="product")).casefold() == "hero"
            for image in existing
        )
        if has_hero and all(
            path and Path(path).is_file() and Path(path).suffix.casefold() == ".webp"
            for path in paths
        ):
            return existing

    if not _has_image_sources(task):
        return []
    return prepare_task_images(task, markdown, require_hero=True)


def export_task_docx(config: AppConfig, task: TaskRecord) -> Path:
    title = task.selected_title or task.topic or "Article"
    markdown = _current_article(task, title)
    validate_article_layout(markdown)
    images = _prepared_images(task, markdown)

    document = Document()
    configure_styles(document, config)
    render_markdown(document, markdown, config, images=images)
    enforce_document_typography(document)

    output_dir = Path(task.task_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{safe_filename(title)}.docx"
    document.save(output_path)
    return output_path


def configure_styles(document: Document, config: AppConfig) -> None:
    styles = document.styles
    set_style_font(styles["Normal"], WORD_FONT_NAME, config.body_size, bold=False)
    set_style_font(styles["Title"], WORD_FONT_NAME, config.title_1_size, bold=True)
    set_style_font(styles["Heading 1"], WORD_FONT_NAME, config.title_1_size, bold=True)
    set_style_font(styles["Heading 2"], WORD_FONT_NAME, config.title_2_size, bold=True)
    set_style_font(styles["Heading 3"], WORD_FONT_NAME, config.title_3_size, bold=True)

    section = document.sections[0]
    section.top_margin = Inches(0.8)
    section.bottom_margin = Inches(0.8)
    section.left_margin = Inches(0.9)
    section.right_margin = Inches(0.9)


def set_style_font(style, font_name: str, size_pt: float, bold: bool) -> None:
    font = style.font
    font.name = font_name
    font.size = Pt(size_pt)
    font.bold = bold
    font.color.rgb = RGBColor(0, 0, 0)
    run_properties = style.element.get_or_add_rPr()
    run_fonts = run_properties.get_or_add_rFonts()
    run_fonts.set(qn("w:ascii"), font_name)
    run_fonts.set(qn("w:hAnsi"), font_name)
    run_fonts.set(qn("w:eastAsia"), font_name)


def _set_ooxml_run_font(run_element, *, black: bool = False) -> None:
    run_properties = run_element.get_or_add_rPr()
    run_fonts = run_properties.get_or_add_rFonts()
    run_fonts.set(qn("w:ascii"), WORD_FONT_NAME)
    run_fonts.set(qn("w:hAnsi"), WORD_FONT_NAME)
    run_fonts.set(qn("w:eastAsia"), WORD_FONT_NAME)
    run_fonts.set(qn("w:cs"), WORD_FONT_NAME)
    if black:
        color = run_properties.find(qn("w:color"))
        if color is None:
            color = OxmlElement("w:color")
            run_properties.append(color)
        color.set(qn("w:val"), BLACK_HEX)
        color.attrib.pop(qn("w:themeColor"), None)


def enforce_document_typography(document: Document) -> None:
    """Directly stamp Times New Roman on every run and black on headings."""

    for run_element in document.element.body.xpath(".//w:r"):
        _set_ooxml_run_font(run_element)

    heading_styles = {"Title", "Heading 1", "Heading 2", "Heading 3"}
    for paragraph in document.paragraphs:
        if paragraph.style and paragraph.style.name in heading_styles:
            for run_element in paragraph._p.xpath(".//w:r"):
                _set_ooxml_run_font(run_element, black=True)


def render_markdown(
    document: Document,
    markdown: str,
    config: AppConfig,
    *,
    images: Iterable[object] | None = None,
) -> None:
    image_list = list(images or [])
    placements = resolve_image_placements(markdown, image_list) if image_list else []
    before: dict[int, list[ImagePlacement]] = defaultdict(list)
    after: dict[int, list[ImagePlacement]] = defaultdict(list)
    for placement in placements:
        (before if placement.position == "before" else after)[placement.line_index].append(placement)

    generated_markers = {placement.image["marker"] for placement in placements}
    for line_index, raw_line in enumerate(markdown.splitlines()):
        for placement in before.get(line_index, []):
            add_inline_image(document, placement.image, config)

        line = raw_line.rstrip()
        if line and line.strip() not in generated_markers:
            if line.startswith("# "):
                document.add_heading(line[2:].strip(), level=1)
            elif line.startswith("## "):
                document.add_heading(line[3:].strip(), level=2)
            elif line.startswith("### "):
                document.add_heading(line[4:].strip(), level=3)
            elif line.startswith("- "):
                paragraph = document.add_paragraph(style="List Bullet")
                add_text_with_links(paragraph, line[2:].strip(), config)
            else:
                paragraph = document.add_paragraph()
                faq_question = FAQ_BOLD_QUESTION_PATTERN.fullmatch(line)
                add_text_with_links(
                    paragraph,
                    faq_question.group(1) if faq_question else line,
                    config,
                    bold=bool(faq_question),
                )

        after_line = after.get(line_index, [])
        if after_line and line and document.paragraphs:
            document.paragraphs[-1].paragraph_format.keep_with_next = True
        for placement in after_line:
            add_inline_image(document, placement.image, config)


def add_inline_image(document: Document, image: Mapping[str, Any], config: AppConfig) -> None:
    path = Path(str(image["prepared_path"]))
    paragraph = document.add_paragraph()
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    paragraph.paragraph_format.space_after = Pt(0)
    paragraph.paragraph_format.keep_with_next = True
    run = paragraph.add_run()
    add_webp_picture(run, path)

    description = str(image.get("product_name") or image.get("filename") or "Article image")
    for doc_properties in paragraph._p.xpath(".//wp:docPr"):
        doc_properties.set("descr", description)
        doc_properties.set("title", description)

    marker_paragraph = document.add_paragraph()
    marker_paragraph.paragraph_format.space_before = Pt(0)
    if image.get("role") == "hero":
        marker_paragraph.paragraph_format.keep_with_next = True
    add_run(marker_paragraph, str(image["marker"]), config)


def add_webp_picture(run, path: Path) -> None:
    """Embed a WebP as an inline OOXML image.

    python-docx 1.1.x cannot parse WebP headers, so ``Run.add_picture`` rejects
    otherwise valid files. Creating an ordinary OPC image part plus DrawingML
    inline shape keeps the actual WebP bytes and remains standards-compliant.
    """

    width_px, height_px = image_pixel_size(path)
    max_width_inches = 5.8
    max_height_inches = 6.4
    scale = min(max_width_inches / width_px, max_height_inches / height_px)
    width = Inches(width_px * scale)
    height = Inches(height_px * scale)

    package = run.part.package
    partname = package.next_partname("/word/media/image%d.webp")
    image_part = Part(partname, "image/webp", path.read_bytes(), package)
    relationship_id = run.part.relate_to(
        image_part,
        RELATIONSHIP_TYPE.IMAGE,
    )
    inline = CT_Inline.new_pic_inline(
        run.part.next_id,
        relationship_id,
        path.name,
        width,
        height,
    )
    run._r.add_drawing(inline)


def add_text_with_links(
    paragraph,
    text: str,
    config: AppConfig,
    *,
    bold: bool = False,
) -> None:
    cursor = 0
    for match in MARKDOWN_LINK_PATTERN.finditer(text):
        if match.start() > cursor:
            add_plain_text_with_links(
                paragraph,
                text[cursor : match.start()],
                config,
                bold=bold,
            )
        add_hyperlink(paragraph, match.group(1), match.group(2), config)
        cursor = match.end()
    if cursor < len(text):
        add_plain_text_with_links(paragraph, text[cursor:], config, bold=bold)


def add_plain_text_with_links(
    paragraph,
    text: str,
    config: AppConfig,
    *,
    bold: bool = False,
) -> None:
    cursor = 0
    for match in URL_PATTERN.finditer(text):
        if match.start() > cursor:
            add_run(paragraph, text[cursor : match.start()], config, bold=bold)
        url = match.group(1).rstrip(".,;:")
        trailing = match.group(1)[len(url) :]
        add_hyperlink(paragraph, url, url, config)
        if trailing:
            add_run(paragraph, trailing, config, bold=bold)
        cursor = match.end()
    if cursor < len(text):
        add_run(paragraph, text[cursor:], config, bold=bold)


def add_run(paragraph, text: str, config: AppConfig, *, bold: bool = False) -> None:
    if text:
        run = paragraph.add_run(text)
        run.bold = bold
        run.font.name = WORD_FONT_NAME
        run.font.size = Pt(config.body_size)
        run_properties = run._element.get_or_add_rPr()
        run_fonts = run_properties.get_or_add_rFonts()
        run_fonts.set(qn("w:ascii"), WORD_FONT_NAME)
        run_fonts.set(qn("w:hAnsi"), WORD_FONT_NAME)
        run_fonts.set(qn("w:eastAsia"), WORD_FONT_NAME)
        run_fonts.set(qn("w:cs"), WORD_FONT_NAME)


def add_hyperlink(paragraph, text: str, url: str, config: AppConfig | None = None) -> None:
    part = paragraph.part
    r_id = part.relate_to(url, RELATIONSHIP_TYPE.HYPERLINK, is_external=True)

    hyperlink = OxmlElement("w:hyperlink")
    hyperlink.set(qn("r:id"), r_id)

    run = OxmlElement("w:r")
    run_properties = OxmlElement("w:rPr")

    bold = OxmlElement("w:b")
    bold.set(qn("w:val"), "1")
    run_properties.append(bold)
    bold_complex = OxmlElement("w:bCs")
    bold_complex.set(qn("w:val"), "1")
    run_properties.append(bold_complex)

    color = OxmlElement("w:color")
    color.set(qn("w:val"), "0563C1")
    run_properties.append(color)

    underline = OxmlElement("w:u")
    underline.set(qn("w:val"), "single")
    run_properties.append(underline)

    if config is not None:
        fonts = OxmlElement("w:rFonts")
        fonts.set(qn("w:ascii"), WORD_FONT_NAME)
        fonts.set(qn("w:hAnsi"), WORD_FONT_NAME)
        fonts.set(qn("w:eastAsia"), WORD_FONT_NAME)
        fonts.set(qn("w:cs"), WORD_FONT_NAME)
        run_properties.append(fonts)
        size = OxmlElement("w:sz")
        size.set(qn("w:val"), str(int(round(config.body_size * 2))))
        run_properties.append(size)

    run.append(run_properties)
    text_node = OxmlElement("w:t")
    if text[:1].isspace() or text[-1:].isspace():
        text_node.set(qn("xml:space"), "preserve")
    text_node.text = text
    run.append(text_node)
    hyperlink.append(run)
    paragraph._p.append(hyperlink)
