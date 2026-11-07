#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gold_gate.py — 多文档条款级金标门禁。

默认读 `contract/gold/index.json`，对每份标书跑 requirement 导出 + 金标评估：

  - 总 recall（按条款数加权）≥ 0.90
  - 最差单文档 recall ≥ 0.85
  - miss 里出现「废标 / 否决 / 必须 / ★」→ 直接失败（即使 recall 达标）

单文档模式：`--pdf x.pdf --gold y.json`。
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INDEX = ROOT / "contract" / "gold" / "index.json"
DEFAULT_PDF = ROOT / "data" / "tenders" / "北京怀柔-公园绿地养护-公开招标文件.pdf"
DEFAULT_GOLD = ROOT / "contract" / "gold_clauses_20.json"
TOTAL_THRESHOLD = 0.90
WORST_THRESHOLD = 0.85
RISK_WORDS = ("废标", "否决", "必须", "★")


def passes(recall: float | None, threshold: float) -> bool:
    if recall is None:
        return True
    return recall >= threshold


def verdict(results: list[dict], total_threshold: float = TOTAL_THRESHOLD, worst_threshold: float = WORST_THRESHOLD) -> dict:
    """results: [{doc, recall, total, covered, miss:[...]}]"""
    scored = [item for item in results if item.get("recall") is not None]
    if not scored:
        return {"passed": True, "skipped": True, "reason": "没有可用金标"}

    covered = sum(item["covered"] for item in scored)
    total = sum(item["total"] for item in scored)
    overall = round(covered / total, 3) if total else None
    worst = min(item["recall"] for item in scored)
    risk = [
        {"doc": item["doc"], "id": miss.get("id"), "text": miss.get("text", "")[:80]}
        for item in scored
        for miss in item.get("miss", [])
        if any(word in (miss.get("text") or "") for word in RISK_WORDS)
    ]

    passed = (overall is None or overall >= total_threshold) and worst >= worst_threshold and not risk
    return {
        "passed": passed,
        "skipped": False,
        "overall_recall": overall,
        "worst_recall": worst,
        "total_threshold": total_threshold,
        "worst_threshold": worst_threshold,
        "covered": covered,
        "total": total,
        "risk_miss": risk,
        "docs": [
            {"doc": item["doc"], "recall": item["recall"], "total": item["total"], "covered": item["covered"],
             "exact": item.get("exact"), "exact_display": item.get("exact_display")}
            for item in scored
        ],
    }


def _evaluate_doc(pdf: Path, gold: Path) -> dict:
    sys.path.insert(0, str(ROOT / "adapter"))
    sys.path.insert(0, str(ROOT / "scripts"))
    from eval_chunks import evaluate
    from vector_export import export_chunks

    with tempfile.TemporaryDirectory() as folder:
        target = Path(folder) / "req.jsonl"
        export = export_chunks(pdf, target, absorb=False, build_graph=False)
        result = evaluate(gold, target)
    return {"export": export, "eval": result}


def run_index(
    index_path: str | Path = DEFAULT_INDEX,
    total_threshold: float = TOTAL_THRESHOLD,
    worst_threshold: float = WORST_THRESHOLD,
) -> dict:
    index_path = Path(index_path)
    if not index_path.exists():
        return {"skipped": True, "reason": "index.json 缺失", "index": str(index_path)}

    entries = json.loads(index_path.read_text(encoding="utf-8"))
    results: list[dict] = []
    for entry in entries:
        pdf = ROOT / entry["pdf"]
        gold = ROOT / entry["gold"]
        if not pdf.exists() or not gold.exists():
            results.append({"doc": entry["doc"], "recall": None, "total": 0, "covered": 0, "miss": [],
                            "note": "文件缺失"})
            continue
        outcome = _evaluate_doc(pdf, gold)
        evaluation = outcome["eval"]
        results.append(
            {
                "doc": entry["doc"],
                "recall": evaluation["recall"],
                "total": evaluation["total"],
                "covered": evaluation["covered"],
                "exact": evaluation["exact"],
                "exact_display": evaluation["exact_display"],
                "miss": evaluation["miss"],
                "requirements": outcome["export"].get("requirements"),
                "table_rows": outcome["export"].get("table_rows"),
                "low_quality": outcome["export"].get("low_quality"),
                "ocr_fallback_count": outcome["export"].get("ocr_fallback_count"),
            }
        )

    summary = verdict(results, total_threshold, worst_threshold)
    summary["results"] = results
    return summary


def run(pdf: str | Path = DEFAULT_PDF, gold: str | Path = DEFAULT_GOLD, threshold: float = TOTAL_THRESHOLD) -> dict:
    pdf, gold = Path(pdf), Path(gold)
    if not pdf.exists() or not gold.exists():
        return {"skipped": True, "reason": "标书或金标文件缺失", "pdf": str(pdf), "gold": str(gold)}
    outcome = _evaluate_doc(pdf, gold)
    evaluation = outcome["eval"]
    return {
        "skipped": False,
        "passed": passes(evaluation.get("recall"), threshold),
        "threshold": threshold,
        "recall": evaluation.get("recall"),
        "exact": evaluation.get("exact"),
        "exact_display": evaluation.get("exact_display"),
        "covered": evaluation.get("covered"),
        "total": evaluation.get("total"),
        "requirements": outcome["export"].get("requirements"),
        "low_quality": outcome["export"].get("low_quality"),
        "ocr_fallback_count": outcome["export"].get("ocr_fallback_count"),
        "miss": evaluation.get("miss"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="条款级金标门禁")
    parser.add_argument("--index", default=str(DEFAULT_INDEX))
    parser.add_argument("--pdf")
    parser.add_argument("--gold")
    parser.add_argument("--threshold", type=float, default=TOTAL_THRESHOLD)
    parser.add_argument("--worst-threshold", type=float, default=WORST_THRESHOLD)
    args = parser.parse_args(argv)

    if args.pdf and args.gold:
        result = run(args.pdf, args.gold, args.threshold)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("passed") or result.get("skipped") else 1

    summary = run_index(args.index, args.threshold, args.worst_threshold)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if summary.get("skipped"):
        print("金标门禁跳过", file=sys.stderr)
        return 0
    if not summary["passed"]:
        reasons = []
        if summary.get("overall_recall") is not None and summary["overall_recall"] < summary["total_threshold"]:
            reasons.append(f"总 recall {summary['overall_recall']} < {summary['total_threshold']}")
        if summary["worst_recall"] < summary["worst_threshold"]:
            reasons.append(f"最差文档 {summary['worst_recall']} < {summary['worst_threshold']}")
        if summary.get("risk_miss"):
            reasons.append(f"风险条款漏检 {len(summary['risk_miss'])} 条")
        print("金标门禁未通过：" + "；".join(reasons), file=sys.stderr)
        return 1
    print(
        f"金标门禁通过：总 {summary['overall_recall']} / 最差 {summary['worst_recall']}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
