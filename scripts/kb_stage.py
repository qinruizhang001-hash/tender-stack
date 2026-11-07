#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kb_stage.py — 工作台流水线里的「知识沉淀」阶段。

调用 vector_export 导出 requirement 并按 TENDER_KB_ABSORB 吸收到 kb/inbox。
任何失败只返回 fallback，不抛错（不能炸整单审查）。

用法：python scripts/kb_stage.py --pdf 标书.pdf --out out/arm/kb [--doc-type service]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "adapter"))


def _gate_rows(gate_path: str | Path | None) -> list[dict]:
    if not gate_path:
        return []
    path = Path(gate_path)
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        status = str(row.get("status") or "").strip()
        mapped = "pass" if status in {"通过", "已响应"} else "fail" if status in {"阻断", "不通过"} else "unknown"
        rows.append({"req_id": row.get("req_id"), "status": mapped})
    return rows


def _status(value: str) -> str:
    value = (value or "").strip()
    if value in {"通过", "已响应"}:
        return "pass"
    if value in {"阻断", "不通过"}:
        return "fail"
    return "unknown"


def _gate_pairs(gate_path: str | Path | None, ledger_path: str | Path | None, requirements_path: str | Path) -> list[tuple[str, str]]:
    """把 ledger 的 R-xxx 状态映射到导出 requirement 的 id（按原文对齐）。"""
    import csv
    import re

    if not gate_path or not requirements_path:
        return []
    gate_file = Path(gate_path)
    ledger_file = Path(ledger_path) if ledger_path else gate_file.parent / "requirements.csv"
    if not gate_file.exists() or not ledger_file.exists():
        return []

    def norm(value: str) -> str:
        return re.sub(r"\s+", "", value or "")

    requirements = []
    for line in Path(requirements_path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            requirements.append(json.loads(line))

    with ledger_file.open(encoding="utf-8-sig") as handle:
        ledger = {row["req_id"]: row.get("requirement", "") for row in csv.DictReader(handle)}

    import difflib

    def best_match(text: str) -> str | None:
        probe = text[:120]
        best: tuple[float, str] | None = None
        for row in requirements:
            body = norm(row.get("text", ""))
            if not body:
                continue
            if probe[:40] in body or body[:40] in text:
                return row["id"]
            ratio = difflib.SequenceMatcher(None, probe[:120], body[:120]).ratio()
            if ratio >= 0.72 and (best is None or ratio > best[0]):
                best = (ratio, row["id"])
        return best[1] if best else None

    pairs: list[tuple[str, str]] = []
    for line in gate_file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        graded = json.loads(line)
        text = norm(ledger.get(graded.get("req_id"), ""))
        if not text:
            continue
        matched = best_match(text)
        if matched:
            pairs.append((matched, _status(graded.get("status"))))
    return pairs


def run(pdf: str | Path, out_dir: str | Path, doc_type: str = "", gate_path: str | Path | None = None,
        clarifications: list[dict] | None = None) -> dict:
    from vector_export import export_chunks

    pdf, out_dir = Path(pdf), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "requirements.jsonl"
    if gate_path is None:
        candidate = out_dir.parent / "gated" / "requirements.graded.jsonl"
        gate_path = candidate if candidate.exists() else None
    gate_rows = _gate_rows(gate_path)
    try:
        info = export_chunks(pdf, target, clarifications=clarifications)

        # gate 之后补图：ledger req_id → 导出 requirement id（按原文对齐）
        pairs = _gate_pairs(gate_path, Path(gate_path).parent / "requirements.csv" if gate_path else None, target)
        if pairs:
            from decision_graph import apply_gate, load as load_graph, save as save_graph

            graph = load_graph(info.get("source_sha256", ""))
            if graph is not None:
                apply_gate(graph, pairs)
                save_graph(graph)
                info["graph_edges"] = graph["counts"]["edges"]
                info["graph_gate_edges"] = len(pairs)
        return {"ok": True, "stats": info, "requirements": str(target)}
    except Exception as error:  # noqa: BLE001 - 知识沉淀失败不影响审查
        return {"ok": False, "fallback": f"{type(error).__name__}: {error}"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--doc-type", default="")
    parser.add_argument("--gate", default="", help="gated/requirements.graded.jsonl")
    args = parser.parse_args(argv)
    result = run(args.pdf, args.out, args.doc_type, gate_path=args.gate or None)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
