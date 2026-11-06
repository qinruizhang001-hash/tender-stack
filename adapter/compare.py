#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""compare.py — A/B 两臂对比：模型调用量、漏报率、语义条款保留率

漏报率口径（两条独立口径，避免自证）：
  1. 确定性条款口径：扫描层确认的硬性/废标条款（stable quotes），
     在最终台账里能否找到对应记录。这是方案 B 直接承诺的指标。
  2. 上游黄金集口径：rag-tender 自带的 tests/evaluation/sample_conditions.json，
     与本组合无关的第三方标注，作为非循环验证。

用法：
  python adapter/compare.py --a out/armA_real/gated --b out/armB_real/gated \
      --prescan out/prescan_real/known_issues.json --measure-a out/measure_A.json \
      --measure-b out/measure_B.json --run-a <runA_id> --run-b <runB_id> \
      --out out/compare_real.json
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from difflib import SequenceMatcher
from pathlib import Path


def norm(value: str) -> str:
    return re.sub(r"[\s\u3000，。、；：（）()【】\[\]“”\"'·,.;:!?\-—_/]", "", value or "")


def read_rows(path: Path) -> list[dict]:
    with (path / "requirements.csv").open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def covered(quote: str, rows: list[dict]) -> bool:
    target = norm(quote)
    if len(target) < 6:
        return False
    for row in rows:
        for field in ("source_quote", "requirement"):
            text = norm(row.get(field) or "")
            if not text:
                continue
            if target in text or text in target:
                return True
            # 长条款被拆成两条：用 60% 片段判定
            if len(target) >= 12:
                head = target[: len(target) // 2]
                if head in text:
                    return True
    return False


def covered_by_numbers(statement: str, rows: list[dict]) -> bool:
    """条款被拆成多行时的兜底核验：关键数字与名词都出现即认为已覆盖。"""
    numbers = re.findall(r"\d+(?:\.\d+)?", statement)
    if not numbers:
        return False
    blob = "".join(norm(row.get("requirement") or "") + norm(row.get("source_quote") or "") for row in rows)
    if not all(number in blob for number in numbers):
        return False
    nouns = [word for word in ["业绩", "资质", "营业执照", "保证金", "质保", "认证"] if word in statement]
    return all(noun in blob for noun in nouns)


def fuzzy_covered(quote: str, rows: list[dict], min_run: int = 12) -> bool:
    """逐字未命中、行号也对不上时的兜底：≥12 字连续重合即指向同一句。

    真实模型会把跨行残句补全成完整句，引文跨度与扫描层原文不同，
    只用逐字比对会把实际已入账的条款判成漏报。
    """
    needle = norm(quote)
    if len(needle) < min_run:
        return False
    for row in rows:
        for field in ("requirement", "source_quote"):
            hay = norm(row.get(field) or "")
            if len(hay) < min_run:
                continue
            match = SequenceMatcher(None, needle, hay, autojunk=False).find_longest_match(
                0, len(needle), 0, len(hay)
            )
            if match.size >= min_run:
                return True
    return False


def hit(item: dict, rows: list[dict], lines: set[int]) -> str:
    """三级判定：逐字引文 → 行号 → 最长公共子串。返回命中级别，未命中返回 ''。"""
    quote = item.get("quote") or ""
    if covered(quote, rows):
        return "quote"
    line = item.get("line")
    if line and int(line) in lines:
        return "line"
    if fuzzy_covered(quote, rows):
        return "fuzzy"
    return ""


def ledger_lines(rows: list[dict], only_llm: bool = False) -> set[int]:
    out: set[int] = set()
    for row in rows:
        if only_llm and "via=scan" in row["note"]:
            continue
        try:
            value = int(row.get("line_no") or -1)
        except (TypeError, ValueError):
            continue
        if value > 0:
            out.add(value)
    return out


def pick_run(measure_path: Path, n_requirements: int, run_id: str = "") -> dict:
    data = json.loads(measure_path.read_text(encoding="utf-8"))
    runs = data.get("runs") or []
    if not runs:
        return {}
    if run_id:
        for run in runs:
            if run["run_id"] == run_id or run["run_id"].startswith(run_id):
                return run
        return {}
    exact = [run for run in runs if run["requirements"] == n_requirements]
    return (exact or runs)[-1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--a", required=True)
    parser.add_argument("--b", required=True)
    parser.add_argument("--prescan", required=True)
    parser.add_argument("--measure-a", required=True)
    parser.add_argument("--measure-b", required=True)
    parser.add_argument("--golden", default="")
    parser.add_argument("--run-a", default="")
    parser.add_argument("--run-b", default="")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    rows_a = read_rows(Path(args.a))
    rows_b = read_rows(Path(args.b))
    prescan = json.loads(Path(args.prescan).read_text(encoding="utf-8"))
    items = prescan.get("by_quote") or []
    quotes = [item["quote"] for item in items]
    lines_a, lines_b = ledger_lines(rows_a), ledger_lines(rows_b)

    def judge(rows: list[dict], lines: set[int]) -> tuple[list[str], list[str]]:
        """返回 (逐字口径漏报, 三级口径漏报)。"""
        strict = [item["quote"] for item in items if not covered(item["quote"], rows)]
        graded = [item["quote"] for item in items if not hit(item, rows, lines)]
        return strict, graded

    miss_a_strict, miss_a = judge(rows_a, lines_a)
    miss_b_strict, miss_b = judge(rows_b, lines_b)

    result = {
        "documents": {"arm_a": args.a, "arm_b": args.b},
        "deterministic_golden": {
            "total": len(quotes),
            "arm_a_missed": len(miss_a),
            "arm_b_missed": len(miss_b),
            "arm_a_miss_rate": round(len(miss_a) / max(len(quotes), 1) * 100, 1),
            "arm_b_miss_rate": round(len(miss_b) / max(len(quotes), 1) * 100, 1),
            "arm_a_missed_strict": len(miss_a_strict),
            "arm_b_missed_strict": len(miss_b_strict),
            "arm_a_miss_rate_strict": round(
                len(miss_a_strict) / max(len(quotes), 1) * 100, 1
            ),
            "arm_b_miss_rate_strict": round(
                len(miss_b_strict) / max(len(quotes), 1) * 100, 1
            ),
            "criteria": "主口径：逐字引文 → 行号 → 最长公共子串(≥12字)，任一级命中即算覆盖；strict 为纯逐字口径",
            "arm_a_missed_samples": miss_a[:5],
            "arm_b_missed_samples": miss_b[:5],
        },
        "ledger": {
            "arm_a_total": len(rows_a),
            "arm_b_total": len(rows_b),
            "arm_a_llm": sum(1 for row in rows_a if "via=scan" not in row["note"]),
            "arm_b_llm": sum(1 for row in rows_b if "via=scan" not in row["note"]),
            "arm_b_scan": sum(1 for row in rows_b if "via=scan" in row["note"]),
        },
    }

    # 语义条款保留：A 臂中不被确定性黄金集覆盖的条目，在 B 臂是否仍在
    semantic_a = [
        row
        for row in rows_a
        if not any(covered(quote, [row]) for quote in quotes)
    ]
    kept = sum(1 for row in semantic_a if covered(row["source_quote"], rows_b))
    result["semantic_retention"] = {
        "arm_a_semantic_items": len(semantic_a),
        "kept_in_arm_b": kept,
        "retention_rate": round(kept / max(len(semantic_a), 1) * 100, 1),
        "note": "按引文字符串比对，真实模型会改写引文跨度，该值偏低；抗改写口径见 line_coverage。",
    }

    # 行号级覆盖：A 臂（纯模型）引用的行，B 臂**最终台账**是否也引用。
    # B 臂含扫描条目，若只统计 llm 行，会把"扫描层补上了"误算成丢失。
    lines_a, lines_b = ledger_lines(rows_a, True), ledger_lines(rows_b, False)
    if lines_a and lines_b:
        result["line_coverage"] = {
            "arm_a_lines": len(lines_a),
            "arm_b_lines": len(lines_b),
            "shared": len(lines_a & lines_b),
            "retention_rate": round(len(lines_a & lines_b) / len(lines_a) * 100, 1),
            "arm_b_only": len(lines_b - lines_a),
        }

    # 上游独立黄金集（可选）
    if args.golden and Path(args.golden).exists():
        golden = json.loads(Path(args.golden).read_text(encoding="utf-8"))
        items = golden.get("requirements") or []
        miss_a_g = [item["statement"] for item in items if not covered(item["statement"], rows_a)]
        miss_b_g = [item["statement"] for item in items if not covered(item["statement"], rows_b)]
        # 拆条兜底：严格匹配未命中，但关键数字/名词都在台账里 → 判为"拆条未漏"
        detail = []
        for item in items:
            statement = item["statement"]
            if not covered(statement, rows_a) and not covered(statement, rows_b):
                detail.append(
                    {
                        "statement": statement,
                        "numeric_covered_in_a": covered_by_numbers(statement, rows_a),
                        "numeric_covered_in_b": covered_by_numbers(statement, rows_b),
                    }
                )
        result["upstream_golden"] = {
            "source": str(args.golden),
            "total": len(items),
            "arm_a_missed": len(miss_a_g),
            "arm_b_missed": len(miss_b_g),
            "arm_a_miss_rate": round(len(miss_a_g) / max(len(items), 1) * 100, 1),
            "arm_b_miss_rate": round(len(miss_b_g) / max(len(items), 1) * 100, 1),
            "missed_items": {"arm_a": miss_a_g, "arm_b": miss_b_g},
            "split_clause_detail": detail,
            "note": "严格匹配未命中但 numeric_covered=true 的条目，实为同一条款被拆成多行，不算漏报。",
        }

    # 调用量
    run_a = pick_run(Path(args.measure_a), len(rows_a), args.run_a)
    run_b = pick_run(Path(args.measure_b), len(rows_b), args.run_b)
    if run_a and run_b:
        def delta(key: str) -> dict:
            before, after = run_a.get(key, 0), run_b.get(key, 0)
            return {
                "arm_a": before,
                "arm_b": after,
                "delta": after - before,
                "change_pct": round((after - before) / before * 100, 1) if before else None,
            }

        result["model_cost"] = {
            "calls": delta("calls"),
            "extract_calls": delta("calls_extract"),
            "prompt_tokens": delta("prompt_tokens"),
            "completion_tokens": delta("completion_tokens"),
            "requirements": delta("requirements"),
        }

    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"{'指标':<28}{'A 臂(仅模型)':>14}{'B 臂(扫描+模型)':>18}{'变化':>12}")
    print("-" * 74)
    golden = result["deterministic_golden"]
    print(f"{'确定性条款漏报率':<26}{golden['arm_a_miss_rate']:>13.1f}%{golden['arm_b_miss_rate']:>17.1f}%{golden['arm_b_miss_rate'] - golden['arm_a_miss_rate']:>11.1f}pp")
    print(f"{'台账条目数':<28}{len(rows_a):>13}{len(rows_b):>18}{len(rows_b) - len(rows_a):>+12}")
    if "model_cost" in result:
        cost = result["model_cost"]
        print(f"{'模型调用次数':<28}{cost['calls']['arm_a']:>13}{cost['calls']['arm_b']:>18}{str(cost['calls']['change_pct']) + '%':>12}")
        print(f"{'提取调用次数':<28}{cost['extract_calls']['arm_a']:>13}{cost['extract_calls']['arm_b']:>18}{str(cost['extract_calls']['change_pct']) + '%':>12}")
        print(f"{'输入 token':<28}{cost['prompt_tokens']['arm_a']:>13}{cost['prompt_tokens']['arm_b']:>18}{str(cost['prompt_tokens']['change_pct']) + '%':>12}")
        print(f"{'输出 token':<28}{cost['completion_tokens']['arm_a']:>13}{cost['completion_tokens']['arm_b']:>18}{str(cost['completion_tokens']['change_pct']) + '%':>12}")
    if "upstream_golden" in result:
        up = result["upstream_golden"]
        print(f"{'上游黄金集漏报率':<26}{up['arm_a_miss_rate']:>13.1f}%{up['arm_b_miss_rate']:>17.1f}%{up['arm_b_miss_rate'] - up['arm_a_miss_rate']:>11.1f}pp")
    semantic = result["semantic_retention"]
    print(f"{'语义条款保留率(引文口径)':<26}{'—':>13}{str(semantic['retention_rate']) + '%':>18}{'':>12}")
    if "line_coverage" in result:
        lc = result["line_coverage"]
        print(
            f"{'行号级保留率(抗改写)':<27}{lc['arm_a_lines']:>12}{str(lc['retention_rate']) + '%':>18}{'':>12}"
        )


if __name__ == "__main__":
    main()
