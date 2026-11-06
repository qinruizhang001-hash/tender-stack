#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OCR 后端探测与向量库导出（requirement 级）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import fitz

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "adapter"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import ocr_backend  # noqa: E402
from vector_export import export_chunks  # noqa: E402


def _pdf(tmp_path) -> Path:
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((60, 80), "第一章 投标邀请", fontname="china-s", fontsize=18)
    page.insert_text((60, 120), "一、资格要求", fontname="china-s", fontsize=13)
    page.insert_text((60, 150), "1.投标人须具有劳务派遣经营许可证。", fontname="china-s", fontsize=12)
    page.insert_text((60, 180), "2.投标人须提供近三个月的社保缴纳证明。", fontname="china-s", fontsize=12)
    page.insert_text((60, 210), "3.本项目不接受联合体投标。", fontname="china-s", fontsize=12)
    path = tmp_path / "doc.pdf"
    doc.save(path)
    doc.close()
    return path


def test_后端只有真实可用的两种():
    flags = ocr_backend.available(refresh=True)
    assert set(flags) == {"paddle", "rapidocr"}, "mineru 是假选项，不应再出现"
    assert isinstance(ocr_backend.describe()["selected"], str)


def test_侧车缺依赖时不报_paddle(tmp_path, monkeypatch):
    python = tmp_path / "python.exe"
    python.write_text("not-a-real-python", encoding="utf-8")
    monkeypatch.setattr(ocr_backend, "paddle_python", lambda: python)
    monkeypatch.setattr(ocr_backend, "PADDLE_WORKER", tmp_path / "missing_worker.py")
    monkeypatch.setattr(ocr_backend, "_PROBE_CACHE", None)

    assert ocr_backend.available(refresh=True)["paddle"] is False


def test_强制指定不可用后端时降级(monkeypatch):
    monkeypatch.setenv("TENDER_OCR", "mineru")
    monkeypatch.setattr(ocr_backend, "_PROBE_CACHE", {"paddle": False, "rapidocr": True})
    assert ocr_backend.backend() == "rapidocr"


def test_导出_requirement_级_jsonl(tmp_path):
    info = export_chunks(_pdf(tmp_path), tmp_path / "req.jsonl")
    assert info["level"] == "requirement"
    assert info["requirements"] >= 3, "资格要求章应拆出至少 3 条"
    assert (tmp_path / "req.jsonl").exists()
    assert (tmp_path / "req.sections.jsonl").exists()

    rows = [json.loads(line) for line in (tmp_path / "req.jsonl").read_text(encoding="utf-8").splitlines()]
    for key in ("id", "text", "number", "section", "page", "pages", "source", "chars"):
        assert key in rows[0]
    assert rows[0]["number"], "条款编号不应为空"
    assert rows[0]["page"] >= 1


def test_导出_id_稳定(tmp_path):
    pdf = _pdf(tmp_path)
    first = [json.loads(line)["id"] for line in _run(pdf, tmp_path / "a.jsonl")]
    second = [json.loads(line)["id"] for line in _run(pdf, tmp_path / "b.jsonl")]
    assert first == second, "重跑 id 必须一致"


def _run(pdf: Path, target: Path) -> list[str]:
    export_chunks(pdf, target)
    return target.read_text(encoding="utf-8").splitlines()


def test_章节级导出(tmp_path):
    info = export_chunks(_pdf(tmp_path), tmp_path / "sec.jsonl", level="section")
    assert info["sections"] >= 1
    rows = [json.loads(line) for line in (tmp_path / "sec.jsonl").read_text(encoding="utf-8").splitlines()]
    assert "has_table" in rows[0]
    assert "quality" in rows[0]


def test_金标评估命中率(tmp_path):
    from eval_chunks import evaluate

    chunks = tmp_path / "c.jsonl"
    chunks.write_text(
        json.dumps({"text": "投标人须具有劳务派遣经营许可证。"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    gold = tmp_path / "gold.json"
    gold.write_text(
        json.dumps(["劳务派遣经营许可证", "近三个月社保"], ensure_ascii=False),
        encoding="utf-8",
    )
    result = evaluate(gold, chunks)
    assert result["total"] == 2
    assert result["covered"] == 1
    assert result["recall"] == 0.5

def test_导出带_ocr_fallback(tmp_path, monkeypatch):
    """降级事件必须出现在导出 stats 里，不能丢在解析层。"""
    import bid_chunker

    monkeypatch.setattr(
        bid_chunker,
        "chunk_pdf",
        lambda *_args, **_kwargs: (
            [{"section": "第一章", "text": "投标人须提供社保证明。", "page": 1, "pages": [1], "kind": "section"}],
            {"backend": "layout", "ocr_fallback": [{"from": "paddle", "to": "rapidocr", "reason": "x", "page": 1}],
             "toc_pages": [], "table_suspect": 0, "mode": "sections"},
        ),
    )
    info = export_chunks(tmp_path / "any.pdf", tmp_path / "out.jsonl")
    assert info["ocr_fallback_count"] == 1
    assert info["ocr_fallback"][0]["to"] == "rapidocr"
    assert info["low_quality"] == 0

def test_条款被切成两条也算覆盖(tmp_path):
    """金标条款被拆到两条 requirement 里：coverage 命中，exact 不命中。"""
    from eval_chunks import evaluate

    chunks = tmp_path / "c.jsonl"
    chunks.write_text(
        "\n".join(
            [
                json.dumps({"text": "投标人须具有有效的《劳务派遣经营许可证》", "number": "3.2"}, ensure_ascii=False),
                json.dumps({"text": "并加盖电子签章。", "number": ""}, ensure_ascii=False),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    gold = tmp_path / "g.json"
    gold.write_text(
        json.dumps([{"id": "G01", "text": "投标人须具有有效的《劳务派遣经营许可证》并加盖电子签章。"}], ensure_ascii=False),
        encoding="utf-8",
    )
    result = evaluate(gold, chunks)
    assert result["total"] == 1
    assert result["covered"] == 1, result
    assert result["exact"] == 0
    assert result["recall"] == 1.0


def test_条款缺失进入_miss(tmp_path):
    from eval_chunks import evaluate

    chunks = tmp_path / "c.jsonl"
    chunks.write_text(json.dumps({"text": "无关内容", "number": ""}, ensure_ascii=False) + "\n", encoding="utf-8")
    gold = tmp_path / "g.json"
    gold.write_text(json.dumps([{"id": "G02", "text": "投标人须提供近三个月社保缴纳证明。"}], ensure_ascii=False), encoding="utf-8")
    result = evaluate(gold, chunks)
    assert result["covered"] == 0
    assert result["miss"][0]["id"] == "G02"

def test_金标门禁阈值判断(tmp_path):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from gold_gate import passes, run

    assert passes(0.95, 0.90) is True
    assert passes(0.89, 0.90) is False
    assert passes(None, 0.90) is True   # 金标为空不算失败

    skipped = run(tmp_path / "missing.pdf", tmp_path / "missing.json")
    assert skipped["skipped"] is True
