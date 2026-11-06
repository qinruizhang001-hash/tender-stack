#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""文件物理属性校验的回归测试（不调模型，纯本地构造样本）。"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest
from docx import Document
from pypdf import PdfWriter

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "adapter"))

import file_meta_check as f  # noqa: E402


def _make_docx(path: Path, author: str = "", last_by: str = "", company: str = "", tracked: bool = False) -> Path:
    document = Document()
    document.add_paragraph("投标文件正文")
    document.core_properties.author = author
    document.core_properties.last_modified_by = last_by
    document.save(path)

    if company or tracked:
        with zipfile.ZipFile(path) as archive:
            items = {name: archive.read(name) for name in archive.namelist()}
        if company:
            app = items["docProps/app.xml"].decode("utf-8").replace("<Company/>", f"<Company>{company}</Company>")
            items["docProps/app.xml"] = app.encode("utf-8")
        if tracked:
            body = items["word/document.xml"].decode("utf-8")
            ins = ('<w:ins w:id="900" w:author="李四" w:date="2026-11-01T10:00:00Z">'
                   '<w:r><w:t>新增</w:t></w:r></w:ins>')
            items["word/document.xml"] = body.replace("</w:body>", ins + "</w:body>").encode("utf-8")
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in items.items():
                archive.writestr(name, data)
    return path


def _make_pdf(path: Path, author: str = "", pages: int = 1) -> Path:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=595, height=842)
    if author:
        writer.add_metadata({"/Author": author})
    with path.open("wb") as handle:
        writer.write(handle)
    return path


def test_docx_作者泄露(tmp_path):
    report = f.check_file(_make_docx(tmp_path / "a.docx", author="张三"))
    assert report.status == f.LEAK
    assert any(item["field"] == "Author" for item in report.leaks)


def test_docx_公司泄露(tmp_path):
    report = f.check_file(_make_docx(tmp_path / "b.docx", author="Administrator", company="某某科技有限公司"))
    assert report.status == f.LEAK
    assert report.leaks[0]["field"] == "Company"


def test_docx_最后修改者泄露(tmp_path):
    report = f.check_file(_make_docx(tmp_path / "c.docx", author="Administrator", last_by="李四"))
    assert report.status == f.LEAK
    assert any(item["field"] == "LastModifiedBy" for item in report.leaks)


def test_默认词不算泄露(tmp_path):
    report = f.check_file(_make_docx(tmp_path / "d.docx", author="Administrator", last_by="User"))
    assert report.status == f.PASS
    assert report.leaks == []


def test_空作者不算泄露(tmp_path):
    report = f.check_file(_make_docx(tmp_path / "e.docx", author="", last_by=""))
    assert report.status == f.PASS


def test_修订记录(tmp_path):
    report = f.check_file(_make_docx(tmp_path / "f.docx", author="Administrator", tracked=True))
    assert report.status == f.REVISION
    assert report.revisions["insertions"] == 1
    assert report.revisions["authors"] == ["李四"]


def test_pdf_作者泄露(tmp_path):
    report = f.check_file(_make_pdf(tmp_path / "a.pdf", author="王五", pages=3))
    assert report.status == f.LEAK
    assert report.pages == 3


def test_干净pdf通过(tmp_path):
    report = f.check_file(_make_pdf(tmp_path / "b.pdf"))
    assert report.status == f.PASS
    assert report.pages == 1


def test_docx页数来自app_xml(tmp_path):
    report = f.check_file(_make_docx(tmp_path / "g.docx", author="Administrator"))
    assert report.pages == 1
    assert "app.xml" in report.pages_note


def test_不支持的扩展名(tmp_path):
    path = tmp_path / "x.txt"
    path.write_text("hello", encoding="utf-8")
    assert f.check_file(path).status == f.REVIEW


def test_文件不存在(tmp_path):
    assert f.check_file(tmp_path / "missing.pdf").status == f.REVIEW


@pytest.mark.parametrize("value,expected", [
    ("", False), ("   ", False), ("Administrator", False), ("user", False),
    ("张三", True), ("某某科技有限公司", True), ("123456", False), ("--", False),
])
def test_is_meaningful(value, expected):
    assert f.is_meaningful(value) is expected
