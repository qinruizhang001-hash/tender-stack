#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gold_eval.py — 用人工标注的 50 条黄金集评估某一臂的「捞全 / 捞错」

黄金集（contract/gold_50.jsonl）是从一条基准运行的台账里分层抽样的：
  ONLY   = 只在这条运行里出现的条目（用来判断“多出来的”是不是真要求）
  SHARED = 与其他运行重叠的条目（用来判断“共同的”是否稳定）
每条人工标注：
  Y = 真要求（对投标人有约束、影响废标/评分/履约）
  B = 边界（信息性、条件性、弱义务，可用但价值低）
  N = 不是要求（纯描述、评标规则、交叉引用等）

对任意一臂：
  recall_y   = 该臂覆盖了多少条 Y      → 捞全率
  wrong_n    = 该臂把多少条 N 也当成要求 → 捞错
  cover_b    = 该臂覆盖了多少条 B

用法：
  python adapter/gold_eval.py --ledger out/armC_v2/ledger --gold contract/gold_50.jsonl
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path


def norm(text: str) -> str:
    return re.sub(r"[\s，。；：、（）()【】\[\]“”\"'‘’]", "", text or "")


def read_rows(folder: Path) -> list[dict]:
    for candidate in (folder / "gated/requirements.csv", folder / "ledger/requirements.csv", folder / "requirements.csv"):
        if candidate.exists():
            with candidate.open(encoding="utf-8-sig") as handle:
                return list(csv.DictReader(handle))
    raise SystemExit(f"找不到 requirements.csv：{folder}")


def covered(needle: str, rows: list[dict], min_run: int = 12) -> bool:
    """黄金条目是否被这一臂覆盖：包含关系，或 ≥12 字连续重合。"""
    if len(needle) < 4:
        return False
    for row in rows:
        for field in ("requirement", "source_quote"):
            hay = norm(row.get(field) or "")
            if len(hay) < 4:
                continue
            if needle in hay or hay in needle:
                return True
            if len(needle) >= min_run and len(hay) >= min_run:
                match = SequenceMatcher(None, needle, hay, autojunk=False).find_longest_match(
                    0, len(needle), 0, len(hay)
                )
                if match.size >= min_run:
                    return True
    return False


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", required=True, help="含 requirements.csv 的目录（gated/ 或 ledger/）")
    parser.add_argument("--gold", default="contract/gold_50.jsonl")
    parser.add_argument("--out", default="", help="可选：写出 JSON 结果")
    args = parser.parse_args()

    gold = [json.loads(line) for line in Path(args.gold).read_text(encoding="utf-8").splitlines() if line.strip()]
    rows = read_rows(Path(args.ledger))

    result = {
        "ledger": args.ledger,
        "rows": len(rows),
        "by_stratum": {},
        "by_label": dict(Counter(item["label"] for item in gold)),
        "missed_y": [],
    }

    for stratum in ("ONLY", "SHARED"):
        items = [item for item in gold if item["stratum"] == stratum]
        if not items:
            continue
        hit = {"Y": 0, "B": 0, "N": 0}
        total = {"Y": 0, "B": 0, "N": 0}
        for item in items:
            label = item["label"]
            total[label] += 1
            if covered(norm(item["text"]), rows):
                hit[label] += 1
                if label == "Y":
                    pass
            elif label == "Y":
                result["missed_y"].append({"id": item["id"], "req_id": item["req_id"], "text": item["text"][:80]})
        result["by_stratum"][stratum] = {"total": total, "hit": hit}

    y_total = sum(v["total"]["Y"] for v in result["by_stratum"].values())
    y_hit = sum(v["hit"]["Y"] for v in result["by_stratum"].values())
    n_total = sum(v["total"]["N"] for v in result["by_stratum"].values())
    n_hit = sum(v["hit"]["N"] for v in result["by_stratum"].values())
    b_total = sum(v["total"]["B"] for v in result["by_stratum"].values())
    b_hit = sum(v["hit"]["B"] for v in result["by_stratum"].values())

    result["summary"] = {
        "recall_y": round(y_hit / max(y_total, 1) * 100, 1),
        "wrong_n": n_hit,
        "wrong_n_total": n_total,
        "cover_b": round(b_hit / max(b_total, 1) * 100, 1),
        "y_hit": y_hit,
        "y_total": y_total,
    }

    print(f"台账 {len(rows)} 条 | 覆盖真要求 {y_hit}/{y_total} = {result['summary']['recall_y']}% "
          f"| 误收非要求 {n_hit}/{n_total} | 边界覆盖 {b_hit}/{b_total}")
    for stratum, data in result["by_stratum"].items():
        t, h = data["total"], data["hit"]
        print(f"  {stratum:6s} Y {h['Y']}/{t['Y']}  B {h['B']}/{t['B']}  N {h['N']}/{t['N']}")
    if result["missed_y"]:
        print("  漏掉的真要求：")
        for item in result["missed_y"][:6]:
            print(f"    {item['id']} {item['text']}")

    if args.out:
        Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
