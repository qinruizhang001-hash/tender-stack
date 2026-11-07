#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kb_regraph.py — 重生成条款决策图（含门禁边）。

两种用法：

  1) 从已有导出产物重挂门禁边（不重解析 PDF）：
     python -m scripts.kb_regraph --doc-hash <hash> \
         --requirements out/<arm>/kb/requirements.jsonl \
         --gate out/<arm>/gated/requirements.graded.jsonl \
         --ledger out/<arm>/gated/requirements.csv

  2) 整单重跑（重新解析 + 建图 + 挂门禁）：
     python -m scripts.kb_regraph --pdf 标书.pdf --out out/<arm>/kb \
         --gate out/<arm>/gated/requirements.graded.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "adapter"))

import decision_graph as dg  # noqa: E402


def patch_existing(doc_hash: str, requirements: str | Path, gate: str | Path, ledger: str | Path) -> dict:
    """在已建好的图上补 reviewed_as（按原文把 ledger 的 R-xxx 映射到导出 id）。"""
    sys.path.insert(0, str(ROOT / "scripts"))
    from kb_stage import _gate_pairs

    graph = dg.load(doc_hash)
    if graph is None:
        return {"ok": False, "stage": "graph", "errors": ["找不到该 hash 的图，请先用 --pdf 整单重建"]}
    pairs = _gate_pairs(gate, ledger, requirements)
    if not pairs:
        return {"ok": False, "stage": "mapping", "errors": ["gate 行与导出 requirement 未对齐"]}
    dg.apply_gate(graph, pairs)
    path = dg.save(graph)
    gate_edges = sum(1 for edge in graph["edges"] if edge["kind"] == "reviewed_as")
    return {"ok": True, "path": str(path), "pairs": len(pairs), "reviewed_as": gate_edges}


def rebuild(pdf: str | Path, out: str | Path, gate: str | Path | None) -> dict:
    sys.path.insert(0, str(ROOT / "scripts"))
    import kb_stage

    return kb_stage.run(pdf, out, gate_path=gate)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--doc-hash", default="")
    parser.add_argument("--requirements", default="")
    parser.add_argument("--gate", default="")
    parser.add_argument("--ledger", default="")
    parser.add_argument("--pdf", default="")
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)

    if args.pdf and args.out:
        result = rebuild(args.pdf, args.out, args.gate or None)
    elif args.doc_hash and args.requirements and args.gate:
        ledger = args.ledger or str(Path(args.gate).parent / "requirements.csv")
        result = patch_existing(args.doc_hash, args.requirements, args.gate, ledger)
    else:
        parser.error("需要 --pdf/--out 或 --doc-hash/--requirements/--gate")

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
