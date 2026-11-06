#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Word（.docx）解析：段落、表格结构、以及表格里的数值进入规则判定。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from docx import Document

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "adapter"))

from bid_chunker import chunk_pdf  # noqa: E402
from match_bid import numeric_precheck  # noqa: E402


@pytest.fixture
def docx_file(tmp_path) -> Path:
    doc = Document()
    doc.add_paragraph("投标文件")
    doc.add_paragraph("第一章 商务部分")
    table = doc.add_table(rows=3, cols=3)
    values = [["序号", "项目", "投标总价（万元）"], ["1", "设备", "3"], ["2", "安装", "8"]]
    for row_index, row in enumerate(values):
        for column_index, value in enumerate(row):
            table.cell(row_index, column_index).text = value
    doc.add_paragraph("第二章 技术部分")
    path = tmp_path / "投标文件.docx"
    doc.save(path)
    return path


def test_读取段落与表格(docx_file):
    chunks, stats = chunk_pdf(docx_file)
    assert stats["backend"] == "docx"
    assert stats["chunks"] >= 3
    texts = "".join(chunk["text"] for chunk in chunks)
    assert "第一章 商务部分" in texts
    assert "第二章 技术部分" in texts


def test_表格带结构(docx_file):
    chunks, _ = chunk_pdf(docx_file)
    table = next(chunk for chunk in chunks if chunk["kind"] == "table")
    assert table["table"]["header"] == ["序号", "项目", "投标总价（万元）"]
    assert table["table"]["rows"][0] == ["1", "设备", "3"]


def test_表格数值进入规则判定(docx_file):
    chunks, _ = chunk_pdf(docx_file)
    table = next(chunk for chunk in chunks if chunk["kind"] == "table")
    verdict = numeric_precheck("投标报价不低于50000元", [table])
    assert verdict is not None
    assert verdict["coverage"] == "负偏离"
    assert "3万元" in verdict["evidence"]


def test_合并单元格去重(tmp_path):
    doc = Document()
    table = doc.add_table(rows=2, cols=3)
    table.cell(0, 0).merge(table.cell(0, 1)).text = "合并表头"
    table.cell(0, 2).text = "金额"
    for column, value in enumerate(("1", "设备", "3万")):
        table.cell(1, column).text = value
    path = tmp_path / "合并.docx"
    doc.save(path)

    chunks, _ = chunk_pdf(path)
    table_chunk = next(chunk for chunk in chunks if chunk["kind"] == "table")
    assert table_chunk["table"]["header"][0] == "合并表头"
    assert "合并表头 | 合并表头" not in table_chunk["text"]
