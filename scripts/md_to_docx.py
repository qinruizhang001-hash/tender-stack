# -*- coding: utf-8 -*-
"""把成果说明.md 排成 DOCX（表格/标题/加粗），再用 Word 导出 PDF。

用法：
  python scripts/md_to_docx.py 路径/00-成果说明.md 路径/00-成果说明.docx
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import nsdecls, qn
from docx.shared import Cm, Pt, RGBColor

FONT = "微软雅黑"
ACCENT = RGBColor(0x1F, 0x5F, 0xD8)
ACCENT_HEX = "1F5FD8"
INK = RGBColor(0x14, 0x1A, 0x21)
MUTED = RGBColor(0x5A, 0x65, 0x72)
LINE_HEX = "DCE3EC"


def set_font(run, size=10.5, bold=False, color=INK):
    run.font.name = FONT
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.color.rgb = color
    run._element.rPr.rFonts.set(qn("w:eastAsia"), FONT)


def shade(cell, color):
    cell._tc.get_or_add_tcPr().append(parse_xml(rf'<w:shd {nsdecls("w")} w:fill="{color}"/>'))


def borders(table, color=LINE_HEX):
    element = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        node = OxmlElement(f"w:{edge}")
        node.set(qn("w:val"), "single")
        node.set(qn("w:sz"), "4")
        node.set(qn("w:space"), "0")
        node.set(qn("w:color"), color)
        element.append(node)
    table._tbl.tblPr.append(element)


def add_rich(paragraph, text, size=10.5, color=INK, base_bold=False):
    text = text.replace("`", "")
    for chunk in re.split(r"(\*\*[^*]+\*\*)", text):
        if not chunk:
            continue
        if chunk.startswith("**") and chunk.endswith("**"):
            run = paragraph.add_run(chunk[2:-2])
            set_font(run, size, True, color)
        else:
            run = paragraph.add_run(chunk.replace("*", ""))
            set_font(run, size, base_bold, color)


def heading(doc, text, level):
    paragraph = doc.add_paragraph()
    if level == 1:
        paragraph.paragraph_format.space_before = Pt(6)
        paragraph.paragraph_format.space_after = Pt(10)
        add_rich(paragraph, text, 22, INK, base_bold=True)
        rule = doc.add_paragraph()
        rule.paragraph_format.space_after = Pt(10)
        pPr = rule._p.get_or_add_pPr()
        pBdr = OxmlElement("w:pBdr")
        bottom = OxmlElement("w:bottom")
        bottom.set(qn("w:val"), "single")
        bottom.set(qn("w:sz"), "14")
        bottom.set(qn("w:space"), "1")
        bottom.set(qn("w:color"), ACCENT_HEX)
        pBdr.append(bottom)
        pPr.append(pBdr)
        return

    paragraph.paragraph_format.space_before = Pt(16)
    paragraph.paragraph_format.space_after = Pt(8)
    paragraph.paragraph_format.keep_with_next = True
    pPr = paragraph._p.get_or_add_pPr()
    pBdr = OxmlElement("w:pBdr")
    left = OxmlElement("w:left")
    left.set(qn("w:val"), "single")
    left.set(qn("w:sz"), "24")
    left.set(qn("w:space"), "8")
    left.set(qn("w:color"), ACCENT_HEX)
    pBdr.append(left)
    pPr.append(pBdr)
    run = paragraph.add_run(text)
    set_font(run, 14.5, True, INK)


def build(md_path: Path, out_path: Path) -> None:
    lines = md_path.read_text(encoding="utf-8").splitlines()
    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = FONT
    style.font.size = Pt(10.5)
    style.element.rPr.rFonts.set(qn("w:eastAsia"), FONT)

    section = doc.sections[0]
    section.page_width, section.page_height = Cm(21), Cm(29.7)
    section.top_margin = Cm(2.2)
    section.bottom_margin = Cm(2.0)
    section.left_margin = Cm(2.4)
    section.right_margin = Cm(2.4)

    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if not stripped or stripped == "---":
            index += 1
            continue
        if stripped.startswith("# "):
            heading(doc, stripped[2:], 1)
            index += 1
            continue
        if stripped.startswith("## "):
            heading(doc, stripped[3:], 2)
            index += 1
            continue
        if (
            stripped.startswith("|")
            and index + 1 < len(lines)
            and set(lines[index + 1].replace("|", "").strip()) <= {"-", ":"}
        ):
            header = [cell.strip() for cell in stripped.strip("|").split("|")]
            rows, cursor = [], index + 2
            while cursor < len(lines) and lines[cursor].strip().startswith("|"):
                rows.append([cell.strip() for cell in lines[cursor].strip().strip("|").split("|")])
                cursor += 1
            table = doc.add_table(rows=1, cols=len(header))
            table.alignment = WD_TABLE_ALIGNMENT.CENTER
            borders(table)
            for column, text in enumerate(header):
                cell = table.rows[0].cells[column]
                cell.text = ""
                run = cell.paragraphs[0].add_run(text)
                set_font(run, 9.3, True, RGBColor(0xFF, 0xFF, 0xFF))
                shade(cell, ACCENT_HEX)
            for row in rows:
                cells = table.add_row().cells
                for column, text in enumerate(row[: len(header)]):
                    cells[column].text = ""
                    add_rich(cells[column].paragraphs[0], text, 9.3)
            spacer = doc.add_paragraph()
            spacer.paragraph_format.space_after = Pt(4)
            index = cursor
            continue
        if stripped.startswith(">"):
            paragraph = doc.add_paragraph()
            paragraph.paragraph_format.left_indent = Cm(0.4)
            paragraph.paragraph_format.space_after = Pt(4)
            add_rich(paragraph, stripped.lstrip("> ").strip(), 9.2, MUTED)
            index += 1
            continue
        if stripped.startswith("- "):
            paragraph = doc.add_paragraph()
            paragraph.paragraph_format.left_indent = Cm(0.5)
            paragraph.paragraph_format.first_line_indent = Cm(-0.35)
            paragraph.paragraph_format.space_after = Pt(3)
            marker = paragraph.add_run("· ")
            set_font(marker, 9, True, ACCENT)
            add_rich(paragraph, stripped[2:], 10.5)
            index += 1
            continue

        paragraph = doc.add_paragraph()
        paragraph.paragraph_format.space_after = Pt(6)
        paragraph.paragraph_format.line_spacing = 1.5
        add_rich(paragraph, stripped, 10.5, MUTED if stripped.startswith("*") else INK)
        index += 1

    doc.save(out_path)


if __name__ == "__main__":
    build(Path(sys.argv[1]), Path(sys.argv[2]))
    print("docx ok")
