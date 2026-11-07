#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R7 接线：doc_hash 贯穿、context 同源、澄清写入。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "adapter"))
sys.path.insert(0, str(ROOT / "scripts"))

import decision_graph as dg  # noqa: E402
import kb as kb_module  # noqa: E402

DOC = "b" * 64


def _graph(tmp_path) -> None:
    rows = [{"id": "r1", "kind": "text", "text": "投标人须提供营业执照", "display_text": "1. 投标人须提供营业执照",
             "number": "1", "section": "第一章", "page": 3, "pages": [3], "source_sha256": DOC}]
    sections = [{"id": "c1", "section": "第一章", "pages": [3], "text": "正文"}]
    dg.save(dg.build_from_export(rows, sections, doc_hash=DOC), tmp_path)


def test_澄清写入后仍是_pending(tmp_path, monkeypatch):
    import kb_clarify

    monkeypatch.setattr(dg, "GRAPH_DIR", tmp_path)
    _graph(tmp_path)
    result = kb_clarify.add_clarification(DOC, "r1", "pending", "待补社保凭证", "张三")
    assert result["ok"] is True
    graph = dg.load(DOC, tmp_path)
    assert any(node["kind"] == "clarification" for node in graph["nodes"])
    assert dg.decision_status(graph, "r1") == "pending"
    assert dg.summary(graph)["counts"]["accepted"] == 0


def test_澄清必须署名(tmp_path, monkeypatch):
    import kb_clarify

    monkeypatch.setattr(dg, "GRAPH_DIR", tmp_path)
    _graph(tmp_path)
    result = kb_clarify.add_clarification(DOC, "r1", "accepted", "x", "")
    assert result["ok"] is False and result["stage"] == "by"


def test_澄清_accept_才升级(tmp_path, monkeypatch):
    import kb_clarify

    monkeypatch.setattr(dg, "GRAPH_DIR", tmp_path)
    _graph(tmp_path)
    kb_clarify.add_clarification(DOC, "r1", "accepted", "已澄清", "李四")
    assert dg.decision_status(dg.load(DOC, tmp_path), "r1") == "accepted"


def test_服务端两条路径同源(monkeypatch):
    import os

    os.environ["WORKBENCH_AUTOSTART"] = "0"
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "workbench"))
    server = pytest.importorskip("server")

    calls: list[dict] = []

    def fake_context(rows=None, doc_hash="", keywords=None, k=3):
        calls.append({"doc_hash": doc_hash, "keywords": keywords})
        return {"source": "graph", "items": [], "graph_neighbors": [{"id": "chunk:c1"}], "allow_llm": False,
                "no_graph_evidence": False}

    monkeypatch.setattr(kb_module, "context", fake_context)
    panel = server.kb_context([{"requirement": "保证金"}], DOC)
    api = server.api_kb_context(q="保证金", doc_hash=DOC)
    assert panel["source"] == "graph" and panel["graph_neighbors"]
    assert api["source"] == "graph"
    assert calls[0]["doc_hash"] == DOC and calls[1]["doc_hash"] == DOC

def test_apply_gate_只连图上节点(tmp_path):
    _graph(tmp_path)
    graph = dg.load(DOC, tmp_path)
    dg.apply_gate(graph, [("r1", "pass"), ("ghost", "fail")])
    assert any(node["kind"] == "gate_result" for node in graph["nodes"])
    assert any(edge["kind"] == "reviewed_as" and edge["from"] == "req:r1" for edge in graph["edges"])
    assert not any(edge["to"].startswith("req:ghost") for edge in graph["edges"])


def test_gate_pairs_按原文对齐(tmp_path):
    import kb_stage

    req = tmp_path / "requirements.jsonl"
    req.write_text(json.dumps({"id": "abc123", "text": "投标人须提供营业执照", "number": "1"}, ensure_ascii=False) + "\n", encoding="utf-8")
    graded = tmp_path / "gated" / "requirements.graded.jsonl"
    graded.parent.mkdir()
    graded.write_text(json.dumps({"req_id": "R-001", "status": "通过"}, ensure_ascii=False) + "\n", encoding="utf-8")
    ledger = graded.parent / "requirements.csv"
    ledger.write_text("req_id,requirement\nR-001,投标人须提供营业执照\n", encoding="utf-8-sig")

    pairs = kb_stage._gate_pairs(graded, ledger, req)
    assert pairs == [("abc123", "pass")]

def _synthetic_pdf(tmp_path) -> Path:
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((60, 80), "第一章 投标邀请", fontname="china-s", fontsize=18)
    page.insert_text((60, 120), "1.投标人须提供营业执照。", fontname="china-s", fontsize=12)
    path = tmp_path / "doc.pdf"
    doc.save(path)
    doc.close()
    return path


def test_kb_stage_落盘图含门禁边(tmp_path, monkeypatch):
    """不是只 mock：真跑 kb_stage，读磁盘 JSON 断言。"""
    import kb_stage

    monkeypatch.setattr(dg, "GRAPH_DIR", tmp_path / "graph")
    pdf = _synthetic_pdf(tmp_path)
    out = tmp_path / "arm" / "kb"
    gate = tmp_path / "arm" / "gated" / "requirements.graded.jsonl"
    gate.parent.mkdir(parents=True)
    gate.write_text(json.dumps({"req_id": "R-001", "status": "通过"}, ensure_ascii=False) + "\n", encoding="utf-8")
    ledger = gate.parent / "requirements.csv"
    ledger.write_text("req_id,requirement\nR-001,投标人须提供营业执照\n", encoding="utf-8-sig")

    result = kb_stage.run(pdf, out, gate_path=gate)
    assert result["ok"] is True
    doc_hash = result["stats"]["source_sha256"]
    graph = json.loads((tmp_path / "graph" / f"{doc_hash}.json").read_text(encoding="utf-8"))
    assert any(node["kind"] == "gate_result" for node in graph["nodes"])
    assert any(edge["kind"] == "reviewed_as" for edge in graph["edges"]), "磁盘图缺 reviewed_as"


def test_regraph_从已有导出补门禁边(tmp_path, monkeypatch):
    import kb_regraph

    monkeypatch.setattr(dg, "GRAPH_DIR", tmp_path / "graph")
    rows = [{"id": "r1", "kind": "text", "text": "投标人须提供营业执照", "display_text": "1. 投标人须提供营业执照",
             "number": "1", "section": "第一章", "page": 3, "pages": [3]}]
    sections = [{"id": "c1", "section": "第一章", "pages": [3], "text": "正文"}]
    dg.save(dg.build_from_export(rows, sections, doc_hash=DOC), tmp_path / "graph")

    requirements = tmp_path / "requirements.jsonl"
    requirements.write_text(json.dumps({"id": "r1", "text": "投标人须提供营业执照", "number": "1"}, ensure_ascii=False) + "\n", encoding="utf-8")
    gate = tmp_path / "requirements.graded.jsonl"
    gate.write_text(json.dumps({"req_id": "R-001", "status": "通过"}, ensure_ascii=False) + "\n", encoding="utf-8")
    ledger = tmp_path / "requirements.csv"
    ledger.write_text("req_id,requirement\nR-001,投标人须提供营业执照\n", encoding="utf-8-sig")

    result = kb_regraph.patch_existing(DOC, requirements, gate, ledger)
    assert result["ok"] is True and result["reviewed_as"] >= 1


def test_baseline_数值不变不重写(tmp_path, monkeypatch):
    import kb as kb_module

    kb_dir = tmp_path / "kb"
    (kb_dir / "sealed").mkdir(parents=True)
    baseline = kb_dir / "sealed" / "baseline.json"
    baseline.write_text(json.dumps({"sealed_recall": 0.95, "updated_at": "2026-01-01T00:00:00"}), encoding="utf-8")
    monkeypatch.setattr(kb_module, "KB", kb_dir)
    monkeypatch.setattr(kb_module, "_run_pytest", lambda name, timeout=600: (True, "ok"))
    monkeypatch.setattr(kb_module, "_sealed_recall", lambda: 0.95)
    monkeypatch.setenv("TENDER_KB_ALLOW_NO_GATE", "1")
    monkeypatch.delenv("CI", raising=False)

    card = {"case_id": "b1", "source_doc_hash": "e" * 64, "doc_type": "service", "kind": "gold_miss",
            "evidence": "某条款", "lesson": "某结论", "created_at": "2026-11-07", "linked_test": "x"}
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_x.py").write_text("def x():\n    pass\n", encoding="utf-8")
    monkeypatch.setattr(kb_module, "test_exists", lambda name, tests_dir=None: True)

    before = baseline.read_text(encoding="utf-8")
    kb_module.promote(card, approve=True, approved_by="tester", run_gate=False,
                      proposed_dir=tmp_path / "p", approved_dir=tmp_path / "a", tests_dir=tests)
    assert baseline.read_text(encoding="utf-8") == before, "数值没变却改了 baseline"

def test_重导出保留门禁边(tmp_path, monkeypatch):
    import vector_export

    monkeypatch.setattr(dg, "GRAPH_DIR", tmp_path / "graph")
    pdf = _synthetic_pdf(tmp_path)
    first = vector_export.export_chunks(pdf, tmp_path / "a.jsonl", absorb=False)
    graph = dg.load(first["source_sha256"])
    req_id = next(node["requirement_id"] for node in graph["nodes"] if node["kind"] == "requirement")
    dg.apply_gate(graph, [(req_id, "pass")])
    dg.save(graph)

    vector_export.export_chunks(pdf, tmp_path / "b.jsonl", absorb=False)
    rebuilt = dg.load(first["source_sha256"])
    assert any(edge["kind"] == "reviewed_as" for edge in rebuilt["edges"]), "重导出把门禁边冲掉了"


def test_导出可关闭建图(tmp_path, monkeypatch):
    import vector_export

    monkeypatch.setattr(dg, "GRAPH_DIR", tmp_path / "graph")
    pdf = _synthetic_pdf(tmp_path)
    info = vector_export.export_chunks(pdf, tmp_path / "c.jsonl", absorb=False, build_graph=False)
    assert info["graph_build"].startswith("fallback")
    assert not list((tmp_path / "graph").glob("*.json"))

def test_重建台账跑门禁(tmp_path):
    import kb_gate_rebuild

    req = tmp_path / "req.jsonl"
    lines = [
        json.dumps({"id": "r1", "text": "投标人须提供营业执照", "page": 1, "section": "第一章"}, ensure_ascii=False),
        json.dumps({"id": "r2", "text": "投标人须提供社保证明", "page": 2, "section": "第一章"}, ensure_ascii=False),
    ]
    req.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = kb_gate_rebuild.rebuild(req, tmp_path / "gate")
    assert result["ok"] is True
    graded = Path(result["graded"])
    rows = [json.loads(line) for line in graded.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 2
    assert all(row["status"] == "需复核" for row in rows), "无行号必须转需复核，不能伪造 pass"
    meta = json.loads((tmp_path / "gate" / "rebuild_meta.json").read_text(encoding="utf-8"))
    assert meta["note"].startswith("line_no=0")


def test_门禁与原文模糊对齐(tmp_path):
    import kb_stage

    requirements = tmp_path / "requirements.jsonl"
    requirements.write_text(
        json.dumps({"id": "x1", "text": "投标人须提供近三个月社保证明"}, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    gate = tmp_path / "requirements.graded.jsonl"
    gate.write_text(json.dumps({"req_id": "R-001", "status": "通过"}, ensure_ascii=False) + "\n", encoding="utf-8")
    ledger = tmp_path / "requirements.csv"
    ledger.write_text("req_id,requirement\nR-001,投标人须提供近三个月社保证明材料\n", encoding="utf-8-sig")

    pairs = kb_stage._gate_pairs(gate, ledger, requirements)
    assert pairs and pairs[0][0] == "x1" and pairs[0][1] == "pass"


def test_澄清脚本写盘可见(tmp_path, monkeypatch):
    import kb_clarify

    monkeypatch.setattr(dg, "GRAPH_DIR", tmp_path)
    _graph(tmp_path)
    assert kb_clarify.main(["--doc-hash", DOC, "--requirement", "r1", "--status", "pending",
                            "--note", "待澄清", "--by", "张三"]) == 0
    graph = dg.load(DOC, tmp_path)
    nodes = [node for node in graph["nodes"] if node["kind"] == "clarification"]
    assert nodes and nodes[0]["by"] == "张三"
    assert dg.decision_status(graph, "r1") == "pending"
