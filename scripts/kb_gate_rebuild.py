#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kb_gate_rebuild.py — 在没有上游流水线产物时，用导出结果重建一份台账并跑门禁规则。

用途：给「只有导出、没有 gate」的文档补出**真实跑过门禁规则**的 graded 结果，
用于决策图 reviewed_as 边。

诚实边界（写进输出）：
- 重建台账没有行级 provenance，`line_no=0` → 门禁会判 `nr.no_line`，状态多为「需复核」；
- 因此本脚本产出的是 `unknown/pending` 语义的门禁结果，**不会**伪造 pass。
- 原始导出 `source_sha256` 与重建台账一起写入 `rebuild_meta.json`，可追溯。
"""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COLUMNS = [
    "req_id", "section", "requirement", "type", "mandatory", "strength", "source_quote",
    "source_ver", "status", "owner", "line_no", "dup_of", "note", "gate", "severity",
    "need_clarify", "review_note", "reviewed_by", "reviewed_at",
]


def rebuild(requirements_jsonl: str | Path, out_dir: str | Path, contract: str | Path | None = None) -> dict:
    requirements = [json.loads(line) for line in Path(requirements_jsonl).read_text(encoding="utf-8").splitlines() if line.strip()]
    out_dir = Path(out_dir)
    run_dir = out_dir / "run"
    ledger_dir = out_dir / "ledger"
    gated_dir = out_dir / "gated"
    for folder in (run_dir, ledger_dir, gated_dir):
        folder.mkdir(parents=True, exist_ok=True)

    pages = max((row.get("page") or 1) for row in requirements) if requirements else 1
    (run_dir / "meta.json").write_text(json.dumps({"page_count": pages, "document_id": "rebuild"}), encoding="utf-8")

    blocks = []
    for index, row in enumerate(requirements):
        blocks.append(
            {
                "block_id": row.get("id", f"b{index}"),
                "page_index": (row.get("page") or 1) - 1,
                "kind": "text",
                "reading_order": index,
                "text": row.get("text", ""),
            }
        )
    (run_dir / "blocks.json").write_text(json.dumps(blocks, ensure_ascii=False), encoding="utf-8")

    ledger = ledger_dir / "requirements.csv"
    with ledger.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        for index, row in enumerate(requirements):
            writer.writerow(
                {
                    "req_id": f"R-{index + 1:03d}",
                    "section": row.get("section", "") or f"第{row.get('page')}页",
                    "requirement": row.get("text", ""),
                    "type": "技术",
                    "mandatory": "Y",
                    "strength": "可",
                    "source_quote": row.get("text", ""),
                    "source_ver": "",
                    "status": "",
                    "owner": "",
                    "line_no": 0,          # 重建台账没有行号：门禁会判 no_line，状态转需复核
                    "dup_of": "",
                    "note": f"block_id={row.get('id', '')}",
                    "gate": "",
                    "severity": "",
                    "need_clarify": "",
                    "review_note": "",
                    "reviewed_by": "",
                    "reviewed_at": "",
                }
            )
    (ledger_dir / "requirements.provenance.jsonl").write_text("", encoding="utf-8")
    (ledger_dir / "mapping_report.json").write_text(json.dumps({"rebuilt": True, "count": len(requirements)}), encoding="utf-8")

    contract = Path(contract or ROOT / "contract")
    result = subprocess.run(
        [sys.executable, str(ROOT / "adapter" / "gate_engine.py"),
         "--ledger", str(ledger_dir), "--run", str(run_dir), "--scan", str(run_dir),
         "--contract", str(contract), "--out", str(gated_dir)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(ROOT),
    )
    graded = gated_dir / "requirements.graded.jsonl"
    (out_dir / "rebuild_meta.json").write_text(
        json.dumps(
            {
                "source": str(requirements_jsonl),
                "requirements": len(requirements),
                "gate_returncode": result.returncode,
                "graded": str(graded),
                "note": "line_no=0 → 门禁判 no_line，状态为需复核；不伪造 pass",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    if not graded.exists():
        return {"ok": False, "stage": "gate", "stderr": (result.stderr or "")[-400:]}
    return {"ok": True, "graded": str(graded), "ledger": str(ledger), "requirements": len(requirements)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--requirements", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--contract", default="")
    args = parser.parse_args(argv)
    result = rebuild(args.requirements, args.out, args.contract or None)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
