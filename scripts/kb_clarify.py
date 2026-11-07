#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kb_clarify.py — 最小 HITL 入口：把一条澄清写进条款决策图。

  python -m scripts.kb_clarify --doc-hash <hash> --requirement R-012 \
      --status pending --note "待供应商补充社保凭证" --by 张三

规则：
- 必须给 doc_hash 与 by（人工署名）；
- status 只能是 accepted / pending / rejected；
- 只写图，不改 gold、不改台账。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "adapter"))

import decision_graph as dg  # noqa: E402


def add_clarification(doc_hash: str, requirement_id: str, status: str, note: str, by: str) -> dict:
    if not doc_hash.strip():
        return {"ok": False, "stage": "doc_hash", "errors": ["doc_hash 必填"]}
    if not by.strip():
        return {"ok": False, "stage": "by", "errors": ["--by 必填"]}
    if status not in dg.CLARIFICATION_STATUS:
        return {"ok": False, "stage": "status", "errors": [f"status 非法：{status}"]}

    graph = dg.load(doc_hash)
    if graph is None:
        return {"ok": False, "stage": "graph", "errors": ["找不到该文档的图"]}

    node_id = f"clar:{dg._hash(doc_hash, requirement_id, note, by)}"
    graph["nodes"].append(
        dg._node(
            "clarification",
            node_id,
            doc_hash=doc_hash,
            page=None,
            requirement_id=requirement_id,
            status=status,
            note=note[:200],
            by=by,
        )
    )
    graph["edges"].append(dg._edge("reviewed_as", f"req:{requirement_id}", node_id, status=status))
    graph["counts"] = {"nodes": len(graph["nodes"]), "edges": len(graph["edges"])}
    path = dg.save(graph)
    return {"ok": True, "node": node_id, "path": str(path), "status": dg.decision_status(graph, requirement_id)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--doc-hash", required=True)
    parser.add_argument("--requirement", required=True)
    parser.add_argument("--status", default="pending", choices=sorted(dg.CLARIFICATION_STATUS))
    parser.add_argument("--note", default="")
    parser.add_argument("--by", required=True)
    args = parser.parse_args(argv)

    result = add_clarification(args.doc_hash, args.requirement, args.status, args.note, args.by)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
