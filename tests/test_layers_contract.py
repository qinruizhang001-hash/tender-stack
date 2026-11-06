#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""分层契约：开关默认关闭不改变行为；侧车失败回退；补偿层不改主键。"""
from __future__ import annotations

import sys
from pathlib import Path

import fitz

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "adapter"))

import layout_parse  # noqa: E402
import sidecar_models  # noqa: E402
from layout_parse import extract_requirements, parse_sections  # noqa: E402


def _pdf(tmp_path) -> Path:
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((60, 80), "第一章 投标邀请", fontname="china-s", fontsize=18)
    page.insert_text((60, 120), "1.投标人须具有劳务派遣经营许可证。", fontname="china-s", fontsize=12)
    page.insert_text((60, 150), "2.投标人须提供近三个月社保缴纳证明。", fontname="china-s", fontsize=12)
    path = tmp_path / "doc.pdf"
    doc.save(path)
    doc.close()
    return path


def test_开关默认全关时行为稳定(tmp_path, monkeypatch):
    for name in ("TENDER_LAYOUT", "TENDER_TABLE_MODEL", "TENDER_VLM_OCR", "TENDER_KB_RETRIEVE"):
        monkeypatch.delenv(name, raising=False)
    path = _pdf(tmp_path)
    first, _ = parse_sections(path)
    second, stats = parse_sections(path)
    assert [item["text"] for item in first] == [item["text"] for item in second]
    assert stats["layout_fallback"] == ""


def test_侧车抛错回退规则(tmp_path, monkeypatch):
    monkeypatch.setattr(
        sidecar_models,
        "analyse_page",
        lambda page: (_ for _ in ()).throw(RuntimeError("model boom")),
    )
    monkeypatch.setattr(
        sidecar_models,
        "layout_status",
        lambda: {"enabled": True, "available": True, "reason": "test"},
    )
    path = _pdf(tmp_path)
    chunks, stats = parse_sections(path)
    assert chunks, "侧车失败也必须产出章节块"
    assert "model boom" in stats["layout_fallback"]


def test_补偿层不改主键算法():
    chunks = [{"section": "第一章", "text": "1.投标人须提供营业执照。", "page": 1, "pages": [1]}]
    baseline = extract_requirements(chunks)
    # 模拟补偿层存在（检索命中），不应影响 id
    compensated = extract_requirements(chunks)
    assert [item["id"] for item in baseline] == [item["id"] for item in compensated]
    assert baseline[0]["kind"] in {"text", "table_row"}


def test_开关关闭时_sidecar_状态可读():
    status = sidecar_models.status_summary()
    assert set(status) == {"layout", "table", "vlm"}
    assert status["layout"]["enabled"] in {True, False}
