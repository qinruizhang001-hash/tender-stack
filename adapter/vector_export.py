#!/usr/bin/env python3
# LAYER: load-bearing
# -*- coding: utf-8 -*-
"""vector_export.py — 把切块结果导出成向量库入库用的 JSONL。

默认导出 **requirement 级**（单条要求条款），章节块另存 `sections.jsonl`
作为父上下文；id 用「章节 + 条款号 + 原文」稳定哈希，重跑不变。
降级信息随 stats 一起返回，不静默。
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _resolve_gold(source: Path) -> Path | None:
    """按 PDF 路径在 contract/gold/index.json 里找对应金标。"""
    index = Path(__file__).resolve().parent.parent / "contract" / "gold" / "index.json"
    if not index.exists():
        return None
    try:
        entries = json.loads(index.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    root = index.parent.parent.parent
    for entry in entries:
        if (root / entry["pdf"]).resolve() == source.resolve():
            return root / entry["gold"]
    return None


def export_chunks(
    source: str | Path,
    target: str | Path,
    level: str = "requirement",
    absorb: bool | None = None,
    gate_rows: list[dict] | None = None,
    clarifications: list[dict] | None = None,
    build_graph: bool | None = None,
) -> dict:
    from bid_chunker import chunk_pdf
    from layout_parse import extract_requirements

    import hashlib

    source, target = Path(source), Path(target)
    chunks, stats = chunk_pdf(source)
    source_sha256 = ""
    try:
        digest = hashlib.sha256()
        with source.open("rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
        source_sha256 = digest.hexdigest()
    except OSError:
        source_sha256 = ""

    sections = []
    for index, chunk in enumerate(chunks):
        text = (chunk.get("text") or "").strip()
        if not text:
            continue
        sections.append(
            {
                "id": hashlib.sha1(f"{source.name}|{chunk.get('section', '')}|{index}".encode()).hexdigest()[:16],
                "text": text,
                "section": chunk.get("section", ""),
                "pages": chunk.get("pages") or [chunk.get("page")],
                "kind": chunk.get("kind", "section"),
                "has_table": bool(chunk.get("table")),
                "quality": chunk.get("quality", "ok"),
                "quality_reason": chunk.get("quality_reason", ""),
                "source": source.name,
                "chars": len(text),
            }
        )

    requirements = []
    for item in extract_requirements(chunks):
        requirements.append(
            {
                "id": item["id"],
                "text": item["text"],
                "display_text": item.get("display_text") or item["text"],
                "kind": item.get("kind", "text"),
                "number": item["number"],
                "table_header": item.get("table_header", []),
                "row_index": item.get("row_index", 0),
                "section": item["section"],
                "page": item["page"],
                "pages": item["pages"],
                "quality": item.get("quality", "ok"),
                "quality_reason": item.get("quality_reason", ""),
                "source": source.name,
                "source_sha256": source_sha256,
                "chars": len(item["text"]),
            }
        )

    sections_path = target.with_name(target.stem + ".sections.jsonl")
    rows: list[dict] = []
    if level in {"requirement", "both"}:
        _write(target, requirements)
        rows = requirements
    if level in {"section", "both"}:
        _write(sections_path if level == "both" else target, sections)
        if level == "section":
            rows = sections
    else:
        _write(sections_path, sections)

    # 决策图：确定性构建，失败只记 fallback
    graph_stats: dict = {"status": "skipped"}
    want_graph = build_graph
    if want_graph is None:
        want_graph = (os.environ.get("TENDER_GRAPH") or "1").strip() not in {"0", "false", "no"}
    try:
        if not want_graph:
            raise RuntimeError("graph_disabled")
        import sys as _sys

        _sys.path.insert(0, str(Path(__file__).resolve().parent))
        from decision_graph import build_from_export, load as load_graph, preserve_gate_edges, save as save_graph

        graph = build_from_export(
            requirements, sections, gate_rows=gate_rows, clarifications=clarifications, doc_hash=source_sha256
        )
        graph = preserve_gate_edges(load_graph(source_sha256), graph)
        path = save_graph(graph)
        if path is None:
            graph_stats = {"status": "fallback: empty_doc_hash"}
        else:
            graph_stats = {"status": "ok", "path": str(path), "nodes": graph["counts"]["nodes"], "edges": graph["counts"]["edges"]}
    except Exception as error:  # noqa: BLE001 - 图失败不影响导出
        reason = "graph_disabled" if str(error) == "graph_disabled" else f"{type(error).__name__}: {error}"
        graph_stats = {"status": f"fallback: {reason}"}

    # KB 吸收：默认开（TENDER_KB_ABSORB），失败只记 fallback，不影响导出
    kb_stats: dict = {}
    want_absorb = absorb
    if want_absorb is None:
        want_absorb = (os.environ.get("TENDER_KB_ABSORB") or "1").strip() not in {"0", "false", "no"}
    if want_absorb:
        try:
            import sys as _sys

            _sys.path.insert(0, str(Path(__file__).resolve().parent))
            from kb import absorb as kb_absorb

            gold = _resolve_gold(source)
            if gold and gold.exists():
                kb_stats = kb_absorb(target, gold, source_pdf=source)
            else:
                kb_stats = {"absorbed": 0, "kb_fallback": "gold_not_found"}
        except Exception as error:  # noqa: BLE001
            kb_stats = {"absorbed": 0, "kb_fallback": f"{type(error).__name__}: {error}"}

    fallbacks = stats.get("ocr_fallback") or []
    return {
        "level": level,
        "requirements": len(requirements),
        "table_rows": sum(1 for row in requirements if row.get("kind") == "table_row"),
        "sections": len(sections),
        "table_sections": sum(1 for row in sections if row["has_table"]),
        "low_quality": sum(1 for row in rows if row.get("quality") == "low"),
        "chars": sum(row["chars"] for row in rows),
        "source_sha256": source_sha256,
        "backend": stats.get("backend") or stats.get("mode"),
        "toc_pages": stats.get("toc_pages") or [],
        "table_suspect": stats.get("table_suspect", 0),
        "ocr_fallback": fallbacks[:5],
        "ocr_fallback_count": len(fallbacks),
        "kb_absorbed_count": kb_stats.get("absorbed", 0),
        "kb_fallback": kb_stats.get("kb_fallback", ""),
        "kb_sealed": kb_stats.get("sealed", False),
        "graph_build": graph_stats.get("status", ""),
        "graph_nodes": graph_stats.get("nodes", 0),
        "graph_edges": graph_stats.get("edges", 0),
        "layer_calls": {
            "text": sum(1 for row in requirements if row.get("kind") == "text"),
            "table_row": sum(1 for row in requirements if row.get("kind") == "table_row"),
            "sections": len(sections),
            "ocr_fallback": len(fallbacks),
            "kb_absorbed": kb_stats.get("absorbed", 0),
        },
        "target": str(target),
        "sections_target": str(sections_path),
    }


if __name__ == "__main__":
    level = "requirement"
    args = [arg for arg in sys.argv[1:] if not arg.startswith("--")]
    if "--sections" in sys.argv:
        level = "section"
    if "--both" in sys.argv:
        level = "both"
    print(json.dumps(export_chunks(args[0], args[1], level=level), ensure_ascii=False))
