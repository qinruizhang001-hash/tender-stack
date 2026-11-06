#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""measure.py — 按 run 统计模型调用量与产物规模

只以**只读方式**打开 rag-tender 的 SQLite（不写、不改、不 import 它的代码），
用 model_calls 表 + jobs.payload 里的 run_id 归集到每一次分析。

用法：
  python adapter/measure.py --db data/rt_A/workbench.db --out out/measure_A.json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path


def measure(
    db_path: Path,
    fallback_run: str = "",
    fallback_since: str = "",
) -> dict:
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row

    runs = {}
    for row in connection.execute(
        "SELECT id, project_id, status, stage, coverage_notes FROM analysis_runs"
    ):
        runs[row["id"]] = {
            "run_id": row["id"],
            "project_id": row["project_id"],
            "status": row["status"],
            "stage": row["stage"],
            "coverage_notes": json.loads(row["coverage_notes"] or "[]"),
            "calls": 0,
            "calls_extract": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "by_purpose": Counter(),
        }

    # 文档 → 项目（提取阶段的调用挂在 process_document 任务上，payload 里是 document_id）
    doc_project = {
        row["id"]: row["project_id"]
        for row in connection.execute("SELECT id, project_id FROM documents")
    }
    # 项目 → 最近一次 run（每个项目只跑一次分析）
    project_run: dict[str, str] = {}
    for row in connection.execute(
        "SELECT id, project_id, created_at FROM analysis_runs ORDER BY created_at"
    ):
        project_run[row["project_id"]] = row["id"]

    unattributed = Counter()
    for row in connection.execute(
        """
        SELECT m.purpose, m.input_tokens, m.output_tokens, m.created_at, j.payload
        FROM model_calls m LEFT JOIN jobs j ON j.id = m.job_id
        """
    ):
        payload = {}
        if row["payload"]:
            try:
                payload = json.loads(row["payload"])
            except json.JSONDecodeError:
                payload = {}
        run_id = payload.get("run_id")
        if run_id not in runs:
            project_id = doc_project.get(payload.get("document_id") or "")
            run_id = project_run.get(project_id or "")
        if run_id not in runs and fallback_run in runs:
            # 直调提取（job_id 为空）没有 payload，用时间窗归集到指定 run
            if not fallback_since or (row["created_at"] or "") >= fallback_since:
                run_id = fallback_run
        if run_id not in runs:
            unattributed[row["purpose"]] += 1
            continue
        item = runs[run_id]
        item["calls"] += 1
        item["by_purpose"][row["purpose"]] += 1
        if row["purpose"] in {"extract_requirements", "extract_document_requirements"}:
            item["calls_extract"] += 1
        item["prompt_tokens"] += row["input_tokens"] or 0
        item["completion_tokens"] += row["output_tokens"] or 0

    for run_id, item in runs.items():
        count = connection.execute(
            "SELECT COUNT(*) FROM requirements WHERE run_id = ?", (run_id,)
        ).fetchone()[0]
        item["requirements"] = count
        item["by_purpose"] = dict(item["by_purpose"])

    connection.close()
    return {"db": str(db_path), "runs": list(runs.values()), "unattributed": dict(unattributed)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument(
        "--fallback-run",
        default="",
        help="直调提取产生的调用没有 job.payload，可归集到该 run（配合 --fallback-since）",
    )
    parser.add_argument("--fallback-since", default="", help="归集时间下界，如 '2026-11-02 14:51'")
    args = parser.parse_args()

    result = measure(Path(args.db), args.fallback_run, args.fallback_since)
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    for item in result["runs"]:
        print(
            f"{item['run_id'][:8]} status={item['status']:<12} "
            f"calls={item['calls']:<4} extract={item['calls_extract']:<4} "
            f"in_tok={item['prompt_tokens']:<8} out_tok={item['completion_tokens']:<7} "
            f"reqs={item['requirements']:<4} purposes={item['by_purpose']}"
        )


if __name__ == "__main__":
    main()
