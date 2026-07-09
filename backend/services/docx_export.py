from __future__ import annotations

import re
from pathlib import Path

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.opc.constants import RELATIONSHIP_TYPE
from docx.shared import Inches, Pt

from config import AppConfig
from models import Product, TaskRecord


INVALID_FILENAME_CHARS = r'<>:"/\|?*'


def safe_filename(value: str) -> str:
    cleaned = "".join("_" if char in INVALID_FILENAME_CHARS else char for char in value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:120] or "article"


def export_task_docx(config: AppConfig, task: TaskRecord) -> Path:
    title = task.selected_title or task.topic or "Article"
    document = Document()
    configure_styles(document, config)

    markdown = task.article or f"# {title}\n\n"
    render_markdown(document, markdown, config)
    append_products(document, task.products, config)

    output_path = Path(task.task_dir) / f"{safe_filename(title)}.docx"
    document.save(output_path)
    return output_path


def configure_styles(document: Document, config: AppConfig) -> None:
    styles = document.styles
    set_style_font(styles["Normal"], config.docx_font, config.body_size, bold=False)
    set_style_font(styles["Title"], config.docx_font, config.title_1_size, bold=True)
    set_style_font(styles["Heading 1"], config.docx_font, config.title_1_size, bold=True)
    set_style_font(styles["Heading 2"], config.docx_font, config.title_2_size, bold=True)
    set_style_font(styles["Heading 3"], config.docx_font, config.title_3_size, bold=True)

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
    style.element.rPr.rFonts.set(qn("w:eastAsia"), font_name)


def render_markdown(document: Document, markdown: str, config: AppConfig) -> None:
    for raw_line in markdown.splitlines():
        line = raw_line.rstrip()
        if not line:
            continue
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
            add_text_with_links(paragraph, line, config)


def append_products(document: Document, products: list[Product], config: AppConfig) -> None:
    if not products:
        return
    document.add_heading("Recommended Products", level=2)
    for product in products:
        if not product.name and not product.url and not product.description and not product.image_path:
            continue
        paragraph = document.add_paragraph()
        paragraph.add_run(product.name or "Product").bold = True
        if product.url:
            paragraph.add_run(": ")
            add_hyperlink(paragraph, product.url, product.url)
        if product.description:
            desc = document.add_paragraph()
            add_text_with_links(desc, product.description, config)
        if product.image_path:
            image_path = Path(product.image_path)
            if image_path.exists():
                try:
                    document.add_picture(str(image_path), width=Inches(4.8))
                except Exception:
                    pass


MARKDOWN_LINK_PATTERN = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
URL_PATTERN = re.compile(r"(https?://[^\s)]+)")


def add_text_with_links(paragraph, text: str, config: AppConfig) -> None:
    cursor = 0
    for match in MARKDOWN_LINK_PATTERN.finditer(text):
        if match.start() > cursor:
            add_plain_text_with_links(paragraph, text[cursor : match.start()], config)
        add_hyperlink(paragraph, match.group(1), match.group(2))
        cursor = match.end()
    if cursor < len(text):
        add_plain_text_with_links(paragraph, text[cursor:], config)


def add_plain_text_with_links(paragraph, text: str, config: AppConfig) -> None:
    cursor = 0
    for match in URL_PATTERN.finditer(text):
        if match.start() > cursor:
            add_run(paragraph, text[cursor : match.start()], config)
        url = match.group(1).rstrip(".,;:")
        trailing = match.group(1)[len(url) :]
        add_hyperlink(paragraph, url, url)
        if trailing:
            add_run(paragraph, trailing, config)
        cursor = match.end()
    if cursor < len(text):
        add_run(paragraph, text[cursor:], config)


def add_run(paragraph, text: str, config: AppConfig) -> None:
    if text:
        run = paragraph.add_run(text)
        run.font.name = config.docx_font
        run.font.size = Pt(config.body_size)


def add_hyperlink(paragraph, text: str, url: str) -> None:
    part = paragraph.part
    r_id = part.relate_to(url, RELATIONSHIP_TYPE.HYPERLINK, is_external=True)

    hyperlink = OxmlElement("w:hyperlink")
    hyperlink.set(qn("r:id"), r_id)

    run = OxmlElement("w:r")
    run_properties = OxmlElement("w:rPr")

    color = OxmlElement("w:color")
    color.set(qn("w:val"), "0563C1")
    run_properties.append(color)

    underline = OxmlElement("w:u")
    underline.set(qn("w:val"), "single")
    run_properties.append(underline)

    run.append(run_properties)
    text_node = OxmlElement("w:t")
    text_node.text = text
    run.append(text_node)
    hyperlink.append(run)
    paragraph._p.append(hyperlink)
