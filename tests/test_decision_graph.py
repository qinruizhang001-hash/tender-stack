#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""决策图：确定性构建、锚点、边规则、wiki-first、沉默≠接受。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import fitz

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "adapter"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import decision_graph as dg  # noqa: E402

DOC = "a" * 64


def _rows() -> list[dict]:
    return [
        {"id": "r1", "kind": "text", "text": "投标人须具有劳务派遣经营许可证", "display_text": "1. 投标人须具有劳务派遣经营许可证",
         "number": "1", "section": "第一章 > 资格", "page": 3, "pages": [3], "source_sha256": DOC},
        {"id": "r2", "kind": "text", "text": "供应商所投产品符合本国产品标准", "display_text": "1.3 供应商所投产品符合本国产品标准",
         "number": "1.3", "section": "第一章 > 政策", "page": 14, "pages": [14, 15], "source_sha256": DOC},
        {"id": "r3", "kind": "table_row", "text": "12.8.2 | 投标保证金 | □无 □有", "display_text": "12.8.2. 投标保证金",
         "number": "12.8.2", "section": "第二章 > 资料表", "page": 8, "pages": [8], "source_sha256": DOC},
    ]


def _sections() -> list[dict]:
    return [
        {"id": "c1", "section": "第一章 > 资格", "pages": [3], "text": "资格条款正文"},
        {"id": "c2", "section": "第一章 > 政策", "pages": [14, 15], "text": "本国产品政策正文"},
        {"id": "c3", "section": "第二章 > 资料表", "pages": [8], "text": "资料表正文"},
    ]


def test_schema_kinds_锁死():
    assert dg.NODE_KINDS == {"requirement", "table_row", "evidence_chunk", "clarification", "gate_result"}
    assert dg.EDGE_KINDS == {"continues", "cites", "evidences", "supersedes", "reviewed_as"}
    assert dg.SCHEMA_VERSION == 1


def test_节点锚点齐全():
    graph = dg.build_from_export(_rows(), _sections(), doc_hash=DOC)
    assert graph["schema_version"] == 1
    req = next(node for node in graph["nodes"] if node["id"] == "req:r1")
    for key in ("doc_hash", "page", "requirement_id", "text", "section"):
        assert req.get(key) not in (None, ""), key
    assert any(node["kind"] == "table_row" for node in graph["nodes"])
    assert any(node["kind"] == "evidence_chunk" for node in graph["nodes"])


def test_evidences_与_continues():
    graph = dg.build_from_export(_rows(), _sections(), doc_hash=DOC)
    edges = graph["edges"]
    assert any(edge["kind"] == "evidences" and edge["from"] == "req:r1" and edge["to"] == "chunk:c1" for edge in edges)
    # r2 跨页 14/15 → continues 到后续页 chunk
    assert any(edge["kind"] == "continues" and edge["from"] == "req:r2" and edge["page"] == 15 for edge in edges)


def test_cites_表行指向邻近条款():
    graph = dg.build_from_export(_rows(), _sections(), doc_hash=DOC)
    cites = [edge for edge in graph["edges"] if edge["kind"] == "cites" and edge["from"] == "req:r3"]
    assert cites == [], "没有同 section 的条款时不得乱连"

    rows = _rows()
    rows.append({"id": "r4", "kind": "text", "text": "资料表说明", "display_text": "2. 资料表说明",
                 "number": "2", "section": "第二章 > 资料表", "page": 8, "pages": [8], "source_sha256": DOC})
    graph = dg.build_from_export(rows, _sections(), doc_hash=DOC)
    assert any(edge["kind"] == "cites" and edge["from"] == "req:r3" and edge["to"] == "req:r4" for edge in graph["edges"])


def test_reviewed_as_来自_gate_行():
    gates = [{"req_id": "r1", "status": "pass"}, {"req_id": "r2", "status": "unknown"}]
    graph = dg.build_from_export(_rows(), _sections(), gate_rows=gates, doc_hash=DOC)
    assert any(node["kind"] == "gate_result" and node["status"] == "pass" for node in graph["nodes"])
    assert any(edge["kind"] == "reviewed_as" and edge["from"] == "req:r1" for edge in graph["edges"])


def test_supersedes_边():
    graph = dg.build_from_export(_rows(), _sections(), supersedes=[{"old": "r1", "new": "r2"}], doc_hash=DOC)
    edge = next(edge for edge in graph["edges"] if edge["kind"] == "supersedes")
    assert edge["from"] == "req:r2" and edge["to"] == "req:r1"


def test_neighbours_一跳():
    graph = dg.build_from_export(_rows(), _sections(), doc_hash=DOC)
    payload = dg.neighbours(graph, "req:r1")
    assert payload["node"]["id"] == "req:r1"
    assert any(node["id"] == "chunk:c1" for node in payload["neighbours"])
    missing = dg.neighbours(graph, "req:nope")
    assert missing["node"] is None and missing["source"] == "none"


def test_保存与加载(tmp_path):
    graph = dg.build_from_export(_rows(), _sections(), doc_hash=DOC)
    path = dg.save(graph, tmp_path)
    assert path.name == f"{DOC}.json"
    loaded = dg.load(DOC, tmp_path)
    assert loaded["counts"] == graph["counts"]


def test_沉默不等于接受():
    graph = dg.build_from_export(_rows(), _sections(), doc_hash=DOC)
    for req in ("r1", "r2", "r3"):
        assert dg.decision_status(graph, req) == "unknown"
    result = dg.summary(graph)
    assert result["counts"]["accepted"] == 0
    assert result["claim"] != "全部满足"
    assert "未澄清" in result["claim"] or "沉默" in result["claim"]


def test_pending_澄清仍是_pending():
    graph = dg.build_from_export(
        _rows(), _sections(), doc_hash=DOC,
        clarifications=[{"requirement_id": "r1", "status": "pending", "note": "待供应商澄清", "page": 3}],
    )
    assert dg.decision_status(graph, "r1") == "pending"
    assert dg.summary(graph)["counts"]["accepted"] == 0


def test_accepted_澄清才标_accepted():
    graph = dg.build_from_export(
        _rows(), _sections(), doc_hash=DOC,
        clarifications=[{"requirement_id": "r1", "status": "accepted", "note": "已澄清", "page": 3}],
    )
    assert dg.decision_status(graph, "r1") == "accepted"
    assert dg.decision_status(graph, "r2") == "unknown"
    assert dg.summary(graph)["claim"] != "全部满足"


def test_wiki_first_图优先(monkeypatch):
    graph = dg.build_from_export(_rows(), _sections(), doc_hash=DOC)
    dg.save(graph)
    import kb as kb_module

    called = {"n": 0}

    def fake_retrieve(*args, **kwargs):
        called["n"] += 1
        return {"items": [], "fallback": ""}

    monkeypatch.setattr(kb_module, "retrieve", fake_retrieve)
    result = dg.query_wiki_first(requirement_id="r1", doc_hash=DOC)
    assert result["source"] == "graph"
    assert result["items"]
    assert result["allow_llm"] is False
    assert called["n"] == 0


def test_wiki_first_降级到_kb_与空(monkeypatch, tmp_path):
    import kb as kb_module

    monkeypatch.setattr(kb_module, "retrieve", lambda **kwargs: {"items": [{"case_id": "x"}], "fallback": ""})
    result = dg.query_wiki_first(keywords=["保证金"], doc_hash="", graph_root=tmp_path)
    assert result["source"] == "kb"
    assert result["no_graph_evidence"] is True

    monkeypatch.setattr(kb_module, "retrieve", lambda **kwargs: {"items": [], "fallback": ""})
    empty = dg.query_wiki_first(keywords=["不存在"], graph_root=tmp_path)
    assert empty["source"] == "none"
    assert empty["allow_llm"] is True


def test_图构建失败不影响导出(tmp_path, monkeypatch):
    import vector_export

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((60, 80), "第一章 投标邀请", fontname="china-s", fontsize=18)
    page.insert_text((60, 120), "1.投标人须提供营业执照。", fontname="china-s", fontsize=12)
    pdf = tmp_path / "x.pdf"
    doc.save(pdf)
    doc.close()

    monkeypatch.setattr(dg, "build_from_export", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("graph boom")))
    info = vector_export.export_chunks(pdf, tmp_path / "out.jsonl", absorb=False)
    assert info["graph_build"].startswith("fallback")
    assert info["requirements"] >= 1

def test_空hash拒写图(tmp_path):
    rows = [{key: value for key, value in row.items() if key != "source_sha256"} for row in _rows()]
    graph = dg.build_from_export(rows, _sections(), doc_hash="")
    assert dg.save(graph, tmp_path) is None
    assert list(tmp_path.glob("*.json")) == []


def test_带gate行导出图含reviewed_as(tmp_path, monkeypatch):
    import vector_export

    monkeypatch.setattr(dg, "GRAPH_DIR", tmp_path)
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((60, 80), "第一章 投标邀请", fontname="china-s", fontsize=18)
    page.insert_text((60, 120), "1.投标人须提供营业执照。", fontname="china-s", fontsize=12)
    pdf = tmp_path / "x.pdf"
    doc.save(pdf)
    doc.close()

    info = vector_export.export_chunks(pdf, tmp_path / "out.jsonl", absorb=False)
    assert info["graph_build"] == "ok"
    graph = json.loads((tmp_path / f"{info['source_sha256']}.json").read_text(encoding="utf-8"))

    # gate 行按导出 id 对齐后补图
    req_id = next(node["requirement_id"] for node in graph["nodes"] if node["kind"] == "requirement")
    dg.apply_gate(graph, [(req_id, "pass"), ("ghost", "fail")])
    assert any(node["kind"] == "gate_result" for node in graph["nodes"])
    assert any(edge["kind"] == "reviewed_as" and edge["from"] == f"req:{req_id}" for edge in graph["edges"])
    assert not any(edge["from"] == "req:ghost" for edge in graph["edges"])


def test_context_无hash不读图(monkeypatch, tmp_path):
    import kb as kb_module

    monkeypatch.setattr(dg, "GRAPH_DIR", tmp_path)
    dg.save(dg.build_from_export(_rows(), _sections(), doc_hash=DOC), tmp_path)
    monkeypatch.setattr(kb_module, "retrieve", lambda *a, **k: {"items": [{"case_id": "k1"}], "fallback": ""})

    result = kb_module.context([{"requirement": "投标保证金"}], doc_hash="")
    assert result["source"] == "kb"
    assert result["graph_neighbors"] == []


def test_context_有hash走图(monkeypatch, tmp_path):
    import kb as kb_module

    monkeypatch.setattr(dg, "GRAPH_DIR", tmp_path)
    dg.save(dg.build_from_export(_rows(), _sections(), doc_hash=DOC), tmp_path)
    result = kb_module.context([{"requirement": "投标保证金"}], doc_hash=DOC)
    assert result["source"] == "graph"
    assert result["graph_neighbors"]
