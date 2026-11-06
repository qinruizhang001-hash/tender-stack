#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫描版 PDF 的 OCR 文本补全测试。"""
from __future__ import annotations

import sys
from pathlib import Path

import fitz
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "adapter"))

import bid_chunker  # noqa: E402


@pytest.fixture
def scanned_pdf(tmp_path) -> Path:
    source = fitz.open()
    page = source.new_page()
    page.insert_text((60, 120), "投标人须提供近三个月的社保缴纳证明", fontname="china-s", fontsize=16)
    image = page.get_pixmap(dpi=180).tobytes("png")
    source.close()

    doc = fitz.open()
    sheet = doc.new_page(width=595, height=842)
    sheet.insert_image(fitz.Rect(0, 0, 595, 842), stream=image)
    path = tmp_path / "扫描.pdf"
    doc.save(path)
    doc.close()
    return path


def test_没有文字层时不需要_OCR_也能跑(scanned_pdf):
    chunks, stats = bid_chunker.chunk_pdf(scanned_pdf, ocr=False)
    assert stats["ocr_done_pages"] == []
    assert stats["chunks"] == 0


@pytest.mark.skipif(not bid_chunker.ocr_available(), reason="未安装 rapidocr")
def test_扫描页被_OCR_补全(scanned_pdf):
    chunks, stats = bid_chunker.chunk_pdf(scanned_pdf)
    assert stats["ocr_done_pages"] == [1]
    assert stats["ocr_missing_pages"] == []
    text = "".join(chunk["text"] for chunk in chunks).replace(" ", "")
    assert "社保" in text or "注册资本" in text


@pytest.mark.skipif(not bid_chunker.ocr_available(), reason="未安装 rapidocr")
def test_OCR_有页数上限(scanned_pdf):
    chunks, stats = bid_chunker.chunk_pdf(scanned_pdf, max_ocr_pages=0)
    assert stats["ocr_done_pages"] == []
    assert stats["ocr_limited_pages"] == 1

def test_OCR_降级可观测(tmp_path, monkeypatch):
    """侧车失败时必须留下 from/to/reason，不能静默换引擎。"""
    import bid_chunker
    import ocr_backend

    source = fitz.open()
    page = source.new_page()
    page.insert_text((60, 120), "投标人须提供社保证明", fontname="china-s", fontsize=16)
    image = page.get_pixmap(dpi=150).tobytes("png")
    source.close()
    doc = fitz.open()
    sheet = doc.new_page(width=595, height=842)
    sheet.insert_image(fitz.Rect(0, 0, 595, 842), stream=image)
    path = tmp_path / "扫描.pdf"
    doc.save(path)
    doc.close()

    monkeypatch.setattr(bid_chunker, "_OCR_CACHE", tmp_path / "ocr_cache")
    monkeypatch.setattr(ocr_backend, "available", lambda refresh=False: {"paddle": True, "rapidocr": True})

    def boom(_path):
        raise RuntimeError("sidecar down")

    monkeypatch.setattr(ocr_backend, "ocr_image_file", boom)
    monkeypatch.delenv("TENDER_OCR_STRICT", raising=False)

    events: list[dict] = []
    text = bid_chunker._ocr_page(path, 1, events=events)
    assert events and events[0]["from"] == "paddle" and events[0]["to"] == "rapidocr"
    assert "sidecar down" in events[0]["reason"]
    assert isinstance(text, str)

    monkeypatch.setattr(bid_chunker, "_OCR_CACHE", tmp_path / "ocr_cache_strict")  # 避开上一轮缓存
    monkeypatch.setenv("TENDER_OCR_STRICT", "1")
    import pytest

    with pytest.raises(RuntimeError):
        bid_chunker._ocr_page(path, 1, events=[])
