#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""validate_ledger.py — 逐列校验台账是否真正符合 ai-bid-document 契约

不只看"有没有值"，还看：必填是否为空、枚举是否越界、格式是否合规、
溯源旁挂是否齐、quote 是否能在原文块里逐字找到。

用法：
  python adapter/validate_ledger.py --ledger out/armA_real/gated --run out/armA_real \
      --contract contract
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--contract", default="contract")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    ledger_dir = Path(args.ledger)
    schema = json.loads((Path(args.contract) / "ledger_schema.json").read_text(encoding="utf-8"))
    with (ledger_dir / "requirements.csv").open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        header = list(reader.fieldnames or [])

    blocks = {
        block["block_id"]: block.get("text", "")
        for block in json.loads((Path(args.run) / "blocks.json").read_text(encoding="utf-8"))
    }
    provenance = {}
    prov_path = ledger_dir / "requirements.provenance.jsonl"
    if not prov_path.exists():
        # 兜底：映射层原始输出目录
        candidate = Path(args.run) / "ledger" / "requirements.provenance.jsonl"
        if candidate.exists():
            prov_path = candidate
    if prov_path.exists():
        for line in prov_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                item = json.loads(line)
                provenance[item["req_id"]] = item

    expected = [column["name"] for column in schema["columns"]]
    column_report = []

    # 1) 列结构完全一致（顺序也要一致）
    structure_ok = header == expected
    column_report.append(
        {
            "check": "列结构",
            "target": " | ".join(expected),
            "pass": structure_ok,
            "detail": "列名与顺序完全一致" if structure_ok else f"实际列: {header}",
        }
    )

    for column in schema["columns"]:
        name = column["name"]
        values = [row.get(name, "") for row in rows]
        filled = [value for value in values if str(value).strip()]
        detail = f"{len(filled)}/{len(rows)} 非空"
        ok = True

        if column.get("required") and len(filled) != len(rows):
            ok = False
            detail += "（必填有空值）"

        if column["type"] == "enum" and filled:
            allowed = set(column["enum"])
            bad = [value for value in filled if value not in allowed]
            if bad:
                ok = False
                detail += f"（越界值: {sorted(set(bad))[:5]}）"

        if column["type"] == "integer" and filled:
            bad = [value for value in filled if not re.fullmatch(r"-?\d+", str(value))]
            if bad:
                ok = False
                detail += f"（非整数: {bad[:3]}）"

        if column.get("pattern") and filled:
            bad = [value for value in filled if not re.fullmatch(column["pattern"], str(value))]
            if bad:
                ok = False
                detail += f"（格式不符: {bad[:3]}）"

        column_report.append({"check": f"列 {column['index']:>2} {name}", "pass": ok, "detail": detail})

    # 2) 溯源：block_id + quote 是否齐、quote 是否逐字
    block_ok, quote_ok, verbatim_ok = 0, 0, 0
    for row in rows:
        req_id = row["req_id"]
        prov = provenance.get(req_id, {})
        block_id = prov.get("block_id") or ""
        if block_id:
            block_ok += 1
        if row["source_quote"].strip():
            quote_ok += 1
        if block_id and row["source_quote"] and row["source_quote"] in blocks.get(block_id, ""):
            verbatim_ok += 1

    column_report.append(
        {
            "check": "溯源 block_id",
            "pass": block_ok == len(rows),
            "detail": f"{block_ok}/{len(rows)} 条有 block_id",
        }
    )
    column_report.append(
        {
            "check": "溯源 quote",
            "pass": quote_ok == len(rows),
            "detail": f"{quote_ok}/{len(rows)} 条有 source_quote",
        }
    )
    column_report.append(
        {
            "check": "quote 逐字可回查",
            "pass": verbatim_ok == len(rows),
            "detail": f"{verbatim_ok}/{len(rows)} 条 quote 能在 block 原文中逐字命中",
        }
    )

    passed = all(item["pass"] for item in column_report)
    result = {
        "passed": passed,
        "total_records": len(rows),
        "checks": column_report,
        "failed": [item["check"] for item in column_report if not item["pass"]],
    }

    if args.out:
        Path(args.out).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    width = max(len(item["check"]) for item in column_report)
    print(f"{'列/检查项'.ljust(width)}  结果   明细")
    print("-" * (width + 40))
    for item in column_report:
        mark = "PASS" if item["pass"] else "FAIL"
        print(f"{item['check'].ljust(width)}  {mark}   {item['detail']}")
    print("-" * (width + 40))
    print(f"总体：{'通过' if passed else '不通过'}（{len(rows)} 条记录）")


if __name__ == "__main__":
    main()
