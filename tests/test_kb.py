#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""案例知识库：吸收、检索降级、受控晋升。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "adapter"))

import kb  # noqa: E402


def _export(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")


def test_吸收写入_inbox(tmp_path):
    export = tmp_path / "req.jsonl"
    _export(
        export,
        [
            {"id": "r1", "text": "投标人须具有劳务派遣经营许可证", "display_text": "1. 投标人须具有劳务派遣经营许可证", "quality": "ok", "page": 3},
            {"id": "r2", "text": "待人工确认的碎片", "display_text": "待人工确认的碎片", "quality": "low", "page": 4},
        ],
    )
    gold = tmp_path / "gold.json"
    gold.write_text(
        json.dumps([{"id": "G01", "page": 3, "text": "投标人须具有劳务派遣经营许可证"}, {"id": "G02", "page": 9, "text": "投标保证金不予退还的情形"}], ensure_ascii=False),
        encoding="utf-8",
    )
    out = tmp_path / "inbox"

    result = kb.absorb(export, gold, out, doc_type="service", source_hash="a" * 64, sealed_index=tmp_path / "none.json")
    assert result["absorbed"] == 2, result   # 1 gold_miss + 1 rule_gap
    cards = [json.loads(line) for line in path_lines(out)]
    assert cards and all(card["source_doc_hash"] == "a" * 64 for card in cards)
    kinds = {card["kind"] for card in cards}
    assert "gold_miss" in kinds and "rule_gap" in kinds
    for card in cards:
        assert card["lesson"] and card["created_at"] and card["patch_candidate"]["status"] == "draft"


def path_lines(folder: Path):
    for path in folder.glob("*.jsonl"):
        yield from path.read_text(encoding="utf-8").splitlines()


def test_sealed_文档不被吸收(tmp_path):
    export = tmp_path / "req.jsonl"
    _export(export, [{"id": "r1", "text": "投标人须具有许可证", "quality": "ok", "page": 1}])
    gold = tmp_path / "gold.json"
    gold.write_text(json.dumps([{"page": 9, "text": "某条缺失条款"}], ensure_ascii=False), encoding="utf-8")
    sealed = tmp_path / "sealed.json"
    sealed.write_text(json.dumps({"doc_hashes": ["b" * 64]}), encoding="utf-8")

    result = kb.absorb(export, gold, tmp_path / "inbox", source_hash="b" * 64, sealed_index=sealed)
    assert result["sealed"] is True
    assert result["absorbed"] == 0


def test_检索默认可用_索引缺失时降级(tmp_path, monkeypatch):
    monkeypatch.delenv("TENDER_KB_RETRIEVE", raising=False)
    empty = kb.retrieve(doc_type="service", keywords=["社保"], kb_dir=tmp_path / "missing")
    assert empty["items"] == []
    assert empty["fallback"] == ""

    monkeypatch.setenv("TENDER_KB_RETRIEVE", "0")
    disabled = kb.retrieve(kb_dir=tmp_path)
    assert disabled["fallback"] == "disabled"


def test_检索命中已确认案例(tmp_path):
    folder = tmp_path / "approved"
    folder.mkdir()
    card = {
        "case_id": "c1", "source_doc_hash": "a" * 64, "doc_type": "service", "kind": "gold_miss",
        "evidence": "投标保证金不予退还", "lesson": "勾选行未合并", "created_at": "2026-11-06", "page": 8,
    }
    (folder / "cases.jsonl").write_text(json.dumps(card, ensure_ascii=False) + "\n", encoding="utf-8")
    result = kb.retrieve(doc_type="service", keywords=["保证金"], kb_dir=folder)
    assert len(result["items"]) == 1
    assert result["items"][0]["lesson"] == "勾选行未合并"


def test_晋升缺少_linked_test_被拒(tmp_path):
    card = {
        "case_id": "x1", "source_doc_hash": "a" * 64, "doc_type": "service", "kind": "gold_miss",
        "evidence": "某条款", "lesson": "某结论", "created_at": "2026-11-06", "linked_test": "",
    }
    result = kb.promote(card, run_gate=False, proposed_dir=tmp_path / "p", approved_dir=tmp_path / "a", tests_dir=tmp_path / "t")
    assert result["ok"] is False
    assert result["stage"] == "linked_test"


def test_生产路径禁止跳过门禁(tmp_path, monkeypatch):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_demo.py").write_text("def test_case_link():\n    assert True\n", encoding="utf-8")
    card = {
        "case_id": "x0", "source_doc_hash": "a" * 64, "doc_type": "service", "kind": "gold_miss",
        "evidence": "某条款", "lesson": "某结论", "created_at": "2026-11-06", "linked_test": "test_case_link",
    }
    monkeypatch.delenv("TENDER_KB_ALLOW_NO_GATE", raising=False)
    result = kb.promote(card, run_gate=False, proposed_dir=tmp_path / "p", approved_dir=tmp_path / "a", tests_dir=tests)
    assert result["ok"] is False
    assert result["stage"] == "gate_required"


def test_晋升生成补丁草稿(tmp_path, monkeypatch):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_demo.py").write_text("def test_case_link():\n    assert True\n", encoding="utf-8")
    monkeypatch.setenv("TENDER_KB_ALLOW_NO_GATE", "1")
    monkeypatch.delenv("CI", raising=False)
    card = {
        "case_id": "x2", "source_doc_hash": "a" * 64, "doc_type": "service", "kind": "gold_miss",
        "evidence": "某条款", "lesson": "某结论", "created_at": "2026-11-06", "linked_test": "test_case_link",
        "patch_candidate": {"type": "gold", "status": "draft", "content": "某条款"},
    }
    result = kb.promote(
        card, approve=False, run_gate=False,
        proposed_dir=tmp_path / "p", approved_dir=tmp_path / "a", gold_dir=tmp_path / "g", tests_dir=tests,
    )
    assert result["ok"] is True
    assert Path(result["patch"]).exists()
    assert json.loads(Path(result["patch"]).read_text(encoding="utf-8"))["case_id"] == "x2"


def test_批准必须署名(tmp_path, monkeypatch):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_demo.py").write_text("def test_case_link():\n    assert True\n", encoding="utf-8")
    monkeypatch.setenv("TENDER_KB_ALLOW_NO_GATE", "1")
    card = {
        "case_id": "x4", "source_doc_hash": "a" * 64, "doc_type": "service", "kind": "gold_miss",
        "evidence": "某条款", "lesson": "某结论", "created_at": "2026-11-06", "linked_test": "test_case_link",
    }
    result = kb.promote(card, approve=True, approved_by="unknown", run_gate=False,
                        proposed_dir=tmp_path / "p", approved_dir=tmp_path / "a", tests_dir=tests)
    assert result["ok"] is False and result["stage"] == "approved_by"


def test_批准写入_approved_但不自动写_gold(tmp_path, monkeypatch):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_demo.py").write_text("def test_case_link():\n    assert True\n", encoding="utf-8")
    monkeypatch.setenv("TENDER_KB_ALLOW_NO_GATE", "1")
    monkeypatch.setattr(kb, "_run_pytest", lambda name, timeout=600: (True, "ok"))
    card = {
        "case_id": "x3", "source_doc_hash": "c" * 64, "doc_type": "service", "kind": "gold_miss",
        "evidence": "某条款", "lesson": "某结论", "created_at": "2026-11-06", "linked_test": "test_case_link",
        "patch_candidate": {"type": "gold", "status": "draft", "content": "某条款"},
    }
    result = kb.promote(
        card, approve=True, approved_by="tester", run_gate=False,
        proposed_dir=tmp_path / "p", approved_dir=tmp_path / "a", gold_dir=tmp_path / "g", tests_dir=tests,
    )
    assert result["ok"] is True
    assert "x3" in (tmp_path / "a" / "cases.jsonl").read_text(encoding="utf-8")
    assert not (tmp_path / "g").exists(), "approve 不应自动写 gold 文件"




def test_sealed_recall_按金标计算(tmp_path, monkeypatch):
    import vector_export
    import eval_chunks

    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    gold = tmp_path / "g.json"
    gold.write_text('[{"text":"abc"}]', encoding="utf-8")
    index = tmp_path / "index.json"
    index.write_text(json.dumps({"doc_hashes": [], "gold_files": [{"pdf": str(pdf), "gold": str(gold)}]}), encoding="utf-8")

    monkeypatch.setattr(kb, "SEALED_INDEX", index)
    monkeypatch.setattr(vector_export, "export_chunks", lambda *a, **k: {"requirements": 1})
    monkeypatch.setattr(eval_chunks, "evaluate", lambda *a, **k: {"total": 10, "covered": 9})

    assert kb._sealed_recall() == 0.9



def test_sealed_卡片晋升被拒(monkeypatch, tmp_path):
    monkeypatch.setenv("TENDER_KB_ALLOW_NO_GATE", "1")
    card = {
        "case_id": "s1", "source_doc_hash": "b" * 64, "doc_type": "service", "kind": "gold_miss",
        "evidence": "某条款", "lesson": "某结论", "created_at": "2026-11-06", "linked_test": "test_case_link",
    }
    monkeypatch.setattr(kb, "sealed_hashes", lambda *args, **kwargs: {"b" * 64})
    result = kb.promote(card, run_gate=False, proposed_dir=tmp_path / "p", approved_dir=tmp_path / "a")
    assert result["ok"] is False and result["stage"] == "sealed"

def test_sealed_recall_下降则拒绝批准(tmp_path, monkeypatch):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_demo.py").write_text("def test_case_link():\n    assert True\n", encoding="utf-8")
    monkeypatch.setenv("TENDER_KB_ALLOW_NO_GATE", "1")
    monkeypatch.setattr(kb, "_run_pytest", lambda name, timeout=600: (True, "ok"))
    monkeypatch.setattr(kb, "_sealed_recall", lambda: 0.5)

    kb_dir = tmp_path / "kb"
    (kb_dir / "sealed").mkdir(parents=True)
    (kb_dir / "sealed" / "baseline.json").write_text(json.dumps({"sealed_recall": 0.9}), encoding="utf-8")
    monkeypatch.setattr(kb, "KB", kb_dir)

    card = {
        "case_id": "s2", "source_doc_hash": "d" * 64, "doc_type": "service", "kind": "gold_miss",
        "evidence": "某条款", "lesson": "某结论", "created_at": "2026-11-06", "linked_test": "test_case_link",
    }
    result = kb.promote(card, approve=True, approved_by="tester", run_gate=False,
                        proposed_dir=tmp_path / "p", approved_dir=tmp_path / "a", tests_dir=tests)
    assert result["ok"] is False
    assert result["stage"] == "sealed_regression"
