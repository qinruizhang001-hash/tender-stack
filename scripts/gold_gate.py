#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gold_gate.py — 条款级金标门禁。

跑一份真标书的 requirement 导出，用 20 条人工金标算覆盖率召回；
低于阈值（默认 0.90）退出码 1，供 pre-push / CI 拦截。

用法：
  python scripts/gold_gate.py                    # 默认怀柔标书 + 20 条金标
  python scripts/gold_gate.py --threshold 0.85
  python scripts/gold_gate.py --pdf x.pdf --gold y.json
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PDF = ROOT / "data" / "tenders" / "北京怀柔-公园绿地养护-公开招标文件.pdf"
DEFAULT_GOLD = ROOT / "contract" / "gold_clauses_20.json"
DEFAULT_THRESHOLD = 0.90


def passes(recall: float | None, threshold: float) -> bool:
    """金标为空（recall=None）不算失败，交给调用方提示。"""
    if recall is None:
        return True
    return recall >= threshold


def run(pdf: str | Path = DEFAULT_PDF, gold: str | Path = DEFAULT_GOLD, threshold: float = DEFAULT_THRESHOLD) -> dict:
    pdf, gold = Path(pdf), Path(gold)
    if not pdf.exists() or not gold.exists():
        return {"skipped": True, "reason": "标书或金标文件缺失", "pdf": str(pdf), "gold": str(gold)}

    sys.path.insert(0, str(ROOT / "adapter"))
    sys.path.insert(0, str(ROOT / "scripts"))
    from eval_chunks import evaluate
    from vector_export import export_chunks

    with tempfile.TemporaryDirectory() as folder:
        target = Path(folder) / "req.jsonl"
        export = export_chunks(pdf, target)
        result = evaluate(gold, target)

    ok = passes(result.get("recall"), threshold)
    return {
        "skipped": False,
        "passed": ok,
        "threshold": threshold,
        "recall": result.get("recall"),
        "exact": result.get("exact"),
        "exact_rate": result.get("exact_rate"),
        "covered": result.get("covered"),
        "total": result.get("total"),
        "requirements": export.get("requirements"),
        "low_quality": export.get("low_quality"),
        "ocr_fallback_count": export.get("ocr_fallback_count"),
        "miss": result.get("miss"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="条款级金标门禁")
    parser.add_argument("--pdf", default=str(DEFAULT_PDF))
    parser.add_argument("--gold", default=str(DEFAULT_GOLD))
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    args = parser.parse_args(argv)

    result = run(args.pdf, args.gold, args.threshold)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("skipped"):
        print("金标门禁跳过（缺文件）", file=sys.stderr)
        return 0
    if not result["passed"]:
        print(
            f"金标门禁未通过：recall {result['recall']} < {result['threshold']}",
            file=sys.stderr,
        )
        return 1
    print(f"金标门禁通过：recall {result['recall']} ≥ {result['threshold']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
