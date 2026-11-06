#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PDF 报告的回归测试：不联网、不依赖真实运行目录。"""
from __future__ import annotations

import sys
from pathlib import Path

import fitz
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "workbench"))

from report import build_report  # noqa: E402

META = {"document_filename": "某某项目-招标文件.pdf", "page_count": 83}

ROWS = [
    {"req_id": "R-001", "section": "资格", "type": "资格", "requirement": "投标人注册资本不低于500万元", "page": "3"},
    {"req_id": "R-002", "section": "技术", "type": "技术", "requirement": "提供ISO13485认证证书", "page": "4"},
]

COVERAGE = [
    {"req_id": "R-001", "requirement": "投标人注册资本不低于500万元", "coverage": "负偏离",
     "bid_page": "3", "judge_basis": "要求 500 万元，证据 300 万元"},
    {"req_id": "R-002", "requirement": "提供ISO13485认证证书", "coverage": "已响应",
     "bid_page": "4", "judge_basis": "证书齐全"},
]


def _text(pdf: bytes) -> str:
    document = fitz.open(stream=pdf, filetype="pdf")
    text = "\n".join(page.get_text() for page in document)
    # PyMuPDF 会把排版空隙提取成 \xa0，统一成普通空格再断言
    return text.replace("\xa0", " ")


def test_生成合法_pdf():
    pdf = build_report(name="demo", meta=META, rows=ROWS, coverage=COVERAGE)
    assert pdf.startswith(b"%PDF-")
    document = fitz.open(stream=pdf, filetype="pdf")
    assert document.page_count >= 2


def test_包含风险条款与页码():
    text = _text(build_report(name="demo", meta=META, rows=ROWS, coverage=COVERAGE))
    assert "负偏离" in text
    assert "R-001" in text
    assert "500" in text and "万元" in text


def test_没有比对结果时明确说明():
    text = _text(build_report(name="demo", meta=META, rows=ROWS, coverage=[]))
    assert "没有比对结果" in text
    assert "不含合规结论" in text


def test_字体子集化后体积可控():
    pdf = build_report(name="demo", meta=META, rows=ROWS, coverage=COVERAGE)
    assert len(pdf) < 2 * 1024 * 1024, "报告不应把完整中文字体嵌进去"


def test_空运行不崩溃():
    pdf = build_report(name="empty", meta={}, rows=[], coverage=[])
    assert pdf.startswith(b"%PDF-")


@pytest.mark.parametrize("name", ["bench_1103_222748", "armC_v4"])
def test_真实运行目录可生成(name):
    root = Path(__file__).resolve().parent.parent
    folder = root / "out" / name
    if not folder.exists():
        pytest.skip("运行目录不存在")
    pdf = build_report(name=name, meta=META, rows=ROWS, coverage=[])
    assert pdf.startswith(b"%PDF-")
