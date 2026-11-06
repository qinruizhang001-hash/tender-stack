#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按标题层级切块 + 表格转 Markdown。"""
from __future__ import annotations

import sys
from pathlib import Path

import fitz

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "adapter"))

from layout_parse import parse_sections, rows_to_markdown  # noqa: E402


def _pdf(tmp_path, pages: list[list[tuple[str, float, bool]]], name="doc.pdf"):
    doc = fitz.open()
    for rows in pages:
        page = doc.new_page(width=595, height=842)
        y = 80
        for text, size, bold in rows:
            page.insert_text((60, y), text, fontname="china-s", fontsize=size)
            y += 26
    path = tmp_path / name
    doc.save(path)
    doc.close()
    return path


def test_标题层级切块(tmp_path):
    path = _pdf(
        tmp_path,
        [[
            ("第一章 投标邀请", 18, True),
            ("一、项目基本情况", 13, False),
            ("项目编号：ABC-001", 12, False),
            ("第二章 投标人须知", 18, True),
            ("1.1 总则", 13, False),
            ("投标人应当遵守以下规定。", 12, False),
        ]],
    )
    chunks, stats = parse_sections(path)
    sections = [chunk["section"] for chunk in chunks]
    assert any("第一章" in item for item in sections)
    assert any("第二章" in item for item in sections)
    first = next(chunk for chunk in chunks if "第一章" in chunk["section"])
    assert first["text"].startswith("第一章")
    assert stats["mode"] == "sections"


def test_资格要求不被切碎(tmp_path):
    """一个完整章节（含子条）必须留在同一块里。"""
    path = _pdf(
        tmp_path,
        [[
            ("第一章 投标邀请", 18, True),
            ("二、申请人的资格要求", 13, False),
            ("1.满足《政府采购法》第二十二条规定；", 12, False),
            ("2.落实政府采购政策需满足的资格要求：", 12, False),
            ("2.1 中小企业政策", 13, False),
            ("本项目专门面向中小企业采购。", 12, False),
            ("3.1 其他特定资格要求：须具有劳务派遣经营许可证。", 12, False),
        ]],
    )
    chunks, _ = parse_sections(path)
    block = next(chunk for chunk in chunks if "资格要求" in chunk["text"])
    assert "2.1 中小企业政策" in block["text"]
    assert "劳务派遣经营许可证" in block["text"]


def test_表格输出为_markdown(tmp_path):
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((60, 80), "第一章 采购需求", fontname="china-s", fontsize=18)
    x0, y0, width, height = 60, 120, 150, 28
    rows = [("序号", "项目", "金额"), ("1", "社保", "3万"), ("2", "纳税", "5万")]
    for index in range(len(rows) + 1):
        page.draw_line((x0, y0 + index * height), (x0 + width * 3, y0 + index * height), width=1)
    for index in range(4):
        page.draw_line((x0 + index * width, y0), (x0 + index * width, y0 + len(rows) * height), width=1)
    for row_index, row in enumerate(rows):
        for column, value in enumerate(row):
            page.insert_text((x0 + column * width + 8, y0 + row_index * height + 18), value, fontname="china-s", fontsize=12)
    path = tmp_path / "table.pdf"
    doc.save(path)
    doc.close()

    chunks, stats = parse_sections(path)
    tables = [chunk for chunk in chunks if "| --- |" in chunk["text"]]
    assert tables, "表格应转成 Markdown"
    body = tables[0]["text"]
    assert "| 序号 | 项目 | 金额 |" in body
    assert "| 1 | 社保 | 3万 |" in body
    assert stats["table_chunks"] >= 1


def test_rows_to_markdown_转义():
    markdown = rows_to_markdown([["a|b", "c"], ["d", "e\nf"]])
    assert markdown.splitlines()[0] == "| a\\|b | c |"
    assert "| d | e f |" in markdown


def test_扫描件退回_OCR_路径():
    from bid_chunker import chunk_pdf

    folder = Path(__file__).resolve().parent.parent / "data" / "tenders" / "imported"
    samples = list(folder.glob("*.pdf")) if folder.exists() else []
    if not samples:
        return
    chunks, stats = chunk_pdf(samples[0])
    assert stats["chunks"] == len(chunks)

def test_目录页不进正文(tmp_path):
    doc = fitz.open()
    cover = doc.new_page(width=595, height=842)
    cover.insert_text((180, 200), "（2026 年版）", fontname="china-s", fontsize=22)
    toc = doc.new_page(width=595, height=842)
    toc.insert_text((240, 80), "目  录", fontname="china-s", fontsize=18)
    y = 130
    for line in ("第一章   投标邀请 ................ 1", "第二章   投标人须知 .............. 5"):
        toc.insert_text((80, y), line, fontname="china-s", fontsize=12)
        y += 26
    body = doc.new_page(width=595, height=842)
    body.insert_text((60, 80), "第一章 投标邀请", fontname="china-s", fontsize=18)
    body.insert_text((60, 120), "一、项目基本情况", fontname="china-s", fontsize=13)
    body.insert_text((60, 160), "项目编号：ABC-001，预算金额 100 万元。", fontname="china-s", fontsize=12)
    path = tmp_path / "toc.pdf"
    doc.save(path)
    doc.close()

    chunks, stats = parse_sections(path)
    sections = " ".join(chunk["section"] for chunk in chunks)
    texts = " ".join(chunk["text"] for chunk in chunks)
    assert "录" not in sections, f"目录混进了 section：{sections}"
    assert "2026" not in sections
    assert "...." not in texts
    assert 2 in stats["toc_pages"]


def test_封面版本行不当章节(tmp_path):
    path = _pdf(
        tmp_path,
        [[
            ("（2026 年版）", 24, True),
            ("北京市政府采购项目", 20, True),
            ("第一章 投标邀请", 18, True),
            ("项目编号：ABC-001。", 12, False),
        ]],
    )
    chunks, _ = parse_sections(path)
    for chunk in chunks:
        assert "2026" not in chunk["section"]
        assert "北京市政府采购项目" not in chunk["section"]


def test_条款级拆分带页码(tmp_path):
    path = _pdf(
        tmp_path,
        [[
            ("第一章 投标邀请", 18, True),
            ("二、申请人的资格要求", 13, False),
            ("1.满足《政府采购法》第二十二条规定。", 12, False),
            ("2.落实政府采购政策需满足的资格要求。", 12, False),
            ("3.其他特定资格要求：须具有劳务派遣许可证。", 12, False),
        ]],
    )
    from layout_parse import extract_requirements

    chunks, _ = parse_sections(path)
    items = extract_requirements(chunks)
    assert len(items) >= 3, f"应拆出至少 3 条，实际 {len(items)}"
    assert all(item["page"] and item["section"] for item in items)
    assert any("劳务派遣" in item["text"] for item in items)
    assert len({item["id"] for item in items}) == len(items)

def test_单行多条款拆成两条():
    from layout_parse import extract_requirements

    chunks = [{
        "section": "第一章 > 资格要求",
        "text": "1.满足《政府采购法》第二十二条规定；2.落实中小企业采购政策。",
        "page": 3,
        "pages": [3],
    }]
    items = extract_requirements(chunks)
    assert len(items) == 2, f"同行两条应拆开，实际 {len(items)}"
    assert [item["number"] for item in items] == ["1", "2"]
    assert items[0]["page"] == 3


def test_编号乱序合并标_low():
    from layout_parse import extract_requirements

    chunks = [{
        "section": "第一章 > 资格要求",
        "text": "5.第五条要求内容比较长。\n2.第二条要求内容比较长。",
        "page": 4,
        "pages": [4],
    }]
    items = extract_requirements(chunks)
    assert len(items) == 1, "乱序编号应合并回一条"
    assert items[0]["quality"] == "low"
    assert items[0]["quality_reason"] == "req_number_chaos"


def test_无点线目录仍判目录(tmp_path):
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((230, 80), "目  录", fontname="china-s", fontsize=18)
    entries = [
        "第一章 投标邀请 1",
        "第二章 投标人须知 5",
        "第三章 资格审查 21",
        "第四章 评标办法 23",
        "第五章 采购需求 40",
        "第六章 合同条款 55",
    ]
    y = 130
    for entry in entries:
        page.insert_text((80, y), entry, fontname="china-s", fontsize=12)
        y += 26
    path = tmp_path / "toc_plain.pdf"
    doc.save(path)
    doc.close()

    _, stats = parse_sections(path)
    assert 1 in stats["toc_pages"], "无点线目录页应被识别"


def test_正文页不被误判目录(tmp_path):
    path = _pdf(
        tmp_path,
        [[
            ("第一章 投标邀请", 18, True),
            ("1.项目编号：ABC-001。", 12, False),
            ("2.项目名称：公园养护。", 12, False),
            ("3.预算金额：366 万元。", 12, False),
            ("4.采购需求：劳务派遣。", 12, False),
            ("5.合同履行期限：一年。", 12, False),
        ]],
    )
    _, stats = parse_sections(path)
    assert stats["toc_pages"] == []

def test_表行条款进_requirement():
    from layout_parse import extract_requirements

    chunks = [{
        "section": "第三章 > 评标",
        "text": "| 条款号 | 内容 |\n| --- | --- |",
        "page": 8,
        "pages": [8],
        "tables": [[["条款号", "内容"], ["12.8.2", "□无 □有，投标保证金不予退还"]],
                   [["序号", "金额"], ["1", "30000"], ["2", "50000"]]],
    }]
    items = extract_requirements(chunks)
    rows = [item for item in items if item["kind"] == "table_row"]
    assert rows, "勾选行应进 requirement"
    assert rows[0]["number"] == "12.8.2"
    assert "□" in rows[0]["text"]
    assert rows[0]["display_text"].startswith("12.8.2. ")
    assert all(item["kind"] != "table_row" or "30000" not in item["text"] for item in items), "纯数字矩阵不拆条"


def test_行尾编号切成独立条():
    from layout_parse import extract_requirements

    chunks = [{"section": "第一章", "text": "投标人应当提供近三个月社保缴纳证明，详见条款 1.", "page": 2, "pages": [2]}]
    items = extract_requirements(chunks)
    assert any(item["number"] == "1" for item in items), items


def test_开关默认关闭(monkeypatch):
    import sidecar_models

    monkeypatch.delenv("TENDER_LAYOUT", raising=False)
    monkeypatch.delenv("TENDER_TABLE_MODEL", raising=False)
    monkeypatch.delenv("TENDER_VLM_OCR", raising=False)
    status = sidecar_models.status_summary()
    assert status["layout"]["enabled"] is False
    assert status["table"]["enabled"] is False


def test_开启版面开关但缺依赖时回退(tmp_path, monkeypatch):
    monkeypatch.setenv("TENDER_LAYOUT", "1")
    path = _pdf(tmp_path, [[("第一章 投标邀请", 18, True), ("投标人须提供营业执照。", 12, False)]])
    _, stats = parse_sections(path)
    assert stats["layout"]["enabled"] is True
    assert stats["layout_fallback"] == "not_installed"
