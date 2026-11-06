#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""golden_check.py — 单臂对确定性黄金集的覆盖率

A/B 对比需要两臂；但真实模型复跑真实标书成本高，单臂也需要一个
"扫描层确认过的条款，这一臂捞到了没有"的绝对口径。

用法：
  python adapter/golden_check.py --ledger out/armC_real/gated \
      --prescan out/prescan_real/known_issues.json --out out/golden_C_real.json
"""
from __future__ import annotations

import argparse
import json
import re
from difflib import SequenceMatcher
from pathlib import Path

from compare import covered, covered_by_numbers, read_rows


def normalize(text: str) -> str:
    return re.sub(r"[\s，。；：、（）()【】\[\]“”\"'‘’]", "", text or "")


def fuzzy_covered(quote: str, rows: list[dict], min_run: int = 12) -> bool:
    """逐字未命中、行号也对不上时，用最长公共子串兜底。

    真实模型会改写引文跨度（补全跨行残句、合并相邻句），逐字比对会把
    实际已入账的条款判成漏报。≥12 字连续重合即认为指向同一句。
    """
    needle = normalize(quote)
    if len(needle) < min_run:
        return False
    for row in rows:
        for field in ("requirement", "source_quote"):
            hay = normalize(row.get(field) or "")
            if len(hay) < min_run:
                continue
            match = SequenceMatcher(None, needle, hay, autojunk=False).find_longest_match(
                0, len(needle), 0, len(hay)
            )
            if match.size >= min_run:
                return True
    return False


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", required=True, help="含 requirements.csv 的目录")
    parser.add_argument("--prescan", required=True, help="known_issues.json")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    rows = read_rows(Path(args.ledger))
    prescan = json.loads(Path(args.prescan).read_text(encoding="utf-8"))
    items = prescan.get("by_quote") or []
    quotes = [item["quote"] for item in items]

    # 台账已落到行号：同一行被引用即视为命中（真实模型会改写引文跨度，逐字比对会高估漏报）
    ledger_lines = set()
    for row in rows:
        try:
            value = int(row.get("line_no") or -1)
        except (TypeError, ValueError):
            continue
        if value > 0:
            ledger_lines.add(value)

    missed = []
    for item in items:
        quote = item["quote"]
        if covered(quote, rows):
            continue
        line = item.get("line")
        if line and int(line) in ledger_lines:
            continue
        if fuzzy_covered(quote, rows):
            continue
        missed.append(quote)

    # 兜底：严格引文未命中，但关键数字都在台账里 → 视为拆条命中
    rescued = [quote for quote in missed if covered_by_numbers(quote, rows)]

    result = {
        "ledger": args.ledger,
        "prescan": args.prescan,
        "golden_total": len(quotes),
        "missed": len(missed),
        "miss_rate": round(len(missed) / max(len(quotes), 1) * 100, 1),
        "missed_numeric_rescued": len(rescued),
        "miss_rate_after_rescue": round(
            (len(missed) - len(rescued)) / max(len(quotes), 1) * 100, 1
        ),
        "ledger_lines": len(ledger_lines),
        "missed_samples": missed[:10],
        "ledger_rows": len(rows),
        "ledger_llm": sum(1 for row in rows if "via=scan" not in row["note"]),
        "criteria": "逐字引文 → 行号 → 最长公共子串(≥12字) 三级判定，任一级命中即算覆盖",
    }
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        f"黄金集 {result['golden_total']} 条 | 漏 {result['missed']} 条 "
        f"({result['miss_rate']}%) | 拆条兜底后 {result['miss_rate_after_rescue']}% "
        f"| 台账 {result['ledger_rows']} 条（模型 {result['ledger_llm']}）"
    )
    for quote in result["missed_samples"]:
        print("  漏:", quote[:70].replace("\n", " "))


if __name__ == "__main__":
    main()
